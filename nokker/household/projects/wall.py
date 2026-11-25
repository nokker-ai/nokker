"""Tableau de bord familial du mur (lot F1, côté noyau ; docs/FAMILY-PROJECTS.md §10 « Mur »).

Un écran mural est une vue glance (`nkg_…`, surface `wall`, docs/AMBIENT.md §6.3). Ce module sert, pour ce jeton
SEULEMENT (`api/server.py::_glance_gate`) :

    GET  /v1/glance/household                 → vue du foyer (ETag, 304) ou `null` sans foyer
    POST /v1/glance/guest {on}                → mode invité du foyer (4 h au plus)
    POST /v1/glance/tasks/{ht}/done {done?}   → coche d'une tâche du jour (projets du foyer)
    POST /v1/glance/shopping/{sh}/done {done?}  → coche d'un article de courses

Règles tenues ici, jamais par le front :
- members : prénoms seulement (jamais une adresse ; sans prénom saisi : « Membre n ») ;
- agenda : 7 jours, SEULEMENT les agendas partagés avec le foyer (source injectée `calendars`), pas de titre pour un
  événement privé ni en mode invité ;
- tasks : celles du jour, hors projets réservés aux parents ; projects : avancement des projets du foyer (pas parents) ;
- weather / departures : pack Essentiels s'il est branché (`weather`, `departures`), sinon null / [] ;
- meals : [] (aucune source en v1) ; shopping : la liste de courses du tableau commun du foyer s'il existe ;
- ha : seulement les entités choisies (`wall_ha`), jamais `lock.*`, `alarm_control_panel.*`, ni un `cover` de type
  garage ou portail (filtré à la lecture ET au réglage) ;
- mode invité : titres d'agenda, tâches, menus, courses et projets retirés ; expire seul au bout de 4 h ;
- coches : idempotentes, locales au foyer (aucun effet externe), journalisées « depuis le mur », 30 par heure et par
  mur ; 404 pour une tâche hors foyer, d'un projet réservé aux parents, ou cachée (mode invité).
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from nokker.household.projects.service import TZ, FamilyProjects, Home
from nokker.tenancy.context import tenant_scope

logger = logging.getLogger("nokker.household.wall")

GUEST_MAX_S = 4 * 3600
CHECKS_PER_HOUR = 30
READS_PER_MIN = 6
AGENDA_DAYS = 7
MAX_HA = 30
COLORS = ("#E4572E", "#17BEBB", "#FFC914", "#76B041", "#7E5BEF", "#F25F9C")
FORBIDDEN_HA_DOMAINS = ("lock", "alarm_control_panel")
_GARAGE = re.compile(r"garage|gate|portail|portal|tor\b|cancello", re.IGNORECASE)
_ENTITY = re.compile(r"^[a-z_]+\.[a-z0-9_]{1,80}$")
WALL_PATHS = re.compile(r"^/v1/glance/(household|guest|tasks/[A-Za-z0-9_-]{1,40}/done|shopping/[A-Za-z0-9_-]{1,40}/done)$")


class WallError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def is_wall_path(path: str) -> bool:
    return bool(WALL_PATHS.match(path.split("?", 1)[0].rstrip("/")))


def ha_allowed(entity: dict[str, Any]) -> bool:
    """Jamais une serrure, une alarme, ni une porte de garage ou un portail (classe ou nom)."""
    eid = str(entity.get("entity_id") or "")
    domain = eid.split(".", 1)[0]
    if domain in FORBIDDEN_HA_DOMAINS:
        return False
    if domain == "cover":
        cls = str(entity.get("device_class") or "")
        if cls in ("garage", "gate") or _GARAGE.search(eid) or _GARAGE.search(str(entity.get("name") or "")):
            return False
    return True


def ha_entity_ok(entity_id: str) -> bool:
    """Réglage `wall_ha` : forme `domaine.objet`, domaine permis, pas un cover de garage ou portail par son nom."""
    return bool(_ENTITY.match(entity_id)) and ha_allowed({"entity_id": entity_id})


def initials(name: str) -> str:
    parts = [p for p in re.split(r"[\s-]+", name) if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def shopping_id(text: str) -> str:
    return "sh_" + hashlib.sha256(" ".join(text.split()).lower().encode("utf-8")).hexdigest()[:12]


@dataclass
class HouseholdWall:
    projects: FamilyProjects
    # Compte → prénom saisi ("" sans prénom) : jamais l'adresse.
    first_name: Callable[[str], str]
    # Compte du titulaire → [(compte propriétaire, source d'agenda)] des agendas PARTAGÉS avec le foyer. None : [].
    calendars: Callable[[str], list[tuple[str | None, Any]]] | None = None
    # Foyer → articles de la liste de courses commune. None : [].
    shopping: Callable[[Home], list[str]] | None = None
    # Lieu du foyer (`wall_place`) → météo {now_c, code, hi_c, lo_c, summary} | None ; départs [{stop, line, to,
    # minutes}] (pack Essentiels, ses caches). Sans lieu réglé : null / [] sans appel.
    weather: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None
    departures: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None
    # (compte du titulaire, entités choisies) → états [{entity_id, name, state, unit?, device_class?}]. None : [].
    ha_states: Callable[[str, list[str]], list[dict[str, Any]]] | None = None
    clock: Callable[[], float] = time.time
    _checks: dict[str, deque] = field(default_factory=dict, repr=False)
    _reads: dict[str, deque] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ── accès ──
    def home(self, device: Any) -> Home | None:
        if getattr(device, "surface", "") != "wall":
            raise WallError(403, "not_wall", "jeton d'un écran mural seulement")
        h = self.projects.home_of(device.account_id)
        if h is None or (getattr(device, "household_id", "") and device.household_id != h.household_id):
            return None
        return h

    def _limit(self, table: dict[str, deque], key: str, n: int, window: float) -> bool:
        with self._lock:
            now = self.clock()
            q = table.setdefault(key, deque())
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= n:
                return False
            q.append(now)
            return True

    def read_allowed(self, device_id: str) -> bool:
        return self._limit(self._reads, device_id, READS_PER_MIN, 60.0)

    def _settings(self, h: Home) -> Any:
        with tenant_scope(h.tenant_id):
            return self.projects.store.settings(h.household_id)

    def guest_on(self, h: Home) -> bool:
        return self._settings(h).guest_until > self.clock()

    # ── vue ──
    def view(self, device: Any) -> dict[str, Any] | None:
        h = self.home(device)
        if h is None:
            return None
        s = self._settings(h)
        guest = s.guest_until > self.clock()
        tz = ZoneInfo(TZ)
        today = datetime.fromtimestamp(self.clock(), tz).date()
        members = []
        for i, p in enumerate(h.people):
            name = (self.first_name(p.account_id) or "").strip() or f"Membre {i + 1}"
            members.append({"id": p.account_id, "name": name, "color": COLORS[i % len(COLORS)],
                            "initials": initials(name)})
        ids = {p.account_id for p in h.people}
        out: dict[str, Any] = {"members": members, "guest": guest, "agenda": self._agenda(device, ids, guest),
                               "tasks": [], "meals": [], "shopping": [], "weather": None, "departures": [],
                               "projects": [], "ha": self._ha(device, s.wall_ha)}
        place = s.wall_place
        try:
            out["weather"] = self.weather(place) if self.weather is not None and place else None
        except Exception:  # noqa: BLE001 — source en panne : champ vide, jamais un 500
            logger.warning("mur : météo illisible", exc_info=True)
        try:
            out["departures"] = list(self.departures(place) or []) if self.departures is not None and place \
                and place.get("stops") else []
        except Exception:  # noqa: BLE001
            logger.warning("mur : départs illisibles", exc_info=True)
        if guest:
            return out
        with tenant_scope(h.tenant_id):
            projects = [p for p in self.projects.store.projects(h.household_id)
                        if p.visibility == "household" and p.status == "active"]
            tasks = self.projects.store.tasks(h.household_id)
        shared = {p.project_id for p in projects}
        day = today.isoformat()
        out["tasks"] = [{"id": t.task_id, "member_id": t.assignee if t.assignee in ids else None, "title": t.title,
                         "done": t.status == "done", "due": t.due}
                        for t in tasks if t.project_id in shared and t.due == day]
        for p in projects:
            mine = [t for t in tasks if t.project_id == p.project_id]
            todo = sorted((t for t in mine if t.status == "todo" and t.due), key=lambda t: t.due)
            row: dict[str, Any] = {"id": p.project_id, "title": p.title,
                                   "progress": round(sum(t.status == "done" for t in mine) / len(mine), 2)
                                   if mine else 0.0}
            if todo:
                row["next_due"], row["next_step"] = todo[0].due, todo[0].title
            out["projects"].append(row)
        done = set(s.shopping_done)
        try:
            items = list(self.shopping(h) or []) if self.shopping is not None else []
        except Exception:  # noqa: BLE001
            logger.warning("mur : liste de courses illisible", exc_info=True)
            items = []
        seen: set[str] = set()
        for text in items:
            sid = shopping_id(str(text))
            if sid in seen:
                continue
            seen.add(sid)
            out["shopping"].append({"id": sid, "text": " ".join(str(text).split())[:120], "done": sid in done})
        return out

    def _agenda(self, device: Any, members: set[str], guest: bool) -> list[dict[str, Any]]:
        if self.calendars is None:
            return []
        from nokker.connectors.calendar import parse_events_result
        from nokker.core.ports import ToolContext

        ctx = ToolContext(thread_id=f"wall:{device.id}", turn_index=0, channel="glance", sender=device.account_id)
        out = []
        try:
            sources = self.calendars(device.account_id)
        except Exception:  # noqa: BLE001
            logger.warning("mur : agendas partagés illisibles", exc_info=True)
            return []
        for owner, src in sources:
            try:
                r = src.call("calendar_events", {"days": AGENDA_DAYS}, ctx)
                if getattr(r, "is_error", False):
                    continue
                events = parse_events_result(r.content).get("events") or []
            except Exception:  # noqa: BLE001 — un agenda en panne n'emporte pas les autres
                continue
            for e in events:
                if not isinstance(e, dict) or not e.get("start"):
                    continue
                private = bool(e.get("private")) or str(e.get("class") or "").upper() in ("PRIVATE", "CONFIDENTIAL")
                key = f"{owner}|{e.get('uid') or ''}|{e.get('start')}|{e.get('summary') or ''}"
                row: dict[str, Any] = {"id": "ev_" + hashlib.sha256(key.encode()).hexdigest()[:12],
                                       "member_id": owner if owner in members else None, "start": str(e["start"]),
                                       "all_day": bool(e.get("all_day")) or len(str(e["start"])) == 10}
                if e.get("end"):
                    row["end"] = str(e["end"])
                if not guest and not private and e.get("summary"):
                    row["title"] = " ".join(str(e["summary"]).split())[:120]
                out.append(row)
        out.sort(key=lambda x: x["start"])
        return out

    def _ha(self, device: Any, chosen: list[str]) -> list[dict[str, Any]]:
        wanted = [e for e in chosen if ha_entity_ok(e)][:MAX_HA]
        if not wanted or self.ha_states is None:
            return []
        try:
            states = self.ha_states(device.account_id, wanted) or []
        except Exception:  # noqa: BLE001
            logger.warning("mur : Home Assistant illisible", exc_info=True)
            return []
        out = []
        for st in states:
            eid = str(st.get("entity_id") or "")
            if eid not in wanted or not ha_allowed(st):
                continue
            row = {"entity_id": eid, "name": str(st.get("name") or eid)[:80], "state": str(st.get("state") or ""),
                   "domain": eid.split(".", 1)[0]}
            if st.get("unit"):
                row["unit"] = str(st["unit"])[:12]
            out.append(row)
        return out

    # ── écritures (locales au foyer) ──
    def set_guest(self, device: Any, on: Any) -> dict[str, Any]:
        if not isinstance(on, bool):
            raise WallError(400, "bad_request", "corps attendu : {on: true | false}")
        h = self.home(device)
        if h is None:
            raise WallError(404, "no_household", "aucun foyer")
        s = self._settings(h)
        s.guest_until = self.clock() + GUEST_MAX_S if on else 0.0
        with tenant_scope(h.tenant_id):
            self.projects.store.put_settings(s)
        logger.info("mur %s : mode invité %s", device.id, "allumé" if on else "éteint")
        return {"guest": on, "until": datetime.fromtimestamp(s.guest_until, ZoneInfo("UTC")).isoformat() if on else None}

    def _check_budget(self, device: Any) -> None:
        if not self._limit(self._checks, device.id, CHECKS_PER_HOUR, 3600.0):
            raise WallError(429, "wall_rate", "trop de coches depuis ce mur : réessayer plus tard")

    def task_done(self, device: Any, task_id: str, done: Any = True) -> dict[str, Any]:
        if not isinstance(done, bool):
            raise WallError(400, "bad_request", "done : vrai ou faux")
        h = self.home(device)
        if h is None or self.guest_on(h):
            raise WallError(404, "not_found", "tâche introuvable")
        with tenant_scope(h.tenant_id):
            t = self.projects.store.task(h.household_id, task_id)
            p = self.projects.store.project(h.household_id, t.project_id) if t is not None else None
        if t is None or p is None or p.visibility != "household":
            raise WallError(404, "not_found", "tâche introuvable")
        self._check_budget(device)
        if (t.status == "done") != done:  # idempotent : rien ne bouge si l'état est déjà le bon
            with tenant_scope(h.tenant_id):
                t = self.projects.store.set_done(h.household_id, task_id, done, f"wall:{device.id}", self.clock())
            logger.info("tâche %s %s depuis le mur %s", task_id, "faite" if done else "rouverte", device.id)
        return {"id": t.task_id, "done": t.status == "done"}

    def shopping_done(self, device: Any, item_id: str, done: Any = True) -> dict[str, Any]:
        if not isinstance(done, bool):
            raise WallError(400, "bad_request", "done : vrai ou faux")
        h = self.home(device)
        if h is None or self.guest_on(h):
            raise WallError(404, "not_found", "article introuvable")
        items = {shopping_id(str(x)) for x in (self.shopping(h) or [])} if self.shopping is not None else set()
        if item_id not in items:
            raise WallError(404, "not_found", "article introuvable")
        self._check_budget(device)
        s = self._settings(h)
        cur = set(s.shopping_done) & items  # les articles retirés de la liste sont oubliés
        if (item_id in cur) != done:
            cur = cur | {item_id} if done else cur - {item_id}
            logger.info("article %s %s depuis le mur %s", item_id, "coché" if done else "décoché", device.id)
        s.shopping_done = sorted(cur)
        with tenant_scope(h.tenant_id):
            self.projects.store.put_settings(s)
        return {"id": item_id, "done": done}


_wall: HouseholdWall | None = None


def configure(wall: HouseholdWall | None) -> None:
    global _wall
    _wall = wall


def service() -> HouseholdWall | None:
    return _wall


def etag(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


def handle(h: Any, method: str, path: str, device: Any, wall: HouseholdWall) -> None:
    """Routes du mur, appelées par `_glance_gate` APRÈS la résolution du jeton nkg_."""
    import json

    route = path.split("?", 1)[0].rstrip("/")
    try:
        if route == "/v1/glance/household" and method == "GET":
            if getattr(device, "surface", "") != "wall":
                raise WallError(403, "not_wall", "jeton d'un écran mural seulement")
            if not wall.read_allowed(device.id):
                raise WallError(429, "glance_rate", "trop de lectures : réessayez plus tard")
            body = json.dumps(wall.view(device), ensure_ascii=False, sort_keys=True).encode("utf-8")
            tag = etag(body)
            if (h.headers.get("If-None-Match") or "").strip() == tag:
                h.send_response(304)
                h.send_header("ETag", tag)
                h.send_header("Cache-Control", "no-cache")
                h.end_headers()
                return
            h.send_response(200)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(body)))
            h.send_header("ETag", tag)
            h.send_header("Cache-Control", "no-cache")
            h.end_headers()
            h.wfile.write(body)
            return
        if method != "POST":
            raise WallError(405, "method", "méthode non permise")
        raw = h._read_json_body()
        body = raw if isinstance(raw, dict) else {}
        parts = route.split("/")
        if route == "/v1/glance/guest":
            h._json(200, wall.set_guest(device, body.get("on")))
        elif parts[3] == "tasks":
            h._json(200, wall.task_done(device, parts[4], body.get("done", True)))
        else:
            h._json(200, wall.shopping_done(device, parts[4], body.get("done", True)))
    except WallError as e:
        h._json(e.status, {"error": {"code": e.code, "message": e.message}})


__all__ = ["CHECKS_PER_HOUR", "configure", "service", "FORBIDDEN_HA_DOMAINS", "GUEST_MAX_S", "HouseholdWall", "WallError", "etag",
           "ha_allowed", "ha_entity_ok", "handle", "initials", "is_wall_path", "shopping_id"]
