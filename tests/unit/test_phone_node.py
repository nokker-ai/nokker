"""Nœud téléphone, côté nokker.ai (P7a, docs/LOKKAL-PHONE.md §3) : inscription sous carte forte (signée sur un AUTRE
appareil), jeton retiré une fois, jeton limité à /v1/gateway/phone/*, preuve de clé (ES256, ±60 s, nonce unique),
battement, file des opérations fermées, médias signés à usage unique, et P8 par la file. Par le VRAI serveur."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from nokker.cloud.phone import (
    MemoryPhoneStore,
    PhoneError,
    PhoneService,
    clean_beat,
    proof_message,
    verify_signature,
)
from nokker.tenancy.context import tenant_scope
from tests.unit.test_crew_deck import _call, cloud_http  # noqa: F401 — fixture (vrai serveur, vrai lien)


def keypair():
    priv = ec.generate_private_key(ec.SECP256R1())
    spki = priv.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return priv, base64.urlsafe_b64encode(spki).decode().rstrip("=")  # base64url, comme le SDK


def open_sealed(priv, approval_id, sealed):
    """Ce que fait l'app (clé du nœud) : ECDH avec la clé éphémère, HKDF, AES-256-GCM, AAD = approval_id."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from nokker.sites.crypto import _hkdf, _jwk_pub, unb64u

    key = _hkdf(priv.exchange(ec.ECDH(), _jwk_pub(sealed["epk"])), f"nokker-phone-v1|{approval_id}")
    return json.loads(AESGCM(key).decrypt(unb64u(sealed["iv"]), unb64u(sealed["ct"]), approval_id.encode()))[
        "machine_token"]


def proof(priv, method, host, path_qs, body=b"", ts=None, nonce=None, raw=True):
    ts = str(int(ts if ts is not None else time.time()))
    nonce = nonce or secrets.token_hex(12)
    der = priv.sign(proof_message(method, host, path_qs, ts, nonce, body), ec.ECDSA(hashes.SHA256()))
    if raw:
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

        r, s = decode_dss_signature(der)
        der = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"v1;ts={ts};nonce={nonce};sig={base64.urlsafe_b64encode(der).decode().rstrip('=')}"


def req(port, method, path, *, token=None, body=None, priv=None, host=None, ts=None, nonce=None, raw_proof=None):
    data = json.dumps(body).encode() if body is not None else b""
    host = host or f"127.0.0.1:{port}"
    h = {"Content-Type": "application/json", "Host": host}
    if token:
        h["Authorization"] = f"Bearer {token}"
    if raw_proof is not None:
        h["X-Nokker-Node-Proof"] = raw_proof
    elif priv is not None:
        h["X-Nokker-Node-Proof"] = proof(priv, method, host, path, data, ts=ts, nonce=nonce)
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=h, data=data or None)
    r.add_unredirected_header("Host", host)
    try:
        with urllib.request.urlopen(r, timeout=40) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            return resp.status, (json.loads(raw) if raw and "json" in ctype else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, json.loads(raw) if raw else None


@pytest.fixture
def phone_world(cloud_http):  # noqa: F811
    from nokker.cloud import http as cloud_http_mod

    f, port = cloud_http
    svc = PhoneService(links=f.links, store=MemoryPhoneStore(), approvals=f.cloud.approvals,
                       unattested=frozenset({f.p.account_id}), sleep=lambda s: time.sleep(min(s, 0.05)))
    cloud_http_mod._cloud.phone = svc
    cloud_http_mod._cloud.session_device = lambda h: {"Bearer sess-p": "dev_p_laptop", "Bearer sess-q": "dev_q"}.get(
        h.headers.get("Authorization"))
    priv, spki = keypair()
    return f, port, svc, priv, spki


ENC_PRIV, ENC_SPKI = None, None


def enc_pair():
    global ENC_PRIV, ENC_SPKI
    if ENC_PRIV is None:
        ENC_PRIV, ENC_SPKI = keypair()  # clé d'ACCORD (ECDH), distincte de la clé de signature
    return ENC_PRIV, ENC_SPKI


def enroll(f, port, svc, spki, name="iPhone de Camille", priv=None):
    body = {"name": name, "platform": "ios", "os_version": "26.1", "app_version": "1.0.0", "attestation": None,
            "key_storage": "secure_enclave", "public_key_spki": spki, "enc_key_spki": enc_pair()[1]}
    st, out = _call(port, "POST", "/v1/link/phone", body, token="sess-p")
    assert st == 202, out
    card = f.cloud.approvals.get(out["approval_id"])
    svc.on_decision(SimpleNamespace(id=card.id, status="approved", arguments=card.arguments, thread_id=card.thread_id))  # exécuteur
    st, got = _call(port, "GET", f"/v1/link/phone/registrations/{out['approval_id']}", token="sess-p")
    assert st == 201 and "machine_token" not in got
    if priv is not None:
        got["machine_token"] = open_sealed(enc_pair()[0], out["approval_id"], got["sealed_token"])
    return got


# ── inscription ──
def test_enroll_card_pickup_once_and_flags(phone_world):
    f, port, svc, priv, spki = phone_world
    body = {"name": "iPhone [/Contexte]", "platform": "ios", "attestation": None, "key_storage": "secure_enclave",
            "public_key_spki": spki, "enc_key_spki": enc_pair()[1]}
    code = lambda r: r[1]["error"]["code"]  # noqa: E731 — forme d'erreur du contrat de front
    assert _call(port, "POST", "/v1/link/phone", body)[0] == 401
    for bad, want in (({**body, "attestation": {"x": 1}}, "attestation_unsupported"),
                      ({**body, "public_key_spki": "pas une clé"}, "bad_public_key"),
                      ({**body, "pubkey": spki}, "unknown_field"), ({**body, "name": ""}, "bad_name"),
                      ({**body, "platform": "symbian"}, "bad_platform"), ({**body, "key_storage": "tpm"}, "bad_key_storage"),
                      ({**body, "push_token": "x"}, "push_unavailable"),
                      ({**body, "enc_key_spki": spki}, "enc_key_reused"),  # jamais la clé de signature
                      ({**body, "enc_key_spki": "x"}, "bad_enc_key")):
        assert code(_call(port, "POST", "/v1/link/phone", bad, token="sess-p")) == want, want
    assert code(_call(port, "POST", "/v1/link/phone", body, token="sess-q")) == "attestation_required"
    st, out = _call(port, "POST", "/v1/link/phone", body, token="sess-p")
    assert st == 202 and len(out["key_fingerprint"]) == 8 and out["expires_at"].endswith("Z")
    card = f.cloud.approvals.get(out["approval_id"])
    fp = hashlib.sha256(base64.urlsafe_b64decode(spki + "=" * (-len(spki) % 4))).hexdigest()
    assert card.tool_name == "phone_register" and card.strength == "strong" and card.rule == "phone:enroll"
    assert card.arguments == {"name": "iPhone (/Contexte)", "platform": "ios", "key_fingerprint": fp[:8],
                              "key_storage": "secure_enclave", "attested": False, "requested_from": "dev_p_laptop"}
    assert card.reason == f"Inscrire le téléphone « iPhone (/Contexte) » comme nœud (clé {fp[:8]})."
    assert "AUTRE" in card.advice and spki not in json.dumps(card.arguments)  # ni la clé ni le compte sur la carte
    pick = f"/v1/link/phone/registrations/{card.id}"
    assert _call(port, "GET", pick, token="sess-p") == (202, {"status": "pending"})
    assert _call(port, "GET", pick, token="sess-q")[0] == 404  # pas sa carte
    svc.on_decision(SimpleNamespace(id=card.id, status="approved", arguments=card.arguments, thread_id=card.thread_id))
    st, got = _call(port, "GET", f"/v1/link/phone/{card.id}", token="sess-p")  # alias
    assert st == 201 and "machine_token" not in got and got["member"] == f.p.account_id
    assert set(got) == {"machine_id", "sealed_token", "member", "caps_allowed"}  # JAMAIS machine_token
    token = open_sealed(enc_pair()[0], card.id, got["sealed_token"])  # scellé pour la clé d'accord du nœud
    assert f.links.machine_for_token(token).kind == "phone"
    with pytest.raises(Exception):
        open_sealed(priv, card.id, got["sealed_token"])  # la clé de SIGNATURE ne l'ouvre pas
    assert code(_call(port, "GET", pick, token="sess-p")) == "gone"  # une seule fois
    phones = _call(port, "GET", "/v1/link/phone", token="sess-p")[1]["phones"]
    assert phones[0]["key_fingerprint"] == fp[:8] and phones[0]["live"] is False
    machines = _call(port, "GET", "/v1/link/machines", token="sess-p")[1]["machines"]
    assert [(m["kind"], m["host"]) for m in machines] == [("phone", "")]
    assert f.links.machines_of(f.p.account_id) == [] and not f.links.has_machine(f.p.tenant_id)  # pas un Lokkal
    assert f.links.machine_by_host(f"{machines[0]['machine_id']}.phone.invalid") is None
    # refusée
    st, out = _call(port, "POST", "/v1/link/phone", body, token="sess-p")
    svc.on_decision(SimpleNamespace(id=out["approval_id"], status="rejected", arguments={},
                                    thread_id=f.cloud.approvals.get(out["approval_id"]).thread_id))
    assert code(_call(port, "GET", f"/v1/link/phone/registrations/{out['approval_id']}", token="sess-p")) == "rejected"


def test_enroll_card_is_never_signed_by_the_requesting_device():
    from nokker.approvals.errors import ProofInvalid
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.core.ports import Proof
    from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict
    from tests.unit.approvals.conftest import gen_keypair, sign_p1363

    backend = InMemoryApprovals()

    def device(user):
        priv, spki = gen_keypair()
        d = backend.enroll_device(user=user, name="x", public_key_spki=spki)
        backend.confirm_device_cli(d.code)
        return d, priv

    phone_dev, phone_priv = device("alice")
    laptop, laptop_priv = device("alice")

    def card():
        turn = Turn(thread_id="phone-enroll-alice", index=0, input=InputCheck(text=""), channel="kernel", sender="alice")
        call = ToolCall(id=f"c{secrets.token_hex(3)}", name="phone_enroll", arguments={"requested_from": phone_dev.id})
        return backend.request(turn, call, Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1,
                                                   rule="phone:enroll", reason="r", strength="standard"), None)

    def decide(c, dev, priv):
        ch = backend.challenge(c.id)
        sig = sign_p1363(priv, f"{ch.canonical()}.approve".encode())
        return backend.decide(c.id, "approve", Proof(challenge=ch.canonical(), signature=sig, device_id=dev.id))

    c = card()
    with pytest.raises(ProofInvalid, match="autre de tes appareils"):
        decide(c, phone_dev, phone_priv)
    assert decide(card(), laptop, laptop_priv).status == "approved"


# ── preuve de clé et portée du jeton ──
def test_phone_token_only_on_phone_routes_and_only_with_proof(phone_world):
    f, port, svc, priv, spki = phone_world
    tok = enroll(f, port, svc, spki, priv=priv)["machine_token"]
    beat = {"v": 1, "reach": "live", "mode": "pocket", "caps": {"device_calendar": "on", "location": "denied"},
            "net": {"type": "wifi"}, "power": {"charging": True, "level": 0.8}, "lat": 46.2}
    for path in ("/v1/gateway/machine", "/v1/gateway/machine/load", "/v1/inference/chat/completions"):
        st, out = req(port, "GET" if path == "/v1/gateway/machine" else "POST", path, token=tok, body={}, priv=priv)
        assert st in (403, 404) and (st != 403 or out["error"]["code"] == "phone_scope"), path
    assert req(port, "GET", "/v1/gateway/machine", token=tok)[1]["error"]["code"] == "phone_scope"
    m1, _w, _lc = f.add_machine(f.p, "atelier")
    assert req(port, "POST", "/v1/gateway/phone/beat", token=f.machines[m1.host][3], body=beat, priv=priv)[1][
        "error"]["code"] == "phone_scope"  # un Lokkal n'entre pas sur les routes du téléphone
    assert req(port, "POST", "/v1/gateway/phone/beat", token=tok, body=beat)[1]["error"]["code"] == "proof_invalid"
    other, _ = keypair()
    assert req(port, "POST", "/v1/gateway/phone/beat", token=tok, body=beat, priv=other)[1]["error"]["code"] == "proof_invalid"
    assert req(port, "POST", "/v1/gateway/phone/beat", token=tok, body=beat, priv=priv,
               ts=time.time() - 120)[1]["error"]["code"] == "proof_invalid"  # hors ±60 s
    good = proof(priv, "POST", f"127.0.0.1:{port}", "/v1/gateway/phone/beat", json.dumps(beat).encode())
    assert req(port, "POST", "/v1/gateway/phone/beat", token=tok, body=beat, raw_proof=good) == (
        200, {"ok": True, "state": "live", "next_beat_s": 60})
    assert req(port, "POST", "/v1/gateway/phone/beat", token=tok, body={**beat, "reach": "push"}, priv=priv)[1][
        "error"]["code"] == "push_unavailable"  # phase S : sans push
    assert req(port, "POST", "/v1/gateway/phone/beat", token=tok, body=beat, raw_proof=good)[1]["error"] \
        ["code"] == "proof_invalid"  # nonce rejoué
    tampered = proof(priv, "POST", f"127.0.0.1:{port}", "/v1/gateway/phone/beat", b'{"reach":"push"}')
    assert req(port, "POST", "/v1/gateway/phone/beat", token=tok, body=beat, raw_proof=tampered)[1]["error"] \
        ["code"] == "proof_invalid"  # corps différent de celui signé
    with tenant_scope(f.p.tenant_id):
        node = svc.store.nodes_of(f.p.account_id)[0]
    assert "lat" not in json.dumps(node.beat) and node.has_cap("device_calendar") and not node.has_cap("location")
    assert svc.online_nodes(f.p.account_id, "device_calendar")


def test_signature_forms_and_beat_cleaning():
    priv, spki = keypair()
    msg = proof_message("GET", "App.Nokker.ai", "/v1/x?a=1", "1", "n" * 16, b"")
    der = priv.sign(msg, ec.ECDSA(hashes.SHA256()))
    assert verify_signature(spki, msg, der)  # DER (Secure Enclave)
    assert not verify_signature(spki, msg + b"x", der)
    b = clean_beat({"reach": "live", "caps": {"ocr": "on", "sms": "on"}, "gps": [1, 2]})
    assert set(b["caps"]) == {"location", "lan_home", "device_calendar", "camera", "ocr", "stt", "egress"}
    assert "gps" not in b
    with pytest.raises(PhoneError):
        clean_beat({"x": "y" * 5000})


# ── file, médias, P8 ──
def test_task_queue_long_poll_lease_result_and_expiry(phone_world):
    f, port, svc, priv, spki = phone_world
    tok = enroll(f, port, svc, spki, priv=priv)["machine_token"]
    with tenant_scope(f.p.tenant_id):
        node = svc.store.nodes_of(f.p.account_id)[0]
        t = svc.enqueue(f.p.account_id, node, "zone.state", {"zone_id": "maison"}, deadline_s=60)
        with pytest.raises(PhoneError):
            svc.enqueue(f.p.account_id, node, "ha.call", {})  # opération hors liste fermée
    st, out = req(port, "GET", "/v1/gateway/phone/tasks?wait=1", token=tok, priv=priv)
    assert st == 200 and [x["task_id"] for x in out["tasks"]] == [t.task_id] and out["tasks"][0]["args"] == {
        "zone_id": "maison"}
    assert req(port, "GET", "/v1/gateway/phone/tasks?wait=0", token=tok, priv=priv)[1]["tasks"] == []  # bail
    assert out["tasks"][0]["deadline"].endswith("Z") and out["tasks"][0]["origin"] == {"kind": "turn"}
    assert req(port, "POST", f"/v1/gateway/phone/tasks/{t.task_id}/lease", token=tok, body={}, priv=priv)[1][
        "lease_until"].endswith("Z")
    res = f"/v1/gateway/phone/tasks/{t.task_id}/result"
    assert req(port, "POST", res, token=tok, body={"status": "done"}, priv=priv)[1]["error"]["code"] == "result_required"
    assert req(port, "POST", res, token=tok, body={"status": "failed"}, priv=priv)[1]["error"]["code"] == "error_required"
    assert req(port, "POST", res, token=tok, body={"status": "x"}, priv=priv)[1]["error"]["code"] == "bad_status"
    st, r = req(port, "POST", res, token=tok, body={"status": "done", "result": "dans la zone"}, priv=priv)
    assert (st, r) == (200, {"stored": True})
    assert req(port, "POST", res, token=tok, body={"status": "done", "result": "dans la zone"}, priv=priv) == (
        200, {"stored": False})  # même résultat : idempotent
    assert req(port, "POST", res, token=tok, body={"status": "done", "result": "autre"}, priv=priv)[1]["error"][
        "code"] == "already_answered"
    assert req(port, "POST", f"/v1/gateway/phone/tasks/{t.task_id}/lease", token=tok, body={}, priv=priv)[1]["error"][
        "code"] == "task_done"
    log = _call(port, "GET", f"/v1/link/phone/{node.machine_id}/tasks", token="sess-p")[1]["tasks"]
    assert log[0] == {"task_id": t.task_id, "op": "zone.state", "origin": "turn", "status": "done", "at": log[0]["at"]}
    assert "result" not in json.dumps(log) and _call(port, "GET", f"/v1/link/phone/{node.machine_id}/tasks",
                                                     token="sess-q")[0] == 404
    with tenant_scope(f.p.tenant_id):
        assert svc.wait(t.task_id, 0.1).result == "dans la zone"
        late = svc.enqueue(f.p.account_id, node, "zone.state", {}, deadline_s=-1)  # échue
    assert req(port, "GET", "/v1/gateway/phone/tasks?wait=0", token=tok, priv=priv)[1]["tasks"] == []
    with tenant_scope(f.p.tenant_id):
        assert svc.store.task(late.task_id).status == "expired"
    assert req(port, "POST", "/v1/gateway/phone/tasks/pt_0000000000000000/result", token=tok,
               body={"status": "done"}, priv=priv)[0] == 404
    big = "x" * (64 * 1024 + 1)
    with tenant_scope(f.p.tenant_id):
        t2 = svc.enqueue(f.p.account_id, node, "zone.state", {})
    req(port, "GET", "/v1/gateway/phone/tasks?wait=0", token=tok, priv=priv)  # en bail
    assert req(port, "POST", f"/v1/gateway/phone/tasks/{t2.task_id}/result", token=tok,
               body={"status": "done", "result": big}, priv=priv)[1]["error"]["code"] == "result_too_large"
    with tenant_scope(f.p.tenant_id):
        pending = svc.enqueue(f.p.account_id, node, "zone.state", {})
    assert _call(port, "POST", f"/v1/link/machines/{node.machine_id}/revoke", {}, token="sess-p") == (200, {"revoked": True})
    with tenant_scope(f.p.tenant_id):
        assert svc.store.task(pending.task_id).status == "cancelled"  # retiré : sa file est annulée
    assert req(port, "GET", "/v1/gateway/phone/tasks?wait=0", token=tok, priv=priv)[0] == 401  # jeton mort


def test_media_url_signed_single_use_and_bound_to_the_node(phone_world):
    f, port, svc, priv, spki = phone_world
    got = enroll(f, port, svc, spki, priv=priv)
    svc.media_source = lambda account, ref: (b"\x89PNG..." + ref.encode(), "image/png") if ref == "img_1" else None
    url = svc.media_url(got["machine_id"], "img_1")
    path = url.split("app.nokker.ai", 1)[1]
    st, data = req(port, "GET", path, token=got["machine_token"], priv=priv)
    assert st == 200 and data.startswith(b"\x89PNG")
    assert req(port, "GET", path, token=got["machine_token"], priv=priv)[0] == 410  # usage unique
    other = svc.media_url("mc_" + "0" * 16, "img_1").split("app.nokker.ai", 1)[1]
    assert req(port, "GET", other, token=got["machine_token"], priv=priv)[1]["error"]["code"] == "not_your_blob"
    svc.clock = lambda: time.time() - 200  # URL émise il y a 200 s : échue (2 min)
    stale = svc.media_url(got["machine_id"], "img_1").split("app.nokker.ai", 1)[1]
    svc.clock = time.time
    assert req(port, "GET", stale, token=got["machine_token"], priv=priv)[0] == 410


def test_p8_goes_through_an_online_phone_then_falls_back(phone_world):
    from nokker.devices.broker import DeviceBroker, DeviceRequestError
    from nokker.devices.store import MemoryDeviceStore

    f, port, svc, priv, spki = phone_world
    tok = enroll(f, port, svc, spki, priv=priv)["machine_token"]
    broker = DeviceBroker(devices=MemoryDeviceStore(), publish=lambda k, d: None, timeout_s=0.2,
                          via_node=lambda m, op, a: svc.calendar_list(m, a) if op == "calendar.list" else None)
    with tenant_scope(f.p.tenant_id):
        with pytest.raises(DeviceRequestError) as e:  # téléphone pas en ligne : repli device.request (aucun appareil)
            broker.request(f.p.account_id, "calendar.list", {"from": "a", "to": "b"})
        assert e.value.code == "no_device"
    beat = {"v": 1, "reach": "live", "caps": {"device_calendar": "on"}}
    assert req(port, "POST", "/v1/gateway/phone/beat", token=tok, body=beat, priv=priv)[0] == 200

    def phone():  # l'app : relève puis répond (champ hors liste blanche compris)
        for _ in range(40):
            st, out = req(port, "GET", "/v1/gateway/phone/tasks?wait=1", token=tok, priv=priv)
            if out and out.get("tasks"):
                t = out["tasks"][0]
                items = [{"title": "Dentiste", "start": "2026-11-07T09:00:00+01:00", "end": "2026-11-07T09:30:00+01:00",
                          "all_day": False, "calendar": "Perso", "notes": "", "source": "caldav"}]
                req(port, "POST", f"/v1/gateway/phone/tasks/{t['task_id']}/result", token=tok,
                    body={"status": "done", "result": json.dumps(items)}, priv=priv)
                return

    th = threading.Thread(target=phone, daemon=True)
    th.start()
    with tenant_scope(f.p.tenant_id):
        items = broker.request(f.p.account_id, "calendar.list", {"from": "2026-11-07T00:00:00+00:00",
                                                                  "to": "2026-11-08T00:00:00+00:00"})
    th.join(5)
    assert items == [{"title": "Dentiste", "start": "2026-11-07T09:00:00+01:00", "end": "2026-11-07T09:30:00+01:00",
                      "all_day": False, "calendar": "Perso", "notes": ""}]  # liste blanche : "source" retirée


# Vecteur produit par le VRAI SDK de front (packages/sdk/src/node.ts @ af23117 : sealToken, proofString + WebCrypto
# ECDSA), une fois, avec une clé jetable : le noyau doit l'ouvrir et le vérifier, sans Node dans la CI.
SDK_VECTOR = {"pkcs8": "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQg4LLmrZdWD9oD+Jlt2gz+F/DnHzm4uzyx4Qzd23TNI2WhRANCAARoPdpos8SX8WJiBIud0Zx7sjxUxMJ8Dx4YSklbBLmBQOoLK0PiFFkysIP2MMdHhOgoy2vFkakkRULibKmTfifF", "sealed": {"v": 1, "epk": {"kty": "EC", "crv": "P-256", "x": "2QeCoBVLCGL7OOPU9ECHK98zIHj0gpDFfFg8kwOAAFU", "y": "pOxG164ohZkYScIpx9XyCy426LrPNcNuj1F1iqwxLGg"}, "iv": "g1q_7Co8ZDMarJxZ", "ct": "28HWoCG4fG1oRjhPMhgAgIlRY2jGhNu7pbKBzQNJffu7ApR55xcXHNQKNtBF-ZMwxFQ"}, "proof": {"spki": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEZePP6mWlJ4JDhI90vCYhZ4gig4ivwrzjxcF5TIKpe8mKnB_XVk5p2CJwh9KfM0rftK7rfECv21O-D1CdPK-SIw", "method": "post", "host": "App.Nokker.AI", "path": "/v1/gateway/phone/beat?x=1", "ts": "1793936154", "nonce": "9FMrY3DBduxzob1Rs4rsbw", "body": "{\"v\":1,\"reach\":\"live\"}", "sig": "U5wqKJvuYTQPzrcKUtbjbLSmQOrP6eOjeExoRBmr5L0rzKbyj_5R0I_LaLKGULRtxBhcwB6rJz1hrKhoch41_Q"}}


def test_cross_check_with_the_front_sdk_vector():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from nokker.sites.crypto import _hkdf, _jwk_pub, unb64u

    v = SDK_VECTOR
    priv = serialization.load_der_private_key(base64.b64decode(v["pkcs8"]), None)
    s = v["sealed"]
    key = _hkdf(priv.exchange(ec.ECDH(), _jwk_pub(s["epk"])), "nokker-phone-v1|ap_croise_2")
    assert json.loads(AESGCM(key).decrypt(unb64u(s["iv"]), unb64u(s["ct"]), b"ap_croise_2")) == {
        "machine_token": "jeton-du-sdk-7"}  # sceau du SDK ouvert par la construction du noyau
    pr = v["proof"]
    msg = proof_message(pr["method"], pr["host"], pr["path"], pr["ts"], pr["nonce"], pr["body"].encode())
    sig = base64.urlsafe_b64decode(pr["sig"] + "=" * (-len(pr["sig"]) % 4))
    assert verify_signature(pr["spki"], msg, sig)  # preuve du SDK (r||s, méthode et hôte normalisés) vérifiée


def test_registration_survives_a_restart_and_the_token_is_never_stored_in_clear(phone_world):
    f, port, svc, priv, spki = phone_world
    body = {"name": "iPhone", "platform": "ios", "attestation": None, "key_storage": "secure_enclave",
            "public_key_spki": spki, "enc_key_spki": enc_pair()[1]}
    st, out = _call(port, "POST", "/v1/link/phone", body, token="sess-p")
    card = f.cloud.approvals.get(out["approval_id"])
    # « redémarrage » : un nouveau service sur le MÊME magasin, sans rien en mémoire
    from nokker.cloud import http as cloud_http_mod

    svc2 = PhoneService(links=f.links, store=svc.store, approvals=f.cloud.approvals, unattested=svc.unattested)
    cloud_http_mod._cloud.phone = svc2
    svc2.on_decision(SimpleNamespace(id=card.id, status="approved", arguments=card.arguments, thread_id=card.thread_id))
    with tenant_scope(f.p.tenant_id):
        row = svc2.store.reg(card.id)
    assert row["state"] == "ready" and "machine_token" not in json.dumps(row)  # seul le SCEAU est gardé
    svc3 = PhoneService(links=f.links, store=svc.store, approvals=f.cloud.approvals, unattested=svc.unattested)
    cloud_http_mod._cloud.phone = svc3  # second redémarrage avant le retrait
    st, got = _call(port, "GET", f"/v1/link/phone/registrations/{card.id}", token="sess-p")
    token = open_sealed(enc_pair()[0], card.id, got["sealed_token"])
    assert st == 201 and f.links.machine_for_token(token).kind == "phone"
    with tenant_scope(f.p.tenant_id):
        assert svc3.store.reg(card.id)["sealed_token"] is None and svc3.store.claim_reg(card.id) is None
    assert _call(port, "GET", f"/v1/link/phone/registrations/{card.id}", token="sess-p")[0] == 410


def test_phone_token_refused_on_sites_registry(phone_world):
    # Portée du téléphone vérifiée AVANT la route du registre privé de sites-chrome (fusion r46).
    f, port, svc, priv, spki = phone_world
    tok = enroll(f, port, svc, spki, priv=priv)["machine_token"]
    for signed in (priv, None):
        st, out = req(port, "POST", "/v1/lokkal/sites-registry", token=tok, body={}, priv=signed)
        assert st == 403 and out["error"]["code"] == "phone_scope"
