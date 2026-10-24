"""Contrat de la boucle : ordre du tour, portes, escalade, relance Deep — avec un modèle scripté HOSTILE.

Les fakes sont volontairement idiots : ce test vérifie la boucle, pas les piliers.
"""
from __future__ import annotations

import hashlib

from nokker.core.loop import Kernel
from nokker.core.ports import Assembled, Completion, Recall, RouteDecision, ToolSpec
from nokker.core.turn import (
    Decision, Effect, Gate, InputCheck, Message, Tier, ToolCall, ToolResult, TurnInput, Usage, Verdict,
)

PREFIX = "Tu es Atlas."


class ScriptedProvider:
    def __init__(self, script, model="scripted"):
        self.script, self.model, self.seen = list(script), model, []

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        self.seen.append(messages)
        return Completion(message=self.script.pop(0), usage=Usage(prompt=100), model=self.model, latency_s=0.01)


class Ctx:
    def assemble(self, turn, history, recall, loaded, tier):
        msgs = [{"role": "system", "content": PREFIX}, {"role": "user", "content": turn.input.text}]
        msgs += [m.to_openai() for m in turn.messages]
        return Assembled(messages=msgs, tools=[], prefix_sha256=hashlib.sha256(PREFIX.encode()).hexdigest())

    def admit_result(self, r):
        return r


class Mem:
    def recall(self, q, k=5):
        return []

    def propose(self, t, f):
        pass


class Inv:
    def check_input(self, text):
        flags = {"fiscal"} if "TVA" in text else set()
        return InputCheck(text=text.replace("hunter2", "[secret:pw]"), domain_flags=flags)

    def check_action(self, call, spec, turn):
        eff = spec.effect if spec else Effect.WRITE
        d = {Effect.SEND_BULK: Decision.DENY, Effect.DESTRUCTIVE: Decision.HITL}.get(eff, Decision.ALLOW)
        return Verdict(gate=Gate.ACTION, decision=d, layer=1, rule=f"effect:{eff.value}")

    def redact_result(self, result, spec):
        if spec and spec.effect is Effect.SECRET:
            result.content, result.secret_handles = "[secret:pw]", ["pw"]
        return result

    def scrub_output(self, text):
        return text.replace("hunter2", "[secret:pw]")


class GatesFake:
    def __init__(self, inv, redo_once=False):
        self.inv, self.redo_once = inv, redo_once

    def action(self, call, spec, turn):
        return [self.inv.check_action(call, spec, turn)]

    def switch(self, reply, turn, tier):
        if self.redo_once and tier is Tier.SHIP:
            return [Verdict(gate=Gate.SWITCH, decision=Decision.REDO_DEEP, layer=2, rule="doute")]
        if "fiscal" in turn.input.domain_flags and not turn.escalated:
            return [Verdict(gate=Gate.SWITCH, decision=Decision.ESCALATE, layer=1, rule="domaine:fiscal")]
        return [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=1, rule="ok")]


class Esc:
    def escalate(self, turn, reason):
        return f"Je transmets à Alice ({reason})."


class Pol:
    def choose(self, state):
        if state.switch_verdicts:
            return RouteDecision(tier=Tier.DEEP, reasoning=True, reason="porte bascule: doute")
        return RouteDecision(tier=Tier.SHIP, reasoning=False, reason="défaut")


class Tools:
    specs = {"send_bulk_email": ToolSpec("send_bulk_email", "mailing", {}, Effect.SEND_BULK),
             "infra_lvextend": ToolSpec("infra_lvextend", "lvm", {}, Effect.DESTRUCTIVE),
             "calendar_list": ToolSpec("calendar_list", "agenda", {}, Effect.READ),
             "vault_get": ToolSpec("vault_get", "coffre", {}, Effect.SECRET),
             "clarify": ToolSpec("clarify", "pose une question", {}, Effect.READ)}

    def __init__(self):
        self.called = []

    def core(self):
        return []

    def spec(self, n):
        return self.specs.get(n)

    def find(self, q, limit=5):
        return [s for s in self.specs.values() if q in s.name]

    def index(self):
        return [(s.name, s.description) for s in self.specs.values()]

    def call(self, n, a, ctx):
        self.called.append(n)
        if n == "vault_get":
            return ToolResult(call_id="", name=n, content="hunter2")
        if n == "clarify":
            return ToolResult(call_id="", name=n, content=f"Question: {a.get('question', '')}")
        return ToolResult(call_id="", name=n, content="ok")


class StoreFake:
    def __init__(self):
        self.turns = []

    def history(self, t, limit=None):
        return [x for x in self.turns if x.thread_id == t]

    def next_index(self, t):
        return len(self.history(t))

    def append(self, turn):
        self.turns.append(turn)


class LedgerFake:
    def __init__(self):
        self.calls, self.verdicts = [], []

    def record(self, t, i, c):
        self.calls.append(c)

    def record_verdict(self, t, i, v):
        self.verdicts.append(v)


def kernel(script, deep_script=(), redo_once=False):
    inv, tools = Inv(), Tools()
    ship, deep = ScriptedProvider(script), ScriptedProvider(list(deep_script), model="deep")
    k = Kernel(store=StoreFake(), context=Ctx(), memory=Mem(), invariants=inv, gates=GatesFake(inv, redo_once),
               escalator=Esc(), policy=Pol(), providers={Tier.SHIP: ship, Tier.DEEP: deep}, tools=tools,
               ledger=LedgerFake())
    return k, tools, ship, deep


def tc(name, **args):
    return ToolCall(id=f"c-{name}", name=name, arguments=args)


def test_hostile_bulk_send_is_denied_and_never_executed():
    k, tools, *_ = kernel([Message("assistant", tool_calls=[tc("send_bulk_email")]), Message("assistant", "envoyé !")])
    turn = k.run_turn(TurnInput("t1", "envoie la promo à toute la liste"))
    assert tools.called == []
    assert any(v.decision is Decision.DENY for v in turn.verdicts)


def test_destructive_waits_for_human():
    k, tools, *_ = kernel([Message("assistant", tool_calls=[tc("infra_lvextend")]), Message("assistant", "fait")])
    turn = k.run_turn(TurnInput("t1", "le disque est plein"))
    assert tools.called == [] and [p.name for p in turn.pending_approvals] == ["infra_lvextend"]


def test_fiscal_flag_escalates_even_if_model_answers():
    k, *_ = kernel([Message("assistant", "La TVA est de 8,1 %, c'est réglé.")])
    turn = k.run_turn(TurnInput("t1", "quel taux de TVA pour ma facture à l'étranger ?"))
    assert turn.escalated and "Alice" in turn.reply and "8,1" not in turn.reply


def test_secret_never_reaches_reply_or_store():
    k, *_ = kernel([Message("assistant", "ton mot de passe est hunter2")])
    turn = k.run_turn(TurnInput("t1", "rappelle-moi le mot de passe hunter2"))
    assert "hunter2" not in turn.reply and "hunter2" not in turn.input.text


def test_redo_deep_keeps_prefix_and_drops_candidate():
    k, _, ship, deep = kernel([Message("assistant", "brouillon douteux")], [Message("assistant", "réponse sûre")],
                              redo_once=True)
    turn = k.run_turn(TurnInput("t1", "question difficile"))
    assert turn.reply == "réponse sûre"
    assert [c.tier for c in turn.calls] == [Tier.SHIP, Tier.DEEP]
    assert len({c.prefix_sha256 for c in turn.calls}) == 1
    assert all("brouillon douteux" not in str(m) for m in deep.seen[0])


def test_read_tool_runs_and_routes_are_logged():
    k, tools, *_ = kernel([Message("assistant", tool_calls=[tc("calendar_list")]), Message("assistant", "2 rdv")])
    turn = k.run_turn(TurnInput("t1", "mon agenda ?"))
    assert tools.called == ["calendar_list"] and turn.reply == "2 rdv"
    assert all(c.route_reason for c in turn.calls)


def test_secret_tool_result_is_redacted_before_context_and_store():
    k, tools, ship, _ = kernel([Message("assistant", tool_calls=[tc("vault_get")]), Message("assistant", "c'est noté")])
    turn = k.run_turn(TurnInput("t1", "lis le mot de passe grafana", sender="alice"))
    assert "hunter2" not in str(turn.messages) and "hunter2" not in str(ship.seen)
    assert turn.sender == "alice"


def test_escalation_tool_ends_the_turn_with_a_reply():
    script = [Message("assistant", tool_calls=[tc("escalate_to_human", reason="TVA")])] * 8
    k, _, ship, _ = kernel(script)
    turn = k.run_turn(TurnInput("t1", "quel taux de TVA ?"))
    assert turn.escalated and turn.reply and len(turn.calls) == 1


def test_clarify_ends_the_turn_with_its_question_as_reply():
    """Régression P1-S1 (17.10.2026), même principe que l'escalade (commit 2f8d35c) : un humain lit la
    RÉPONSE du tour, jamais l'argument d'un outil. Sans ce comportement, le 2e appel scripté ici (un
    texte de remplissage qui ne redit jamais la question) serait devenu la réponse finale — observé en
    réel contre le Router : « En attente de ta réponse — j'ai posé la question ci-dessus via `clarify`. »,
    sans la question elle-même."""
    script = [Message("assistant", tool_calls=[tc("clarify", question="combien de Go ?")]),
              Message("assistant", "texte de remplissage qui ne redit pas la question")]
    k, tools, ship, _ = kernel(script)
    turn = k.run_turn(TurnInput("t1", "agrandis le disque"))
    assert turn.reply == "Question: combien de Go ?"
    assert len(turn.calls) == 1  # le 2e message scripté n'est jamais consommé : le tour s'arrête avant
    assert not turn.escalated  # clarify n'est PAS une escalade de sûreté (distinction avec ESCALATE_TOOL)
    assert tools.called == ["clarify"]


def test_duplicate_read_call_same_turn_is_not_re_executed():
    """Régression P1-S1 (17.10.2026) : un appel READ identique (même nom, mêmes arguments) DANS LE MÊME
    TOUR renvoie le résultat déjà obtenu, sans ré-exécuter la source — mesuré en réel : `memory_get`/
    `infra_disk_usage` rappelés plusieurs fois une fois le résultat déjà connu, jusqu'à épuiser max_calls
    sur certaines tâches."""
    k, tools, *_ = kernel([
        Message("assistant", tool_calls=[tc("calendar_list"), tc("calendar_list")]),
        Message("assistant", "2 rdv"),
    ])
    turn = k.run_turn(TurnInput("t1", "mon agenda ?"))
    assert tools.called == ["calendar_list"]  # exécuté UNE fois, pas deux, malgré 2 tool_calls
    tool_msgs = [m for m in turn.messages if m.role == "tool"]
    assert len(tool_msgs) == 2  # le modèle reçoit bien UNE réponse par tool_call qu'il a émis
    assert tool_msgs[0].content == "ok"
    assert "déjà appelé" in tool_msgs[1].content and "ok" in tool_msgs[1].content
    read_verdicts = [v for v in turn.verdicts if v.rule == "effect:read"]
    assert len(read_verdicts) == 1  # pas de 2e verdict pour un appel qui n'a pas été réévalué


def test_recall_hit_logs_a_memory_verdict_layer0():
    """Signal de mesure (P1-S1, 17.10.2026) : un juge de banc doit pouvoir reconnaître qu'un rappel
    automatique a eu lieu, sans que le modèle ait appelé memory_search lui-même."""
    class MemWithRecall(Mem):
        def recall(self, q, k=5):
            return [Recall(note="hote21-gpu-experiment-safety", text="vérifier clinfo -l", score=0.81)]

    inv, tools = Inv(), Tools()
    k = Kernel(store=StoreFake(), context=Ctx(), memory=MemWithRecall(), invariants=inv,
              gates=GatesFake(inv), escalator=Esc(), policy=Pol(),
              providers={Tier.SHIP: ScriptedProvider([Message("assistant", "ok")]),
                         Tier.DEEP: ScriptedProvider([])},
              tools=tools, ledger=LedgerFake())
    turn = k.run_turn(TurnInput("t1", "charge GPU sur hote21 ?"))
    memory_verdicts = [v for v in turn.verdicts if v.rule == "memory:recall_used"]
    assert len(memory_verdicts) == 1
    assert memory_verdicts[0].layer == 0 and memory_verdicts[0].decision is Decision.ALLOW


def test_no_recall_logs_no_memory_verdict():
    k, *_ = kernel([Message("assistant", "ok")])  # Mem() par défaut -> recall() == []
    turn = k.run_turn(TurnInput("t1", "bonjour"))
    assert not [v for v in turn.verdicts if v.rule == "memory:recall_used"]


def _recall_kernel(memory, context=None, budget=2.5):
    inv = Inv()
    return Kernel(store=StoreFake(), context=context or Ctx(), memory=memory, invariants=inv,
                  gates=GatesFake(inv), escalator=Esc(), policy=Pol(),
                  providers={Tier.SHIP: ScriptedProvider([Message("assistant", "ok")]),
                             Tier.DEEP: ScriptedProvider([])},
                  tools=Tools(), ledger=LedgerFake(), recall_budget_s=budget)


def test_recall_runs_in_parallel_with_context_preparation():
    """Le rappel part en tête de tour : sa latence se recouvre avec celle de la fenêtre de contexte au lieu
    de s'y ajouter (20.10.2026, 1,4-1,5 s mesurées sur le canari)."""
    import time as _t

    class SlowMem(Mem):
        def recall(self, q, k=5):
            _t.sleep(0.3)
            return [Recall(note="n", text="t", score=0.9)]

    class SlowCtx(Ctx):
        def history_window(self, thread_id):
            _t.sleep(0.3)
            return []

    t0 = _t.monotonic()
    turn = _recall_kernel(SlowMem(), SlowCtx()).run_turn(TurnInput("t1", "question"))
    assert _t.monotonic() - t0 < 0.55
    assert [v for v in turn.verdicts if v.rule == "memory:recall_used"]


def test_late_recall_is_dropped_and_turn_goes_on():
    import time as _t

    class StuckMem(Mem):
        def recall(self, q, k=5):
            _t.sleep(1.0)
            return [Recall(note="n", text="t", score=0.9)]

    t0 = _t.monotonic()
    turn = _recall_kernel(StuckMem(), budget=0.1).run_turn(TurnInput("t1", "question"))
    assert _t.monotonic() - t0 < 0.6
    assert turn.reply == "ok"
    assert [v for v in turn.verdicts if v.rule == "memory:recall_late"]
    assert not [v for v in turn.verdicts if v.rule == "memory:recall_used"]


def test_failing_recall_never_breaks_the_turn():
    class BrokenMem(Mem):
        def recall(self, q, k=5):
            raise RuntimeError("serveur mémoire tombé")

    turn = _recall_kernel(BrokenMem()).run_turn(TurnInput("t1", "question"))
    assert turn.reply == "ok"


def test_duplicate_destructive_call_same_turn_is_re_evaluated_every_time():
    """Un doublon n'est JAMAIS tu pour un effet qui n'est pas READ — ici DESTRUCTIVE, toujours réévalué,
    jamais rendu silencieusement « déjà fait » (ce serait une façon d'esquiver la porte d'action)."""
    k, tools, *_ = kernel([
        Message("assistant", tool_calls=[tc("infra_lvextend"), tc("infra_lvextend")]),
        Message("assistant", "fait"),
    ])
    turn = k.run_turn(TurnInput("t1", "le disque est plein"))
    assert tools.called == []  # DESTRUCTIVE sans GO -> jamais exécuté, les deux fois
    destructive_verdicts = [v for v in turn.verdicts if v.rule == "effect:destructive"]
    assert len(destructive_verdicts) == 2  # réévalué à CHAQUE appel, pas de court-circuit


def test_memory_calls_are_budgeted_per_turn():
    calls = [Message("assistant", tool_calls=[ToolCall(id=f"m{i}", name="memory_search", arguments={"query": str(i)})])
             for i in range(6)] + [Message("assistant", "voilà")]
    k, tools, *_ = kernel(calls)
    k.tools.specs["memory_search"] = ToolSpec("memory_search", "mémoire", {}, Effect.READ)
    turn = k.run_turn(TurnInput("t1", "qui es-tu ?"))
    assert tools.called.count("memory_search") == 3
    assert turn.reply == "voilà"


# ───────────────────────────────────────────────────────────────────────────────────────────────
# P1-S1 (diag latence, 17.10.2026) : un palier (Router) qui échoue malgré les réessais internes du
# provider (amont Ship sans repli, voir providers/sokkan.py) ne doit JAMAIS laisser le tour sans
# réponse — régression trouvée en mesurant ce chantier (l'exception remontait jusqu'au `except`
# générique de run_turn, qui fixait `turn.error` mais jamais `turn.reply`, donc un tour "vide").
# ───────────────────────────────────────────────────────────────────────────────────────────────

class FailingProvider:
    """Simule un `providers.sokkan.SokkanProvider` dont TOUS les essais internes ont échoué (budget
    total dépassé ou erreur réseau persistante) — `complete()` lève, comme le ferait le vrai provider."""

    def __init__(self, exc_message="HTTP 504 de https://router.sokkan.ch/v1 après 2 tentative(s)"):
        self.exc_message, self.model = exc_message, "scripted"

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        raise RuntimeError(self.exc_message)


def test_unavailable_tier_never_leaves_a_blank_turn():
    inv, tools = Inv(), Tools()
    k = Kernel(store=StoreFake(), context=Ctx(), memory=Mem(), invariants=inv, gates=GatesFake(inv),
               escalator=Esc(), policy=Pol(),
               providers={Tier.SHIP: FailingProvider(), Tier.DEEP: ScriptedProvider([])},
               tools=tools, ledger=LedgerFake())
    turn = k.run_turn(TurnInput("t1", "bonjour"))
    assert turn.reply and turn.reply.strip()  # jamais une réponse vide
    assert turn.escalated is True
    unavailable = [v for v in turn.verdicts if v.rule == "router:unavailable"]
    assert len(unavailable) == 1
    assert unavailable[0].layer == 0 and unavailable[0].decision is Decision.ESCALATE
    assert turn.error is None  # dégradé proprement, pas une exception non rattrapée


def test_retried_but_successful_call_logs_visibility_verdict_without_blocking():
    """Un appel qui a eu besoin de plus d'un essai (amont lent, pas en panne) ne bloque RIEN — mais se
    voit au ledger (`router:retried`, ALLOW) pour qu'un opérateur voie un amont qui se dégrade."""
    class RetriedProvider(ScriptedProvider):
        def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
            comp = super().complete(messages, tools, reasoning=reasoning, session_key=session_key,
                                    max_tokens=max_tokens)
            comp.attempts = 2
            return comp

    inv, tools = Inv(), Tools()
    k = Kernel(store=StoreFake(), context=Ctx(), memory=Mem(), invariants=inv, gates=GatesFake(inv),
               escalator=Esc(), policy=Pol(),
               providers={Tier.SHIP: RetriedProvider([Message("assistant", "ok")]), Tier.DEEP: ScriptedProvider([])},
               tools=tools, ledger=LedgerFake())
    turn = k.run_turn(TurnInput("t1", "bonjour"))
    assert turn.reply == "ok" and not turn.escalated
    retried = [v for v in turn.verdicts if v.rule == "router:retried"]
    assert len(retried) == 1
    assert retried[0].layer == 0 and retried[0].decision is Decision.ALLOW
    assert "2" in retried[0].reason


def _loaded_per_call(k):
    seen = []
    orig = k.context.assemble

    def spy(turn, history, recall, loaded, tier):
        seen.append([s.name for s in loaded])
        return orig(turn, history, recall, loaded, tier)

    k.context.assemble = spy
    return seen


def test_all_loading_exposes_every_tool_schema_without_a_cap():
    k, tools, *_ = kernel([Message("assistant", "ok")])
    k.tool_loading = "all"
    seen = _loaded_per_call(k)
    k.run_turn(TurnInput("t1", "bonjour"))
    assert seen and set(seen[0]) == set(Tools.specs)


def test_relevance_loading_loads_nothing_for_small_talk_and_the_family_the_message_asks_for():
    k, tools, *_ = kernel([Message("assistant", "ok"), Message("assistant", "ok")])
    seen = _loaded_per_call(k)
    k.run_turn(TurnInput("t1", "bonjour, ça va ?"))
    k.run_turn(TurnInput("t2", "qu'est-ce que j'ai dans mon agenda demain ?"))
    assert seen[0] == [] and seen[1] == ["calendar_list"]


def test_tool_used_in_a_recent_turn_is_reloaded_for_a_follow_up():
    k, tools, *_ = kernel([Message("assistant", "", tool_calls=[tc("vault_get")]), Message("assistant", "ok"),
                           Message("assistant", "ok")])
    seen = _loaded_per_call(k)
    k.run_turn(TurnInput("t1", "bonjour"))
    k.run_turn(TurnInput("t1", "et l'autre ?"))
    assert "vault_get" in seen[-1]


def test_tool_called_without_its_schema_runs_and_is_loaded_for_the_rest_of_the_turn():
    k, tools, *_ = kernel([Message("assistant", "", tool_calls=[tc("calendar_list")]), Message("assistant", "ok")])
    seen = _loaded_per_call(k)
    k.run_turn(TurnInput("t1", "bonjour"))
    assert tools.called == ["calendar_list"]
    assert seen[0] == [] and seen[1] == ["calendar_list"]


def test_reusable_tokens_measures_the_identical_head_between_calls_of_a_thread():
    """Chantier cache (scope v1 §3.1) : le noyau mesure le préfixe qu'un cache POURRAIT servir, quel que soit
    l'amont. 1er appel du fil : inconnu ; appel suivant du même tour (après un outil) : presque tout est repris."""
    from nokker.core.loop import reusable_tokens
    k, *_ = kernel([Message("assistant", tool_calls=[tc("calendar_list")]), Message("assistant", "ok")])
    turn = k.run_turn(TurnInput("t1", "agenda ?"))
    first, second = turn.calls
    assert first.reusable_tokens is None
    assert second.reusable_tokens is not None and second.reusable_tokens > 0
    # Le prompt précédent est repris EN ENTIER en tête (rien ne change avant la fin : date, outils, système).
    from nokker.core.loop import prompt_text
    prev, cur = (prompt_text(type("A", (), {"messages": m, "tools": []})) for m in k.providers[Tier.SHIP].seen)
    assert cur.startswith(prev)
    assert reusable_tokens("abcdef", "abcxyz", 60) == 30
    assert reusable_tokens(None, "abc", 10) is None


def test_max_calls_ends_with_a_wrap_up_answer_kept_in_history():
    """20.10.2026 : la bonne page trouvée au 8e appel, puis « boucle d'outils non terminée » escaladée — et le tour
    suivant répondait à la question restée orpheline. Désormais : un dernier appel sans outils conclut."""
    loop_call = Message("assistant", tool_calls=[tc("calendar_list")])
    k, tools, ship, _ = kernel([loop_call] * 8 + [Message("assistant", "Voici ce que j'ai trouvé.")])
    turn = k.run_turn(TurnInput("t1", "cherche"))
    assert turn.reply == "Voici ce que j'ai trouvé." and not turn.escalated
    assert turn.error is None  # la conclusion a réussi : le tour n'est pas en échec (visible par un verdict, pas une erreur)
    assert [v for v in turn.verdicts if v.rule == "loop:max_calls"]
    assert turn.calls[-1].route_reason.startswith("conclusion") and turn.calls[-1].reusable_tokens is not None
    last = ship.seen[-1]
    assert last[-1]["role"] == "user" and "Limite d'appels" in last[-1]["content"]
    assert turn.messages[-1] == Message("assistant", "Voici ce que j'ai trouvé.")
    assert all("Limite d'appels" not in (m.content or "") for m in turn.messages)


def test_max_calls_wrap_up_failure_still_escalates_and_closes_the_turn():
    loop_call = Message("assistant", tool_calls=[tc("calendar_list")])
    k, *_ = kernel([loop_call] * 8)  # rien pour la conclusion : le provider scripté lève
    turn = k.run_turn(TurnInput("t1", "cherche"))
    assert turn.escalated and turn.reply
    assert turn.messages[-1].role == "assistant" and turn.messages[-1].content == turn.reply


def test_max_calls_wrap_up_refused_by_the_gate_escalates_with_the_gate_reason():
    """Revue 20.10 : un refus de la porte bascule sur la conclusion escaladait avec le motif générique « boucle d'outils
    non terminée » ; le motif de la porte doit arriver à l'humain, comme dans la boucle."""
    loop_call = Message("assistant", tool_calls=[tc("calendar_list")])
    k, *_ = kernel([loop_call] * 8 + [Message("assistant", "La TVA due est de 77 CHF.")])
    turn = k.run_turn(TurnInput("t1", "quel montant de TVA sur 1000 CHF ?"))  # drapeau fiscal → GatesFake escalade
    assert turn.escalated and "domaine:fiscal" in turn.reply
    assert "boucle d'outils" not in turn.reply and turn.error is None


def test_max_calls_wrap_up_doubt_is_redone_on_deep():
    """Revue 20.10 : un REDO_DEEP sur la conclusion était traité comme un ALLOW et le palier douté rejoué."""
    loop_call = Message("assistant", tool_calls=[tc("calendar_list")])
    k, tools, ship, deep = kernel([loop_call] * 8 + [Message("assistant", "conclusion Ship douteuse")],
                                  deep_script=[Message("assistant", "conclusion Deep")], redo_once=True)
    turn = k.run_turn(TurnInput("t1", "cherche"))
    assert turn.reply == "conclusion Deep" and not turn.escalated and turn.error is None
    assert [c.tier for c in turn.calls][-2:] == [Tier.SHIP, Tier.DEEP]


def test_recall_text_is_scrubbed_before_entering_the_turn_head():
    """Revue 20.10 : le rappel entre dans Turn.head, persisté et rejoué — une valeur du coffre dans une note ne doit
    jamais y entrer (invariant 5 de NEVER-CLEAR)."""
    class LeakyMem(Mem):
        def recall(self, q, k=5):
            return [Recall(note="hote2-ssh-access", text="mot de passe hunter2 pour hote2", score=0.9)]

    seen = []

    class SpyCtx(Ctx):  # le faux moteur ne construit pas Turn.head : on regarde ce que le noyau lui passe
        def assemble(self, turn, history, recall, loaded, tier):
            seen.append(list(recall))
            return super().assemble(turn, history, recall, loaded, tier)

    _recall_kernel(LeakyMem(), SpyCtx()).run_turn(TurnInput("t1", "comment j'accède à hote2 ?"))
    assert seen and seen[0][0].text == "mot de passe [secret:pw] pour hote2"
