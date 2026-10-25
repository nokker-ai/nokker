"""API locale de Nokker Lokkal (docs/LOKKAL.md §7) : jeton local, contrôle du `Host`, sessions de code.

Sur la machine de l'utilisateur, le noyau sert la même API que la bêta, plus `/v1/local/*`. Trois protections, parce
qu'une page web ouverte dans le navigateur peut joindre 127.0.0.1 (CSRF, DNS rebinding) :
  1. le **jeton local**, écrit au démarrage dans `token_path` (0600, lisible par l'utilisateur seulement) et exigé en
     `Authorization: Bearer` : c'est l'identité « propriétaire local » (`LOCAL_OWNER`), distincte de toute clé d'API ;
  2. le **`Host`** de chaque requête doit être dans `allowed_hosts` (127.0.0.1 / localhost au port servi) : une page
     d'un autre domaine qui fait pointer son nom sur 127.0.0.1 est refusée ;
  3. le port n'est publié que sur 127.0.0.1 de l'hôte (compose : `127.0.0.1:<port>:<port>`). Dans le container, le pair
     est la passerelle docker, pas 127.0.0.1 : l'adresse du pair n'est donc PAS un contrôle ici.

Sessions de code : `POST /v1/local/sessions {cwd}` ouvre un fil `th_…` dont les outils de code travaillent dans `cwd`
(sous une des `roots` déclarées, sinon 403) ; `SessionCodeTools` route chaque appel vers la `CodeToolSource` du fil.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import ToolResult
import httpx

from nokker.local.setup import LocalLink, LocalSetup, LocalVault, VaultError
from nokker.secrets.base import SecretsError
from nokker.secrets.vault import Vault
from nokker.tools.code import DEFAULT_SANDBOX_IMAGE, CodeToolSource

LOCAL_OWNER = "local-owner"
logger = logging.getLogger("nokker.local.api")
# Champs ajoutés à chaque ligne de `GET /v1/local/sessions` (§7.4.1), avec leur valeur quand rien n'est connu.
SESSION_DEFAULTS: dict[str, Any] = {"title": None, "last_turn_at": None, "working": False, "pending_approvals": 0}


def _chosen_thread(body: Any) -> tuple[str | None, str | None]:
    """Fil demandé à l'ouverture : `thread_id`, ou son alias `resume` (forme du SDK et du mock). (fil, erreur)."""
    if not isinstance(body, dict):
        return None, None
    values = []
    for key in ("thread_id", "resume"):
        v = body.get(key)
        if v in (None, ""):
            continue
        if not isinstance(v, str):
            return None, f"{key} : chaîne attendue"
        values.append(v)
    if len(set(values)) > 1:
        return None, "thread_id et resume désignent deux fils différents : n'en donner qu'un"
    return (values[0] if values else None), None


def _instance_id(state_dir: Path) -> str:
    """Identifiant stable d'un noyau local : son dossier d'état (une installation = un dossier)."""
    import hashlib

    return hashlib.sha256(str(Path(state_dir).expanduser().resolve()).encode()).hexdigest()[:12]


def ensure_token(path: str | Path) -> str:
    """Le jeton local : lu s'il existe, sinon créé (32 octets aléatoires) avec les droits 0600."""
    p = Path(path).expanduser()
    if p.is_file():
        tok = p.read_text(encoding="utf-8").strip()
        if len(tok) >= 32:
            return tok
    p.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(tok + "\n")
    os.chmod(p, 0o600)
    return tok


def _project_skills(local: dict[str, Any], state_dir: Path) -> Any:
    """`ProjectSkills` si `local.project_skills` est allumé (confiance écrite dans les réglages de l'utilisateur)."""
    if not local.get("project_skills", False):
        return None
    from nokker.tools.skills import ProjectSkills

    return ProjectSkills(user_settings=Path(local.get("settings_path") or state_dir / "settings.json").expanduser())


@dataclass
class SessionCodeTools:
    """`ToolSource` des outils de code, une `CodeToolSource` par fil ouvert par `open()`."""

    backend: str = "docker"
    image: str = DEFAULT_SANDBOX_IMAGE
    network_name: str | None = None
    host_roots: dict[str, str] = field(default_factory=dict)  # racine vue par le noyau -> vue par le démon docker
    instance: str | None = None  # étiquette nokker.instance de ses bacs à sable (ménage au démarrage)
    # Commandes en arrière-plan (lot X6, `local.background_processes`) ; leurs journaux en backend local sous
    # `state_dir/processes`.
    background: bool = False
    state_dir: str | None = None
    _by_thread: dict[str, CodeToolSource] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def open(self, thread_id: str, cwd: str, root: str) -> None:
        rel = os.path.relpath(cwd, root)
        host_root = self.host_roots.get(root)
        host = os.path.normpath(os.path.join(host_root, rel)) if host_root else None
        src = CodeToolSource(root=cwd, backend=self.backend, image=self.image, network_name=self.network_name,
                             host_root=host, instance=self.instance, background=self.background,
                             state_dir=self.state_dir)
        with self._lock:
            old = self._by_thread.pop(thread_id, None)
            self._by_thread[thread_id] = src
        if old is not None:
            old.close()

    def sessions(self) -> dict[str, str]:
        with self._lock:
            return {tid: s.root for tid, s in self._by_thread.items()}

    def specs(self) -> list[ToolSpec]:
        # Les schémas ne dépendent pas du dossier : ceux d'une source quelconque (dossier courant, jamais ouvert).
        return CodeToolSource(root=os.getcwd(), backend="local", background=self.background).specs()

    def for_thread(self, thread_id: str) -> CodeToolSource | None:
        """La source du fil ; un fil de sous-agent `<fil>/sa-<id>` (delegate) a celle de son
        parent."""
        with self._lock:
            src = self._by_thread.get(thread_id)
            while src is None and "/sa-" in thread_id:
                thread_id = thread_id.rsplit("/sa-", 1)[0]
                src = self._by_thread.get(thread_id)
            return src

    def find_process(self, proc_id: str) -> tuple[str, Any, CodeToolSource] | None:
        """(fil, processus, source) d'un processus en arrière-plan `bg_…`, quel que soit son fil (aperçu d'un port)."""
        with self._lock:
            srcs = list(self._by_thread.values())
        for s in srcs:
            found = s.find_process(proc_id)
            if found is not None:
                return found[0], found[1], s
        return None

    def processes(self) -> list[dict[str, Any]]:
        """Tous les processus en arrière-plan des sessions ouvertes (`GET /v1/local/processes`)."""
        with self._lock:
            items = list(self._by_thread.items())
        out = []
        for tid, s in items:
            table = s.process_table(tid)
            if table is not None:
                out.extend({"thread_id": tid, **p.view()} for p in table.list())
        return out
    def run_hook(self, thread_id: str, command: str, stdin: str, timeout: int) -> tuple[int | None, str]:
        """Hook d'un fil (nokker/tools/hooks.py) dans le bac à sable de SA session ; un sous-agent `<fil>/sa-<id>`
        utilise celui du fil parent (même dossier monté)."""
        base = thread_id.split("/sa-", 1)[0]
        src = self.for_thread(base)
        if src is None:
            raise LookupError("pas de session de code sur ce fil")
        return src.run_hook(base, command, stdin, timeout)

    def workspace_path(self, thread_id: str, path: str) -> str:
        """Destination d'une copie dans le dossier de travail du fil (ssh_copy, lot X11), vue du bac à sable."""
        base = thread_id.split("/sa-", 1)[0]
        src = self.for_thread(base)
        if src is None:
            raise LookupError("pas de session de code sur ce fil")
        return src.workspace_path(base, path)

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        src = self.for_thread(ctx.thread_id)
        if src is None:
            return ToolResult(call_id="", name=name, is_error=True,
                              content="pas de session de code sur ce fil (POST /v1/local/sessions)")
        return src.call(name, arguments, ctx)

    def close(self) -> None:
        with self._lock:
            srcs = list(self._by_thread.values())
            self._by_thread.clear()
        for s in srcs:
            s.close()


@dataclass
class LocalApi:
    token: str
    roots: list[str]
    allowed_hosts: frozenset[str]
    code: SessionCodeTools
    setup: LocalSetup | None = None
    on_default_model: Any = None  # appelé avec (base_url, clé, modèle) quand le modèle par défaut change
    state_dir: Path | None = None  # NOKKER_HOME = HERMES_HOME : jeton, coffre, skills/, checkpoints/
    link: LocalLink | None = None
    settings_path: Path | None = None  # réglages de l'utilisateur, où « always » écrit
    relay_host: str | None = None  # <nom>.nokker.app : accepté comme Host seulement tunnel ouvert (§7.5)
    tunnel: Any = None  # nokker.local.tunnel.Tunnel, posé par serve quand la machine est liée
    # (thread_id, cwd) -> dict | None : serve y lit le `.nokker/settings.json` du dépôt ; un dict rendu est ajouté à la
    # réponse d'ouverture (ex. `hooks`, avec l'avis des hooks du dépôt pas encore de confiance)
    on_session_open: Any = None
    # Hooks des sessions (lot X3, nokker/tools/hooks.py) : drapeau `local.hooks`, éteint par défaut ; `hooks` est le
    # HookRunner posé par serve quand le drapeau est allumé.
    hooks_enabled: bool = False
    hooks: Any = None
    # Skills du dépôt (`<dépôt>/.nokker/skills`, nokker/tools/skills.py `ProjectSkills`) : drapeau
    # `local.project_skills`, éteint par défaut ; confiance par empreinte comme les hooks du dépôt.
    project_skills: Any = None
    # Sous-agents (nokker.tools.delegate.DelegateToolSource), posé par serve : l'annulation d'un fil arrête ses enfants,
    # `delegate_status` est appelable hors tour.
    subagents: Any = None
    # Connecteurs relayés (lot X20, docs/LOKKAL.md §7.12) : drapeau `local.relayed_connectors`, éteint par défaut ;
    # `relayed` est la source posée par serve (nokker/local/relayed.py) quand il est allumé.
    relayed_enabled: bool = False
    relayed: Any = None
    mirror: Any = None  # nokker.local.mirror.MirrorWorker, posé par serve quand le miroir tourne (L5)
    load: Any = None  # nokker.local.load.LoadReporter, posé par serve (X5) : envoi de la charge, relancé au tunnel
    # Approbations fortes depuis le téléphone (lot X10) : `apply_decision` est le chemin de la décision locale (posé par
    # make_handler), `remote_approvals` le nokker.local.remote_approvals.RemoteApprovals posé par serve.
    apply_decision: Any = None
    remote_approvals: Any = None
    # Pages de session (docs/BUBBLES.md §10) : ÉTEINTES par défaut tant que les clients publiés ne savent pas afficher
    # un kind de bulle inconnu (la CLI rc.5 plante sur `artifact`) ; `local.pages: true` les allume.
    pages_enabled: bool = False
    # Coffre (X4, nokker/secrets/) : portée `user` derrière /v1/local/secrets, portée `system` sous `setup.vault`.
    vault: Vault | None = None
    # Packs d'agents (lot X16) : la `PackToolSource` du noyau et « ce fil existe-t-il ? », posés par serve.
    packs: Any = None
    packs_thread: Any = None
    _pages: Any = None
    _preview: Any = None
    # Bulles `artifact` d'une page changée hors d'un tour (`BubbleHub.page_changed`), posé par le serveur.
    page_changed: Any = None

    @property
    def preview_signer(self) -> Any:
        """Liens signés de l'aperçu d'un port (lot X6, docs/LOKKAL.md §7.7)."""
        if self._preview is None:
            from nokker.tools.processes import PreviewSigner

            self._preview = PreviewSigner()
        return self._preview
    # Mémoire de projet (lot X1, docs/LOKKAL.md §7.11) : `local.project_memory` ; None = éteinte, rien ne change.
    project_memory: Any = None
    # Ops (lot X11, docs/LOKKAL.md §7.14) : `local.ops` ; None = éteint (ni outils ssh, ni bloc `ops`, ni journal).
    ssh: Any = None  # nokker.tools.ssh.SshToolSource
    audit: Any = None  # nokker.ledger.audit.AuditLog
    # Délégation web → Lokkal (lot X21, docs/LOKKAL.md §7.16) : inventaire des ressources `local` par noms
    # (nokker.local.resources.Inventory) et sessions ouvertes par un jeton de délégation (nokker.local.delegation).
    inventory: Any = None
    delegated: Any = None
    # Agents du deck multi-machines (lot X15, docs/LOKKAL.md §7.19) : passages demandés par nokker.ai, déménagements,
    # deck publié (nokker.local.crew.LocalCrew) ; None = moteur des agents éteint ou machine sans Crew.
    crew: Any = None
    # Boîte mail posée dans Lokkal pour le propriétaire local (docs/LOKKAL.md §7.25, nokker.local.connectors) :
    # `/v1/local/connectors*`, outils mail des tours, relève du déclencheur mail: ; mot de passe par NOM du coffre.
    connectors: Any = None
    # (thread_id) -> {title, last_turn_at, working, pending_approvals} : posé par `make_handler`, qui a le hub, les
    # verrous de tour et les cartes ; None = valeurs par défaut dans `GET /v1/local/sessions`.
    describe_session: Any = None
    # Bus d'événements (`EventBus.publish`), posé par serve : `checkpoint.created` des points de retour (§7.4.7).
    emit: Any = None

    def _emit(self, name: str, payload: dict[str, Any]) -> None:
        if self.emit is not None:
            self.emit(name, payload)

    @property
    def pages(self) -> Any:
        """Magasin des pages de session (docs/BUBBLES.md §10), sous `state_dir/pages`."""
        if self._pages is None:
            from nokker.pages.store import PageStore

            self._pages = PageStore((self.state_dir or Path("~/.config/nokker").expanduser()) / "pages")
        return self._pages

    def start_tunnel(self, relay_url: str, local_base: str, web_base: str | None = None) -> bool:
        """Ouvre le tunnel si la machine est liée (jeton de machine au coffre, nom connu). Rend True si lancé."""
        from nokker.local.tunnel import Tunnel

        if self.setup is None:
            return False
        token = self.setup.vault.get("machine_token")
        self.relay_host = self.setup.state.get("relay_host")
        if not token or not self.relay_host:
            return False
        self.tunnel = Tunnel(url=relay_url, machine_token=token, local_base=local_base, web_base=web_base,
                             on_up=self.load.kick if self.load is not None else None)  # charge envoyée à la connexion
        self.tunnel.start()
        return True

    @classmethod
    def from_config(cls, local: dict[str, Any], port: int) -> LocalApi:
        code = local.get("code") or {}
        # Config STATIQUE (compose) : ce qui dépend de la machine se lit dans l'environnement que pose l'installeur.
        env_roots = os.environ.get(code.get("roots_env") or "", "")
        raw_roots = [r for r in env_roots.split(":") if r] if env_roots else list(code.get("roots") or [])
        roots = [os.path.realpath(os.path.expanduser(r)) for r in raw_roots]
        host_roots = {os.path.realpath(os.path.expanduser(k)): v for k, v in (code.get("host_roots") or {}).items()}
        env_hosts = os.environ.get(local.get("allowed_hosts_env") or "", "")
        hosts = ([h.strip() for h in env_hosts.split(",") if h.strip()] if env_hosts else None) \
            or local.get("allowed_hosts") or [f"127.0.0.1:{port}", f"localhost:{port}"]
        state_dir = Path(local.get("state_dir") or "~/.config/nokker").expanduser()
        vault = Vault.from_config(local.get("secrets"), state_dir)  # choix explicite ; erreur de config = arrêt
        setup = LocalSetup(state_path=state_dir / "setup.json", vault=LocalVault(provider=vault.system))
        link = LocalLink(setup=setup, base_url=local.get("link_url") or "https://app.nokker.ai")
        from nokker.context.project_memory import from_local_config
        from nokker.local.delegation import DelegatedSessions
        from nokker.local.resources import Inventory

        def machine_token() -> str | None:
            try:
                return setup.vault.get("machine_token")
            except Exception:  # noqa: BLE001 — coffre sans clé : machine considérée non liée
                return None

        # même inventaire que les outils ssh (X11) : dans le dossier des réglages de l'utilisateur, jamais un dépôt
        settings_dir = Path(local.get("settings_path") or state_dir / "settings.json").expanduser().parent
        hosts_path = Path(local.get("hosts_path") or settings_dir / "hosts.yaml").expanduser()
        api = cls(project_memory=from_local_config(local, state_dir),
                   inventory=Inventory(roots=roots, hosts_path=hosts_path),
                   delegated=DelegatedSessions(base_url=link.base_url, token=machine_token),
                   token=ensure_token(local.get("token_path") or state_dir / "local-token"), roots=roots, setup=setup,
                   state_dir=state_dir, vault=vault, link=link, pages_enabled=bool(local.get("pages", False)),
                   hooks_enabled=bool(local.get("hooks", False)),
                   project_skills=_project_skills(local, state_dir),
                   relayed_enabled=bool(local.get("relayed_connectors", False)),
                   settings_path=Path(local.get("settings_path") or state_dir / "settings.json").expanduser(),
                   allowed_hosts=frozenset(h.lower() for h in hosts),
                   code=SessionCodeTools(backend=code.get("backend", "docker"),
                                         instance=local.get("instance") or _instance_id(state_dir),
                                         image=code.get("image") or DEFAULT_SANDBOX_IMAGE,
                                         network_name=code.get("network_name"), host_roots=host_roots,
                                         background=bool(local.get("background_processes", False)),
                                         state_dir=str(state_dir)))
        if local.get("ops"):
            api.enable_ops(local)
        from nokker.local.connectors import FILE_NAME, LocalConnectors

        api.connectors = LocalConnectors(state_dir / FILE_NAME, vault)
        return api

    def enable_ops(self, local: dict[str, Any]) -> None:
        """Outils ssh et journal d'audit (lot X11, drapeau `local.ops`). Inventaire et known_hosts dans les réglages
        de l'UTILISATEUR (dossier de `settings.json`), jamais dans un dépôt ; journal sous l'état du noyau."""
        from nokker.ledger.audit import AuditLog, default_path
        from nokker.tools.ssh import SshToolSource

        conf = self.settings_path.parent if self.settings_path is not None else (self.state_dir or Path("."))
        resolver = self.vault.resolver() if self.vault is not None else None

        def secret(name: str) -> str:
            if resolver is None:
                raise SecretsError(f"référence secret:{name} : pas de coffre sur cette instance", "unavailable")
            return resolver.value(name)

        self.ssh = SshToolSource(sandbox=self.code.run_hook, secret=secret,
                                 hosts_path=Path(local.get("hosts_path") or conf / "hosts.yaml").expanduser(),
                                 known_hosts_path=Path(local.get("known_hosts_path") or conf / "known_hosts")
                                 .expanduser(), workspace_path=self.code.workspace_path)
        self.audit = AuditLog(Path(local.get("audit_path") or default_path(self.state_dir or conf)).expanduser())

    def tool_sources(self) -> list[Any]:
        """Sources d'outils d'une session locale : outils de code par fil enveloppés par les points de retour
        (photo avant chaque écriture), outils des points de retour, skills (L3). Tout l'état
        (skills/, checkpoints/) sous `state_dir` = NOKKER_HOME = HERMES_HOME."""
        from nokker.tools.checkpoints import auto_prune, checkpoint_sources
        from nokker.tools.skills import SkillToolSource

        home = self.state_dir

        def root_of(thread_id: str) -> Path | None:
            cwd = self.code.sessions().get(thread_id)
            return Path(cwd) if cwd else None

        from nokker.pages.tools import PageToolSource, PagingToolSource

        wrapper, checkpoints = checkpoint_sources(self.code, root_of, home, emit=self._emit)
        auto_prune(home)
        memory = []
        if self.project_memory is not None:
            from nokker.context.project_memory import ProjectMemoryTools

            memory = [ProjectMemoryTools(self.project_memory)]
        if self.ssh is not None:
            # Ops (X11), hors de l'enveloppe des points de retour : seul `ssh_copy` avec `local_path` écrit dans le
            # dossier, sans photo avant (limite connue) ; il passe au journal d'audit comme tout appel à effet.
            memory.append(self.ssh)
        skills = SkillToolSource(home=home, project=self.project_skills)
        if not self.pages_enabled:
            return [wrapper, checkpoints, skills, *memory]
        # Pages (§10) : l'enveloppe des pages autour de celle des points de retour (photo avant, version de page après).
        return [PagingToolSource(wrapper, self.pages, root_of), checkpoints, PageToolSource(self.pages, root_of),
                skills, *memory]

    def host_ok(self, host: str | None) -> bool:
        """Hôtes acceptés : ceux de la configuration, et, tunnel ouvert, `<nom>.nokker.app` et ses deux origines de pages
        (`<nom>--pages.nokker.app`, pages privées ; `<nom>--pub.nokker.app`, pages publiques, §7.26) que le relais pose
        sur les requêtes qu'il fait passer."""
        h = (host or "").strip().lower()
        if not h:
            return False
        if h in self.allowed_hosts:
            return True
        if not (self.relay_host and self.tunnel is not None and self.tunnel.state == "up"):
            return False
        rh = self.relay_host.lower()
        name, dot, domain = rh.partition(".")
        return h == rh or (bool(dot) and h in (f"{name}--pages.{domain}", f"{name}--pub.{domain}"))

    def is_owner(self, authorization: str | None) -> bool:
        auth = authorization or ""
        tok = auth[7:].strip() if auth.startswith("Bearer ") else ""
        return bool(tok) and hmac.compare_digest(tok, self.token)

    def relayed_owner(self, authorization: str | None) -> dict[str, Any] | None:
        """Requête venue par la passerelle : jeton signé par nokker.ai (accès ou délégation, §7.5), vérifié avec les
        clés épinglées au lien, et du compte lié à CETTE machine. Rend ses claims, ou None. Un jeton d'une autre
        portée (lien de pages, 60 s, pour le relais seulement) n'est jamais le propriétaire. Un jeton de délégation
        (`scope: lokkal:session`) n'ouvre que les routes de sa session (`delegation_allows`, X21)."""
        from nokker.cloud.link import ISSUER, TokenError, verify_token

        if self.setup is None:
            return None
        jwks, account = self.setup.state.get("jwks"), self.setup.state.get("account")
        auth = authorization or ""
        tok = auth[7:].strip() if auth.startswith("Bearer ") else ""
        if not (jwks and account and tok.count(".") == 2):
            return None
        try:
            claims = verify_token(tok, jwks, machine=self.setup.state.get("machine_id"),
                                  issuer=self.link.base_url.rstrip("/") if self.link is not None else ISSUER)
        except TokenError:
            return None
        if claims.get("scope") not in (None, "lokkal:session"):
            return None
        return claims if claims.get("sub") == account else None

    def delegation_claims(self, authorization: str | None) -> dict[str, Any] | None:
        """Claims d'un jeton de DÉLÉGATION valide (noyau de nokker.ai, X21), sinon None."""
        claims = self.relayed_owner(authorization)
        return claims if claims is not None and claims.get("scope") == "lokkal:session" else None

    def delegation_allows(self, method: str, path: str, session_id: str | None, authorization: str | None) -> bool:
        """Filtre de la passerelle (X21) : True si la requête relayée n'est PAS une délégation, ou si c'est une route
        de la session déléguée. Rien d'autre de la machine n'est lisible par nokker.ai."""
        claims = self.delegation_claims(authorization)
        if claims is None:
            return True
        return self.delegated is not None and self.delegated.allows(method, path, session_id, claims)

    def _session_row(self, thread_id: str, cwd: str) -> dict[str, Any]:
        """Une ligne de `GET /v1/local/sessions` (§7.4.1) : `{thread_id, cwd}` d'origine, plus `title`, `last_turn_at`,
        `working`, `pending_approvals` donnés par `describe_session` (posé par `make_handler`) ; sans lui, ou s'il lève,
        les valeurs par défaut (aucun tour connu)."""
        row: dict[str, Any] = {"thread_id": thread_id, "cwd": cwd, **SESSION_DEFAULTS}
        if self.describe_session is not None:
            try:
                extra = self.describe_session(thread_id) or {}
            except Exception:  # noqa: BLE001 — la liste des sessions ne tombe jamais pour un détail
                logger.warning("description de la session %s impossible", thread_id, exc_info=True)
                extra = {}
            row.update({k: v for k, v in extra.items() if k in SESSION_DEFAULTS})
        return row

    def _root_of(self, cwd: str) -> str | None:
        full = os.path.realpath(os.path.expanduser(cwd))
        for r in self.roots:
            if full == r or full.startswith(r + os.sep):
                return r
        return None

    def handle(self, method: str, path: str, authorization: str | None,
               body: dict[str, Any], relayed: bool = False) -> tuple[int, dict[str, Any]] | None:
        """(statut, corps) pour une route `/v1/local/*`, ou None si la route n'existe pas (404 par l'appelant)."""
        if not path.startswith("/v1/local/"):
            return None
        # Sur la machine : le jeton local seul ; par la passerelle : un jeton signé par nokker.ai seul (§7.5).
        owner = self.relayed_owner(authorization) is not None if relayed else self.is_owner(authorization)
        if not owner:
            return 401, {"error": {"message": "jeton local requis"}}
        if path == "/v1/local/agent-runs" or path.startswith(("/v1/local/agent-runs/", "/v1/local/agents/")):
            # X15 : nokker.ai seul, par la passerelle, avec un jeton qui nomme l'opération (jamais le jeton local).
            if self.crew is None:
                return 404, {"error": {"message": "agents du web non pris en charge sur cette machine"}}
            return self.crew.handle(method, path, body, self.delegation_claims(authorization) if relayed else None)
        if path == "/v1/local/sessions" and method == "POST":
            claims = self.delegation_claims(authorization) if relayed else None
            resource = body.get("resource") if isinstance(body, dict) else None
            cwd = str(body.get("cwd") or "")
            if isinstance(resource, dict):
                # Besoin nommé (X21) : la machine retrouve elle-même le dossier ; nokker.ai n'en connaît que le nom.
                found = self.inventory.resolve(str(resource.get("kind") or ""), str(resource.get("name") or "")) \
                    if self.inventory is not None else None
                if found is None:
                    return 404, {"error": {"message": "ressource non déclarée sur cette machine"}}
                cwd = found
            root = self._root_of(cwd) if cwd else None
            if root is None or not os.path.isdir(cwd):
                return 403, {"error": {"message": "dossier hors des racines déclarées"}}
            chosen, err = _chosen_thread(body)
            if err:
                return 400, {"error": {"message": err}}
            thread_id = chosen or f"th_{uuid.uuid4().hex[:16]}"
            if claims is not None:
                if self.delegated is None:
                    return 403, {"error": {"message": "délégation non prise en charge sur cette machine"}}
                thread_id = f"th_{uuid.uuid4().hex[:16]}"  # jamais un fil choisi par nokker.ai (un fil local existant)
                self.delegated.register(thread_id, claims)
            self.code.open(thread_id, os.path.realpath(cwd), root)
            extra = self.on_session_open(thread_id, os.path.realpath(cwd)) if self.on_session_open is not None else None
            if self.project_memory is not None:  # consignes du dépôt importées, clé de projet posée (ne lève pas)
                self.project_memory.open_session(thread_id, os.path.realpath(cwd), root)
            if self.project_skills is not None:  # skills du dépôt : empreinte et confiance, pour ce fil
                extra = {**(extra if isinstance(extra, dict) else {}),
                         "skills": self.project_skills.open_session(thread_id, os.path.realpath(cwd), root)}
            if self.ssh is not None:  # inventaire des hôtes lu UNE fois, pour ce fil (lot X11)
                extra = {**(extra if isinstance(extra, dict) else {}), "ops": self.ssh.open_session(thread_id)}
            return 201, {**(extra if isinstance(extra, dict) else {}), "thread_id": thread_id,
                         "cwd": os.path.realpath(cwd)}
        if path == "/v1/local/hooks/trust" and method == "POST":
            # Faire confiance au code d'un dépôt se fait sur la machine (jeton local), jamais par la passerelle.
            if relayed:
                return 403, {"error": {"message": "confiance des hooks : sur la machine seulement (jeton local)"}}
            if self.hooks is None:
                return 404, {"error": {"message": "hooks désactivés (local.hooks)"}}
            return self.hooks.trust(str(body.get("thread_id") or ""), str(body.get("sha256") or ""))
        if path == "/v1/local/skills/trust" and method == "POST":
            # Même règle que les hooks : la confiance dans le contenu d'un dépôt se donne sur la machine seulement.
            if relayed:
                return 403, {"error": {"message": "confiance des skills : sur la machine seulement (jeton local)"}}
            if self.project_skills is None:
                return 404, {"error": {"message": "skills du dépôt désactivés (local.project_skills)"}}
            return self.project_skills.trust(str(body.get("thread_id") or ""), str(body.get("sha256") or ""))
        if method == "GET" and path.startswith("/v1/local/sessions/") and path.endswith("/skills"):
            # État de confiance des skills du dépôt d'une session déjà ouverte (reprise d'une session, §7.21.3) ;
            # lecture seule : rouvrir la session remplacerait sa source de code.
            thread_id = path[len("/v1/local/sessions/"):-len("/skills")]
            if self.project_skills is None:
                return 404, {"error": {"message": "skills du dépôt désactivés (local.project_skills)"}}
            st = self.project_skills.status(thread_id) if thread_id in self.code.sessions() else None
            if st is None:
                return 404, {"error": {"message": "pas de session locale sur ce fil"}}
            return 200, {"thread_id": thread_id, "skills": st}
        if path == "/v1/local/sessions" and method == "GET":
            return 200, {"sessions": [self._session_row(t, c) for t, c in self.code.sessions().items()]}
        if path.startswith("/v1/local/setup") and self.setup is not None:
            return self._setup(method, path, body)
        if path == "/v1/local/machine" and method == "GET":
            import socket

            linked = self.setup is not None and "router" in self.setup.vault.names()
            # tunnel : « down » tant que le client du tunnel (§7.5, semaine 2) n'existe pas
            up = self.tunnel is not None and self.tunnel.state == "up"
            return 200, {"machine_id": self.code.instance, "name": socket.gethostname(), "linked": linked,
                         "tunnel": "up" if up else "down", "url": f"https://{self.relay_host}" if up else None}
        if path == "/v1/local/link" or path.startswith("/v1/local/link/"):
            return self._link(method, path)
        if path == "/v1/local/mirror/projects" or path.startswith("/v1/local/mirror/projects/"):
            return self._mirror_projects(method, path, body)
        if path == "/v1/local/mirror" or path.startswith("/v1/local/mirror/"):
            return self._mirror(method, path, body)
        if path == "/v1/local/resources" and method == "GET":
            # Ce que la machine publie à nokker.ai (X21) : noms seulement, comme envoyé.
            return 200, self.inventory.collect() if self.inventory is not None else {"v": 1}
        if path == "/v1/local/secrets" or path.startswith("/v1/local/secrets/"):
            return self._secrets(method, path, body, relayed)
        if path == "/v1/local/mcp-servers" or path.startswith("/v1/local/mcp-servers/"):
            return self._mcp_servers(method, path, body, relayed)
        if path == "/v1/local/packs" or path.startswith("/v1/local/packs/"):
            return self._packs(method, path, body, relayed)
        if path.split("?", 1)[0].rstrip("/") == "/v1/local/connectors" or path.startswith("/v1/local/connectors/"):
            if self.connectors is None:
                return 404, {"error": {"message": "connecteurs locaux non pris en charge sur cette machine"}}
            return self.connectors.handle(method, path, body, relayed)
        return None

    def _packs(self, method: str, path: str, body: Any, relayed: bool) -> tuple[int, dict[str, Any]] | None:
        """Packs d'agents (lot X16, LOKKAL §7.30) : `GET` → la liste (`pack_list`) ; `POST /{nom}/remove {thread_id?}` →
        le même retrait que l'outil `pack_remove` (compétences, cartes non activées ; carte `pack_agents` dans le fil
        pour les agents créés depuis le pack, jamais de suppression silencieuse). Le geste est celui du propriétaire
        (jeton local) : pas de carte d'approbation en plus ; par la passerelle, refusé."""
        from nokker.core.ports import ToolContext
        from nokker.tools.packs import PackError

        if self.packs is None:
            return 404, {"error": {"message": "packs non pris en charge sur cette machine"}}
        if path == "/v1/local/packs" and method == "GET":
            return 200, self.packs.list_packs()
        rest = path[len("/v1/local/packs/"):]
        if method != "POST" or not rest.endswith("/remove") or rest.count("/") != 1:
            return None
        if relayed:
            return 403, {"error": {"message": "retirer un pack : sur la machine seulement (jeton local)"}}
        thread = body.get("thread_id") if isinstance(body, dict) else None
        if thread is not None and not isinstance(thread, str):
            return 400, {"error": {"message": "thread_id : identifiant de fil (texte)", "code": "bad_body"}}
        if thread and (self.packs_thread is None or not self.packs_thread(thread)):
            return 404, {"error": {"message": "fil inconnu", "code": "unknown_thread"}}
        ctx = ToolContext(thread_id=thread or "", turn_index=0, channel="api", sender=LOCAL_OWNER)
        try:
            return 200, self.packs.remove(rest[:-len("/remove")], ctx)
        except PackError as e:
            return 404, {"error": {"message": str(e), "code": "unknown_pack"}}

    def _mcp_servers(self, method: str, path: str, body: Any, relayed: bool) -> tuple[int, dict[str, Any]] | None:
        """Serveurs MCP ajoutés depuis une session (lot X16) : `GET` → la liste (noms de secrets, outils déclarés en
        lecture) ; `PUT /{nom}/read-tools {read_tools}` → remplace les outils en lecture (actif au prochain démarrage).
        Un nom d'écriture évidente n'est jamais déclaré par ici (409) : il passe par l'outil `mcp_server_read_tools`
        et sa carte forte. Écrire par la passerelle : refusé (la config de la machine se règle sur la machine)."""
        from urllib.parse import unquote

        from nokker.tools.mcp_config import (
            FILE_NAME, McpConfigError, check_read_tools, load_user_servers, secret_names, set_read_tools, write_words,
        )
        from nokker.tools.packs import user_home

        file = user_home(self.state_dir) / FILE_NAME
        if path == "/v1/local/mcp-servers" and method == "GET":
            items = []
            for s in load_user_servers(file):
                item = {"name": s["name"], "url": s["url"], "secrets": secret_names(s)}
                for key in ("purpose", "read_tools", "confirmed_writes"):
                    if s.get(key):
                        item[key] = s[key]
                items.append(item)
            return 200, {"servers": items}
        rest = path[len("/v1/local/mcp-servers/"):]
        if method != "PUT" or not rest.endswith("/read-tools"):
            return None
        if relayed:
            return 403, {"error": {"message": "les serveurs MCP se règlent sur la machine (jeton local)"}}
        name = unquote(rest[:-len("/read-tools")])
        reads = body.get("read_tools") if isinstance(body, dict) else None
        if not isinstance(reads, list):
            return 400, {"error": {"message": "corps {read_tools: [noms d'outils]} requis", "code": "bad_body"}}
        writes = [str(t) for t in reads if write_words(str(t))]
        if writes:
            return 409, {"error": {"code": "needs_strong_card", "message": (
                f"{', '.join(writes)} : nom d'écriture évidente ; le déclarer en lecture demande une carte forte : "
                "demande-le dans une session (outil mcp_server_read_tools avec confirm_writes)")}}
        try:
            check_read_tools(reads)
            if not any(s["name"] == name for s in load_user_servers(file)):
                return 404, {"error": {"message": f"serveur ajouté inconnu : {name[:40]}", "code": "not_found"}}
            entry = set_read_tools(file, name, reads)
        except McpConfigError as e:
            return 400, {"error": {"message": str(e), "code": "bad_body"}}
        except OSError as e:
            return 500, {"error": {"message": f"écriture impossible : {type(e).__name__}"}}
        return 200, {"server": name, "read_tools": entry.get("read_tools") or [],
                     "active": "au prochain redémarrage du noyau"}

    def _secrets(self, method: str, path: str, body: dict[str, Any],
                 relayed: bool) -> tuple[int, dict[str, Any]] | None:
        """Coffre (X4) : `GET` → noms et dates ; `PUT /{nom} {value}` → nom et date (la valeur n'est jamais
        renvoyée) ; `DELETE /{nom}`. Écrire ou effacer par la passerelle : refusé (la valeur traverserait le relais) ;
        lire les noms, permis."""
        from urllib.parse import unquote

        if self.vault is None:
            return 404, {"error": {"message": "pas de coffre sur cette instance"}}
        name = unquote(path[len("/v1/local/secrets/"):]) if path.startswith("/v1/local/secrets/") else None
        try:
            if name is None and method == "GET":
                return 200, {**self.vault.status(), "secrets": [i.as_dict() for i in self.vault.list()]}
            if name is None or method not in ("PUT", "DELETE"):
                return None
            if relayed:
                return 403, {"error": {"message": "le coffre ne s'écrit que sur la machine (jeton local)"}}
            if method == "PUT":
                value = body.get("value") if isinstance(body, dict) else None
                return 200, self.vault.set(name, value).as_dict()  # type: ignore[arg-type]  (validé par set)
            from nokker.secrets.base import check_name

            check_name(name)
            if not self.vault.delete(name):
                return 404, {"error": {"message": f"secret {name} absent"}}
            return 200, {"name": name, "deleted": True}
        except SecretsError as e:
            if e.code in ("bad_name", "bad_value"):
                return 400, {"error": {"message": str(e), "code": e.code}}
            return 503, {"error": {"message": str(e), "code": e.code}}

    def _mirror_projects(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
        """Choix du miroir par projet (§6.3, §6.7, X21) : `GET` → `{projects: [{project, label, mirror}]}` ;
        `PUT /{projet} {mirror: bool}`. Non par défaut : une note de projet reste `local` tant que le projet n'est pas
        mis en miroir."""
        pm = self.project_memory
        if pm is None:
            return 404, {"error": {"message": "mémoire de projet désactivée (local.project_memory)"}}
        if path == "/v1/local/mirror/projects" and method == "GET":
            return 200, {"projects": pm.projects()}
        project = path[len("/v1/local/mirror/projects/"):]
        if method != "PUT" or not project:
            return None
        if not isinstance(body, dict) or not isinstance(body.get("mirror"), bool):
            return 400, {"error": {"message": "corps {mirror: true|false} requis"}}
        if not pm.set_mirrored(project, body["mirror"]):
            return 404, {"error": {"message": "projet inconnu"}}
        return 200, {"project": project, "mirror": body["mirror"]}

    def _mirror(self, method: str, path: str, body: Any = None) -> tuple[int, dict[str, Any]] | None:
        """Miroir web ⇄ machine (L5, §7.23) : état (`GET`), effacement des copies de la machine chez nokker.ai (envoi en
        pause), reprise (tout est renvoyé), resynchronisation complète (`POST /resync` → 202 ; 409 en pause), choix des
        pages copiées (`GET /pages`, `PUT /pages/{page_id} {mirror: bool}` : 400 corps, 404 page inconnue)."""
        if self.mirror is None:
            if path == "/v1/local/mirror" and method == "GET":
                return 200, {"enabled": False}
            return 404, {"error": {"message": "miroir non actif sur cette machine"}}
        if path == "/v1/local/mirror" and method == "GET":
            return 200, self.mirror.status()
        if path == "/v1/local/mirror/erase" and method == "POST":
            try:
                return 200, {"erased_turns": self.mirror.erase_remote(), "paused": True}
            except httpx.HTTPError:
                return 502, {"error": {"message": "nokker.ai n'a pas confirmé l'effacement : rien n'a changé ici"}}
        if path == "/v1/local/mirror/resume" and method == "POST":
            self.mirror.resume()
            return 200, self.mirror.status()
        if path == "/v1/local/mirror/resync" and method == "POST":
            if not self.mirror.request_resync():
                return 409, {"error": {"message": "miroir en pause (copies effacées) : reprendre d'abord"}}
            return 202, self.mirror.status()
        if path == "/v1/local/mirror/pages" and method == "GET":
            return 200, self.mirror.pages_view()
        if path.startswith("/v1/local/mirror/pages/") and method == "PUT":
            page_id = path[len("/v1/local/mirror/pages/"):]
            if not isinstance(body, dict) or not isinstance(body.get("mirror"), bool):
                return 400, {"error": {"message": "corps {mirror: true|false} requis"}}
            try:
                if not self.mirror.set_page_mirror(page_id, body["mirror"]):
                    return 404, {"error": {"message": "page inconnue"}}
            except ValueError as e:  # page épinglée `local` (§7.26)
                return 409, {"error": {"code": "local_page", "message": str(e)}}
            return 200, {"page_id": page_id, "mirror": body["mirror"]}
        return None

    def sync_moderation(self) -> list[Any]:
        """Applique aux pages de la machine ce que nokker.ai a dit de leur modération (`LocalLink.moderation`, lu avec
        le droit de publier, §7.26 point 6) : dépublier et marquer les pages retirées, démarquer celles que l'opérateur
        a rendues ; les bulles suivent. Rien sans liste connue."""
        moderation = getattr(self.link, "moderation", None) if self.link is not None else None
        if not isinstance(moderation, dict) or (not self.pages_enabled and self._pages is None):
            return []
        changed = self.pages.apply_moderation(moderation)
        for meta in changed:
            if self.page_changed is not None:
                self.page_changed(meta)
        return changed

    def _link(self, method: str, path: str) -> tuple[int, dict[str, Any]] | None:
        if self.link is None:
            return 404, {"error": {"message": "lien de compte non configuré"}}
        import socket

        try:
            if path == "/v1/local/link/start" and method == "POST":
                return 200, self.link.start(socket.gethostname())
            if path == "/v1/local/link/status" and method == "GET":
                st = self.link.status()
                if isinstance(st, dict) and st.get("state") == "approved" and hasattr(self.link, "pages_public"):
                    st = {**st, "pages_public": bool(self.link.pages_public())}  # droit de publier (§7.26), caché 5 min
                    self.sync_moderation()  # lue dans la même réponse de nokker.ai (§7.26 point 6)
                return 200, st
            if path == "/v1/local/link" and method == "DELETE":
                return 200, self.link.unlink()
        except VaultError as e:
            return 503, {"error": {"message": str(e)}}
        except (httpx.HTTPError, KeyError, ValueError):
            return 502, {"error": {"message": "service de lien nokker.ai indisponible"}}
        return None

    def _setup(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
        setup = self.setup
        try:
            if path == "/v1/local/setup" and method == "GET":
                view = setup.view()
                if self.relayed is not None:  # connecteurs du compte lié, relayés (X20) : jamais d'identifiant
                    view["connectors"] = self.relayed.status()["connectors"]
                return 200, view
            if path == "/v1/local/setup/models/test" and method == "POST":
                return 200, setup.test_model(body)
            if path == "/v1/local/setup/models" and method == "PUT":
                view = setup.put_models(body)
                chosen = setup.provider_for_default()
                if chosen is not None and self.on_default_model is not None:
                    self.on_default_model(*chosen)
                return 200, view
            if path == "/v1/local/setup/done" and method == "POST":
                return 200, setup.done()
        except VaultError as e:
            return 503, {"error": {"message": str(e)}}
        except ValueError as e:
            return 400, {"error": {"message": str(e)}}
        return None


__all__ = ["LOCAL_OWNER", "SESSION_DEFAULTS", "LocalApi", "SessionCodeTools", "ensure_token"]
