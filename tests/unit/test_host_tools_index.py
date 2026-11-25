"""Index des outils en `prefix: "host"` (docs/HOST-EMBED.md §13) : rendu seulement s'il reste des outils DIFFÉRÉS ; les autres préfixes
gardent l'index tel qu'avant, octet pour octet, même quand tous les schémas sont déjà chargés."""
from __future__ import annotations

from pathlib import Path

import pytest

import nokker
from nokker.context import prefix_mode
from nokker.context.engine import ContextEngineV0
from nokker.core.loop import Kernel
from nokker.core.turn import Tier, TurnInput
from nokker.ledger.costs import InMemoryLedger
from nokker.local.api import LOCAL_OWNER
from nokker.store.pg import MemoryStore
from tests.unit.api.test_server import AllowGates, AllowInvariants, NoEscalator, NullMem, ShipOnlyPolicy
from tests.unit.test_host_system_prompt import Capture, _tools

HEADER = "[Outils disponibles"


@pytest.fixture(autouse=True)
def _clean():
    prefix_mode.registry()._by_thread.clear()
    yield
    prefix_mode.registry()._by_thread.clear()


def _kernel(cap, loading, n_tools=18):
    path = Path(nokker.__file__).parent / "prompts" / "prefix-lokkal-code.md"
    tools = _tools(n_tools)
    return Kernel(store=MemoryStore(), context=ContextEngineV0(tools=tools, prefix_path=str(path)), memory=NullMem(),
                  invariants=AllowInvariants(), gates=AllowGates(), escalator=NoEscalator(), policy=ShipOnlyPolicy(),
                  providers={Tier.SHIP: cap}, tools=tools, ledger=InMemoryLedger(), tool_loading=loading)


def _system(loading, thread, mode=None, n_tools=18):
    if mode:
        prefix_mode.registry().set(thread, mode)
    cap = Capture()
    _kernel(cap, loading, n_tools).run_turn(TurnInput(thread_id=thread, user_text="salut", channel="api", sender=LOCAL_OWNER))
    return cap.system, cap.calls[-1]["tools"]


def test_host_mode_without_deferred_tools_renders_no_index():
    system, tools = _system("all", "th_a", "host")
    assert HEADER not in system and "srv_tool_00" not in system  # tout est dans `tools`
    assert len([t for t in tools if "srv_tool_" in str(t)]) == 18


def test_host_mode_with_deferred_tools_keeps_the_index():
    system, tools = _system("relevance", "th_b", "host")
    assert HEADER in system and "srv_tool_00" in system
    assert not [t for t in tools if "srv_tool_" in str(t)]  # rien de chargé : tout est différé


def test_default_prefix_keeps_the_index_byte_for_byte_even_when_everything_is_loaded():
    sys_all, _ = _system("all", "th_c")
    sys_rel, _ = _system("relevance", "th_d")
    assert HEADER in sys_all and HEADER in sys_rel
    assert sys_all == sys_rel  # l'index ne dépend pas des schémas chargés


def test_index_is_unchanged_by_the_loaded_argument_outside_host_mode():
    path = Path(nokker.__file__).parent / "prompts" / "prefix-lokkal-code.md"
    eng = ContextEngineV0(tools=_tools(5), prefix_path=str(path))
    everything = eng.tools.find("", limit=100)
    assert eng.tools_index_block() == eng.tools_index_block(everything) and HEADER in eng.tools_index_block(everything)


def test_host_mode_index_comes_back_when_a_tool_is_still_deferred():
    path = Path(nokker.__file__).parent / "prompts" / "prefix-lokkal-code.md"
    eng = ContextEngineV0(tools=_tools(5), prefix_path=str(path))
    everything = eng.tools.find("", limit=100)
    from nokker.tools.scope import thread_scope

    prefix_mode.registry().set("th_e", "host")
    with thread_scope("th_e"):
        assert eng.tools_index_block(everything) == ""
        assert HEADER in eng.tools_index_block(everything[1:])
        assert HEADER in eng.tools_index_block(None)  # appelant sans la liste : comportement d'avant
