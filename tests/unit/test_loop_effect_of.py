"""Effet par appel (`ToolSpec.effect_of`, Nokker Lokkal L0) et lectures du tour invalidées après un appel à effet.

Fakes repris de test_loop_contract.py ; la seule différence est un registre qui porte un outil à effet par appel
(`shell` : `ls` = READ, le reste = DESTRUCTIVE) et un outil d'écriture."""
from __future__ import annotations

from nokker.core.loop import Kernel
from nokker.core.ports import ToolSpec
from nokker.core.turn import Decision, Effect, Gate, Message, Tier, TurnInput, Verdict
from tests.unit.test_loop_contract import (
    Ctx, Esc, GatesFake, Inv, LedgerFake, Mem, Pol, ScriptedProvider, StoreFake, Tools, tc,
)


class InvPerCall(Inv):
    def check_action(self, call, spec, turn):
        eff = spec.effect_for(call.arguments) if spec else Effect.WRITE
        d = {Effect.SEND_BULK: Decision.DENY, Effect.DESTRUCTIVE: Decision.HITL}.get(eff, Decision.ALLOW)
        return Verdict(gate=Gate.ACTION, decision=d, layer=1, rule=f"effect:{eff.value}")


class CodeTools(Tools):
    specs = {**Tools.specs,
             "file_read": ToolSpec("file_read", "lit", {}, Effect.READ),
             "file_write": ToolSpec("file_write", "écrit", {}, Effect.WRITE),
             "shell": ToolSpec("shell", "commande", {}, Effect.DESTRUCTIVE,
                               effect_of=lambda a: Effect.READ if a.get("command") == "ls" else Effect.DESTRUCTIVE)}


def kernel(script):
    inv, tools = InvPerCall(), CodeTools()
    k = Kernel(store=StoreFake(), context=Ctx(), memory=Mem(), invariants=inv, gates=GatesFake(inv), escalator=Esc(),
               policy=Pol(), providers={Tier.SHIP: ScriptedProvider(script), Tier.DEEP: ScriptedProvider([])},
               tools=tools, ledger=LedgerFake())
    return k, tools


def test_effect_for_defaults_and_falls_back_on_a_broken_classifier():
    plain = ToolSpec("x", "x", {}, Effect.READ)
    assert plain.effect_for({"a": 1}) is Effect.READ

    def broken(_):
        raise RuntimeError("classement cassé")

    spec = ToolSpec("y", "y", {}, Effect.DESTRUCTIVE, effect_of=broken)
    assert spec.effect_for({}) is Effect.DESTRUCTIVE  # jamais plus permissif que le déclaré


def test_per_call_effect_reaches_the_gate():
    k, tools = kernel([Message("assistant", tool_calls=[tc("shell", command="ls")]),
                       Message("assistant", tool_calls=[tc("shell", command="rm -r build")]),
                       Message("assistant", "fait")])
    turn = k.run_turn(TurnInput("t1", "nettoie"))
    assert tools.called == ["shell"]  # `ls` exécuté, `rm -r build` retenu
    assert [v.rule for v in turn.verdicts if v.gate is Gate.ACTION] == ["effect:read", "effect:destructive"]
    assert len(turn.pending_approvals) == 1 and turn.pending_approvals[0].arguments == {"command": "rm -r build"}


def test_a_write_invalidates_the_reads_already_made_this_turn():
    """Lire, écrire, relire le même fichier dans un tour : la relecture est ré-exécutée (sinon le modèle verrait
    l'ancien contenu). Deux lectures sans écriture entre elles restent dédoublonnées."""
    k, tools = kernel([Message("assistant", tool_calls=[tc("file_read", path="a.py")]),
                       Message("assistant", tool_calls=[tc("file_read", path="a.py")]),
                       Message("assistant", tool_calls=[tc("file_write", path="a.py")]),
                       Message("assistant", tool_calls=[tc("file_read", path="a.py")]),
                       Message("assistant", "fait")])
    turn = k.run_turn(TurnInput("t1", "corrige a.py"))
    assert tools.called == ["file_read", "file_write", "file_read"]
    tool_msgs = [m.content for m in turn.messages if m.role == "tool"]
    assert "déjà appelé" in tool_msgs[1] and "déjà appelé" not in tool_msgs[3]


def test_a_reply_cut_by_the_length_limit_is_not_final():
    """Banc L0 (L01, 23.10) : à 2048 jetons, le 397B était coupé en pleine réflexion, sans appel d'outil, et ce
    texte tronqué devenait la réponse. Le noyau demande de continuer (au plus `truncation_retries` fois)."""
    from nokker.core.ports import Completion
    from nokker.core.turn import Usage

    class Cut(ScriptedProvider):
        def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
            self.seen.append((messages, max_tokens))
            msg = self.script.pop(0)
            return Completion(message=msg, usage=Usage(prompt=1), model="m", latency_s=0.0,
                              finish_reason="length" if msg.content.startswith("Donc") else "stop")

    ship = Cut([Message("assistant", "Donc parse_unary appelle"), Message("assistant", tool_calls=[tc("file_read")]),
                Message("assistant", "Corrigé, tests verts.")])
    inv = InvPerCall()
    k = Kernel(store=StoreFake(), context=Ctx(), memory=Mem(), invariants=inv, gates=GatesFake(inv), escalator=Esc(),
               policy=Pol(), providers={Tier.SHIP: ship, Tier.DEEP: ScriptedProvider([])}, tools=CodeTools(),
               ledger=LedgerFake(), max_tokens=16384)
    turn = k.run_turn(TurnInput("t1", "corrige"))
    assert turn.reply == "Corrigé, tests verts."
    assert all(mt == 16384 for _, mt in ship.seen)
    assert any("[noyau] Ta réponse a été coupée" in m.content for m in turn.messages if m.role == "user")


def test_cancel_stops_the_turn_before_the_next_call():
    k, tools = kernel([Message("assistant", tool_calls=[tc("file_read", path="a")]),
                       Message("assistant", tool_calls=[tc("file_read", path="b")]),
                       Message("assistant", "fini")])
    real_call = tools.call

    def call_then_cancel(n, a, ctx):
        k.cancel("t1")  # l'utilisateur annule pendant le premier outil
        return real_call(n, a, ctx)

    tools.call = call_then_cancel
    turn = k.run_turn(TurnInput("t1", "lis"))
    assert tools.called == ["file_read"] and turn.error == "cancelled" and turn.reply == "Annulé à ta demande."
    k2, tools2 = kernel([Message("assistant", "ok")])
    k2.cancel("t1")  # une annulation d'AVANT le tour ne le vise pas
    assert k2.run_turn(TurnInput("t1", "salut")).reply == "ok"


def test_local_escalator_never_names_anyone_else():
    from nokker.core.turn import InputCheck, Turn
    from nokker.qa.escalate import LocalEscalator

    t = Turn(thread_id="th", index=0, input=InputCheck(text="x"), channel="api")
    text = LocalEscalator().escalate(t, "la commande attend ton approbation")
    assert "Alice" not in text and "attend ton approbation" in text


def test_approval_block_keeps_the_end_of_a_long_output():
    from nokker.approvals import executor

    class A:
        tool_name, arguments, status, decided_by, decided_via = ("terminal", {"command": "python -m pytest"},
                                                                   "executed", "local-owner", "local-token")

    long = "x" * 20000 + "\n7 passed"
    head, tail = long[:executor.RESULT_CHAR_LIMIT // 3], long[-(2 * executor.RESULT_CHAR_LIMIT // 3):]
    block = executor._approbation_block(A(), outcome=f"{head}[coupé]{tail}")
    assert "Commande : python -m pytest" in block and block.count("7 passed") == 1 and "ne la relance pas" in block
