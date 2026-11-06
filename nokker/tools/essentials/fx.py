"""Taux de change — taux de référence quotidiens de la BCE (euro foreign exchange reference rates).

Un seul fichier (`eurofxref-daily.xml`, ~1,5 Ko) publié vers 16:00 CET chaque jour ouvré TARGET ; gardé 1 h. Les taux
croisés (CHF→USD…) passent par l'euro. La BCE les publie « à titre d'information seulement » et déconseille de les
utiliser pour des transactions : le texte rendu le dit.

La Banque nationale suisse (data.snb.ch) est écartée : ses conditions limitent la réutilisation aux fins non
commerciales (docs/ESSENTIALS.md).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from nokker.tools.essentials.common import EssentialsError, RateLimit, Source, TTLCache, num

URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
SOURCE_LINE = ("Source : Banque centrale européenne, taux de référence (à titre d'information seulement, pas des taux "
               "de transaction).")

_DAY = re.compile(r"<Cube\s+time=['\"](\d{4}-\d{2}-\d{2})['\"]")
_RATE = re.compile(r"<Cube\s+currency=['\"]([A-Z]{3})['\"]\s+rate=['\"]([0-9.]+)['\"]")
_CODE = re.compile(r"^[A-Za-z]{3}$")


@dataclass
class Fx:
    source: Source = field(default_factory=lambda: Source("BCE", RateLimit(per_minute=10)))
    cache: TTLCache = field(default_factory=lambda: TTLCache(ttl_s=3_600, max_items=2))

    def table(self) -> tuple[str, dict[str, float]]:
        cached = self.cache.get("daily")
        if cached is not None:
            return cached
        text = self.source.get(URL, max_bytes=200_000, accept="application/xml,text/xml").decode("utf-8", "replace")
        day = _DAY.search(text)
        rates = {c: float(r) for c, r in _RATE.findall(text)}
        if not day or not rates:
            raise EssentialsError("fichier des taux de la BCE illisible.", "source_erreur")
        rates["EUR"] = 1.0
        value = (day.group(1), rates)
        self.cache.set("daily", value)
        return value

    def convert(self, from_ccy: str, to_ccy: str, amount: float) -> str:
        for code in (from_ccy, to_ccy):
            if not _CODE.match(code):
                raise EssentialsError(f"code de devise invalide : « {code} » (code ISO à 3 lettres, ex. CHF).",
                                      "argument_invalide")
        a, b = from_ccy.upper(), to_ccy.upper()
        day, rates = self.table()
        missing = [c for c in (a, b) if c not in rates]
        if missing:
            raise EssentialsError(f"devise non publiée par la BCE : {', '.join(missing)}. Devises disponibles : "
                                  f"{', '.join(sorted(rates))}.", "introuvable")
        rate = rates[b] / rates[a]
        # Aucun arrondi caché : taux publiés tels quels, taux croisé et montant en pleine précision, arrondi affiché à
        # part et nommé comme tel.
        published = " ; ".join(f"1 EUR = {num(rates[c])} {c}" for c in sorted({a, b}) if c != "EUR") or "EUR"
        converted = amount * rate
        return (f"Taux de référence BCE du {day} (date du taux, publié vers 16:00 CET, "
                f"UTC+01:00) : {published} (valeurs publiées).\n"
                f"Taux {a}→{b} = {num(rates[b])} / {num(rates[a])} = {num(rate)} {b} par {a}.\n"
                f"{num(amount)} {a} = {num(converted)} {b} (arrondi au centime : {converted:.2f} {b}).")


__all__ = ["SOURCE_LINE", "Fx"]
