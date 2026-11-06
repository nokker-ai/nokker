"""`delegate_router` (harnais du 70B, éteint par défaut) : la voix (profondeur 0) confie et attend, sans palier ; le noyau
lance UN enfant « routeur » (son propre modèle) qui choisit ship, deep ou les deux par SON delegate (tier, readonly),
attend, et rend UN compte rendu. Profondeur voix → routeur → ship/deep ; au-delà refusé. Drapeau éteint : inchangé.
Vraie `DelegateToolSource` ; noyaux factices dont le `run_turn` joue le modèle (le routeur appelle delegate)."""
from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from nokker.core.ports import RouteState, ToolContext, ToolSpec
from nokker.core.turn import Effect, InputCheck, Tier, ToolResult, Turn
from nokker.route.policy import PolicyV0
from nokker.tools import delegate as dg
from nokker.tools.delegate import (CONV_DESCRIPTION, ROUTER_PROMPT, ROUTER_VOICE_DESCRIPTION, DelegateToolSource)
from nokker.tools.registry import Registry
from nokker.tools.scope import thread_scope

ROUTER_MODEL = object()  # le fournisseur du modèle du routeur (sentinelle)


@dataclasses.dataclass
class FakeKernel:
    policy: Any
    providers: dict
    registry: Any = None
    plan: tuple = ()  # routeur : appels delegate à faire, ex. (("ship",), ("deep",))
    card: bool = False  # enfant : pose une carte (mail_move) au lieu de finir
    extra: dict = dataclasses.field(default_factory=dict)  # routeur : arguments ajoutés à chaque appel delegate
    seen: dict = dataclasses.field(default_factory=dict)
    limits: Any = None

    def cancel(self, thread_id):
        return None

    def run_turn(self, inp, stream=None):
        self.seen["runs"] = self.seen.get("runs", 0) + 1
        tier = self.policy.choose(RouteState(turn=Turn(thread_id=inp.thread_id, index=0, input=InputCheck(text="x")),
                                             call_index=0)).tier
        self.seen.update(text=inp.user_text, tier=tier, model=self.providers.get(Tier.SHIP))
        if self.providers.get(Tier.SHIP) is ROUTER_MODEL:  # le routeur : il fait faire, puis UN compte rendu
            reports = []
            with thread_scope(inp.thread_id):  # comme Kernel.run_turn
                specs = {s.name: s for s in self.registry._all()}
                self.seen["tools"] = set(specs)
                self.seen["delegate_props"] = set(specs["delegate"].parameters["properties"])
                for step in self.plan:
                    args = {"tasks": [{"goal": f"part {t}"} for t in step], "isolate": False}
                    if len(set(step)) == 1 and step[0]:
                        args["tier"] = step[0]
                    args.update(self.extra)
                    r = self.registry.call("delegate", args, ToolContext(thread_id=inp.thread_id, turn_index=0,
                                                                          channel="sa", sender="alice", kernel=self))
                    reports.append(r.content)
            return Turn(thread_id=inp.thread_id, index=0, input=InputCheck(text=""),
                        reply="compte rendu unique : " + " | ".join(reports))
        if self.card:  # l'enfant appelle mail_move : SA carte, posée par les portes de l'outil réel (jamais *_send :
            from nokker.core.turn import ToolCall  # DENIED_FOR_CHILD, voir test_no_send_tool_for_router_children)

            return Turn(thread_id=inp.thread_id, index=0, input=InputCheck(text=""), reply="rangement prêt",
                        pending_approvals=[ToolCall(id="m1", name="mail_move",
                                                    arguments={"message_id": "m-1", "destination": "Archives"})])
        return Turn(thread_id=inp.thread_id, index=0, input=InputCheck(text=""), reply=f"fait en {tier.value}")


class Files:
    core = False

    def specs(self):
        return [ToolSpec(name="read_file", description="lire", parameters={}, effect=Effect.READ),
                ToolSpec(name="write_file", description="écrire", parameters={}, effect=Effect.WRITE)]

    def call(self, name, args, ctx):
        return ToolResult(call_id="", name=name, content="ok")


@pytest.fixture
def router_on():
    dg.ROUTER["provider"] = ROUTER_MODEL
    yield
    dg.ROUTER["provider"] = None


def setup(plan=(("ship",),), card=False, request="", extra=None, limits=None):
    parent = FakeKernel(policy=PolicyV0(), providers={Tier.SHIP: "ship-model", Tier.DEEP: "deep-model"}, card=card,
                        extra=dict(extra or {}), limits=limits)
    kids: list[FakeKernel] = []

    def factory(registry, thread_id, observer):
        k = dataclasses.replace(parent, registry=registry, plan=plan, seen={},
                                card=card and "/sa-" in thread_id and thread_id.count("/sa-") == 2)
        kids.append(k)
        return k

    src = DelegateToolSource(factory, None, tier=True)
    src.bind(Registry(sources=[Files(), src]))
    turn = Turn(thread_id="t1", index=0, input=InputCheck(text=request)) if request else None
    ctx = ToolContext(thread_id="t1", turn_index=0, channel="api", sender="alice", kernel=parent, turn=turn)
    return src, kids, ctx


def _voice_spec(src):
    with thread_scope("t1"):
        return next(s for s in src.specs() if s.name == "delegate")


def test_voice_schema_has_no_tier_fork_readonly_nor_background(router_on):
    src, *_ = setup()
    spec = _voice_spec(src)
    assert spec.description == ROUTER_VOICE_DESCRIPTION
    assert set(spec.parameters["properties"]) == {"tasks"}
    assert set(spec.parameters["properties"]["tasks"]["items"]["properties"]) == {"goal", "context", "output_schema"}
    assert [s.name for s in src.specs()] == ["delegate"]  # ni status ni cancel pour la voix


@pytest.mark.parametrize("plan,tiers", [((("ship",),), ["ship"]), ((("deep",),), ["deep"]),
                                       ((("ship",), ("deep",)), ["ship", "deep"])])
def test_the_router_chooses_ship_deep_or_both_and_returns_one_report(router_on, plan, tiers):
    src, kids, ctx = setup(plan)
    r = src.call("delegate", {"tasks": [{"goal": "trouve la panne", "context": "logs"}], "tier": "deep",
                              "fork": True}, ctx)  # tier/fork de la voix ignorés
    out = json.loads(r.content)
    assert not r.is_error and len(out) == 1  # UN compte rendu
    router = kids[0]
    assert router.seen["model"] is ROUTER_MODEL and router.seen["tier"] is Tier.SHIP  # son modèle, palier propre
    assert router.seen["text"].startswith(ROUTER_PROMPT) and "trouve la panne" in router.seen["text"]
    assert router.seen["tools"] == {"delegate"}  # il confie, rien d'autre (04.11 : avec la lecture, il répondait seul)
    assert router.seen["delegate_props"] == {"tasks", "tier", "reason"}  # conversation : readonly tranché par le noyau
    workers = kids[1:]
    assert [w.seen["tier"].value for w in workers] == tiers  # le palier choisi par le routeur
    assert all(w.seen["model"] == "ship-model" for w in workers)  # pas le modèle du routeur
    assert out[0]["summary"].startswith("compte rendu unique")
    assert [t["level"] for t in out[0]["routed"]] == tiers  # traces : palier et durée par enfant
    roles = {"ship": "exécution technique", "deep": "analyse approfondie"}
    assert [t["role"] for t in out[0]["routed"]] == [roles[t] for t in tiers]  # QUI a répondu, pour la voix
    assert all("duration_s" in t for t in out[0]["routed"])
    # la voix ne reçoit AUCUN nom de modèle (ni du routeur ni de ses enfants) : il reste aux traces (journal)
    assert "model" not in out[0] and all("model" not in t for t in out[0]["routed"])
    assert not src._routers and not src._routed  # rien ne reste


def test_workers_do_not_delegate_further_depth_three_refused(router_on):
    src, kids, ctx = setup()
    src.call("delegate", {"tasks": [{"goal": "g"}]}, ctx)
    worker = kids[1]
    assert "delegate" not in {s.name for s in worker.registry._all()}
    r = src.call("delegate", {"tasks": [{"goal": "g"}], "isolate": False},
                 ToolContext(thread_id="t1/sa-aaaa/sa-bbbb", turn_index=0, channel="sa", sender="alice"))
    assert r.is_error and "profondeur max" in r.content


def test_flag_off_is_unchanged():
    assert dg.ROUTER["provider"] is None
    src, kids, ctx = setup()
    with thread_scope("t1"):
        spec = next(s for s in src.specs() if s.name == "delegate")
    assert spec.description == CONV_DESCRIPTION and "tier" in spec.parameters["properties"]
    r = src.call("delegate", {"tasks": [{"goal": "g"}], "tier": "deep", "isolate": False}, ctx)
    out = json.loads(r.content)
    assert len(kids) == 1 and kids[0].seen["tier"] is Tier.DEEP and "routed" not in out[0] and "model" not in out[0]


def test_session_voice_sees_the_router_schema(router_on):
    """Le prototype des schémas de session est calculé à l'IMPORT : importé routeur allumé (comme serve), il garde le
    schéma d'avant (delegate, status, cancel) ; seule la voix voit le schéma du routeur."""
    import importlib

    import nokker.code_sessions.subagents as sub_mod

    sub_mod = importlib.reload(sub_mod)  # import pendant que le routeur est allumé
    SessionSubagents = sub_mod.SessionSubagents

    class _Cfg:
        max_subagents = 3

    class _Sessions:
        cfg = _Cfg()

    src = SessionSubagents(_Sessions())
    with thread_scope("tn_x:s-0123456789abcdef"):
        assert [(s.name, s.description) for s in src.specs()] == [("delegate", ROUTER_VOICE_DESCRIPTION)]
    with thread_scope("tn_x:s-0123456789abcdef/sa-a"):
        assert {"delegate", "delegate_status", "delegate_cancel"} <= {s.name for s in src.specs()}



def test_router_texts_are_frozen_word_for_word():
    """Textes vus par un modèle, validés en relecture (03.11) : toute modification repasse par lui."""
    assert ROUTER_VOICE_DESCRIPTION == (
        "Confie un travail à l'équipe technique et attends son compte rendu : décris le but et le contexte utile ; le "
        "noyau choisit qui travaille et comment. Avant l'appel, dis en une phrase à la personne ce que tu confies et "
        "que tu attends la réponse ; ne conclus rien avant le compte rendu. Tu restes la voix : transmets-le "
        "fidèlement, sans rien ajouter, en disant qui a répondu.")
    # v2 (04.11, correctif du banc d'orchestration B) : NOUVEAUX textes, À FAIRE VALIDER PAR relecture avant la prod.
    assert ROUTER_PROMPT == (
        "Tu es le routeur : tu ne réponds pas toi-même à la demande ci-dessous, tu la confies avec delegate, en un seul "
        "appel. Pour chaque appel, choisis tier : ship pour relever, chercher, lister, trier ou comparer des faits, ou "
        "pour exécuter une action précise ; deep dès qu'il faut juger, recommander, diagnostiquer une cause, repérer ce "
        "qui manque ou concevoir une organisation, même si la demande commence par une recherche. Lis le message de la "
        "personne : c'est lui qui dit ce qu'elle veut, la demande de la voix n'en est qu'un résumé. Écris chaque but "
        "complet, avec ce que la personne a demandé de faire ou de juger. L'équipe n'envoie aucun message : si la "
        "demande en exige un, fais préparer le texte et dis dans le compte rendu qu'il reste à l'envoyer. Attends leurs "
        "comptes rendus, puis rends UN seul compte rendu, fidèle et complet : chiffres, noms et sources tels qu'ils ont "
        "été rapportés, ce qui n'a pas été trouvé ou pas fait, et ce qui reste à décider ; sans rien inventer ni "
        "ajouter. Tu ne parles pas à la personne : s'il manque une information, dis-le dans le compte rendu.")
    assert (dg.ROUTER_REQUEST_HEADER, dg.ROUTER_TASKS_HEADER) == ("Message de la personne :", "Demande de la voix :")
    assert dg._ROUTER_TIER_PARAM["description"] == (
        "ship : relever, chercher, lister, trier, comparer des faits, exécuter une action précise ; deep : juger, "
        "recommander, diagnostiquer, repérer ce qui manque, concevoir")
    assert dg._ROUTER_REASON_PARAM["description"] == "en une phrase courte : pourquoi ce palier"
    assert dg.ROUTER_ROLES == {"ship": "exécution technique", "deep": "analyse approfondie"}



def _gate(spec, args, thread):
    """Décision des VRAIES portes (GatesV0 + invariants de la config par défaut) pour l'appel delegate."""
    from nokker.api.server import build_invariants
    from nokker.config import NokkerConfig
    from nokker.core.turn import ToolCall
    from nokker.qa.gates import GatesV0

    inv = build_invariants(NokkerConfig())
    turn = Turn(thread_id=thread, index=0, input=InputCheck(text="trouve la panne"), channel="web", sender="alice")
    with thread_scope(thread):
        return [v.decision.value for v in GatesV0(invariants=inv).action(ToolCall(id="c", name="delegate",
                                                                                   arguments=args), spec, turn)]


def test_voice_and_router_delegate_calls_pose_no_card_in_conversation(router_on):
    """L'appel de la VOIX et celui du ROUTEUR vers ses enfants : WRITE, sans carte (la porte est sur l'outil réel)."""
    src, kids, ctx = setup()
    with thread_scope("t1"):
        voice = next(s for s in src.specs() if s.name == "delegate")
    from nokker.core.ports import call_effect

    with thread_scope("t1"):
        assert call_effect(voice, {"tasks": [{"goal": "g"}]}) is Effect.WRITE
    assert "hitl" not in _gate(voice, {"tasks": [{"goal": "g"}]}, "t1")
    src._routers.add("t1/sa-r1")  # un routeur en cours
    with thread_scope("t1/sa-r1"):
        rspec = next(s for s in src.specs() if s.name == "delegate")
        assert call_effect(rspec, {"tasks": [{"goal": "g"}], "tier": "ship"}) is Effect.WRITE
        # conversation : un readonly envoyé par le routeur est ignoré (le noyau le tranche), l'appel reste WRITE
        assert call_effect(rspec, {"tasks": [{"goal": "g"}], "readonly": True}) is Effect.WRITE
    assert "hitl" not in _gate(rspec, {"tasks": [{"goal": "g"}], "tier": "ship"}, "t1/sa-r1")


def test_negative_a_child_card_on_the_real_tool_reaches_the_voice(router_on):
    """Un enfant non readonly (action demandée) qui appelle mail_move pose SA carte ; la voix le voit (interrupted,
    approval_pending)."""
    src, kids, ctx = setup(card=True, request="Range le mail de x dans Archives.")
    out = json.loads(src.call("delegate", {"tasks": [{"goal": "ranger le mail de x"}]}, ctx).content)
    assert kids[1].registry is not None and not src._signals
    assert out[0]["status"] == "interrupted" and out[0]["exit_reason"] == "approval_pending"
    assert out[0]["routed"][0]["exit_reason"] == "approval_pending"


def test_flag_off_conversation_delegate_without_readonly_stays_destructive():
    assert dg.ROUTER["provider"] is None
    src, *_ = setup()
    from nokker.core.ports import call_effect

    with thread_scope("t1"):
        spec = next(s for s in src.specs() if s.name == "delegate")
        assert call_effect(spec, {"tasks": [{"goal": "g"}]}) is Effect.DESTRUCTIVE
    assert "hitl" in _gate(spec, {"tasks": [{"goal": "g"}]}, "t1")



@pytest.mark.parametrize("decision,reply", [("approved", "mail rangé"), ("rejected", "refusé par alice : rien déplacé")])
def test_a_child_resumed_after_its_card_notes_the_voice_never_the_router(router_on, decision, reply):
    """Carte d'un enfant de profondeur 2 décidée après la fin du tour : sa reprise est notée (gabarit EXISTANT) dans le
    fil de la VOIX ; le routeur, dont le tour est fini, n'est jamais relancé. Aucun nom de modèle dans la note."""
    src, kids, ctx = setup(card=True, request="Range le mail de x dans Archives.")
    notes = []
    src.note = lambda thread, line: notes.append((thread, line))
    src.call("delegate", {"tasks": [{"goal": "ranger le mail de x"}]}, ctx)
    router = kids[0]
    runs = router.seen["runs"]
    [worker] = list(src._parked.values())
    assert worker.voice_thread == "t1" and worker.parent_thread_id.count("/sa-") == 1  # parent = le routeur
    turn = Turn(thread_id=worker.thread_id, index=1, input=InputCheck(text=""), reply=reply)
    src._resumed(worker, turn)  # tour de suite de la carte (exécuteur), approuvée ou refusée
    assert [t for t, _ in notes] == ["t1"]  # la voix, jamais le fil du routeur
    line = notes[0][1]
    assert line.startswith(f"[sous-agent {worker.id} — exécution technique] repris après la carte : ") and reply in line
    assert "ship-model" not in line and "deep-model" not in line
    assert router.seen["runs"] == runs  # le routeur n'est jamais relancé
    assert worker.thread_id not in src._parked



def test_router_resumed_note_is_frozen_and_the_old_template_is_unchanged_elsewhere():
    """Gabarit validé en relecture (03.11), mot pour mot ; pour un enfant SANS routeur, le gabarit d'avant."""
    assert dg.ROUTER_RESUMED_NOTE == "[sous-agent {id} — {role}] repris après la carte : {status}, {exit_reason} — {summary}"
    src, kids, ctx = setup()
    notes = []
    src.note = lambda thread, line: notes.append((thread, line))
    ch = dg._Child(index=0, id="abcd1234", thread_id="t1/sa-abcd1234", task={"goal": "g"}, schema=None,
                   parent_thread_id="t1", tier="deep")  # enfant direct (pas de routeur)
    ch.parked = True
    src._resumed(ch, Turn(thread_id=ch.thread_id, index=1, input=InputCheck(text=""), reply="fait"))
    assert notes == [("t1", "[sous-agent abcd1234] repris après la carte : completed, completed — fait")]



# ── correctif du 04.11 : règles du noyau (router_rules) avant le modèle du routeur ──

def _spy(src):
    """Les `_Child` réellement lancés (routeur compris), dans l'ordre."""
    seen, run0 = [], src._run_child

    def run(ch, c):
        seen.append(ch)
        return run0(ch, c)

    src._run_child = run
    return seen


def _run(request, plan=(("ship",),), extra=None, goal="fais le travail", limits=None, card=False):
    """(source, enfants ship/deep lancés (`_Child`), noyaux factices, résultat rendu à la voix)."""
    src, kids, ctx = setup(plan, card=card, request=request, extra=extra, limits=limits)
    seen = _spy(src)
    out = json.loads(src.call("delegate", {"tasks": [{"goal": goal}]}, ctx).content)
    return src, [c for c in seen if not c.router], kids, out


def test_the_router_sees_the_persons_message_before_the_voice_tasks(router_on):
    src, _, kids, _ = _run("Range les promotions de la semaine dans le dossier Promotions.", goal="lister les promotions")
    text = kids[0].seen["text"]
    assert text.startswith(ROUTER_PROMPT)
    assert text.index("Message de la personne :\nRange les promotions") < text.index("Demande de la voix :\n[")


def test_no_person_message_for_a_card_continuation(router_on):
    src, kids, ctx = setup()
    ctx = dataclasses.replace(ctx, turn=Turn(thread_id="t1", index=0,
                                             input=InputCheck(text="[Approbation] …", origin="approval")))
    src.call("delegate", {"tasks": [{"goal": "g"}]}, ctx)
    assert "Message de la personne" not in kids[0].seen["text"]


@pytest.mark.parametrize("router_readonly", [True, False, None])
def test_readonly_comes_from_the_request_not_from_the_router(router_on, router_readonly):
    """Banc B du 04.11 : le routeur OMETTAIT readonly (défaut faux) sur des lectures, et le posait sur une action
    réécrite en lecture par la voix. En conversation, le noyau tranche d'après le message et les tâches."""
    extra = {} if router_readonly is None else {"readonly": router_readonly}
    _, ws, _, out = _run("Compare les offres fibre d'AlpNet et de Lacfibre.", extra=extra)
    assert [w.readonly for w in ws] == [True] and "lecture seule (aucune action" in ws[0].rule
    _, ws, _, out = _run("Range les promotions dans le dossier Promotions.", extra=extra, goal="lister les promotions")
    assert [w.readonly for w in ws] == [False] and "action : range" in ws[0].rule
    assert all(set(t) & {"readonly", "rule", "model"} == set() for t in out[0]["routed"])  # traces seulement


def test_deep_is_a_floor_when_the_request_asks_for_judgment(router_on):
    _, ws, kids, _ = _run("J'hésite entre Helvia et Securalp : laquelle me convient ?", plan=(("ship",),))
    assert [k.seen["tier"] for k in kids[1:]] == [Tier.DEEP] and ws[0].rule.startswith("plancher deep")
    _, ws, kids, _ = _run("Relève les horaires des piscines.", plan=(("deep",),))  # monter en deep reste permis
    assert kids[1].seen["tier"] is Tier.DEEP and ws[0].rule.startswith("palier du routeur")


def test_missing_tier_is_ship_and_refused_deep_floor_falls_back_to_ship(router_on):
    _, ws, kids, _ = _run("Relève les horaires des piscines.", plan=(("",),))
    assert kids[1].seen["tier"] is Tier.SHIP and ws[0].rule.startswith("palier absent")

    class NoDeep:
        def deep_available(self, sender):
            return False

    _, ws, kids, out = _run("Pourquoi ma facture a doublé ?", plan=(("ship",),), limits=NoDeep())
    assert kids[1].seen["tier"] is Tier.SHIP and ws[0].rule.startswith("deep indisponible")


def test_router_output_schema_and_role_are_dropped(router_on):
    """Banc B : schémas posés par le routeur → enfants `failed / output_schema_invalid` ; le routeur n'en pose plus."""
    src, kids, ctx = setup((("ship",),))
    seen = _spy(src)
    call0 = src.call

    def router_adds_schema(name, args, c):
        if c.thread_id in src._routers:
            args = {**args, "tasks": [{**t, "output_schema": {"type": "object"}, "role": "reviewer"}
                                      for t in args["tasks"]]}
        return call0(name, args, c)

    src.call = router_adds_schema
    src.bind(Registry(sources=[Files(), src]))
    src.call("delegate", {"tasks": [{"goal": "relève les prix"}]}, ctx)
    [worker] = [c for c in seen if not c.router]
    assert worker.schema is None and "role" not in worker.task


def test_router_texts_mention_no_readonly_in_conversation_but_keep_it_in_session(router_on):
    src, *_ = setup()
    src._routers.add("t1/sa-r1")
    with thread_scope("t1/sa-r1"):
        spec = next(s for s in src.specs() if s.name == "delegate")
    assert spec.parameters["required"] == ["tasks", "tier"]
    assert set(spec.parameters["properties"]) == {"tasks", "tier", "reason"}
    assert set(spec.parameters["properties"]["tasks"]["items"]["properties"]) == {"goal", "context"}
    session = DelegateToolSource(lambda *a: None, object(), tier=True)
    assert set(session._router_spec().parameters["properties"]) == {"tasks", "tier", "reason", "readonly", "isolate"}


def test_no_send_tool_for_router_children(router_on):
    """Décision du 04.11 (`*_send`) : DENIED_FOR_CHILD s'applique aux enfants du routeur, lecture seule ou non. Un
    enfant ne pose donc jamais de carte d'envoi : l'envoi reste à la voix (sa carte forte)."""

    class Mail:
        core = False

        def specs(self):
            return [ToolSpec(name=n, description=n, parameters={}, effect=e) for n, e in (
                ("mail_search", Effect.READ), ("mail_move", Effect.WRITE), ("mail_send", Effect.SEND_EXTERNAL),
                ("send_message", Effect.SEND_EXTERNAL))]

        def call(self, name, args, ctx):
            return ToolResult(call_id="", name=name, content="ok")

    src, kids, ctx = setup(request="Range le mail de x et réponds-lui.")
    seen = _spy(src)
    src.bind(Registry(sources=[Files(), Mail(), src]))
    src.call("delegate", {"tasks": [{"goal": "ranger et répondre"}]}, ctx)
    worker = kids[1]
    assert [c.readonly for c in seen if not c.router] == [False]  # action demandée : pas en lecture seule
    names = {s.name for s in worker.registry._all()}
    assert {"mail_search", "mail_move"} <= names and not names & {"mail_send", "send_message"}
    assert worker.registry.call("mail_send", {}, ToolContext(thread_id="x", turn_index=0, channel="sa")).is_error


@pytest.mark.parametrize("role", ["advisor", "tier-1", "Technique", "worker"])
def test_a_role_invented_by_the_voice_is_ignored_not_refused(router_on, role):
    """Banc B P3 (04.11) : `role` n'est pas au schéma de la voix ; un rôle inventé faisait échouer l'appel entier."""
    src, kids, ctx = setup()
    r = src.call("delegate", {"tasks": [{"goal": "lis le devis", "context": "mails", "role": role}]}, ctx)
    out = json.loads(r.content)
    assert not r.is_error and "error" not in out[0], out
    assert kids and out[0]["summary"].startswith("compte rendu unique")
