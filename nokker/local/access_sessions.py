"""Sessions d'ACCÈS du web local de Lokkal : un code court échangé contre un jeton de session, à la place du jeton local
dans l'URL (`#token=`, qui finissait tôt ou tard dans un journal ou une erreur : fuite du 18.11.2026).

- `mint()` (jeton local seulement, jamais par la passerelle) : code de 128 bits en base64url, usage unique, 60 s,
  au plus 5 vivants. Seule son empreinte est gardée.
- `exchange(code, agent)` (SANS Authorization, sur la machine) : comparaison à temps constant avec chaque code vivant ;
  au plus 10 échanges ratés par minute (429) ; `code_used` / `code_expired` (410), jamais un jeton. Rend un jeton de
  SESSION distinct du jeton local : 256 bits, gardé HACHÉ (sha256) dans `<état>/access-sessions.json` (0600), valable
  30 jours GLISSANTS (chaque usage repousse l'échéance), révocable.
- Le jeton de session ouvre les routes du propriétaire, SAUF : émettre des codes, lister ou révoquer des sessions (jeton
  local seul, `nokker/local/api.py`) ; il est refusé par la passerelle comme le jeton local.

- PORTÉE (`scope`, portée du code émis, rendue à l'échange) : `owner` (défaut, ci-dessus) ; `kiosk` (écran HDMI du
  kiosque, docs/KIOSK.md : liste blanche GET de `KIOSK_ROUTES`, rien d'autre) ; `present` (grand écran de démo : droits
  du propriétaire SAUF la gestion, les décisions de cartes, les outils, l'export et l'effacement — `scope_allows` ;
  durée FIXE de 12 h, jamais repoussée). Un écran posé dans une pièce ne tranche jamais une carte.

Aucune table : Lokkal est une machine, et le jeton local vit déjà dans l'état (même dossier, mêmes droits)."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

CODE_TTL_S = 60
CODES_MAX = 5
SESSION_TTL_S = 30 * 24 * 3600
FAILS_MAX = 10
FAILS_WINDOW_S = 60.0
TOUCH_EVERY_S = 60.0          # la date de dernier usage est écrite au plus une fois par minute et par session
AGENT_MAX = 60
_CODE_FORM = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_ID_FORM = re.compile(r"^as_[0-9a-f]{16}$")
SCOPES = ("owner", "kiosk", "present")
PRESENT_TTL_S = 12 * 3600     # durée FIXE (non glissante) d'une session `present`
KIOSK_ROUTES = frozenset({"/v1/local/kiosk", "/v1/capabilities", "/healthz"})
# `present` : préfixes refusés quelle que soit la méthode (gestion, réglages, connecteurs, outils, effacement)
_PRESENT_DENY = ("/v1/local/session-code", "/v1/local/access-sessions", "/v1/local/setup", "/v1/local/accord",
                 "/v1/local/settings", "/v1/settings", "/v1/local/connectors", "/v1/connectors", "/v1/local/tools/call",
                 "/v1/local/erase", "/v1/account/erase", "/v1/erase", "/v1/local/byok", "/v1/local/vault")
# `present` : lecture seule (une carte se lit, ne se tranche pas)
_PRESENT_READ_ONLY = ("/v1/approvals", "/v1/local/approvals", "/v1/devices")


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def scope_allows(scope: str, method: str, path: str) -> bool:
    """Ce qu'un jeton de session de cette portée peut appeler (sur la machine ; la passerelle refuse tout jeton de
    session). `owner` : tout (les routes de gestion exigent de toute façon le jeton local)."""
    path = path.split("?", 1)[0].rstrip("/") or "/"
    method = method.upper()
    if scope == "owner":
        return True
    if scope == "kiosk":
        return method in ("GET", "HEAD") and path in KIOSK_ROUTES
    if scope == "present":
        if any(_under(path, p) for p in _PRESENT_DENY):
            return False
        if "/export" in path or path.endswith("/erase"):
            return False
        if method not in ("GET", "HEAD", "OPTIONS") and any(_under(path, p) for p in _PRESENT_READ_ONLY):
            return False
        return True
    return False


class AccessError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status, self.code = status, code


def _h(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class AccessSessions:
    def __init__(self, path: Path | str | None, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path is not None else None
        self.clock = clock
        self._lock = threading.Lock()
        self._codes: list[dict[str, Any]] = []      # {"h", "exp", "used"}
        self._fails: deque[float] = deque()
        self._sessions: dict[str, dict[str, Any]] = self._load()

    # ─── stockage ───
    def _load(self) -> dict[str, dict[str, Any]]:
        if self.path is None or not self.path.is_file():
            return {}
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8")).get("sessions") or []
        except (OSError, ValueError, AttributeError):
            return {}
        return {r["id"]: r for r in rows if isinstance(r, dict) and _ID_FORM.fullmatch(str(r.get("id", "")))
                and isinstance(r.get("h"), str)}

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "sessions": list(self._sessions.values())}, f, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    # ─── codes ───
    def _live_codes(self, now: float) -> list[dict[str, Any]]:
        self._codes = [c for c in self._codes if c["exp"] > now]
        return self._codes

    def mint(self, scope: Any = "owner") -> dict[str, Any]:
        if scope is None:
            scope = "owner"
        if scope not in SCOPES:
            raise AccessError(400, "bad_scope")
        now = self.clock()
        with self._lock:
            live = [c for c in self._live_codes(now) if not c["used"]]
            if len(live) >= CODES_MAX:
                raise AccessError(429, "too_many_codes")
            code = secrets.token_urlsafe(16)  # 128 bits
            self._codes.append({"h": _h(code), "exp": now + CODE_TTL_S, "used": False, "scope": scope})
        return {"code": code, "expires_in": CODE_TTL_S, "scope": scope}

    def exchange(self, code: Any, agent: str = "", expect: Any = None) -> dict[str, Any]:
        """`expect` (facultatif) : la portée qu'attend la page qui échange (`/kiosk` → `kiosk`). Portée du code différente →
        409 wrong_scope SANS consommer le code ni créer de session (sinon un code owner ouvert sur /kiosk laissait une
        session de 30 jours orpheline, contrôle réel du 18.11.2026) ; ce n'est pas un échec compté (le code est bon)."""
        now = self.clock()
        if not isinstance(code, str) or not _CODE_FORM.fullmatch(code):
            raise AccessError(400, "bad_code")
        if expect is not None and expect not in SCOPES:
            raise AccessError(400, "bad_scope")
        with self._lock:
            while self._fails and now - self._fails[0] >= FAILS_WINDOW_S:
                self._fails.popleft()
            if len(self._fails) >= FAILS_MAX:
                raise AccessError(429, "rate_limited")
            h = _h(code)
            hit = None
            for c in self._codes:  # tous comparés, à temps constant : la durée ne dit pas quel code est proche
                if hmac.compare_digest(c["h"], h):
                    hit = c
            if hit is None or hit["exp"] <= now:
                self._fails.append(now)
                self._live_codes(now)
                raise AccessError(410, "code_expired")
            if hit["used"]:
                self._fails.append(now)
                raise AccessError(410, "code_used")
            scope = hit.get("scope", "owner")
            if expect is not None and scope != expect:
                raise AccessError(409, "wrong_scope")  # code intact : il sert encore à la bonne page
            hit["used"] = True
            ttl = PRESENT_TTL_S if scope == "present" else SESSION_TTL_S
            token = secrets.token_urlsafe(32)  # 256 bits, distinct du jeton local
            sid = "as_" + secrets.token_hex(8)
            self._sessions[sid] = {"id": sid, "h": _h(token), "created_at": now, "last_used_at": now,
                                   "expires_at": now + ttl, "agent": _agent(agent), "scope": scope}
            self._save()
        return {"token": token, "expires_in": ttl, "scope": scope}

    # ─── sessions ───
    def verify(self, token: str | None) -> str | None:
        """Identifiant de la session de ce jeton s'il est valable (et l'échéance repoussée), sinon None."""
        found = self.lookup(token)
        return found[0] if found else None

    def lookup(self, token: str | None) -> tuple[str, str] | None:
        """(identifiant, portée) de la session de ce jeton s'il est valable, sinon None. Repousse l'échéance des
        portées glissantes (`owner`, `kiosk`) ; jamais celle d'une session `present` (12 h fixes)."""
        if not token or len(token) < 32 or len(token) > 128:
            return None
        now = self.clock()
        h = _h(token)
        with self._lock:
            found = None
            for s in self._sessions.values():
                if hmac.compare_digest(s["h"], h):
                    found = s
            if found is None:
                return None
            if found["expires_at"] <= now:
                del self._sessions[found["id"]]
                self._save()
                return None
            scope = found.get("scope", "owner")
            if now - found["last_used_at"] >= TOUCH_EVERY_S:
                found["last_used_at"] = now
                if scope != "present":
                    found["expires_at"] = now + SESSION_TTL_S
                self._save()
            return found["id"], scope

    def list(self) -> list[dict[str, Any]]:
        now = self.clock()
        with self._lock:
            gone = [k for k, s in self._sessions.items() if s["expires_at"] <= now]
            for k in gone:
                del self._sessions[k]
            if gone:
                self._save()
            return [{**{k: s[k] for k in ("id", "created_at", "last_used_at", "expires_at", "agent")},
                     "scope": s.get("scope", "owner")}
                    for s in sorted(self._sessions.values(), key=lambda s: s["created_at"])]

    def revoke_scope(self, scope: str) -> int:
        """Révoque toutes les sessions d'une portée (le lanceur du kiosque, à l'arrêt de sa unit)."""
        if scope not in SCOPES:
            raise AccessError(400, "bad_scope")
        with self._lock:
            gone = [k for k, s in self._sessions.items() if s.get("scope", "owner") == scope]
            for k in gone:
                del self._sessions[k]
            if gone:
                self._save()
            return len(gone)

    def revoke(self, sid: str) -> bool:
        with self._lock:
            if sid not in self._sessions:
                return False
            del self._sessions[sid]
            self._save()
            return True


def _agent(ua: str | None) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", "", str(ua or "")).strip()
    return text[:AGENT_MAX]
