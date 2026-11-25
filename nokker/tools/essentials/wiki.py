"""Résumé d'un article Wikipédia — API REST de Wikimedia (`/api/rest_v1/page/summary/{titre}`).

Texte sous CC BY-SA 4.0 : réutilisable, y compris commercialement, avec attribution (lien vers l'article) et mention de
la licence ; le résultat porte les deux. Politique User-Agent de Wikimedia : un User-Agent qui donne un contact
(`Nokker/1 (+https://nokker.ai)`) ; limite des robots identifiés : 200 requêtes par minute — on en garde 60.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote

from nokker.tools.essentials.common import EssentialsError, NotFound, RateLimit, Source, TTLCache

LANGS = ("fr", "de", "it", "en")
URL = "https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}?redirect=true"
MAX_CHARS = 2_000


@dataclass
class Wiki:
    source: Source = field(default_factory=lambda: Source("Wikipédia", RateLimit(per_minute=60)))
    cache: TTLCache = field(default_factory=lambda: TTLCache(ttl_s=86_400, max_items=512))

    def summary(self, title: str, lang: str) -> tuple[str, str]:
        """(texte, ligne de source)."""
        lang = lang if lang in LANGS else "fr"
        url = URL.format(lang=lang, title=quote(title.strip().replace(" ", "_"), safe=""))
        cached = self.cache.get(url)
        if cached is not None:
            return cached
        try:
            data = self.source.get_json(url, max_bytes=500_000)
        except NotFound:
            raise EssentialsError(f"pas d'article « {title} » sur Wikipédia ({lang}). Vérifie le titre exact, ou "
                                  "cherche avec web_search.", "introuvable") from None
        page_title = data.get("title") or title
        extract = (data.get("extract") or "").strip()
        if len(extract) > MAX_CHARS:
            extract = extract[: MAX_CHARS - 1] + "…"
        # Lien reconstruit par le noyau (il sort du cadre de données) : jamais une URL fournie par la réponse.
        page = f"https://{lang}.wikipedia.org/wiki/{quote(str(page_title).replace(' ', '_'), safe='')}"
        desc = (data.get("description") or "").strip()
        kind = data.get("type")
        stamp = str(data.get("timestamp") or "").strip()
        lines = [f"{page_title}" + (f" — {desc}" if desc else ""),
                 f"Version de l'article du {stamp} (UTC)" if stamp else "Version de l'article : date inconnue"]
        if kind == "disambiguation":
            lines.append("(page d'homonymie : précise le titre)")
        lines.append(extract or "(pas de résumé)")
        text = "\n".join(lines)
        source = f"Source : Wikipédia ({lang}), {page} — texte sous licence CC BY-SA 4.0."
        self.cache.set(url, (text, source))
        return text, source


__all__ = ["LANGS", "Wiki"]
