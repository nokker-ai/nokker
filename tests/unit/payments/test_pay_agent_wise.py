"""Agent de paiement, émetteur `wise` (docs/PAY-AGENT.md §10), de bout en bout SANS réseau : noyau (mandat, outil,
routes, rappels) ↔ agent (`payagent`, routes appelées en direct) ↔ Wise (réponses de forme documentée sur
docs.wise.com le 02.11.2026, aucun appel réel) ↔ coffre (faux). Passkeys synthétiques des tests de paiements."""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from nokker.core.ports import ToolContext
from nokker.payments import agent as kagent
from nokker.payments.mandate import agent_card_block, agent_pay_of, verify_envelope
from nokker.payments.tools import PayTools
from nokker.tenancy.context import tenant_scope
from payagent import http as ahttp
from payagent.checkout import CHALLENGE, FORM, OTP, SUCCESS, host_ok, run_checkout
from payagent.config import AgentConfig
from payagent.issuers import IssuerError
from payagent.issuers.wise import Wise, from_major, to_major
from payagent.service import PayAgent, threeds_challenge
from payagent.store import DeviceKey, MemoryDevices, MemoryStore
from payagent.vault import EvervaultVault, FakeVault, VaultError
from tests.unit.payments.conftest import ALICE, BOB, CART, ORIGIN, TENANT, assertion

KTOKEN, CBSECRET = "k" * 32, "c" * 32
PROFILE_ID = 29751964  # exemple de la doc Wise
HOST = "shop.example.ch"
PERSON = {"first_name": "Alice", "last_name": "Example", "date_of_birth": "1990-01-31",
          "address_line": "Rue Exemple 1", "city": "Genève", "post_code": "1200", "country_iso3": "che",
          "email": "alice@example.com", "phone": "+41790000000"}

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PEM = _KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                      serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def signed_event(event: dict) -> tuple[bytes, dict]:
    raw = json.dumps(event).encode()
    sig = base64.b64encode(_KEY.sign(raw, padding.PKCS1v15(), hashes.SHA256())).decode()
    return raw, {"X-Signature-SHA256": sig, "X-Delivery-Id": f"dl-{time.time_ns()}"}


@dataclass
class FakeWise:
    """Transport Wise : réponses de la forme documentée ; enregistre chaque appel (sans jamais rendre de PAN)."""

    calls: list = field(default_factory=list)
    order_status: str = "COMPLETED"
    programs: list = field(default_factory=lambda: [
        {"name": "VISA_DEBIT_CONSUMER_CH_1_VIRTUAL_CARDS_API", "scheme": "VISA", "defaultCurrency": "CHF",
         "cardType": "VIRTUAL_NON_UPGRADEABLE"}])
    fail: dict = field(default_factory=dict)   # sous-chaîne de chemin -> (statut, corps, en-têtes)
    n: int = 0
    kyc: str = "PASSED"

    def __call__(self, method, url, headers, body):
        path = url.split("wise.com", 1)[-1]
        data = json.loads(body) if body and headers.get("Content-Type") == "application/json" else body
        self.calls.append((method, path, headers, data))
        for key, resp in self.fail.items():
            if key in path:
                return resp
        if path.endswith("/profiles/personal-profile"):
            return 200, {"id": PROFILE_ID, "type": "PERSONAL"}, {}
        if path.endswith("/kyc-reviews") and method == "POST":
            return 202, None, {}
        if "/kyc-reviews" in path and method == "GET" and "pageSize" in path:
            return 200, [{"id": "2f8c0a3e-0000-4000-8000-000000000001", "status": self.kyc}], {}
        if "/kyc-reviews/" in path and method == "GET":
            return 200, {"id": path.rsplit("/", 1)[-1], "status": self.kyc}, {}
        if "/kyc-reviews/" in path and method == "PATCH":
            if self.kyc != "WAITING_CUSTOMER_INPUT":
                return 200, {"status": self.kyc}, {}
            return 200, {"status": self.kyc, "link": {"value": "https://wise.example/embedded-flows/verification?token=t",
                                                      "expiresAt": "2026-11-02T22:05:00Z"}}, {}
        if path.endswith("/oauth/token"):
            return 200, {"access_token": "cc-token", "token_type": "bearer", "expires_in": 43199}, {}
        if path.endswith("/card-orders/availability"):
            return 200, {"cardPrograms": self.programs}, {}
        if path.endswith("/card-orders") and method == "POST":
            self.n += 1
            tok = f"3ab81206-d258-4caf-cea5-60067ba8{self.n:04d}"
            return 200, {"id": 100 + self.n, "status": self.order_status,
                         "cardToken": tok if self.order_status != "PLACED" else None}, {}
        if "/card-orders/" in path and method == "GET":
            return 200, {"id": int(path.rsplit("/", 1)[-1]), "status": "COMPLETED",
                         "cardToken": f"3ab81206-d258-4caf-cea5-60067ba8{self.n:04d}"}, {}
        if path.endswith("/spend-limits") or path.endswith("/spending-permissions"):
            return 200, {}, {}
        if path.endswith("/status") and method == "PUT":
            return 200, {"token": path.split("/cards/", 1)[1].split("/", 1)[0], "status": {"value": data["status"]}}, {}
        if "/cards/" in path and method == "GET" and "/transactions" not in path:
            return 200, {"token": path.rsplit("/", 1)[-1], "status": {"value": "ACTIVE"}, "lastFourDigits": "0420"}, {}
        if path.endswith("/3dsecure/challenge-result"):
            return 204, None, {}
        if "/balances" in path:
            return 200, [{"id": 1, "currency": "CHF", "type": "STANDARD",
                          "amount": {"value": 250.5, "currency": "CHF"}}], {}
        if "/transactions" in path:
            return 200, {"transactions": [{"id": 7, "state": "COMPLETED", "creationTime": "2026-11-02T10:00:00Z",
                                           "transactionAmount": {"amount": 42.9, "currency": "CHF"},
                                           "merchant": {"name": "Boutique Example"}}]}, {}
        return 404, {"code": "not_found"}, {}

    def paths(self, method=None):
        return [p for m, p, _, _ in self.calls if method is None or m == method]


@dataclass
class Rig:
    world: object
    agent: PayAgent
    wise: FakeWise
    vault: FakeVault
    client: kagent.PayAgentClient
    callbacks: list
    events: list

    def proof(self, account: str, challenge: str):
        priv, dev = self.world.keys[account]
        return {"challenge": challenge, "device_id": dev.id, **assertion(priv, challenge)}

    def link(self, account: str = ALICE):
        return self.client.link_start(account, dict(PERSON))

    def register(self, account: str = ALICE, profile: str = f"wise:{PROFILE_ID}"):
        if self.agent.store.get_link(account) is None:
            self.link(account)
        ch = kagent.method_challenge(account, profile, int(time.time()))
        return self.client.register(account, profile, self.proof(account, ch), holder_name="Alice Example")

    def signed(self, account: str = ALICE, **over):
        card = self.world.request(account, **over)
        self.world.approve(account, card.id, signer=self.world.svc.approver_of(account)[0])
        with tenant_scope(TENANT[account]):
            return card, self.world.svc.store.by_approval(card.id)

    def pay(self, account: str, card):
        with tenant_scope(TENANT[account]):
            return PayTools(payments=self.world.svc).call(
                "pay_request", CART, ToolContext(thread_id=f"{TENANT[account]}:t1", turn_index=0, channel="web",
                                                 approval_id=card.id))

    def challenge(self, card_ref: str, *, amount=129.9, currency="CHF", url="https://shop.example.ch",
                  ref="148990660", ttl=300, profile=PROFILE_ID):
        return {"schema_version": "4.0.0", "subscription_id": "ec6d37c2-4611-457a-b210-d1a5f4e354c7",
                "event_type": "cards#3ds-challenge", "sent_at": "2026-11-02T06:47:15.123Z",
                "data": {"resource": {"client_id": "nokker-test", "card_token": card_ref, "profile_id": profile,
                                      "type": "card"},
                         "challenge_expires_after": ttl, "occurred_at": "2026-11-02T06:47:15.789Z",
                         "challenge_method": "PUSH",
                         "transaction": {"reference": ref, "channel": "browser",
                                         "merchant": {"category": None, "country": "CH", "name": "BOUTIQUE EXAMPLE",
                                                      "url": url},
                                         "money_value": {"value": amount, "currency": currency}}}}

    def webhook(self, event: dict):
        raw, headers = signed_event(event)
        return ahttp.route(self.agent, "POST", "/wise/webhook", headers, raw)

    def deliver(self):
        """Rappels de l'agent → noyau (ce que fait la route signée), dans l'ordre."""
        for body in self.callbacks:
            self.world.svc.on_agent_callback(body)
        self.callbacks.clear()


@pytest.fixture
def rig(world) -> Rig:
    wise_t = FakeWise()
    devices = MemoryDevices()
    for acc, (_, dev) in world.keys.items():
        devices.keys[dev.id] = DeviceKey(id=dev.id, user=acc, public_key_spki=dev.public_key_spki, status="approved")
    cfg = AgentConfig(issuer="wise", kernel_token=KTOKEN, callback_secret=CBSECRET, webauthn_origin=ORIGIN,
                      max_amount_minor=50_000, psp_hosts=("js.psp.example",), deny_hosts=("casino.example",))
    callbacks: list = []
    vault = FakeVault()
    issuer = Wise("client-id", "s" * 32, transport=wise_t, webhook_key_pem=_PEM, sleep=lambda s: None)
    agent = PayAgent(cfg, store=MemoryStore(), devices=devices, issuer=issuer, vault=vault, notify=callbacks.append)

    def transport(method, url, headers, body):
        return ahttp.route(agent, method, url.split("pay-agent:8790", 1)[1], headers, body or b"")

    client = kagent.PayAgentClient("http://pay-agent:8790", KTOKEN, transport=transport)
    events: list = []
    world.svc.publish = lambda ev, data: events.append((ev, data))
    world.svc.agent = client
    world.svc.agent_settings = kagent.AgentSettings.from_config({"url": "http://pay-agent:8790", "issuer": "wise",
                                                                 "deny_hosts": ["casino.example"]})
    return Rig(world=world, agent=agent, wise=wise_t, vault=vault, client=client, callbacks=callbacks, events=events)


# ───────────────────────── réglages, mandat ─────────────────────────

def test_issuer_defaults_to_wise_and_rejects_unknown():
    assert kagent.AgentSettings.from_config({}).issuer == "wise"
    with pytest.raises(ValueError):
        kagent.AgentSettings.from_config({"issuer": "paypal"})


def test_card_block_is_bound_to_the_mandates_merchant():
    m = {"human_pays": False, "merchant": {"id": HOST}, "agent_pay": agent_card_block("wise", HOST)}
    assert agent_pay_of(m) == {"via": "issuer_card", "executor": "nokker-pay-agent", "issuer": "wise",
                               "merchant_host": HOST}
    assert agent_pay_of({**m, "merchant": {"id": "autre.example.ch"}}) is None
    assert agent_pay_of({**m, "agent_pay": {**m["agent_pay"], "issuer": "stripe"}}) is None
    for bad in ("localhost", "", "a b.ch", "-x.example.ch"):
        with pytest.raises(Exception):
            agent_card_block("wise", bad)


def test_agent_config_needs_wise_and_vault_settings():
    probs = " ".join(AgentConfig(issuer="wise").problems())
    assert "PAYAGENT_WISE_CLIENT_ID" in probs and "PAYAGENT_EVERVAULT" in probs and "rk_" not in probs
    assert "PAYAGENT_ISSUER" in " ".join(AgentConfig(issuer="other").problems())


# ───────────────────────── moyen : profil Wise ─────────────────────────

def test_method_is_a_wise_profile_signed_by_the_passkey(rig):
    m = rig.register()
    assert m["issuer"] == "wise" and m["kind"] == "wise_card" and m["brand"] == "VISA" and m["last4"] is None
    assert rig.wise.paths("GET")[-1] == f"/2026Q4/spend/profiles/{PROFILE_ID}/card-orders/availability"


@pytest.mark.parametrize("case", ["bad_ref", "no_holder", "foreign_challenge", "not_my_profile", "not_linked"])
def test_method_refused(rig, case):
    if case != "not_linked":
        rig.link(ALICE)
    profile = {"bad_ref": "wise:abc", "not_my_profile": "wise:12345"}.get(case, f"wise:{PROFILE_ID}")
    ch = kagent.method_challenge(ALICE, profile, int(time.time()))
    with pytest.raises(kagent.AgentUnavailable) as e:
        if case == "foreign_challenge":
            rig.client.register(BOB, profile, rig.proof(ALICE, ch), holder_name="Bob")
        else:
            rig.client.register(ALICE, profile, rig.proof(ALICE, ch),
                                holder_name=None if case == "no_holder" else "Alice")
    assert e.value.status in (400, 403) and rig.client.method(ALICE) is None


def test_profile_without_virtual_program_is_refused(rig):
    rig.wise.programs = []
    with pytest.raises(kagent.AgentUnavailable) as e:
        rig.register()
    assert e.value.code == "issuer"


# ───────────────────────── pose de la carte « à payer » ─────────────────────────

def test_mandate_names_the_card_rail_for_any_https_merchant(rig):
    rig.register()
    card = rig.world.request(ALICE)
    with tenant_scope(TENANT[ALICE]):
        rec = rig.world.svc.store.by_approval(card.id)
        out = rig.world.svc.card_json(rec)
    assert rec.mandate["human_pays"] is False
    assert rec.mandate["agent_pay"] == agent_card_block("wise", HOST)
    assert out["pay_by"] == "agent"


@pytest.mark.parametrize("url", ["http://shop.example.ch/checkout", "https://www.casino.example/pay",
                                 "https://localhost/pay"])
def test_person_pays_when_the_card_rail_does_not_apply(rig, url):
    rig.register()
    card = rig.world.request(ALICE, url=url)
    with tenant_scope(TENANT[ALICE]):
        assert rig.world.svc.store.by_approval(card.id).mandate["human_pays"] is True


def test_model_texts_name_a_single_use_card(rig):
    assert PayTools(payments=rig.world.svc).specs()[0].description == kagent.PAY_DESCRIPTION_CARD


# ───────────────────────── émission ─────────────────────────

def test_signed_mandate_issues_one_card_capped_to_the_mandate(rig):
    rig.register()
    card, rec = rig.signed()
    res = rig.pay(ALICE, card)
    assert not res.is_error
    assert res.content == kagent.AGENT_CARD_TEXT.format(mid=rec.id, merchant="Boutique Example",
                                                        amount="129.90 CHF", until=rec.mandate["valid_until"])
    posts = [(p, d, h) for m, p, h, d in rig.wise.calls if m in ("POST", "PATCH")]
    order = next(x for x in posts if x[0].endswith("/card-orders"))
    assert order[1] == {"program": "VISA_DEBIT_CONSUMER_CH_1_VIRTUAL_CARDS_API", "cardHolderName": "Alice Example",
                        "lifetimeLimit": 0}
    assert order[2]["X-idempotence-uuid"] and order[2]["Authorization"] == "Bearer cc-token"
    limits = next(x for x in posts if x[0].endswith("/spend-limits"))
    assert limits[1] == {"lifetime": {"value": {"amount": 129.9, "currency": "CHF"}},
                         "transaction": {"value": {"amount": 129.9, "currency": "CHF"}}}
    perms = next(x for x in posts if x[0].endswith("/spending-permissions"))[1]["permissions"]
    assert {"type": "ECOM", "isEnabled": True} in perms and {"type": "ATM_WITHDRAWAL", "isEnabled": False} in perms
    # le PAN part de Wise vers le coffre, jamais par l'agent : aucun appel /twcard-data depuis l'agent
    assert not [p for p in rig.wise.paths() if "twcard-data" in p]
    cap = rig.vault.captures[0]
    assert cap["details_url"].endswith("/twcard-data/v1/sensitive-card-data/details")
    assert cap["headers"]["x-tw-twcard-card-token"].startswith("3ab81206")
    with tenant_scope(TENANT[ALICE]):
        state = rig.world.svc.store.get_agent(rec.id)
    assert state["kind"] == "card" and state["status"] == "active" and state["max_amount_minor"] == 12990
    blob = json.dumps([state, rig.events, res.content])
    assert "3ab81206" not in blob and "ev:encrypted" not in blob and "vc_" not in blob


def test_issue_is_idempotent_and_one_card_per_mandate(rig):
    rig.register()
    card, rec = rig.signed()
    first = rig.client.issue(ALICE, rec.envelope)
    again = rig.client.issue(ALICE, rec.envelope)
    assert first["id"] == again["id"] and rig.wise.n == 1


@pytest.mark.parametrize("where", ["/spend-limits", "/card-orders"])
def test_issuer_failure_means_no_usable_card(rig, where):
    rig.register()
    rig.wise.fail[where] = (422, {"code": "invalid"}, {})
    card, rec = rig.signed()
    res = rig.pay(ALICE, card)
    assert "refus de l'émetteur de la carte" in res.content
    if where == "/spend-limits":  # carte créée mais plafond non posé : bloquée aussitôt
        assert any(d == {"status": "BLOCKED"} for m, p, h, d in rig.wise.calls if p.endswith("/status"))


def test_vault_down_blocks_the_new_card(rig):
    rig.register()
    rig.vault.fail = VaultError("down", "panne")
    card, rec = rig.signed()
    res = rig.pay(ALICE, card)
    assert "refus de l'émetteur de la carte" in res.content
    assert any(d == {"status": "BLOCKED"} for m, p, h, d in rig.wise.calls if p.endswith("/status"))


def test_sca_answer_is_an_error_not_a_guess(rig):
    rig.register()
    rig.wise.fail["/card-orders"] = (403, None, {"x-2fa-approval": "ott", "x-2fa-approval-result": "REJECTED"})
    with pytest.raises(IssuerError) as e:
        rig.agent.issuer.issue_card(profile_ref=f"wise:{PROFILE_ID}", currency="CHF", limit_minor=100,
                                    idempotency_key="x", vault=rig.vault, holder_name="Alice")
    assert e.value.code == "sca_required"


def test_card_waits_for_the_order_then_retries_token_once(rig):
    rig.wise.order_status = "PLACED"
    rig.register()
    card, rec = rig.signed()
    assert rig.client.issue(ALICE, rec.envelope)["status"] == "active"
    assert any("/card-orders/1" in p for p in rig.wise.paths("GET"))


# ───────────────────────── 3-D Secure ─────────────────────────

def _issued(rig):
    rig.register()
    card, rec = rig.signed()
    rig.client.issue(ALICE, rec.envelope, kernel_ref=TENANT[ALICE])
    rig.deliver()
    c = rig.agent.store.card_by_mandate(rec.id)
    return card, rec, c


def test_challenge_is_held_for_the_persons_passkey_then_approved(rig):
    card, rec, c = _issued(rig)
    assert rig.webhook(rig.challenge(c.card_ref))[0] == 200
    rig.deliver()
    with tenant_scope(TENANT[ALICE]):
        state = rig.world.svc.store.get_agent(rec.id)
        pending = rig.world.svc.agent_threeds(ALICE, rec.id)
    assert state["status"] == "requires_action"
    assert state["threeds"]["amount_minor"] == 12990 and state["threeds"]["merchant"] == "BOUTIQUE EXAMPLE"
    assert not [p for p in rig.wise.paths() if p.endswith("/challenge-result")]  # pas d'auto-approbation
    ch = json.loads(pending["challenge"])
    assert ch["aud"] == "nokker-3ds" and ch["amount_minor"] == 12990 and ch["merchant"] == "BOUTIQUE EXAMPLE"
    with tenant_scope(TENANT[ALICE]):
        out = rig.world.svc.agent_answer_threeds(ALICE, rec.id, {"challenge_id": pending["challenge_id"],
                                                                 "proof": rig.proof(ALICE, pending["challenge"])})
    assert out["agent"]["status"] == "active" and "threeds" not in out["agent"]
    res = [d for m, p, h, d in rig.wise.calls if p.endswith("/challenge-result")]
    assert res == [{"transactionReference": "148990660", "challengeStatus": "APPROVED"}]
    assert rig.wise.calls[-1][1] == f"/2026Q4/spend/profiles/{PROFILE_ID}/3dsecure/challenge-result"


def test_mandate_signature_is_not_a_3ds_answer(rig):
    card, rec, c = _issued(rig)
    rig.webhook(rig.challenge(c.card_ref))
    pending = rig.agent.threeds(ALICE, rec.id)
    mandate_proof = rec.envelope["proof"]
    with pytest.raises(kagent.AgentUnavailable) as e:
        rig.client.answer_threeds(ALICE, rec.id, challenge_id=pending["challenge_id"], proof=mandate_proof)
    assert e.value.code == "proof"
    # défi d'un autre montant signé : refusé aussi
    other = json.loads(pending["challenge"])
    other["amount_minor"] = 1
    forged = json.dumps(other, sort_keys=True, separators=(",", ":"))
    with pytest.raises(kagent.AgentUnavailable):
        rig.client.answer_threeds(ALICE, rec.id, challenge_id=pending["challenge_id"],
                                  proof=rig.proof(ALICE, forged))
    with pytest.raises(kagent.AgentUnavailable):  # passkey de bob sur le défi d'alice
        rig.client.answer_threeds(ALICE, rec.id, challenge_id=pending["challenge_id"],
                                  proof=rig.proof(BOB, pending["challenge"]))
    assert not [p for p in rig.wise.paths() if p.endswith("/challenge-result")]


@pytest.mark.parametrize("over,why", [({"amount": 500.0}, "amount"), ({"currency": "EUR"}, "currency"),
                                      ({"url": "https://evil.example.net"}, "merchant"),
                                      ({"profile": 1}, "profile")])
def test_challenge_that_does_not_match_the_mandate_is_rejected_at_once(rig, over, why):
    card, rec, c = _issued(rig)
    status, out = rig.webhook(rig.challenge(c.card_ref, **over))
    assert status == 200 and out == {"rejected": why}
    res = [d for m, p, h, d in rig.wise.calls if p.endswith("/challenge-result")]
    assert res == [{"transactionReference": "148990660", "challengeStatus": "REJECTED"}]
    assert rig.agent.store.card_by_mandate(rec.id).status == "active"


def test_challenge_without_merchant_url_is_shown_as_unverified(rig):
    card, rec, c = _issued(rig)
    assert rig.webhook(rig.challenge(c.card_ref, url=None))[1]["pending"]
    assert rig.agent.threeds(ALICE, rec.id)["merchant_match"] == "unverified"


def test_decline_rejects_and_late_answer_expires(rig):
    card, rec, c = _issued(rig)
    rig.webhook(rig.challenge(c.card_ref))
    p = rig.agent.threeds(ALICE, rec.id)
    rig.client.answer_threeds(ALICE, rec.id, challenge_id=p["challenge_id"], proof=None, decline=True)
    assert [d["challengeStatus"] for m, pa, h, d in rig.wise.calls if pa.endswith("/challenge-result")] == ["REJECTED"]
    rig.webhook(rig.challenge(c.card_ref, ref="2"))
    p2 = rig.agent.threeds(ALICE, rec.id)
    rig.agent.clock = lambda: time.time() + 400
    with pytest.raises(kagent.AgentUnavailable) as e:
        rig.client.answer_threeds(ALICE, rec.id, challenge_id=p2["challenge_id"],
                                  proof=rig.proof(ALICE, p2["challenge"]))
    assert e.value.status == 410


def test_webhook_signature_and_redelivery(rig):
    card, rec, c = _issued(rig)
    raw, headers = signed_event(rig.challenge(c.card_ref))
    assert ahttp.route(rig.agent, "POST", "/wise/webhook", {**headers, "X-Signature-SHA256": "AAAA"}, raw)[0] == 400
    assert ahttp.route(rig.agent, "POST", "/wise/webhook", headers, raw)[1]["pending"]
    assert ahttp.route(rig.agent, "POST", "/wise/webhook", headers, raw)[1] == {"duplicate": "148990660"}


def test_unknown_card_challenge_is_rejected(rig):
    rig.register()
    assert rig.webhook(rig.challenge("nope"))[1] == {"rejected": "unknown_card"}


# ───────────────────────── fin de vie de la carte ─────────────────────────

def test_completed_purchase_blocks_the_card_and_forgets_it(rig):
    card, rec, c = _issued(rig)
    ev = {"event_type": "cards#transaction-state-change",
          "data": {"resource": {"card_token": c.card_ref, "profile_id": PROFILE_ID, "type": "card"},
                   "transaction_id": 7, "transaction_type": "ECOM_PURCHASE", "transaction_state": "COMPLETED"}}
    assert rig.webhook(ev)[1] == {"handled": "transaction"}
    after = rig.agent.store.card_by_mandate(rec.id)
    assert after.status == "deactivated" and after.used and after.deactivated_reason == "used"
    assert after.vault_sealed == {} and c.vault_ref in rig.vault.forgotten
    rig.deliver()
    with tenant_scope(TENANT[ALICE]):
        assert rig.world.svc.store.get_agent(rec.id)["used"] is True


def test_revoke_and_expiry_block_the_card(rig):
    card, rec, c = _issued(rig)
    with tenant_scope(TENANT[ALICE]):
        out = rig.world.svc.agent_revoke(ALICE, rec.id)
    assert out["agent"]["status"] == "deactivated"
    card2, rec2 = rig.signed(amount="12.00")
    rig.client.issue(ALICE, rec2.envelope)
    rig.agent.clock = lambda: time.time() + 7200
    assert rig.agent.expire_due() == 1
    assert rig.agent.store.card_by_mandate(rec2.id).deactivated_reason == "expired"


def test_issuer_reads_balance_and_transactions(rig):
    rig.register()
    w = rig.agent.issuer
    assert w.balances(profile_ref=f"wise:{PROFILE_ID}")[0].amount_minor == 25050
    tx = w.transactions(profile_ref=f"wise:{PROFILE_ID}", card_ref="t")[0]
    assert tx.amount_minor == 4290 and tx.status == "COMPLETED"
    q = rig.wise.paths("GET")[-1]
    assert "fromCreationTime=" in q and "toCreationTime=" in q


def test_amount_conversions():
    assert to_major(12990, "CHF") == 129.9 and to_major(500, "JPY") == 500
    assert from_major(129.9, "CHF") == 12990 and from_major(0.1, "EUR") == 10


# ───────────────────────── navigateur de checkout ─────────────────────────

@dataclass
class FakePage:
    states: list
    page_url: str = "https://shop.example.ch/checkout"
    shown: tuple | None = (12990, "CHF")
    filled: list = field(default_factory=list)
    clicks: int = 0

    def url(self):
        return self.page_url

    def total(self):
        return self.shown

    def fill_card(self, fields):
        self.filled.append(fields)

    def submit(self):
        self.clicks += 1

    def state(self):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def screenshot(self):
        return b"png"


def test_checkout_fills_tokens_waits_through_3ds_and_marks_used(rig):
    card, rec, c = _issued(rig)
    rig.webhook(rig.challenge(c.card_ref))
    page = FakePage(states=[CHALLENGE, CHALLENGE, CHALLENGE, SUCCESS])
    out = rig.agent.checkout(ALICE, rec.id, page, sleep=lambda s: None)
    assert out["outcome"] == "paid" and page.clicks == 1
    f = page.filled[0]
    assert f.number.startswith("tok_num_") and f.allowed_hosts == (HOST, "js.psp.example")
    assert rig.agent.store.card_by_mandate(rec.id).used is True


@pytest.mark.parametrize("page,outcome", [
    (FakePage(states=[FORM], page_url="https://evil.example.net/pay"), "refused"),
    (FakePage(states=[FORM], shown=(99999, "CHF")), "refused"),
    (FakePage(states=[FORM], shown=(12990, "EUR")), "refused"),
    (FakePage(states=[OTP]), "handoff"),
])
def test_checkout_never_pays_outside_the_mandate(rig, page, outcome):
    card, rec, c = _issued(rig)
    out = rig.agent.checkout(ALICE, rec.id, page, sleep=lambda s: None)
    assert out["outcome"] == outcome
    if outcome == "refused":
        assert page.filled == [] and page.clicks == 0
        assert rig.agent.store.card_by_mandate(rec.id).status == "deactivated"


def test_checkout_challenge_wait_extends_to_the_3ds_deadline():
    t = [0.0]
    page = FakePage(states=[CHALLENGE] * 320 + [SUCCESS])  # au-delà des 120 s de base, avant 300 + 30
    from payagent.vault import CheckoutFields

    fields = CheckoutFields(number="n", cvc="c", expiry="e", holder="h", allowed_hosts=(HOST,), expires_at=1e9)
    out = run_checkout(page, fields=fields, merchant_host=HOST, max_amount_minor=12990, currency="CHF",
                       challenge_deadline=lambda: 300.0, clock=lambda: t[0],
                       sleep=lambda s: t.__setitem__(0, t[0] + s), settle_s=120)
    assert out.outcome == "paid" and t[0] >= 300


def test_host_check():
    assert host_ok("https://pay.shop.example.ch/x", HOST) and not host_ok("http://shop.example.ch", HOST)
    assert not host_ok("https://shop.example.ch.evil.net", HOST)


def test_evervault_skeleton_makes_no_call():
    v = EvervaultVault(app_id="app", api_key="k", function="f")
    with pytest.raises(VaultError) as e:
        v.capture_card(issuer="wise", card_ref="t", key_url="u", details_url="u", headers={})
    assert e.value.code == "not_configured"


def test_challenge_text_is_canonical(rig):
    card, rec, c = _issued(rig)
    rig.webhook(rig.challenge(c.card_ref))
    t = rig.agent.store.pending_threeds(c.id)
    assert threeds_challenge(t) == rig.agent.threeds(ALICE, rec.id)["challenge"]
    assert verify_envelope(rec.envelope, now=time.time()).valid


def test_kernel_3ds_routes_are_posts_of_the_agent_and_answer_the_approver_only(rig):
    from nokker.payments import http as phttp
    from nokker.payments.service import PaymentError

    card, rec, c = _issued(rig)
    rig.webhook(rig.challenge(c.card_ref))
    phttp.configure(rig.world.svc)
    try:
        assert phttp.is_payments_post_path(f"/v1/payments/mandates/{rec.id}/3ds")
    finally:
        phttp.configure(None)
    with tenant_scope(TENANT[ALICE]):
        status, out = phttp.dispatch(rig.world.svc, "GET", ["mandates", rec.id, "3ds"], ALICE, None, lambda: {})
        assert status == 200 and json.loads(out["challenge"])["challenge_id"] == out["challenge_id"]
        status, done = phttp.dispatch(rig.world.svc, "POST", ["mandates", rec.id, "3ds"], ALICE, None,
                                      lambda: {"challenge_id": out["challenge_id"],
                                               "proof": rig.proof(ALICE, out["challenge"])})
        assert status == 200 and done["agent"]["status"] == "active"
    with tenant_scope(TENANT[BOB]), pytest.raises(PaymentError) as e:
        rig.world.svc.agent_threeds(BOB, rec.id)
    assert e.value.status == 404


def test_agent_deny_list_says_seller_and_a_mismatched_host_is_never_sealed(rig, monkeypatch):
    from dataclasses import replace as dc_replace

    from nokker.approvals.errors import ProofInvalid

    rig.register()
    rig.agent.cfg = dc_replace(rig.agent.cfg, deny_hosts=("example.ch",))  # le noyau ne le sait pas
    card, rec = rig.signed()
    res = rig.pay(ALICE, card)
    assert kagent.AGENT_FAILED_WHY["seller"] in res.content
    # bloc qui nomme un autre hôte que le marchand du mandat : la passkey ne peut pas le sceller
    monkeypatch.setattr(rig.world.svc, "_agent_pay_for",
                        lambda cart, approver: agent_card_block("wise", "autre.example.ch"))
    with pytest.raises(ProofInvalid):
        rig.signed(amount="12.00", items=[{"name": "Filtre", "quantity": 1, "unit_price": "12.00"}])
    assert kagent.AGENT_FAILED_WHY["merchant"] == "marchand différent de celui approuvé"


# ───────────────────────── ouverture du compte émetteur (KYC Wise) ─────────────────────────

def test_link_opens_a_profile_and_starts_a_card_kyc_without_keeping_identity(rig):
    rig.wise.kyc = "WAITING_CUSTOMER_INPUT"
    link = rig.link()
    assert link["state"] == "WAITING_CUSTOMER_INPUT" and link["profile_ref"] == f"wise:{PROFILE_ID}"
    prof = next(d for m, p, h, d in rig.wise.calls if p.endswith("/profiles/personal-profile"))
    assert prof["address"]["countryIso3Code"] == "che" and prof["externalCustomerId"].startswith("nk_")
    assert ALICE not in json.dumps(prof)  # l'identifiant du compte ne part pas chez Wise
    assert any(d == {"type": "CARD"} for m, p, h, d in rig.wise.calls if p.endswith("/kyc-reviews"))
    assert "Alice" not in json.dumps(rig.agent.store.get_link(ALICE).__dict__)
    # KYC pas fini : enregistrer la carte est refusé
    ch = kagent.method_challenge(ALICE, f"wise:{PROFILE_ID}", int(time.time()))
    with pytest.raises(kagent.AgentUnavailable) as e:
        rig.client.register(ALICE, f"wise:{PROFILE_ID}", rig.proof(ALICE, ch), holder_name="Alice")
    assert e.value.status == 409 and e.value.code == "kyc"
    # lien hébergé par Wise, retour fixé par le noyau
    out = rig.client.link_url(ALICE, "https://app.example.ch/#/account?wise=1")
    assert out["url"].startswith("https://") and out["expires_at"]
    assert next(d for m, p, h, d in rig.wise.calls if m == "PATCH" and "/kyc-reviews/" in p) == \
        {"redirectUrl": "https://app.example.ch/#/account?wise=1"}
    # le webhook dit PASSED : l'enregistrement passe
    rig.wise.kyc = "PASSED"
    ev = {"schema_version": "5.0.0", "event_type": "kyc-reviews#state-change",
          "data": {"resource": {"id": "2f8c0a3e-0000-4000-8000-000000000001", "state": "PASSED",
                                "prevState": "PROCESSING", "profileId": PROFILE_ID}}}
    assert rig.webhook(ev)[1] == {"handled": "kyc"}
    assert rig.client.register(ALICE, f"wise:{PROFILE_ID}", rig.proof(ALICE, ch), holder_name="Alice")["issuer"] == "wise"


@pytest.mark.parametrize("bad", [{"email": "nope"}, {"phone": "0790000000"}, {"date_of_birth": "31.01.1990"},
                                 {"country_iso3": "ch"}, {"first_name": ""}])
def test_link_refuses_unreadable_identity(rig, bad):
    with pytest.raises(kagent.AgentUnavailable) as e:
        rig.client.link_start(ALICE, {**PERSON, **bad})
    assert e.value.status == 400 and rig.agent.store.get_link(ALICE) is None


def test_kernel_method_routes_for_wise(rig):
    from nokker.payments import http as phttp

    rig.world.svc.agent_settings = kagent.AgentSettings.from_config(
        {"url": "http://pay-agent:8790", "issuer": "wise", "link_return_url": "https://app.example.ch/#/account"})
    svc = rig.world.svc
    with tenant_scope(TENANT[ALICE]):
        st, view = phttp.dispatch(svc, "GET", ["method"], ALICE, None, lambda: {})
        assert view["issuer"] == "wise" and view["link"] is None and view["method"] is None
        with pytest.raises(Exception):  # pas de compte ouvert : pas de défi
            phttp.dispatch(svc, "POST", ["method", "challenge"], ALICE, None, lambda: {})
        st, out = phttp.dispatch(svc, "POST", ["method", "link"], ALICE, None, lambda: {"person": dict(PERSON)})
        assert out["link"]["state"] == "PASSED" and "profile_ref" not in out["link"]
        # le défi porte le profil ouvert pour CE compte, même si le web en nomme un autre
        st, ch = phttp.dispatch(svc, "POST", ["method", "challenge"], ALICE, None,
                                lambda: {"payment_method": "wise:999"})
        assert ch["payment_method"] == f"wise:{PROFILE_ID}" and f"wise:{PROFILE_ID}" in ch["challenge"]
        st, bal = phttp.dispatch(svc, "GET", ["method", "balance"], ALICE, None, lambda: {})
        assert bal == {"balances": [{"currency": "CHF", "amount_minor": 25050}]}
        rig.wise.kyc = "WAITING_CUSTOMER_INPUT"
        rig.agent.store.put_link(dc_replace_link(rig.agent.store.get_link(ALICE), state="WAITING_CUSTOMER_INPUT"))
        st, url = phttp.dispatch(svc, "POST", ["method", "link", "url"], ALICE, None, lambda: {})
        assert url["url"].startswith("https://")
    patch = [d for m, p, h, d in rig.wise.calls if m == "PATCH" and "/kyc-reviews/" in p][-1]
    assert patch == {"redirectUrl": "https://app.example.ch/#/account"}


def dc_replace_link(link, **kw):
    from dataclasses import replace as dc_replace

    return dc_replace(link, **kw)
