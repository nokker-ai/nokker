"""Plan (lot X18, docs/LOKKAL.md §7.18) sur le VRAI noyau et le vrai moteur Crew (monde de tests/unit/agents, faux
modèle) : consignes héritées injectées dans le message système d'une session rattachée et d'un passage d'agent,
avancement calculé depuis les filles, hiérarchie bornée, proposition de découpe (jamais une création silencieuse),
brief du matin planifié rangé en bulle, cloisonnement par locataire."""
from __future__ import annotations

import json

import pytest

from nokker.core.turn import TurnInput
from nokker.incidents.service import Operate, sign
from nokker.incidents.store import MemoryIncidents
from nokker.plan.service import BRIEF_NAME, Plan
from nokker.plan.store import MemoryPlan
from nokker.plan.tools import PlanToolSource
from nokker.plan.types import PlanError
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from tests.unit.agents.conftest import make_world


@pytest.fixture(autouse=True)
def _scope():
    with tenant_scope(INTERNAL_TENANT):
        yield


def plan_world(tmp_path, steps=()):
    w = make_world(tmp_path, steps)
    plan = Plan(store=MemoryPlan(), agents=w.agents, hub=w.bubbles, history=w.kernel.store.history, publish=w.rec,
                clock=w.clock, has_tool=lambda n: n in w.tools.specs, scrub=w.kernel.invariants.scrub_output)
    w.bubbles.plan = plan
    w.kernel.context.plan = plan
    src = PlanToolSource(plan)
    for s in src.specs():
        w.tools.specs[s.name] = s
    inner = w.tools.call

    def call(name, args, ctx):
        if name.startswith("plan_"):
            w.tools.calls.append((name, dict(args)))
            return src.call(name, args, ctx)
        return inner(name, args, ctx)

    w.tools.call = call
    return w, plan


def tree(plan):
    goal = plan.create({"title": "Lancer la bêta", "intent": "100 testeurs actifs d'ici le 6.11",
                        "constraints": ["aucune donnée de testeur hors de Suisse"]}, "alice")
    lot = plan.create({"parent_id": goal["id"], "title": "Onboarding", "decisions": ["connexion par code e-mail"]},
                      "alice")
    t1 = plan.create({"parent_id": lot["id"], "title": "Écran d'accueil", "intent": "trois étapes au plus"}, "alice")
    t2 = plan.create({"parent_id": lot["id"], "title": "Invitations"}, "alice")
    t3 = plan.create({"parent_id": goal["id"], "title": "Page de prix"}, "alice")
    return goal, lot, t1, t2, t3


# ─────────────────────────────── le contexte descend ───────────────────────────────


def test_kinds_default_by_depth_and_children_inherit_the_project(tmp_path):
    w, plan = plan_world(tmp_path)
    goal, lot, t1, _, _ = tree(plan)
    assert (goal["kind"], lot["kind"], t1["kind"]) == ("goal", "milestone", "task")
    assert t1["depth"] == 2 and plan.get_card(t1["id"])["ancestors"][0]["id"] == goal["id"]


def test_context_block_carries_every_ancestor_from_root_to_card(tmp_path):
    w, plan = plan_world(tmp_path)
    goal, lot, t1, t2, _ = tree(plan)
    block = plan.context(t1["id"])["block"]
    order = [block.index(x) for x in ("100 testeurs", "hors de Suisse", "code e-mail", "trois étapes")]
    assert order == sorted(order)
    assert "la carte sur laquelle tu travailles" in block.split("Écran d'accueil")[1].splitlines()[0]
    # une carte sœur n'hérite pas des consignes de sa sœur
    sister = plan.context(t2["id"])["block"]
    assert "trois étapes" not in sister and "code e-mail" in sister


def test_attached_session_gets_the_inherited_block_in_its_system_message(tmp_path):
    w, plan = plan_world(tmp_path, [("text", "Je commence par l'accueil.")])
    goal, lot, t1, _, _ = tree(plan)
    th = w.bubbles.create_thread("Session accueil", "alice")
    plan.attach(t1["id"], th.id, "alice")
    w.kernel.run_turn(TurnInput(thread_id=th.id, user_text="Au travail.", channel="api", sender="alice"))
    system = w.model.seen[-1][0]["content"]
    assert "[Contexte du plan" in system and "hors de Suisse" in system and "connexion par code e-mail" in system
    # un autre fil ne reçoit rien
    other = w.bubbles.create_thread("Autre", "alice")
    w.kernel.run_turn(TurnInput(thread_id=other.id, user_text="Bonjour.", channel="api", sender="alice"))
    assert "[Contexte du plan" not in w.model.seen[-1][0]["content"]


def test_editing_an_ancestor_reaches_the_next_turn(tmp_path):
    w, plan = plan_world(tmp_path, [("text", "a"), ("text", "b")])
    goal, lot, t1, _, _ = tree(plan)
    th = w.bubbles.create_thread("S", "alice")
    plan.attach(t1["id"], th.id, "alice")
    w.kernel.run_turn(TurnInput(thread_id=th.id, user_text="1", channel="api", sender="alice"))
    plan.patch(goal["id"], {"constraints": ["budget 500 € au plus"]}, "alice")
    w.kernel.run_turn(TurnInput(thread_id=th.id, user_text="2", channel="api", sender="alice"))
    assert "budget 500 €" in w.model.seen[-1][0]["content"]


def test_attaching_someone_elses_thread_is_refused(tmp_path):
    w, plan = plan_world(tmp_path)
    _, _, t1, _, _ = tree(plan)
    th = w.bubbles.create_thread("à Bruno", "bruno")
    with pytest.raises(PlanError) as e:
        plan.attach(t1["id"], th.id, "alice")
    assert e.value.status == 403


def test_agent_listed_on_a_card_gets_the_block_in_its_run(tmp_path):
    w, plan = plan_world(tmp_path, [("text", "fait")])
    _, lot, t1, _, _ = tree(plan)
    a = w.agents.create_from_body({"name": "Relecteur", "role": "qa", "instructions": "Relis l'écran."}, "alice")
    plan.patch(t1["id"], {"agents": [a.id]}, "alice")
    w.agents.run_now(a.id, "alice")
    assert "connexion par code e-mail" in w.model.seen[-1][0]["content"]
    with pytest.raises(PlanError):
        plan.patch(t1["id"], {"agents": ["ag_000000000000"]}, "alice")


# ─────────────────────────────── l'avancement remonte ───────────────────────────────


def test_progress_and_status_are_computed_from_the_leaves(tmp_path):
    w, plan = plan_world(tmp_path)
    goal, lot, t1, t2, t3 = tree(plan)
    assert plan.get_card(goal["id"])["status"] == "todo"
    plan.patch(t1["id"], {"status": "done"}, "alice")
    g = plan.get_card(goal["id"])
    assert g["status"] == "in_progress" and g["progress"]["done"] == 1 and g["progress"]["total"] == 3
    assert plan.get_card(lot["id"])["progress"]["ratio"] == 0.5
    plan.patch(t3["id"], {"status": "blocked"}, "alice")
    assert plan.get_card(goal["id"])["status"] == "blocked"
    for t in (t2, t3):
        plan.patch(t["id"], {"status": "done"}, "alice")
    g = plan.get_card(goal["id"])
    assert g["status"] == "done" and g["progress"]["ratio"] == 1.0 and g["computed"] is True
    # l'événement de la feuille remonte à ses ancêtres (leur avancement a changé)
    last = [d["card"] for d in w.rec.of("plan.card.updated")[-2:]]
    assert [c["id"] for c in last] == [t3["id"], goal["id"]] and last[1]["status"] == "done"


def test_a_parent_status_is_never_declared(tmp_path):
    w, plan = plan_world(tmp_path)
    goal, *_ = tree(plan)
    with pytest.raises(PlanError) as e:
        plan.patch(goal["id"], {"status": "done"}, "alice")
    assert e.value.code == "computed"


def test_hierarchy_has_no_cycle_a_depth_limit_and_delete_lifts_children(tmp_path):
    w, plan = plan_world(tmp_path)
    goal, lot, t1, t2, _ = tree(plan)
    with pytest.raises(PlanError):
        plan.patch(goal["id"], {"parent_id": t1["id"]}, "alice")  # sous sa propre descendante
    parent = t1["id"]
    for i in range(3):
        parent = plan.create({"parent_id": parent, "title": f"n{i}"}, "alice")["id"]  # profondeur 5
    with pytest.raises(PlanError):
        plan.create({"parent_id": parent, "title": "trop bas"}, "alice")
    out = plan.delete(lot["id"], "alice")
    assert set(out["lifted"]) == {t1["id"], t2["id"]}
    assert plan.get_card(t1["id"])["parent_id"] == goal["id"]


# ─────────────────────────────── proposition de découpe ───────────────────────────────


def test_model_split_is_a_proposal_until_a_human_accepts_it_in_the_bubble(tmp_path):
    children = [{"title": "Maquette"}, {"title": "Textes", "intent": "FR et EN"}, {"title": "Tests d'usage"}]
    w, plan = plan_world(tmp_path, [("tool", "plan_propose_split", {"card_id": "", "children": children,
                                                                   "reason": "trop gros pour une tâche"}),
                                    ("text", "Je propose trois cartes.")])
    _, _, t1, _, _ = tree(plan)
    w.model.steps[0][2]["card_id"] = t1["id"]
    th = w.bubbles.create_thread("S", "alice")
    before = len(plan.list_cards())
    w.kernel.run_turn(TurnInput(thread_id=th.id, user_text="Découpe l'écran d'accueil.", channel="api",
                                sender="alice"))
    assert len(plan.list_cards()) == before  # rien de créé
    [p] = plan.proposals(t1["id"])
    assert p.state == "proposed" and p.bubble_id
    bubble = w.bubbles.bubbles.get(p.bubble_id)
    assert bubble.kind == "plan_split" and [a["id"] for a in bubble.actions] == ["accept", "refuse"]
    out = w.bubbles.action(p.bubble_id, "alice", "accept")  # le geste de l'humain
    assert len(out["cards"]) == 3 and all(c["created_by"] == "alice" for c in out["cards"])
    assert [c["title"] for c in plan.get_card(t1["id"])["children_cards"]] == ["Maquette", "Textes", "Tests d'usage"]
    assert plan.get_card(t1["id"])["computed"] is True
    with pytest.raises(PlanError) as e:
        plan.decide(p.id, "accept", "alice")
    assert e.value.status == 409


def test_refused_proposal_creates_nothing_and_a_client_cannot_post_a_plan_bubble(tmp_path):
    from nokker.bubbles.types import BubbleError

    w, plan = plan_world(tmp_path)
    _, _, t1, _, _ = tree(plan)
    p = plan.propose_split(t1["id"], [{"title": "x"}], "", "nina")
    n = len(plan.list_cards())
    assert plan.decide(p.id, "refuse", "alice")["proposal"]["state"] == "refused"
    assert len(plan.list_cards()) == n
    th = w.bubbles.create_thread("S", "alice")
    with pytest.raises(BubbleError):
        w.bubbles.create_bubble(th.id, "alice", kind="plan_split", state={"proposal_id": p.id})


def test_deleting_the_card_makes_its_proposal_stale(tmp_path):
    w, plan = plan_world(tmp_path)
    _, _, t1, _, _ = tree(plan)
    p = plan.propose_split(t1["id"], [{"title": "x"}], "", "nina")
    plan.delete(t1["id"], "alice")
    assert plan.store.get_proposal(p.id).state == "stale"


# ─────────────────────────────── brief du matin ───────────────────────────────


def test_morning_brief_is_a_scheduled_agent_whose_reply_lands_in_a_bubble(tmp_path):
    w, plan = plan_world(tmp_path, [("tool", "plan_brief", {}), ("text", "Brief : DiskFull brûle ; accueil en cours.")])
    _, _, t1, _, _ = tree(plan)
    plan.patch(t1["id"], {"status": "in_progress"}, "alice")
    op = Operate(store=MemoryIncidents(), agents=w.agents, clock=w.clock)
    src, key = op.create_source({"name": "prom", "format": "generic"}, "alice")
    raw = json.dumps({"name": "DiskFull", "title": "Disque plein sur host-1"}).encode()
    op.receive(src.id, raw, sign(key, raw, w.clock()))
    plan.operate = lambda: op
    a, created = plan.ensure_brief({}, "alice")
    assert created and a.name == BRIEF_NAME and a.trigger["cron"] == "30 7 * * 1-5"
    assert a.status == "armed" and a.next_run_at is not None and a.tools == ["plan_brief"]
    assert plan.ensure_brief({}, "alice")[1] is False  # un seul brief par personne
    w.clock.t = a.next_run_at + 1
    w.engine.tick()
    [run] = w.runs(a.id)
    assert run.status == "succeeded" and run.trigger == "schedule"
    tool_msgs = [m for m in w.model.seen[-1] if m.get("role") == "tool"]
    assert any("Disque plein sur host-1" in m["content"] and "Écran d'accueil" in m["content"] for m in tool_msgs)
    [brief] = [b for b in w.bubbles.bubbles.list_thread(run.thread_id) if b.kind == "brief"]
    assert brief.state["text"].startswith("Brief : DiskFull") and brief.state["run_id"] == run.id
    assert any(b.kind == "notify" for b in w.bubbles.list_notifications("alice"))


def test_brief_with_a_calendar_connector_adds_the_agenda_tool(tmp_path):
    from nokker.core.ports import ToolSpec
    from nokker.core.turn import Effect

    w, plan = plan_world(tmp_path)
    w.tools.specs["calendar_events"] = ToolSpec("calendar_events", "agenda", {}, Effect.READ)
    a, _ = plan.ensure_brief({"cron": "0 8 * * *"}, "alice")
    assert a.tools == ["plan_brief", "calendar_events"] and "calendar_events" in a.instructions
    assert plan.brief_data()["agenda"].startswith("appelle calendar_events")


def test_brief_without_engine_is_503(tmp_path):
    w, plan = plan_world(tmp_path)
    w.agents.engine = None
    with pytest.raises(PlanError) as e:
        plan.ensure_brief({}, "alice")
    assert e.value.status == 503


# ─────────────────────────────── cloisonnement ───────────────────────────────


def test_cards_of_one_tenant_do_not_exist_in_another(tmp_path):
    w, plan = plan_world(tmp_path)
    goal, *_ = tree(plan)
    with tenant_scope("tn_bbbbbbbbbbbb"):
        assert plan.list_cards() == []
        with pytest.raises(PlanError) as e:
            plan.get_card(goal["id"])
        assert e.value.status == 404
        assert plan.block_for_thread("tn_bbbbbbbbbbbb:th_x") == ""
