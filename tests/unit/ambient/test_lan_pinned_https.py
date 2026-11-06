"""HTTPS de la passerelle du foyer épinglé sur l'IP vérifiée : un DNS qui change entre le contrôle de la liste blanche
et la connexion (rebinding) ne détourne pas la session. Aucun réseau : la connexion TCP et le TLS sont interceptés."""
from __future__ import annotations

import ssl
import sys
import types

import pytest

from nokker.homebridge import lan as lan_mod
from nokker.homebridge.config import Allowlist, from_dict
from nokker.homebridge.lan import Lan, LanError, PinnedHTTPSConnection


class _Stop(OSError):
    pass


class _FakeSock:
    closed = False

    def close(self) -> None:
        self.closed = True


def _rebinding_allow() -> tuple[Allowlist, list[str]]:
    """Premier tour DNS : IP privée (acceptée) ; tous les suivants : IP publique."""
    a = Allowlist.from_config([{"host": "ha.lan", "ports": [8443]}])
    answers = iter([["192.168.1.10"]])
    asked: list[str] = []

    def resolve(host: str) -> list[str]:
        asked.append(host)
        return next(answers, ["93.184.216.34"])

    a.resolve = resolve
    return a, asked


def _intercept(monkeypatch, module=lan_mod) -> dict:
    seen: dict = {}

    def create_connection(address, *a, **kw):
        seen["address"] = address
        seen["sock"] = _FakeSock()
        return seen["sock"]

    def wrap_socket(self, sock, *a, server_hostname=None, **kw):
        seen["server_hostname"] = server_hostname
        seen["verify_mode"] = self.verify_mode
        seen["check_hostname"] = self.check_hostname
        raise _Stop("arrêt du test après la poignée TLS")

    monkeypatch.setattr(module.socket, "create_connection", create_connection)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", wrap_socket)
    return seen


def test_https_connects_to_checked_ip_not_rebound_one(monkeypatch):
    allow, asked = _rebinding_allow()
    seen = _intercept(monkeypatch)
    with pytest.raises(LanError):
        Lan(allow).http("GET", "https://ha.lan:8443/api/")
    assert seen["address"] == ("192.168.1.10", 8443)  # la première IP, pas 93.184.216.34
    assert asked == ["ha.lan"]  # un seul tour DNS
    assert seen["sock"].closed


def test_https_certificate_and_sni_use_the_name(monkeypatch):
    allow, _ = _rebinding_allow()
    seen = _intercept(monkeypatch)
    with pytest.raises(LanError):
        Lan(allow).http("GET", "https://ha.lan:8443/")
    assert seen["server_hostname"] == "ha.lan"
    assert seen["verify_mode"] == ssl.CERT_REQUIRED and seen["check_hostname"] is True


def test_pinned_connection_keeps_host_for_headers():
    c = PinnedHTTPSConnection("ha.lan", "192.168.1.10", 8443, timeout=1.0)
    assert (c.host, c.port, c.pinned_ip) == ("ha.lan", 8443, "192.168.1.10")
    assert c.tls_context.verify_mode == ssl.CERT_REQUIRED and c.tls_context.check_hostname


def _watch_env(tmp_path):
    from nokker.homebridge.watch.net import WatchEnv

    cfg = from_dict({"allow": [{"host": "nas.lan", "ports": [443, 5001]}]}, token="t" * 40, base_dir=tmp_path)
    asked: list[str] = []
    cfg.allow.resolve = lambda h: (asked.append(h), ["192.168.1.10"] if len(asked) == 1 else ["93.184.216.34"])[1]
    return WatchEnv(cfg), asked


@pytest.mark.parametrize("verify", [True, False])
def test_watch_https_pinned_and_keeps_verify_option(monkeypatch, tmp_path, verify):
    from nokker.homebridge.watch.net import LanHttp, UpstreamError

    env, asked = _watch_env(tmp_path)
    seen = _intercept(monkeypatch)
    with pytest.raises((UpstreamError, OSError)):
        LanHttp(env, verify=verify).request("GET", "https://nas.lan:5001/api")
    assert seen["address"] == ("192.168.1.10", 5001) and asked == ["nas.lan"]
    assert seen["server_hostname"] == "nas.lan"
    if verify:
        assert seen["verify_mode"] == ssl.CERT_REQUIRED and seen["check_hostname"] is True
    else:  # option explicite du foyer (certificat auto-signé) : comportement inchangé
        assert seen["verify_mode"] == ssl.CERT_NONE and seen["check_hostname"] is False


def test_truenas_websocket_pinned(monkeypatch, tmp_path):
    from nokker.homebridge.watch import nas
    from nokker.homebridge.watch.net import UpstreamError

    env, asked = _watch_env(tmp_path)
    seen: dict = {}

    def create_connection(address, *a, **kw):
        seen["address"] = address
        seen["sock"] = _FakeSock()
        return seen["sock"]

    def connect(url, *, sock=None, ssl=None, server_hostname=None, **kw):
        seen.update(url=url, sock_passed=sock, server_hostname=server_hostname, ssl=ssl)
        raise _Stop("arrêt du test")

    client = types.ModuleType("websockets.sync.client")
    client.connect = connect
    monkeypatch.setitem(sys.modules, "websockets", types.ModuleType("websockets"))
    monkeypatch.setitem(sys.modules, "websockets.sync", types.ModuleType("websockets.sync"))
    monkeypatch.setitem(sys.modules, "websockets.sync.client", client)
    monkeypatch.setattr(nas.socket, "create_connection", create_connection)

    rpc = nas.websocket_rpc(env, "wss://nas.lan/api/current", "secret:K", True)
    with pytest.raises(UpstreamError):
        rpc("system.info", [])
    assert seen["address"] == ("192.168.1.10", 443) and asked == ["nas.lan"]
    assert seen["sock_passed"] is seen["sock"] and seen["sock"].closed
    assert seen["server_hostname"] == "nas.lan" and seen["ssl"].verify_mode == ssl.CERT_REQUIRED
