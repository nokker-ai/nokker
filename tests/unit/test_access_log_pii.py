"""S2 (backlog bêta 15.11) : le journal d'accès HTTP ne porte ni l'IP cliente, ni la query string (tn_/th_)."""
from __future__ import annotations

import http.client
import logging
import threading
from http.server import ThreadingHTTPServer

import pytest

from nokker.api.server import make_handler
from tests.unit.api.test_server import make_kernel

IP = "203.0.113.7"


@pytest.fixture()
def port():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), {}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _get(port, path, ip=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path, headers={"CF-Connecting-IP": ip} if ip else {})
    r = c.getresponse()
    r.read()
    c.close()
    return r.status


def _lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "nokker.api.server"]


def test_query_et_ip_absentes(port, caplog):
    caplog.set_level(logging.DEBUG)
    _get(port, "/v1/mirror/turns?since=tn_abc123&x=th_def", ip=IP)
    lines = _lines(caplog)
    assert lines
    blob = "\n".join(r.getMessage() + str(r.args) for r in caplog.records)
    for secret in (IP, "tn_abc123", "th_def", "since", "x=", "127.0.0.1"):
        assert secret not in blob, secret
    assert any("/v1/mirror/turns?…" in ln for ln in lines)


def test_hache_stable_et_distinct(port, caplog):
    caplog.set_level(logging.DEBUG)
    _get(port, "/healthz", ip=IP)
    _get(port, "/healthz", ip=IP)
    _get(port, "/healthz", ip="198.51.100.9")
    a, b, c = (ln.split(" - ")[0] for ln in _lines(caplog)[:3])
    assert a == b != c


def test_healthz_intact(port, caplog):
    caplog.set_level(logging.DEBUG)
    assert _get(port, "/healthz") == 200
    assert "/healthz" in _lines(caplog)[0] and "?…" not in _lines(caplog)[0]
