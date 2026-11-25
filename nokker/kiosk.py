"""Mode kiosque d'un Lokkal (docs/KIOSK.md) : l'écran de la maison branché en HDMI sur la machine elle-même.

    GET /v1/local/kiosk   → {now, tz, weather, departures, ha, presenter, night[, screen][, household]}

Règles tenues ici, jamais par le front :
- Lokkal seulement (sur nokker.ai : 404) ; drapeau `kiosk.enabled` éteint par défaut (éteint : 404, comme avant) ;
- jeton LOCAL du titulaire seulement (membre, jeton faux ou périmé : 401) ;
- requête SUR la machine seulement : arrivée par la passerelle (relais ou tunnel) → 403 `kiosk_on_machine`;
- lecture seule : aucune autre méthode (405), aucune action, aucune carte, aucun contenu de mail ni d'agenda ;
- contenu 100 % local : météo et départs du pack Essentiels (s'il est branché ; sinon null / []), états Home
  Assistant des SEULES entités listées dans `kiosk.ha_entities` ET exposées par la liste blanche de la maison,
  jamais `lock.*`, `alarm_control_panel.*` ni un `cover` de garage ou portail (même filtre que le mur F1) ;
- `screen` (lot V7, `ambient.screens.enabled`) : l'écran du mur PUBLIÉ par le titulaire, `{id, version, title,
  frame_url}` ou null s'il n'y en a pas. Le champ n'existe QUE si les écrans sont allumés (sinon la vue est celle d'avant,
  à l'octet). Jamais de HTML dans la vue : le client affiche `<iframe sandbox="" src=frame_url>` (cadre servi par le noyau
  sous sa CSP stricte). Rendu SANS prochain rendez-vous : le kiosque ne montre aucun contenu d'agenda.
- `household` (foyer démo, `kiosk.household_demo`, nokker/kiosk_demo.py) : une vue Foyer FICTIVE lue en configuration, marquée
  `demo: true` ; le champ n'existe que si le bloc est posé et valide (sinon la vue est celle d'avant, à l'octet) ;
- 6 lectures par minute ; une source en panne rend null / [], jamais un 500.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from nokker.household.projects.wall import MAX_HA, ha_allowed, ha_entity_ok

logger = logging.getLogger("nokker.kiosk")

PATH = "/v1/local/kiosk"
READS_PER_MIN = 6
DEFAULT_TZ = "Europe/Zurich"


@dataclass
class Kiosk:
    """Service du kiosque ; `home` = la source maison du noyau (`nokker.home.source.HomeAssistantSource`) ou None,
    `weather(lat, lon)` et `departures(stop, n)` = le pack Essentiels, ou None."""

    place: dict[str, Any] = field(default_factory=dict)
    ha_entities: tuple[str, ...] = ()
    home: Any = None
    weather: Callable[[float, float], dict[str, Any] | None] | None = None
    departures: Callable[[str, int], list[dict[str, Any]]] | None = None
    screen: Callable[[], dict[str, Any] | None] | None = None  # écran publié (V7) ; None : écrans éteints, pas de champ
    demo: Any = None  # foyer démo (nokker/kiosk_demo.py) ; None : pas de champ `household`
    tz: str = DEFAULT_TZ
    night: bool = True  # mode nuit de l'écran (22:00–07:00 : horloge seule) ; `kiosk.night: false` le coupe
    clock: Callable[[], float] = time.time
    _reads: deque = field(default_factory=deque)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def from_config(cls, raw: Any, **kw: Any) -> Kiosk | None:
        """Bloc `kiosk:` de la config ; None si éteint ou mal formé (motif au journal, jamais un plantage)."""
        if not isinstance(raw, dict) or not raw.get("enabled"):
            return None
        place = raw.get("place") or {}
        if not isinstance(place, dict):
            logger.error("kiosk : `place` doit être un objet, kiosque éteint")
            return None
        ents = [str(e) for e in (raw.get("ha_entities") or [])]
        bad = [e for e in ents if not ha_entity_ok(e)]
        if bad:  # serrure, alarme, garage, portail, ou identifiant mal formé : refus au réglage (comme le mur)
            logger.error("kiosk : entités refusées %s, kiosque éteint", bad)
            return None
        demo = None
        if raw.get("household_demo") is not None:  # foyer démo : refusé en entier s'il est mal formé, le kiosque reste
            from nokker.kiosk_demo import Demo, DemoError

            try:
                demo = Demo.parse(raw["household_demo"])
            except DemoError as e:
                logger.error("kiosk : household_demo refusé (%s), pas de foyer démo", e)
        return cls(place=dict(place), ha_entities=tuple(ents[:MAX_HA]), tz=str(raw.get("tz") or DEFAULT_TZ),
                   night=raw.get("night", True) is not False, demo=demo, **kw)

    def read_allowed(self) -> bool:
        now = self.clock()
        with self._lock:
            while self._reads and now - self._reads[0] > 60.0:
                self._reads.popleft()
            if len(self._reads) >= READS_PER_MIN:
                return False
            self._reads.append(now)
            return True

    def _weather(self) -> dict[str, Any] | None:
        if self.weather is None or "lat" not in self.place or "lon" not in self.place:
            return None
        try:
            return self.weather(float(self.place["lat"]), float(self.place["lon"]))
        except Exception:  # noqa: BLE001 — météo en panne : null, jamais un 500
            logger.warning("kiosk : météo illisible")
            return None

    def _departures(self) -> list[dict[str, Any]]:
        if self.departures is None:
            return []
        rows: list[dict[str, Any]] = []
        for stop in self.place.get("stops") or []:
            try:
                rows += self.departures(str(stop), 4)
            except Exception:  # noqa: BLE001 — un arrêt en panne n'emporte pas les autres
                logger.warning("kiosk : départs de l'arrêt %s illisibles", stop)
        return sorted(rows, key=lambda r: r.get("minutes", 0))[:8]

    def _ha(self) -> list[dict[str, Any]]:
        if self.home is None or not self.ha_entities:
            return []
        try:
            snap = self.home.snapshot()  # liste blanche de la maison déjà appliquée
        except Exception:  # noqa: BLE001 — passerelle ou HA injoignable : [], jamais un 500
            logger.warning("kiosk : états de la maison illisibles")
            return []
        out: list[dict[str, Any]] = []
        for eid in self.ha_entities:
            st = snap.states.get(eid)
            if st is None:
                continue
            attrs = dict(st.get("attributes") or {})
            name = str(attrs.get("friendly_name") or eid)
            if not ha_allowed({"entity_id": eid, "device_class": attrs.get("device_class"), "name": name}):  # filtre à la lecture aussi (classe garage/portail posée après coup)
                continue
            row = {"entity_id": eid, "name": name, "state": str(st.get("state")), "domain": eid.split(".", 1)[0]}
            if attrs.get("unit_of_measurement"):
                row["unit"] = str(attrs["unit_of_measurement"])
            out.append(row)
        return out

    def _screen(self) -> dict[str, Any] | None:
        """L'écran publié, SANS son HTML : id, version, titre et l'URL signée du cadre. Source en panne : null."""
        if self.screen is None:
            return None
        try:
            s = self.screen()
        except Exception:  # noqa: BLE001 — écrans en panne : null, jamais un 500
            logger.warning("kiosk : écran publié illisible")
            return None
        if not s or not s.get("frame_url"):
            return None
        return {"id": s.get("id"), "version": s.get("version"), "title": s.get("title"), "frame_url": s["frame_url"]}

    def view(self) -> dict[str, Any]:
        now = datetime.fromtimestamp(self.clock(), ZoneInfo(self.tz))
        out = {"now": now.isoformat(timespec="seconds"), "tz": self.tz, "weather": self._weather(),
               "departures": self._departures(), "ha": self._ha(), "presenter": {"slot": True},
               "night": self.night}
        if self.screen is not None:  # écrans du mur allumés : le champ existe (null si rien n'est publié)
            out["screen"] = self._screen()
        if self.demo is not None:  # foyer démo (configuration) : fictif, marqué `demo: true`
            out["household"] = self.demo.view(now)
        return out


_kiosk: Kiosk | None = None


def configure(k: Kiosk | None) -> None:
    global _kiosk
    _kiosk = k


def service() -> Kiosk | None:
    return _kiosk


def handle(h: Any, method: str, *, lokkal: bool, via_relay: bool, owner: bool) -> None:
    """La route du kiosque ; l'ordre des refus est voulu (rien ne dit à un appel du cloud qu'un kiosque existe)."""
    k = _kiosk
    if not lokkal or k is None:
        h._json(404, {"error": {"code": "not_found", "message": "not found"}})
        return
    if via_relay:
        h._json(403, {"error": {"code": "kiosk_on_machine", "message": "kiosque : sur la machine seulement"}})
        return
    if not owner:
        h._json(401, {"error": {"message": "jeton local requis"}})
        return
    if method != "GET":
        h._json(405, {"error": {"code": "method", "message": "lecture seule"}})
        return
    if not k.read_allowed():
        h._json(429, {"error": {"code": "kiosk_rate", "message": "trop de lectures : réessayez plus tard"}})
        return
    h._json(200, k.view())


__all__ = ["PATH", "READS_PER_MIN", "Kiosk", "configure", "handle", "service"]
