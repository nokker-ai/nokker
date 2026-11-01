"""Routage multi-modèle, 2e passe (01.11.2026) : règles de classe resserrées d'après le banc `bench/route`, type de tour,
palier `nina` à part (ombre puis routage par type, fenêtre de 32 k), seuil d'escalade réglable."""
from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import yaml

from nokker.core.ports import Completion, RouteState
from nokker.core.turn import InputCheck, Message, Tier, Turn, TurnInput, Usage
from nokker.route.classify import TaskClass, classify_rules, turn_kind
from nokker.route.nina import SHADOW_CALL_INDEX, SHADOW_TIER, NinaShadow, fits
from nokker.route.policy import PolicyV1
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from nokker.traces.recorder import MemoryTraceSink

LABELS = Path(__file__).resolve().parents[3] / "bench" / "route" / "labels.yaml"


def state(text, thread="t", index=0, call_index=0, channel="api", **kw):
    turn = Turn(thread_id=thread, index=index, input=InputCheck(text=text))
    turn.channel = channel
    return RouteState(turn=turn, call_index=call_index, **kw)


# ── règles de classe ──

def test_non_programming_uses_of_code_words_stay_conversation():
    for text in ("Le code postal de la boutique c'est 1204 ?", "J'ai reçu un code de vérification par SMS",
                 "Tu as un code promo ?", "Le QR code du billet ne s'affiche pas", "Ma fille change de classe",
                 "Calcule mon net en fonction du taux d'activité", "La partie variable est versée quand ?",
                 "Ouvre rapport.html et résume-le"):
        assert classify_rules(text).task is TaskClass.CONVERSATION, text


def test_code_identifiers_and_code_verbs_are_still_code():
    for text in ("renomme la variable total en montant_total", "la fonction parse_date() renvoie None",
                 "la classe InvoiceRow plante", "corrige le bug dans parser.py", "fix the failing test in utils.go",
                 "le build casse depuis le dernier commit"):
        assert classify_rules(text).task is TaskClass.CODE, text


def test_short_follow_up_that_changes_topic_leaves_code():
    assert classify_rules("et les tests ?", TaskClass.CODE).task is TaskClass.CODE
    assert classify_rules("réponds au mail de Paul en attendant", TaskClass.CODE).task is TaskClass.CONVERSATION
    assert classify_rules("et pour la réunion de jeudi ?", TaskClass.CODE).task is TaskClass.CONVERSATION
    assert classify_rules("et le config.yaml ?", TaskClass.CODE).task is TaskClass.CODE


def test_small_talk_is_trivial_but_go_never_is():
    for text in ("Salut, ça va ?", "Comment ça va aujourd'hui ?", "et toi, ça va ?", "bonne soirée !",
                 "thanks a lot!", "Merci beaucoup, c'est parfait"):
        assert classify_rules(text).task is TaskClass.TRIVIAL, text
    for text in ("ça va, vas-y", "oui ça va", "ok merci, envoie", "comment ça va le déploiement ?"):
        assert classify_rules(text).task is not TaskClass.TRIVIAL, text


def test_labelled_bench_has_no_go_on_the_small_tier_and_few_errors():
    rows = yaml.safe_load(LABELS.read_text())
    wrong = []
    for r in rows:
        prev = TaskClass(r["prev"]) if r.get("prev") else None
        got = classify_rules(r["text"], prev).task
        if r.get("kind") == "approval":
            assert got is not TaskClass.TRIVIAL, r["text"]
        if got.value != r["class"]:
            wrong.append(r["text"])
    assert len(wrong) <= len(rows) // 20, wrong


def test_model_classifier_never_makes_a_request_after_a_thank_you_trivial():
    from nokker.route.classify import ModelClassifier, classify

    class AlwaysA:
        def post(self, body):
            return {"choices": [{"message": {"content": "A"}}]}

    m = ModelClassifier(transport=AlwaysA())
    for text in ("merci, tu peux le supprimer", "merci, envoyez-le", "bonjour, publie le post", "merci tu peux payer"):
        assert classify(text, None, m).task is not TaskClass.TRIVIAL, text


# ── type de tour ──

def test_turn_kinds():
    k = (lambda text, task=TaskClass.CONVERSATION, channel="": turn_kind(text, task, channel))
    assert k("corrige le bug", TaskClass.CODE) == "code"
    assert k("ok vas-y") == "approval"
    assert k("Pourquoi le disque du serveur est plein ?") == "infra_read"
    assert k("Redémarre le service web") == "conversation"  # écriture : pas un diagnostic en lecture
    assert k("Rédige un post LinkedIn pour l'offre") == "content"
    assert k("Démontre que 2+2=4 étape par étape") == "long_reasoning"
    assert k("Ce mail est-il une arnaque ?") == "judgment"
    assert k("Dois-je facturer la TVA à un client français ?") == "escalation"
    assert k("on se voit demain ?", channel="voice") == "voice"


# ── palier nina : politique ──

def test_nina_takes_its_kinds_never_code_and_never_after_a_signal():
    p = PolicyV1(available=lambda: (Tier.SMALL, Tier.SHIP, Tier.DEEP, Tier.NINA),
                 nina_kinds=frozenset({"conversation", "infra_read", "approval"}))
    assert p.choose(state("Qu'est-ce que j'ai demain ?", thread="n1")).tier is Tier.NINA
    assert p.choose(state("Pourquoi le disque est plein ?", thread="n2")).tier is Tier.NINA
    assert p.choose(state("Rédige un post pour l'offre", thread="n3")).tier is Tier.SHIP  # content
    assert p.choose(state("corrige le bug dans app.py", thread="n4")).tier is Tier.DEEP
    assert p.choose(state("merci !", thread="n5")).tier is Tier.SMALL  # le trivial reste au petit palier
    assert p.choose(state("Qu'est-ce que j'ai demain ?", thread="n6", tool_failures=3)).tier is Tier.DEEP
    d = p.choose(state("Qu'est-ce que j'ai demain ?", thread="n7"))
    assert d.reasoning is False and "nina" in d.reason


def test_nina_off_or_absent_changes_nothing():
    ship = PolicyV1(available=lambda: (Tier.SMALL, Tier.SHIP, Tier.DEEP), nina_kinds=frozenset({"conversation"}))
    assert ship.choose(state("Qu'est-ce que j'ai demain ?", thread="o1")).tier is Tier.SHIP
    no_kinds = PolicyV1(available=lambda: (Tier.SHIP, Tier.NINA))
    assert no_kinds.choose(state("Qu'est-ce que j'ai demain ?", thread="o2")).tier is Tier.SHIP


def test_build_policy_reads_nina_kinds_and_tool_failure_threshold():
    from nokker.api.server import build_policy
    from nokker.config import NokkerConfig

    providers = {t: SimpleNamespace(vision=False) for t in (Tier.SHIP, Tier.DEEP, Tier.NINA)}
    cfg = NokkerConfig(route={"policy": "v1", "deep_tool_failures": 5,
                              "nina": {"mode": "route", "kinds": ["conversation", "code", "content"]}})
    p = build_policy(cfg, providers)
    assert p.nina_kinds == frozenset({"conversation"})  # code et rédaction ne vont jamais à Nina
    assert p.deep_tool_failures == 5
    assert p.choose(state("résume", thread="b1", tool_failures=3)).tier is Tier.NINA
    assert p.choose(state("résume", thread="b2", tool_failures=5)).tier is Tier.DEEP
    shadow = build_policy(NokkerConfig(route={"policy": "v1", "nina": {"mode": "shadow"}}), providers)
    assert shadow.nina_kinds == frozenset()


# ── fenêtre de 32 k ──

class Prov:
    def __init__(self, text="ok", model="m", delay=0.0, fail=False, max_context_tokens=0):
        self.text, self.model, self.delay, self.fail, self.seen = text, model, delay, fail, []
        self.max_context_tokens = max_context_tokens
        self.max_tokens_default = 512

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048, **_):
        self.seen.append(json.loads(json.dumps(messages)))
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("HTTP 502")
        return Completion(message=Message(role="assistant", content=self.text), usage=Usage(prompt=10, completion=2),
                          model=self.model, latency_s=0.01)


def test_fits_uses_the_declared_window():
    small = Prov(max_context_tokens=1000)
    assert fits(small, [{"role": "user", "content": "x" * 600}], [], 100)[0]
    ok, need, limit = fits(small, [{"role": "user", "content": "x" * 6000}], [], 100)
    assert not ok and need > limit == 1000
    assert fits(Prov(), [{"role": "user", "content": "x" * 10 ** 6}], [])[0]  # fenêtre inconnue : pas de contrôle


def test_kernel_moves_an_oversized_nina_call_to_ship():
    from tests.unit.traces.test_traces import Ctx, Esc, Gates, Ledger, Mem, Tools
    from nokker.core.loop import Kernel
    from nokker.qa.invariants import DeterministicInvariants
    from nokker.store.pg import MemoryStore

    class NinaPol:
        def choose(self, st):
            from nokker.core.ports import RouteDecision

            return RouteDecision(tier=Tier.NINA, reasoning=False, reason="type conversation → nina")

    inv = DeterministicInvariants()
    nina, ship = Prov("nina", "nina-70b", max_context_tokens=40), Prov("ship", "ship-m")
    k = Kernel(store=MemoryStore(), context=Ctx(), memory=Mem(), invariants=inv, gates=Gates(inv), escalator=Esc(),
               policy=NinaPol(), providers={Tier.SHIP: ship, Tier.NINA: nina}, tools=Tools(), ledger=Ledger())
    with tenant_scope(INTERNAL_TENANT):
        turn = k.run_turn(TurnInput("fil", "un message " * 30))
    assert turn.reply == "ship" and nina.seen == [] and turn.calls[0].tier is Tier.SHIP
    assert any(v.rule == "router:context" and "→ ship" in v.reason for v in turn.verdicts)
    k.providers[Tier.NINA] = Prov("nina", "nina-70b", max_context_tokens=32768)
    with tenant_scope(INTERNAL_TENANT):
        turn = k.run_turn(TurnInput("fil2", "court"))
    assert turn.reply == "nina" and turn.calls[0].tier is Tier.NINA


# ── palier nina : ombre ──

def shadow_kernel(nina, fraction=1.0, sink=None):
    from tests.unit.traces.test_traces import Ctx, Esc, Gates, Ledger, Mem, Pol, Tools
    from nokker.core.loop import Kernel
    from nokker.qa.invariants import DeterministicInvariants
    from nokker.store.pg import MemoryStore

    inv = DeterministicInvariants()
    ship = Prov("réponse réelle", "ship-m")
    sh = NinaShadow(provider=nina, sink=sink or MemoryTraceSink(), fraction=fraction, rng=random.Random(1),
                    seal=inv.redact_context, target="canary")
    k = Kernel(store=MemoryStore(), context=Ctx(), memory=Mem(), invariants=inv, gates=Gates(inv), escalator=Esc(),
               policy=Pol(), providers={Tier.SHIP: ship}, tools=Tools(), ledger=Ledger(), nina_shadow=sh)
    return k, ship, sh


def wait_rows(sink, n, timeout=5.0):
    t0 = time.monotonic()
    while len(sink.rows) < n and time.monotonic() - t0 < timeout:
        time.sleep(0.02)
    return sink.rows


def test_shadow_doubles_the_first_call_of_internal_turns_and_logs_both_answers():
    nina = Prov("réponse de nina", "nina-70b")
    k, ship, sh = shadow_kernel(nina)
    with tenant_scope(INTERNAL_TENANT):
        turn = k.run_turn(TurnInput("fil", "qu'est-ce que j'ai demain ?"))
    assert turn.reply == "réponse réelle"  # le tour réel ne voit rien
    rows = wait_rows(sh.sink, 1)
    assert len(rows) == 1
    r = rows[0]
    assert r["call_index"] == SHADOW_CALL_INDEX and r["meta"]["tier"] == SHADOW_TIER
    assert r["completion"][0]["content"] == "réponse de nina"
    assert r["meta"]["shadow"]["real"]["completion"][0]["content"] == "réponse réelle"
    assert r["meta"]["shadow"]["real"]["tier"] == "ship" and r["meta"]["turn_error"] is None
    assert nina.seen[0] == ship.seen[0]  # mêmes messages que l'appel réel


def test_shadow_never_runs_for_testers_and_respects_the_fraction():
    nina = Prov("x", "nina-70b")
    k, _, sh = shadow_kernel(nina)
    with tenant_scope("tn_aaaabbbbcccc"):
        k.run_turn(TurnInput("tn_aaaabbbbcccc:fil", "bonjour, mon agenda ?"))
    time.sleep(0.2)
    assert sh.sink.rows == [] and nina.seen == []
    k0, _, sh0 = shadow_kernel(Prov(), fraction=0.0)
    with tenant_scope(INTERNAL_TENANT):
        k0.run_turn(TurnInput("fil", "mon agenda ?"))
    assert sh0.stats["sent"] == 0


def test_shadow_error_or_slowness_has_no_effect_on_the_real_turn():
    slow = Prov("lent", "nina-70b", delay=1.0)
    k, _, sh = shadow_kernel(slow)
    t0 = time.monotonic()
    with tenant_scope(INTERNAL_TENANT):
        turn = k.run_turn(TurnInput("fil", "mon agenda ?"))
    assert turn.reply == "réponse réelle" and time.monotonic() - t0 < 0.8
    failing = Prov(fail=True)
    k2, _, sh2 = shadow_kernel(failing)
    with tenant_scope(INTERNAL_TENANT):
        turn2 = k2.run_turn(TurnInput("fil", "mon agenda ?"))
    assert turn2.reply == "réponse réelle"
    rows = wait_rows(sh2.sink, 1)
    assert rows[0]["completion"] == [] and "HTTP 502" in rows[0]["meta"]["shadow"]["error"]


def test_shadow_skips_calls_over_the_window_and_when_busy():
    k, _, sh = shadow_kernel(Prov(max_context_tokens=20))
    with tenant_scope(INTERNAL_TENANT):
        k.run_turn(TurnInput("fil", "un long message " * 20))
    assert sh.stats["skipped_context"] == 1 and sh.stats["sent"] == 0
    gate = threading.Event()

    class Blocking(Prov):
        def complete(self, *a, **kw):
            gate.wait(2)
            return super().complete(*a, **kw)

    kb, _, shb = shadow_kernel(Blocking())
    shb._slots = threading.Semaphore(1)
    with tenant_scope(INTERNAL_TENANT):
        kb.run_turn(TurnInput("f1", "a ?"))
        kb.run_turn(TurnInput("f2", "b ?"))
    gate.set()
    assert shb.stats["sent"] == 1 and shb.stats["skipped_busy"] == 1


def test_build_kernel_keeps_nina_out_of_the_served_tiers_in_shadow_mode(monkeypatch):
    from nokker.api.server import build_kernel
    from nokker.config import NokkerConfig, TierConfig

    monkeypatch.setenv("BENCH_KEY", "k")
    tiers = {"ship": TierConfig(base_url="http://x.invalid/v1", model="s", api_key_env="BENCH_KEY"),
             "nina": TierConfig(base_url="http://y.invalid/v1", model="nina", api_key_env="BENCH_KEY")}
    off = build_kernel(NokkerConfig(tiers=tiers, route={"policy": "v1"}), memory=None)
    assert Tier.NINA not in off.providers and off.nina_shadow is None
    routed = build_kernel(NokkerConfig(tiers=tiers, route={"policy": "v1", "nina": {"mode": "route"}}), memory=None)
    assert routed.providers[Tier.NINA].max_context_tokens == 32768
    sink = MemoryTraceSink()
    shadow_cfg = NokkerConfig(tiers=tiers, route={"policy": "v1", "nina": {"mode": "shadow", "shadow_fraction": 0.1}})
    from nokker.route import nina as nina_mod

    monkeypatch.setattr(nina_mod, "build_shadow", lambda c, p, i=None, sink=None: NinaShadow(p, MemoryTraceSink(), 0.1))
    shadowed = build_kernel(shadow_cfg, memory=None)
    assert Tier.NINA not in shadowed.providers and shadowed.nina_shadow is not None
    assert shadowed.nina_shadow.provider.model == "nina" and sink.rows == []
