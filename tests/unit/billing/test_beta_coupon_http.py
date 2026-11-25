"""Coupon bêta PAR COMPTE (C16, décision du 15.11.2026) par les VRAIS serveurs : console opérateur (`serve_admin`) qui
pose l'éligibilité à l'invitation, front1 en mémoire qui la porte jusqu'au redeem, création du compte, puis API du noyau
(`make_handler`) : plan, Checkout, webhook Stripe signé. Stripe est un faux client (aucun appel réseau, aucun objet
Stripe créé) ; les adresses sont fictives.

Règles vérifiées : offre Solo seulement ; éligibilité manuelle, posée à l'invitation ; usage unique lié au compte,
consommé au webhook ; webhook rejoué sans double consommation ; compte non éligible au plein tarif avec un message clair.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

import pytest

from nokker.admin.overview import MemoryOverview
from nokker.admin.server import serve_admin
from nokker.admin.store import MemoryAdminStore, normalize_application
from nokker.api.server import make_handler
from nokker.approvals.memory import InMemoryApprovals
from nokker.billing import http as billing_http
from nokker.billing.beta_coupon import MemoryGrants, message
from nokker.billing.plans import plans_from_config
from nokker.billing.service import Billing
from nokker.billing.store import MemoryBilling
from nokker.billing.stripe import sign
from nokker.onboarding import terms
from nokker.onboarding.acceptances import MemoryAcceptances
from nokker.onboarding.front import MemoryFront
from nokker.onboarding.service import Onboarding
from nokker.tenancy.store import MemoryTenancy
from tests.unit.admin.test_admin import RAW, post, verifier
from tests.unit.api.test_server import make_kernel
from tests.unit.approvals.conftest import gen_keypair
from tests.unit.billing.test_service import NOW, SECRET, FakeStripe
from tests.unit.team.test_http import _session

PRICES = {"solo": "price_solo", "famille": "price_famille", "byok": "price_byok"}
COUPON = "COUPON_TEST_SOLO6"  # id de test : jamais un coupon live


@pytest.fixture
def world():
    # ── console opérateur + front1 : deux candidatures, une seule invitée avec l'éligibilité ──
    store = MemoryAdminStore()
    store.seed_operators({"bruno@example.ch": "ops", "lire@example.ch": "lecteur"}, "system:config")
    raw2 = {**RAW[0], "id": 2, "prenom": "Bob", "email": "bob@example.org"}
    store.import_applications("front1", [normalize_application(r) for r in (RAW[0], raw2)], "system:beta-pull")
    front = MemoryFront({1: {"email": "alice@example.org", "prenom": "Alice", "lang": "fr"},
                         2: {"email": "bob@example.org", "prenom": "Bob", "lang": "fr"}})
    tenancy, approvals, grants = MemoryTenancy(), InMemoryApprovals(), MemoryGrants()
    overview = MemoryOverview()  # comptes vus par la console (lignes ajoutées à chaque création)
    admin = serve_admin(store, verifier(), "127.0.0.1", 0, front, overview=overview, beta_grants=grants)
    console = f"http://127.0.0.1:{admin.server_address[1]}"

    # ── noyau : facturation (faux Stripe, coupon de test), onboarding, API ──
    stripe = FakeStripe()
    bill = Billing(store=MemoryBilling(), stripe=stripe, plans=plans_from_config(None), prices=PRICES,
                   webhook_secret=SECRET, app_url="https://app.nokker.ai/", clock=lambda: NOW,
                   beta_coupon=COUPON, grants=grants)
    billing_http.configure(bill)
    onboarding = Onboarding(front, tenancy, approvals, MemoryAcceptances(), beta_grants=lambda: bill.grants)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), {}, approvals=approvals,
                                                               tenancy=tenancy, open_api=False, internal_tenant=False))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def api(method, path, body=None, token=None, raw=None, headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        if token:
            h["Authorization"] = f"Bearer {token}"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=h, data=data)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                out = r.read()
                return r.status, json.loads(out) if out else None
        except urllib.error.HTTPError as e:
            out = e.read()
            return e.code, json.loads(out) if out else None

    def invite_and_join(candidature, beta_coupon):
        body = {"lang": "fr", **({"beta_coupon": True} if beta_coupon else {})}
        st, sent = post(console, f"/admin/v1/applications/{candidature}/invite", body, email="bruno@example.ch")
        assert st == 200 and sent["beta_coupon"] is beta_coupon
        token = next(t for t, v in front.tokens.items() if v["id"] == sent["invitation_id"])
        out = onboarding.accept(token, terms.load("fr").sha256, gen_keypair()[1], "Portable", "198.51.100.7")
        account = tenancy.account(out["account_id"])
        assert account.invitation_id == str(sent["invitation_id"])
        overview.account_rows.append({"id": account.account_id, "tenant_id": account.tenant_id,
                                      "invitation_id": account.invitation_id})
        return account, _session(approvals, account.account_id)

    def old_account(email):  # compte né avant C16 : pas d'invitation éligible
        account = tenancy.create_account(email)
        overview.account_rows.append({"id": account.account_id, "tenant_id": account.tenant_id, "invitation_id": None})
        return account, _session(approvals, account.account_id)

    def grant_later(account_id, email="bruno@example.ch", **kw):
        return post(console, f"/admin/v1/accounts/{account_id}/beta-coupon", {}, email=email, **kw)

    def hook(eid, etype, obj):
        payload = json.dumps({"id": eid, "type": etype, "created": NOW, "data": {"object": obj}}).encode()
        return api("POST", "/v1/billing/webhook", raw=payload, headers={"Stripe-Signature": sign(payload, SECRET, NOW)})

    yield {"api": api, "hook": hook, "join": invite_and_join, "grants": grants, "stripe": stripe, "store": store,
           "old": old_account, "grant_later": grant_later}
    httpd.shutdown()
    admin.shutdown()
    billing_http.configure(None)


def _beta_sessions(stripe):
    return [c for c in stripe.calls if c[0] == "checkout_beta"]


def test_eligibilite_posee_a_l_invitation_seulement_par_l_operateur(world):
    alice, ta = world["join"](1, True)
    bob, tb = world["join"](2, False)
    assert world["grants"].get(alice.invitation_id)["state"] == "granted"
    assert world["grants"].get(bob.invitation_id) is None  # pas coché à l'invitation : pas d'offre, jamais
    sent = [j for j in world["store"].journal if j["action"] == "invitation.sent"]
    assert [json.dumps(j, default=str).count('"beta_coupon": true') for j in sent] == [1, 0]  # journal de l'opérateur
    st, plan = world["api"]("GET", "/v1/billing/plan", token=ta)
    assert st == 200 and plan["beta_offer"] == {"plan": "solo", "months": 6, "state": "available"}
    assert world["api"]("GET", "/v1/billing/plan", token=tb)[1]["beta_offer"]["state"] == "none"


def test_compte_non_eligible_plein_tarif_et_message_clair(world):
    _, tb = world["join"](2, False)
    st, out = world["api"]("POST", "/v1/billing/checkout", {"plan": "solo"}, token=tb)
    assert st == 200 and out["beta_coupon"] == "not_eligible"
    assert out["message"] == message("not_eligible", "fr") and "plein tarif" in out["message"]
    assert out["url"] == "https://checkout.stripe.test/s" and _beta_sessions(world["stripe"]) == []


def _chf(cents):
    return {"currency": "chf", "unit_amount": cents, "recurring": {"interval": "month"}}


def test_coupon_refuse_sur_une_autre_offre(world):
    alice, ta = world["join"](1, True)
    # prix lus sur Stripe (seule source), noms pris dans la table des plans : rien d'écrit en dur dans le message
    world["stripe"].price_objs.update({"price_famille": _chf(4900), "price_byok": _chf(950)})
    expected = {"famille": "L'offre bêta (6 mois offerts) ne vaut que sur Solo : sur Famille, tu paies le tarif "
                           "normal (49 CHF/mois).",
                "byok": "L'offre bêta (6 mois offerts) ne vaut que sur Solo : sur BYOK, tu paies le tarif "
                        "normal (9,50 CHF/mois)."}
    for plan, text in expected.items():
        st, out = world["api"]("POST", "/v1/billing/checkout", {"plan": plan}, token=ta)
        assert st == 200 and out["beta_coupon"] == "other_plan" and out["message"] == text
    assert _beta_sessions(world["stripe"]) == []  # aucune session remisée
    assert world["grants"].get(alice.invitation_id)["state"] == "granted"  # l'offre Solo reste entière


def test_coupon_consomme_une_seule_fois_au_webhook(world):
    alice, ta = world["join"](1, True)
    st, out = world["api"]("POST", "/v1/billing/checkout", {"plan": "solo"}, token=ta)
    assert st == 200 and out["beta_coupon"] == "applied"
    (call,) = _beta_sessions(world["stripe"])
    assert call[3:] == ("solo", COUPON, alice.invitation_id)
    g = world["grants"].get(alice.invitation_id)
    assert g["state"] == "reserved" and world["api"]("GET", "/v1/billing/plan", token=ta)[1]["beta_offer"]["state"] == "pending"
    # le retour du navigateur ne consomme rien : seul le webhook de CETTE session le fait
    session = g["checkout_session"]
    obj = {"id": session, "customer": "cus_1", "metadata": {"beta_grant": alice.invitation_id, "plan": "solo"}}
    assert world["hook"]("evt_done_1", "checkout.session.completed", obj) == (200, {"received": True})
    g = world["grants"].get(alice.invitation_id)
    assert g["state"] == "consumed" and g["consumed_event"] == "evt_done_1"
    assert world["api"]("GET", "/v1/billing/plan", token=ta)[1]["beta_offer"]["state"] == "used"
    # un second Checkout Solo du même compte : plein tarif, l'offre a servi
    st, out = world["api"]("POST", "/v1/billing/checkout", {"plan": "solo"}, token=ta)
    assert out["beta_coupon"] == "used" and "déjà servi" in out["message"]
    assert len(_beta_sessions(world["stripe"])) == 1


def test_webhook_rejoue_sans_double_consommation(world, caplog):
    alice, ta = world["join"](1, True)
    world["api"]("POST", "/v1/billing/checkout", {"plan": "solo"}, token=ta)
    session = world["grants"].get(alice.invitation_id)["checkout_session"]
    obj = {"id": session, "customer": "cus_1", "metadata": {"beta_grant": alice.invitation_id}}
    for _ in range(3):  # même événement rejoué par Stripe : idempotent (event.id déjà vu)
        assert world["hook"]("evt_done_1", "checkout.session.completed", obj)[0] == 200
    # autre id d'événement pour la même session (double livraison) : rien de plus
    assert world["hook"]("evt_done_2", "checkout.session.completed", obj)[0] == 200
    g = world["grants"].get(alice.invitation_id)
    assert g["state"] == "consumed" and g["consumed_event"] == "evt_done_1" and g["checkout_session"] == session
    # session expirée APRÈS consommation : ne rend rien
    assert world["hook"]("evt_exp_1", "checkout.session.expired", obj)[0] == 200
    assert world["grants"].get(alice.invitation_id)["state"] == "consumed"
    # une autre session remisée qui aboutirait quand même : comptée nulle part, journalisée
    other = {**obj, "id": "cs_autre"}
    with caplog.at_level("ERROR", logger="nokker.billing"):
        assert world["hook"]("evt_done_3", "checkout.session.completed", other)[0] == 200
    assert world["grants"].get(alice.invitation_id)["consumed_event"] == "evt_done_1"
    assert any("déjà consommé" in r.getMessage() for r in caplog.records)


def test_session_expiree_rend_l_offre_et_reprise_expire_l_ancienne(world):
    alice, ta = world["join"](1, True)
    world["api"]("POST", "/v1/billing/checkout", {"plan": "solo"}, token=ta)
    first = world["grants"].get(alice.invitation_id)["checkout_session"]
    world["api"]("POST", "/v1/billing/checkout", {"plan": "solo"}, token=ta)  # nouvel essai : l'ancienne expire
    assert ("expire", first) in world["stripe"].calls
    second = world["grants"].get(alice.invitation_id)["checkout_session"]
    assert second != first
    meta = {"beta_grant": alice.invitation_id}
    # l'expiration de l'ANCIENNE session ne rend pas la réservation de la nouvelle
    world["hook"]("evt_exp_a", "checkout.session.expired", {"id": first, "metadata": meta})
    assert world["grants"].get(alice.invitation_id)["state"] == "reserved"
    world["hook"]("evt_exp_b", "checkout.session.expired", {"id": second, "metadata": meta})
    assert world["grants"].get(alice.invitation_id)["state"] == "granted"
    assert world["api"]("GET", "/v1/billing/plan", token=ta)[1]["beta_offer"]["state"] == "available"


def test_l_offre_est_liee_au_compte_et_ne_se_transmet_pas(world):
    """Un webhook ne peut consommer que l'attribution qu'il nomme ; un autre compte, même avec la même offre posée
    ailleurs, garde la sienne entière."""
    alice, ta = world["join"](1, True)
    bob, tb = world["join"](2, False)
    world["grants"].grant(bob.invitation_id, "test", datetime.fromtimestamp(NOW, tz=timezone.utc))
    world["api"]("POST", "/v1/billing/checkout", {"plan": "solo"}, token=ta)
    session = world["grants"].get(alice.invitation_id)["checkout_session"]
    world["hook"]("evt_a", "checkout.session.completed",
                  {"id": session, "metadata": {"beta_grant": alice.invitation_id}})
    assert world["grants"].get(alice.invitation_id)["state"] == "consumed"
    assert world["grants"].get(bob.invitation_id)["state"] == "granted"
    assert world["api"]("GET", "/v1/billing/plan", token=tb)[1]["beta_offer"]["state"] == "available"


def test_offre_posee_apres_coup_par_la_console_idempotente_et_journalisee(world):
    """Comptes nés avant C16 (ou invitation non cochée) : l'opérateur pose l'offre après coup ; même cycle, même usage
    unique ; jamais une seconde offre pour un compte qui a déjà celle de son invitation."""
    old, to = world["old"]("ancien@example.org")
    assert world["api"]("GET", "/v1/billing/plan", token=to)[1]["beta_offer"]["state"] == "none"
    # refus : sans en-tête console, sans rôle d'écriture, compte inconnu
    assert world["grant_later"](old.account_id, console_header=False)[0] == 403
    assert world["grant_later"](old.account_id, email="lire@example.ch")[0] == 403
    assert world["grant_later"]("us_inconnu")[0] == 404
    st, out = world["grant_later"](old.account_id)
    assert st == 200 and out == {"account_id": old.account_id, "granted": True, "state": "granted"}
    st, out = world["grant_later"](old.account_id)  # rejouée : rien de neuf
    assert st == 200 and out["granted"] is False and out["state"] == "granted"
    journal = [j for j in world["store"].journal if j["action"] == "beta_coupon.grant"]
    assert len(journal) == 2 and all(j["object"] == f"account:{old.account_id}" for j in journal)
    assert world["api"]("GET", "/v1/billing/plan", token=to)[1]["beta_offer"]["state"] == "available"
    # même cycle : Checkout Solo remisé, consommé une fois au webhook
    st, out = world["api"]("POST", "/v1/billing/checkout", {"plan": "solo"}, token=to)
    assert out["beta_coupon"] == "applied"
    key = f"account:{old.account_id}"
    assert _beta_sessions(world["stripe"])[-1][-1] == key
    session = world["grants"].get(key)["checkout_session"]
    for eid in ("evt_o1", "evt_o1", "evt_o2"):
        world["hook"](eid, "checkout.session.completed", {"id": session, "metadata": {"beta_grant": key}})
    g = world["grants"].get(key)
    assert g["state"] == "consumed" and g["consumed_event"] == "evt_o1"
    assert world["grant_later"](old.account_id)[1] == {"account_id": old.account_id, "granted": False, "state": "consumed"}
    # compte dont l'invitation portait l'offre : pas de seconde offre
    alice, _ = world["join"](1, True)
    st, out = world["grant_later"](alice.account_id)
    assert st == 200 and out["granted"] is False and out["state"] == "granted"
    assert world["grants"].get(f"account:{alice.account_id}") is None


def test_message_autre_offre_en_anglais_et_sans_prix_lisible(world):
    """Compte en anglais : nom anglais de la table (Family) et prix au point ; Stripe muet : pas de prix inventé."""
    from nokker.billing.plans import plans_from_config

    plans = plans_from_config(None)
    assert plans["famille"].name("en") == "Family" and plans["famille"].name("fr") == "Famille"
    price = {"amount": 49, "currency": "CHF", "interval": "month"}
    assert message("other_plan", "en", solo="Solo", plan="Family", price=price) == (
        "The beta offer (6 months free) only applies to Solo: on Family, you pay the regular price (49 CHF/month).")
    assert message("other_plan", "fr", solo="Solo", plan="Famille", price=None) == (
        "L'offre bêta (6 mois offerts) ne vaut que sur Solo : sur Famille, tu paies le tarif normal.")
    alice, ta = world["join"](1, True)  # prix famille absent chez le faux Stripe → pas de parenthèse
    out = world["api"]("POST", "/v1/billing/checkout", {"plan": "famille"}, token=ta)[1]
    assert out["message"].endswith("sur Famille, tu paies le tarif normal.")
