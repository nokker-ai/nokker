"""ACCORD branché dans Lokkal : drapeau éteint par défaut (rien chargé, routes en 404 côté hub absent), configure_accord
sans port quand NOKKER_ACCORD_BIND manque, routes de l'app (fenêtre, code, état, révocation), champ inconnu de hello
ignoré (champ `caps` futur de la v2)."""
from __future__ import annotations

from types import SimpleNamespace

from nokker.accord import http as accord_http
from nokker.accord.hub import AccordHub
from nokker.accord.simulator import SimulatedDevice
from nokker.accord.store import MemoryAccordStore
from nokker.approvals.memory import InMemoryApprovals
from nokker.config import NokkerConfig

FP = "ef" * 32


class FakeHandler:
    def __init__(self, body=None):
        self.body, self.sent = body, []

    def _json(self, status, obj):
        self.sent.append((status, obj))

    def _read_json_body(self):
        return self.body


def test_flag_off_by_default(tmp_path, monkeypatch):
    assert ((NokkerConfig().local or {}).get("accord") or {}).get("enabled") is None
    cfg = SimpleNamespace(local={"accord": {"enabled": False}}, pg_dsn=lambda: "")
    assert accord_http.configure_accord(cfg, InMemoryApprovals(), SimpleNamespace(state_dir=tmp_path)) is None
    assert not (tmp_path / "accord").exists()  # aucun certificat tiré
    h = FakeHandler()
    accord_http.handle(h, "GET", "/v1/local/accord", None)
    assert h.sent[0][0] == 404


def test_enabled_without_bind_has_no_port(tmp_path, monkeypatch):
    monkeypatch.delenv("NOKKER_ACCORD_BIND", raising=False)
    cfg = SimpleNamespace(local={"accord": {"enabled": True}}, pg_dsn=lambda: "")
    hub = accord_http.configure_accord(cfg, InMemoryApprovals(), SimpleNamespace(state_dir=tmp_path))
    assert hub is not None and hub.server is None and hub.allow_test_key is False
    assert (tmp_path / "accord" / "cert.pem").is_file()


def test_app_routes_pair_status_revoke():
    hub = AccordHub(InMemoryApprovals(), MemoryAccordStore(), FP)
    h = FakeHandler()
    accord_http.handle(h, "POST", "/v1/local/accord/pair/open", hub)
    assert h.sent[-1][0] == 200 and h.sent[-1][1]["pairing"]["open"] is True and h.sent[-1][1]["host_fp8"] == FP[:8]
    d = SimulatedDevice()
    d.host_fp = FP
    s = hub.session()
    s.handle(d.pair_message("246810"))
    h = FakeHandler({"code": "000000"})
    accord_http.handle(h, "POST", "/v1/local/accord/pair", hub)
    assert h.sent[-1] == (400, {"error": {"code": "bad_code", "message": "bad_code"}})
    h = FakeHandler({"code": "246810"})
    accord_http.handle(h, "POST", "/v1/local/accord/pair", hub)
    dev_id = h.sent[-1][1]["device_id"]
    assert h.sent[-1][0] == 200 and h.sent[-1][1]["key8"] == d.key8
    h = FakeHandler()
    accord_http.handle(h, "DELETE", f"/v1/local/accord/devices/{dev_id}", hub)
    assert h.sent[-1] == (200, {"device_id": dev_id, "revoked": True})
    accord_http.handle(h, "DELETE", f"/v1/local/accord/devices/{dev_id}", hub)
    assert h.sent[-1][0] == 404


def test_unknown_hello_field_is_ignored():
    hub = AccordHub(InMemoryApprovals(), MemoryAccordStore(), FP)
    d = SimulatedDevice()
    d.host_fp = FP
    hub.open_window()
    s = hub.session()
    s.handle(d.pair_message("112233"))
    hub.confirm_code("112233")
    out = s.handle({"type": "hello", "v": 1, "pubkey": d.pubkey, "caps": {"camera": True}, "fw": "0.1.0"})
    assert out[0]["type"] == "challenge"
