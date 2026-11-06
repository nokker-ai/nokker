"""Vigilance météorologique de Météo-France par département (France métropolitaine) — `weather_alerts_fr`.

Source sans clé : fichiers de la vigilance publiés par Météo-France sur data.gouv.fr (jeu « Vigilance météorologique »,
Licence Ouverte 2.0), `https://files.data.gouv.fr/meteofrance/data/vigilance/metropole/AAAA/MM/JJ/HHMMSS/
CDP_CARTE_EXTERNE.json`, mis à jour à chaque nouvelle carte (au moins deux fois par jour). Le dossier du jour est listé
(index HTML de files.data.gouv.fr), le plus récent qui porte une carte est lu.

Les prévisions locales chiffrées de Météo-France ne sont publiées qu'en grilles de modèles (AROME/ARPEGE, GRIB2) ou par
le portail API avec un jeton : hors de cette vague (docs/ESSENTIALS.md).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from nokker.tools.essentials.common import EssentialsError, NotFound, RateLimit, Source, TTLCache
from nokker.tools.essentials.geo_fr import Commune

BASE = "https://files.data.gouv.fr/meteofrance/data/vigilance/metropole"
SOURCE_LINE = ("Source : Météo-France, vigilance météorologique (data.gouv.fr, Licence Ouverte 2.0) ; communes : "
               "geo.api.gouv.fr.")

PHENOMENA = {"1": "vent violent", "2": "pluie-inondation", "3": "orages", "4": "crues", "5": "neige-verglas",
             "6": "canicule", "7": "grand froid", "8": "avalanches", "9": "vagues-submersion"}
COLORS = {1: "vert", 2: "jaune", 3: "orange", 4: "rouge"}
_DIR = re.compile(r'href="[^"]*?/?(\d{6})/"')


@dataclass
class Vigilance:
    source: Source = field(default_factory=lambda: Source("Météo-France (data.gouv.fr)", RateLimit(per_minute=20)))
    cache: TTLCache = field(default_factory=lambda: TTLCache(ttl_s=900, max_items=4))
    clock_utc: Any = None

    def _now(self) -> datetime:
        return self.clock_utc() if self.clock_utc else datetime.now(timezone.utc)

    def card(self) -> dict[str, Any]:
        cached = self.cache.get("card")
        if cached is not None:
            return cached
        today = self._now().date()
        for day in (today, today - timedelta(days=1)):
            base = f"{BASE}/{day:%Y/%m/%d}"
            try:
                index = self.source.get(base + "/", max_bytes=500_000, accept="text/html").decode("utf-8", "replace")
            except NotFound:
                continue
            for folder in sorted(set(_DIR.findall(index)), reverse=True)[:6]:
                try:
                    data = self.source.get_json(f"{base}/{folder}/CDP_CARTE_EXTERNE.json", max_bytes=3_000_000)
                except NotFound:
                    continue  # dossier qui ne porte que les textes
                if isinstance(data, dict) and isinstance(data.get("product"), dict):
                    self.cache.set("card", data)
                    return data
        raise EssentialsError("aucune carte de vigilance Météo-France publiée ces deux derniers jours.",
                              "source_erreur")

    def render(self, commune: Commune) -> str:
        product = self.card()["product"]
        dep = commune.department
        lines = [f"Vigilance météorologique Météo-France pour {commune.name} ({commune.department_name}, département "
                 f"{dep}), carte du {product.get('update_time', '?')} :"]
        for period in product.get("periods") or []:
            label = {"J": "aujourd'hui", "J1": "demain"}.get(period.get("echeance"), period.get("echeance") or "?")
            items = next((d for d in ((period.get("timelaps") or {}).get("domain_ids") or [])
                          if str(d.get("domain_id")) == dep), None)
            head = (f"- {label} (du {period.get('begin_validity_time', '?')} au "
                    f"{period.get('end_validity_time', '?')}) : ")
            if items is None:
                lines.append(head + "département absent de la carte.")
                continue
            level = COLORS.get(int(items.get("max_color_id") or 0), "inconnu")
            parts = []
            for ph in items.get("phenomenon_items") or []:
                color = int(ph.get("phenomenon_max_color_id") or 0)
                if color >= 2:
                    spans = ", ".join(f"{t.get('begin_time')} → {t.get('end_time')}"
                                      for t in ph.get("timelaps_items") or [] if int(t.get("color_id") or 0) >= 2)
                    parts.append(f"{PHENOMENA.get(str(ph.get('phenomenon_id')), 'phénomène ' + str(ph.get('phenomenon_id')))}"
                                 f" {COLORS.get(color, '?')}" + (f" ({spans})" if spans else ""))
            lines.append(head + f"niveau maximal {level}" + (f" ; {' ; '.join(parts)}" if parts else
                                                              " ; aucun phénomène au-dessus du vert"))
        lines.append("Échelle de vigilance : vert, jaune, orange, rouge (pas de prévision chiffrée dans cet outil).")
        return "\n".join(lines)


__all__ = ["COLORS", "PHENOMENA", "SOURCE_LINE", "Vigilance"]
