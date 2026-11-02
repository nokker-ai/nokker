"""Fork de sous-agent (`delegate(fork=true)`, nokker/context/fork.py) : drapeau éteint = schéma et enfant d'avant ;
allumé, l'enfant part du contexte EXACT du dernier appel du parent (préfixe octet pour octet, mêmes schémas, même
affinité de cache) et rend son résumé comme un enfant classique. Cloisonnement : la base d'un fil ne sert qu'au même
locataire et au même tour."""
from __future__ import annotations

import json

from bench.advisor_fork.rig import StepModel, child_factory
from bench.conv.runner import HERE as CONV, build_rig, render
from nokker.core.ports import Assembled, ToolContext, ToolSpec
from nokker.core.turn import Effect, InputCheck, Tier, ToolResult, Turn, TurnInput
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from nokker.tools import delegate as dg
from nokker.tools.delegate import FORK_NOTE, FORK_UNAVAILABLE, DelegateToolSource


class Docs:
    core = False

    def specs(self):
        return [ToolSpec("lire_doc", "lire un document", {"type": "object", "properties": {}}, Effect.READ)]

    def call(self, name, args, ctx):
        return ToolResult(call_id="", name=name, content="contenu du document " * 50)


class ReadOnlyDelegate(DelegateToolSource):
    def _effect_of(self, args):
        return Effect.READ


def step_for(fork):
    def step(messages):
        i = max(k for k, m in enumerate(messages) if m.get("role") == "user")
        text = str(messages[i].get("content") or "")
        after = [m.get("name") for m in messages[i + 1:] if m.get("role") == "tool"]
        if "ENFANT" in text:
            return ("résumé de l'enfant", []) if after else ("", [("lire_doc", {})])
        if "délègue" not in text:
            return "Noté.", []
        if "delegate" in after:
            return "terminé", []
        if "lire_doc" in after:
            return "", [("delegate", {"tasks": [{"goal": "ENFANT revois les arrondis"}], "isolate": False,
                                      "fork": fork})]
        return "", [("lire_doc", {})]
    return step


def run(fork_flag, fork_arg):
    with tenant_scope(INTERNAL_TENANT):
        r = build_rig({"turns": [], "config": {"delegate_fork": fork_flag}}, CONV / "canary-config.yaml", None, "prefix")
        k = r.kernel
        model = StepModel(step_for(fork_arg))
        k.providers = {Tier.SHIP: model, Tier.DEEP: model}
        d = ReadOnlyDelegate(child_factory(k), None, max_children=1, fork=fork_flag)
        k.tools.internal.sources.extend([Docs(), d])
        d.bind(k.tools.internal)
        k.tools.refresh()
        k.run_turn(TurnInput(thread_id="p", user_text="On travaille sur la facturation.", channel="api", sender="bench"))
        turn = k.run_turn(TurnInput(thread_id="p", user_text="délègue la revue", channel="api", sender="bench"))
    return k, d, model, turn


def is_child(s):
    return "ENFANT" in next(str(m.get("content")) for m in reversed(s.messages) if m.get("role") == "user")


def test_flag_off_schema_and_child_unchanged():
    d = DelegateToolSource(lambda *a: None, None)
    assert d.specs()[0].parameters is dg._SPECS_PARAMS  # même objet qu'avant le chantier
    bg = DelegateToolSource(lambda *a: None, None, background=True).specs()[0].parameters
    assert "fork" not in bg["properties"] and "background" in bg["properties"]
    k, _, model, turn = run(False, True)  # fork=true envoyé quand même : ignoré
    child = [s for s in model.seen if is_child(s)]
    assert child and FORK_NOTE not in json.dumps(child[0].messages, ensure_ascii=False)
    assert child[0].session_key.startswith("p/sa-")
    assert not k._fork_bases  # rien n'est gardé drapeau éteint


def test_flag_on_schema_adds_fork_param_only():
    on = DelegateToolSource(lambda *a: None, None, fork=True).specs()[0]
    off = DelegateToolSource(lambda *a: None, None).specs()[0]
    assert on.description == off.description
    assert set(on.parameters["properties"]) - set(off.parameters["properties"]) == {"fork"}
    assert on.parameters["properties"]["fork"] == dg._FORK_PARAM


def test_fork_child_inherits_exact_prefix_tools_and_affinity():
    k, _, model, turn = run(True, True)
    parent = [s for s in model.seen if not is_child(s)]
    child = [s for s in model.seen if is_child(s)]
    delegating = next(s for s in parent if "lire_doc" in [m.get("name") for m in s.messages if m.get("role") == "tool"]
                      and s.messages[-1].get("role") == "tool" and not is_child(s))
    first = child[0]
    n = len(delegating.messages)
    assert first.messages[:n] == delegating.messages  # le contexte du parent, octet pour octet
    assert first.tools[:len(delegating.tools)] == delegating.tools
    assert render(first.messages, first.tools).startswith(
        render(delegating.messages, delegating.tools)[:-1])  # préfixe rendu (sans le crochet fermant)
    assert first.messages[n]["role"] == "user" and FORK_NOTE + "ENFANT revois les arrondis" in first.messages[n]["content"]
    assert {s.session_key for s in child} == {"p"}  # affinité de cache du fil parent
    out = json.loads(next(m.content for m in turn.messages if m.role == "tool" and m.name == "delegate"))
    assert out[0]["status"] == "completed" and out[0]["summary"] == "résumé de l'enfant"
    assert turn.reply == "terminé"


def test_fork_child_reusable_tokens_measured_against_parent():
    k, _, model, turn = run(True, True)
    child_calls = [c for c in k.ledger._calls if c.thread_id.startswith("p/sa-")]
    assert child_calls[0].reusable_tokens and child_calls[0].reusable_tokens > 0.8 * child_calls[0].prompt_tokens


def test_fork_unavailable_without_base():
    d = DelegateToolSource(lambda *a: None, None, fork=True)
    d.bind(type("R", (), {"sources": []})())

    class NoBase:
        def fork_base(self, thread_id, turn_index):
            return None

    ctx = ToolContext(thread_id="p", turn_index=1, channel="api", kernel=NoBase())
    res = d.call("delegate", {"tasks": [{"goal": "x"}], "fork": True}, ctx)
    assert res.is_error and res.content == FORK_UNAVAILABLE


def _kernel_with_base():
    with tenant_scope(INTERNAL_TENANT):
        r = build_rig({"turns": [], "config": {"delegate_fork": True}}, CONV / "canary-config.yaml", None, "prefix")
    k = r.kernel
    return k


def test_fork_base_isolated_by_tenant_and_turn():
    k = _kernel_with_base()
    t = Turn(thread_id="fil", index=4, input=InputCheck(text="secret d'alice"))
    base = Assembled(messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "secret d'alice"}],
                     tools=[], prefix_sha256="x")
    with tenant_scope("tn_alice"):
        k._keep_fork_base(t, base)
        assert k.fork_base("fil", 4).messages[1]["content"] == "secret d'alice"
        assert k.fork_base("fil", 5) is None  # un autre tour du fil : pas de base
    with tenant_scope("tn_bob"):  # même identifiant de fil, autre locataire : rien
        assert k.fork_base("fil", 4) is None
    with tenant_scope("tn_alice"):  # copie : l'enfant ne modifie pas la base du parent
        k.fork_base("fil", 4).messages[1]["content"] = "modifié"
        assert k.fork_base("fil", 4).messages[1]["content"] == "secret d'alice"


def test_fork_bases_bounded():
    k = _kernel_with_base()
    k.fork_bases_max = 3
    base = Assembled(messages=[{"role": "system", "content": "s"}], tools=[], prefix_sha256="x")
    with tenant_scope(INTERNAL_TENANT):
        for i in range(5):
            k._keep_fork_base(Turn(thread_id=f"f{i}", index=0, input=InputCheck(text="")), base)
        assert len(k._fork_bases) == 3 and k.fork_base("f0", 0) is None and k.fork_base("f4", 0) is not None
