"""Vue « glance » des lunettes (docs/AMBIENT.md §6) : un jeton d'appareil de portée glance, en LECTURE SEULE.

- Même mécanisme que les écrans (`ambient_displays`, `kind = 'glance'`) : la base ne garde que l'empreinte du jeton
  (`nkg_…`), jamais le jeton. Le titulaire (le compte dont la vue lit les données) révoque ; un ADULTE de son foyer
  aussi (route des écrans du foyer).
- Appairage par code court ou QR : le téléphone connecté crée une OFFRE (`offer`) → code de 8 caractères (alphabet sans
  0/O/1/I/L/U), valable 5 minutes, à usage unique ; les lunettes le saisissent ou scannent le QR et le PRÉSENTENT
  (`claim`) → le jeton est émis une seule fois. Le code n'est gardé que par son empreinte.
- Portée tenue par le noyau : un jeton glance n'ouvre QUE `GET /v1/glance` (brief, dernier passage de l'agent Brief,
  cartes en attente, incidents en cours). Toute autre route, toute écriture, toute décision de carte : 403
  (`api/server.py::_glance_gate`).
- Échecs d'appairage bornés (20 par minute et par processus, puis 429) : 8 caractères sur 30 symboles et 5 minutes de
  vie rendent l'essai au hasard vain, la borne coupe court à une rafale.
"""
from __future__ import annotations

import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from nokker.ambient.displays import Display, DisplayError, sha

GLANCE_PREFIX = "nkg_"
_GTOKEN = re.compile(r"^nkg_[A-Za-z0-9_-]{32,64}$")
_DISPLAY_ID = re.compile(r"^dp_[0-9a-f]{16}$")
ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"  # sans 0/O, 1/I/L, U
CODE_LEN = 8
OFFER_TTL_S = 300
MAX_GLANCE = 5  # vues actives ou en attente, par compte
MAX_FAILS_PER_MIN = 20
READS_PER_MIN = 2  # GET /v1/glance : par jeton (clé = id de l'appareil, jamais le jeton)
QR_PREFIX = "NOKKER-GLANCE:"


def is_glance_token(token: str) -> bool:
    return bool(token) and token.startswith(GLANCE_PREFIX)


def _norm(code: str) -> str:
    c = re.sub(r"[\s-]", "", (code or "").upper())
    if c.startswith(QR_PREFIX.replace("-", "").rstrip(":")):
        c = c.split(":", 1)[-1]
    return c


def code_sha(code: str) -> str:
    return sha("glance:" + _norm(code))


@dataclass
class Glance:
    store: Any  # DisplayStore (le même que les écrans)
    clock: Callable[[], float] = time.time
    _fails: deque = field(default_factory=deque, repr=False)
    _reads: dict[str, deque] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def offer(self, account_id: str, tenant_id: str, household_id: str = "", name: str = "",
              surface: str = "glasses") -> dict[str, Any]:
        """Offre d'appairage créée par le téléphone CONNECTÉ du titulaire. `surface` : glasses (défaut) | wall (écran
        mural, P10b) ; fixée ici, un mur naît avec `show_brief` faux."""
        if surface not in SURFACES:
            raise DisplayError(400, "bad_surface", "surface : glasses ou wall")
        live = [d for d in self.store.of_account(account_id, "glance") if d.revoked_at is None
                and (d.token_sha256 is not None or (d.setup_until or 0) > self.clock())]
        if len(live) >= MAX_GLANCE:
            raise DisplayError(409, "too_many_glance", f"{MAX_GLANCE} vues glance par compte au plus")
        code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
        now = self.clock()
        default = "Écran mural" if surface == "wall" else "Lunettes"
        d = Display(id="dp_" + secrets.token_hex(8), tenant_id=tenant_id, household_id=household_id,
                    account_id=account_id, name=" ".join(str(name or default).split())[:40] or default,
                    model="glance", width=0, height=0, depth=1, refresh_s=60, token_sha256=None, created_at=now,
                    pair_sha256=code_sha(code), setup_until=now + OFFER_TTL_S, kind="glance", surface=surface,
                    show_brief=surface != "wall")
        self.store.add(d)
        shown = f"{code[:4]}-{code[4:]}"
        return {"offer_id": d.id, "code": shown, "qr": QR_PREFIX + code, "expires_in": OFFER_TTL_S}

    def claim(self, code: str) -> dict[str, Any]:
        """Les lunettes présentent le code : jeton émis UNE fois. Code inconnu, expiré ou déjà pris : 404."""
        with self._lock:
            now = self.clock()
            while self._fails and self._fails[0] < now - 60:
                self._fails.popleft()
            if len(self._fails) >= MAX_FAILS_PER_MIN:
                raise DisplayError(429, "too_many_attempts", "trop d'essais : réessayez dans une minute")
        c = _norm(code)
        d = self.store.by_pairing_pending(code_sha(c), self.clock(), "glance") \
            if len(c) == CODE_LEN and all(ch in ALPHABET for ch in c) else None
        token = GLANCE_PREFIX + secrets.token_urlsafe(32)
        if d is None or not self.store.set_token(d.id, sha(token)):
            with self._lock:
                self._fails.append(self.clock())
            raise DisplayError(404, "unknown_code", "code inconnu ou expiré")
        return {"token": token, "device": {"id": d.id, "name": d.name}, "scope": "glance"}

    def resolve(self, token: str) -> Display | None:
        if not token or not _GTOKEN.match(token):
            return None
        d = self.store.by_token(sha(token))
        if d is None or d.kind != "glance" or not d.active:
            return None
        self.store.touch(d.id, self.clock())
        return d

    def read_allowed(self, device_id: str) -> int:
        """0 si la lecture passe (et elle est comptée) ; sinon le nombre de secondes à attendre (Retry-After).
        Fenêtre glissante de 60 s, `READS_PER_MIN` lectures par appareil, en mémoire du processus."""
        with self._lock:
            now = self.clock()
            q = self._reads.setdefault(device_id, deque())
            while q and q[0] <= now - 60:
                q.popleft()
            if len(q) >= READS_PER_MIN:
                return max(1, int(q[0] + 60 - now + 0.999))
            q.append(now)
            if len(self._reads) > 10_000:  # borne mémoire : on oublie les appareils muets
                for k in [k for k, v in self._reads.items() if not v or v[-1] <= now - 60]:
                    self._reads.pop(k, None)
            return 0

    def devices(self, account_id: str) -> dict[str, Any]:
        return {"devices": [d.to_json() for d in self.store.of_account(account_id, "glance")]}

    def set_show_brief(self, account_id: str, device_id: str, body: Any) -> dict[str, Any]:
        """`PATCH /v1/glance/devices/{id} {show_brief}` : session du TITULAIRE (jamais le jeton nkg_, refusé avant)."""
        if not isinstance(body, dict) or set(body) != {"show_brief"} or not isinstance(body["show_brief"], bool):
            raise DisplayError(400, "bad_request", "corps attendu : {show_brief: true | false}")
        mine = next((d for d in self.store.of_account(account_id, "glance") if d.id == device_id), None) \
            if _DISPLAY_ID.match(device_id or "") else None
        if mine is None or mine.revoked_at is not None:
            raise DisplayError(404, "not_found", "appareil inconnu")
        if mine.surface != "wall":
            raise DisplayError(400, "not_wall", "show_brief : écran mural seulement (des lunettes montrent tout)")
        d = self.store.set_show_brief(account_id, device_id, body["show_brief"])
        if d is None:
            raise DisplayError(404, "not_found", "appareil inconnu")
        return d.to_json()

    def revoke(self, account_id: str, device_id: str) -> dict[str, Any]:
        if not _DISPLAY_ID.match(device_id or "") or not self.store.revoke_owned(account_id, device_id, self.clock()):
            raise DisplayError(404, "not_found", "appareil inconnu")
        return {"revoked": True}


SURFACES = ("glasses", "wall")
NEXT_EVENT_TITLE_MAX = 120


def next_event(sources: list[Any], account_id: str, tenant_id: str, now: datetime) -> dict[str, Any] | None:
    """Le premier événement des 24 prochaines heures (en cours compris) des agendas CONNECTÉS du titulaire, dans son
    locataire : `{start, all_day, title}`. Jamais l'agenda du téléphone (une lecture glance ne réveille pas l'app).
    None sans agenda ou sans événement ; une panne lève (le champ part dans `unavailable`)."""
    from nokker.connectors.calendar import parse_events_result
    from nokker.core.ports import ToolContext
    from nokker.devices.calendar import DeviceCalendarSource

    ctx = ToolContext(thread_id=f"{tenant_id}:glance", turn_index=0, channel="glance", sender=account_id)
    best: tuple[datetime, dict[str, Any]] | None = None
    tried = failed = 0
    for src in sources:
        if isinstance(src, DeviceCalendarSource) or not any(x.name == "calendar_events" for x in src.specs()):
            continue
        tried += 1
        r = src.call("calendar_events", {"days": 1}, ctx)
        if getattr(r, "is_error", False):
            failed += 1
            continue
        for e in parse_events_result(r.content).get("events") or []:
            start = _start_of(e, now)
            if start is None or start > now + timedelta(hours=24):
                continue
            if best is None or start < best[0]:
                best = (start, e)
    if tried and failed == tried:
        raise RuntimeError("agendas illisibles")
    if best is None:
        return None
    e = best[1]
    return {"start": str(e.get("start") or ""), "all_day": bool(e.get("all_day")),
            "title": " ".join(str(e.get("summary") or "").split())[:NEXT_EVENT_TITLE_MAX]}


def _start_of(e: dict[str, Any], now: datetime) -> datetime | None:
    raw = str(e.get("start") or "")
    try:
        if len(raw) == 10:  # journée entière : minuit, heure de la machine du titulaire inconnue ici → fuseau de `now`
            d = datetime.fromisoformat(raw).replace(tzinfo=now.tzinfo)
        else:
            d = datetime.fromisoformat(raw)
            if d.tzinfo is None:
                d = d.replace(tzinfo=now.tzinfo)
    except ValueError:
        return None
    return d


def wall_view(view: dict[str, Any], device: Any) -> dict[str, Any]:
    """Vue glance filtrée PAR LE NOYAU (P10b) : `approvals_count` partout ; sur un mur, jamais le contenu d'une carte,
    incidents réduits à {id, title}, brief et titre du prochain événement seulement si `show_brief`."""
    out = dict(view)
    out["approvals_count"] = len(view.get("approvals") or [])
    out["surface"], out["show_brief"] = device.surface, bool(device.show_brief)
    if device.surface != "wall":
        return out
    out["approvals"] = []
    out["incidents"] = [{"id": i.get("id"), "title": i.get("title")} for i in view.get("incidents") or []
                        if isinstance(i, dict)]
    if not device.show_brief:
        out["brief"], out["brief_last_run"] = None, None
        out["unavailable"] = [*[u for u in view.get("unavailable") or [] if u not in ("brief", "brief_last_run")],
                              "brief"]
        if isinstance(out.get("next_event"), dict):
            out["next_event"] = {k: v for k, v in out["next_event"].items() if k != "title"}
    return out


__all__ = ["BRIEF_SPEECH_PATH", "BRIEF_SPEECH_PER_HOUR", "GLANCE_PREFIX", "SURFACES", "BriefSpeech", "Glance",
           "OFFER_TTL_S", "QR_PREFIX", "code_sha", "is_glance_token", "next_event", "wall_view"]


# ─────────────────── annonce du matin lue par l'avatar du mur (V6, docs/AVATAR.md §2) ───────────────────

BRIEF_SPEECH_PATH = "/v1/glance/brief/speech"
BRIEF_SPEECH_PER_HOUR = 6
BRIEF_SPEECH_CACHE = 200


@dataclass
class BriefSpeech:
    """`GET /v1/glance/brief/speech` (jeton nkg_ d'un MUR) : le résumé du dernier passage de l'agent Brief — le texte
    que le mur affiche déjà — dit par la voix du titulaire, horodaté pour l'avatar. Une synthèse par brief et par mur
    (cache borné, en mémoire du processus), au plus `BRIEF_SPEECH_PER_HOUR` lectures par heure et par mur."""

    clock: Callable[[], float] = time.time
    _hits: dict[str, deque] = field(default_factory=dict, repr=False)
    _cache: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @staticmethod
    def check(device: Any) -> tuple[int, str] | None:
        """(statut, code) si l'appareil ne peut pas lire le brief à voix haute, sinon None."""
        if getattr(device, "surface", "glasses") != "wall":
            return 403, "not_wall"
        if not getattr(device, "show_brief", False):
            return 403, "brief_hidden"
        return None

    def rate(self, device_id: str) -> int:
        """0 si la lecture passe (comptée), sinon le nombre de secondes à attendre."""
        with self._lock:
            now = self.clock()
            q = self._hits.setdefault(device_id, deque())
            while q and q[0] <= now - 3600:
                q.popleft()
            if len(q) >= BRIEF_SPEECH_PER_HOUR:
                return max(1, int(q[0] + 3600 - now) + 1)
            q.append(now)
            return 0

    def cached(self, device_id: str, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._cache.get((device_id, run_id))

    def remember(self, device_id: str, run_id: str, speech: dict[str, Any]) -> None:
        with self._lock:
            for k in [k for k in self._cache if k[0] == device_id]:  # un seul brief gardé par mur
                self._cache.pop(k, None)
            if len(self._cache) >= BRIEF_SPEECH_CACHE:
                self._cache.pop(next(iter(self._cache)), None)
            self._cache[(device_id, run_id)] = speech
