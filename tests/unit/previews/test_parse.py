"""`nokker/previews/parse.py` — OpenGraph/Twitter/favicon/title/lang, id oEmbed depuis l'URL,
`kind`, extrait Trafilatura. Tout sur des fixtures HTML en mémoire — aucun réseau."""
from __future__ import annotations

from nokker.previews.parse import (
    embed_from_url,
    extract_excerpt,
    extract_meta,
    guess_kind,
    parse_oembed_json,
)

OG_HTML = """<!doctype html>
<html lang="fr-CH">
<head>
<meta property="og:title" content="Kubernetes Probes &amp; Health">
<meta property="og:description" content="Documentation des sondes Kubernetes.">
<meta property="og:site_name" content="Kubernetes">
<meta property="og:type" content="article">
<meta property="og:image" content="/img/probes.png">
<link rel="icon" href="/favicon.ico">
<title>Ignoré car og:title présent</title>
</head>
<body>texte</body>
</html>"""

TWITTER_ONLY_HTML = """<!doctype html>
<html>
<head>
<meta name="twitter:title" content="Titre Twitter">
<meta name="twitter:description" content="Description Twitter">
<meta name="twitter:image" content="https://cdn.example.com/x.jpg">
<link rel="shortcut icon" href="https://example.com/icon.png">
</head>
<body></body>
</html>"""

TITLE_ONLY_HTML = """<!doctype html>
<html><head><title>Juste un titre &amp; rien d'autre</title></head><body></body></html>"""


def test_extract_meta_prefers_opengraph_and_resolves_relative_urls():
    meta = extract_meta(OG_HTML, "https://kubernetes.io/docs/probes/")
    assert meta.title == "Kubernetes Probes & Health"
    assert meta.description == "Documentation des sondes Kubernetes."
    assert meta.site_name == "Kubernetes"
    assert meta.og_type == "article"
    assert meta.image == "https://kubernetes.io/img/probes.png"
    assert meta.favicon == "https://kubernetes.io/favicon.ico"
    assert meta.lang == "fr"  # "fr-CH" -> sous-tag principal


def test_extract_meta_falls_back_to_twitter_card():
    meta = extract_meta(TWITTER_ONLY_HTML, "https://example.com/page")
    assert meta.title == "Titre Twitter"
    assert meta.description == "Description Twitter"
    assert meta.image == "https://cdn.example.com/x.jpg"
    assert meta.favicon == "https://example.com/icon.png"


def test_extract_meta_falls_back_to_title_tag_and_unescapes_entities():
    meta = extract_meta(TITLE_ONLY_HTML, "https://example.com/")
    assert meta.title == "Juste un titre & rien d'autre"
    assert meta.image is None
    assert meta.favicon is None
    assert meta.lang is None


def test_extract_meta_empty_html_gives_empty_fields():
    meta = extract_meta("<html></html>", "https://example.com/")
    assert meta.title == ""
    assert meta.image is None
    assert meta.favicon is None


# ─────────────────────────────── embed (YouTube/Vimeo/X) ───────────────────────────────


def test_embed_youtube_watch_url():
    embed = embed_from_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30s")
    assert embed.provider == "youtube"
    assert embed.id == "dQw4w9WgXcQ"
    assert "youtube.com/oembed" in embed.oembed_url


def test_embed_youtube_short_url():
    embed = embed_from_url("https://youtu.be/dQw4w9WgXcQ")
    assert embed.provider == "youtube"
    assert embed.id == "dQw4w9WgXcQ"


def test_embed_youtube_shorts_url():
    embed = embed_from_url("https://www.youtube.com/shorts/abc123XYZ")
    assert embed.provider == "youtube"
    assert embed.id == "abc123XYZ"


def test_embed_vimeo_url():
    embed = embed_from_url("https://vimeo.com/76979871")
    assert embed.provider == "vimeo"
    assert embed.id == "76979871"


def test_embed_x_status_url():
    embed = embed_from_url("https://x.com/someuser/status/1234567890123456789")
    assert embed.provider == "x"
    assert embed.id == "1234567890123456789"


def test_embed_twitter_domain_alias():
    embed = embed_from_url("https://twitter.com/someuser/status/42")
    assert embed.provider == "x"
    assert embed.id == "42"


def test_embed_none_for_unrelated_url():
    assert embed_from_url("https://kubernetes.io/docs/probes/") is None


def test_embed_none_for_youtube_channel_page():
    assert embed_from_url("https://www.youtube.com/c/somechannel") is None


def test_parse_oembed_json_extracts_known_fields():
    raw = {"title": "Rick Astley - Never Gonna Give You Up", "thumbnail_url": "https://i.ytimg.com/x.jpg",
          "provider_name": "YouTube", "html": "<iframe ...>"}
    out = parse_oembed_json(raw)
    assert out == {"title": "Rick Astley - Never Gonna Give You Up", "image": "https://i.ytimg.com/x.jpg",
                   "site_name": "YouTube"}


# ─────────────────────────────── kind ───────────────────────────────


def test_guess_kind_video_from_embed():
    embed = embed_from_url("https://youtu.be/dQw4w9WgXcQ")
    assert guess_kind("https://youtu.be/dQw4w9WgXcQ", content_type="text/html", og_type="", embed=embed,
                      has_excerpt=False) == "video"


def test_guess_kind_repo_from_github_host():
    assert guess_kind("https://github.com/nokker-ai/nokker", content_type="text/html", og_type="", embed=None,
                      has_excerpt=False) == "repo"


def test_guess_kind_repo_requires_org_and_repo_segments():
    # une page github sans org/repo (ex. la page d'accueil) n'est pas un "repo"
    assert guess_kind("https://github.com/", content_type="text/html", og_type="", embed=None,
                      has_excerpt=False) != "repo"


def test_guess_kind_doc_from_pdf_content_type():
    assert guess_kind("https://example.com/rapport", content_type="application/pdf; charset=binary", og_type="",
                      embed=None, has_excerpt=False) == "doc"


def test_guess_kind_doc_from_pdf_extension():
    assert guess_kind("https://example.com/rapport.pdf", content_type="", og_type="", embed=None,
                      has_excerpt=False) == "doc"


def test_guess_kind_article_from_og_type():
    assert guess_kind("https://example.com/post", content_type="text/html", og_type="article", embed=None,
                      has_excerpt=False) == "article"


def test_guess_kind_article_from_long_excerpt():
    assert guess_kind("https://example.com/post", content_type="text/html", og_type="", embed=None,
                      has_excerpt=True) == "article"


def test_guess_kind_other_as_fallback():
    assert guess_kind("https://example.com/", content_type="text/html", og_type="", embed=None,
                      has_excerpt=False) == "other"


# ─────────────────────────────── extrait Trafilatura ───────────────────────────────

ARTICLE_HTML = """<!doctype html>
<html><head><title>Un article</title></head>
<body>
<article>
<h1>Un article de test</h1>
<p>""" + ("Ceci est une phrase de contenu suffisamment longue pour que Trafilatura la retienne comme texte "
          "principal de la page plutôt que comme un fragment de navigation sans intérêt. ") * 20 + """</p>
</article>
</body></html>"""


def test_extract_excerpt_truncates_to_max_chars_without_cutting_a_word():
    excerpt = extract_excerpt(ARTICLE_HTML, "https://example.com/article", max_chars=200)
    assert excerpt
    assert len(excerpt) <= 201  # 200 + le caractère "…" ajouté
    assert not excerpt.rstrip("…").endswith((" ",))
    assert "…" in excerpt


def test_extract_excerpt_empty_for_trivial_html():
    assert extract_excerpt("<html><body></body></html>", "https://example.com/") == ""
