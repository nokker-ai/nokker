"""Le téléphone en nœud maison (P10a, docs/LOKKAL-PHONE.md §4.4) : opérations de home-bridge par la file (exclues :
écrire ou supprimer une configuration, afficher une caméra), plafond de 256 Kio, `lan_home` sur le Wi-Fi « maison »
seulement, actions portées par une décision maison et attestées (JWT `nokker-phone-task`), `PhoneBridge` à l'interface
de HomeBridge, et la source maison du cloud (mêmes outils que Lokkal, sans `maison_config`)."""
from __future__ import annotations

import base64
import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from nokker.ambient.bridge import BridgeError
from nokker.cloud import phone_home as ph
from nokker.cloud.link import Signer
from nokker.cloud.phone import PhoneError
from nokker.tenancy.context import tenant_scope
from tests.unit.test_crew_deck import _call, cloud_http  # noqa: F401 — fixture
from tests.unit.test_phone_node import enroll, phone_world, req  # noqa: F401 — fixtures et aides


def _jwt(token):
    head, body, _ = token.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    return json.loads(base64.urlsafe_b64decode(pad(head))), json.loads(base64.urlsafe_b64decode(pad(body)))


@pytest.fixture
def home(phone_world):  # noqa: F811
    f, port, svc, priv, spki = phone_world
    svc.signer = Signer.generate()
    tok = enroll(f, port, svc, spki, priv=priv)["machine_token"]
    with tenant_scope(f.p.tenant_id):
        node = svc.store.nodes_of(f.p.account_id)[0]
        svc.store.add_node(replace(node, attested=True))  # actions : nœud attesté seulement
        node = svc.store.node(node.machine_id)
    return f, port, svc, priv, tok, node


def beat(port, tok, priv, *, home_wifi=True):
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


def test_ops_are_the_bridges_minus_config_and_camera_and_actions_need_a_decision(home):
    f, port, svc, priv, tok, node = home
    acc = f.p.account_id
    with tenant_scope(f.p.tenant_id):
        for op in ("ha.write_config", "ha.delete_config", "ha.camera_show"):
            with pytest.raises(PhoneError) as e:
                svc.enqueue(acc, node, op, {})
            assert e.value.code == "op_not_allowed"
        t = svc.enqueue(acc, node, "ha.events", {"after": 3, "timeout": 99})
        assert t.args == {"after": 3, "timeout": 20.0}
        call = {"domain": "light", "service": "turn_on", "entity_ids": ["light.salon"], "data": {}}
        with pytest.raises(PhoneError) as e:
            svc.enqueue(acc, node, "ha.call_service", call)  # aucune décision
        assert e.value.code == "decision_required"
        other = {"class": "direct", "domain": "light", "service": "turn_off", "entity_ids": ["light.salon"]}
        with pytest.raises(PhoneError):
            svc.enqueue(acc, node, "ha.call_service", call, origin={"decision": other})  # décision d'autre chose
        lock = {"domain": "lock", "service": "unlock", "entity_ids": ["lock.entree"], "data": {}}
        direct_lock = {"class": "direct", "domain": "lock", "service": "unlock", "entity_ids": ["lock.entree"]}
        with pytest.raises(PhoneError):  # jamais la liste sensible sans carte forte, même mal classée
            svc.enqueue(acc, node, "ha.call_service", lock, origin={"decision": direct_lock})
        ok = svc.enqueue(acc, node, "ha.call_service", lock, origin={"decision": {"class": "strong",
                                                                               "approval_id": "ap_x"}})
        assert ok.origin["decision"]["approval_id"] == "ap_x"
        unattested = replace(node, attested=False)
        with pytest.raises(PhoneError) as e:
            svc.enqueue(acc, unattested, "ha.call_service", lock, origin={"decision": {"class": "strong",
                                                                                     "approval_id": "ap_x"}})
        assert e.value.code == "attestation_required"
        assert svc.enqueue(acc, unattested, "ha.states", {}).op == "ha.states"  # lecture : sans attestation


def test_an_action_on_the_wire_carries_a_signed_attestation_of_its_decision(home):
    f, port, svc, priv, tok, node = home
    beat(port, tok, priv)
    call = {"domain": "light", "service": "turn_on", "entity_ids": ["light.salon"], "data": {"brightness": 80}}
    with tenant_scope(f.p.tenant_id):
        t = svc.enqueue(f.p.account_id, node, "ha.call_service", call,
                        origin={"decision": {"class": "direct", "domain": "light", "service": "turn_on",
                                             "entity_ids": ["light.salon"]}})
    st, out = req(port, "GET", "/v1/gateway/phone/tasks?wait=0", token=tok, priv=priv)
    wire = out["tasks"][0]
    head, claims = _jwt(wire["attestation"])
    assert head["typ"] == "nokker-phone-task" and head["alg"] == "ES256" and head["kid"] == svc.signer.kid
    assert claims["aud"] == node.machine_id and claims["task_id"] == t.task_id == wire["task_id"]
    assert claims["args_sha256"] == wire["args_sha256"] and claims["decision"]["class"] == "direct"
    assert claims["approval_id"] is None and 0 < claims["exp"] - claims["iat"] <= 120
    with tenant_scope(f.p.tenant_id):  # une lecture : pas d'attestation
        svc.enqueue(f.p.account_id, node, "ha.states", {})
    st, out = req(port, "GET", "/v1/gateway/phone/tasks?wait=0", token=tok, priv=priv)
    assert "attestation" not in out["tasks"][0]


def test_lan_home_only_on_the_home_wifi_and_the_bridge_speaks_like_home_bridge(home):
    f, port, svc, priv, tok, node = home
    acc, tid = f.p.account_id, f.p.tenant_id
    bridge = ph.PhoneBridge(phone=svc, account=acc, tenant=tid, wait_s=5)
    beat(port, tok, priv, home_wifi=False)
    with pytest.raises(BridgeError) as e:
        bridge.op("ha.states", {})
    assert str(e.value) == ("passerelle du foyer : ha.states en échec (no_home_node) — aucun nœud maison en ligne sur "
                            "le Wi-Fi de la maison")
    beat(port, tok, priv)
    big = {"states": [{"entity_id": f"sensor.s{i}", "state": "x" * 900} for i in range(200)]}  # > 64 Kio, < 256 Kio
    th = phone_does(port, tok, priv, lambda t: {"status": "done", "result": json.dumps(big)})
    assert len(bridge.op("ha.states", {})["states"]) == 200
    th.join(5)
    th = phone_does(port, tok, priv, lambda t: {"status": "failed", "error": {"code": "ha_auth_failed"}})
    with pytest.raises(BridgeError) as e:
        bridge.op("ha.registry", {})
    th.join(5)
    assert "(ha_auth_failed) — jeton Home Assistant refusé, à saisir à nouveau dans l'app Nokker du téléphone" in str(e.value)
    with pytest.raises(BridgeError) as e:
        bridge.op("ha.call_service", {"domain": "light", "service": "turn_on", "entity_ids": ["light.a"]})
    assert "(decision_required)" in str(e.value)  # hors d'une décision maison : refusé avant la file
    with pytest.raises(BridgeError):
        bridge.op("ha.write_config", {})


def test_decisions_set_by_the_cloud_home_reach_the_phone(home):
    f, port, svc, priv, tok, node = home
    beat(port, tok, priv)
    bridge = ph.PhoneBridge(phone=svc, account=f.p.account_id, tenant=f.p.tenant_id, wait_s=5)
    seen = []
    th = phone_does(port, tok, priv, lambda t: {"status": "done", "result": "{\"ok\": true}"}, seen)
    with ph.decision({"class": "direct"}):  # posée par CloudHome autour de l'outil `maison`
        assert bridge.op("ha.call_service", {"domain": "light", "service": "turn_off",
                                             "entity_ids": ["light.salon"], "data": {}}) == {"ok": True}
    th.join(5)
    _, claims = _jwt(seen[0]["attestation"])
    assert claims["decision"] == {"class": "direct", "domain": "light", "service": "turn_off",
                                  "entity_ids": ["light.salon"]}
    th = phone_does(port, tok, priv, lambda t: {"status": "done", "result": "{\"ok\": true}"}, seen)
    with ph.decision({"class": "strong", "approval_id": "ap_42"}):  # maison_action_forte sous carte forte
        bridge.op("ha.call_service", {"domain": "lock", "service": "unlock", "entity_ids": ["lock.entree"], "data": {}})
    th.join(5)
    assert _jwt(seen[1]["attestation"])[1]["approval_id"] == "ap_42"
    with ph.decision({"class": "direct"}), pytest.raises(BridgeError):  # `direct` ne porte pas une notification
        bridge.op("ha.notify", {"target": "notify.x", "message": "bonjour"})


def test_cloud_home_is_offered_only_with_a_home_node_with_the_same_tools_minus_config(home):
    from nokker.home.source import MAISON_DESC, MAISON_DESC_CLOUD

    f, port, svc, priv, tok, node = home
    cloud = ph.CloudHome(phone=svc, account_of=lambda tid: f.p.account_id if tid == f.p.tenant_id else None)
    beat(port, tok, priv, home_wifi=False)
    assert not cloud.available_for(f.p.tenant_id)
    beat(port, tok, priv)
    assert cloud.available_for(f.p.tenant_id) and not cloud.available_for("tn_autre000000")
    specs = {s.name: s for s in cloud.specs()}
    assert set(specs) == {"maison", "maison_action_forte"}  # ni maison_config ni notifier (aucun destinataire)
    assert specs["maison"].description == MAISON_DESC_CLOUD != MAISON_DESC
    assert "petit modèle hébergé en Europe" in MAISON_DESC_CLOUD and "local" not in MAISON_DESC_CLOUD.split(".")[0]
    with tenant_scope(f.p.tenant_id):
        src = cloud.source(f.p.tenant_id)
        assert src is cloud.source(f.p.tenant_id) and src.cloud and src.tiers == ("small",)
        r = cloud.call("maison_config", {}, SimpleNamespace(approval_id=None, sender=f.p.account_id))
    assert r.is_error


def test_a_garage_door_or_gate_never_opens_on_a_direct_decision_and_unknown_class_is_refused(home):
    f, port, svc, priv, tok, node = home
    acc = f.p.account_id
    call = {"domain": "cover", "service": "open_cover", "entity_ids": ["cover.x"], "data": {}}

    def direct(classes):
        return {"decision": {"class": "direct", "domain": "cover", "service": "open_cover", "entity_ids": ["cover.x"],
                             **({"device_classes": classes} if classes is not None else {})}}

    with tenant_scope(f.p.tenant_id):
        for bad in ({"cover.x": "garage"}, {"cover.x": "door"}, {"cover.x": "gate"}, {"cover.x": None}, {}, None):
            with pytest.raises(PhoneError) as e:
                svc.enqueue(acc, node, "ha.call_service", call, origin=direct(bad))
            assert e.value.code == "decision_required", bad
        assert svc.enqueue(acc, node, "ha.call_service", call, origin=direct({"cover.x": "shutter"})).op == "ha.call_service"
        assert svc.enqueue(acc, node, "ha.call_service", call, origin={"decision": {"class": "strong",
                                                                                  "approval_id": "ap_g"}})
        svc.cancel_all(node.machine_id)  # la suite relève une seule tâche : celle du pont
    # par le pont : la classe vient de l'état lu par la source ; état inconnu → refus avant la file
    beat(port, tok, priv)
    bridge = ph.PhoneBridge(phone=svc, account=acc, tenant=f.p.tenant_id, wait_s=2)
    states = {"cover.garage": {"device_class": "garage"}, "cover.volet": {"device_class": "shutter"}}
    for eid in ("cover.garage", "cover.inconnu"):
        with ph.decision({"class": "direct", "attributes": states.get}), pytest.raises(BridgeError) as e:
            bridge.op("ha.call_service", {"domain": "cover", "service": "open_cover", "entity_ids": [eid], "data": {}})
        assert "(decision_required)" in str(e.value), eid
    seen = []
    th = phone_does(port, tok, priv, lambda t: {"status": "done", "result": "{\"ok\": true}"}, seen)
    with ph.decision({"class": "direct", "attributes": states.get}):
        bridge.op("ha.call_service", {"domain": "cover", "service": "open_cover", "entity_ids": ["cover.volet"],
                                      "data": {}})
    th.join(5)
    assert _jwt(seen[0]["attestation"])[1]["decision"]["device_classes"] == {"cover.volet": "shutter"}


def test_transport_caps_follow_the_sdk_256k_for_states_registry_history_logbook_only(home):
    f, port, svc, priv, tok, node = home
    big = json.dumps({"x": "y" * (100 * 1024)})  # > 64 Kio, < 256 Kio
    for op, want in (("ha.history", 200), ("ha.query", 413)):
        args = {"entity_ids": ["sensor.a"]} if op == "ha.history" else {"domain": "weather", "service": "get_forecasts",
                                                                         "entity_ids": ["weather.home"], "data": {}}
        with tenant_scope(f.p.tenant_id):
            svc.cancel_all(node.machine_id)
            t = svc.enqueue(f.p.account_id, node, op, args)
        req(port, "GET", "/v1/gateway/phone/tasks?wait=0", token=tok, priv=priv)
        st, out = req(port, "POST", f"/v1/gateway/phone/tasks/{t.task_id}/result", token=tok, priv=priv,
                      body={"status": "done", "result": big})
        assert st == want, (op, st, out)
