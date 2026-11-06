"""Transports publics suisses — transport.opendata.ch (API non officielle d'Opendata.ch, logiciel MIT ; données de
l'horaire national et du temps réel publiées sur opentransportdata.swiss, « Timetable data: opentransportdata.swiss »).

Pas de limite fixe par client, mais l'API demande de mettre en cache et ralentit qui interroge trop souvent : cache de
60 s par question (le temps réel y est rafraîchi toutes les ~30 s) et au plus 30 requêtes par minute pour tout le
processus.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import re

from nokker.tools.essentials.common import Ambiguous, EssentialsError, RateLimit, Source, TTLCache, fold, plural

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^\d{1,2}:\d{2}$")

BASE = "https://transport.opendata.ch/v1"
SOURCE_LINE = "Source : transport.opendata.ch (Opendata.ch) — données horaires : opentransportdata.swiss."
SOURCE_LINE_FALLBACK = ("Source : transport.opendata.ch (Opendata.ch) — données horaires : opentransportdata.swiss et "
                        "SNCF (Navitia, api.sncf.com, ODbL).")


def _hhmm(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        return datetime.fromisoformat(iso.replace("+0100", "+01:00").replace("+0200", "+02:00")).strftime("%H:%M")
    except ValueError:
        return iso[11:16] if len(iso) >= 16 else iso


def _zone(iso: str | None) -> str:
    """« 2026-11-05, heures UTC+01:00 » d'après un horodatage ISO de l'API."""
    if not iso:
        return "date inconnue"
    try:
        dt = datetime.fromisoformat(iso.replace("+0100", "+01:00").replace("+0200", "+02:00"))
    except ValueError:
        return iso
    off = dt.strftime("%z")
    return f"{dt.date().isoformat()}, heures locales UTC{off[:3]}:{off[3:]}"


def _delay(stop: dict[str, Any]) -> str:
    d = stop.get("delay")
    if isinstance(d, (int, float)) and d > 0:
        return f" (+{int(d)} min)"
    return ""


def _platform(stop: dict[str, Any]) -> str:
    prog = (stop.get("prognosis") or {}).get("platform")
    plat = stop.get("platform")
    if prog and plat and prog != plat:
        return f", voie {prog} (au lieu de {plat})"
    return f", voie {prog or plat}" if (prog or plat) else ""


def _line(j: dict[str, Any]) -> str:
    cat = (j.get("category") or "").strip()
    num = (j.get("number") or "").strip()
    return " ".join(x for x in (cat, num) if x) or (j.get("name") or "?")


def _duration(raw: str | None) -> str:
    # « 00d00:49:00 »
    if not raw or "d" not in raw:
        return raw or "?"
    days, rest = raw.split("d", 1)
    h, m = rest.split(":")[:2]
    total_h = int(days) * 24 + int(h)
    return f"{total_h} h {int(m):02d}" if total_h else f"{int(m)} min"


@dataclass
class Transit:
    source: Source = field(default_factory=lambda: Source("transport.opendata.ch", RateLimit(per_minute=30)))
    cache: TTLCache = field(default_factory=lambda: TTLCache(ttl_s=60, max_items=512))
    places: TTLCache = field(default_factory=lambda: TTLCache(ttl_s=86_400, max_items=1024))
    # Repli France (vague 2, `nokker/tools/essentials/navitia.py`) : None = horaire suisse seul (vague 1, inchangé).
    fallback: Any = None

    def _unknown(self, exc: EssentialsError) -> bool:
        return self.fallback is not None and exc.code == "introuvable"

    def departures_with_source(self, station: str, limit: int, when_date: str, when_time: str) -> tuple[str, str]:
        """(texte, ligne de source) : l'horaire suisse, puis le repli SNCF seulement s'il ne connaît pas l'arrêt."""
        try:
            return self.departures(station, limit, when_date, when_time), SOURCE_LINE
        except EssentialsError as exc:
            if not self._unknown(exc):
                raise
        return self.fallback.departures(station, limit, when_date, when_time), SOURCE_LINE_FALLBACK

    def trip_with_source(self, origin: str, destination: str, when_date: str, when_time: str, arrival: bool,
                         limit: int) -> tuple[str, str]:
        try:
            return self.trip(origin, destination, when_date, when_time, arrival, limit), SOURCE_LINE
        except EssentialsError as exc:
            if not self._unknown(exc):
                raise
        return self.fallback.trip(origin, destination, when_date, when_time, arrival, limit), SOURCE_LINE_FALLBACK

    def resolve(self, query: str, kind: str) -> str:
        """Nom exact d'un arrêt (ou lieu) de l'horaire suisse. Un seul candidat, ou un seul dont le nom égale la
        demande : pris. Sinon `Ambiguous` avec la liste — jamais de choix silencieux."""
        key = f"{kind}|{fold(query)}"
        cached = self.places.get(key)
        if cached is not None:
            return cached
        params = {"query": query}
        if kind == "station":
            params["type"] = "station"
        data = self.source.get_json(f"{BASE}/locations?{urlencode(params)}")
        names = list(dict.fromkeys(str(st.get("name")) for st in data.get("stations") or []
                                   if isinstance(st, dict) and st.get("name")))
        if not names:
            raise EssentialsError(f"arrêt ou lieu « {query} » introuvable dans l'horaire suisse.", "introuvable")
        exact = [n for n in names if fold(n) == fold(query)]
        if len(exact) == 1 or len(names) == 1:
            name = exact[0] if exact else names[0]
            self.places.set(key, name)
            return name
        raise Ambiguous("arrêt" if kind == "station" else "lieu", query, exact if len(exact) > 1 else names)

    def departures(self, station: str, limit: int, when_date: str, when_time: str) -> str:
        for value, rx, what in ((when_date, _DATE, "date (AAAA-MM-JJ)"), (when_time, _TIME, "heure (hh:mm)")):
            if value and not rx.match(value):
                raise EssentialsError(f"{what} illisible : « {value} ».", "argument_invalide")
        station = self.resolve(station, "station")
        params = {"station": station, "limit": str(limit)}
        if when_date or when_time:
            from zoneinfo import ZoneInfo

            day = when_date or datetime.now(ZoneInfo("Europe/Zurich")).date().isoformat()
            params["datetime"] = f"{day} {when_time or '00:00'}"
        url = f"{BASE}/stationboard?{urlencode(params)}"
        cached = self.cache.get(url)
        if cached is not None:
            return cached
        data = self.source.get_json(url)
        st = (data.get("station") or {}).get("name")
        board = data.get("stationboard") or []
        if not st:
            raise EssentialsError(f"arrêt « {station} » introuvable dans l'horaire suisse.", "introuvable")
        first_dep = ((board[0] or {}).get("stop") or {}).get("departure") if board else None
        lines = [f"Prochains départs de {st} ({_zone(first_dep)}) :"]
        for j in board[:limit]:
            stop = j.get("stop") or {}
            lines.append(f"- {_hhmm(stop.get('departure'))}{_delay(stop)} {_line(j)} → {j.get('to') or '?'}"
                         f"{_platform(stop)}")
        if not board:
            lines.append("- aucun départ trouvé.")
        text = "\n".join(lines)
        self.cache.set(url, text)
        return text

    def trip(self, origin: str, destination: str, when_date: str, when_time: str, arrival: bool, limit: int) -> str:
        origin, destination = self.resolve(origin, "all"), self.resolve(destination, "all")
        params = {"from": origin, "to": destination, "limit": str(limit)}
        if when_date:
            params["date"] = when_date
        if when_time:
            params["time"] = when_time
        if arrival:
            params["isArrivalTime"] = "1"
        url = f"{BASE}/connections?{urlencode(params)}"
        cached = self.cache.get(url)
        if cached is not None:
            return cached
        data = self.source.get_json(url)
        conns = data.get("connections") or []
        if not conns:
            raise EssentialsError(f"aucun trajet trouvé de « {origin} » à « {destination} » (noms d'arrêts suisses).",
                                  "introuvable")
        first = conns[0]
        a = ((first.get("from") or {}).get("station") or {}).get("name") or origin
        b = ((first.get("to") or {}).get("station") or {}).get("name") or destination
        lines = [f"Trajets de {a} à {b} ({_zone((first.get('from') or {}).get('departure'))}) :"]
        for i, c in enumerate(conns[:limit], start=1):
            dep, arr = c.get("from") or {}, c.get("to") or {}
            transfers = c.get("transfers")
            head = (f"{i}. départ {_hhmm(dep.get('departure'))}{_delay(dep)}{_platform(dep)} → arrivée "
                    f"{_hhmm(arr.get('arrival'))}{_delay(arr)}, durée {_duration(c.get('duration'))}, "
                    + (plural(int(transfers), "changement", "changements") if isinstance(transfers, int)
                       else "? changement"))
            lines.append(head)
            for s in c.get("sections") or []:
                j = s.get("journey")
                sd, sa = s.get("departure") or {}, s.get("arrival") or {}
                sdn = (sd.get("station") or {}).get("name") or "?"
                san = (sa.get("station") or {}).get("name") or "?"
                if j:
                    lines.append(f"   {_line(j)} {sdn} {_hhmm(sd.get('departure'))}{_delay(sd)}{_platform(sd)} → "
                                 f"{san} {_hhmm(sa.get('arrival'))}{_delay(sa)}")
                elif s.get("walk") is not None:
                    lines.append(f"   à pied {sdn} → {san}")
        text = "\n".join(lines)
        self.cache.set(url, text)
        return text


__all__ = ["SOURCE_LINE", "SOURCE_LINE_FALLBACK", "Transit"]
