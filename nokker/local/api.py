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
        """La source du fil ; un fil de sous-agent `<fil>/sa-<id>` (delegate, couloir features) a celle de son
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
    mirror: Any = None  # nokker.local.mirror.MirrorWorker, posé par serve quand le miroir tourne (L5)
    load: Any = None  # nokker.local.load.LoadReporter, posé par serve (X5) : envoi de la charge, relancé au tunnel
    # Pages de session (docs/BUBBLES.md §10) : ÉTEINTES par défaut tant que les clients publiés ne savent pas afficher
    # un kind de bulle inconnu (la CLI rc.5 plante sur `artifact`) ; `local.pages: true` les allume.
    pages_enabled: bool = False
    # Coffre (X4, nokker/secrets/) : portée `user` derrière /v1/local/secrets, portée `system` sous `setup.vault`.
    vault: Vault | None = None
    _pages: Any = None
    _preview: Any = None

    @property
    def preview_signer(self) -> Any:
        """Liens signés de l'aperçu d'un port (lot X6, docs/LOKKAL.md §7.7)."""
        if self._preview is None:
            from nokker.tools.processes import PreviewSigner

            self._preview = PreviewSigner()
        return self._preview
    # Mémoire de projet (lot X1, docs/LOKKAL.md §7.11) : `local.project_memory` ; None = éteinte, rien ne change.
    project_memory: Any = None

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

        return cls(project_memory=from_local_config(local, state_dir),
                   token=ensure_token(local.get("token_path") or state_dir / "local-token"), roots=roots, setup=setup,
                   state_dir=state_dir, vault=vault, link=link, pages_enabled=bool(local.get("pages", False)),
                   hooks_enabled=bool(local.get("hooks", False)),
                   settings_path=Path(local.get("settings_path") or state_dir / "settings.json").expanduser(),
                   allowed_hosts=frozenset(h.lower() for h in hosts),
                   code=SessionCodeTools(backend=code.get("backend", "docker"),
                                         instance=local.get("instance") or _instance_id(state_dir),
                                         image=code.get("image") or DEFAULT_SANDBOX_IMAGE,
                                         network_name=code.get("network_name"), host_roots=host_roots,
                                         background=bool(local.get("background_processes", False)),
                                         state_dir=str(state_dir)))

    def tool_sources(self) -> list[Any]:
        """Sources d'outils d'une session locale : outils de code par fil enveloppés par les points de retour
        (photo avant chaque écriture), outils des points de retour, skills (L3, couloir features). Tout l'état
        (skills/, checkpoints/) sous `state_dir` = NOKKER_HOME = HERMES_HOME."""
        from nokker.tools.checkpoints import auto_prune, checkpoint_sources
        from nokker.tools.skills import SkillToolSource

        home = self.state_dir

        def root_of(thread_id: str) -> Path | None:
            cwd = self.code.sessions().get(thread_id)
            return Path(cwd) if cwd else None

        from nokker.pages.tools import PageToolSource, PagingToolSource

        wrapper, checkpoints = checkpoint_sources(self.code, root_of, home)
        auto_prune(home)
        memory = []
        if self.project_memory is not None:
            from nokker.context.project_memory import ProjectMemoryTools

            memory = [ProjectMemoryTools(self.project_memory)]
        if not self.pages_enabled:
            return [wrapper, checkpoints, SkillToolSource(home=home), *memory]
        # Pages (§10) : l'enveloppe des pages autour de celle des points de retour (photo avant, version de page après).
        return [PagingToolSource(wrapper, self.pages, root_of), checkpoints, PageToolSource(self.pages, root_of),
                SkillToolSource(home=home), *memory]

    def host_ok(self, host: str | None) -> bool:
        h = (host or "").strip().lower()
        if not h:
            return False
        if h in self.allowed_hosts:
            return True
        return bool(self.relay_host) and h == self.relay_host.lower() and self.tunnel is not None \
            and self.tunnel.state == "up"

    def is_owner(self, authorization: str | None) -> bool:
        auth = authorization or ""
        tok = auth[7:].strip() if auth.startswith("Bearer ") else ""
        return bool(tok) and hmac.compare_digest(tok, self.token)

    def relayed_owner(self, authorization: str | None) -> dict[str, Any] | None:
        """Requête venue par la passerelle : jeton signé par nokker.ai (accès ou délégation, §7.5), vérifié avec les
        clés épinglées au lien, et du compte lié à CETTE machine. Rend ses claims, ou None."""
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
        return claims if claims.get("sub") == account else None

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
        if path == "/v1/local/sessions" and method == "POST":
            cwd = str(body.get("cwd") or "")
            root = self._root_of(cwd) if cwd else None
            if root is None or not os.path.isdir(cwd):
                return 403, {"error": {"message": "dossier hors des racines déclarées"}}
            thread_id = str(body.get("thread_id") or f"th_{uuid.uuid4().hex[:16]}")
            self.code.open(thread_id, os.path.realpath(cwd), root)
            extra = self.on_session_open(thread_id, os.path.realpath(cwd)) if self.on_session_open is not None else None
            if self.project_memory is not None:  # consignes du dépôt importées, clé de projet posée (ne lève pas)
                self.project_memory.open_session(thread_id, os.path.realpath(cwd), root)
            return 201, {**(extra if isinstance(extra, dict) else {}), "thread_id": thread_id,
                         "cwd": os.path.realpath(cwd)}
        if path == "/v1/local/hooks/trust" and method == "POST":
            # Faire confiance au code d'un dépôt se fait sur la machine (jeton local), jamais par la passerelle.
            if relayed:
                return 403, {"error": {"message": "confiance des hooks : sur la machine seulement (jeton local)"}}
            if self.hooks is None:
                return 404, {"error": {"message": "hooks désactivés (local.hooks)"}}
            return self.hooks.trust(str(body.get("thread_id") or ""), str(body.get("sha256") or ""))
        if path == "/v1/local/sessions" and method == "GET":
            return 200, {"sessions": [{"thread_id": t, "cwd": c} for t, c in self.code.sessions().items()]}
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
        if path == "/v1/local/mirror" or path.startswith("/v1/local/mirror/"):
            return self._mirror(method, path)
        if path == "/v1/local/secrets" or path.startswith("/v1/local/secrets/"):
            return self._secrets(method, path, body, relayed)
        return None

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

    def _mirror(self, method: str, path: str) -> tuple[int, dict[str, Any]] | None:
        """Miroir web ⇄ machine (L5) : état, effacement des copies de la machine chez nokker.ai (envoi en pause),
        reprise (tout est renvoyé)."""
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
        return None

    def _link(self, method: str, path: str) -> tuple[int, dict[str, Any]] | None:
        if self.link is None:
            return 404, {"error": {"message": "lien de compte non configuré"}}
        import socket

        try:
            if path == "/v1/local/link/start" and method == "POST":
                return 200, self.link.start(socket.gethostname())
            if path == "/v1/local/link/status" and method == "GET":
                return 200, self.link.status()
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
                return 200, setup.view()
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


__all__ = ["LOCAL_OWNER", "LocalApi", "SessionCodeTools", "ensure_token"]
