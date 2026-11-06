"""`delegate(readonly=true)` (drapeau `delegate_tier`) : le NOYAU contraint l'enfant. Outils filtrés aux READ, tout appel
WRITE/DESTRUCTIVE refusé quel que soit le chemin (boucle, execute_code, script_tool_call, appel direct), effet du
delegate = READ ; fail closed sur un effet inconnu."""
from __future__ import annotations

import dataclasses
import json
import sys


from nokker.core.loop import Kernel
from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, Message, Tier, ToolCall, ToolResult, TurnInput
from nokker.qa.gates import GatesV0
from nokker.qa.invariants import DeterministicInvariants
from nokker.qa.secrets import KnownSecrets
from nokker.tools import delegate as dg
from nokker.tools.delegate import DelegateToolSource, _Child
from nokker.tools.execute_code import TOOL, ExecuteCodeConfig, ExecuteCodeSource
from nokker.tools.registry import Registry, StaticToolSource
from tests.unit.test_loop_approvals import Ctx, Esc, LedgerFake, Mem, Pol, ScriptedProvider, StoreFake

READONLY_DESC = ("true : les sous-agents ne pourront que lire et rendre un rapport ou un diagnostic ; aucune écriture, "
                 "aucune action")
REFUSAL = ("REFUSÉ par le noyau (readonly: sous-agent en lecture seule (readonly=true) : write_file écrit ou agit, il "
           "ne peut que lire ; rends ton rapport ou ton diagnostic).")


class Odd:
    """Effet non déclaré / inconnu."""
    effect = "mystère"
    name = "odd"
    description = ""
    parameters: dict = {"type": "object"}
    effect_of = None

    def effect_for(self, args):
        return self.effect


def parent_source(calls):
    def tool(name, effect, effect_of=None):
        spec = ToolSpec(name=name, description=name, parameters={"type": "object"}, effect=effect, effect_of=effect_of)

        def run(args, ctx):
            calls.append((name, dict(args)))
            return ToolResult("", name, f"{name} ok")
        return spec, run

    return StaticToolSource(entries=[
        tool("read_file", Effect.READ), tool("write_file", Effect.WRITE), tool("rm_all", Effect.DESTRUCTIVE),
        tool("sneaky", Effect.READ, effect_of=lambda a: Effect.WRITE if a.get("w") else Effect.READ),
        tool("broken_class", Effect.READ, effect_of=lambda a: 1 / 0),
        (Odd(), lambda a, c: ToolResult("", "odd", "odd ok")),
    ])


class Sandbox:
    def script_command(self, thread_id, argv):
        return [sys.executable if a == "python3" else a for a in argv]


def make(flag=True, **kw):
    calls: list = []
    src = DelegateToolSource(lambda *a: None, None, tier=flag, **kw)
    base = parent_source(calls)
    xc = ExecuteCodeSource(resolve=lambda ctx: Sandbox(), cfg=ExecuteCodeConfig.from_dict(
        {"tools": ["read_file", "write_file", "rm_all", "sneaky", "odd"]}))
    parent = Registry(sources=[base, xc, src])
    src.bind(parent)
    return src, parent, calls


def child_registry(src, readonly):
    ch = _Child(index=0, id="c1", thread_id="t1/sa-c1", task={"goal": "g"}, schema=None, registry=src._registry,
                readonly=readonly)
    return src._child_registry(ch, None, 0)


def names(reg):
    return {s.name for s in reg._all()}


CTX = ToolContext(thread_id="t1/sa-c1", turn_index=0, channel="subagent", sender="alice")


def test_readonly_child_sees_no_write_tool():
    src, _, _ = make()
    ro = names(child_registry(src, True))
    assert {"read_file", "sneaky", "broken_class"} <= ro  # READ déclarés : listés, jugés appel par appel
    assert not ro & {"write_file", "rm_all", "odd", TOOL, "delegate"}
    assert {"write_file", "rm_all", TOOL} <= names(child_registry(src, False))


def test_forced_write_call_is_refused_by_the_kernel_and_not_executed():
    src, _, calls = make()
    reg = child_registry(src, True)
    for name, args in (("write_file", {"p": 1}), ("rm_all", {}), ("sneaky", {"w": True}), ("broken_class", {}),
                       ("odd", {}), ("inconnu", {})):
        r = reg.call(name, args, CTX)
        assert r.is_error and ("outil inconnu" in r.content or r.content == REFUSAL.replace("write_file", name)), name
    assert calls == []
    # filtré du registre : « inconnu » ici ; le refus motivé vient de la porte du noyau enfant (test suivant) et de la
    # source elle-même si l'outil lui est quand même adressé
    src_w = reg.sources[0]
    r = src_w.call("write_file", {}, CTX)
    assert r.is_error and r.content == REFUSAL
    assert src_w.call("sneaky", {"w": True}, CTX).is_error and src_w.call("broken_class", {}, CTX).is_error
    assert src_w.call("odd", {}, CTX).is_error
    assert not reg.call("read_file", {}, CTX).is_error and calls == [("read_file", {})]
    # readonly absent : l'écriture passe
    assert not child_registry(src, False).call("write_file", {}, CTX).is_error


def kernel_for(reg, readonly=True):
    inv = DeterministicInvariants(known=KnownSecrets())
    script = [Message("assistant", tool_calls=[ToolCall(id="x1", name=TOOL, arguments={"code": CODE})]),
              Message("assistant", "fini")]
    k = Kernel(store=StoreFake(), context=Ctx(), memory=Mem(), invariants=inv, gates=GatesV0(invariants=inv),
               escalator=Esc(), policy=Pol(), providers={Tier.SHIP: ScriptedProvider(script)}, tools=reg,
               ledger=LedgerFake())
    return dataclasses.replace(k, gates=dg._ReadonlyGates(k.gates)) if readonly else k


CODE = ("print(tools.read_file())\n"
        "try:\n    tools.write_file(p=1)\nexcept Exception as e:\n    print('w', e)\n"
        "try:\n    tools.rm_all()\nexcept Exception as e:\n    print('d', e)\n")


def test_execute_code_cannot_write_in_a_readonly_child():
    """Chemin indirect : execute_code exposé à l'enfant (source rendue visible) → script_tool_call → registre de
    l'enfant. L'écriture est refusée par le noyau, jamais exécutée."""
    src, parent, calls = make()
    reg = child_registry(src, True)
    xc = next(s for s in parent.sources if isinstance(s, ExecuteCodeSource))

    class ReadXC:  # pire cas : execute_code mal classé READ, donc ni filtré ni refusé à l'entrée
        core = False

        def specs(self):
            return [dataclasses.replace(sp, effect=Effect.READ) for sp in xc.specs()]

        def call(self, name, arguments, ctx):
            return xc.call(name, arguments, ctx)

    reg.sources.append(ReadXC())
    k = kernel_for(reg)
    turn = k.run_turn(TurnInput("t1/sa-c1", "go", sender="alice"))
    block = next((m.content for m in turn.messages if m.role == "tool"), "")
    assert ("read_file", {}) in calls and not [c for c in calls if c[0] in ("write_file", "rm_all")]
    assert block.count("outil inconnu") >= 2 or "REFUSÉ" in block  # write_file et rm_all refusés dans le script


def test_script_tool_call_refuses_write_in_a_readonly_child():
    src, _, calls = make()
    k = kernel_for(child_registry(src, True))
    turn = k.script_turn(CTX)
    out = k.script_tool_call("write_file", {"p": 1}, turn, CTX)
    assert out["status"] == "error" and "outil inconnu" in out["content"]  # filtré : le noyau ne le connaît pas
    # outil READ déclaré mais classé WRITE pour CET appel : la porte du noyau enfant refuse avec le motif
    out = k.script_tool_call("sneaky", {"w": True}, turn, CTX)
    assert out["status"] == "refused" and "readonly" in out["content"] and "lecture seule" in out["content"]
    out = k.script_tool_call("rm_all", {}, turn, CTX)
    assert calls == [] and out["status"] != "ok"
    # même chemin, enfant ordinaire : l'écriture passe
    k2 = kernel_for(child_registry(src, False), readonly=False)
    assert k2.script_tool_call("write_file", {}, k2.script_turn(CTX), CTX)["status"] == "ok"


def test_direct_registry_path_with_unwrapped_effect_is_refused_through_child_source():
    src, _, calls = make()
    reg = child_registry(src, True)
    assert reg.call("write_file", {}, CTX).is_error and calls == []


def test_delegate_effect_is_read_when_readonly_so_no_strong_card():
    src, *_ = make()
    spec = src.specs()[0]
    assert spec.effect_for({"tasks": [], "readonly": True, "isolate": False}) is Effect.READ
    assert spec.effect_for({"tasks": [], "readonly": True}) is Effect.READ
    # readonly absent ou faux : règle d'isolation inchangée
    assert spec.effect_for({"tasks": [], "isolate": False}) is Effect.DESTRUCTIVE
    assert spec.effect_for({"tasks": [], "readonly": False, "isolate": False}) is Effect.DESTRUCTIVE
    assert spec.effect_for({"tasks": []}) is Effect.DESTRUCTIVE  # pas de dossier de travail


def test_flag_off_no_readonly_parameter_and_effect_unchanged():
    src = DelegateToolSource(lambda *a: None, None)
    spec = src.specs()[0]
    assert spec.parameters is dg._SPECS_PARAMS and "readonly" not in spec.parameters["properties"]
    assert spec.effect_for({"readonly": True}) is Effect.DESTRUCTIVE  # ignoré, drapeau éteint
    bg = DelegateToolSource(lambda *a: None, None, background=True)
    assert "readonly" not in bg.specs()[0].parameters["properties"]


def test_flag_on_adds_the_readonly_parameter_only():
    src, *_ = make()
    p = src.specs()[0].parameters
    assert p["properties"]["readonly"] == {"type": "boolean", "default": False, "description": READONLY_DESC}
    assert "readonly" not in p["required"]


def test_flag_off_child_is_not_readonly_even_if_asked():
    src = DelegateToolSource(lambda *a: None, None)
    assert dg._READONLY_PARAM["description"] == READONLY_DESC
    assert not (src.tier and {"readonly": True}.get("readonly") is True)


def test_call_marks_the_child_readonly_only_when_asked():
    seen = []

    class K:
        def run_turn(self, inp, stream=None):
            from nokker.core.turn import InputCheck, Turn
            return Turn(thread_id=inp.thread_id, index=0, input=InputCheck(text=""), reply="fait")

    src, parent, _ = make()
    src.kernel_factory = lambda reg, tid, obs: (seen.append(set(names(reg))), K())[1]
    ctx = ToolContext(thread_id="t1", turn_index=0, channel="api", sender="alice", kernel=K())
    for extra in ({"readonly": True}, {}, {"readonly": False}):
        r = src.call("delegate", {"tasks": [{"goal": "g"}], "isolate": False, **extra}, ctx)
        assert not r.is_error and json.loads(r.content)[0]["status"] != "failed"
    assert "write_file" not in seen[0] and "write_file" in seen[1] and "write_file" in seen[2]


def test_run_child_wraps_the_gates_of_a_readonly_child_only():
    import dataclasses as dc

    from nokker.core.turn import InputCheck, Turn

    @dc.dataclass
    class K:
        gates: object
        kids: list

        def run_turn(self, inp, stream=None):
            return Turn(thread_id=inp.thread_id, index=0, input=InputCheck(text=""), reply="fait")

    src, _, _ = make()
    made = []
    src.kernel_factory = lambda reg, tid, obs: (made.append(K(gates=object(), kids=[])), made[-1])[1]
    ctx = ToolContext(thread_id="t1", turn_index=0, channel="api", sender="alice", kernel=K(gates=None, kids=[]))
    for extra in ({"readonly": True}, {}):
        src.call("delegate", {"tasks": [{"goal": "g"}], "isolate": False, **extra}, ctx)


    assert len(made) == 2
    # le noyau EXÉCUTÉ est la copie à portes enveloppées (la fabrique rend l'original) : on le vérifie via _run_child
    seen = []
    orig = dg._ReadonlyGates.action
    dg._ReadonlyGates.action = lambda self, *a: (seen.append(1), orig(self, *a))[1]
    try:
        class G:
            def action(self, *a):
                return []
        K.run_turn = lambda self, inp, stream=None: (
            seen.append(type(self.gates).__name__),
            Turn(thread_id=inp.thread_id, index=0, input=InputCheck(text=""), reply="fait"))[1]
        src.kernel_factory = lambda reg, tid, obs: K(gates=G(), kids=[])
        for extra in ({"readonly": True}, {}):
            src.call("delegate", {"tasks": [{"goal": "g"}], "isolate": False, **extra}, ctx)
    finally:
        dg._ReadonlyGates.action = orig
    assert seen == ["_ReadonlyGates", "G"]


# ───────────────────── provenance : héritage des sources lues et refus à la place de la carte ─────────────────────

from nokker.approvals.memory import InMemoryApprovals  # noqa: E402
from nokker.qa.provenance import build_provenance  # noqa: E402

DENIED_SEARCH = ("REFUSÉ par le noyau (provenance:recherche: provenance : la valeur « kiwi7788 » (argument query) vient "
                 "du contenu d'un mail")


def prov_source(calls):
    def tool(name, content):
        spec = ToolSpec(name=name, description=name, parameters={"type": "object"}, effect=Effect.READ)

        def run(args, ctx):
            calls.append((name, dict(args)))
            return ToolResult("", name, content)
        return spec, run

    return StaticToolSource(entries=[
        tool("mail_read", "From: x@y.ch\n\nle code projet est kiwi7788"),
        tool("memory_get", "note : le code projet est kiwi7788"),
        tool("web_search", "résultats"),
    ])


def prov_kernel(registry, guard, approvals, readonly, calls_script):
    inv = DeterministicInvariants(known=KnownSecrets())
    script = [Message("assistant", tool_calls=[ToolCall(id=f"c{i}", name=n, arguments=a)])
              for i, (n, a) in enumerate(calls_script)] + [Message("assistant", "fini")]
    k = Kernel(store=StoreFake(), context=Ctx(), memory=Mem(), invariants=inv, gates=GatesV0(invariants=inv),
               escalator=Esc(), policy=Pol(), providers={Tier.SHIP: ScriptedProvider(script)}, tools=registry,
               ledger=LedgerFake(), approvals=approvals, provenance=guard)
    if readonly:
        k = dataclasses.replace(k, gates=dg._ReadonlyGates(k.gates), provenance=dg._ReadonlyProvenance(k.provenance))
    return k


def prov_child_registry(calls, readonly):
    src = DelegateToolSource(lambda *a: None, None, tier=True)
    parent = Registry(sources=[prov_source(calls), src])
    src.bind(parent)
    return child_registry_for(src, readonly), parent


def child_registry_for(src, readonly):
    ch = _Child(index=0, id="c1", thread_id="t1/sa-c1", task={"goal": "g"}, schema=None, registry=src._registry,
                readonly=readonly)
    return src._child_registry(ch, None, 0)


def tool_texts(turn):
    return [m.content for m in turn.messages if m.role == "tool"]


def test_readonly_child_mail_read_then_web_search_with_the_value_is_refused_never_run():
    calls: list = []
    guard = build_provenance({"enabled": True})
    reg, _ = prov_child_registry(calls, True)
    appr = InMemoryApprovals()
    k = prov_kernel(reg, guard, appr, True, [("mail_read", {}), ("web_search", {"query": "kiwi7788"})])
    turn = k.run_turn(TurnInput("t1/sa-c1", "cherche", sender="alice", channel="subagent"))
    texts = tool_texts(turn)
    assert [c[0] for c in calls] == ["mail_read"]  # web_search jamais exécuté
    assert texts[1].startswith(DENIED_SEARCH) and "valeur lue ailleurs est refusée.)" in texts[1] and "carte forte" not in texts[1]
    assert appr.pending() == [] and not turn.pending_approvals  # pas de carte, pas de passage silencieux


def test_child_inherits_what_the_parent_read_readonly_refused_and_normal_gets_a_card():
    for readonly in (True, False):
        calls: list = []
        guard = build_provenance({"enabled": True})
        reg, parent_reg = prov_child_registry(calls, readonly)
        appr = InMemoryApprovals()
        pk = prov_kernel(parent_reg, guard, appr, False, [("memory_get", {})])
        pk.run_turn(TurnInput("t1", "regarde la note", sender="alice"))  # le parent lit la mémoire
        k = prov_kernel(reg, guard, appr, readonly, [("web_search", {"query": "kiwi7788"})])
        turn = k.run_turn(TurnInput("t1/sa-c1", "cherche", sender="alice", channel="subagent"))
        assert [c[0] for c in calls] == ["memory_get"], readonly  # le web_search de l'enfant n'a pas tourné
        if readonly:
            assert tool_texts(turn)[0].startswith("REFUSÉ par le noyau (provenance:recherche: provenance : ")
            assert "contenant une valeur lue ailleurs est refusée.)" in tool_texts(turn)[0] and appr.pending() == []
        else:
            pend = appr.pending()
            assert [p.tool_name for p in pend] == ["web_search"] and pend[0].rule == "provenance:recherche"
            assert "EN ATTENTE" in tool_texts(turn)[0]


def test_provenance_off_is_unchanged_in_a_readonly_child():
    calls: list = []
    reg, _ = prov_child_registry(calls, True)
    k = prov_kernel(reg, None, None, True, [("mail_read", {}), ("web_search", {"query": "kiwi7788"})])
    k2 = dataclasses.replace(k, provenance=None)
    turn = k2.run_turn(TurnInput("t1/sa-c1", "cherche", sender="alice", channel="subagent"))
    assert [c[0] for c in calls] == ["mail_read", "web_search"] and "refus" not in " ".join(tool_texts(turn))


def test_user_card_rule_on_a_read_tool_is_a_refusal_in_a_readonly_child():
    from nokker.core.turn import Decision, Gate, Verdict

    class AskReads:  # règle de l'utilisateur : « carte » sur toute lecture
        def __init__(self, inner):
            self.inner = inner

        def action(self, call, spec, turn):
            return [Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="user:ask-reads",
                            reason="lecture : demander", strength="standard", subject=call.id)]

        def switch(self, reply, turn, tier):
            return self.inner.switch(reply, turn, tier)

    for readonly in (True, False):
        calls: list = []
        reg, _ = prov_child_registry(calls, readonly)
        appr = InMemoryApprovals()
        k = prov_kernel(reg, None, appr, False, [("web_search", {"query": "x"})])
        k = dataclasses.replace(k, gates=AskReads(k.gates))
        if readonly:
            k = dataclasses.replace(k, gates=dg._ReadonlyGates(k.gates))
        turn = k.run_turn(TurnInput("t1/sa-c1", "cherche", sender="alice", channel="subagent"))
        assert calls == []  # jamais exécuté
        if readonly:
            assert tool_texts(turn)[0].startswith("REFUSÉ par le noyau (user:ask-reads: lecture : demander)")
            assert appr.pending() == [] and not turn.pending_approvals
        else:
            assert len(appr.pending()) == 1  # enfant ordinaire : la carte reste
