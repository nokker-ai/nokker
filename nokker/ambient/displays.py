"""Écrans e-ink du foyer (docs/AMBIENT.md §3) : enregistrement, jeton par appareil, révocation, tableau commun, image.

- Un écran appartient à un FOYER (projet `household`, locataire du payeur). Seul un membre ADULTE du foyer l'enregistre
  ou le révoque ; tout membre lit la liste. Le jeton (`nkd_…`) n'est montré qu'une fois ; le noyau n'en garde que
  l'empreinte SHA-256. Révoqué = refusé tout de suite (401), ici et, en Lokkal, sur la passerelle du foyer.
- Le tableau (`Board`) est COMMUN au foyer : il est écrit par les membres (app, API), jamais tiré des notes, de la
  mémoire de profil ni des fils d'un membre. L'image ne lit que lui.
- Appairage TRMNL (`/api/setup`, en-tête `ID` = adresse MAC) : l'écran a été déclaré avec sa MAC dans l'app ; dans
  les 15 minutes, le premier `/api/setup` reçoit un jeton NEUF (l'empreinte remplace l'ancienne), une seule fois. Le
  noyau ne garde jamais de jeton en clair.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from nokker.ambient.frame import Board, FrameError, FrameRenderer, screen_size
from nokker.tenancy.context import tenant_scope

TOKEN_PREFIX = "nkd_"
_TOKEN = re.compile(r"^nkd_[A-Za-z0-9_-]{32,64}$")
_DISPLAY_ID = re.compile(r"^dp_[0-9a-f]{16}$")
_MAC = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
SETUP_WINDOW_S = 15 * 60
DEFAULT_REFRESH_S = 900
MIN_REFRESH_S, MAX_REFRESH_S = 300, 86400
MAX_DISPLAYS = 10  # par foyer
MAX_NAME = 40


class DisplayError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def mac_sha(mac: str) -> str:
    return sha("mac:" + mac.strip().upper())


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


@dataclass
class Display:
    id: str
    tenant_id: str
    household_id: str
    account_id: str
    name: str
    model: str
    width: int
    height: int
    depth: int
    refresh_s: int
    token_sha256: str | None
    created_at: float
    pair_sha256: str | None = None
    setup_until: float | None = None
    revoked_at: float | None = None
    last_seen_at: float | None = None
    kind: str = "eink"  # eink | glance

    @property
    def active(self) -> bool:
        return self.revoked_at is None and self.token_sha256 is not None

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "model": self.model, "width": self.width, "height": self.height,
                "depth": self.depth, "refresh_s": self.refresh_s, "created_at": self.created_at,
                "revoked": self.revoked_at is not None, "paired": self.token_sha256 is not None,
                "trmnl": self.kind == "eink" and self.pair_sha256 is not None, "last_seen_at": self.last_seen_at,
                "kind": self.kind, "account_id": self.account_id}


class DisplayStore(Protocol):
    def add(self, d: Display) -> None: ...

    def by_token(self, token_sha256: str) -> Display | None: ...

    def by_pairing_pending(self, pair_sha256: str, now: float, kind: str = "eink") -> Display | None: ...

    def set_token(self, display_id: str, token_sha256: str) -> bool: ...

    def of_household(self, tenant_id: str, household_id: str) -> list[Display]: ...

    def revoke(self, tenant_id: str, household_id: str, display_id: str, now: float) -> bool: ...

    def of_account(self, account_id: str, kind: str) -> list[Display]: ...

    def revoke_owned(self, account_id: str, display_id: str, now: float) -> bool: ...

    def touch(self, display_id: str, now: float) -> None: ...

    def active(self) -> list[Display]: ...

    def board(self, household_id: str) -> Board: ...  # dans le tenant_scope du foyer

    def put_board(self, household_id: str, board: Board, by: str) -> Board: ...


@dataclass
class MemoryDisplayStore:
    """Dev et tests. Le tableau est rangé par (locataire COURANT, foyer) : même cloisonnement que la RLS."""

    rows: dict[str, Display] = field(default_factory=dict)
    boards: dict[tuple[str, str], Board] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, d: Display) -> None:
        with self._lock:
            self.rows[d.id] = copy.copy(d)

    def by_token(self, token_sha256: str) -> Display | None:
        with self._lock:
            for d in self.rows.values():
                if d.token_sha256 and hmac.compare_digest(d.token_sha256, token_sha256):
                    return copy.copy(d)
        return None

    def by_pairing_pending(self, pair_sha256: str, now: float, kind: str = "eink") -> Display | None:
        with self._lock:
            for d in self.rows.values():
                if d.kind == kind and d.pair_sha256 == pair_sha256 and d.revoked_at is None \
                        and d.token_sha256 is None and (d.setup_until or 0) > now:
                    return copy.copy(d)
        return None

    def set_token(self, display_id: str, token_sha256: str) -> bool:
        with self._lock:
            d = self.rows.get(display_id)
            if d is None or d.revoked_at is not None:
                return False
            d.token_sha256, d.setup_until = token_sha256, None
            return True

    def of_account(self, account_id: str, kind: str) -> list[Display]:
        with self._lock:
            return sorted((copy.copy(d) for d in self.rows.values() if d.account_id == account_id and d.kind == kind),
                          key=lambda d: d.created_at)

    def revoke_owned(self, account_id: str, display_id: str, now: float) -> bool:
        with self._lock:
            d = self.rows.get(display_id)
            if d is None or d.account_id != account_id or d.revoked_at is not None:
                return False
            d.revoked_at, d.token_sha256, d.setup_until = now, None, None
            return True

    def of_household(self, tenant_id: str, household_id: str) -> list[Display]:
        with self._lock:
            return sorted((copy.copy(d) for d in self.rows.values()
                           if d.tenant_id == tenant_id and d.household_id == household_id), key=lambda d: d.created_at)

    def revoke(self, tenant_id: str, household_id: str, display_id: str, now: float) -> bool:
        with self._lock:
            d = self.rows.get(display_id)
            if d is None or d.tenant_id != tenant_id or d.household_id != household_id or d.revoked_at is not None:
                return False
            d.revoked_at, d.token_sha256, d.setup_until = now, None, None
            return True

    def touch(self, display_id: str, now: float) -> None:
        with self._lock:
            if display_id in self.rows:
                self.rows[display_id].last_seen_at = now

    def active(self) -> list[Display]:
        with self._lock:
            return [copy.copy(d) for d in self.rows.values() if d.active and d.kind == "eink"]

    def board(self, household_id: str) -> Board:
        from nokker.tenancy.context import current_tenant

        with self._lock:
            b = self.boards.get((current_tenant(), household_id))
            return copy.deepcopy(b) if b is not None else Board()

    def put_board(self, household_id: str, board: Board, by: str) -> Board:
        from nokker.tenancy.context import current_tenant

        key = (current_tenant(), household_id)
        with self._lock:
            prev = self.boards.get(key)
            board.version = (prev.version if prev else 0) + 1
            self.boards[key] = copy.deepcopy(board)
            return copy.deepcopy(board)


_COLS = ("id", "tenant_id", "household_id", "account_id", "name", "model", "width", "height", "depth", "refresh_s",
         "token_sha256", "created_at", "pair_sha256", "setup_until", "revoked_at", "last_seen_at", "kind")


class PgDisplayStore:
    """Tables `ambient_displays` et `ambient_boards` (RLS du locataire du foyer), migration 054. `ambient_displays` est
    sous RLS depuis 058 : les lectures par jeton passent AVANT de connaître le locataire, par une connexion sans
    locataire qui n'ouvre que la ligne de cette clé (`connect_key`) ; les écritures, dans le locataire de l'écran."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _key(self, key: str):
        """`ambient_displays` sous RLS (058) : avant le locataire, la seule ligne qui porte cette clé (empreinte du
        jeton, de l'appairage, identifiant d'écran, compte d'une vue glance)."""
        from nokker.store.conn import connect_key

        return connect_key(self.dsn, key)

    def _for(self, tenant_id: str):
        from nokker.store.conn import connect_tenant

        return connect_tenant(self.dsn, tenant_id)

    def _tenant(self):
        from nokker.store.conn import connect

        return connect(self.dsn)

    @staticmethod
    def _row(r: tuple | None) -> Display | None:
        return Display(**dict(zip(_COLS, r))) if r else None

    def add(self, d: Display) -> None:
        vals = asdict(d)
        with self._for(d.tenant_id) as conn:
            conn.execute(f"INSERT INTO ambient_displays ({', '.join(_COLS)}) VALUES ({', '.join(['%s'] * len(_COLS))})",
                         tuple(vals[c] for c in _COLS))

    def by_token(self, token_sha256: str) -> Display | None:
        with self._key(token_sha256) as conn:
            return self._row(conn.execute(f"SELECT {', '.join(_COLS)} FROM ambient_displays WHERE token_sha256=%s "
                                          "AND revoked_at IS NULL", (token_sha256,)).fetchone())

    def by_pairing_pending(self, pair_sha256: str, now: float, kind: str = "eink") -> Display | None:
        with self._key(pair_sha256) as conn:
            return self._row(conn.execute(f"SELECT {', '.join(_COLS)} FROM ambient_displays WHERE pair_sha256=%s "
                                          "AND kind=%s AND revoked_at IS NULL AND token_sha256 IS NULL "
                                          "AND setup_until > %s ORDER BY created_at DESC LIMIT 1",
                                          (pair_sha256, kind, now)).fetchone())

    def set_token(self, display_id: str, token_sha256: str) -> bool:
        with self._key(display_id) as conn:
            cur = conn.execute("UPDATE ambient_displays SET token_sha256=%s, setup_until=NULL WHERE id=%s "
                               "AND revoked_at IS NULL", (token_sha256, display_id))
            return cur.rowcount == 1

    def of_account(self, account_id: str, kind: str) -> list[Display]:
        with self._key(account_id) as conn:
            rows = conn.execute(f"SELECT {', '.join(_COLS)} FROM ambient_displays WHERE account_id=%s AND kind=%s "
                                "ORDER BY created_at", (account_id, kind)).fetchall()
        return [d for d in (self._row(r) for r in rows) if d is not None]

    def revoke_owned(self, account_id: str, display_id: str, now: float) -> bool:
        with self._key(account_id) as conn:
            cur = conn.execute("UPDATE ambient_displays SET revoked_at=%s, token_sha256=NULL, setup_until=NULL "
                               "WHERE id=%s AND account_id=%s AND revoked_at IS NULL", (now, display_id, account_id))
            return cur.rowcount == 1

    def of_household(self, tenant_id: str, household_id: str) -> list[Display]:
        with self._for(tenant_id) as conn:
            rows = conn.execute(f"SELECT {', '.join(_COLS)} FROM ambient_displays WHERE tenant_id=%s AND household_id=%s "
                                "ORDER BY created_at", (tenant_id, household_id)).fetchall()
        return [d for d in (self._row(r) for r in rows) if d is not None]

    def revoke(self, tenant_id: str, household_id: str, display_id: str, now: float) -> bool:
        with self._for(tenant_id) as conn:
            cur = conn.execute("UPDATE ambient_displays SET revoked_at=%s, token_sha256=NULL, setup_until=NULL "
                               "WHERE id=%s AND tenant_id=%s AND household_id=%s AND revoked_at IS NULL",
                               (now, display_id, tenant_id, household_id))
            return cur.rowcount == 1

    def touch(self, display_id: str, now: float) -> None:
        with self._key(display_id) as conn:
            conn.execute("UPDATE ambient_displays SET last_seen_at=%s WHERE id=%s", (now, display_id))

    def active(self) -> list[Display]:
        """Écrans e-ink en service de tous les foyers (publication de fond) : locataire par locataire."""
        from nokker.store.conn import known_tenants

        rows: list[tuple] = []
        for tid in known_tenants(self.dsn):
            with self._for(tid) as conn:
                rows += conn.execute(f"SELECT {', '.join(_COLS)} FROM ambient_displays WHERE tenant_id=%s "
                                     "AND revoked_at IS NULL AND token_sha256 IS NOT NULL AND kind='eink'",
                                     (tid,)).fetchall()
        return [d for d in (self._row(r) for r in rows) if d is not None]

    def board(self, household_id: str) -> Board:
        with self._tenant() as conn:
            r = conn.execute("SELECT content, version, updated_at FROM ambient_boards WHERE household_id=%s",
                             (household_id,)).fetchone()
        if not r:
            return Board()
        content = r[0] if isinstance(r[0], dict) else json.loads(r[0] or "{}")
        return Board.clean(content, version=int(r[1]), updated_at=float(r[2]))

    def put_board(self, household_id: str, board: Board, by: str) -> Board:
        content = {k: v for k, v in board.to_json().items() if k not in ("version", "updated_at")}
        with self._tenant() as conn:
            r = conn.execute("INSERT INTO ambient_boards (household_id, content, version, updated_at, updated_by) "
                             "VALUES (%s, %s::jsonb, 1, %s, %s) ON CONFLICT (tenant_id, household_id) DO UPDATE SET "
                             "content=EXCLUDED.content, version=ambient_boards.version + 1, "
                             "updated_at=EXCLUDED.updated_at, updated_by=EXCLUDED.updated_by RETURNING version",
                             (household_id, json.dumps(content, ensure_ascii=False), board.updated_at, by)).fetchone()
        board.version = int(r[0])
        return board


@dataclass
class Membership:
    """Ce que l'API sait du membre qui agit : son compte, son foyer (id, locataire du foyer), son profil."""

    account_id: str
    household_id: str
    household_tenant: str
    profile: str  # adult | teen | child
    household_name: str = ""


@dataclass
class Frame:
    png: bytes
    filename: str
    refresh_s: int
    display: Display


@dataclass
class Displays:
    """Service des écrans. `on_publish` / `on_revoke` : accroches de la passerelle du foyer (Lokkal), facultatives."""

    store: Any
    renderer: FrameRenderer = field(default_factory=FrameRenderer)
    clock: Callable[[], float] = time.time
    now_local: Callable[[], dt.datetime] = lambda: dt.datetime.now().astimezone()
    household_name: Callable[[str, str], str] = lambda tenant, household: ""
    on_revoke: Callable[[Display], None] | None = None
    on_board: Callable[[str, str], None] | None = None  # (locataire, foyer) : le tableau a changé
    # Lokkal : appairage TRMNL servi par la passerelle du foyer (`eink.setup_offer` : empreinte de la MAC, jeton, durée).
    setup_offer: Callable[[str, str, int], None] | None = None
    _cache: dict[tuple[Any, ...], bytes] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ── gestion (membre du foyer) ──
    def register(self, who: Membership, body: Any) -> dict[str, Any]:
        if who.profile != "adult":
            raise DisplayError(403, "adult_required", "seul un membre adulte du foyer enregistre un écran")
        if not isinstance(body, dict):
            raise DisplayError(400, "bad_display", "écran : un objet {name, model, …}")
        try:
            w, h, d = screen_size(str(body.get("model") or "trmnl"), body.get("width"), body.get("height"),
                                  body.get("depth"))
        except FrameError as e:
            raise DisplayError(400, "bad_display", str(e)) from None
        name = " ".join(str(body.get("name") or "Écran").split())[:MAX_NAME] or "Écran"
        try:
            refresh = int(body.get("refresh_s") or DEFAULT_REFRESH_S)
        except (TypeError, ValueError):
            raise DisplayError(400, "bad_display", "refresh_s : un entier") from None
        refresh = max(MIN_REFRESH_S, min(MAX_REFRESH_S, refresh))
        mac = str(body.get("mac") or "").strip()
        if mac and not _MAC.match(mac):
            raise DisplayError(400, "bad_display", "mac : six octets AA:BB:CC:DD:EE:FF")
        live = [x for x in self.store.of_household(who.household_tenant, who.household_id)
                if x.revoked_at is None and x.kind == "eink"]
        if len(live) >= MAX_DISPLAYS:
            raise DisplayError(409, "too_many_displays", f"{MAX_DISPLAYS} écrans par foyer au plus")
        now = self.clock()
        offer = bool(mac) and self.setup_offer is not None
        token = new_token() if (not mac or offer) else None
        disp = Display(id="dp_" + secrets.token_hex(8), tenant_id=who.household_tenant, household_id=who.household_id,
                       account_id=who.account_id, name=name, model=str(body.get("model") or "trmnl"), width=w,
                       height=h, depth=d, refresh_s=refresh, token_sha256=sha(token) if token else None,
                       created_at=now, pair_sha256=mac_sha(mac) if mac else None,
                       setup_until=now + SETUP_WINDOW_S if mac else None)
        self.store.add(disp)
        out: dict[str, Any] = {"display": disp.to_json()}
        if mac:  # TRMNL : le jeton va à l'appareil seul (/api/setup), jamais à l'app
            out["setup_window_s"] = SETUP_WINDOW_S
            if offer and token:
                try:
                    self.setup_offer(disp.pair_sha256 or "", token, SETUP_WINDOW_S)
                except Exception:  # noqa: BLE001 — l'appairage passera par l'adresse publique du noyau
                    pass
        else:
            out["token"] = token  # montré UNE fois
        return out

    def list(self, who: Membership) -> dict[str, Any]:
        return {"displays": [d.to_json() for d in self.store.of_household(who.household_tenant, who.household_id)]}

    def revoke(self, who: Membership, display_id: str) -> dict[str, Any]:
        if who.profile != "adult":
            raise DisplayError(403, "adult_required", "seul un membre adulte du foyer révoque un écran")
        if not _DISPLAY_ID.match(display_id or ""):
            raise DisplayError(404, "not_found", "écran inconnu")
        mine = {d.id: d for d in self.store.of_household(who.household_tenant, who.household_id)}
        if display_id not in mine or not self.store.revoke(who.household_tenant, who.household_id, display_id,
                                                           self.clock()):
            raise DisplayError(404, "not_found", "écran inconnu")
        with self._lock:
            for k in [k for k in self._cache if k[0] == display_id]:
                self._cache.pop(k, None)
        if self.on_revoke is not None:
            try:
                self.on_revoke(mine[display_id])
            except Exception:  # noqa: BLE001 — la révocation est faite en base ; la passerelle expire seule (TTL)
                pass
        return {"revoked": True}

    def board(self, who: Membership) -> dict[str, Any]:
        with tenant_scope(who.household_tenant):
            return {"board": self.store.board(who.household_id).to_json()}

    def put_board(self, who: Membership, body: Any) -> dict[str, Any]:
        if who.profile == "child":
            raise DisplayError(403, "minor_refused", "un profil enfant ne modifie pas le tableau du foyer")
        try:
            board = Board.clean(body, updated_at=self.clock())
        except FrameError as e:
            raise DisplayError(400, "bad_board", str(e)) from None
        with tenant_scope(who.household_tenant):
            saved = self.store.put_board(who.household_id, board, who.account_id)
        if self.on_board is not None:
            try:
                self.on_board(who.household_tenant, who.household_id)
            except Exception:  # noqa: BLE001
                pass
        return {"board": saved.to_json()}

    # ── appareils (sans session : le jeton fait foi) ──
    def resolve(self, token: str) -> Display | None:
        if not token or not _TOKEN.match(token):
            return None
        d = self.store.by_token(sha(token))
        return d if d is not None and d.active and d.kind == "eink" else None

    def frame(self, d: Display, *, seen: bool = True) -> Frame:
        """Image du foyer de `d`, lue dans le locataire du foyer ; mise en cache par (écran, version, jour, taille)."""
        with tenant_scope(d.tenant_id):
            board = self.store.board(d.household_id)
        now = self.now_local()
        key = (d.id, board.version, now.date().isoformat(), d.width, d.height, d.depth)
        with self._lock:
            png = self._cache.get(key)
        if png is None:
            png = self.renderer.render(board, d.width, d.height, d.depth, now=now,
                                       household_name=self.household_name(d.tenant_id, d.household_id))
            with self._lock:
                for k in [k for k in self._cache if k[0] == d.id]:
                    self._cache.pop(k, None)
                self._cache[key] = png
        if seen:  # l'appareil lui-même (pas la publication vers la passerelle)
            self.store.touch(d.id, self.clock())
        filename = f"{d.id}-{board.version}-{now.strftime('%Y%m%d')}"
        return Frame(png=png, filename=filename, refresh_s=d.refresh_s, display=d)

    def trmnl_setup(self, mac: str) -> str | None:
        """Jeton NEUF pour un écran TRMNL déclaré avec cette MAC, dans sa fenêtre d'appairage ; sinon None."""
        if not _MAC.match(mac or ""):
            return None
        d = self.store.by_pairing_pending(mac_sha(mac), self.clock())
        if d is None:
            return None
        token = new_token()
        return token if self.store.set_token(d.id, sha(token)) else None


__all__ = ["Display", "DisplayError", "Displays", "Frame", "Membership", "MemoryDisplayStore", "PgDisplayStore",
           "mac_sha", "new_token", "sha"]
