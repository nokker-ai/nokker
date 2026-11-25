"""Recette du mode plan (15.11) : le plan reste possible, et le refus d'écriture ne montre jamais un nom de règle."""
from __future__ import annotations

from nokker.core.ports import ToolSpec
from nokker.core.turn import Decision, Effect, Gate, ToolCall, Verdict
from nokker.qa.permissions import LocalPermissions, Mode, PermissionGates
from nokker.tools.code import CodeToolSource


class _Turn:
    def __init__(self, thread_id):
        self.thread_id = thread_id
        self.channel = "web"


class _Inner:
    class invariants:
        @staticmethod
        def check_action(call, spec, turn):
            return Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="ok")

    def action(self, call, spec, turn):
        return [self.invariants.check_action(call, spec, turn)]


def _gates(base=Mode.ACCEPT_EDITS):
    return PermissionGates(inner=_Inner(), permissions=LocalPermissions(mode=base))


def _call(name, **args):
    return ToolCall(id="c1", name=name, arguments=args)


def test_todo_stays_usable_in_plan_mode():
    """Cause écartée : `todo` est classé READ (lire ET écrire la liste de la session) ; le mode plan ne le refuse pas."""
    specs = {s.name: s for s in CodeToolSource(root="/tmp", backend="local").specs()}
    gates = _gates()
    gates.permissions.set_thread_mode("t", "plan")
    items = [{"id": "1", "content": "lire", "status": "pending"}]
    v = gates.action(_call("todo", todos=items), specs["todo"], _Turn("t"))
    assert [x.decision for x in v] == [Decision.ALLOW]
    v = gates.action(_call("patch", mode="replace", path="a.py"), specs["patch"], _Turn("t"))
    assert v[0].decision is Decision.DENY and v[0].code == "plan_mode"


def test_the_refusal_rule_is_named_after_the_thread_mode_not_the_base_mode():
    """Défaut A : fil mis en plan par `PUT …/mode` alors que le mode de base est accept_edits → la règle disait
    `permissions:accept_edits` (jargon montré, et mauvaise étiquette pour le relecteur automatique)."""
    gates = _gates(Mode.ACCEPT_EDITS)
    gates.permissions.set_thread_mode("t", "plan")
    spec = ToolSpec("patch", "p", {}, Effect.WRITE)
    v = gates.action(_call("patch", path="a.py"), spec, _Turn("t"))
    assert v[0].decision is Decision.DENY and v[0].rule == "permissions:plan"
    other = gates.action(_call("terminal", command="ls"), ToolSpec("terminal", "t", {}, Effect.DESTRUCTIVE), _Turn("u"))
    assert other[0].rule != "permissions:plan"  # un autre fil garde son mode


def test_the_model_never_reads_an_internal_rule_name_in_a_plan_refusal():
    from tests.unit.test_loop_approvals import make_kernel, tc
    from nokker.core.turn import Message, TurnInput

    k, tools = make_kernel([Message("assistant", tool_calls=[tc("patch", path="a.py")]),
                            Message("assistant", "ok")])
    tools.specs["patch"] = ToolSpec("patch", "p", {}, Effect.WRITE)
    perms = LocalPermissions(mode=Mode.ACCEPT_EDITS)
    perms.set_thread_mode("t1", "plan")
    k.gates = PermissionGates(inner=k.gates, permissions=perms)
    turn = k.run_turn(TurnInput("t1", "modifie a.py"))
    seen = " ".join(m.content or "" for m in turn.messages if m.role == "tool")
    assert tools.called == []
    assert "permissions:" not in seen and "lecture seule" in seen  # le motif, sans le nom de la règle


# ─────────────── B : le mode changé pendant un tour est relu au début du tour suivant ───────────────


def _engine_kernel(tmp_path, script, mode_note=None):
    from datetime import datetime

    from nokker.context.engine import ZURICH, ContextEngineV0
    from tests.unit.test_loop_approvals import Esc, Inv, LedgerFake, Mem, Pol, ScriptedProvider, StoreFake, Tools
    from nokker.core.loop import Kernel
    from nokker.core.turn import Tier

    prefix = tmp_path / "prefix.md"
    prefix.write_text("PRÉFIXE", encoding="utf-8")
    tools = Tools()
    tools.specs["patch"] = ToolSpec("patch", "p", {}, Effect.WRITE)
    ctx = ContextEngineV0(tools=tools, prefix_path=str(prefix), now_fn=lambda: datetime(2026, 11, 15, tzinfo=ZURICH),
                          mode_note=mode_note)
    inv = Inv()
    ship = ScriptedProvider(script)
    k = Kernel(store=StoreFake(), context=ctx, memory=Mem(), invariants=inv, gates=_Inner(), escalator=Esc(),
               policy=Pol(), providers={Tier.SHIP: ship}, tools=tools, ledger=LedgerFake())
    perms = LocalPermissions(mode=Mode.ACCEPT_EDITS)
    k.gates = PermissionGates(inner=k.gates, permissions=perms)
    return k, tools, ship, perms


def test_mode_changed_after_a_plan_refusal_is_allowed_next_turn_but_history_still_says_read_only(tmp_path):
    """Cause établie : la porte relit bien le mode (le patch du tour 2 passe), mais le modèle ne voit que l'historique,
    où le refus du tour 1 (« lecture seule ») reste tel quel, et aucune ligne ne lui donne le mode courant."""
    from tests.unit.test_loop_approvals import tc
    from nokker.core.turn import Message, TurnInput

    script = [Message("assistant", tool_calls=[tc("patch", path="a.py")]), Message("assistant", "lecture seule"),
              Message("assistant", tool_calls=[tc("patch", path="a.py")]), Message("assistant", "fait")]
    k, tools, ship, perms = _engine_kernel(tmp_path, script)
    perms.set_thread_mode("t1", "plan")
    k.run_turn(TurnInput("t1", "modifie a.py"))
    assert tools.called == []
    perms.set_thread_mode("t1", "accept_edits")  # PUT …/mode pendant / après le tour 1
    k.run_turn(TurnInput("t1", "vas-y, modifie a.py"))
    assert tools.called == ["patch"]  # la porte, elle, a relu le mode
    turn2_first_call = ship.seen[2]
    assert any(m.get("role") == "tool" and "lecture seule" in (m.get("content") or "") for m in turn2_first_call)
    assert "accept_edits" not in " ".join(str(m.get("content")) for m in turn2_first_call)  # rien ne dit le mode courant


def test_mode_note_hook_is_read_at_the_start_of_each_turn_and_frozen_in_the_turn_head(tmp_path):
    from tests.unit.test_loop_approvals import tc
    from nokker.core.turn import Message, TurnInput

    holder = {}
    script = [Message("assistant", content="un"), Message("assistant", tool_calls=[tc("patch", path="a.py")]),
              Message("assistant", content="deux")]
    k, tools, ship, perms = _engine_kernel(tmp_path, script, mode_note=lambda tid: f"<<{holder[tid]}>>")
    holder["t1"] = "plan"
    k.run_turn(TurnInput("t1", "bonjour"))
    holder["t1"] = "accept_edits"  # changé entre deux tours
    k.run_turn(TurnInput("t1", "ensuite"))
    first = [m for m in ship.seen[0] if m.get("role") == "user"][-1]["content"]
    second_calls = ship.seen[1:]
    last_user = lambda msgs: [m for m in msgs if m.get("role") == "user"][-1]["content"]  # noqa: E731
    assert "<<plan>>" in first and "<<accept_edits>>" not in first
    assert all("<<accept_edits>>" in last_user(c) for c in second_calls)  # tous les appels du tour 2
    assert "<<plan>>" in " ".join(str(m.get("content")) for m in second_calls[0])  # le tour 1 reste tel qu'il fut


def test_mode_note_hook_absent_or_failing_changes_nothing(tmp_path):
    from nokker.core.turn import Message, TurnInput

    def boom(tid):
        raise RuntimeError("x")

    heads = []
    for hook in (None, boom, lambda tid: ""):
        k, _, ship, _ = _engine_kernel(tmp_path, [Message("assistant", content="ok")], mode_note=hook)
        k.run_turn(TurnInput("t1", "bonjour"))
        heads.append([m for m in ship.seen[0] if m.get("role") == "user"][-1]["content"])
    assert heads[0] == heads[1] == heads[2]
