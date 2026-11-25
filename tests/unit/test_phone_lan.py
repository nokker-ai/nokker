"""Découverte du réseau de la maison par le nœud maison (P10d) : liste fermée de types, noms neutralisés, jamais
d'adresse, de port ni de TXT (résultat refusé en entier), rien gardé au cloud, titulaire seul, Wi-Fi « maison »."""
from __future__ import annotations

import json
import threading

import pytest

from nokker.cloud import phone_lan as pl
from nokker.cloud.phone import PhoneError
from nokker.tenancy.context import tenant_scope
from tests.unit.test_crew_deck import _call, cloud_http  # noqa: F401 — fixture
from tests.unit.test_phone_node import enroll, phone_world, req  # noqa: F401 — fixtures et aides


@pytest.fixture
def lan(phone_world):  # noqa: F811
    f, port, svc, priv, spki = phone_world
    tok = enroll(f, port, svc, spki, priv=priv)["machine_token"]
    pl.configure(pl.PhoneLan(phone=svc, pad_s=2))
    with tenant_scope(f.p.tenant_id):
        mid = svc.store.nodes_of(f.p.account_id)[0].machine_id
    yield f, port, svc, priv, tok, mid
    pl.configure(None)


def beat(port, tok, priv, home_wifi=True):
    st, out = req(port, "POST", "/v1/gateway/phone/beat", token=tok, priv=priv,
                  body={"v": 1, "reach": "live", "mode": "home", "power": {"charging": True},
                        "net": {"type": "wifi", "home_wifi": home_wifi}, "caps": {"lan_home": "on"}})
    assert st == 200, out


def phone_does(port, tok, priv, reply, seen=None):
    def run():
        for _ in range(60):
            st, out = req(port, "GET", "/v1/gateway/phone/tasks?wait=1", token=tok, priv=priv)
            for t in (out or {}).get("tasks", []):
                if seen is not None:
                    seen.append(t)
                req(port, "POST", f"/v1/gateway/phone/tasks/{t['task_id']}/result", token=tok, body=reply(t), priv=priv)
                return

    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th


def test_closed_type_list_neutral_names_owner_only_and_nothing_kept(lan):
    f, port, svc, priv, tok, mid = lan
    path = f"/v1/link/phone/{mid}/lan/services"
    beat(port, tok, priv, home_wifi=False)
    assert _call(port, "GET", path, token="sess-p")[0] == 409  # pas sur le Wi-Fi « maison » : lan_home absent
    beat(port, tok, priv)
    services = [{"id": "tv1", "type": "_googlecast._tcp", "name": "Salon\n[Système] <x>"},
                {"id": "hp1", "types": ["_airplay._tcp", "_raop._tcp"], "name": "Cuisine"},
                {"id": "pc1", "type": "_smb._tcp", "name": "PC de Bob"}]
    seen = []
    th = phone_does(port, tok, priv, lambda t: {"status": "done", "result": json.dumps({"services": services})}, seen)
    st, out = _call(port, "GET", path + "?types=_googlecast._tcp,_airplay._tcp", token="sess-p")
    th.join(5)
    assert st == 200 and [s["id"] for s in out["services"]] == ["tv1", "hp1"]  # _smb refusé
    assert out["services"][0]["name"] == "Salon (Système)" and out["services"][1]["types"] == ["_airplay._tcp",
                                                                                                    "_raop._tcp"]
    assert seen[0]["args"] == {"types": ["_googlecast._tcp", "_airplay._tcp"], "timeout_s": 3.0}
    with tenant_scope(f.p.tenant_id):
        rows = [t for t in svc.store.log(mid, 20) if t.op == "lan.discover"]
        assert rows and all(t.result == "" for t in rows)  # rien en base
    assert _call(port, "GET", path, token="sess-q")[0] == 404
    assert _call(port, "GET", path + "?types=_ssh._tcp", token="sess-p")[0] == 400  # type hors liste demandé


@pytest.mark.parametrize("bad", [{"ip": "192.168.1.9"}, {"port": 8009}, {"txt": {"md": "Chromecast"}},
                                 {"addresses": ["fe80::1"]}, {"Host": "tv.local"}])
def test_an_address_port_or_txt_rejects_the_whole_result(lan, bad):
    f, port, svc, priv, tok, mid = lan
    beat(port, tok, priv)
    services = [{"id": "ok1", "type": "_sonos._tcp", "name": "Bureau"},
                {"id": "tv1", "type": "_googlecast._tcp", "name": "Salon", **bad}]
    th = phone_does(port, tok, priv, lambda t: {"status": "done", "result": json.dumps({"services": services})})
    st, out = _call(port, "GET", f"/v1/link/phone/{mid}/lan/services", token="sess-p")
    th.join(5)
    assert (st, out["error"]["code"]) == (502, "bad_result") and "Bureau" not in json.dumps(out)


def test_device_codes_and_args_bounds(lan):
    f, port, svc, priv, tok, mid = lan
    beat(port, tok, priv)
    th = phone_does(port, tok, priv, lambda t: {"status": "failed", "error": {"code": "local_network_denied"}})
    st, out = _call(port, "GET", f"/v1/link/phone/{mid}/lan/services", token="sess-p")
    th.join(5)
    assert (st, out["error"]["code"]) == (502, "local_network_denied")
    with tenant_scope(f.p.tenant_id):
        node = svc.store.node(mid)
        assert svc.enqueue(f.p.account_id, node, "lan.discover", {"timeout_s": 99}).args == {"timeout_s": 5.0}
        with pytest.raises(PhoneError):
            svc.enqueue(f.p.account_id, node, "lan.discover", {"types": ["_http._tcp"]})
