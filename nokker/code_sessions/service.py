"""Service des sessions de code multi-utilisateur (docs/LOKKAL.md §7.28).

Une session appartient à UN compte (`user_id`, `us_…`) dans SON locataire (`tenant_id`) :
  - racine `<workspace_root>/<tenant>/<user>/<session>` (`paths.session_root`), fil `<tenant>:<session>`
    (`scoped_thread_id`) ;
  - un bac à sable par session ouverte (`CodeToolSource`, backend docker ou kubernetes), libéré après `idle_ttl_s`
    sans appel (l'espace de travail reste ; le prochain appel en rouvre un) et à la fermeture ;
  - les outils de code ne servent que le fil de la session, appelé dans le locataire de la session, par son
    propriétaire (ou une carte qu'il a décidée) ; tout autre appel répond « pas de session de code sur ce fil » ;
  - portes : celles de Lokkal (`LocalPermissions` : mode du fil, restrictions du `.nokker/settings.json` du dépôt et des
    projets d'équipe — restreindre seulement), devant les portes du noyau ; un fil hors session garde les portes du
    noyau seules ; les cartes passent par les approbations multi-locataire existantes ;
  - hooks (drapeau `code.hooks`) : ceux du dépôt, après confiance de l'utilisateur, dans le bac à sable ;
  - skills du dépôt (`code.project_skills`, `skills.py`) : après confiance du propriétaire, rangée chez lui ;
  - sous-agents (`code.max_subagents`, `subagents.py`) : fils `<fil>/sa-<id>` de la session, dans SON bac à sable, avec
    ses portes ; arrêtés à la fermeture ;
  - dépôt de travail : clone https d'une forge autorisée avec un identifiant référencé par NOM (`forge.py`,
    `secrets.py`).
"""
from __future__ import annotations

import logging
import os
import re
import secrets as _secrets
import shutil
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nokker.code_sessions import forge
from nokker.code_sessions.config import CodeConfig, k8s_labels
from nokker.code_sessions.paths import PathRefused, check_ids, purge_root, relative_root, session_root
from nokker.code_sessions.store import LIVE, BaseCodeStore, CodeSession
from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import ToolResult
from nokker.secrets.base import SecretsError, check_name, check_value
from nokker.tenancy.context import NoTenant, current_tenant, scoped_thread_id, tenant_of_thread, tenant_scope
from nokker.tenancy.resolve import is_account

logger = logging.getLogger("nokker.code_sessions")

SETTINGS_REL = (".nokker", "settings.json")
MAX_SETTINGS_BYTES = 256 * 1024
_REF = re.compile(r"^[A-Za-z0-9._/][A-Za-z0-9._/+-]{0,199}$")
_SECRET_KEYS = ("token", "password", "value", "secret", "auth", "username")
_SESSION_THREAD = re.compile(r"^tn_[A-Za-z0-9_-]+:cs_[0-9a-f]{16}$")
SUBAGENT_MARK = "/sa-"
NO_SESSION = "pas de session de code sur ce fil (POST /v1/code/sessions)"


class CodeError(Exception):
    """Erreur rendue au client : `{"error": {"code", "message"}}` avec `status`. Jamais de valeur de secret."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None


def safe_repo_file(root: str, parts: tuple[str, ...] = SETTINGS_REL) -> str | None:
    """Chemin d'un fichier du dépôt lu PAR LE NOYAU, ou None : aucun composant ne doit être un lien symbolique (un dépôt
    cloné en contient peut-être un vers un fichier du noyau), le fichier doit être régulier et borné."""
    cur = root
    for i, part in enumerate(parts):
        cur = os.path.join(cur, part)
        try:
            st = os.lstat(cur)
        except OSError:
            return None
        if stat.S_ISLNK(st.st_mode):
            return None
        last = i == len(parts) - 1
        if (last and (not stat.S_ISREG(st.st_mode) or st.st_size > MAX_SETTINGS_BYTES)) or \
                (not last and not stat.S_ISDIR(st.st_mode)):
            return None
    return cur


def repo_settings_state(root: str) -> str:
    """absent | ok | unsafe (lien symbolique, pas un fichier, trop gros)."""
    if not os.path.lexists(os.path.join(root, *SETTINGS_REL)) and not os.path.lexists(os.path.join(root, ".nokker")):
        return "absent"
    return "ok" if safe_repo_file(root) is not None else "unsafe"


@dataclass
class _Open:
    tenant: str
    source: Any
    last: float


@dataclass
class CodeSessions:
    cfg: CodeConfig
    store: BaseCodeStore
    secrets: Any = None  # TenantSecrets (coffre par locataire) ; None : aucun dépôt avec identifiant
    cloner: Callable[..., None] = forge.clone
    clock: Callable[[], float] = time.time
    hub: Any = None  # BubbleHub : le fil de la session est créé au nom de son propriétaire (membres, 403 sinon)
    note: Callable[[str, str], None] | None = None  # Kernel.add_owner_note : une ligne au prochain tour du fil
    # (compte, URL du dépôt) -> [règles] des projets d'équipe qui déclarent ce dépôt (nokker/team/), restreindre seulement
    team_rules: Callable[[str, str], list[dict[str, Any]]] | None = None
    scrub: Callable[[str], str] = lambda s: s  # caviardage du noyau (sorties de hooks)
    redact: Any = None  # invariants.redact_result (charge d'un post_tool)
    checkpoints: Any = None  # SessionCheckpoints (un magasin par session) : oublié à la fermeture
    minor: Callable[[str], bool] | None = None  # foyer (§7.29) : compte → profil mineur ; un mineur n'ouvre pas de session
    # Sous-agents (`subagents.py`) : (registre filtré, fil de l'enfant, observateur) -> noyau de l'enfant, une copie du
    # noyau parent prise quand il part (`serve` : `dataclasses.replace`) ; None = aucun sous-agent ne part.
    kernel_factory: Callable[..., Any] | None = None
    parent_registry: Callable[[], Any] | None = None  # le registre du locataire courant (filtré pour l'enfant)
    emit: Callable[[str, dict[str, Any]], Any] | None = None  # EventBus.publish : subagent.*, skill.untrusted
    parent_observer: Any = None  # BubbleHub : l'enfant le nourrit (son fil y naît au nom du propriétaire)
    skills_fallback: Callable[[], Any] | None = None  # skills du compte (packs du web), hors session et après le dépôt
    _open: dict[str, _Open] = field(default_factory=dict, init=False, repr=False)
    _perms: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _hooks: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _creating: dict[tuple[str, str], threading.Lock] = field(default_factory=dict, init=False, repr=False)
    _tools: Any = field(default=None, init=False, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _skills: dict[str, tuple[Any, Any]] = field(default_factory=dict, init=False, repr=False)
    _delegates: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _child_dirs: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _skill_tools: Any = field(default=None, init=False, repr=False)
    _subagent_tools: Any = field(default=None, init=False, repr=False)

    # ─────────────────────────────── sessions ───────────────────────────────

    def view(self, s: CodeSession) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": s.id, "thread_id": s.id, "status": s.status, "mode": s.mode, "workspace": self.cfg.mount,
            "backend": self.cfg.backend,
            "repo": {"url": s.repo_url, "ref": s.repo_ref, "credential": s.credential} if s.repo_url else None,
            "created_at": _iso(s.created_at), "last_used_at": _iso(s.last_used_at)}
        if self.cfg.hooks:
            runner = self._hooks.get(s.id)
            out["hooks"] = (runner.status(s.thread_id) if runner is not None else None) or \
                {"enabled": True, "project": "invalid" if s.repo_url and self._settings_unsafe(s) else "absent"}
        if self.cfg.project_skills:
            found = self._skills_of(s)
            out["skills"] = (found[0].status(s.thread_id) if found is not None else None) or \
                {"enabled": True, "project": "absent", "root": self.cfg.mount, "sha256": None, "count": 0,
                 "notice": None}
        if self.cfg.max_subagents > 0:
            d = self._delegates.get(s.id)
            out["subagents"] = {"limit": self.cfg.max_subagents, "live": d.live_count() if d is not None else 0}
        return out

    def _settings_unsafe(self, s: CodeSession) -> bool:
        try:
            return repo_settings_state(self._root(s)) == "unsafe"
        except PathRefused:
            return False

    def _root(self, s: CodeSession) -> str:
        tenant, user, sid = s.root_rel.split("/")
        return session_root(self.cfg.workspace_root, tenant, user, sid, create=False)

    def _own(self, user: str, session_id: str) -> CodeSession:
        s = self.store.get(session_id) if isinstance(session_id, str) else None
        if s is None or s.user_id != user or s.tenant_id != current_tenant() or s.status not in LIVE:
            raise CodeError(404, "not_found", "session inconnue")
        return s

    def create(self, user: str, body: dict[str, Any]) -> dict[str, Any]:
        tenant = current_tenant()
        if not isinstance(body, dict):
            raise CodeError(400, "bad_body", "corps JSON (objet) attendu")
        try:
            check_ids(tenant, user, "cs_" + "0" * 16)
        except PathRefused:
            raise CodeError(403, "account_required", "session de code : compte requis") from None
        if self.minor is not None and self.minor(user):
            raise CodeError(403, "minor_not_allowed", "sessions de code : pas pour un profil mineur")
        mode = str(body.get("mode") or self.cfg.default_mode)
        if mode not in self.cfg.modes:
            raise CodeError(400, "bad_mode", f"mode refusé : {mode!r} (parmi {', '.join(self.cfg.modes)})")
        repo = body.get("repo")
        url = ref = cred = token = None
        depth = None
        if repo is not None:
            if not isinstance(repo, dict):
                raise CodeError(400, "bad_repo", "repo : {url, credential?, ref?, depth?}")
            if any(k in repo for k in _SECRET_KEYS):
                raise CodeError(400, "secret_value_refused", "une valeur d'identifiant n'entre jamais dans la requête "
                                                             "de session : PUT /v1/code/credentials/{nom}, puis "
                                                             "credential: <nom>")
            try:
                url = forge.parse_repo(str(repo.get("url") or ""), self.cfg.forge_hosts)
            except forge.ForgeError as e:
                raise CodeError(400, e.code, e.message) from None
            if repo.get("ref") is not None:
                ref = str(repo["ref"])
                if not _REF.fullmatch(ref) or ".." in ref or ref.startswith("-"):
                    raise CodeError(400, "bad_ref", "ref : nom de branche ou d'étiquette")
            if repo.get("depth") is not None:
                d = repo["depth"]
                if isinstance(d, bool) or not isinstance(d, int) or not 1 <= d <= 100_000:
                    raise CodeError(400, "bad_depth", "depth : entier de 1 à 100000")
                depth = d
            if repo.get("credential") is not None:
                cred = str(repo["credential"])
                token = self._credential_value(tenant, cred)
        with self._lock:
            user_lock = self._creating.setdefault((tenant, user), threading.Lock())
        # Un verrou par compte (plafond sans course entre deux ouvertures) : un clone long ne bloque personne d'autre.
        with user_lock:
            if self.store.count_live(user) >= self.cfg.max_sessions_per_user:
                raise CodeError(409, "session_limit", f"{self.cfg.max_sessions_per_user} sessions de code ouvertes "
                                                      "au plus par utilisateur : fermes-en une")
            sid = "cs_" + _secrets.token_hex(8)
            try:
                root = session_root(self.cfg.workspace_root, tenant, user, sid)
            except PathRefused as e:
                raise CodeError(500, "workspace_refused", str(e)) from None
            now = self.clock()
            s = CodeSession(id=sid, tenant_id=tenant, user_id=user, thread_id=scoped_thread_id(tenant, sid),
                            root_rel=relative_root(tenant, user, sid), mode=mode, repo_url=url, repo_ref=ref,
                            credential=cred, created_at=now, last_used_at=now)
            if url is not None:
                try:
                    self.cloner(url, root, token, ref=ref, depth=depth, timeout=self.cfg.clone_timeout_s)
                except forge.ForgeError as e:
                    purge_root(self.cfg.workspace_root, tenant, user, sid)
                    msg = forge.redact(e.message, [token] if token else [])
                    logger.info("session de code : clone refusé (%s, %s)", e.code, url)
                    raise CodeError(503 if e.code == "git_missing" else 502, e.code, msg) from None
            self.store.put(s)
        self._prepare(s, root)
        if self.hub is not None:
            try:
                self.hub.ensure_thread_for_turn(s.thread_id, user, "session de code" + (f" — {url}" if url else ""))
            except Exception:  # noqa: BLE001 — le fil naîtra au premier tour
                logger.warning("fil de la session %s non créé au hub", sid, exc_info=True)
        if self.note is not None:
            self.note(s.thread_id, f"[session de code] dossier de travail {self.cfg.mount}"
                                   + (f" (dépôt {url}" + (f", {ref}" if ref else "") + ")" if url else "")
                                   + f", mode {mode} ; outils read_file, search_files, write_file, patch, terminal, "
                                     "todo, points de retour")
        logger.info("session de code %s ouverte (%s, %s)", sid, tenant, self.cfg.backend)
        return self.view(s)

    def _credential_value(self, tenant: str, name: str) -> str:
        try:
            check_name(name)
        except SecretsError:
            raise CodeError(400, "bad_name", "credential : nom de secret invalide") from None
        if self.secrets is None:
            raise CodeError(503, "vault_unavailable", "pas de coffre pour les identifiants de forge (code.secrets)")
        try:
            value = self.secrets.for_tenant(tenant).get(name)
        except SecretsError as e:
            raise CodeError(503, "vault_unavailable", str(e)) from None
        if not value:
            raise CodeError(400, "credential_missing", f"identifiant {name} absent du coffre de ce locataire")
        return value

    def list(self, user: str) -> dict[str, Any]:
        return {"sessions": [self.view(s) for s in self.store.list_user(user, live_only=True)],
                "limit": self.cfg.max_sessions_per_user, "modes": list(self.cfg.modes),
                "default_mode": self.cfg.default_mode, "idle_ttl_s": self.cfg.idle_ttl_s}

    def get(self, user: str, session_id: str) -> dict[str, Any]:
        return self.view(self._own(user, session_id))

    def close(self, user: str, session_id: str, purge: bool = False) -> dict[str, Any]:
        s = self._own(user, session_id)
        stopped = self._stop_children(s)  # AVANT le bac à sable : leurs outils se taisent, leurs noyaux s'arrêtent
        self._release(s.id)
        with self._lock:
            self._perms.pop(s.id, None)
            runner = self._hooks.pop(s.id, None)
            self._skills.pop(s.id, None)
        if runner is not None:
            runner.close(s.thread_id)
        self.store.update(s.id, status="closed", closed_at=self.clock())
        purged = False
        if self.checkpoints is not None:
            self.checkpoints.forget(s.id)
        if purge:
            tenant, u, sid = s.root_rel.split("/")
            purged = purge_root(self.cfg.workspace_root, tenant, u, sid)
            home = self.checkpoint_home(tenant, u, sid, create=False)
            if os.path.lexists(home):  # ses points de retour partent avec elle
                if os.path.islink(home):
                    os.unlink(home)
                else:
                    shutil.rmtree(home)
        logger.info("session de code %s fermée (espace %s, %d sous-agent(s) arrêté(s))", s.id,
                    "effacé" if purged else "gardé", stopped)
        return {"id": s.id, "closed": True, "purged": purged}

    def _stop_children(self, s: CodeSession) -> int:
        """Fermeture ou effacement d'une session : ses sous-agents (fond, premier plan, en attente d'une carte) sont
        arrêtés (`interrupted / cancelled`, noyau annulé, outils muets, carte refusée), leur état est retiré ; leurs
        worktrees restent dans l'espace de travail (`purge=1` les efface avec lui)."""
        with self._lock:
            d = self._delegates.pop(s.id, None)
            for tid in [t for t in self._child_dirs if base_thread(t) == s.thread_id]:
                self._child_dirs.pop(tid, None)
        n = 0
        if d is not None:
            d.state_path = None  # plus rien n'est écrit pour elle (le fil de garde d'un lot de fond finit plus tard)
            try:
                n = d.cancel_children(s.thread_id)
            except Exception:  # noqa: BLE001 — la fermeture n'échoue jamais pour un enfant
                logger.warning("sous-agents de %s : arrêt en échec", s.id, exc_info=True)
        state = self._subagents_state(s)
        try:
            if state.is_symlink() or state.is_file():
                state.unlink()
        except OSError:
            logger.warning("état des sous-agents de %s non retiré", s.id, exc_info=True)
        return n

    # ─────────────────────────────── fil → session ───────────────────────────────

    def session_for_thread(self, thread_id: str) -> CodeSession | None:
        """Session vivante du fil, dans le locataire courant (None hors session ou d'un autre locataire). Le fil d'un
        sous-agent `<fil>/sa-<id>` est celui de sa session : mêmes portes, même bac à sable, même propriétaire."""
        tenant = current_tenant()
        if not isinstance(thread_id, str) or tenant_of_thread(thread_id) != tenant:
            return None
        s = self.store.by_thread(base_thread(thread_id))
        return s if s is not None and s.tenant_id == tenant and s.status in LIVE else None

    def is_code_thread(self, thread_id: str) -> bool:
        try:
            return self.session_for_thread(thread_id) is not None
        except NoTenant:
            return False

    def source_for_thread(self, thread_id: str) -> tuple[CodeSession, Any] | None:
        s = self.session_for_thread(thread_id)
        if s is None:
            return None
        with self._lock:
            o = self._open.get(s.id)
            if o is None or o.tenant != s.tenant_id:
                o = self._open[s.id] = _Open(tenant=s.tenant_id, source=self._make_source(s), last=self.clock())
                if s.status != "active":
                    s = self.store.update(s.id, status="active", last_used_at=o.last) or s
            o.last = self.clock()
        return s, o.source

    def checkpoint_home(self, tenant: str, user: str, session: str, *, create: bool = True) -> Path:
        """Home de l'amont pour les points de retour de CETTE session : `<state_dir>/<tenant>/<user>/checkpoints/
        <session>` (0700), hors de toute racine montée dans un bac à sable (`code.state_dir`, contrôlé au démarrage)."""
        check_ids(tenant, user, session)
        home = Path(self.cfg.state_dir) / tenant / user / "checkpoints" / session
        if create:
            home.mkdir(mode=0o700, parents=True, exist_ok=True)
            if home.is_symlink() or not home.is_dir():
                raise PathRefused("dossier des points de retour : lien symbolique refusé")
        return home

    def touch(self, session_id: str) -> None:
        with self._lock:
            o = self._open.get(session_id)
            if o is not None:
                o.last = self.clock()

    def root_of(self, thread_id: str) -> Path | None:
        """Racine (côté noyau) du fil d'une session du locataire courant : points de retour."""
        try:
            s = self.session_for_thread(thread_id)
            return Path(self._root(s)) if s is not None else None
        except (NoTenant, PathRefused):
            return None

    def _make_source(self, s: CodeSession) -> Any:
        from nokker.tools.code import DEFAULT_SANDBOX_IMAGE, CodeToolSource

        root = self._root(s)
        if self.cfg.backend == "kubernetes":
            return CodeToolSource(root=root, backend="kubernetes", env_factory=self.cfg.env_factory,
                                  k8s=self.cfg.k8s_cfg(), sub_path=s.root_rel, instance=self.cfg.instance,
                                  labels=k8s_labels(self.cfg.instance, s.tenant_id, s.id))
        d = self.cfg.docker
        host = d.get("host_root")
        return CodeToolSource(root=root, backend="docker",
                              image=str(d.get("image") or os.environ.get("NOKKER_SANDBOX_IMAGE") or DEFAULT_SANDBOX_IMAGE),
                              network=bool(d.get("network", True)), network_name=d.get("network_name"),
                              host_root=os.path.join(str(host), s.root_rel) if host else None,
                              instance=self.cfg.instance)

    def _release(self, session_id: str) -> None:
        with self._lock:
            o = self._open.pop(session_id, None)
        if o is not None:
            try:
                o.source.close()
            except Exception:  # noqa: BLE001 — libération au mieux
                logger.warning("bac à sable de %s : fermeture en échec", session_id, exc_info=True)

    def reap_idle(self, now: float | None = None) -> int:
        """Libère le bac à sable des sessions sans appel depuis `idle_ttl_s` ; l'espace de travail reste, la session
        passe `idle` (le prochain appel la rouvre). Rend le nombre libéré."""
        now = self.clock() if now is None else now
        with self._lock:
            expired = [(sid, o) for sid, o in self._open.items() if now - o.last >= self.cfg.idle_ttl_s]
            for sid, _ in expired:
                self._open.pop(sid, None)
        for sid, o in expired:
            try:
                o.source.close()
            except Exception:  # noqa: BLE001
                logger.warning("bac à sable de %s : fermeture en échec", sid, exc_info=True)
            try:
                with tenant_scope(o.tenant):
                    s = self.store.get(sid)
                    if s is not None and s.status == "active":
                        self.store.update(sid, status="idle", last_used_at=o.last)
            except Exception:  # noqa: BLE001 — base injoignable : l'état sera corrigé au prochain appel
                logger.warning("session %s : état idle non écrit", sid, exc_info=True)
        if expired:
            logger.info("sessions de code : %d bac(s) à sable libéré(s) (inactivité)", len(expired))
        return len(expired)

    def start_reaper(self) -> threading.Thread:
        period = max(5.0, min(60.0, self.cfg.idle_ttl_s / 4))

        def loop() -> None:
            while not self._stop.wait(period):
                try:
                    self.reap_idle()
                except Exception:  # noqa: BLE001 — le balayeur ne meurt jamais
                    logger.warning("balayeur des sessions de code en échec", exc_info=True)

        t = threading.Thread(target=loop, name="nokker-code-reaper", daemon=True)
        t.start()
        return t

    def close_all(self) -> None:
        self._stop.set()
        with self._lock:
            opened = list(self._open)
            delegates = list(self._delegates.values())
        for d in delegates:  # arrêt du noyau : leurs enfants aussi (relus `interrupted / restart` au prochain départ)
            try:
                d.cancel_all()
            except Exception:  # noqa: BLE001
                logger.warning("sous-agents : arrêt en échec", exc_info=True)
        for sid in opened:
            self._release(sid)

    def run_hook(self, thread_id: str, command: str, stdin: str, timeout: int) -> tuple[int | None, str]:
        """Commande d'un hook dans le bac à sable de la session du fil (jamais dans le noyau). Locataire tiré du fil :
        un hook de début ou de fin de tour tourne dans un fil d'exécution sans contexte."""
        with tenant_scope(tenant_of_thread(thread_id)):
            found = self.source_for_thread(thread_id)
            if found is None:
                raise LookupError(NO_SESSION)
            # un sous-agent : le bac à sable de sa session (jamais un second)
            return found[1].run_hook(found[0].thread_id, command, stdin, timeout)

    # ─────────────────────────────── portes et hooks ───────────────────────────────

    def permissions_for(self, thread_id: str) -> Any:
        """`LocalPermissions` du fil d'une session (mode, restrictions du dépôt et des équipes), ou None."""
        try:
            s = self.session_for_thread(thread_id)
        except NoTenant:
            return None
        if s is None:
            return None
        with self._lock:
            p = self._perms.get(s.id)
        if p is None:
            try:
                p = self._prepare(s, self._root(s))
            except PathRefused:
                return None
        return p

    def _prepare(self, s: CodeSession, root: str) -> Any:
        """Permissions du fil (mode de la session, `.nokker/settings.json` du dépôt s'il est sûr, projets d'équipe) et,
        drapeau `code.hooks`, hooks du dépôt — à l'ouverture, ou au premier appel après un redémarrage."""
        from nokker.qa.permissions import LocalPermissions, Mode

        perms = LocalPermissions(mode=Mode(s.mode))
        settings = safe_repo_file(root)
        perms.restrict_for_thread(s.thread_id, settings or "")
        if self.team_rules is not None and s.repo_url:
            try:
                for rules in self.team_rules(s.user_id, s.repo_url) or []:
                    perms.restrict_with(s.thread_id, rules if isinstance(rules, dict) else {})
            except Exception:  # noqa: BLE001 — équipe illisible : le dépôt et le mode restent
                logger.warning("règles d'équipe de la session %s illisibles", s.id, exc_info=True)
        if self.cfg.project_skills:
            from nokker.code_sessions.skills import make_project_skills

            project, catalog = make_project_skills(state_dir=self.cfg.state_dir, tenant=s.tenant_id, user=s.user_id,
                                                   session_id=s.id, root=root, mount=self.cfg.mount,
                                                   mode_of=perms.mode_for, emit=self._emitter(s))
            project.open_session(s.thread_id, root, root)
            with self._lock:
                self._skills[s.id] = (project, catalog)
        runner = None
        if self.cfg.hooks:
            from nokker.tools.hooks import HookRunner

            runner = HookRunner(sandbox=self.run_hook, scrub=self.scrub, redact=self.redact, mode_of=perms.mode_for,
                                user_settings=Path(self.cfg.state_dir) / s.tenant_id / s.user_id / "settings.json")
            if repo_settings_state(root) != "unsafe":
                runner.open_session(s.thread_id, root)
        with self._lock:
            self._perms[s.id] = perms
            if runner is not None:
                self._hooks[s.id] = runner
        return perms

    def runner_for(self, thread_id: str) -> Any:
        if not self.cfg.hooks:
            return None
        try:
            s = self.session_for_thread(thread_id)
        except NoTenant:
            return None
        if s is None:
            return None
        if s.id not in self._hooks:
            self.permissions_for(thread_id)
        return self._hooks.get(s.id)

    def trust_hooks(self, user: str, session_id: str, sha256: str) -> dict[str, Any]:
        s = self._own(user, session_id)
        runner = self.runner_for(s.thread_id)
        if runner is None:
            raise CodeError(404, "not_found", "hooks désactivés (code.hooks) ou pas de hooks de dépôt")
        status, body = runner.trust(s.thread_id, str(sha256 or ""))
        if status != 200:
            raise CodeError(status, "hooks_" + ("changed" if status == 409 else "absent"),
                            str((body.get("error") or {}).get("message") or "refusé"))
        return {**body, "thread_id": s.id}

    def tools(self) -> TenantCodeTools:
        if self._tools is None:
            self._tools = TenantCodeTools(self)
        return self._tools

    # ─────────────────────────────── skills du dépôt ───────────────────────────────

    def skill_tools(self) -> Any:
        """`skill_list` / `skill_view` des comptes (`skills.py`) : le dépôt de la session d'abord, puis le compte."""
        if self._skill_tools is None:
            from nokker.code_sessions.skills import CodeSessionSkills

            self._skill_tools = CodeSessionSkills(self, fallback=lambda: self.skills_fallback()
                                                  if self.skills_fallback is not None else None)
        return self._skill_tools

    def _skills_of(self, s: CodeSession) -> tuple[Any, Any] | None:
        with self._lock:
            found = self._skills.get(s.id)
        if found is None and self.cfg.project_skills:
            try:
                self._prepare(s, self._root(s))  # après un redémarrage : relu au premier usage
            except PathRefused:
                return None
            with self._lock:
                found = self._skills.get(s.id)
        return found

    def skills_for_thread(self, thread_id: str) -> tuple[CodeSession, Any, Any] | None:
        """(session, skills du dépôt, catalogue) du fil d'une session vivante du locataire courant (ou d'un de ses
        sous-agents) ; skills éteints : (session, None, None) ; hors session : None."""
        s = self.session_for_thread(thread_id)
        if s is None:
            return None
        if not self.cfg.project_skills:
            return s, None, None
        found = self._skills_of(s)
        return (s, *found) if found is not None else (s, None, None)

    def trust_skills(self, user: str, session_id: str, sha256: str) -> dict[str, Any]:
        s = self._own(user, session_id)
        if not self.cfg.project_skills:
            raise CodeError(404, "not_found", "skills du dépôt désactivés (code.project_skills)")
        found = self._skills_of(s)
        if found is None:
            raise CodeError(404, "skills_absent", "pas de skills de dépôt dans cette session")
        status, body = found[0].trust(s.thread_id, str(sha256 or ""))
        if status != 200:
            code = {409: "skills_changed", 404: "skills_absent"}.get(status, "skills_unavailable")
            raise CodeError(status, code, str((body.get("error") or {}).get("message") or "refusé"))
        return {**body, "thread_id": s.id}

    # ─────────────────────────────── sous-agents ───────────────────────────────

    def subagent_tools(self) -> Any:
        """`delegate`, `delegate_status`, `delegate_cancel` des comptes (`subagents.py`)."""
        if self._subagent_tools is None:
            from nokker.code_sessions.subagents import SessionSubagents

            self._subagent_tools = SessionSubagents(self)
        return self._subagent_tools

    def _subagents_state(self, s: CodeSession) -> Path:
        return Path(self.cfg.state_dir) / s.tenant_id / s.user_id / "subagents" / f"{s.id}.json"

    def delegate_for(self, s: CodeSession) -> Any:
        """La `DelegateToolSource` de CETTE session (ses enfants, son plafond, son état), créée au premier usage ; None
        sans fabrique de noyau d'enfant."""
        if self.kernel_factory is None or self.cfg.max_subagents <= 0:
            return None
        from nokker.tools.delegate import DelegateToolSource

        with self._lock:
            d = self._delegates.get(s.id)
            if d is None:
                n = self.cfg.max_subagents
                d = DelegateToolSource(
                    lambda registry, thread_id, observer: self.kernel_factory(registry, thread_id, observer),
                    self.tools(), max_children=n, max_background=n, max_live=n, require_isolation=True,
                    background=True, emit=self._emitter(s), parent_observer=self.parent_observer,
                    note=self.note, state_path=self._subagents_state(s))
                d.bind(self._parent_registry)
                self._delegates[s.id] = d
        return d

    def _parent_registry(self) -> Any:
        return self.parent_registry() if self.parent_registry is not None else None

    def set_child_dir(self, thread_id: str, subdir: str) -> None:
        with self._lock:
            self._child_dirs[thread_id] = subdir

    def child_dir(self, thread_id: str) -> str:
        with self._lock:
            return self._child_dirs.get(thread_id, "")

    def drop_child(self, thread_id: str) -> None:
        """Fin d'un enfant isolé : son espace dans le bac à sable de la session est rendu (le bac à sable reste)."""
        sid = base_thread(thread_id).partition(":")[2]
        with self._lock:
            self._child_dirs.pop(thread_id, None)
            o = self._open.get(sid)
        if o is not None:
            o.source.close(thread_id)

    def subagent_kernel(self, thread_id: str) -> Any:
        """Noyau de l'enfant qui a posé une carte sur `thread_id` (exécuteur d'approbation), ou None : jamais celui du
        parent."""
        if SUBAGENT_MARK not in thread_id:
            return None
        with tenant_scope(tenant_of_thread(thread_id)):
            s = self.session_for_thread(thread_id)
        with self._lock:
            d = self._delegates.get(s.id) if s is not None else None
        return d.kernel_for_card(thread_id) if d is not None else None

    def owner_of_thread(self, thread_id: str) -> str | None:
        """Propriétaire de la session de code d'un fil (ou d'un de ses sous-agents), quel que soit son état ; None hors
        session. Seul décideur des cartes de ces fils (`BaseApprovals.thread_owner`)."""
        base = base_thread(thread_id) if isinstance(thread_id, str) else ""
        if not _SESSION_THREAD.fullmatch(base):
            return None
        with tenant_scope(tenant_of_thread(base)):
            s = self.store.by_thread(base)
        return s.user_id if s is not None else None

    def _emitter(self, s: CodeSession) -> Callable[[str, dict[str, Any]], None]:
        """Événements d'une session : au propriétaire seulement (`_audience`), dans son locataire."""
        def emit(name: str, payload: dict[str, Any]) -> None:
            if self.emit is not None:
                self.emit(name, {**payload, "_audience": [s.user_id], "_tenant": s.tenant_id})
        return emit

    def gates(self, inner: Any) -> CodeSessionGates:
        return CodeSessionGates(inner=inner, sessions=self)

    def wrap_hooks(self, registry: Any) -> Any:
        from nokker.tools.hooks import HookedTools

        return HookedTools(inner=registry, runner=_HookDispatch(self))

    # ─────────────────────────────── identifiants de forge ───────────────────────────────

    def _vault(self) -> Any:
        if self.secrets is None:
            raise CodeError(503, "vault_unavailable", "pas de coffre pour les identifiants de forge (code.secrets)")
        return self.secrets.for_tenant(current_tenant())

    def credentials(self) -> dict[str, Any]:
        try:
            items = self._vault().list()
        except SecretsError as e:
            raise CodeError(503, "vault_unavailable", str(e)) from None
        return {"credentials": [{"name": i.name, "updated_at": i.updated_at.isoformat() if i.updated_at else None}
                                for i in items]}

    def put_credential(self, name: str, value: Any) -> dict[str, Any]:
        try:
            check_name(name)
            check_value(value)
            self._vault().set(name, value)
        except SecretsError as e:
            raise CodeError(400 if e.code in ("bad_name", "bad_value") else 503, e.code, str(e)) from None
        return {"name": name, "set": True}

    def delete_credential(self, name: str) -> dict[str, Any]:
        try:
            check_name(name)
            gone = self._vault().delete(name)
        except SecretsError as e:
            raise CodeError(400 if e.code == "bad_name" else 503, e.code, str(e)) from None
        if not gone:
            raise CodeError(404, "not_found", f"identifiant {name} absent")
        return {"name": name, "deleted": True}


class TenantCodeTools:
    """`ToolSource` des outils de code pour les comptes : route chaque appel vers le bac à sable de la session du fil,
    dans le locataire courant, pour le propriétaire de la session seulement."""

    def __init__(self, sessions: CodeSessions) -> None:
        self.sessions = sessions
        self._specs: list[ToolSpec] | None = None

    @property
    def mount(self) -> str:  # points de retour : chemins du bac à sable → chemins du volume
        return self.sessions.cfg.mount

    def specs(self) -> list[ToolSpec]:
        if self._specs is None:
            from nokker.tools.code import CodeToolSource

            self._specs = CodeToolSource(root=os.getcwd(), backend="local").specs()
        return self._specs

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def err(text: str) -> ToolResult:
            return ToolResult(call_id="", name=name, content=text, is_error=True)

        try:
            found = self.sessions.source_for_thread(ctx.thread_id)
        except NoTenant:
            return err(NO_SESSION)
        if found is None:
            return err(NO_SESSION)
        s, src = found
        if ctx.sender is not None and is_account(ctx.sender) and ctx.sender != s.user_id:
            return err("REFUSÉ : cette session de code appartient à un autre utilisateur")
        if SUBAGENT_MARK in ctx.thread_id:  # un sous-agent : le bac à sable de la session, son dossier à lui
            try:
                src.share_workspace(s.thread_id, ctx.thread_id, self.sessions.child_dir(ctx.thread_id))
            except ValueError as e:
                return err(f"REFUSÉ : {e}")
        try:
            return src.call(name, arguments, ctx)
        finally:
            self.sessions.touch(s.id)  # dernier usage : l'inactivité se compte depuis la fin de l'appel

    def for_thread(self, thread_id: str) -> Any:
        """La source de code d'une session pour `DelegateToolSource` (git dans son bac à sable, enfant sur son
        worktree), ou None hors session."""
        from nokker.code_sessions.subagents import SessionCode

        try:
            s = self.sessions.session_for_thread(thread_id)
        except NoTenant:
            return None
        return SessionCode(self.sessions, thread_id) if s is not None else None

    def close(self) -> None:
        self.sessions.close_all()


@dataclass
class CodeSessionGates:
    """Portes d'un noyau qui sert des sessions de code : sur le fil d'une session, les permissions de Lokkal
    (`PermissionGates` : mode, règles, hardline, cartes) devant les portes du noyau ; sur tout autre fil, les portes
    du noyau seules. Le drapeau de domaine devient consultatif dans une session (comme en Lokkal)."""

    inner: Any
    sessions: CodeSessions

    def __post_init__(self) -> None:
        if hasattr(self.inner, "code_session") and getattr(self.inner, "code_session", None) is None:
            self.inner.code_session = self.sessions.is_code_thread

    def action(self, call: Any, spec: Any, turn: Any) -> list[Any]:
        perms = self.sessions.permissions_for(getattr(turn, "thread_id", ""))
        if perms is None:
            return self.inner.action(call, spec, turn)
        from nokker.qa.permissions import PermissionGates

        return PermissionGates(inner=self.inner, permissions=perms,
                               code_session=self.sessions.is_code_thread).action(call, spec, turn)

    def switch(self, reply: str, turn: Any, tier: Any) -> list[Any]:
        return self.inner.switch(reply, turn, tier)

    def __getattr__(self, name: str) -> Any:  # deliverable, invariants… : ceux des portes du noyau
        if name in ("inner", "sessions"):
            raise AttributeError(name)
        return getattr(self.inner, name)


class _HookDispatch:
    """Le coureur de hooks vu par `HookedTools` : celui de la session du fil, ou rien."""

    def __init__(self, sessions: CodeSessions) -> None:
        self.sessions = sessions

    def pre_tool(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> str | None:
        r = self.sessions.runner_for(ctx.thread_id)
        return r.pre_tool(name, arguments, ctx) if r is not None else None

    def post_tool(self, name: str, arguments: dict[str, Any], ctx: ToolContext, result: ToolResult,
                  spec: Any = None) -> str:
        r = self.sessions.runner_for(ctx.thread_id)
        return r.post_tool(name, arguments, ctx, result, spec) if r is not None else ""

    def turn_end(self, turn: Any) -> None:
        r = self.sessions.runner_for(getattr(turn, "thread_id", ""))
        if r is not None:
            r.turn_end(turn)


def base_thread(thread_id: str) -> str:
    """Fil de la session d'un fil de sous-agent `<fil>/sa-<id>` (lui-même sinon)."""
    return thread_id.split(SUBAGENT_MARK, 1)[0]


__all__ = ["CodeError", "CodeSessionGates", "CodeSessions", "NO_SESSION", "TenantCodeTools", "base_thread",
           "repo_settings_state", "safe_repo_file"]
