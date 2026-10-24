"""nokker/previews/parse.py — OpenGraph/Twitter cards, favicon, extrait Trafilatura, `kind`, oEmbed.

Parsing par expressions régulières (pas de `beautifulsoup`/`lxml` ajoutés ICI : `trafilatura` les
amène déjà comme dépendance transitive pour l'extrait, mais les balises `<meta>`/`<link>`/`<title>`
sont assez régulières pour un regex — pas besoin d'un arbre DOM complet pour ça, et ça évite une
dépendance de plus juste pour 20 lignes)."""
from __future__ import annotations

import html as html_module
import logging
import re
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlsplit

logger = logging.getLogger("nokker.previews.parse")

_META_TAG_RE = re.compile(r"<meta\b([^>]*?)/?>", re.IGNORECASE | re.DOTALL)
_LINK_TAG_RE = re.compile(r"<link\b([^>]*?)/?>", re.IGNORECASE | re.DOTALL)
_TITLE_TAG_RE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_HTML_LANG_RE = re.compile(r"<html\b[^>]*\blang\s*=\s*[\"']([a-zA-Z][a-zA-Z-]*)[\"']", re.IGNORECASE)
_ATTR_RE = re.compile(r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


def _parse_attrs(attr_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _ATTR_RE.finditer(attr_text):
        key = m.group(1).lower()
        val = m.group(2) if m.group(2) is not None else (m.group(3) or "")
        out[key] = html_module.unescape(val).strip()
    return out


@dataclass
class MetaInfo:
    title: str = ""
    description: str = ""
    site_name: str = ""
    og_type: str = ""
    image: str | None = None
    favicon: str | None = None
    lang: str | None = None


def extract_meta(body_text: str, base_url: str) -> MetaInfo:
    """OpenGraph d'abord, Twitter card en repli, `<title>`/`<html lang>` en dernier recours. Les
    URLs d'image/favicon sont résolues en ABSOLU contre *base_url* (final_url après redirections) —
    une page qui écrit `<link rel="icon" href="/favicon.png">` ne donne sinon rien d'utilisable."""
    og: dict[str, str] = {}
    twitter: dict[str, str] = {}
    for m in _META_TAG_RE.finditer(body_text):
        attrs = _parse_attrs(m.group(1))
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        content = attrs.get("content", "")
        if key.startswith("og:") and content:
            og.setdefault(key[3:], content)
        elif key.startswith("twitter:") and content:
            twitter.setdefault(key[8:], content)

    icons: list[tuple[str, str]] = []  # (rel, href), ordre d'apparition
    for m in _LINK_TAG_RE.finditer(body_text):
        attrs = _parse_attrs(m.group(1))
        rel = (attrs.get("rel") or "").lower()
        href = attrs.get("href") or ""
        if href and "icon" in rel:
            icons.append((rel, href))

    def pick_icon() -> str | None:
        for wanted in ("icon", "shortcut icon"):
            for rel, href in icons:
                if rel == wanted:
                    return href
        return icons[0][1] if icons else None

    title = og.get("title") or twitter.get("title") or ""
    if not title:
        m = _TITLE_TAG_RE.search(body_text)
        if m:
            title = html_module.unescape(re.sub(r"\s+", " ", m.group(1))).strip()

    description = og.get("description") or twitter.get("description") or ""
    image = og.get("image") or twitter.get("image")
    favicon = pick_icon()
    lang_m = _HTML_LANG_RE.search(body_text)
    lang = lang_m.group(1).split("-")[0].lower() if lang_m else (og.get("locale", "").split("_")[0].lower() or None)

    return MetaInfo(
        title=title[:300],
        description=description[:600],
        site_name=(og.get("site_name") or "")[:150],
        og_type=og.get("type") or "",
        image=urljoin(base_url, image) if image else None,
        favicon=urljoin(base_url, favicon) if favicon else None,
        lang=lang or None,
    )


# ─────────────────────────────── oEmbed : provider + id depuis l'URL ───────────────────────────────


@dataclass(frozen=True)
class EmbedRef:
    provider: str
    id: str
    oembed_url: str  # endpoint oEmbed à interroger (gardé par le même fetch SSRF que le reste)


def _youtube_id(parts) -> str | None:
    host = parts.netloc.lower().removeprefix("www.").removeprefix("m.")
    if host == "youtu.be":
        seg = parts.path.strip("/").split("/")[0]
        return seg or None
    if host == "youtube.com":
        path = parts.path.rstrip("/")
        if path in ("/watch",):
            from urllib.parse import parse_qs

            vals = parse_qs(parts.query).get("v")
            return vals[0] if vals else None
        for prefix in ("/embed/", "/shorts/", "/live/"):
            if path.startswith(prefix):
                return path[len(prefix):].split("/")[0] or None
    return None


def _vimeo_id(parts) -> str | None:
    host = parts.netloc.lower().removeprefix("www.")
    if not (host == "vimeo.com" or host.endswith(".vimeo.com")):
        return None
    segs = [s for s in parts.path.split("/") if s]
    for seg in reversed(segs):
        if seg.isdigit():
            return seg
    return None


def _x_id(parts) -> str | None:
    host = parts.netloc.lower().removeprefix("www.")
    if host not in ("twitter.com", "x.com"):
        return None
    segs = [s for s in parts.path.split("/") if s]
    if len(segs) >= 3 and segs[1] == "status" and segs[2].isdigit():
        return segs[2]
    return None


def embed_from_url(url: str) -> EmbedRef | None:
    """`None` si *url* n'est pas une vidéo/un tweet reconnu — `kind` retombe alors sur le reste
    (OpenGraph/Trafilatura). Pas de dépendance au réseau ICI : seulement de la forme de l'URL ;
    l'appel oEmbed réel (titre/vignette) se fait séparément, gardé comme le reste (`fetch.py`)."""
    parts = urlsplit(url)
    encoded = quote(url, safe="")
    vid = _youtube_id(parts)
    if vid:
        return EmbedRef(provider="youtube", id=vid,
                        oembed_url=f"https://www.youtube.com/oembed?format=json&url={encoded}")
    vid = _vimeo_id(parts)
    if vid:
        return EmbedRef(provider="vimeo", id=vid, oembed_url=f"https://vimeo.com/api/oembed.json?url={encoded}")
    vid = _x_id(parts)
    if vid:
        return EmbedRef(provider="x", id=vid, oembed_url=f"https://publish.twitter.com/oembed?url={encoded}")
    return None


def parse_oembed_json(raw: dict) -> dict:
    """Extrait les quelques champs oEmbed utiles (titre/vignette) — forme libre selon le
    fournisseur (`docs/BUBBLES.md` ne fixe que `embed.provider`/`embed.id`, le reste enrichit
    `title`/`image` s'ils manquaient)."""
    return {
        "title": raw.get("title") or "",
        "image": raw.get("thumbnail_url") or None,
        "site_name": raw.get("provider_name") or "",
    }


# ─────────────────────────────── kind ───────────────────────────────

_REPO_HOSTS = ("github.com", "gitlab.com", "codeberg.org")


def guess_kind(url: str, *, content_type: str, og_type: str, embed: EmbedRef | None, has_excerpt: bool) -> str:
    if embed is not None:
        return "video"
    parts = urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.")
    path_segs = [s for s in parts.path.split("/") if s]
    if host in _REPO_HOSTS and len(path_segs) >= 2:
        return "repo"
    ct = content_type.split(";")[0].strip().lower()
    if ct == "application/pdf" or parts.path.lower().endswith((".pdf", ".docx", ".odt")):
        return "doc"
    if og_type in ("article", "book") or (ct in ("text/html", "application/xhtml+xml") and has_excerpt):
        return "article"
    return "other"


# ─────────────────────────────── extrait Trafilatura ───────────────────────────────


def extract_excerpt(body_text: str, url: str, *, max_chars: int = 1200) -> str:
    """`≤ max_chars`, coupé à la dernière frontière de mot pour ne pas trancher au milieu —
    `trafilatura` est optionnel à l'exécution (absent -> extrait vide, jamais une exception qui
    casse l'aperçu entier) même s'il est déclaré en dépendance ferme du projet."""
    try:
        import trafilatura
    except ImportError:  # pragma: no cover — dépendance déclarée, ce repli ne joue qu'en dev dégradé
        logger.warning("trafilatura absent : extrait vide")
        return ""
    try:
        text = trafilatura.extract(body_text, url=url, include_comments=False, include_tables=False,
                                   favor_precision=True)
    except Exception:
        logger.exception("trafilatura.extract a levé une exception")
        return ""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_space = truncated.rfind(" ")
    if last_space > max_chars * 0.6:
        truncated = truncated[:last_space]
    return truncated.rstrip() + "…"


__all__ = [
    "EmbedRef", "MetaInfo", "embed_from_url", "extract_excerpt", "extract_meta", "guess_kind", "parse_oembed_json",
]
