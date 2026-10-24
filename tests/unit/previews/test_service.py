"""`nokker/previews/service.py` — bout en bout, AUCUN réseau : `client_factory` remplace le
transport httpx par `httpx.MockTransport`, `resolver` remplace `socket.getaddrinfo` par une table
en mémoire (hôtes nommés -> IP publique de test) — la VRAIE garde SSRF reste exercée, juste sans
DNS/sockets réels."""
from __future__ import annotations

import socket

import httpx
import pytest

from nokker.previews.service import PreviewError, PreviewService

_HOSTS = {
    "page.example.com": "93.184.216.34",
    "cdn.example.com": "93.184.216.35",
    "www.youtube.com": "93.184.216.36",
    "youtu.be": "93.184.216.36",
    "interne.example.com": "100.64.12.34",  # Tailscale : doit rester bloqué même ici
}


def _resolver(host, port, proto=socket.IPPROTO_TCP):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_HOSTS[host], port))]


OG_PAGE = b"""<!doctype html>
<html lang="fr"><head>
<meta property="og:title" content="Un super article">
<meta property="og:description" content="Une description correcte.">
<meta property="og:site_name" content="Example">
<meta property="og:type" content="article">
<meta property="og:image" content="https://cdn.example.com/cover.jpg">
<link rel="icon" href="https://cdn.example.com/favicon.png">
</head><body><article><p>""" + (b"Contenu suffisant pour Trafilatura. " * 15) + b"""</p></article></body></html>"""


def _service(tmp_path, handler, *, resolver=_resolver, **kwargs):
    def factory(hop, timeout_s):
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout_s, follow_redirects=False)

    return PreviewService(cache_dir=str(tmp_path), client_factory=factory, resolver=resolver, **kwargs)


def test_preview_full_pipeline_with_image_and_favicon(tmp_path):
    def handler(request):
        url = str(request.url)
        if url == "http://page.example.com/article":
            return httpx.Response(200, headers={"content-type": "text/html"}, content=OG_PAGE)
        if url in ("https://cdn.example.com/cover.jpg", "https://cdn.example.com/favicon.png"):
            return httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG-octets")
        raise AssertionError(f"URL inattendue : {url}")

    svc = _service(tmp_path, handler)
    result = svc.preview("http://page.example.com/article")

    assert result["title"] == "Un super article"
    assert result["site_name"] == "Example"
    assert result["kind"] == "article"
    assert result["lang"] == "fr"
    assert result["embed"] is None
    assert result["image"].startswith("/v1/previews/img/")
    assert result["favicon"].startswith("/v1/previews/img/")
    assert result["excerpt"]
    assert len(result["excerpt"]) <= 1201
    assert result["fetched_at"]

    # L'image est signée (docs/BUBBLES.md §9.3) : /v1/previews/img/<sha>?exp=<epoch>&sig=<hmac>.
    assert "?exp=" in result["image"] and "&sig=" in result["image"]
    sha = result["image"].split("?", 1)[0].rsplit("/", 1)[-1]
    cached = svc.image(sha)
    assert cached is not None
    assert cached == (b"\x89PNG-octets", "image/png")


def test_preview_is_cached_and_second_call_does_not_refetch(tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<title>T</title>")

    svc = _service(tmp_path, handler)
    first = svc.preview("http://page.example.com/x")
    second = svc.preview("http://page.example.com/x")
    assert first == second
    assert calls["n"] == 1  # 2e appel servi par le cache mémoire (24h), aucune nouvelle requête


def test_preview_blocked_by_ssrf_raises_preview_error_400(tmp_path):
    def handler(request):
        raise AssertionError("ne doit jamais être appelé : bloqué avant tout envoi")

    svc = _service(tmp_path, handler)
    with pytest.raises(PreviewError) as exc_info:
        svc.preview("http://interne.example.com/secret")
    assert exc_info.value.status == 400


def test_preview_network_failure_raises_preview_error_502(tmp_path):
    def handler(request):
        raise httpx.ConnectError("refusé")

    svc = _service(tmp_path, handler)
    with pytest.raises(PreviewError) as exc_info:
        svc.preview("http://page.example.com/down")
    assert exc_info.value.status == 502


def test_preview_video_embed_merges_oembed_title_and_thumbnail(tmp_path):
    def handler(request):
        url = str(request.url)
        if url.startswith("http://youtu.be/"):
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<title>brut</title>")
        if "youtube.com/oembed" in url:
            return httpx.Response(200, headers={"content-type": "application/json"},
                                  content=b'{"title": "Titre oEmbed", "thumbnail_url": '
                                          b'"https://cdn.example.com/thumb.jpg", "provider_name": "YouTube"}')
        if url == "https://cdn.example.com/thumb.jpg":
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"jpeg-bytes")
        raise AssertionError(f"URL inattendue : {url}")

    svc = _service(tmp_path, handler)
    result = svc.preview("http://youtu.be/dQw4w9WgXcQ")

    assert result["kind"] == "video"
    assert result["embed"] == {"provider": "youtube", "id": "dQw4w9WgXcQ"}
    assert result["title"] == "Titre oEmbed"  # l'oEmbed l'emporte sur le <title> brut de la page
    assert result["site_name"] == "YouTube"
    assert result["image"] is not None


def test_image_fetch_failure_does_not_fail_the_whole_preview(tmp_path):
    def handler(request):
        url = str(request.url)
        if url == "http://page.example.com/article":
            return httpx.Response(200, headers={"content-type": "text/html"}, content=OG_PAGE)
        return httpx.Response(500)  # og:image et favicon échouent tous les deux

    svc = _service(tmp_path, handler)
    result = svc.preview("http://page.example.com/article")
    assert result["title"] == "Un super article"
    assert result["image"] is None
    assert result["favicon"] is None


def test_image_with_wrong_content_type_is_not_cached(tmp_path):
    def handler(request):
        url = str(request.url)
        if url == "http://page.example.com/article":
            return httpx.Response(200, headers={"content-type": "text/html"}, content=OG_PAGE)
        # le serveur ment : og:image pointe en réalité vers du HTML
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>pas une image</html>")

    svc = _service(tmp_path, handler)
    result = svc.preview("http://page.example.com/article")
    assert result["image"] is None
    assert result["favicon"] is None


def test_preview_rejects_empty_url(tmp_path):
    svc = _service(tmp_path, lambda request: httpx.Response(200))
    with pytest.raises(PreviewError) as exc_info:
        svc.preview("   ")
    assert exc_info.value.status == 400


# ─────────────────────────────── preview_many ───────────────────────────────


def test_preview_many_runs_concurrently_and_preserves_order(tmp_path):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"},
                              content=f"<title>{request.url}</title>".encode())

    svc = _service(tmp_path, handler)
    urls = ["http://page.example.com/a", "http://cdn.example.com/b", "http://page.example.com/c"]
    results = svc.preview_many(urls, limit=5)
    assert [r["url"] for r in results] == urls
    assert all("error" not in r for r in results)


def test_preview_many_respects_limit():
    svc = PreviewService(cache_dir="/tmp/unused-nokker-preview-test")
    assert svc.preview_many([], limit=5) == []


def test_preview_many_one_bad_url_does_not_break_the_batch(tmp_path):
    def handler(request):
        if "interne.example.com" in str(request.url):
            raise AssertionError("ne doit jamais être appelé")
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<title>ok</title>")

    svc = _service(tmp_path, handler)
    urls = ["http://page.example.com/ok", "http://interne.example.com/blocked"]
    results = svc.preview_many(urls, limit=5)
    by_url = {r["url"]: r for r in results}
    assert "error" not in by_url["http://page.example.com/ok"]
    assert "error" in by_url["http://interne.example.com/blocked"]


# ─────────────────────── images signées (docs/BUBBLES.md §9.3) ───────────────────────


def test_signed_image_url_is_verifiable_by_the_same_service(tmp_path):
    svc = PreviewService(cache_dir=str(tmp_path))
    url = svc.signed_image_url("a" * 64)
    assert url.startswith("/v1/previews/img/" + "a" * 64 + "?exp=")
    sha, query = url.split("/v1/previews/img/")[1].split("?", 1)
    params = dict(p.split("=", 1) for p in query.split("&"))
    assert sha == "a" * 64
    assert svc.verify_image_sig("a" * 64, params["exp"], params["sig"])
    assert not svc.verify_image_sig("a" * 64, params["exp"], "0" * len(params["sig"]))


def test_explicit_sign_secret_is_shared_across_instances(tmp_path):
    # même secret explicite (`previews_sign_secret_env`, config) -> une signature posée par une
    # instance se vérifie par une AUTRE instance (nécessaire : signature et vérification ne sont
    # pas forcément la même instance de process à process, contrairement au cas par défaut ci-dessous).
    svc1 = PreviewService(cache_dir=str(tmp_path), sign_secret="meme-secret")
    svc2 = PreviewService(cache_dir=str(tmp_path), sign_secret="meme-secret")
    url = svc1.signed_image_url("b" * 64)
    sha, query = url.split("/v1/previews/img/")[1].split("?", 1)
    params = dict(p.split("=", 1) for p in query.split("&"))
    assert svc2.verify_image_sig(sha, params["exp"], params["sig"])


def test_two_services_without_explicit_secret_do_not_cross_verify(tmp_path):
    svc1 = PreviewService(cache_dir=str(tmp_path))
    svc2 = PreviewService(cache_dir=str(tmp_path))
    url = svc1.signed_image_url("c" * 64)
    sha, query = url.split("/v1/previews/img/")[1].split("?", 1)
    params = dict(p.split("=", 1) for p in query.split("&"))
    assert not svc2.verify_image_sig(sha, params["exp"], params["sig"])  # secrets générés différents
