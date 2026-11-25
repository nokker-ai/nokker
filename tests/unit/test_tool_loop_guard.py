"""Garde-fou natif contre les boucles d'outils de lecture (`nokker/core/tool_loop_guard.py`, docs/HOST-EMBED.md §14).

Les séquences « boucle » sont celles mesurées sur un banc réel (Ship 27B, 3 passes) : `docs_search` avec le même mot et un `k` qui
double, `ui_targets` avec un filtre qui change à chaque appel, résultat constant. Les séquences « légitimes » doivent passer
sans note : lectures de fichiers différents, recherches différentes, pagination, commandes de terminal différentes."""
from __future__ import annotations

import itertools

import pytest

from nokker.core import session_opts
from nokker.core import tool_loop_guard as tlg
from nokker.core.loop import Kernel
from nokker.core.model_texts import ALL_TABLES
from nokker.core.ports import ToolSpec
from nokker.core.tool_loop_guard import Thresholds, ToolLoopGuard
from nokker.core.turn import Effect, Message, Tier, ToolCall, ToolResult, TurnInput
from tests.unit.test_loop_contract import (
    Ctx, Esc, GatesFake, Inv, LedgerFake, Mem, Pol, ScriptedProvider, StoreFake,
)

NO_HIT = '{"results": [{"id": "mbdyn-input-0412", "title": "Max Iterations", "text": "max iterations: <number>"}]}'
EMPTY_UI = '{"targets": [], "screen": "graph"}'


def run_guard(seq, limits=None):
    """seq = [(outil, arguments, résultat)] → liste des actions rendues par le garde-fou."""
    g = ToolLoopGuard(limits or Thresholds())
    return [g.after(tool, args, out) for tool, args, out in seq]


def actions(seq, limits=None):
    return [a for a, _n in run_guard(seq, limits)]


# ── séquences réelles du banc (boucles B01 et B04) ─────────────────────────────────────────────────────────────────
def b01_sequence(n):
    ks = [None, 10, 10, 10, 20, 20, 30, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000]
    qs = ["magnetic bearing element syntax", "magnetic", "bearing", "magnetic bearing", "magnetic", "magnetic bearing element"]
    out = []
    for i in range(n):
        q = qs[i] if i < len(qs) else "magnetic"
        args = {"query": q, "solver": "mbdyn", **({"k": ks[i]} if ks[i] else {})}
        out.append(("docs_search", args, NO_HIT))
    return out


def b04_sequence(filters):
    return [("ui_targets", {"filter": f} if f else {}, EMPTY_UI) for f in filters]


def test_b01_docs_search_with_a_doubling_k_is_caught_then_suspended():
    acts = actions(b01_sequence(19))
    assert acts[:3] == [None, None, None]
    assert acts[3] == "note"  # 4e appel au résultat identique
    assert acts[7] == "suspend"  # 8e : plafond
    assert acts.index("suspend") == 7


def test_b01_short_runs_of_the_other_passes_get_the_note():
    """Passes 2 et 3 : ×4 et ×5, les mots changent (« bearing », « magnetic bearing ») et le résultat ne bouge pas."""
    assert actions(b01_sequence(4))[-1] == "note"
    assert actions(b01_sequence(5))[-2:] == ["note", "note"]


def test_b04_ui_targets_with_a_different_filter_each_time_is_caught():
    """Passe 3 : 17 filtres tous différents (plot, graph, freq, Hz, peak…), la liste des cibles est toujours vide."""
    filters = ["plot", "graph", "graph.node", "req", "plot.point", "fréquence", "frf", "réponse", "amp", "Hz", "freq",
               "courbe", "pic", "peak", "plot.zone", "plot.point:", "plot.point:freq"]
    acts = actions(b04_sequence(filters))
    assert acts[3] == "note" and acts[7] == "suspend"


def test_b04_progressive_filter_is_caught_with_a_navigation_between():
    """Passe 1 : le filtre s'allonge (`plot.point:freq@100..100..100`) ; ui_open/ui_navigate (écritures) entre deux
    ne remettent pas le compteur à zéro."""
    g = ToolLoopGuard(Thresholds())
    got = []
    for f in ["plot", "graph", None, "plot", "x", "graph.node", "req", "plot.point", "plot.zone", "plot.point:",
              "plot.point:freq", "plot.point:freq@", "plot.point:freq@100"]:
        got.append(g.after("ui_targets", {"filter": f} if f else {}, EMPTY_UI)[0])
    assert "note" in got and got[-1] == "suspend"


# ── usages légitimes : jamais de note ──────────────────────────────────────────────────────────────────────────────
def test_read_file_of_different_files_is_never_caught():
    seq = [("read_file", {"path": f"src/mod{i}.py"}, f"def f{i}():\n    return {i}\n" * 5) for i in range(15)]
    assert set(actions(seq)) == {None}


def test_read_file_of_near_identical_paths_with_different_content_is_not_caught():
    seq = [("read_file", {"path": f"src/pkg/module_{i}.py"}, f"class C{i}:\n    value = {i * 7}\n" + "x = 1\n" * 20)
           for i in range(12)]
    assert set(actions(seq)) == {None}


def test_empty_files_are_not_a_loop():
    """Six `__init__.py` vides : le contenu est littéralement vide, seuls des arguments voisins pourraient le grouper."""
    seq = [("read_file", {"path": f"{d}/__init__.py"}, "") for d in ("a", "b", "c", "d", "e", "f")]
    assert "note" not in actions(seq)


def test_search_files_with_different_queries_and_different_results_is_not_caught():
    seq = [("search_files", {"query": q}, f"{q}.py:12: def {q}()\n{q}.py:40: {q}()") for q in
           ("parse_args", "load_config", "retry", "timeout", "cache", "render", "emit", "close", "flush", "merge")]
    assert set(actions(seq)) == {None}


def test_three_empty_searches_with_really_different_queries_stay_silent():
    seq = [("search_files", {"query": q}, "[]") for q in ("alpha", "beta", "gamma")]
    assert set(actions(seq)) == {None}


def test_empty_result_echoing_the_query_is_one_cluster():
    """« aucun résultat pour 'foo' » et pour 'bar' : le même résultat vide."""
    seq = [("search_files", {"query": q}, f"Aucun résultat pour '{q}'") for q in ("foo", "bar", "baz", "qux")]
    assert actions(seq)[-1] == "note"


def test_pagination_with_new_results_each_page_is_not_caught():
    seq = [("docs_search", {"query": "magnetic", "solver": "mbdyn", "page": p},
            "\n".join(f"mbdyn-input-{p}{j:03d} section {p}-{j}" for j in range(10))) for p in range(1, 9)]
    assert set(actions(seq)) == {None}


def test_same_tool_three_times_with_really_different_arguments_and_results():
    seq = [("memory_get", {"note": "a"}, "contenu de la note a"), ("memory_get", {"note": "zeta"}, "autre contenu"),
           ("memory_get", {"note": "mu"}, "troisième texte différent")]
    assert set(actions(seq)) == {None}


def test_different_tools_do_not_share_a_cluster():
    seq = [(t, {"q": "x"}, "[]") for t in ("a", "b", "c", "d", "e", "f", "g", "h")]
    assert set(actions(seq)) == {None}


def test_custom_thresholds():
    acts = actions(b01_sequence(6), Thresholds(2, 4))
    assert acts == [None, "note", "note", "suspend", "suspend", "suspend"]


def test_empty_detection():
    for empty in ("", "  ", "[]", "{}", "null", '{"results": [], "count": 0}', '{"a": {"b": []}}', "No matches found",
                  "Aucun résultat pour 'x'", "0 results"):
        assert tlg.is_empty_result(empty), empty
    for full in ('{"results": [1]}', "def f(): pass", "[0, 1]", "found 3 matches in a.py"):
        assert not tlg.is_empty_result(full), full


def test_args_near():
    assert tlg.args_near({"q": "Magnetic  Bearing", "k": 10}, {"q": "magnetic bearing", "k": 20000})
    assert not tlg.args_near({"q": "magnetic bearing element"}, {"q": "magnetic bearing elements"})
    assert not tlg.args_near({"path": "src/m1.py"}, {"path": "src/m2.py"})  # deux chemins voisins sont deux fichiers
    assert not tlg.args_near({"q": "x"}, {"q": "x", "k": 1})
    assert not tlg.args_near({"q": "x", "flag": True}, {"q": "x", "flag": False})


def test_nearly_identical_results_with_near_arguments_form_a_cluster():
    """Le classement bouge d'un score : résultat presque identique, arguments voisins."""
    base = "\n".join(f"mbdyn-input-{i:04d} Max Iterations score=0.{40 + i}" for i in range(30))
    seq = [("docs_search", {"query": "magnetic bearing", "k": 10 * (i + 1)}, base.replace("score=0.40", f"score=0.{i}1"))
           for i in range(5)]
    assert actions(seq)[-1] == "note"
    # même corps de résultat mais requêtes voisines (chemins) : pas une grappe
    seq = [("read_file", {"path": f"src/m{i}.py"}, base.replace("score=0.40", f"score=0.{i}1")) for i in range(8)]
    assert set(actions(seq)) == {None}


def test_validate_shapes():
    assert tlg.validate(None) == (None, None)
    assert tlg.validate(False) == (False, None)
    assert tlg.validate(True) == (Thresholds(), None)
    assert tlg.validate({"note_after": 3, "suspend_after": 6}) == (Thresholds(3, 6), None)
    assert tlg.validate({"note_after": 3}) == (Thresholds(3, 8), None)
    for bad in ("false", 0, 1, [], {}, {"note_after": 1, "suspend_after": 5}, {"note_after": 5, "suspend_after": 5},
                {"note_after": 3, "suspend_after": 500}, {"note_after": "4"}, {"note_after": True, "suspend_after": 9},
                {"note_after": 4, "suspend_after": 8, "extra": 1}):
        assert tlg.validate(bad)[1] == tlg.ERROR, bad


def test_resolve_session_then_global_then_default():
    assert tlg.resolve(None, None) == Thresholds()
    assert tlg.resolve(None, False) is None
    assert tlg.resolve(None, Thresholds(3, 5)) == Thresholds(3, 5)
    assert tlg.resolve(False, Thresholds(3, 5)) is None
    assert tlg.resolve(Thresholds(6, 9), False) == Thresholds(6, 9)


def test_texts_are_in_four_languages():
    for key in ("gate.tool_loop_note", "gate.tool_loop_suspended"):
        assert set(ALL_TABLES[key]) == {"fr", "en", "de", "it"}
    assert "{tool}" in ALL_TABLES["gate.tool_loop_note"]["it"] and "{n}" in ALL_TABLES["gate.tool_loop_note"]["de"]


# ── dans la boucle du noyau ────────────────────────────────────────────────────────────────────────────────────────
_ids = itertools.count(1)


def call(name, **args):
    return ToolCall(id=f"c{next(_ids)}", name=name, arguments=args)


class LoopTools:
    """Outils : `docs_search` (résultat constant), `read_file` (contenu selon le chemin), `terminal` (écriture, sortie
    selon la commande), `process_logs` (lecture `fresh`, sortie constante)."""
    specs = {"docs_search": ToolSpec("docs_search", "doc", {}, Effect.READ),
             "read_file": ToolSpec("read_file", "lit", {}, Effect.READ),
             "terminal": ToolSpec("terminal", "commande", {}, Effect.WRITE),
             "process_logs": ToolSpec("process_logs", "journal", {}, Effect.READ, fresh=True)}

    def __init__(self):
        self.called = []

    def core(self):
        return []

    def spec(self, n):
        return self.specs.get(n)

    def find(self, q, limit=5):
        return []

    def index(self):
        return []

    def call(self, n, a, ctx):
        self.called.append((n, dict(a)))
        if n == "read_file":
            return ToolResult(call_id="", name=n, content=f"contenu de {a.get('path')} " + str(hash(a.get("path")) % 997))
        if n == "terminal":
            return ToolResult(call_id="", name=n, content=f"sortie de {a.get('command')}")
        return ToolResult(call_id="", name=n, content=NO_HIT)


class Observer:
    def __init__(self):
        self.events = []
        self.results = []

    def tool_loop_guard(self, turn, data):
        self.events.append(dict(data))

    def tool_result(self, turn, tc, result, ms):
        self.results.append(result.content)

    def tool_call(self, turn, tc):
        pass


def kernel(script, **opts):
    inv, tools = Inv(), LoopTools()
    k = Kernel(store=StoreFake(), context=Ctx(), memory=Mem(), invariants=inv, gates=GatesFake(inv), escalator=Esc(),
               policy=Pol(), providers={Tier.SHIP: ScriptedProvider(script), Tier.DEEP: ScriptedProvider([])},
               tools=tools, ledger=LedgerFake(), max_calls=40, **opts)
    k.observer = Observer()
    return k, tools


def tool_texts(turn):
    return [m.content for m in turn.messages if m.role == "tool"]


def many(name, argsets):
    return [Message("assistant", tool_calls=[call(name, **a)]) for a in argsets]


@pytest.fixture(autouse=True)
def _clean_registry():
    session_opts.tool_loop_guard_registry().clear("t1")
    yield
    session_opts.tool_loop_guard_registry().clear("t1")


def b01_args(n):
    return [a for _t, a, _o in b01_sequence(n)]


def test_loop_adds_a_note_then_suspends_the_tool_for_the_rest_of_the_turn():
    k, tools = kernel([*many("docs_search", b01_args(12)), Message("assistant", "je conclus")])
    turn = k.run_turn(TurnInput("t1", "syntaxe de magnetic bearing ?"))
    out = tool_texts(turn)
    assert "Tu viens d'appeler docs_search 4 fois" in out[3] and "Tu viens d'appeler" not in out[2]
    assert len(tools.called) == 8  # les 8 premiers appels partent, les suivants sont suspendus sans être exécutés
    assert all("est suspendu pour ce tour" in t for t in out[8:])
    assert "docs_search est suspendu pour ce tour : trop d'appels sans résultat nouveau ; continue sans lui" in out[8]
    assert turn.reply == "je conclus"


def test_loop_events_carry_no_argument_and_come_once_per_step():
    k, _tools = kernel([*many("docs_search", b01_args(10)), Message("assistant", "ok")])
    k.run_turn(TurnInput("t1", "x"))
    assert k.observer.events == [{"tool": "docs_search", "n": 4, "action": "note"},
                                 {"tool": "docs_search", "n": 8, "action": "suspend"}]


def test_the_note_goes_to_the_model_message_only_not_to_the_result_events():
    k, _tools = kernel([*many("docs_search", b01_args(5)), Message("assistant", "ok")])
    turn = k.run_turn(TurnInput("t1", "x"))
    assert "Tu viens d'appeler" in tool_texts(turn)[3]
    assert not any("Tu viens d'appeler" in r for r in k.observer.results)


def test_stall_detection_still_sees_identical_repeats_under_the_guard():
    k, _tools = kernel([*many("docs_search", [{"query": "a"}] * 5), Message("assistant", "je conclus")], stall_limit=3)
    turn = k.run_turn(TurnInput("t1", "x"))
    assert [v for v in turn.verdicts if v.rule == "loop:stalled"] and turn.reply == "je conclus"


def test_loop_note_does_not_pollute_the_turn_cache():
    """Un appel strictement identique, resservi par le mémo du tour, ne reprend pas la note de l'appel d'origine."""
    k, _tools = kernel([*many("docs_search", [{"query": "a"}] * 3 + [{"query": "b"}, {"query": "c"}]),
                        Message("assistant", "ok")])
    turn = k.run_turn(TurnInput("t1", "x"))
    out = tool_texts(turn)
    assert out[0] == NO_HIT and "déjà appelé" in out[1]
    assert "Tu viens d'appeler" not in out[0]


def test_exact_repeats_served_from_the_turn_cache_count_too():
    k, tools = kernel([*many("docs_search", [{"query": "a"}] * 6), Message("assistant", "ok")])
    turn = k.run_turn(TurnInput("t1", "x"))
    assert len(tools.called) == 1
    assert "Tu viens d'appeler docs_search 4 fois" in tool_texts(turn)[3]


def test_read_file_over_many_files_runs_untouched():
    k, tools = kernel([*many("read_file", [{"path": f"src/m{i}.py"} for i in range(14)]), Message("assistant", "lu")])
    turn = k.run_turn(TurnInput("t1", "lis tout"))
    assert len(tools.called) == 14
    assert not any("Tu viens d'appeler" in t or "suspendu" in t for t in tool_texts(turn))
    assert k.observer.events == []


def test_terminal_with_different_commands_is_not_a_loop():
    k, tools = kernel([*many("terminal", [{"command": f"echo {i}"} for i in range(12)]), Message("assistant", "fait")])
    turn = k.run_turn(TurnInput("t1", "lance"))
    assert len(tools.called) == 12
    assert not any("Tu viens d'appeler" in t or "suspendu" in t for t in tool_texts(turn))


def test_effect_tools_are_never_noted_nor_suspended():
    """Écriture, terminal, outil MCP non déclaré en lecture, outil à carte : 12 appels aux résultats identiques, ni note ni suspension."""
    names = {"write_file": Effect.WRITE, "patch": Effect.WRITE, "terminal": Effect.WRITE, "mcp_unknown": Effect.WRITE,
             "send_card": Effect.DESTRUCTIVE}
    for name, eff in names.items():
        k, tools = kernel([*many(name, [{"x": 1}] * 12), Message("assistant", "ok")])
        tools.specs = {**LoopTools.specs, name: ToolSpec(name, "effet", {}, eff)}
        turn = k.run_turn(TurnInput("t1", "fais"))
        out = tool_texts(turn)
        assert not any("Tu viens d'appeler" in x or "suspendu" in x for x in out), name
        assert k.observer.events == [], name


def test_fresh_reads_are_exempt():
    k, tools = kernel([*many("process_logs", [{"id": "bg_1"}] * 10), Message("assistant", "vu")])
    turn = k.run_turn(TurnInput("t1", "le serveur ?"))
    assert len(tools.called) == 10
    assert not any("Tu viens d'appeler" in t or "suspendu" in t for t in tool_texts(turn))


def test_session_false_disables_the_guard():
    session_opts.tool_loop_guard_registry().set("t1", False)
    k, tools = kernel([*many("docs_search", b01_args(12)), Message("assistant", "ok")])
    turn = k.run_turn(TurnInput("t1", "x"))
    assert len(tools.called) == 12 and not any("Tu viens d'appeler" in t or "suspendu" in t for t in tool_texts(turn))


def test_session_thresholds_override_the_global_ones():
    session_opts.tool_loop_guard_registry().set("t1", Thresholds(2, 3))
    k, tools = kernel([*many("docs_search", b01_args(6)), Message("assistant", "ok")], tool_loop_guard=False)
    turn = k.run_turn(TurnInput("t1", "x"))
    assert len(tools.called) == 3
    assert "Tu viens d'appeler docs_search 2 fois" in tool_texts(turn)[1]


def test_global_false_disables_it_and_global_thresholds_apply():
    k, tools = kernel([*many("docs_search", b01_args(12)), Message("assistant", "ok")], tool_loop_guard=False)
    k.run_turn(TurnInput("t1", "x"))
    assert len(tools.called) == 12
    k, tools = kernel([*many("docs_search", b01_args(12)), Message("assistant", "ok")], tool_loop_guard=Thresholds(3, 5))
    k.run_turn(TurnInput("t1", "x"))
    assert len(tools.called) == 5


def test_sub_agent_thread_inherits_the_session_option():
    session_opts.tool_loop_guard_registry().set("t1", Thresholds(2, 3))
    assert session_opts.tool_loop_guard_registry().get("t1/sa-abc123") == Thresholds(2, 3)
    assert session_opts.tool_loop_guard_registry().get("t2") is None


def test_note_follows_the_language_of_the_turn():
    k, _tools = kernel([*many("docs_search", b01_args(5)), Message("assistant", "ok")])
    turn = k.run_turn(TurnInput("t1", "syntax of the magnetic bearing?", lang="en", lang_recognized=True))
    assert "You just called docs_search 4 times without getting a new result" in tool_texts(turn)[3]


def test_each_trigger_is_persisted_as_a_ledger_verdict_without_arguments():
    k, _tools = kernel([*many("docs_search", b01_args(10)), Message("assistant", "ok")])
    turn = k.run_turn(TurnInput("t1", "x"))
    rows = [v for v in turn.verdicts if v.rule == "loop:tool_loop_guard"]
    assert [v.reason for v in rows] == ["tool=docs_search n=4 action=note", "tool=docs_search n=8 action=suspend"]
    assert all(v.layer == 0 and v.subject for v in rows)
    assert [v.rule for v in k.ledger.verdicts].count("loop:tool_loop_guard") == 2  # écrits au ledger aussi
