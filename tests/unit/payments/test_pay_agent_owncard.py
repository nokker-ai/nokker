"""Agent de paiement, `own_card` (Lokkal, docs/PAY-AGENT.md §11), de bout en bout SANS réseau ni vraie carte : noyau
(mandat, outil, routes) ↔ agent (routes appelées en direct) ↔ coffre local (faux `SecretsProvider`) ↔ checkout (fausse
page avec challenge OTP). Numéros de test publics des réseaux seulement."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import pytest

from nokker.core.ports import ToolContext
from nokker.payments import agent as kagent
from nokker.payments.mandate import agent_own_block, agent_pay_of
from nokker.payments.tools import PayTools
from nokker.tenancy.context import tenant_scope
from payagent import http as ahttp
from payagent.checkout import CHALLENGE, FORM, OTP, SUCCESS
from payagent.config import AgentConfig
from payagent.owncard import CardVault, brand_of
from payagent.service import PayAgent
from payagent.store import DeviceKey, MemoryDevices, MemoryStore
from tests.unit.payments.conftest import ALICE, BOB, CART, ORIGIN, TENANT

KTOKEN, CBSECRET = "k" * 32, "c" * 32
VISA = "4242424242424242"  # carte de test publique
CARD = {"number": "4242 4242 4242 4242", "exp_month": 12, "exp_year": 2031, "holder": "Alice Example"}


class FakeSecrets:
    kind, scope = "file", "system"

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.reads = 0

    def get(self, name):
        self.reads += 1
        return self.data.get(name)

    def set(self, name, value):
        self.data[name] = value

    def delete(self, name):
        return self.data.pop(name, None) is not None

    def list(self):
        return []


@dataclass
class Page:
    states: list
    page_url: str = "https://shop.example.ch/checkout"
    shown: tuple | None = (12990, "CHF")
    cse: bool = False
    filled: list = field(default_factory=list)
    otps: list = field(default_factory=list)
    clicks: int = 0

    def url(self):
        return self.page_url

    def total(self):
        return self.shown

    def client_side_encryption(self):
        return self.cse

    def fill_card(self, f):
        self.filled.append(f)

    def fill_otp(self, code):
        self.otps.append(code)

    def submit(self):
        self.clicks += 1

    def state(self):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def screenshot(self):
        return b"png"


@dataclass
class Rig:
    world: object
    agent: PayAgent
    secrets: FakeSecrets
    client: kagent.PayAgentClient
    callbacks: list
    pages: list

    def signed(self, account=ALICE, **over):
        card = self.world.request(account, **over)
        self.world.approve(account, card.id, signer=self.world.svc.approver_of(account)[0])
        with tenant_scope(TENANT[account]):
            return card, self.world.svc.store.by_approval(card.id)

    def pay(self, account, card):
        with tenant_scope(TENANT[account]):
            return PayTools(payments=self.world.svc).call(
                "pay_request", CART, ToolContext(thread_id=f"{TENANT[account]}:t1", turn_index=0, channel="web",
                                                 approval_id=card.id))

    def done(self, mid, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            c = self.agent.store.card_by_mandate(mid)
            if c is not None and c.status == "deactivated":
                time.sleep(0.05)  # rappels en fil à part
                return c
            time.sleep(0.01)
        raise AssertionError("checkout pas fini")

    def deliver(self):
        for body in list(self.callbacks):
            self.world.svc.on_agent_callback(body)
        self.callbacks.clear()


@pytest.fixture
def rig(world) -> Rig:
    devices = MemoryDevices()
    for acc, (_, dev) in world.keys.items():
        devices.keys[dev.id] = DeviceKey(id=dev.id, user=acc, public_key_spki=dev.public_key_spki, status="approved")
    cfg = AgentConfig(issuer="own_card", kernel_token=KTOKEN, callback_secret=CBSECRET, webauthn_origin=ORIGIN)
    secrets_ = FakeSecrets()
    callbacks, pages = [], []

    def open_page(url):
        page = pages.pop(0)
        return page

    agent = PayAgent(cfg, store=MemoryStore(), devices=devices, cards=CardVault(secrets_), open_page=open_page,
                     notify=callbacks.append, sleep=lambda s: time.sleep(0.01), background=True)

    def transport(method, url, headers, body):
        return ahttp.route(agent, method, url.split("pay-agent:8790", 1)[1], headers, body or b"")

    client = kagent.PayAgentClient("http://pay-agent:8790", KTOKEN, transport=transport)
    world.svc.publish = lambda ev, data: None
    world.svc.agent = client
    world.svc.agent_settings = kagent.AgentSettings.from_config({"url": "http://pay-agent:8790", "issuer": "own_card"})
    return Rig(world=world, agent=agent, secrets=secrets_, client=client, callbacks=callbacks, pages=pages)


# ───────────────────────── enregistrement de la carte ─────────────────────────

def test_card_goes_to_the_local_vault_and_only_brand_last4_come_back(rig):
    out = rig.client.put_card(ALICE, dict(CARD))
    assert out == {**out, "brand": "visa", "last4": "4242", "exp_month": 12, "exp_year": 2031, "issuer": "own_card",
                   "cvv_kept": False}
    assert VISA in rig.secrets.data.values() and "Alice Example" in rig.secrets.data.values()
    assert not any(k.endswith(".cvv") for k in rig.secrets.data)  # défaut : CVV demandé à chaque paiement
    assert VISA not in str(rig.client.card(ALICE)) and ALICE not in "".join(rig.secrets.data)  # noms : empreinte


@pytest.mark.parametrize("bad", [{"number": "4242 4242 4242 4241"}, {"exp_year": 2020}, {"exp_month": 13},
                                 {"holder": ""}])
def test_unreadable_cards_are_refused(rig, bad):
    with pytest.raises(kagent.AgentUnavailable) as e:
        rig.client.put_card(ALICE, {**CARD, **bad})
    assert e.value.status == 400 and not rig.secrets.data


def test_cvv_mode_kept_or_asked(rig):
    rig.client.put_card(ALICE, dict(CARD))
    assert rig.client.cvv_mode(ALICE, True, "123") == {"cvv_kept": True}
    assert rig.client.card(ALICE)["cvv_kept"] is True
    assert rig.client.cvv_mode(ALICE, False) == {"cvv_kept": False}
    assert not any(k.endswith(".cvv") for k in rig.secrets.data)


def test_remove_forgets_the_card(rig):
    rig.client.put_card(ALICE, dict(CARD))
    assert rig.client.remove(ALICE) is True and not rig.secrets.data and rig.client.card(ALICE) is None


def test_brands():
    assert brand_of(VISA) == "visa" and brand_of("5555555555554444") == "mastercard" and brand_of("378282246310005") == "amex"


# ───────────────────────── mandat et texte du modèle ─────────────────────────

def test_mandate_names_own_card_and_model_texts(rig):
    rig.client.put_card(ALICE, dict(CARD))
    card = rig.world.request(ALICE)
    with tenant_scope(TENANT[ALICE]):
        rec = rig.world.svc.store.by_approval(card.id)
    assert rec.mandate["agent_pay"] == agent_own_block("shop.example.ch") and agent_pay_of(rec.mandate)
    assert PayTools(payments=rig.world.svc).specs()[0].description == kagent.PAY_DESCRIPTION_OWN


# ───────────────────────── checkout ─────────────────────────

def test_kept_cvv_pays_with_no_prompt(rig):
    rig.client.put_card(ALICE, {**CARD, "keep_cvv": True, "cvv": "123"})
    page = Page(states=[SUCCESS])
    rig.pages.append(page)
    card, rec = rig.signed()
    res = rig.pay(ALICE, card)
    assert res.content == kagent.AGENT_OWN_TEXT.format(mid=rec.id, merchant="Boutique Example", amount="129.90 CHF",
                                                       until=rec.mandate["valid_until"])
    assert "4242" not in res.content
    st = rig.done(rec.id)
    f = page.filled[0]
    assert f.number == VISA and f.cvc == "123" and f.expiry == "12/2031" and page.clicks == 1
    assert st.used and st.status == "deactivated" and st.deactivated_reason == "used"


def _wait_prompt(rig, mid, kind):
    for _ in range(500):
        p = rig.agent._prompts.get(mid)
        if p and p["kind"] == kind:
            return p
        time.sleep(0.01)
    raise AssertionError(f"pas de demande {kind}")


def test_asked_cvv_then_bank_code_typed_by_the_component(rig):
    rig.client.put_card(ALICE, dict(CARD))
    page = Page(states=[OTP, SUCCESS])
    rig.pages.append(page)
    card, rec = rig.signed()
    rig.pay(ALICE, card)
    _wait_prompt(rig, rec.id, "cvv")
    with tenant_scope(TENANT[ALICE]):  # par la route du noyau, comme le web
        assert rig.world.svc.agent_provide(ALICE, rec.id, "cvv", {"value": "321"}) == {"accepted": True}
    _wait_prompt(rig, rec.id, "otp")
    with pytest.raises(kagent.AgentUnavailable):  # pas de CVV hors demande
        rig.client.provide(ALICE, rec.id, "cvv", "999")
    with pytest.raises(kagent.AgentUnavailable):  # BOB ne répond pas pour ALICE
        rig.client.provide(BOB, rec.id, "otp", "482913")
    rig.client.provide(ALICE, rec.id, "otp", "482913")
    assert rig.done(rec.id).used
    assert page.filled[0].cvc == "321" and page.otps == ["482913"]
    kinds = [c.get("prompt", {}).get("kind") for c in rig.callbacks if c["event"] == "prompt"]
    assert kinds == ["cvv", "otp"]
    assert not any("321" in str(c) or "482913" in str(c) for c in rig.callbacks)  # jamais une valeur dans un rappel
    rig.deliver()


def test_bank_app_confirmation_waits_without_touching(rig):
    rig.client.put_card(ALICE, {**CARD, "keep_cvv": True, "cvv": "123"})
    page = Page(states=[CHALLENGE, CHALLENGE, CHALLENGE, SUCCESS])
    rig.pages.append(page)
    card, rec = rig.signed()
    rig.pay(ALICE, card)
    rig.done(rec.id)
    assert page.clicks == 1 and page.otps == []
    assert any(c.get("prompt", {}).get("kind") == "bank_app" for c in rig.callbacks)
    rig.deliver()
    with tenant_scope(TENANT[ALICE]):
        assert "prompt" not in (rig.world.svc.store.get_agent(rec.id) or {})


@pytest.mark.parametrize("page,outcome", [
    (Page(states=[FORM], shown=(99999, "CHF")), "checkout_refused"),
    (Page(states=[FORM], page_url="https://evil.example.net/pay"), "checkout_refused"),
    (Page(states=[FORM], cse=True), "handoff"),
])
def test_nothing_is_read_from_the_vault_before_the_checks(rig, page, outcome):
    rig.client.put_card(ALICE, {**CARD, "keep_cvv": True, "cvv": "123"})
    rig.pages.append(page)
    card, rec = rig.signed()
    reads = rig.secrets.reads
    rig.pay(ALICE, card)
    assert rig.done(rec.id).deactivated_reason == outcome
    assert page.filled == [] and page.clicks == 0 and rig.secrets.reads == reads


def test_no_bank_code_in_time_hands_over(rig):
    rig.client.put_card(ALICE, {**CARD, "keep_cvv": True, "cvv": "123"})
    page = Page(states=[OTP])
    rig.pages.append(page)
    real_ask = rig.agent._ask
    rig.agent._ask = lambda c, kind, ttl: real_ask(c, kind, 0.05)
    card, rec = rig.signed()
    rig.pay(ALICE, card)
    assert rig.done(rec.id).deactivated_reason == "handoff" and page.otps == []


def test_local_passkey_origin_is_accepted_for_own_card_only():
    probs = lambda **kw: " ".join(AgentConfig(**kw).problems())  # noqa: E731
    assert "PAYAGENT_WEBAUTHN_ORIGIN" not in probs(issuer="own_card", webauthn_origin="http://localhost:8080")
    assert "PAYAGENT_WEBAUTHN_ORIGIN" in probs(issuer="own_card", webauthn_origin="http://192.168.1.5:8080")
    assert "PAYAGENT_WEBAUTHN_ORIGIN" in probs(issuer="wise", webauthn_origin="http://localhost:8080")


def test_own_card_model_texts_are_word_for_word():
    """Textes validés le 03.11.2026 : toute modification passe par une nouvelle validation."""
    assert kagent.PAY_DESCRIPTION_OWN == (
        "Arrêt au paiement : prépare une carte « à payer » (marchand, montant, devise, articles, lien) ; la personne "
        "approuve avec sa passkey. Elle paie alors avec sa propre carte : son agent de paiement effectue le paiement "
        "chez ce marchand, au plus le montant approuvé ; s'il ne peut pas, la personne paie elle-même. Ne saisis jamais "
        "de numéro de carte ni de code.")
    assert kagent.PAY_DESCRIPTION_OWN_CHOICE == (
        "Arrêt au paiement : dès qu'une demande mène à payer (commande, achat, facture, abonnement), appelle cet outil "
        "au lieu de répondre en texte ou de demander une confirmation : la carte EST la confirmation. Prépare une carte "
        "« à payer » (marchand, montant, devise, articles, lien) ; la personne approuve avec sa passkey. Elle paie alors "
        "avec sa propre carte : son agent de paiement effectue le paiement chez ce marchand, au plus le montant "
        "approuvé ; s'il ne peut pas, la personne paie elle-même. Ne saisis jamais de numéro de carte ni de code, et ne "
        "confie jamais un paiement à un sous-agent.")
    assert kagent.TO_PAY_OWN_REASON == (
        "à payer : {amount} chez {merchant}. La personne approuve avec sa passkey ; elle paie alors avec sa propre "
        "carte, via son agent de paiement, au plus ce montant.")
    assert kagent.AGENT_OWN_TEXT == (
        "Mandat {mid} signé : la personne paie {merchant} avec sa propre carte, au plus {amount} ; son agent de "
        "paiement effectue le paiement. Si sa banque demande un code ou une confirmation, la personne le fait dans "
        "Nokker ou dans l'appli de sa banque, hors de cette conversation. Rien n'est encore payé. Ne saisis aucun moyen "
        "de paiement ni aucun code, et ne touche plus au paiement.")


def test_pay_tool_choice_picks_the_own_card_variant_only_for_own_card(rig):
    on = PayTools(payments=rig.world.svc, tool_choice=True).specs()[0].description
    off = PayTools(payments=rig.world.svc, tool_choice=False).specs()[0].description
    assert on == kagent.PAY_DESCRIPTION_OWN_CHOICE and off == kagent.PAY_DESCRIPTION_OWN
    rig.world.svc.agent_settings = kagent.AgentSettings.from_config({"url": "http://pay-agent:8790", "issuer": "wise"})
    assert PayTools(payments=rig.world.svc, tool_choice=True).specs()[0].description == kagent.PAY_DESCRIPTION_CARD


def test_a_browser_error_ends_the_payment_instead_of_hanging(rig, caplog):
    """Une erreur du navigateur pendant le checkout : la carte se désactive (checkout_error) et la personne est
    prévenue ; rien de la carte dans le journal (le message de l'erreur peut citer la valeur d'un champ)."""
    class Broken(Page):
        def fill_card(self, f):
            raise RuntimeError(f"fill failed for {f.number}")

    rig.client.put_card(ALICE, {**CARD, "keep_cvv": True, "cvv": "123"})
    rig.pages.append(Broken(states=[SUCCESS]))
    card, rec = rig.signed()
    rig.pay(ALICE, card)
    st = rig.done(rec.id)
    assert st.status == "deactivated" and st.deactivated_reason == "checkout_error" and not st.used
    assert VISA not in caplog.text and "RuntimeError" in caplog.text
    rig.deliver()


def test_the_cart_link_goes_to_the_checkout_page_before_any_check(rig):
    """Le lien de `pay_request` est le panier : le composant passe à la page de paiement (même hôte) AVANT de
    vérifier le total et de lire la carte."""
    order = []

    class Cart(Page):
        def to_checkout(self):
            order.append("hop")
            return True

        def total(self):
            order.append("total")
            return super().total()

    rig.client.put_card(ALICE, {**CARD, "keep_cvv": True, "cvv": "123"})
    page = Cart(states=[SUCCESS])
    rig.pages.append(page)
    card, rec = rig.signed()
    rig.pay(ALICE, card)
    assert rig.done(rec.id).deactivated_reason == "used"
    assert order[0] == "hop" and "total" in order
