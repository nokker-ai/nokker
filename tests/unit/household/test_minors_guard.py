"""Garde-fous des profils mineurs (drapeau `household_minors`) : carte forte du REPRÉSENTANT pour un envoi externe ou
une action destructive, décidée depuis SES appareils (jamais ceux du mineur) ; connecteurs et agents limités à la
liste blanche du foyer ; préfixe adapté à l'âge ; un adulte du foyer n'est pas concerné."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nokker.approvals.errors import ProofInvalid
from nokker.approvals.memory import InMemoryApprovals
from nokker.core.ports import Proof, ToolSpec
from nokker.core.turn import Decision, Effect, Gate, InputCheck, ToolCall, Turn, Verdict
from nokker.household.guard import MinorGates, TenantMinorCache, minor_tool_filter, young_prefix
from nokker.household.model import GUARDIAN_RULE, Settings, consent_text
from nokker.household.service import Household
from nokker.household.store import MemoryHouseholdStore
from nokker.team.service import Team
from nokker.tenancy.context import scoped_thread_id, tenant_scope
from nokker.tenancy.store import MemoryTenancy
from tests.unit.approvals.conftest import gen_keypair, sign_p1363, telegram_init_data

BOT = "123456:FAKE-BOT-TOKEN"
TG = 1000000001
ATT = {"version": consent_text("fr").sha256, "lang": "fr", "legal_guardian": True}


@pytest.fixture
def w():
    tenancy = MemoryTenancy()
    pay, kid, adult = (tenancy.create_account(f"{n}@example.com") for n in ("payeur", "ado", "adulte"))
    hh = Household(team=Team(store=MemoryHouseholdStore(), tenancy=tenancy), settings=Settings(minors=True))
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
        iv_k = hh.invite(pay, {"email": kid.email, "profile": "teen", "attestation": ATT})
        iv_a = hh.invite(pay, {"email": adult.email})
    with tenant_scope(kid.tenant_id):
        hh.accept(kid, iv_k["id"])
    with tenant_scope(adult.tenant_id):
        hh.accept(adult, iv_a["id"])
    return SimpleNamespace(hh=hh, pay=pay, kid=kid, adult=adult, tenancy=tenancy)


class Inner:
    """Portes du noyau d'un testeur, réduites à ce qui compte ici : l'allowlist laisse passer un envoi."""

    class invariants:
        @staticmethod
        def check_action(call, spec, turn):
            if spec.effect is Effect.SEND_BULK:
                return Verdict(gate=Gate.ACTION, decision=Decision.DENY, layer=1, rule="effect:send_bulk")
            return Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="effect:send_external:allowlist")

    def action(self, call, spec, turn):
        return [self.invariants.check_action(call, spec, turn)]


def turn_of(who) -> Turn:
    return Turn(thread_id=scoped_thread_id(who.tenant_id, "th"), index=0, input=InputCheck(text="GO envoie"),
                channel="web", sender=who.account_id)


SEND = ToolSpec(name="mail_send", description="", parameters={}, effect=Effect.SEND_EXTERNAL, source="connector:mail")
TRASH = ToolSpec(name="mail_trash", description="", parameters={}, effect=Effect.DESTRUCTIVE, source="connector:mail")
BULK = ToolSpec(name="mass", description="", parameters={}, effect=Effect.SEND_BULK)
READ = ToolSpec(name="calendar_events", description="", parameters={}, effect=Effect.READ,
                source="connector:calendar")
CALL = ToolCall(id="c1", name="mail_send", arguments={"to": "ami@example.com", "body": "salut"})


def test_minor_send_or_destroy_is_always_a_strong_card_of_the_guardian(w):
    gates = MinorGates(inner=Inner(), minor_state=w.hh.minor_state)
    for spec in (SEND, TRASH):
        (v,) = gates.action(CALL, spec, turn_of(w.kid))
        assert (v.decision, v.rule, v.strength) == (Decision.HITL, GUARDIAN_RULE, "strong")
    (v,) = gates.action(CALL, BULK, turn_of(w.kid))
    assert v.decision is Decision.DENY  # l'envoi de masse reste refusé, pas de carte
    (v,) = gates.action(CALL, SEND, turn_of(w.adult))
    assert v.rule == "effect:send_external:allowlist"  # un adulte du foyer : les portes habituelles
    (v,) = gates.action(CALL, READ, turn_of(w.kid))
    assert v.decision is Decision.ALLOW  # lire n'est pas concerné


def _card(backend, who):
    with tenant_scope(who.tenant_id):
        verdict = Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=GUARDIAN_RULE, reason="mineur",
                          strength="strong")
        return backend.request(turn_of(who), CALL, verdict, None)


def _strong_proof(backend, user, approval_id, decision="approve"):
    priv, spki = gen_keypair()
    dev = backend.enroll_device(user=user, name="téléphone", public_key_spki=spki, telegram_user_id=str(TG))
    backend.confirm_device_cli(dev.code)
    ch = backend.challenge(approval_id)
    sig = sign_p1363(priv, f"{ch.canonical()}.{decision}".encode())
    return Proof(challenge=ch.canonical(), signature=sig, device_id=dev.id, telegram_init_data=telegram_init_data(BOT, TG))


def test_only_the_guardian_decides_the_card_of_a_minor(w):
    backend = InMemoryApprovals(telegram_bot_token=BOT, guardian_of=w.hh.guardian_for_tenant,
                                account_tenant=lambda u: (w.tenancy.account(u).tenant_id
                                                          if u and w.tenancy.account(u) else None))
    card = _card(backend, w.kid)
    with tenant_scope(w.kid.tenant_id):
        with pytest.raises(ProofInvalid):  # le mineur, depuis son propre appareil fort : refusé
            backend.decide(card.id, "approve", _strong_proof(backend, w.kid.account_id, card.id))
        with pytest.raises(ProofInvalid):  # un autre adulte du foyer : refusé
            backend.decide(card.id, "approve", _strong_proof(backend, w.adult.account_id, card.id))
        with pytest.raises(ProofInvalid):  # le jeton local d'une machine n'approuve pas une carte de mineur
            backend.decide_local(card.id, "approve", w.kid.account_id)
        done = backend.decide(card.id, "approve", _strong_proof(backend, w.pay.account_id, card.id))
    assert (done.status, done.decided_by) == ("approved", w.pay.account_id)


def test_without_guardian_wiring_nobody_decides_a_minor_card(w):
    backend = InMemoryApprovals(telegram_bot_token=BOT)
    card = _card(backend, w.kid)
    with tenant_scope(w.kid.tenant_id), pytest.raises(ProofInvalid):
        backend.decide(card.id, "approve", _strong_proof(backend, w.pay.account_id, card.id))


def test_connectors_and_agents_follow_the_household_whitelist(w):
    restrict = minor_tool_filter(w.hh)
    assert restrict(w.adult.tenant_id) is None and restrict(w.pay.tenant_id) is None
    keep = restrict(w.kid.tenant_id)
    draft = ToolSpec(name="agent_draft", description="", parameters={})
    assert not restrict(w.kid.tenant_id)(ToolSpec(name="pack_install", description="", parameters={}))
    web = ToolSpec(name="web_search", description="", parameters={})
    assert [keep(s) for s in (SEND, READ, draft, web)] == [False, False, False, True]
    w.hh.put_policy(w.pay, {"minor_connectors": ["calendar"], "minor_agents": ["devoirs"]})
    keep = restrict(w.kid.tenant_id)
    assert [keep(s) for s in (SEND, READ, draft, web)] == [False, True, True, True]
    w.hh.revoke_consent(w.pay, w.kid.account_id)
    keep = restrict(w.kid.tenant_id)
    assert [keep(s) for s in (SEND, READ, draft, web)] == [False, False, False, True]  # suspendu : plus rien d'ouvert
    # Outils des sessions de code (§7.28) : jamais pour un mineur, même avec une liste blanche ouverte.
    code = [ToolSpec(name=n, description="", parameters={}) for n in ("terminal", "write_file", "patch", "read_file",
                                                                       "search_files", "checkpoint_restore")]
    w.hh.put_policy(w.pay, {"minor_connectors": ["calendar"], "minor_agents": ["devoirs"]})
    assert not any(restrict(w.kid.tenant_id)(spec) for spec in code)
    # Sous-agents et skills du dépôt d'une session (§7.28) : jamais non plus ; les skills des packs du web (même nom,
    # autre source) restent ceux du compte.
    session = [ToolSpec(name=n, description="", parameters={}) for n in ("delegate", "delegate_status",
                                                                          "delegate_cancel")]
    session.append(ToolSpec(name="skill_list", description="", parameters={}, source="builtin:code-skills"))
    assert not any(restrict(w.kid.tenant_id)(spec) for spec in session)
    assert restrict(w.kid.tenant_id)(ToolSpec(name="skill_list", description="", parameters={}, source="builtin:packs"))


def test_registry_of_a_minor_hides_tools_outside_the_whitelist(w):
    from nokker.tenancy.resources import TenantRegistry
    from nokker.tools.registry import Registry

    class Src:
        def specs(self):
            return [SEND, READ]

        def call(self, name, arguments, ctx):
            raise AssertionError("jamais exécuté")

    class Conns:
        def sources_for(self, tid):
            return [Src()]

    reg = TenantRegistry(internal=Registry(), tester_sources=[], connectors=Conns())
    reg.restrict = minor_tool_filter(w.hh)
    with tenant_scope(w.kid.tenant_id):
        assert [s.name for s in reg.current()._all()] == []
        out = reg.call("mail_send", {}, SimpleNamespace(thread_id="th", turn_index=0, channel="web", sender=None))
        assert out.is_error and "inconnu" in out.content
    with tenant_scope(w.adult.tenant_id):
        assert [s.name for s in reg.current()._all()] == ["mail_send", "calendar_events"]


def test_age_adapted_prefix_is_a_dedicated_neutral_file(w):
    cache = TenantMinorCache(w.hh)
    assert cache(w.kid.tenant_id) == "teen" and cache(w.adult.tenant_id) is None
    text = young_prefix("teen")
    assert "13 et 17 ans" in text and "Profil jeune" in text
    assert "moins de 13 ans" in young_prefix("child")


def test_engine_appends_the_young_prefix_for_a_minor_tenant(tmp_path, w):
    from nokker.context.engine import ContextEngineV0
    from nokker.tools.registry import Registry

    prefix = tmp_path / "p.md"
    prefix.write_text("PRODUIT", encoding="utf-8")
    eng = ContextEngineV0(tools=Registry(), prefix_path=str(prefix), product_prefix_path=str(prefix),
                          young_profile=TenantMinorCache(w.hh))
    with tenant_scope(w.kid.tenant_id):
        kid_text, kid_sha = eng._prefix_text, eng._prefix_sha256
    with tenant_scope(w.adult.tenant_id):
        adult_text, adult_sha = eng._prefix_text, eng._prefix_sha256
    assert kid_text.startswith("PRODUIT") and "Profil jeune" in kid_text
    assert adult_text == "PRODUIT" and kid_sha != adult_sha


def test_guardian_cards_routes_list_challenge_and_decide_in_the_minor_tenant(w):
    from nokker.household.cards import GuardianCards
    from nokker.team.model import TeamError

    backend = InMemoryApprovals(telegram_bot_token=BOT, guardian_of=w.hh.guardian_for_tenant)
    card = _card(backend, w.kid)
    ran = []

    class Done:
        def wait(self, s):
            return True

    cards = GuardianCards(household=w.hh, approvals=backend, to_json=lambda a: {"id": a.id, "status": a.status,
                                                                                "tool": a.tool_name},
                          schedule=lambda d: ran.append(d.id) or Done())
    with tenant_scope(w.pay.tenant_id):
        got = cards.pending(w.pay)["approvals"]
    assert [(x["member"]["account_id"], x["approval"]["id"]) for x in got] == [(w.kid.account_id, card.id)]
    with tenant_scope(w.adult.tenant_id), pytest.raises(TeamError) as e:
        cards.pending(w.adult)
    assert e.value.status == 403
    with tenant_scope(w.pay.tenant_id), pytest.raises(TeamError) as e:
        cards.challenge(w.pay, card.id, {"member": w.adult.account_id})  # pas un profil mineur
    assert e.value.status == 404
    priv, spki = gen_keypair()
    dev = backend.enroll_device(user=w.pay.account_id, name="téléphone", public_key_spki=spki,
                                telegram_user_id=str(TG))
    backend.confirm_device_cli(dev.code)
    with tenant_scope(w.pay.tenant_id):
        ch = cards.challenge(w.pay, card.id, {"member": w.kid.account_id})
        out = cards.decide(w.pay, card.id, {"member": w.kid.account_id, "decision": "approve",
                                            "challenge": ch["challenge"], "device_id": dev.id,
                                            "signature": sign_p1363(priv, f"{ch['challenge']}.approve".encode()),
                                            "init_data": telegram_init_data(BOT, TG)})
    assert out["status"] == "approved" and ran == [card.id]


def test_a_minor_card_is_not_pushed_to_the_minor(w):
    sent = []
    backend = InMemoryApprovals(telegram_bot_token=BOT, guardian_of=w.hh.guardian_for_tenant,
                                tester_notifier=sent.append)
    _card(backend, w.kid)
    assert sent == []
