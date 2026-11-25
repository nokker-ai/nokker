"""Passkey LOCALE de Lokkal (docs/PAY-AGENT.md §11) : enregistrement vérifié sur l'origine locale, décision d'une carte
« à payer » own_card avec rpId `localhost`, et — garde-fou non négociable — AUCUNE autre carte ne se signe en local."""
from __future__ import annotations

import base64
import hashlib
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from nokker.approvals.errors import NotLocalApproval, ProofInvalid
from nokker.core.ports import Proof
from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict
from nokker.local.passkeys import LocalPasskeys, PasskeyError, local_origin
from nokker.payments import agent as kagent
from nokker.payments.mandate import verify_envelope
from nokker.tenancy.context import tenant_scope
from tests.unit.payments.conftest import ALICE, TENANT, b64url
from tests.unit.payments.test_pay_agent_owncard import CARD, rig  # noqa: F401 — banc own_card (fixture)

LOCAL = "http://localhost:7420"


def local_assertion(priv, challenge_text: str, decision: str = "approve", *, origin: str = LOCAL,
                    rp_id: str = "localhost", uv: bool = True) -> dict:
    expected = hashlib.sha256(f"{challenge_text}.{decision}".encode()).digest()
    auth = hashlib.sha256(rp_id.encode()).digest() + bytes([0x01 | (0x04 if uv else 0)]) + (0).to_bytes(4, "big")
    cdj = json.dumps({"type": "webauthn.get", "challenge": b64url(expected), "origin": origin},
                     separators=(",", ":")).encode()
    sig = priv.sign(auth + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
    return {"client_data_json": b64url(cdj), "authenticator_data": b64url(auth), "signature": b64url(sig)}


def local_proof(priv, device_id, challenge_text, decision="approve", **kw) -> Proof:
    a = local_assertion(priv, challenge_text, decision, **kw)
    return Proof(challenge=challenge_text, device_id=device_id, webauthn_client_data_json=a["client_data_json"],
                 webauthn_authenticator_data=a["authenticator_data"], webauthn_signature=a["signature"])


@pytest.fixture
def local_key(rig):  # noqa: F811
    """Une passkey locale d'ALICE (dans le banc, le propriétaire local est ALICE)."""
    priv = ec.generate_private_key(ec.SECP256R1())
    spki = base64.b64encode(priv.public_key().public_bytes(serialization.Encoding.DER,
                                                           serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
    dev = rig.world.backend.enroll_device(user=ALICE, name="passkey locale", public_key_spki=spki)
    return priv, rig.world.backend.confirm_device_cli(dev.code)


def _ok(rig):  # noqa: F811
    return rig.world.svc.local_passkey_ok


def _internal_card(rig, **over):  # noqa: F811
    """Sur Lokkal, toutes les cartes sont dans le locataire interne (le propriétaire travaille sur sa machine)."""
    from nokker.tenancy.context import INTERNAL_TENANT
    from tests.unit.payments.conftest import pay_call, to_pay_verdict

    with tenant_scope(INTERNAL_TENANT):
        turn = Turn(thread_id=f"th_{time.time_ns()}", index=0, input=InputCheck(text="achète"), channel="api",
                    sender=ALICE)
        return rig.world.backend.request(turn, pay_call(**over), to_pay_verdict(), None)


def _strong_internal(rig):  # noqa: F811
    from nokker.tenancy.context import INTERNAL_TENANT

    with tenant_scope(INTERNAL_TENANT):
        turn = Turn(thread_id=f"th_{time.time_ns()}", index=0, input=InputCheck(text="x"), channel="api", sender=ALICE)
        return rig.world.backend.request(turn, ToolCall(id="c-x", name="terminal", arguments={"command": "rm -rf /d"}),
                                         Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1,
                                                 rule="effect:destructive", reason="x", strength="strong"), None)


# ───────────────────────── décision locale ─────────────────────────

def test_own_card_to_pay_card_is_signed_with_the_local_passkey(rig, local_key):  # noqa: F811
    rig.client.put_card(ALICE, dict(CARD))
    card = _internal_card(rig)
    priv, dev = local_key
    b = rig.world.backend
    from nokker.tenancy.context import INTERNAL_TENANT

    with tenant_scope(INTERNAL_TENANT):
        assert rig.world.svc.local_passkey_ok(card)
        ch = b.challenge(card.id).canonical()
        out = b.decide_local_passkey(card.id, "approve", local_proof(priv, dev.id, ch), origin=LOCAL,
                                     local_ok=_ok(rig), owner=ALICE)
        rec = rig.world.svc.store.by_approval(card.id)
    assert out.status in ("approved", "executed") and out.decided_via == "local-passkey"
    assert rec.status == "signed"
    check = verify_envelope(rec.envelope, now=time.time(), trusted_spki=dev.public_key_spki, expected_origin=LOCAL)
    assert check.valid  # l'agent de paiement revérifie la même enveloppe avec l'origine locale


# ───────────────────────── garde-fou : rien d'autre ne se signe en local ─────────────────────────

def test_a_non_payment_strong_card_is_never_signed_locally(rig, local_key):  # noqa: F811
    from nokker.tenancy.context import INTERNAL_TENANT

    b = rig.world.backend
    priv, dev = local_key
    strong = _strong_internal(rig)
    with tenant_scope(INTERNAL_TENANT):
        ch = b.challenge(strong.id).canonical()
        with pytest.raises(NotLocalApproval):
            b.decide_local_passkey(strong.id, "approve", local_proof(priv, dev.id, ch), origin=LOCAL,
                                   local_ok=_ok(rig), owner=ALICE)
        assert b.get(strong.id).status == "pending"


def test_a_payment_card_that_is_not_own_card_is_never_signed_locally(rig, local_key):  # noqa: F811
    from nokker.tenancy.context import INTERNAL_TENANT

    rig.world.svc.agent_settings = kagent.AgentSettings.from_config({"url": "http://pay-agent:8790", "issuer": "wise"})
    card = _internal_card(rig)
    priv, dev = local_key
    b = rig.world.backend
    with tenant_scope(INTERNAL_TENANT):
        assert not rig.world.svc.local_passkey_ok(card)
        ch = b.challenge(card.id).canonical()
        with pytest.raises(NotLocalApproval):
            b.decide_local_passkey(card.id, "approve", local_proof(priv, dev.id, ch), origin=LOCAL,
                                   local_ok=_ok(rig), owner=ALICE)


def test_another_users_passkey_or_a_cloud_tenant_card_is_refused(rig, local_key):  # noqa: F811
    from nokker.tenancy.context import INTERNAL_TENANT

    rig.client.put_card(ALICE, dict(CARD))
    b = rig.world.backend
    priv, dev = local_key
    card = _internal_card(rig)
    with tenant_scope(INTERNAL_TENANT):
        ch = b.challenge(card.id).canonical()
        with pytest.raises(ProofInvalid):  # la passkey d'ALICE n'est pas celle du propriétaire « local-owner »
            b.decide_local_passkey(card.id, "approve", local_proof(priv, dev.id, ch), origin=LOCAL,
                                   local_ok=lambda a: True, owner="local-owner")
    cloud = rig.world.request(ALICE)  # carte d'un locataire cloud (tn_alice) : jamais signée par cette voie
    with tenant_scope(TENANT[ALICE]):
        ch = b.challenge(cloud.id).canonical()
        with pytest.raises(ProofInvalid):
            b.decide_local_passkey(cloud.id, "approve", local_proof(priv, dev.id, ch), origin=LOCAL,
                                   local_ok=lambda a: True, owner=ALICE)
        assert b.get(cloud.id).status == "pending"


@pytest.mark.parametrize("case", ["cloud_origin_param", "assertion_from_cloud", "wrong_rp", "no_uv", "replay"])
def test_local_decision_refuses_any_other_origin_or_a_weak_proof(rig, local_key, case):  # noqa: F811
    from nokker.tenancy.context import INTERNAL_TENANT

    rig.client.put_card(ALICE, dict(CARD))
    card = _internal_card(rig)
    priv, dev = local_key
    b = rig.world.backend
    with tenant_scope(INTERNAL_TENANT):
        ch = b.challenge(card.id).canonical()
        kw = {"assertion_from_cloud": {"origin": "https://app.example.ch"}, "wrong_rp": {"rp_id": "app.example.ch"},
              "no_uv": {"uv": False}}.get(case, {})
        proof = local_proof(priv, dev.id, ch, **kw)
        origin = "https://app.example.ch" if case == "cloud_origin_param" else LOCAL
        if case == "replay":
            b.decide_local_passkey(card.id, "reject", local_proof(priv, dev.id, ch, "reject"), origin=LOCAL,
                                   local_ok=_ok(rig), owner=ALICE)
        with pytest.raises(Exception) as e:
            b.decide_local_passkey(card.id, "approve", proof, origin=origin, local_ok=_ok(rig), owner=ALICE)
        assert isinstance(e.value, (ProofInvalid,)) or type(e.value).__name__ in ("ApprovalNotPending",)
        if case != "replay":
            assert b.get(card.id).status == "pending"


def test_the_local_token_alone_never_approves_a_payment(rig):  # noqa: F811
    rig.client.put_card(ALICE, dict(CARD))
    card = rig.world.request(ALICE)
    with tenant_scope(TENANT[ALICE]), pytest.raises(ProofInvalid):
        rig.world.backend.decide_local(card.id, "approve", "local-owner")


# ───────────────────────── enregistrement de la passkey locale ─────────────────────────

class _Approvals:
    """Le strict nécessaire de BaseApprovals pour LocalPasskeys."""

    def __init__(self):
        from nokker.approvals.memory import InMemoryApprovals

        self.b = InMemoryApprovals(webauthn_origin=LOCAL)

    def __getattr__(self, name):
        return getattr(self.b, name)


def _registration(lp, priv, *, origin=LOCAL, typ="webauthn.create", alg=-7, challenge=None):
    opts = lp.options()
    ch = challenge or opts["challenge"]
    cdj = json.dumps({"type": typ, "challenge": ch, "origin": origin}).encode()
    der = priv.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return {"challenge": ch, "credential_id": b64url(b"cred-" + ch.encode()[:12]), "public_key_spki": b64url(der),
            "public_key_alg": alg, "client_data_json": b64url(cdj), "name": "Mac de test"}


def test_register_list_delete(tmp_path):
    lp = LocalPasskeys(_Approvals(), tmp_path / "local-passkeys.json", LOCAL, "local-owner")
    assert lp.list() == [] and lp.allow_credentials() == []
    priv = ec.generate_private_key(ec.SECP256R1())
    out = lp.register(_registration(lp, priv))
    assert lp.list()[0]["device_id"] == out["device_id"] and lp.allow_credentials() == [out["credential_id"]]
    dev = lp.approvals.get_device(out["device_id"])
    assert dev.user == "local-owner" and dev.status == "approved"
    assert oct((tmp_path / "local-passkeys.json").stat().st_mode)[-3:] == "600"
    assert lp.delete(out["device_id"]) is True and lp.list() == []


@pytest.mark.parametrize("bad,code", [({"origin": "https://app.example.ch"}, "proof_invalid"),
                                      ({"typ": "webauthn.get"}, "proof_invalid"),
                                      ({"alg": -257}, "unsupported"),
                                      ({"challenge": "inconnu"}, "proof_invalid")])
def test_registration_is_checked(tmp_path, bad, code):
    lp = LocalPasskeys(_Approvals(), tmp_path / "p.json", LOCAL, "local-owner")
    with pytest.raises(PasskeyError) as e:
        lp.register(_registration(lp, ec.generate_private_key(ec.SECP256R1()), **bad))
    assert e.value.code == code and lp.list() == []


def test_registration_challenge_is_single_use(tmp_path):
    lp = LocalPasskeys(_Approvals(), tmp_path / "p.json", LOCAL, "local-owner")
    body = _registration(lp, ec.generate_private_key(ec.SECP256R1()))
    lp.register(body)
    with pytest.raises(PasskeyError):
        lp.register(dict(body))


def test_local_origin_must_be_localhost(monkeypatch):
    assert local_origin({"passkey_origin": "http://localhost:9000/x"}) == "http://localhost:9000"
    for bad in ("http://127.0.0.1:7420", "https://app.nokker.ai", "http://192.168.1.2:7420"):
        with pytest.raises(ValueError):
            local_origin({"passkey_origin": bad})


# ───────────────────────── dépôt chez nokker.ai ─────────────────────────

def test_own_card_cards_are_never_deposited_at_nokker_ai(rig):  # noqa: F811
    from nokker.local.remote_approvals import RemoteApprovals

    rig.client.put_card(ALICE, dict(CARD))
    pay = rig.world.request(ALICE)
    with tenant_scope(TENANT[ALICE]):  # témoin : une carte forte ordinaire, elle, part chez nokker.ai
        turn = Turn(thread_id=f"{TENANT[ALICE]}:t9", index=0, input=InputCheck(text="x"), channel="web", sender=ALICE)
        other = rig.world.backend.request(turn, ToolCall(id="c-y", name="terminal", arguments={"command": "rm x"}),
                                          Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1,
                                                  rule="effect:destructive", reason="x", strength="strong"), None)
    deposited = []
    class Pending:  # sur Lokkal, toutes les cartes sont dans le locataire interne ; ici, les deux cartes du banc
        def pending(self):
            return [pay, other]

        def get(self, _id):
            return None

    ra = RemoteApprovals(base_url="https://app.example.ch", token=lambda: "t", account=lambda: "us_x",
                         approvals=Pending(), apply=lambda a: None, local_only=rig.world.svc.local_passkey_ok)
    ra._deposit = lambda token, a: deposited.append(a.id) or True
    with tenant_scope(TENANT[ALICE]):
        ra.sync_once()
    assert other.id in deposited and pay.id not in deposited


# ───────────────────────── routes du noyau Lokkal (jeton local) ─────────────────────────

def test_local_routes_challenge_and_decision(rig, tmp_path):  # noqa: F811
    import threading
    from http.server import ThreadingHTTPServer

    from nokker.api.server import make_handler
    from nokker.payments import http as phttp
    from nokker.tenancy.context import INTERNAL_TENANT
    from tests.unit.api.test_approvals_routes import make_kernel_with_approvals
    from tests.unit.test_local_api import _api, _req

    b = rig.world.backend
    kernel, _ = make_kernel_with_approvals(b)
    api = _api(tmp_path)
    api.passkeys = LocalPasskeys(b, tmp_path / "local-passkeys.json", LOCAL, "local-owner")  # comme en prod
    authorized = b.is_authorized
    b.is_authorized = lambda u: u == "local-owner" or authorized(u)  # extra_go_senders: [local-owner] (core-config)
    phttp.configure(rig.world.svc)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: "local-owner"}, approvals=b,
                                                               local_api=api))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        rig.client.put_card("local-owner", dict(CARD))
        from tests.unit.payments.conftest import pay_call, to_pay_verdict

        with tenant_scope(INTERNAL_TENANT):
            turn = Turn(thread_id="th_pay", index=0, input=InputCheck(text="achète"), channel="api",
                        sender="local-owner")  # Lokkal : le propriétaire envoie le tour
            card = b.request(turn, pay_call(), to_pay_verdict(), None)
            other = b.request(Turn(thread_id="th_x", index=0, input=InputCheck(text="x"), channel="api",
                                   sender=ALICE),
                              ToolCall(id="c-x", name="terminal", arguments={"command": "rm x"}),
                              Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="effect:destructive",
                                      reason="x", strength="strong"), None)
        st, body = _req(port, "/v1/local/approvals", token=api.token)
        flags = {a["id"]: a.get("local_passkey", False) for a in body["approvals"]}
        assert st == 200 and flags[card.id] is True and flags[other.id] is False
        # pas de passkey : 412 ; carte ordinaire : 409 not_local
        assert _req(port, f"/v1/local/approvals/{card.id}/challenge", token=api.token, body={})[0] == 412
        st, body = _req(port, f"/v1/local/approvals/{other.id}/challenge", token=api.token, body={})
        assert st == 409 and body["error"]["code"] == "not_local"
        # enregistrement d'une passkey locale par les routes
        priv = ec.generate_private_key(ec.SECP256R1())
        st, opts = _req(port, "/v1/local/passkeys/options", token=api.token, body={})
        reg = _registration(api.passkeys, priv, challenge=opts["challenge"])
        st, dev = _req(port, "/v1/local/passkeys", token=api.token, body=reg)
        assert st == 201
        st, ch = _req(port, f"/v1/local/approvals/{card.id}/challenge", token=api.token, body={})
        assert st == 200 and ch["rp_id"] == "localhost" and ch["allow_credentials"] == [reg["credential_id"]]
        bad = {"decision": "approve", "device_id": dev["device_id"], "challenge": ch["challenge"],
               "webauthn": local_assertion(priv, ch["challenge"], origin="https://app.example.ch")}
        st, body = _req(port, f"/v1/local/approvals/{card.id}/decision", token=api.token, body=bad)
        assert st == 403 and body["error"]["code"] == "proof_invalid"
        st, ch = _req(port, f"/v1/local/approvals/{card.id}/challenge", token=api.token, body={})
        good = {"decision": "approve", "device_id": dev["device_id"], "challenge": ch["challenge"],
                "webauthn": local_assertion(priv, ch["challenge"])}
        st, body = _req(port, f"/v1/local/approvals/{card.id}/decision", token=api.token, body=good)
        assert st in (200, 202), body
        assert body["status"] in ("approved", "executed")
        # comme `/decide` : l'outil approuvé part en fond (pay_request → mandat → agent de paiement)
        assert body["continuation"] in ("queued", "running"), body
        for _ in range(100):
            if b.get(card.id).status == "executed":
                break
            time.sleep(0.05)
        assert b.get(card.id).status == "executed"
        st, body = _req(port, f"/v1/local/approvals/{card.id}/decision", token=api.token, body=good)
        assert st == 409 and body["error"]["code"] == "not_pending"
        # la carte ordinaire n'a pas bougé : elle reste pour nokker.ai
        assert b.get(other.id).status == "pending"
    finally:
        httpd.shutdown()
        phttp.configure(None)


def test_local_owner_reaches_payment_routes_on_the_machine_only(rig, tmp_path):  # noqa: F811
    """Lokkal : /v1/payments/* au propriétaire (jeton local, locataire interne) ; sans jeton 401 ; par la passerelle 403 ;
    le rappel signé de l'agent n'est pas intercepté par la route locale."""
    import threading
    from http.server import ThreadingHTTPServer

    from nokker.api.server import make_handler
    from nokker.payments import http as phttp
    from tests.unit.api.test_approvals_routes import make_kernel_with_approvals
    from tests.unit.test_local_api import _api, _req

    b = rig.world.backend
    kernel, _ = make_kernel_with_approvals(b)
    api = _api(tmp_path)
    phttp.configure(rig.world.svc)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: "local-owner"}, approvals=b,
                                                               local_api=api))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        rig.client.put_card("local-owner", dict(CARD))
        assert _req(port, "/v1/payments/method")[0] == 401
        st, body = _req(port, "/v1/payments/method", token=api.token)
        assert st == 200, body
        assert body["method"]["last4"] == "4242" and "4242424242424242" not in json.dumps(body)
        st, body = _req(port, "/v1/payments/mandates/md_inconnu/cvv", token=api.token, body={"value": "123"})
        assert st in (404, 409), body  # route servie (rien n'est demandé pour ce mandat), pas account_required
        # liste des cartes locales : le backend Postgres exige un locataire fixé (NoTenant sinon → 502 au web)
        from nokker.tenancy.context import current_tenant

        real_pending = b.pending
        b.pending = lambda *a, **k: (current_tenant(), real_pending(*a, **k))[1]
        try:
            assert _req(port, "/v1/local/approvals", token=api.token)[0] == 200
        finally:
            b.pending = real_pending
        # rappel de l'agent : pas de jeton local, il garde sa propre vérification (HMAC) → jamais 401 « jeton local »
        st, body = _req(port, "/v1/payments/agent/events", body={"x": 1})
        assert body.get("error", {}).get("message") != "jeton local requis"
    finally:
        httpd.shutdown()
        phttp.configure(None)


def test_cdp_url_is_joined_by_ip(monkeypatch):
    """Chromium refuse un Host qui n'est ni une IP ni localhost sur son point CDP : le nom du service est résolu."""
    import socket

    from payagent.browser import cdp_by_ip

    assert cdp_by_ip("http://127.0.0.1:9223") == "http://127.0.0.1:9223"
    assert cdp_by_ip("http://localhost:9223") == "http://localhost:9223"
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("172.20.0.5", 9223))])
    assert cdp_by_ip("http://paychrome:9223") == "http://172.20.0.5:9223"
