"""Drapeau de domaine et sessions de code (lot fix-batch-1, défaut 2 ; banc D1, tâche M01 ; banc X9 §5.2 ;
revue du train du 2.11, défaut 4).

Dans une session de code locale, le drapeau est consultatif : la réponse du code fait n'est jamais remplacée. Hors
session (la bêta, une conversation Lokkal), il escalade comme dans `main`, quel que soit le vocabulaire du tour.
"""
from __future__ import annotations

import json

from nokker.core.turn import Decision, Message, Tier, ToolCall, Turn, TurnInput
from nokker.qa.gates import GatesV0, GatesV1
from nokker.qa.invariants import DeterministicInvariants, domain_flags
from tests.unit.test_loop_contract import Esc, LedgerFake, Mem, Pol, ScriptedProvider, StoreFake, Tools

# Consigne de la tâche M01 du banc (bench/lokkal, mot pour mot).
M01 = ("Un client signale que sa facture est fausse quand il utilise un code promo : le rabais semble compté deux fois "
       "et la TVA ne tombe pas juste. Le paquet `shop/` a plusieurs modules ; trouve où est l'erreur (peut-être à plus "
       "d'un endroit) et corrige-la sans casser le reste. Tests dans `tests/`.")

INV = DeterministicInvariants()


def turn_for(text: str, wrote: bool = False) -> Turn:
    t = Turn(thread_id="t", index=0, input=INV.check_input(text))
    if wrote:
        call = ToolCall(id="p1", name="patch", arguments={"path": "shop/tax.py", "old_string": "a", "new_string": "b"})
        t.messages = [Message(role="assistant", tool_calls=[call]),
                      Message(role="tool", content=json.dumps({"ok": True}), tool_call_id="p1", name="patch")]
    return t


def decision(gates, text: str, wrote: bool = False):
    vs = gates.switch("Corrigé : le rabais n'est plus appliqué deux fois.", turn_for(text, wrote), Tier.DEEP)
    return vs, min((v.decision for v in vs), key=lambda d: d is not Decision.ESCALATE)


# Revue du train du 2.11, défaut 4 (arbitrage dev) : le « contexte de code » ne vaut QUE dans une session de code ;
# hors session (la bêta : `GatesV0.code_session` = None, une conversation Lokkal), le comportement est celui de
# `main` : tout drapeau de domaine escalade. Mesuré le 25.10 : une simple date suffisait à lever l'escalade.
DATE_QUESTION = "Ma facture du 25/10/2026 a un montant de 1200 CHF, peux-tu vérifier la TVA ?"
OUTSIDE = [M01, DATE_QUESTION,
           "Écris une fonction qui calcule la TVA d'une facture, avec ses tests unitaires",
           "Le contrat de l'endpoint /invoices change : adapte le module de paiement",
           "```py\ndef total(facture): return facture.ht * 1.081\n```\npourquoi la TVA est fausse ici ?",
           "quel taux de TVA pour ma facture à l'étranger ?",
           "J'ai utilisé un code promo, ma facture est-elle juste ? La TVA me paraît fausse.",
           "Le prix HT/TTC de ma facture est-il juste ?",
           "Est-ce que je peux résilier ce contrat avant la fin du préavis ?",
           "Combien d'impôts je vais payer sur ce bonus ?"]


def test_outside_a_code_session_every_domain_flag_escalates_like_main():
    for gates in (GatesV0(invariants=INV), GatesV1(invariants=INV, judge=QuietJudge())):
        for text in OUTSIDE:
            assert domain_flags(text) & {"fiscal", "juridique", "montant"}, text
            vs, worst = decision(gates, text)
            assert worst is Decision.ESCALATE, (type(gates).__name__, text, vs)
            assert not any(v.rule.endswith(":code") for v in vs)


def test_outside_a_code_session_a_turn_that_wrote_files_still_escalates():
    assert decision(GatesV0(invariants=INV), "et pour l'arrondi de la TVA sur la facture ?", wrote=True)[1] \
        is Decision.ESCALATE


def test_date_question_escalates_in_a_lokkal_conversation_outside_its_code_sessions():
    from nokker.qa.permissions import LocalPermissions, PermissionGates

    gates = PermissionGates(inner=GatesV0(invariants=INV), permissions=LocalPermissions(),
                            code_session=lambda tid: tid == "code-1")
    chat = turn_for(DATE_QUESTION)
    chat.thread_id = "chat-1"
    assert any(v.decision is Decision.ESCALATE for v in gates.switch("…", chat, Tier.SHIP))
    session = turn_for(DATE_QUESTION)
    session.thread_id = "code-1"
    vs = gates.switch("…", session, Tier.SHIP)
    assert all(v.decision is not Decision.ESCALATE for v in vs) and any(v.rule.endswith(":session-code") for v in vs)


class QuietJudge:
    model_name = "juge"

    def ask(self, question_id, prompt, options):
        from nokker.core.ports import JudgeAnswer

        return JudgeAnswer(choice="Non", probabilities={"Oui": 0.01, "Non": 0.99}, model="juge", latency_s=0.0)


def test_m01_in_the_kernel_escalates_outside_a_session_and_keeps_the_answer_inside():
    from nokker.core.loop import Kernel
    from nokker.qa.permissions import LocalPermissions, PermissionGates
    from tests.unit.test_loop_contract import Ctx

    def kernel(gates):
        ship = ScriptedProvider([Message("assistant", "Corrigé dans shop/pricing.py et shop/tax.py, tests verts.")])
        return Kernel(store=StoreFake(), context=Ctx(), memory=Mem(), invariants=INV, gates=gates, escalator=Esc(),
                      policy=Pol(), providers={Tier.SHIP: ship}, tools=Tools(), ledger=LedgerFake())

    assert kernel(GatesV0(invariants=INV)).run_turn(TurnInput("t1", M01)).escalated  # la bêta : comme main
    inside = kernel(PermissionGates(inner=GatesV0(invariants=INV), permissions=LocalPermissions())).run_turn(
        TurnInput("t1", M01))  # `nokker code` : la réponse du code fait reste
    assert not inside.escalated and inside.reply.startswith("Corrigé dans shop/pricing.py")


# ─────────── session de code locale (banc X9 §5.2 : 16 tours sur 152 perdaient leur réponse) ───────────

X9_LIKE = ["Ajoute la TVA à 8,1 % sur chaque ligne de facture et arrondis le total aux 5 centimes",
           "Le total de la facture FAC-217 est faux quand il y a une remise, regarde",
           "Pourquoi le CSV des factures est en point-virgule, déjà ?",
           "Est-ce que je dois facturer la TVA à un client allemand ?"]


def test_in_a_local_code_session_the_domain_flag_never_replaces_the_answer():
    from nokker.qa.permissions import LocalPermissions, PermissionGates

    gates = PermissionGates(inner=GatesV0(invariants=INV), permissions=LocalPermissions())  # `nokker code`
    for text in X9_LIKE:
        vs = gates.switch("Fait, tests verts.", turn_for(text), Tier.DEEP)
        assert all(v.decision is not Decision.ESCALATE for v in vs), text
        assert any(v.rule.endswith(":session-code") for v in vs), text
    advice = gates.switch("…", turn_for(X9_LIKE[-1]), Tier.DEEP)
    assert any("demande de conseil" in v.reason for v in advice)  # tracée au ledger, réponse gardée


def test_lokkal_only_code_session_threads_are_consultative():
    from nokker.qa.permissions import LocalPermissions, PermissionGates

    gates = PermissionGates(inner=GatesV0(invariants=INV), permissions=LocalPermissions(),
                            code_session=lambda tid: tid == "code-1")
    question = "quel taux de TVA pour ma facture à l'étranger ?"
    session = turn_for(question)
    session.thread_id = "code-1"
    chat = turn_for(question)
    chat.thread_id = "chat-1"
    assert all(v.decision is not Decision.ESCALATE for v in gates.switch("…", session, Tier.SHIP))
    assert any(v.decision is Decision.ESCALATE for v in gates.switch("…", chat, Tier.SHIP))


def test_x9_like_session_keeps_every_answer_in_the_kernel():
    from nokker.core.loop import Kernel
    from nokker.qa.permissions import LocalPermissions, PermissionGates
    from tests.unit.test_loop_contract import Ctx

    replies = [Message("assistant", f"Réponse {i} : fait.") for i in range(len(X9_LIKE))]
    k = Kernel(store=StoreFake(), context=Ctx(), memory=Mem(), invariants=INV,
               gates=PermissionGates(inner=GatesV0(invariants=INV), permissions=LocalPermissions()),
               escalator=Esc(), policy=Pol(), providers={Tier.SHIP: ScriptedProvider(replies)}, tools=Tools(),
               ledger=LedgerFake())
    for i, text in enumerate(X9_LIKE):
        turn = k.run_turn(TurnInput("x9", text))
        assert turn.reply == f"Réponse {i} : fait." and not turn.escalated, text
