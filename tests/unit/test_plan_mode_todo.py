"""Mode plan : rien ne s'exécute, donc une étape du plan de travail (`todo`) ne passe jamais à « completed » (recette
rc.9.30 : un agent a coché 4 étapes « completed » en mode plan sans en faire aucune). Hors plan : aucun changement."""
from __future__ import annotations

from nokker.core.ports import ToolSpec
from nokker.core.turn import Decision, Effect, Message, TurnInput
from nokker.qa.permissions import PLAN_TODO_WHY, LocalPermissions, Mode, PermissionGates
from tests.unit.test_loop_contract import kernel, tc

FOUR_DONE = [{"id": str(i), "content": f"étape {i}", "status": "completed"} for i in range(4)]
ADVICE = "Ce n'est pas l'action qui est refusée, seulement le statut : réessaie avec « pending » ou « in_progress » ; tu coches une étape quand elle est faite, hors du mode plan."


def _run(mode, todos, merge=False):
    k, tools, ship, _ = kernel([Message("assistant", tool_calls=[tc("todo", todos=todos, merge=merge)]),
                                Message("assistant", "ok")])
    spec = ToolSpec("todo", "plan", {}, Effect.READ)
    tools.specs = {**tools.specs, "todo": spec}
    tools.core = lambda: [spec]
    k.gates.invariants = k.invariants  # le portail des permissions lit les invariants de la porte qu'il enveloppe
    k.gates = PermissionGates(inner=k.gates, permissions=LocalPermissions(mode=mode))
    turn = k.run_turn(TurnInput("t1", "fais le plan"))
    return turn, tools


def test_plan_mode_refuses_four_completed_todos_with_a_short_text():
    turn, tools = _run(Mode.PLAN, FOUR_DONE)
    assert tools.called == []
    msg = next(m for m in turn.messages if m.role == "tool")
    assert msg.content == f"Refusé : {PLAN_TODO_WHY}. {ADVICE}"
    v = next(v for v in turn.verdicts if v.decision is Decision.DENY)
    assert v.code == "plan_mode"


def test_plan_mode_refuses_a_single_completed_in_a_merge():
    todos = [{"id": "1", "status": "in_progress"}, {"id": "2", "status": "Completed"}]
    turn, tools = _run(Mode.PLAN, todos, merge=True)
    assert tools.called == []


def test_plan_mode_still_allows_pending_in_progress_cancelled_and_reading():
    for todos in ([{"id": "1", "content": "a", "status": "pending"}, {"id": "2", "content": "b", "status": "in_progress"},
                   {"id": "3", "content": "c", "status": "cancelled"}], None):
        turn, tools = _run(Mode.PLAN, todos)
        assert tools.called == ["todo"], todos


def test_outside_plan_mode_completed_is_untouched():
    for mode in (Mode.DEFAULT, Mode.ACCEPT_EDITS, Mode.AUTO):
        turn, tools = _run(mode, FOUR_DONE)
        assert tools.called == ["todo"], mode
