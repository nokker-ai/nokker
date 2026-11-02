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
import re
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
from nokker.tools.code import DEFAULT_SANDBOX_IMAGE, WORKSPACE_MOUNT, CodeToolSource

LOCAL_OWNER = "local-owner"
logger = logging.getLogger("nokker.local.api")
# Champs ajoutés à chaque ligne de `GET /v1/local/sessions` (§7.4.1), avec leur valeur quand rien n'est connu.
SESSION_DEFAULTS: dict[str, Any] = {"title": None, "last_turn_at": None, "working": False, "pending_approvals": 0,
                                    "paused": False}


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


def _network_name(code: dict) -> str | None:
    """Réseau docker des bacs à sable : `network_name_env` (variable d'environnement, ex. NOKKER_SANDBOX_NETWORK posée
    par le compose, `<projet>-sandbox`) l'emporte sur `network_name` — plus de sed de l'installeur sur la config."""
    env_name = code.get("network_name_env")
    if env_name:
        val = os.environ.get(str(env_name), "").strip()
        if val:
            return val
    return code.get("network_name")


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
    # Diagnostics de langage (lot 13, `local.lsp.enabled`) : un `LspManager` pour toute l'instance (plafonds de
    # serveurs partagés entre fils) ; None = éteint.
    lsp: Any = None
    # Injection des secrets à la sortie (`local.code.egress_secrets`, docs/EGRESS-SECRETS.md) : `EgressSecrets` et la
    # portée `user` du coffre local ; None = éteint (environnement d'origine).
    egress: Any = None
    egress_provider: Any = None
    _by_thread: dict[str, CodeToolSource] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def open(self, thread_id: str, cwd: str, root: str) -> None:
        rel = os.path.relpath(cwd, root)
        host_root = self.host_roots.get(root)
        host = os.path.normpath(os.path.join(host_root, rel)) if host_root else None
        extra: dict[str, Any] = {}
        if self.egress is not None and self.backend == "docker":
            setup = self.egress.open_session(_egress_id(thread_id), "local", self.egress_provider, cwd, WORKSPACE_MOUNT)
            if setup is not None:
                extra["sandbox_env"] = dict(setup.env)
        src = CodeToolSource(root=cwd, backend=self.backend, image=self.image, network_name=self.network_name,
                             host_root=host, instance=self.instance, background=self.background,
                             state_dir=self.state_dir, lsp=self.lsp, **extra)
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
        return CodeToolSource(root=os.getcwd(), backend="local", background=self.background, lsp=self.lsp).specs()

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
            threads = list(self._by_thread)
            self._by_thread.clear()
        for s in srcs:
            s.close()
        if self.egress is not None:
            for tid in threads:
                self.egress.close_session(_egress_id(tid))
        if self.lsp is not None:
            self.lsp.close()


def _egress_id(thread_id: str) -> str:
    """Identifiant de session au proxy de sortie pour un fil Lokkal (stable, sans caractère d'URL)."""
    import hashlib

    return "lk-" + hashlib.sha256(thread_id.encode()).hexdigest()[:24]


def _egress(code: dict[str, Any]) -> Any:
    """`EgressSecrets` si `local.code.egress_secrets.enabled` est vrai, sinon None (drapeau éteint par défaut)."""
    from nokker.egress.kernel import EgressSecrets, EgressSecretsConfig

    cfg = EgressSecretsConfig.from_config(code.get("egress_secrets"))
    return EgressSecrets(cfg) if cfg is not None else None


def _lsp_manager(local: dict[str, Any]) -> Any:
    """`LspManager` si `local.lsp.enabled` est vrai (lot 13, docs/LOKKAL-LSP.md), sinon None ; `local.lsp.inline`
    ajoute le bloc `[Diagnostics]` aux résultats d'édition. Éteints tous les deux par défaut."""
    conf = local.get("lsp")
    if not isinstance(conf, dict) or not conf.get("enabled", False):
        return None
    from nokker.tools.lsp import LspManager

    return LspManager(inline=bool(conf.get("inline", False)))


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
    # (relay_url, local_base, web_base) : posé par serve, il permet de lancer le tunnel à l'approbation du lien, sans
    # redémarrage (`link_approved`). None = pas de passerelle configurée.
    tunnel_args: tuple[str, str, str | None] | None = None
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
    # Écrans du foyer (docs/SCREEN-CAST.md) : `local.cast.enabled`, éteint par défaut ; None = ni outils, ni routes
    # `/v1/local/cast/*`, ni écouteur.
    cast: Any = None
    _pages: Any = None
    _preview: Any = None
    # Bulles `artifact` d'une page changée hors d'un tour (`BubbleHub.page_changed`), posé par le serveur.
    page_changed: Any = None
    # Prévenir l'auteur d'une page retirée par la modération, UNE fois, au premier retrait vu (§7.26 point 6) : une
    # bulle `notify` (`BubbleHub.page_moderated`), posé par le serveur.
    page_moderated: Any = None

    @property
    def preview_signer(self) -> Any:
        """Liens signés de l'aperçu d'un port (lot X6, docs/LOKKAL.md §7.7)."""
        if self._preview is None:
            from nokker.tools.processes import PreviewSigner

            self._preview = PreviewSigner()
        return self._preview
    # Mémoire de projet (lot X1, docs/LOKKAL.md §7.11) : `local.project_memory` ; None = éteinte, rien ne change.
    project_memory: Any = None
    # Apprentissage (docs/LEARNING.md) : `local.learning` (skills appris) et/ou `local.memory_pending` (quarantaine) ;
    # None = éteint, rien ne change (ni outil, ni route).
    learning: Any = None
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
    # `LocalPermissions` du noyau (posées par serve) : `GET|PUT /v1/local/sessions/{fil}/mode` (§7.4.10) ; None = 404.
    permissions: Any = None
    # Mode dossier (docs/FOLDER-MODE.md) : `local.folder_mode` ; `folder` est le `nokker.folder.run.FolderRuns` posé par
    # serve quand le drapeau est allumé. None = éteint : ni route, ni outil.
    folder: Any = None

    def _emit(self, name: str, payload: dict[str, Any]) -> None:
        if self.emit is not None:
            self.emit(name, payload)

    def __post_init__(self) -> None:
        if self.link is not None and getattr(self.link, "on_approved", 1) is None:
            self.link.on_approved = self.link_approved  # fin du flux d'appareil : effets sans redémarrage

    @property
    def pages(self) -> Any:
        """Magasin des pages de session (docs/BUBBLES.md §10), sous `state_dir/pages`."""
        if self._pages is None:
            from nokker.pages.store import PageStore

            self._pages = PageStore((self.state_dir or Path("~/.config/nokker").expanduser()) / "pages")
        return self._pages

    def start_tunnel(self, relay_url: str, local_base: str, web_base: str | None = None) -> bool:
        """Ouvre le tunnel si la machine est liée (jeton de machine au coffre, nom connu). Rend True si lancé ; False
        si non liée ou si un tunnel tourne déjà (jamais deux)."""
        from nokker.local.tunnel import Tunnel

        if self.setup is None:
            return False
        if self.tunnel is not None and getattr(self.tunnel, "running", False):
            return False
        token = self.setup.vault.get("machine_token")
        self.relay_host = self.setup.state.get("relay_host")
        if not token or not self.relay_host:
            return False
        self.tunnel = Tunnel(url=relay_url, machine_token=token, local_base=local_base, web_base=web_base,
                             on_up=self.load.kick if self.load is not None else None)  # charge envoyée à la connexion
        self.tunnel.start()
        return True

    def stop_tunnel(self) -> None:
        """Ferme le tunnel (lien retiré) : le Host `<nom>.nokker.app` n'est plus accepté."""
        tunnel, self.tunnel, self.relay_host = self.tunnel, None, None
        if tunnel is not None:
            tunnel.stop()

    def link_approved(self) -> None:
        """Fin du flux d'appareil (`LocalLink.status` vient de ranger la clé et le jeton de machine) : tout ce qui
        n'était lu qu'au démarrage est pris en compte tout de suite — modèle par défaut (clé du lien sur Ship, Deep,
        petit palier et juges du Router) et tunnel de la passerelle. Les valeurs entrent au caviardage sans attendre
        le rafraîchissement du coffre."""
        if self.setup is None:
            return
        if self.vault is not None:
            for name in ("router", "machine_token"):
                try:
                    value = self.setup.vault.get(name)
                except Exception:  # noqa: BLE001 — coffre illisible : rien à caviarder de plus
                    value = None
                if value:
                    self.vault.known.note("system", name, value)
        if self.on_default_model is not None:
            chosen = self.setup.provider_for_default()
            if chosen is not None:
                self.on_default_model(*chosen)
        if self.tunnel_args is not None and self.start_tunnel(*self.tunnel_args):
            logger.info("lokkal : tunnel de la passerelle lancé à l'approbation du lien (%s)", self.tunnel_args[0])

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
                                         network_name=_network_name(code), host_roots=host_roots,
                                         background=bool(local.get("background_processes", False)),
                                         state_dir=str(state_dir), lsp=_lsp_manager(local),
                                         egress=_egress(code), egress_provider=vault.user))
        if api.code.lsp is not None:
            api.code.lsp.emit = api._emit  # `lsp.diagnostics` sur le bus, comme `checkpoint.created`
        from nokker.learning.wiring import local_learning

        api.learning = local_learning(local, state_dir, state_dir, api.project_memory)
        cast_conf = local.get("cast") if isinstance(local.get("cast"), dict) else {}
        if cast_conf.get("enabled") is True:
            from nokker.cast.service import build_cast

            _code = api.code
            api.cast = build_cast(cast_conf, state_dir, vault=vault,
                                  root_of=lambda tid: _code.sessions().get(tid))
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
        if self.learning is not None:  # skills appris (drapeau `local.learning`) : sous la racine de l'utilisateur
            from nokker.learning.wiring import local_tool_source

            learned = local_tool_source(self.learning, skills)
            if learned is not None:
                memory.append(learned)
        if self.cast is not None:  # écrans du foyer (docs/SCREEN-CAST.md), `local.cast.enabled`
            memory.append(self.cast.tool_source())
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
        except TokenError as e:
            logger.info("passerelle : jeton refusé (%s)", e)  # la raison, jamais le jeton
            return None
        if claims.get("scope") not in (None, "lokkal:session"):
            logger.info("passerelle : jeton refusé (portée %r)", claims.get("scope"))
            return None
        if claims.get("sub") != account:
            logger.info("passerelle : jeton refusé (compte du jeton ≠ compte lié à cette machine)")
            return None
        return claims

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

    def _session_mode(self, method: str, thread_id: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Mode d'une session locale (§7.4.10) : `{mode, modes, effective}` ; `effective` = le mode après restriction par
        le dépôt et les équipes du fil (`LocalPermissions.mode_for`)."""
        from nokker.qa.permissions import MODES

        perms = self.permissions
        if perms is None:
            return 404, {"error": {"message": "permissions locales non configurées"}}
        if thread_id not in self.code.sessions():
            return 404, {"error": {"message": "pas de session locale sur ce fil"}}
        if method == "PUT":
            mode = body.get("mode") if isinstance(body, dict) else None
            if not isinstance(mode, str) or mode not in MODES:
                return 400, {"error": {"code": "bad_mode", "message": f"mode refusé : {mode!r} (parmi "
                                                                       f"{', '.join(MODES)})"}}
            try:
                perms.set_thread_mode(thread_id, mode)
            except ValueError as e:
                return 400, {"error": {"code": "mode_above_project", "message": str(e)}}
        out = {"mode": perms.chosen_mode(thread_id).value, "modes": list(MODES),
               "effective": perms.mode_for(thread_id).value}
        if method == "PUT":
            self._emit("session.mode", {"thread_id": thread_id, "mode": out["mode"], "effective": out["effective"]})
        return 200, out

    def _cast(self, method: str, path: str, body: dict[str, Any], relayed: bool) -> tuple[int, dict[str, Any]]:
        """Écrans du foyer (docs/SCREEN-CAST.md §9) : sur la machine seulement (jeton local), jamais par la passerelle —
        la liste des écrans, l'appairage et les décisions ne sortent pas du réseau local.
        GET /v1/local/cast/screens[?refresh=1] ; PUT|DELETE /v1/local/cast/screens/{id} {room, name, owner} ;
        POST /v1/local/cast/screens/{id}/pair {pin?} ; POST /v1/local/cast/requests/{id} {decision: approve|refuse} ;
        DELETE /v1/local/cast/consents {member, screen}."""
        if self.cast is None:
            return 404, {"error": {"message": "écrans désactivés (local.cast.enabled)"}}
        if relayed:
            return 403, {"error": {"message": "écrans : sur la machine seulement (jeton local)"}}
        bare, _, query = path.partition("?")
        parts = bare[len("/v1/local/cast/"):].strip("/").split("/") if bare.startswith("/v1/local/cast/") else []
        body = body if isinstance(body, dict) else {}
        try:
            if parts == ["screens"] and method == "GET":
                return 200, {"screens": self.cast.screens_view(refresh="refresh=1" in query)}
            if len(parts) == 2 and parts[0] == "screens" and method == "PUT":
                owner = body.get("owner", False)
                e = self.cast.book.set(parts[1], room=body.get("room"), name=body.get("name"), owner=owner)
                return 200, {"screen": parts[1], **{k: e.get(k) for k in ("room", "name", "owner")}}
            if len(parts) == 2 and parts[0] == "screens" and method == "DELETE":
                return 200, {"forgotten": self.cast.book.forget(parts[1])}
            if len(parts) == 3 and parts[0] == "screens" and parts[2] == "pair" and method == "POST":
                return 200, self.cast.pair(parts[1], str(body.get("pin") or "") or None)
            if len(parts) == 2 and parts[0] == "requests" and method == "POST":
                decision = body.get("decision")
                if decision not in ("approve", "refuse"):
                    return 400, {"error": {"message": "decision : approve ou refuse"}}
                st = self.cast.decide(parts[1], decision == "approve", LOCAL_OWNER, machine_owner=True)
                return 200, {"cast": {k: v for k, v in st.items() if k != "html"}}
            if parts == ["consents"] and method == "DELETE":
                return 200, {"revoked": self.cast.book.revoke_consent(str(body.get("member") or ""),
                                                                      str(body.get("screen") or ""))}
        except LookupError as e:
            return 404, {"error": {"message": str(e)}}
        except PermissionError as e:
            return 403, {"error": {"message": str(e)}}
        except Exception as e:  # noqa: BLE001 — appareil injoignable pendant l'appairage
            return 502, {"error": {"message": f"écran : {type(e).__name__}"}}
        return 404, {"error": {"message": "route inconnue"}}

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
        if path.startswith("/v1/local/sessions/") and path.endswith("/mode") and method in ("GET", "PUT"):
            return self._session_mode(method, path[len("/v1/local/sessions/"):-len("/mode")], body)
        if path == "/v1/local/sessions" and method == "GET":
            # `roots` : où une session peut s'ouvrir (le web propose ces dossiers ; un `cwd` hors racine → 403).
            return 200, {"sessions": [self._session_row(t, c) for t, c in self.code.sessions().items()],
                         "roots": list(self.roots)}
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
        if path.split("?", 1)[0] == "/v1/local/cast" or path.startswith("/v1/local/cast/"):
            return self._cast(method, path, body, relayed)
        if path == "/v1/local/resources" and method == "GET":
            # Ce que la machine publie à nokker.ai (X21) : noms seulement, comme envoyé.
            return 200, self.inventory.collect() if self.inventory is not None else {"v": 1}
        if path == "/v1/local/secrets" or path.startswith("/v1/local/secrets/"):
            return self._secrets(method, path, body, relayed)
        if path == "/v1/local/mcp-servers" or path.startswith("/v1/local/mcp-servers/"):
            return self._mcp_servers(method, path, body, relayed)
        if path in ("/v1/local/import/scan", "/v1/local/import/apply") and method == "POST":
            return self._import(path.rsplit("/", 1)[1], body, relayed)
        if self.folder is not None and (path == "/v1/local/folder/runs" or path.startswith("/v1/local/folder/runs/")):
            return self._folder(method, path, body, relayed)
        if path.split("?", 1)[0] == "/v1/local/import/report" and method == "GET":
            return self._import_report(path, relayed)
        if path == "/v1/local/import/memory" or path.startswith("/v1/local/import/memory/") or path == "/v1/local/import/undo":
            return self._import_memory(method, path, body, relayed)
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

    def _folder(self, method: str, path: str, body: Any, relayed: bool) -> tuple[int, dict[str, Any]]:
        """Mode dossier (docs/FOLDER-MODE.md §6). Sur la machine seulement : la passerelle (et donc nokker.ai) ne touche
        jamais les fichiers (§6.4). Dossier et sortie sous une racine déclarée. Plan et exécution tournent en fond ;
        le client suit `folder.run` / `folder.step` ou relit `GET …/runs/{id}`."""
        from nokker.folder.tools import FolderError

        if relayed:
            return 403, {"error": {"message": "mode dossier : sur la machine seulement (jeton local)",
                                   "code": "local_only"}}
        runs = self.folder
        b = body if isinstance(body, dict) else {}
        if path == "/v1/local/folder/runs":
            if method == "GET":
                return 200, {"runs": runs.list()}
            if method != "POST":
                return 405, {"error": {"message": "GET ou POST", "code": "bad_method"}}
            folder, task = str(b.get("path") or ""), b.get("task")
            if not folder or not isinstance(task, str) or not task.strip():
                return 400, {"error": {"message": "path et task requis", "code": "bad_body"}}
            out = str(b["out"]) if b.get("out") else None
            folder_real = os.path.realpath(folder)
            if self._root_of(folder_real) is None or not os.path.isdir(folder_real):
                return 403, {"error": {"message": "dossier hors des racines déclarées", "code": "outside_roots"}}
            from nokker.folder.run import default_out

            out_real = os.path.realpath(out) if out else default_out(folder_real)
            if self._root_of(out_real) is None:
                return 403, {"error": {"message": "sortie hors des racines déclarées", "code": "outside_roots"}}
            try:
                run = runs.create(folder_real, task, in_place=bool(b.get("in_place")), out=out_real)
            except FolderError as e:
                return 400, {"error": {"message": str(e), "code": "bad_folder"}}
            view = run.view()  # avant le fil de fond, qui fait avancer le passage
            threading.Thread(target=runs.plan, args=(run,), name=f"folder-plan-{run.run_id}", daemon=True).start()
            return 202, view
        rest = path[len("/v1/local/folder/runs/"):]
        run_id, _, action = rest.partition("/")
        run = runs.get(run_id)
        if run is None:
            return 404, {"error": {"message": "passage inconnu", "code": "not_found"}}
        try:
            if not action and method == "GET":
                return 200, run.view()
            if action == "decide" and method == "POST":
                decision = b.get("decision")
                if decision not in ("approve", "reject"):
                    return 400, {"error": {"message": "decision ∈ approve, reject", "code": "bad_body"}}
                if run.status != "awaiting_approval":
                    return 409, {"error": {"message": f"passage {run.status} : rien à valider", "code": "not_pending"}}
                if decision == "approve" and run.in_place and b.get("confirm_in_place") is not True:
                    return 400, {"error": {"message": "passage en place : confirm_in_place: true requis",
                                           "code": "confirm_in_place"}}
                runs.decide(run, decision == "approve", confirm_in_place=b.get("confirm_in_place") is True)
                if decision == "reject":
                    return 200, run.view()
                view = run.view()
                threading.Thread(target=runs.execute, args=(run,), name=f"folder-run-{run.run_id}",
                                 daemon=True).start()
                return 202, view
            if action == "undo" and method == "POST":
                if run.status in ("planning", "running"):
                    return 409, {"error": {"message": f"passage {run.status} : attends sa fin", "code": "busy"}}
                return 200, runs.rollback(run, force=b.get("force") is True).view()
        except FolderError as e:
            return 409, {"error": {"message": str(e), "code": "conflict"}}
        return 404, {"error": {"message": "route inconnue", "code": "not_found"}}

    def _import(self, action: str, body: Any, relayed: bool) -> tuple[int, dict[str, Any]]:
        """Import d'un autre agent (lot 30, nokker/importers), pour l'écran « Tu viens de X ? » du premier lancement :
        `scan {source, project?}` → le rapport à blanc ; `apply` → refait le scan puis écrit ce qui est prévu. Sur la
        machine seulement (jeton local) : la passerelle ne lit ni n'écrit les réglages de la machine. `project` doit
        être sous une des racines déclarées. Les serveurs MCP ne sont JAMAIS ajoutés par cette route : le rapport les
        propose, l'écran les fait ajouter dans une session (outil `mcp_server_add`, sa carte forte). Le dossier
        personnel lu est celui du noyau (`NOKKER_IMPORT_HOME`, sinon le sien) : sous docker, seul ce qui y est monté."""
        if relayed:
            return 403, {"error": {"message": "import : sur la machine seulement (jeton local)"}}
        from nokker.importers import claude_code, codex, cursor, hermes, openclaw
        from nokker.importers.apply import apply
        from nokker.tools.packs import user_home

        b = body if isinstance(body, dict) else {}
        source = str(b.get("source") or "")
        if source not in ("claude-code", "codex", "cursor", "hermes", "openclaw"):
            return 400, {"error": {"message": "source ∈ claude-code, codex, cursor, hermes, openclaw", "code": "bad_body"}}
        project = None
        if b.get("project"):
            cwd = os.path.realpath(str(b["project"]))
            if self._root_of(cwd) is None or not os.path.isdir(cwd):
                return 403, {"error": {"message": "dossier hors des racines déclarées"}}
            project = Path(cwd)
        home = Path(os.environ.get("NOKKER_IMPORT_HOME") or Path.home()).expanduser()
        nk = user_home(self.state_dir)
        if source == "claude-code":
            report = claude_code.scan(home, project, nokker_home=nk)
        elif source == "cursor":
            report = cursor.scan(home, project)
        elif source == "codex":
            report = codex.scan(home, project, nokker_home=nk)
        elif source == "hermes":
            report = hermes.scan(home, nokker_home=nk)
        else:
            report = openclaw.scan(home, nokker_home=nk)
        if action == "apply":
            settings = self.settings_path or nk / "settings.json"
            apply(report, user_settings=settings, backup_dir=nk / "imports" / f"{source}-{uuid.uuid4().hex[:8]}",
                  memory=self.project_memory)
            for it in report.items:  # MCP : jamais par ici (carte forte dans une session)
                if it.kind == "mcp" and it.status == "skipped":
                    it.reason = "à ajouter dans une session : Nina le propose, vous signez la carte forte"
        return 200, report.to_json()

    def _import_memory(self, method: str, path: str, body: Any, relayed: bool) -> tuple[int, dict[str, Any]] | None:
        """Mémoire importée d'un autre agent comme DONNÉES (lot 30 d, drapeau `local.import_codex_memory`) :
        `GET /v1/local/import/memory` → les entrées (titre, texte caviardé à l'import, date d'origine) ;
        `POST /v1/local/import/undo {source:"codex", what:"memory", name?}` → défait tout l'import, ou une entrée ;
        `POST /v1/local/import/memory/{nom}/promote` → « retenir en note personnelle » (geste explicite : note user).
        Sur la machine seulement (jeton local) : la passerelle n'y lit ni n'y écrit rien."""
        if relayed:
            return 403, {"error": {"message": "import : sur la machine seulement (jeton local)"}}
        pm = self.project_memory
        if pm is None or not getattr(pm, "import_data", False):
            return 404, {"error": {"message": "mémoire importée désactivée (local.import_codex_memory)", "code": "import_memory_off"}}
        from nokker.context.imported import origin_date

        if path == "/v1/local/import/memory" and method == "GET":
            return 200, {"entries": [{"name": n.name, "title": n.title, "text": n.text, "date": origin_date(n.name),
                                      "source": "codex"} for n in pm.list_import_data()]}
        b = body if isinstance(body, dict) else {}
        if path == "/v1/local/import/undo" and method == "POST":
            if b.get("source") != "codex" or b.get("what") != "memory":
                return 400, {"error": {"message": "corps {source: \"codex\", what: \"memory\", name?}", "code": "bad_body"}}
            name = b.get("name")
            if name is not None and not re.fullmatch(r"codex-[a-z0-9-]{1,120}", str(name)):
                return 400, {"error": {"message": "nom d'entrée invalide", "code": "bad_body"}}
            n = pm.forget_import_data(None if name is None else str(name))
            if name is not None and n == 0:
                return 404, {"error": {"message": "entrée inconnue", "code": "not_found"}}
            return 200, {"undone": n}
        m = re.fullmatch(r"/v1/local/import/memory/(codex-[a-z0-9-]{1,120})/promote", path)
        if m and method == "POST":
            note = pm.promote_import_data(m.group(1))
            if note is None:
                return 404, {"error": {"message": "entrée inconnue", "code": "not_found"}}
            return 200, {"note": {"name": note.name, "title": note.title, "scope": "user"}}
        return None

    def _import_report(self, path: str, relayed: bool) -> tuple[int, dict[str, Any]]:
        """Dernier rapport écrit par `nokker import <source> --state-dir <ce dossier>` lancé sur l'HÔTE (chemin
        principal : les dossiers des autres agents portent des identifiants et ne sont jamais montés dans le
        conteneur). `?source=` facultatif. 404 si aucun rapport. Lecture seule, sur la machine seulement."""
        if relayed:
            return 403, {"error": {"message": "import : sur la machine seulement (jeton local)"}}
        import json
        from urllib.parse import parse_qs

        from nokker.tools.packs import user_home

        q = parse_qs(path.split("?", 1)[1]) if "?" in path else {}
        source = (q.get("source") or [""])[0]
        if source and source not in ("claude-code", "codex", "cursor", "hermes", "openclaw"):
            return 400, {"error": {"message": "source ∈ claude-code, codex, cursor, hermes, openclaw", "code": "bad_body"}}
        root = user_home(self.state_dir) / "imports"
        found = sorted(root.glob(f"{source or '*'}-*/report.json"), key=lambda p: p.parent.name.rsplit("-", 2)[-2:])
        if not found:
            return 404, {"error": {"message": "aucun rapport d'import", "code": "not_found"}}
        try:
            report = json.loads(found[-1].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 500, {"error": {"message": "rapport illisible"}}
        return 200, {**report, "dir": found[-1].parent.name}

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
        before = {m.page_id for m in self.pages.all() if m.moderated_at is not None}
        changed = self.pages.apply_moderation(moderation)
        for meta in changed:
            if self.page_changed is not None:
                self.page_changed(meta)
            # premier retrait vu (un retrait déjà connu, même au redémarrage, est sur disque : pas de 2e alerte)
            if meta.moderated_at is not None and meta.page_id not in before and self.page_moderated is not None:
                try:
                    self.page_moderated(meta)
                except Exception:  # noqa: BLE001 — l'alerte ne doit pas empêcher la page d'être marquée
                    logger.exception("alerte de retrait de la page %s impossible", meta.page_id)
        return changed

    def _link(self, method: str, path: str) -> tuple[int, dict[str, Any]] | None:
        if self.link is None:
            return 404, {"error": {"message": "lien de compte non configuré"}}
        self.__post_init__()  # lien posé après coup (tests, rechargement) : même câblage
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
                out = self.link.unlink()
                self.stop_tunnel()
                return 200, out
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
