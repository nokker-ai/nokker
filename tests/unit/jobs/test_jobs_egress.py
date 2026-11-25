"""Pack Emploi — sortie par proxy HTTP explicite (`jobs_pack.proxy`, bêta : nœud Tailscale egress-proxy:1055)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from nokker.jobs import build_client, build_jobs, proxy_of
from nokker.jobs.client import PartnerError, httpx_transport
from nokker.jobs.store import MemoryJobsStore
from tests.unit.jobs.fake_partner import AUDIENCE, PRIVATE_PEM

BASE = "http://100.64.0.10:8131"   # adresse tailnet (CGNAT) de la passerelle du service


def _block(**kw: Any) -> dict[str, Any]:
    return {"enabled": True, "base_url": BASE, "private_key": "secret:JOBS_KEY", "audience": AUDIENCE, **kw}


class _Proxy:
    """Proxy HTTP de test : relève la ligne de requête (forme absolue attendue) et répond comme la passerelle du service emploi
    sans compte (401 au format du contrat)."""

    def __init__(self) -> None:
        seen: list[dict[str, Any]] = []

        class H(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                seen.append({"target": self.path, "auth": self.headers.get("Authorization", ""),
                             "xff": self.headers.get("X-Forwarded-For")})
                body = json.dumps({"error": {"code": "unauthorized", "message": "x"}}).encode()
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a: Any) -> None:
                pass

        self.seen = seen
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def proxy():
    p = _Proxy()
    yield p
    p.close()


@pytest.mark.parametrize("raw,want", [
    ("http://egress-proxy:1055", "http://egress-proxy:1055"),
    ("  http://egress-proxy:1055/ ", "http://egress-proxy:1055"),
    ("http://10.0.0.1", "http://10.0.0.1"),
    ("", None),
    (None, None),
])
def test_proxy_of_accepts_plain_http(raw, want):
    assert proxy_of({"proxy": raw}) == want


@pytest.mark.parametrize("raw", [
    "https://egress-proxy:1055", "socks5://egress-proxy:1055", "egress-proxy:1055", "http://u:p@egress-proxy:1055",
    "http://egress-proxy:1055/x", "http://egress-proxy:99999", "http://:1055", "http://h:1055?q=1",
])
def test_proxy_of_rejects_anything_else(raw):
    with pytest.raises(ValueError):
        proxy_of({"proxy": raw})


def test_invalid_proxy_builds_no_client_rather_than_going_direct():
    assert build_client(_block(proxy="https://egress-proxy:1055"), resolve=lambda r: PRIVATE_PEM) is None
    assert build_jobs(_block(proxy="socks5://x:1"), store=MemoryJobsStore(), account_of=lambda t: None,
                      resolve=lambda r: PRIVATE_PEM) is None


def test_requests_go_through_the_proxy_to_the_tailnet_gateway(proxy, monkeypatch):
    # Variables d'environnement hostiles : ni un autre proxy, ni NO_PROXY ne doivent compter.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "*")
    client = build_client(_block(proxy=proxy.url), resolve=lambda r: PRIVATE_PEM)
    assert client is not None and client.base_url == BASE
    with pytest.raises(PartnerError) as e:
        client.request("GET", "/cv", account="acc-1")
    assert e.value.code == "unauthorized" and e.value.status == 401
    [hit] = proxy.seen
    assert hit["target"] == f"{BASE}/api/v1/partner/cv"        # forme absolue : le proxy joint la passerelle tailnet
    assert hit["auth"].startswith("Bearer ") and hit["xff"] is None


def test_injected_transport_still_wins_over_proxy():
    calls: list[str] = []

    def fake(method, url, headers, body):
        calls.append(url)
        return 200, {}, b'{"ok": true}'

    client = build_client(_block(proxy="http://egress-proxy:1055"), resolve=lambda r: PRIVATE_PEM, transport=fake)
    assert client.request("GET", "/cv").data == {"ok": True}
    assert calls == [f"{BASE}/api/v1/partner/cv"]


def test_without_proxy_the_transport_is_unchanged(monkeypatch):
    import httpx

    seen: dict[str, Any] = {}

    def capture(method, url, **kw):
        seen.update(kw)
        return httpx.Response(200, content=b"{}")

    monkeypatch.setattr(httpx, "request", capture)
    httpx_transport()("GET", "http://x/", {}, None)
    assert "proxy" not in seen and "trust_env" not in seen
    seen.clear()
    httpx_transport(proxy="http://egress-proxy:1055")("GET", "http://x/", {}, None)
    assert seen["proxy"] == "http://egress-proxy:1055" and seen["trust_env"] is False
