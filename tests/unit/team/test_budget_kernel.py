"""Budget de projet sur le VRAI noyau et le vrai moteur Crew (monde de tests/unit/agents, faux modèle) : un tour d'un
fil rattaché au projet est compté au ledger avec le projet ; au-delà du plafond, refusé AVANT l'appel modèle ; un
passage d'agent du projet est écarté sans ouvrir de fil ; l'avertissement de 80 % suit la réponse une fois."""
from __future__ import annotations

import pytest

from nokker.core.turn import TurnInput
from nokker.plan.service import Plan
from nokker.plan.store import MemoryPlan
from nokker.team.budget import ProjectBudget, thread_project_resolver
from nokker.team.service import Team
from nokker.team.store import MemoryTeamStore
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from nokker.tenancy.store import MemoryTenancy
from tests.unit.agents.conftest import make_world

TURN_EUR = 0.00096  # un appel du faux modèle : 1000 jetons d'entrée + 100 de sortie au catalogue EUR


@pytest.fixture(autouse=True)
def _scope():
    with tenant_scope(INTERNAL_TENANT):
        yield


@pytest.fixture
def tw(tmp_path):
    w = make_world(tmp_path, [("text", f"réponse {i}") for i in range(10)])
    tenancy = MemoryTenancy()
    owner, mem = tenancy.create_account("owner@example.org"), tenancy.create_account("mem@example.org")
    team = Team(store=MemoryTeamStore(), tenancy=tenancy, agents=w.agents)
    pid = team.create(owner, {"name": "Plateforme"})["id"]
    team.accept(mem, team.invite(owner, pid, {"email": "mem@example.org", "role": "member"})["id"])
    plan = Plan(store=MemoryPlan(), agents=w.agents, hub=w.bubbles, check_project=lambda s, p: p)
    budget = ProjectBudget(team=team, ledger=w.kernel.ledger, prices=w.kernel.ledger.prices,
                           resolve=thread_project_resolver(agents=w.agents, plan=plan))
    team.budget = budget
    w.kernel.project_budget = budget
    card = plan.create({"title": "API", "project": pid}, mem.account_id)
    w.bubbles.threads.create("session", mem.account_id, thread_id="th_session")
    plan.attach(card["id"], "th_session", mem.account_id)  # « Travailler dessus » : la session sur la carte du projet
    w.__dict__.update(team=team, budget=budget, pid=pid, owner=owner, mem=mem, plan=plan)
    return w


def prespend(w, eur: float) -> None:
    """Dépense déjà faite pour le projet aujourd'hui (une autre session du membre)."""
    from nokker.ledger.attribution import SpendTag, spend_tag
    from tests.unit.team.test_budget import call

    with spend_tag(SpendTag(w.pid, w.owner.tenant_id, w.mem.account_id)):
        w.kernel.ledger.record("th_avant", 0, call(eur * 100))


def turn(w, text="salut", thread="th_session"):
    return w.kernel.run_turn(TurnInput(thread_id=thread, user_text=text, channel="web", sender=w.mem.account_id))


def test_turns_of_an_attached_session_are_counted_then_refused_before_the_model(tw):
    tw.team.put_budget(tw.owner, tw.pid, {"daily": 1.0})
    prespend(tw, 1.0 - 1.5 * TURN_EUR)
    first = turn(tw)
    assert first.reply.startswith("réponse 0")
    rows = tw.kernel.ledger.fetch_calls(thread_id="th_session")
    assert [(r.project_id, r.account) for r in rows] == [(tw.pid, tw.mem.account_id)]
    second = turn(tw)  # encore sous le plafond au départ : passe, et finit même au-delà
    assert second.reply.startswith("réponse 1")
    calls = tw.model.calls
    third = turn(tw)
    assert tw.model.calls == calls  # aucun appel modèle
    assert "Budget du projet « Plateforme » atteint" in third.reply
    assert [v.rule for v in third.verdicts] == ["project_budget:daily"]
    assert tw.kernel.store.history("th_session")[-1].reply == third.reply  # le tour refusé reste dans le fil
    view = tw.team.budget_view(tw.owner, tw.pid)
    assert view["today"]["state"] == "over" and view["today"]["spent"] == pytest.approx(1.0 + 0.5 * TURN_EUR)


def test_a_thread_outside_the_project_is_neither_counted_nor_refused(tw):
    tw.team.put_budget(tw.owner, tw.pid, {"daily": 0})
    tw.bubbles.threads.create("libre", tw.mem.account_id, thread_id="th_libre")
    t = turn(tw, thread="th_libre")
    assert t.reply.startswith("réponse 0")
    assert [r.project_id for r in tw.kernel.ledger.fetch_calls(thread_id="th_libre")] == [None]


def test_eighty_percent_notice_follows_the_reply_of_the_turn_that_crosses_it(tw):
    tw.team.put_budget(tw.owner, tw.pid, {"daily": 1.0})
    prespend(tw, 0.8 - TURN_EUR / 2)  # ce tour fait passer 80 %
    t = turn(tw)
    assert "a passé 80 % de son budget du jour" in t.reply
    assert t.reply.startswith("réponse 0")


def test_agent_of_the_project_is_skipped_without_a_thread_when_over_budget(tw):
    a = tw.agents.create_agent(name="veille", role="r", model="", created_by=tw.mem.account_id, project=tw.pid)
    tw.team.put_budget(tw.owner, tw.pid, {"monthly": 1.0})
    prespend(tw, 1.0 - 1.5 * TURN_EUR)
    run = tw.agents.run_now(a.id, tw.mem.account_id)
    run = tw.run(run.id)
    assert run.status == "succeeded" and run.thread_id
    rows = tw.kernel.ledger.fetch_calls(thread_id=run.thread_id)
    assert rows and {r.project_id for r in rows} == {tw.pid}  # le passage dépense pour le projet
    turn(tw)  # la session du membre fait passer le mois au-delà
    calls = tw.model.calls
    run2 = tw.run(tw.agents.run_now(a.id, tw.mem.account_id).id)
    assert run2.status == "skipped" and run2.thread_id is None and tw.model.calls == calls
    assert "Budget du projet « Plateforme » atteint pour ce mois" in run2.summary
