"""Un tour réel du noyau sur le fil d'une session de code : l'écriture passe par les portes de la session (mode du fil)
et, quand elle est demandée, par une VRAIE carte des approbations multi-locataire, dans le locataire de la session —
jamais exécutée avant la décision."""
from __future__ import annotations

import os

import pytest

from nokker.approvals.memory import InMemoryApprovals
from nokker.code_sessions.config import CodeConfig
from nokker.code_sessions.service import CodeSessions
from nokker.code_sessions.store import MemoryCodeStore
from nokker.core.loop import Kernel
from nokker.core.turn import Message, Tier, ToolCall, TurnInput
from nokker.tenancy.context import tenant_scope
from nokker.tools.registry import Registry
from tests.unit.code_sessions.conftest import code_block
from tests.unit.test_loop_approvals import Ctx, Esc, Gates, Inv, LedgerFake, Mem, Pol, ScriptedProvider, StoreFake

TA = "tn_aaaaaaaaaaaa"
ANA = "us_aaaaaaaaaaa1"


@pytest.fixture
def svc(cluster, fake_upstream):
    s = CodeSessions(cfg=CodeConfig.from_config(code_block(cluster.workspace_root), lokkal=False),
                     store=MemoryCodeStore())
    yield s
    s.close_all()


class KernelGates(Gates):
    """Les portes du noyau ont leurs invariants en `invariants` (GatesV0, GatesV1)."""

    def __init__(self, inv):
        super().__init__(inv)
        self.invariants = inv


def _turn(svc, thread, approvals):
    inv = Inv()
    script = [Message("assistant", tool_calls=[ToolCall(id="c1", name="write_file",
                                                        arguments={"path": "a.txt", "content": "écrit"})]),
              Message("assistant", "fait")]
    k = Kernel(store=StoreFake(), context=Ctx(), memory=Mem(), invariants=inv, gates=svc.gates(KernelGates(inv)),
               escalator=Esc(), policy=Pol(), providers={Tier.SHIP: ScriptedProvider(script)},
               tools=Registry(sources=[svc.tools()]), ledger=LedgerFake(), approvals=approvals)
    with tenant_scope(TA):
        return k.run_turn(TurnInput(thread, "écris a.txt", sender=ANA))


def test_default_mode_writes_only_after_a_card(svc, cluster):
    with tenant_scope(TA):
        s = svc.create(ANA, {"mode": "default"})
    thread = f"{TA}:{s['id']}"
    approvals = InMemoryApprovals()
    turn = _turn(svc, thread, approvals)
    with tenant_scope(TA):
        pending = approvals.pending()
    assert [a.tool_name for a in pending] == ["write_file"] and pending[0].thread_id == thread
    assert not os.path.exists(os.path.join(cluster.workspace_root, TA, ANA, s["id"], "a.txt"))
    assert any(f"approval:{pending[0].id}" in (m.content or "") for m in turn.messages if m.role == "tool")
    with tenant_scope("tn_bbbbbbbbbbbb"):
        assert approvals.pending() == []  # la carte n'existe que dans le locataire de la session


def test_accept_edits_writes_in_the_session_root(svc, cluster):
    with tenant_scope(TA):
        s = svc.create(ANA, {})
    approvals = InMemoryApprovals()
    _turn(svc, f"{TA}:{s['id']}", approvals)
    with tenant_scope(TA):
        assert approvals.pending() == []
    assert open(os.path.join(cluster.workspace_root, TA, ANA, s["id"], "a.txt")).read() == "écrit"
