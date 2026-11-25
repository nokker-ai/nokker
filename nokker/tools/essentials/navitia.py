"""Repli France des transports (`transit_departures`, `transit_trip`) — API SNCF (`https://api.sncf.com/v1`,
moteur Navitia, données sous ODbL), avec un jeton gratuit lu dans un secret (`essentials_v2.navitia_token`,
`secret:NAVITIA_TOKEN`). Sans jeton, aucun repli : les outils restent sur l'horaire suisse seul.

Servi seulement quand l'horaire suisse ne connaît pas l'arrêt (il couvre déjà les arrêts frontaliers du Grand Genève).
Les heures Navitia sont locales (Europe/Paris), sans décalage : le décalage est ajouté au rendu.

Non vérifié en réel tant que le jeton n'existe pas : formats d'après la documentation Navitia (doc.navitia.io).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from nokker.tools.essentials.common import Ambiguous, EssentialsError, RateLimit, Source, TTLCache, fold, plural

BASE = "https://api.sncf.com/v1/coverage/sncf"
PARIS = ZoneInfo("Europe/Paris")


def _dt(raw: str | None) -> datetime | None:
    try:
        return datetime.strptime(str(raw), "%Y%m%dT%H%M%S").replace(tzinfo=PARIS)
    except (TypeError, ValueError):
        return None


def _hhmm(raw: str | None) -> str:
    dt = _dt(raw)
    return dt.strftime("%H:%M") if dt else "?"


def _delay(real: str | None, base: str | None) -> str:
    a, b = _dt(real), _dt(base)
    if a and b and a > b:
        return f" (+{int((a - b).total_seconds() // 60)} min)"
    return ""


def _zone(raw: str | None) -> str:
    dt = _dt(raw)
    if not dt:
        return "date inconnue"
    off = dt.strftime("%z")
    return f"{dt.date().isoformat()}, heures locales UTC{off[:3]}:{off[3:]}"


def _name(raw: Any) -> str:
    """« Thonon-les-Bains (Thonon-les-Bains) » → « Thonon-les-Bains » (Navitia répète la commune entre parenthèses)."""
    text = str(raw or "?").strip()
    if text.endswith(")") and " (" in text:
        head, _, tail = text[:-1].partition(" (")
        if fold(head) == fold(tail):
            return head
    return text


def _line(info: dict[str, Any]) -> str:
    mode = (info.get("commercial_mode") or "").strip()
    code = (info.get("code") or "").strip()
    return " ".join(x for x in (mode, code) if x) or "?"


def _navitia_dt(when_date: str, when_time: str) -> str:
    now = datetime.now(PARIS)
    day = when_date or now.date().isoformat()
    hhmm = when_time or (now.strftime("%H:%M") if not when_date else "00:00")
    return day.replace("-", "") + "T" + hhmm.replace(":", "").zfill(4) + "00"


@dataclass
class NavitiaFrance:
    token: str
    source: Source | None = None
    cache: TTLCache = field(default_factory=lambda: TTLCache(ttl_s=60, max_items=256))
    places: TTLCache = field(default_factory=lambda: TTLCache(ttl_s=86_400, max_items=512))

    def __post_init__(self) -> None:
        if self.source is None:
            # Jeton Navitia : en-tête Authorization (doc Navitia : « Authorization: <token> »), jamais dans l'URL.
            self.source = Source("SNCF (Navitia)", RateLimit(per_minute=20), headers={"Authorization": self.token})

    def stop_area(self, query: str) -> tuple[str, str]:
        """(id, nom) d'une zone d'arrêt SNCF ; `Ambiguous` si plusieurs, `introuvable` si aucune."""
        key = fold(query)
        cached = self.places.get(key)
        if cached is not None:
            return cached
        data = self.source.get_json(f"{BASE}/places?{urlencode({'q': query, 'type[]': 'stop_area', 'count': '8'})}")
        found = [(str(p.get("id")), _name(p.get("name"))) for p in data.get("places") or []
                 if isinstance(p, dict) and p.get("embedded_type") == "stop_area" and p.get("id")]
        if not found:
            raise EssentialsError(f"arrêt « {query} » introuvable dans l'horaire suisse ni dans celui de la SNCF.",
                                  "introuvable")
        exact = [f for f in found if fold(f[1]).split(" (")[0] == key or fold(f[1]) == key]
        if len(exact) == 1 or len(found) == 1:
            hit = exact[0] if exact else found[0]
            self.places.set(key, hit)
            return hit
        raise Ambiguous("arrêt", query, [n for _, n in (exact if len(exact) > 1 else found)])

    def departures(self, station: str, limit: int, when_date: str, when_time: str) -> str:
        sid, name = self.stop_area(station)
        params = {"count": str(limit), "data_freshness": "realtime"}
        if when_date or when_time:
            params["from_datetime"] = _navitia_dt(when_date, when_time)
        url = f"{BASE}/stop_areas/{quote(sid, safe='')}/departures?{urlencode(params)}"
        cached = self.cache.get(url)
        if cached is not None:
            return cached
        deps = self.source.get_json(url).get("departures") or []
        first = ((deps[0] or {}).get("stop_date_time") or {}).get("departure_date_time") if deps else None
        lines = [f"Prochains départs de {name} ({_zone(first)}) :"]
        for d in deps[:limit]:
            sdt, info = d.get("stop_date_time") or {}, d.get("display_informations") or {}
            lines.append(f"- {_hhmm(sdt.get('departure_date_time'))}"
                         f"{_delay(sdt.get('departure_date_time'), sdt.get('base_departure_date_time'))} "
                         f"{_line(info)} → {_name(info.get('direction'))}")
        if not deps:
            lines.append("- aucun départ trouvé.")
        text = "\n".join(lines)
        self.cache.set(url, text)
        return text

    def trip(self, origin: str, destination: str, when_date: str, when_time: str, arrival: bool, limit: int) -> str:
        (fid, fname), (tid, tname) = self.stop_area(origin), self.stop_area(destination)
        params = {"from": fid, "to": tid, "count": str(limit), "data_freshness": "realtime",
                  "datetime_represents": "arrival" if arrival else "departure"}
        if when_date or when_time:
            params["datetime"] = _navitia_dt(when_date, when_time)
        url = f"{BASE}/journeys?{urlencode(params)}"
        cached = self.cache.get(url)
        if cached is not None:
            return cached
        journeys = self.source.get_json(url).get("journeys") or []
        if not journeys:
            raise EssentialsError(f"aucun trajet SNCF trouvé de « {fname} » à « {tname} ».", "introuvable")
        lines = [f"Trajets de {fname} à {tname} ({_zone(journeys[0].get('departure_date_time'))}) :"]
        for i, j in enumerate(journeys[:limit], start=1):
            minutes = int(j.get("duration") or 0) // 60
            dur = f"{minutes // 60} h {minutes % 60:02d}" if minutes >= 60 else f"{minutes} min"
            n = j.get("nb_transfers")
            lines.append(f"{i}. départ {_hhmm(j.get('departure_date_time'))} → arrivée "
                         f"{_hhmm(j.get('arrival_date_time'))}, durée {dur}, "
                         + (plural(int(n), "changement", "changements") if isinstance(n, int) else "? changement"))
            for s in j.get("sections") or []:
                if s.get("type") != "public_transport":
                    continue
                info = s.get("display_informations") or {}
                lines.append(f"   {_line(info)} {_name((s.get('from') or {}).get('name'))} "
                             f"{_hhmm(s.get('departure_date_time'))}"
                             f"{_delay(s.get('departure_date_time'), s.get('base_departure_date_time'))} → "
                             f"{_name((s.get('to') or {}).get('name'))} {_hhmm(s.get('arrival_date_time'))}"
                             f"{_delay(s.get('arrival_date_time'), s.get('base_arrival_date_time'))}")
        text = "\n".join(lines)
        self.cache.set(url, text)
        return text


__all__ = ["NavitiaFrance"]
