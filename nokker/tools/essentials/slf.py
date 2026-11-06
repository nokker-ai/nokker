"""Bulletin d'avalanches du SLF (WSL-Institut pour l'étude de la neige et des avalanches), API CAAML JSON
`https://aws.slf.ch/api/bulletin/caaml/{lang}/json`. Données gratuites sous CC BY 4.0, mention visible de la source
(lien vers www.slf.ch pour un produit numérique) ; le SLF demande que le contenu, avertissement de sécurité, soit
toujours exact et à jour : cache court (15 min), date de validité et prochaine mise à jour toujours rendues.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from nokker.tools.essentials.common import RateLimit, Source, TTLCache, fold, plural

URL = "https://aws.slf.ch/api/bulletin/caaml/{lang}/json"
LANGS = ("fr", "de", "it", "en")
SOURCE_LINE = "Source : SLF, bulletin d'avalanches (www.slf.ch), CC BY 4.0. En montagne, le bulletin complet fait foi."

DANGER_FR = {"low": "1 faible", "moderate": "2 limité", "considerable": "3 marqué", "high": "4 fort",
             "very_high": "5 très fort", "no_snow": "pas de neige", "no_rating": "pas de degré"}
PROBLEM_FR = {"new_snow": "neige fraîche", "wind_slab": "neige soufflée", "persistent_weak_layers":
              "neige ancienne (couches fragiles persistantes)", "wet_snow": "neige mouillée", "gliding_snow":
              "avalanches de glissement", "favourable_situation": "situation favorable", "no_distinct_problem":
              "pas de problème marqué"}
_TAG = re.compile(r"<[^>]+>")


def _plain(html: str | None, limit: int = 600) -> str:
    text = " ".join(_TAG.sub(" ", html or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _elevation(e: dict[str, Any] | None) -> str:
    if not e:
        return ""
    lo, hi = e.get("lowerBound"), e.get("upperBound")
    if lo and hi:
        return f" entre {lo} et {hi} m"
    if lo:
        return f" au-dessus de {lo} m"
    if hi:
        return f" au-dessous de {hi} m"
    return ""


@dataclass
class Avalanche:
    source: Source = field(default_factory=lambda: Source("SLF", RateLimit(per_minute=20)))
    cache: TTLCache = field(default_factory=lambda: TTLCache(ttl_s=900, max_items=8))

    def bulletins(self, lang: str) -> list[dict[str, Any]]:
        lang = lang if lang in LANGS else "fr"
        cached = self.cache.get(lang)
        if cached is not None:
            return cached
        data = self.source.get_json(URL.format(lang=lang), max_bytes=3_000_000)
        out = [b for b in (data.get("bulletins") or []) if isinstance(b, dict)]
        self.cache.set(lang, out)
        return out

    def render(self, region: str, lang: str) -> str:
        bulletins = self.bulletins(lang)
        if not bulletins:
            return ("Aucun bulletin d'avalanches en cours de validité (hors saison, le SLF n'en publie pas, ou entre "
                    "deux publications).")
        if region:
            q = fold(region)
            picked = [b for b in bulletins
                      if any(q in fold(r.get("name") or "") for r in b.get("regions") or [])]
            if not picked:
                return (f"Aucune région du bulletin ne correspond à « {region} ». Donne une région ou une vallée "
                        "suisse (ex. « Conches », « Davos »), ou laisse vide pour la vue d'ensemble.")
        else:
            picked = bulletins
        first = picked[0]
        vt = first.get("validTime") or {}
        lines = [f"Bulletin d'avalanches SLF publié le {first.get('publicationTime', '?')}, valable du "
                 f"{vt.get('startTime', '?')} au {vt.get('endTime', '?')} (UTC) ; prochaine mise à jour "
                 f"{first.get('nextUpdate', '?')}."]
        for b in picked[:8 if not region else 3]:
            names = [r.get("name") or "?" for r in b.get("regions") or []]
            if region:
                q = fold(region)
                shown = [n for n in names if q in fold(n)] or names[:5]
            else:
                shown = names[:5]
            rest = len(names) - len(shown)
            more = f" et {plural(rest, 'autre région', 'autres régions')}" if rest > 0 else ""
            ratings = ", ".join(
                f"{DANGER_FR.get(r.get('mainValue'), r.get('mainValue') or '?')}{_elevation(r.get('elevation'))}"
                + (f" ({r.get('validTimePeriod')})" if r.get("validTimePeriod") not in (None, "all_day") else "")
                for r in b.get("dangerRatings") or []) or "?"
            lines.append(f"- {', '.join(shown)}{more} : danger {ratings}")
            for p in (b.get("avalancheProblems") or [])[:3]:
                aspects = ", ".join(p.get("aspects") or [])
                lines.append(f"  problème : {PROBLEM_FR.get(p.get('problemType'), p.get('problemType') or '?')}"
                             f"{_elevation(p.get('elevation'))}" + (f", expositions {aspects}" if aspects else ""))
            if region:
                for p in (b.get("avalancheProblems") or [])[:1]:
                    if p.get("comment"):
                        lines.append(f"  {_plain(p.get('comment'))}")
        if not region and len(bulletins) > 8:
            lines.append(f"(… {plural(len(bulletins) - 8, 'autre zone', 'autres zones')} : précise une région)")
        return "\n".join(lines)


__all__ = ["Avalanche", "SOURCE_LINE"]
