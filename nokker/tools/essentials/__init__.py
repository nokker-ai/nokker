"""Pack « Essentiels », vague 1 (docs/ESSENTIALS.md) : outils sans compte utilisateur, actifs pour tous les
locataires quand le drapeau `essentials` est allumé. Éteint (défaut) : aucune source construite, contexte du modèle
inchangé à l'octet.

Sources externes : MétéoSuisse (OGD), transport.opendata.ch, BCE, Wikipédia, SLF. Calcul local : jours fériés suisses,
conversions, calculs, dates.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.tools.essentials import fx as fx_mod
from nokker.tools.essentials import (
    air,
    charging,
    common,
    geo_fr,
    holidays_ch,
    hydro,
    local,
    meteo,
    mobility,
    school,
    slf,
    transit,
    vigilance,
)
from nokker.tools.essentials.places_ch import resolve_ch
from nokker.tools.essentials.common import EssentialsError, arg, fail, ok
from nokker.tools.essentials.common import local as local_result
from nokker.tools.essentials.texts import NAMES, TOOLS
from nokker.tools.essentials.texts_v2 import NAMES_V2, TOOLS_V2, TRANSIT_FALLBACK_SUFFIX
from nokker.tools.essentials.wiki import Wiki

logger = logging.getLogger("nokker.tools.essentials")


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _float(value: Any) -> float | None:
    try:
        return float(str(value).replace("'", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


@dataclass
class EssentialsSource:
    """`ToolSource` des outils Essentiels. Chaque service garde son cache et sa limite de débit (tout le processus)."""

    meteo: meteo.MeteoSwiss = field(default_factory=meteo.MeteoSwiss)
    transit: transit.Transit = field(default_factory=transit.Transit)
    fx: fx_mod.Fx = field(default_factory=fx_mod.Fx)
    wiki: Wiki = field(default_factory=Wiki)
    avalanche: slf.Avalanche = field(default_factory=slf.Avalanche)
    # Vague 2 (`essentials_v2`, docs/ESSENTIALS.md §Vague 2) : None = vague 1 seule, textes et résultats inchangés.
    v2: V2 | None = None

    def _texts(self) -> dict[str, tuple[str, dict[str, Any]]]:
        texts = dict(TOOLS)
        if self.v2 is None:
            return texts
        if self.transit.fallback is not None:
            for n in ("transit_departures", "transit_trip"):
                texts[n] = (texts[n][0] + TRANSIT_FALLBACK_SUFFIX, texts[n][1])
        texts.update({n: TOOLS_V2[n] for n in self.v2.names()})
        return texts

    def specs(self) -> list[ToolSpec]:
        texts = self._texts()
        return [ToolSpec(name=n, description=d, parameters=p, effect=Effect.READ, source="builtin")
                for n, (d, p) in texts.items()]

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        handler = getattr(self, f"_{name}", None)
        if name not in self._texts() or handler is None:
            return fail(name, f"outil inconnu : {name}", "introuvable")
        args = arguments if isinstance(arguments, dict) else {}
        try:
            return handler(args)
        except EssentialsError as exc:
            return ToolResult(call_id="", name=name, content=exc.render(), is_error=True)
        except Exception:  # noqa: BLE001 — format inattendu d'une source : jamais une trace au modèle
            logger.warning("outil essentiel %s en échec", name, exc_info=True)
            return fail(name, "la source a renvoyé une réponse inattendue ; réessaie plus tard.", "source_erreur")

    # --- sources externes ---
    def _weather_ch(self, a: dict[str, Any]) -> ToolResult:
        place = arg(a, "place")
        if not place:
            return fail("weather_ch", "lieu manquant (paramètre `place`).")
        text = self.meteo.forecast(place, meteo.parse_days(a.get("days")))
        return ok("weather_ch", meteo.SOURCE_LINE, text)

    def _pollen(self, a: dict[str, Any]) -> ToolResult:
        place = arg(a, "place")
        if not place:
            return fail("pollen", "lieu manquant (paramètre `place`).")
        return ok("pollen", meteo.SOURCE_LINE, self.meteo.pollen(place))

    def _transit_departures(self, a: dict[str, Any]) -> ToolResult:
        station = arg(a, "station")
        if not station:
            return fail("transit_departures", "arrêt manquant (paramètre `station`).")
        text, source = self.transit.departures_with_source(station, _int(a.get("limit"), 8, 1, 15), arg(a, "date", 10),
                                                           arg(a, "time", 5))
        return ok("transit_departures", source, text)

    def _transit_trip(self, a: dict[str, Any]) -> ToolResult:
        origin, dest = arg(a, "from"), arg(a, "to")
        if not origin or not dest:
            return fail("transit_trip", "départ et arrivée requis (paramètres `from` et `to`).")
        arrival = a.get("arrival") in (True, "true", "1", 1)
        text, source = self.transit.trip_with_source(origin, dest, arg(a, "date", 10), arg(a, "time", 5), arrival,
                                                     _int(a.get("limit"), 4, 1, 6))
        return ok("transit_trip", source, text)

    def _fx_rate(self, a: dict[str, Any]) -> ToolResult:
        amount = _float(a.get("amount")) if a.get("amount") not in (None, "") else 1.0
        if amount is None:
            return fail("fx_rate", "montant illisible (paramètre `amount`).")
        text = self.fx.convert(arg(a, "from", 3), arg(a, "to", 3), amount)
        return ok("fx_rate", fx_mod.SOURCE_LINE, text)

    def _wiki_summary(self, a: dict[str, Any]) -> ToolResult:
        title = arg(a, "title", 200)
        if not title:
            return fail("wiki_summary", "titre manquant (paramètre `title`).")
        text, source = self.wiki.summary(title, arg(a, "lang", 5).lower() or "fr")
        return ok("wiki_summary", source, text)

    def _avalanche_bulletin(self, a: dict[str, Any]) -> ToolResult:
        text = self.avalanche.render(arg(a, "region"), arg(a, "lang", 5).lower() or "fr")
        return ok("avalanche_bulletin", slf.SOURCE_LINE, text)

    # --- vague 2 ---
    def _place(self, a: dict[str, Any], name: str) -> str:
        place = arg(a, "place")
        if not place:
            raise EssentialsError("lieu manquant (paramètre `place`).", "argument_invalide")
        return place

    def _spot_ch(self, place: str) -> Any:
        spot = resolve_ch(place, self.meteo, self.transit)
        if spot is None:
            raise EssentialsError(f"lieu « {place} » introuvable en Suisse (localité, NPA ou arrêt). Cet outil ne "
                                  "couvre que la Suisse ; ailleurs, web_search.", "hors_couverture")
        return spot

    def _commune_fr(self, place: str) -> Any:
        """Commune française ; si le même nom désigne AUSSI une localité suisse (Sion, Wil…), `ambigu :` avec les
        deux pays — jamais un choix silencieux d'un côté de la frontière."""
        commune = self.v2.places.resolve(place)
        if commune is None:
            return None
        swiss = None if any(ch.isdigit() for ch in place) else self.meteo.exact_point(place)
        if swiss is not None:
            raise common.Ambiguous("lieu", place.strip(), [f"{commune.label}, France", f"{swiss.label}, Suisse"],
                                   "Redemande avec le code postal français, ou l'outil suisse pour la Suisse.")
        return commune

    def _weather_alerts_fr(self, a: dict[str, Any]) -> ToolResult:
        place = self._place(a, "weather_alerts_fr")
        commune = self._commune_fr(place)
        if commune is None or len(commune.department) > 2:
            raise EssentialsError(f"commune « {place} » introuvable en France métropolitaine. Cet outil ne couvre que "
                                  "la France métropolitaine ; pour la Suisse, weather_ch ; ailleurs, web_search.",
                                  "hors_couverture")
        return ok("weather_alerts_fr", vigilance.SOURCE_LINE, self.v2.vigilance.render(commune))

    def _mobility_nearby(self, a: dict[str, Any]) -> ToolResult:
        kind = arg(a, "kind", 20).lower() or "tout"
        if kind not in ("velo", "trottinette", "voiture", "tout"):
            return fail("mobility_nearby", "type inconnu (velo, trottinette, voiture ou tout).")
        spot = self._spot_ch(self._place(a, "mobility_nearby"))
        return ok("mobility_nearby", mobility.SOURCE_LINE,
                  self.v2.mobility.render(spot, kind, _int(a.get("radius_m"), 500, 50, 2000)))

    def _charging_nearby(self, a: dict[str, Any]) -> ToolResult:
        floor = _float(a.get("min_power_kw")) if a.get("min_power_kw") not in (None, "") else 0.0
        if floor is None or floor < 0:
            return fail("charging_nearby", "puissance illisible (paramètre `min_power_kw`, en kW).")
        spot = self._spot_ch(self._place(a, "charging_nearby"))
        return ok("charging_nearby", charging.SOURCE_LINE,
                  self.v2.charging.render(spot, floor, _int(a.get("radius_m"), 500, 50, 2000)))

    def _water_status(self, a: dict[str, Any]) -> ToolResult:
        place = self._place(a, "water_status")
        return ok("water_status", hydro.SOURCE_LINE, self.v2.water.render(place, self.meteo, self.transit))

    def _air_quality_fr(self, a: dict[str, Any]) -> ToolResult:
        place = self._place(a, "air_quality_fr")
        self._commune_fr(place)  # ambiguïté France/Suisse avant tout appel à Atmo
        text, source = self.v2.air.render(place, self.meteo, self.transit, self.v2.places)
        return ok("air_quality_fr", source, text)

    def _school_holidays(self, a: dict[str, Any]) -> ToolResult:
        region = arg(a, "region", 60)
        if not region:
            return fail("school_holidays", "région manquante (paramètre `region`).")
        text, source = self.v2.school.render(region, arg(a, "year", 12))
        return ok("school_holidays", source, text)

    # --- calcul local ---
    def _holidays(self, a: dict[str, Any]) -> ToolResult:
        year = _int(a.get("year"), datetime.now(ZoneInfo("Europe/Zurich")).year, 1900, 2200)
        raw = arg(a, "canton", 40)
        canton = holidays_ch.canton_code(raw) if raw else None
        if raw and canton is None:
            return fail("holidays", f"canton inconnu : « {raw} » (code à 2 lettres, ex. VD, ou nom du canton).",
                        "introuvable")
        return local_result("holidays", holidays_ch.render(year, canton))

    def _unit_convert(self, a: dict[str, Any]) -> ToolResult:
        value = _float(a.get("value"))
        if value is None:
            return fail("unit_convert", "valeur illisible (paramètre `value`).")
        return local_result("unit_convert", local.convert(value, arg(a, "from_unit", 30), arg(a, "to_unit", 30)))

    def _math_calc(self, a: dict[str, Any]) -> ToolResult:
        return local_result("math_calc", local.calc(str(a.get("expression") or "")))

    def _date_calc(self, a: dict[str, Any]) -> ToolResult:
        days = a.get("days")
        offset = None if days in (None, "") else _int(days, 0, -365_000, 365_000)
        return local_result("date_calc", local.date_calc(arg(a, "start", 30), arg(a, "end", 30), offset))


@dataclass
class V2:
    """Services de la vague 2. `charging` None = outil absent (accord de l'OFEN manquant)."""

    places: geo_fr.FrenchPlaces = field(default_factory=geo_fr.FrenchPlaces)
    vigilance: vigilance.Vigilance = field(default_factory=vigilance.Vigilance)
    mobility: mobility.Mobility = field(default_factory=mobility.Mobility)
    charging: charging.Charging | None = None
    water: hydro.Water = field(default_factory=hydro.Water)
    air: air.Air = field(default_factory=air.Air)
    school: school.SchoolHolidays | None = None

    def __post_init__(self) -> None:
        if self.school is None:
            self.school = school.SchoolHolidays(places=self.places)

    def names(self) -> list[str]:
        return [n for n in NAMES_V2 if n != "charging_nearby" or self.charging is not None]


def build_essentials(settings: dict[str, Any] | None, resolve_secret: Any = None) -> EssentialsSource:
    """Source du pack selon `essentials_v2` : vide ou `enabled` faux = vague 1 seule, à l'identique."""
    cfg = settings or {}
    if not cfg.get("enabled"):
        return EssentialsSource()
    contact = str(cfg.get("contact_email") or "").strip()
    v2 = V2(mobility=mobility.Mobility(source=common.Source(
                "sharedmobility.ch", common.RateLimit(per_minute=30), headers={"From": contact} if contact else {})),
            charging=charging.Charging() if cfg.get("charging_permitted") is True else None)
    src = EssentialsSource(v2=v2)
    src.meteo.france_tool = True
    ref = str(cfg.get("navitia_token") or "")
    token = ""
    if ref.startswith("secret:") and resolve_secret is not None:
        try:
            token = str(resolve_secret(ref) or "")
        except Exception:  # noqa: BLE001 — coffre scellé ou nom absent : pas de repli, journalisé sans valeur
            token = ""
    if token:
        from nokker.tools.essentials.navitia import NavitiaFrance

        src.transit.fallback = NavitiaFrance(token=token)
    elif ref:
        logger.warning("essentials_v2 : jeton %s illisible — transit sans repli SNCF", ref)
    return src


__all__ = ["EssentialsSource", "NAMES", "V2", "build_essentials"]
