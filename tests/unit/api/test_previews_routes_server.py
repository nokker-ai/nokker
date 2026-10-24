"""`nokker/api/server.py` — intégration du SEUL point d'accroche ajouté pour les aperçus
(`do_GET` : `if previews is not None and previews.handle_get(self, self.path): return`). Boucle
locale réelle (`127.0.0.1`, port éphémère, même esprit que `test_server.py`), mais le service
d'aperçus lui-même n'a AUCUN accès réseau (`client_factory=httpx.MockTransport`) : on exerce le
VRAI `do_GET`/`make_handler`, pas une simulation, sans jamais toucher un site tiers réel.

Réutilise le `Kernel` de test de `test_server.py` (mêmes fakes) plutôt que d'en construire un
deuxième jeu — ce test ne porte QUE sur le câblage `previews=` de `make_handler`/`serve`, pas sur
le reste du contrat `/v1/chat/completions` déjà couvert là-bas."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import ThreadingHTTPServer

import httpx

from nokker.api.previews_routes import PreviewsRoutes
from nokker.api.server import make_handler
from nokker.previews.service import PreviewService
from tests.unit.api.test_server import make_kernel


@dataclass
class _Server:
    httpd: ThreadingHTTPServer
    thread: threading.Thread
    port: int

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=5)


def _start(tmp_path, api_keys, handler_fn, *, previews_enabled=True) -> _Server:
    previews = None
    if previews_enabled:
        def factory(hop, timeout_s):
            return httpx.Client(transport=httpx.MockTransport(handler_fn), timeout=timeout_s,
                                follow_redirects=False)

        previews = PreviewsRoutes(service=PreviewService(cache_dir=str(tmp_path), client_factory=factory))
    handler = make_handler(make_kernel(), api_keys, previews=previews)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return _Server(httpd=httpd, thread=thread, port=httpd.server_address[1])


def test_previews_route_reachable_through_real_do_get(tmp_path):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<title>Coucou</title>")

    srv = _start(tmp_path, {"secret-key": "alice"}, handler)
    try:
        req = urllib.request.Request(srv.url("/v1/previews?url=http://8.8.8.8/x"),
                                     headers={"Authorization": "Bearer secret-key"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            body = json.loads(resp.read())
            assert body["title"] == "Coucou"
    finally:
        srv.stop()


def test_previews_route_401_without_bearer(tmp_path):
    srv = _start(tmp_path, {"secret-key": "alice"}, lambda r: httpx.Response(200))
    try:
        req = urllib.request.Request(srv.url("/v1/previews?url=http://8.8.8.8/x"))
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("devait lever HTTPError 401")
        except urllib.error.HTTPError as e:
            assert e.code == 401
    finally:
        srv.stop()


def test_existing_routes_unaffected_when_previews_disabled(tmp_path):
    """`previews=None` (chantier désactivé) : `/healthz` continue de marcher, et `/v1/previews`
    retombe sur le 404 générique existant — rien de nouveau n'apparaît."""
    srv = _start(tmp_path, {}, lambda r: httpx.Response(200), previews_enabled=False)
    try:
        with urllib.request.urlopen(srv.url("/healthz"), timeout=5) as resp:
            assert resp.status == 200

        req = urllib.request.Request(srv.url("/v1/previews?url=http://8.8.8.8/x"))
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("devait lever HTTPError 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.stop()


def test_image_route_reachable_with_security_headers(tmp_path):
    def handler(request):
        url = str(request.url)
        if url == "http://8.8.8.8/page-with-image":
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  content=b'<meta property="og:image" content="https://8.8.4.4/cover.png">')
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNGraw")

    srv = _start(tmp_path, {"secret-key": "alice"}, handler)
    try:
        req = urllib.request.Request(srv.url("/v1/previews?url=http://8.8.8.8/page-with-image"),
                                     headers={"Authorization": "Bearer secret-key"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
        image_path = body["favicon"] or body["image"]
        assert image_path, "l'OG/favicon aurait dû être caché par le mock"

        req2 = urllib.request.Request(srv.url(image_path), headers={"Authorization": "Bearer secret-key"})
        with urllib.request.urlopen(req2, timeout=5) as resp2:
            assert resp2.status == 200
            assert resp2.headers["Content-Type"] == "image/png"
            assert resp2.headers["X-Content-Type-Options"] == "nosniff"
            assert resp2.read() == b"\x89PNGraw"
    finally:
        srv.stop()
