"""Ce que la personne lit d'une escalade (C7e) : « Je ne peux pas continuer » pour une panne, libellé de règle et non
identifiant pour une porte, motif du modèle sans identifiant interne, texte d'action de bulle sans id."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from nokker.approvals.rule_labels import rule_label
from nokker.core.loop import Kernel, person_why, scrub_internal_ids
from nokker.core.turn import Decision, Gate, Message, Tier, TurnInput, Verdict
from nokker.qa.escalate import EscalatorV0, LocalEscalator
from nokker.tenancy.resources import TenantEscalator
from tests.unit.test_failure_label import REALISTIC, _Raising, _down_turn
from tests.unit.test_loop_contract import (Ctx, Esc, Inv, LedgerFake, Mem, Pol, StoreFake, Tools, tc)
from tests.unit.test_local_thread_model import _world, _run  # noqa: F401

IDS = re.compile(r"\b(?:tn|th|bu|ap)_[A-Za-z0-9]|\[approval:|\b(?:permissions|provenance|router|byok|loop|effect|domaine):\w")
TURN = SimpleNamespace(thread_id="t", index=1)


def test_cannot_continue_phrase_and_kept_escalation():
    assert EscalatorV0().cannot_continue(TURN, "le modèle ne répond pas") == "Je ne peux pas continuer : le modèle ne répond pas."
    assert LocalEscalator().cannot_continue(TURN, "x") == "Je ne peux pas continuer : x."
    assert EscalatorV0().escalate(TURN, "x") == "Je transmets à l'équipe : x."  # anciens appels inchangés
    assert LocalEscalator().escalate(TURN, "x") == "J'ai besoin de toi pour continuer : x."
    te = TenantEscalator(EscalatorV0())
    assert te.cannot_continue(TURN, "x") == "Je ne peux pas continuer : x."


@pytest.mark.parametrize("exc", [e for _, e in REALISTIC])
@pytest.mark.parametrize("esc", [EscalatorV0(), LocalEscalator()])
def test_tier_down_is_cannot_continue_not_a_transfer(exc, esc):
    t = _down_turn(exc, esc)
    assert t.escalated and t.messages[-1].content.startswith("Je ne peux pas continuer : le modèle de Nokker ne répond pas (")
    assert "transmets" not in t.messages[-1].content and "besoin de toi" not in t.messages[-1].content
    assert not IDS.search(t.messages[-1].content)


def test_old_escalator_without_cannot_continue_still_works():
    k, _ = _world(None)
    k.escalator = Esc()  # n'a que `escalate`
    p = _Raising(RuntimeError("HTTP 502 de https://x.test: bad"))
    k.providers = {Tier.SHIP: p, Tier.DEEP: p, Tier.SMALL: p}
    assert _run(k).messages[-1].content.startswith("Je transmets à Alice (le modèle de Nokker ne répond pas")


def test_person_why_uses_label_not_rule_id():
    v = Verdict(gate=Gate.ACTION, decision=Decision.ESCALATE, layer=1, rule="provenance:web", reason="page lue sur le web")
    assert person_why(v, Decision.ESCALATE) == f"{rule_label('provenance:web')} : page lue sur le web"
    assert "provenance:" not in person_why(v, Decision.ESCALATE)
    unknown = Verdict(gate=Gate.ACTION, decision=Decision.ESCALATE, layer=1, rule="zz:inconnue", reason="parce que")
    assert person_why(unknown, Decision.ESCALATE) == "parce que"  # une règle sans libellé n'est pas recopiée
    perm = Verdict(gate=Gate.ACTION, decision=Decision.DENY, layer=1, rule="permissions:accept_edits", reason="édition non permise")
    assert person_why(perm, Decision.DENY) == "édition non permise"


class _GateEsc:
    """Portes qui escaladent sur chaque appel d'action et sur la bascule, avec des règles internes."""

    def __init__(self, rule, reason="motif utile"):
        self.rule, self.reason = rule, reason

    def action(self, call, spec, turn):
        return [Verdict(gate=Gate.ACTION, decision=Decision.ESCALATE, layer=1, rule=self.rule, reason=self.reason)]

    def switch(self, reply, turn, tier):
        return [Verdict(gate=Gate.SWITCH, decision=Decision.ESCALATE, layer=1, rule=self.rule, reason=self.reason)]


def _kernel(script, gates, escalator):
    from tests.unit.test_loop_contract import ScriptedProvider
    k = Kernel(store=StoreFake(), context=Ctx(), memory=Mem(), invariants=Inv(), gates=gates, escalator=escalator,
               policy=Pol(), providers={Tier.SHIP: ScriptedProvider(script), Tier.DEEP: ScriptedProvider([])},
               tools=Tools(), ledger=LedgerFake())
    return k


@pytest.mark.parametrize("rule", ["provenance:web", "permissions:accept_edits", "router:unavailable", "zz:interne"])
def test_gate_escalations_never_show_rule_ids(rule):
    # porte de la réponse finale (bascule)
    k = _kernel([Message("assistant", "voilà")], _GateEsc(rule), EscalatorV0())
    t = k.run_turn(TurnInput("t1", "bonjour"))
    assert t.escalated and "transmets" in t.reply and "motif utile" in t.reply and not IDS.search(t.reply), t.reply
    # porte d'une action d'outil
    k = _kernel([Message("assistant", tool_calls=[tc("calendar_list")]), Message("assistant", "ok")], _GateEsc(rule), EscalatorV0())
    t = k.run_turn(TurnInput("t1", "mon agenda ?"))
    assert t.escalated and not IDS.search(t.reply), t.reply


def test_model_reason_scrubbed_of_internal_ids():
    reason = ("accès au fil th_ab12cd34 du locataire tn_ninabot, bulle bu_9f8e7d6c, carte ap_0a1b2c3d "
              "[approval:ap_0a1b2c3d] à revoir")
    out = scrub_internal_ids(reason)
    assert not IDS.search(out) and "à revoir" in out and "  " not in out and " ," not in out, out
    assert scrub_internal_ids("TVA à vérifier (th_abc123)") == "TVA à vérifier"
    assert scrub_internal_ids("garde ce motif, tel quel") == "garde ce motif, tel quel"
    assert scrub_internal_ids("") == ""
    k = _kernel([Message("assistant", tool_calls=[tc("escalate_to_human", reason=reason)])] * 3,
                _GateEsc("ok"), EscalatorV0())
    t = k.run_turn(TurnInput("t1", "bonjour"))
    assert t.escalated and not IDS.search(t.reply) and "à revoir" in t.reply and t.reply.startswith("Je transmets à l'équipe")


def test_bubble_action_text_has_no_id_nor_title_and_model_block_keeps_them():
    from nokker.bubbles.hub import BubbleHub
    b = SimpleNamespace(id="bu_abc123def456", title="Étape 1 sur 2")
    text, meta = BubbleHub._action_text(b, {"id": "done", "label": "Fait"}, "done")
    assert text == "[Action « Fait » demandée sur la bulle]" and not IDS.search(text) and "Étape" not in text
    assert meta == "[Bulle « Étape 1 sur 2 » (bu_abc123def456)]"
    assert BubbleHub._action_text(b, {"id": "go"}, "go")[0] == "[Action « go » demandée sur la bulle]"


def test_scrub_that_empties_the_reason_falls_back_to_the_bare_sentence():
    assert scrub_internal_ids("th_abc123") == ""
    assert EscalatorV0().escalate(SimpleNamespace(thread_id="t", index=0), scrub_internal_ids("[approval:ap_0a1b2c3d]")) == "Je transmets à l'équipe."


def test_bubble_action_label_cannot_break_the_format():
    from nokker.bubbles.hub import BubbleHub
    b = SimpleNamespace(id="bu_abc123def456", title="T")
    text, _ = BubbleHub._action_text(b, {"id": "x", "label": "Fait » ] [ oui « "}, "x")
    assert text == "[Action « Fait oui » demandée sur la bulle]" or text.count("«") == 1 and text.count("»") == 1, text
    assert BubbleHub._action_text(b, {"id": "x", "label": "«»"}, "x")[0] == "[Action « x » demandée sur la bulle]"


def test_domain_rule_labels_compose_in_the_four_languages():
    assert rule_label("domaine:fiscal", "fr") == "Question fiscale"
    assert rule_label("domaine:fiscal+montant", "fr") == "Question fiscale et montant engageant"
    assert rule_label("domaine:fiscal+juridique+montant", "fr") == "Question fiscale, question juridique et montant engageant"
    assert rule_label("domaine:fiscal+montant", "en") == "Tax question and financial commitment"
    assert rule_label("domaine:fiscal+montant", "de") == "Steuerfrage und finanzielle Verpflichtung"
    assert rule_label("domaine:fiscal+juridique", "de") == "Steuerfrage und Rechtsfrage"
    assert rule_label("domaine:fiscal+montant", "it") == "Questione fiscale e impegno di spesa"
    assert rule_label("domaine:montant:carte-a-payer", "fr").startswith("Paiement")  # règle exacte inchangée


def test_unlisted_domain_flag_has_no_label_and_escalation_stays_bare():
    assert rule_label("domaine:medical", "fr") == "domaine:medical"
    assert person_why(Verdict(gate=Gate.SWITCH, decision=Decision.ESCALATE, layer=1, rule="domaine:medical", reason=""),
                      Decision.ESCALATE) == ""
