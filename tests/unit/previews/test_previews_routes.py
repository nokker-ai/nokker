"""`nokker/api/previews_routes.py` — dispatch/auth/forme des réponses, avec un handler HTTP factice
(duck-typing minimal de `BaseHTTPRequestHandler` : `_resolve_identity`/`_json`/`send_response`/
`send_header`/`end_headers`/`wfile`) — pas de vrai socket ici (voir `tests/unit/api/test_server.py`
pour la version en boucle locale qui exerce le VRAI `do_GET` de `server.py`)."""
from __future__ import annotations

import io

import httpx

from nokker.api.previews_routes import PreviewsRoutes, register_previews_routes
from nokker.config import NokkerConfig
from nokker.previews.service import PreviewService


class FakeHandler:
    def __init__(self, sender="alice"):
        self._sender = sender
        self.json_calls: list[tuple[int, dict]] = []
        self.sent_status: int | None = None
        self.sent_headers: dict[str, str] = {}
        self.wfile = io.BytesIO()

    def _resolve_identity(self, body=None):
        return self._sender

    def _json(self, status, obj):
        self.json_calls.append((status, obj))

    def send_response(self, status):
        self.sent_status = status

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass


def _routes(tmp_path, handler_fn) -> PreviewsRoutes:
    def factory(hop, timeout_s):
        return httpx.Client(transport=httpx.MockTransport(handler_fn), timeout=timeout_s, follow_redirects=False)

    service = PreviewService(cache_dir=str(tmp_path), client_factory=factory)
    return PreviewsRoutes(service=service)


# ─────────────────────────────── routage ───────────────────────────────


def test_matches_only_previews_paths():
    routes = _routes("/tmp", lambda r: httpx.Response(200))
    assert routes.matches("/v1/previews")
    assert routes.matches("/v1/previews/img/" + "a" * 64)
    assert not routes.matches("/v1/threads")
    assert not routes.matches("/healthz")


def test_handle_get_returns_false_for_unrelated_path(tmp_path):
    routes = _routes(tmp_path, lambda r: httpx.Response(200))
    fake = FakeHandler()
    assert routes.handle_get(fake, "/v1/approvals") is False
    assert fake.json_calls == []  # n'a touché à rien


# ─────────────────────────────── auth ───────────────────────────────


def test_unauthorized_without_resolved_identity(tmp_path):
    routes = _routes(tmp_path, lambda r: httpx.Response(200))
    fake = FakeHandler(sender=None)
    handled = routes.handle_get(fake, "/v1/previews?url=http://8.8.8.8/")
    assert handled is True
    assert fake.json_calls == [(401, {"error": {"message": "unauthorized"}})]


# ─────────────────────────────── GET /v1/previews ───────────────────────────────


def test_missing_url_param_is_400(tmp_path):
    routes = _routes(tmp_path, lambda r: httpx.Response(200))
    fake = FakeHandler()
    routes.handle_get(fake, "/v1/previews")
    status, body = fake.json_calls[0]
    assert status == 400
    assert "url" in body["error"]["message"]


def test_ssrf_blocked_url_is_400(tmp_path):
    routes = _routes(tmp_path, lambda r: (_ for _ in ()).throw(AssertionError("jamais appelé")))
    fake = FakeHandler()
    routes.handle_get(fake, "/v1/previews?url=http://127.0.0.1/")
    status, body = fake.json_calls[0]
    assert status == 400
    assert "loopback" in body["error"]["message"]


def test_successful_preview_is_200_with_canonical_shape(tmp_path):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<title>Salut</title>")

    routes = _routes(tmp_path, handler)
    fake = FakeHandler()
    routes.handle_get(fake, "/v1/previews?url=http://8.8.8.8/page")
    status, body = fake.json_calls[0]
    assert status == 200
    assert body["title"] == "Salut"
    assert set(body) >= {"url", "final_url", "title", "site_name", "description", "image", "favicon",
                         "excerpt", "lang", "kind", "embed", "fetched_at"}


# ─────────────────────────────── GET /v1/previews/img/{sha} ───────────────────────────────


def test_image_route_serves_cached_bytes_with_security_headers(tmp_path):
    routes = _routes(tmp_path, lambda r: httpx.Response(200))
    sha = routes.service._images.put(b"\x89PNGdata", "image/png")
    fake = FakeHandler()
    handled = routes.handle_get(fake, f"/v1/previews/img/{sha}")
    assert handled is True
    assert fake.sent_status == 200
    assert fake.sent_headers["Content-Type"] == "image/png"
    assert fake.sent_headers["Content-Length"] == str(len(b"\x89PNGdata"))
    assert fake.sent_headers["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in fake.sent_headers["Content-Security-Policy"]
    assert "max-age" in fake.sent_headers["Cache-Control"]
    assert fake.wfile.getvalue() == b"\x89PNGdata"


def test_image_route_404_for_unknown_sha(tmp_path):
    routes = _routes(tmp_path, lambda r: httpx.Response(200))
    fake = FakeHandler()
    routes.handle_get(fake, f"/v1/previews/img/{'0' * 64}")
    assert fake.json_calls == [(404, {"error": {"message": "not found"}})]


def test_image_route_404_for_malformed_sha_path_traversal(tmp_path):
    routes = _routes(tmp_path, lambda r: httpx.Response(200))
    fake = FakeHandler()
    routes.handle_get(fake, "/v1/previews/img/../../../etc/passwd")
    status, _ = fake.json_calls[0]
    assert status == 404


def test_image_route_requires_auth_too(tmp_path):
    routes = _routes(tmp_path, lambda r: httpx.Response(200))
    sha = routes.service._images.put(b"data", "image/png")
    fake = FakeHandler(sender=None)
    routes.handle_get(fake, f"/v1/previews/img/{sha}")
    assert fake.json_calls == [(401, {"error": {"message": "unauthorized"}})]


# ─────────────── images signées (docs/BUBBLES.md §9.3) : <img> ne pose pas d'Authorization ───────────────


def test_image_route_served_with_valid_signature_and_no_identity(tmp_path):
    routes = _routes(tmp_path, lambda r: httpx.Response(200))
    sha = routes.service._images.put(b"donnees", "image/png")
    exp, sig = routes.service.signed_image_url(sha).split("?", 1)[1].split("&")
    exp, sig = exp.split("=", 1)[1], sig.split("=", 1)[1]
    fake = FakeHandler(sender=None)  # aucune identité résolue
    handled = routes.handle_get(fake, f"/v1/previews/img/{sha}?exp={exp}&sig={sig}")
    assert handled is True
    assert fake.sent_status == 200
    assert fake.wfile.getvalue() == b"donnees"
    assert fake.json_calls == []  # jamais le 401 : la signature a suffi


def test_image_route_401_without_identity_when_signature_invalid(tmp_path):
    routes = _routes(tmp_path, lambda r: httpx.Response(200))
    sha = routes.service._images.put(b"donnees", "image/png")
    fake = FakeHandler(sender=None)
    routes.handle_get(fake, f"/v1/previews/img/{sha}?exp=9999999999&sig=0" + "0" * 31)
    assert fake.json_calls == [(401, {"error": {"message": "unauthorized"}})]


def test_image_route_401_with_expired_signature_and_no_identity(tmp_path):
    from nokker.previews.cache import sign_image

    routes = _routes(tmp_path, lambda r: httpx.Response(200))
    sha = routes.service._images.put(b"donnees", "image/png")
    exp, sig = sign_image(sha, routes.service._sign_secret, now=1_000.0, ttl_s=10.0)  # déjà expirée
    fake = FakeHandler(sender=None)
    routes.handle_get(fake, f"/v1/previews/img/{sha}?exp={exp}&sig={sig}")
    assert fake.json_calls == [(401, {"error": {"message": "unauthorized"}})]


def test_image_route_signature_still_falls_back_to_identity_when_present(tmp_path):
    """Une signature absente/invalide ne bloque pas un appel par ailleurs authentifié — même
    comportement qu'avant ce chantier."""
    routes = _routes(tmp_path, lambda r: httpx.Response(200))
    sha = routes.service._images.put(b"donnees", "image/png")
    fake = FakeHandler(sender="alice")
    handled = routes.handle_get(fake, f"/v1/previews/img/{sha}")
    assert handled is True and fake.sent_status == 200


# ─────────────────────────────── register_previews_routes ───────────────────────────────


def test_register_returns_none_when_disabled(tmp_path):
    config = NokkerConfig(previews_enabled=False, previews_cache_dir=str(tmp_path))
    assert register_previews_routes(config) is None


def test_register_returns_routes_when_enabled(tmp_path):
    config = NokkerConfig(previews_enabled=True, previews_cache_dir=str(tmp_path))
    routes = register_previews_routes(config)
    assert isinstance(routes, PreviewsRoutes)


def test_routes_and_bubble_producer_share_one_signing_service(tmp_path, monkeypatch):
    """Canari 18.10 : deux `PreviewService` (routes / producteur) = deux clés aléatoires → 401 sur l'image."""
    import nokker.previews as previews_pkg
    from nokker.api.previews_routes import register_previews_routes
    from nokker.api.server import build_hub
    from nokker.config import NokkerConfig

    monkeypatch.setattr(previews_pkg, "_default_service", None)
    cfg = NokkerConfig(previews_cache_dir=str(tmp_path))
    hub = build_hub(cfg)
    routes = register_previews_routes(cfg)
    assert routes is not None and routes.service is previews_pkg.get_default_service()
    assert hub.producers.request_previews is not None
