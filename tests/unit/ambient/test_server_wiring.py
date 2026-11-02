"""Câblage dans le vrai serveur du noyau : drapeau éteint = aucune route (404, comme la base) et aucun outil ni texte
vu par le modèle ; allumé (Lokkal) = routes du propriétaire et des appareils."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from nokker.ambient import http as ambient_http
from nokker.ambient.raster import png_info
from nokker.config import NokkerConfig


@pytest.fixture(autouse=True)
def _reset():
    yield
    ambient_http.configure(None)


def _call(port, path, *, token=None, body=None, method=None):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=h,
                                 method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _server(tmp_path):
    from nokker.api.server import make_handler
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.local.api import LOCAL_OWNER
    from tests.unit.api.test_approvals_routes import make_kernel_with_approvals
    from tests.unit.test_local_api import _api

    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    api = _api(tmp_path)
    api.state_dir = tmp_path / "state"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: LOCAL_OWNER}, approvals=approvals,
                                                               local_api=api))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port, api, kernel


def test_flag_off_by_default_no_route_no_tool(tmp_path):
    from nokker.api.server import configure_ambient

    cfg = NokkerConfig()
    assert cfg.ambient == {}
    kernel = SimpleNamespace(invariants=SimpleNamespace(scrub_output=lambda s: s))
    configure_ambient(cfg, kernel)
    assert not ambient_http.enabled()
    httpd, port, api, _ = _server(tmp_path)
    try:
        for path in ("/v1/ambient/board", "/v1/ambient/displays", "/v1/eink/trmnl/api/display",
                     "/v1/eink/nkd_" + "a" * 43 + ".png"):
            assert _call(port, path, token=api.token)[0] == 404
    finally:
        httpd.shutdown()


def test_ambient_package_exposes_nothing_to_the_model():
    """Ni outil, ni bloc de contexte, ni prompt : ce lot n'ajoute AUCUN texte vu par le modèle."""
    import pathlib

    import nokker.ambient as pkg

    for f in pathlib.Path(pkg.__file__).parent.glob("*.py"):
        src = f.read_text(encoding="utf-8")
        assert "ToolSpec" not in src and "ToolSource" not in src and "prompts/" not in src, f.name


def test_flag_on_lokkal_routes_end_to_end(tmp_path, monkeypatch):
    from nokker.api.server import configure_ambient

    monkeypatch.delenv("NOKKER_HOME_BRIDGE_URL", raising=False)
    monkeypatch.delenv("NOKKER_PG_DSN", raising=False)
    cfg = NokkerConfig(ambient={"eink": {"enabled": True}})
    kernel = SimpleNamespace(invariants=SimpleNamespace(scrub_output=lambda s: s))
    configure_ambient(cfg, kernel)
    assert ambient_http.enabled() and ambient_http.service().room_audio is None  # voix : pas de passerelle
    httpd, port, api, _ = _server(tmp_path)
    try:
        assert _call(port, "/v1/ambient/displays", body={"model": "trmnl"})[0] == 401  # pas le propriétaire
        st, raw = _call(port, "/v1/ambient/displays", token=api.token, body={"name": "Entrée", "model": "trmnl_4g"})
        assert st == 201
        token = json.loads(raw)["token"]
        assert _call(port, "/v1/ambient/board", token=api.token, body={"courses": ["Lait"]}, method="PUT")[0] == 200
        st, png = _call(port, f"/v1/eink/{token}.png")
        assert st == 200 and png_info(png)[:3] == (800, 480, 2)
        did = json.loads(_call(port, "/v1/ambient/displays", token=api.token)[1])["displays"][0]["id"]
        assert _call(port, f"/v1/ambient/displays/{did}", token=api.token, method="DELETE")[0] == 200
        assert _call(port, f"/v1/eink/{token}.png")[0] == 401
    finally:
        httpd.shutdown()
