"""Agents des membres dans la vue du projet (docs/LOKKAL.md §7.15) : chaque membre voit les agents rattachés au
projet par TOUS les membres (nom, état, déclencheur, dernier passage, propriétaire), jamais leurs secrets, leur mission
ni leurs entrées/sorties ; les actions restent à leur propriétaire, sauf l'owner du projet qui peut désarmer."""
from __future__ import annotations

import pytest

from nokker.agents.hub import AgentsHub
from nokker.agents.memory import InMemoryAgents
from nokker.team.model import TeamError
from nokker.team.service import Team
from nokker.team.store import MemoryTeamStore
from nokker.tenancy.store import MemoryTenancy

PUBLIC_KEYS = {"id", "name", "status", "trigger", "last_run", "owner", "mine", "can_disarm"}


@pytest.fixture
def w():
    tenancy = MemoryTenancy()
    a = {k: tenancy.create_account(f"{k}@example.org") for k in ("owner", "maint", "mem", "view", "out")}
    agents = AgentsHub(InMemoryAgents())
    team = Team(store=MemoryTeamStore(), tenancy=tenancy, agents=agents)
    pid = team.create(a["owner"], {"name": "Plateforme"})["id"]
    for who, role in (("maint", "maintainer"), ("mem", "member"), ("view", "viewer")):
        team.accept(a[who], team.invite(a["owner"], pid, {"email": f"{who}@example.org", "role": role})["id"])
    veille = agents.create_agent(name="veille", role="surveille la file", model="m", created_by=a["mem"].account_id,
                                 project=pid, instructions="lis la boîte de facturation", secrets=["BILLING_TOKEN"])
    perso = agents.create_agent(name="perso", role="r", model="", created_by=a["mem"].account_id)
    tri = agents.create_agent(name="tri", role="r", model="", created_by=a["maint"].account_id, project=pid)
    autre = agents.create_agent(name="ailleurs", role="r", model="", created_by=a["out"].account_id, project=pid)
    v = agents.store.get(veille.id)  # programmé (sans moteur ici : posé à la main dans le magasin)
    v.trigger = {"kind": "schedule", "cron": "0 7 * * *", "tz": "Europe/Zurich"}
    agents.store.save(v)

    class W:
        pass

    x = W()
    x.__dict__.update(team=team, a=a, pid=pid, agents=agents, veille=veille, perso=perso, tri=tri, autre=autre)
    return x


def _status(fn, *args):
    with pytest.raises(TeamError) as e:
        fn(*args)
    return e.value.status, e.value.code


def test_every_member_sees_the_agents_of_all_members_of_the_project(w):
    for who in ("owner", "maint", "mem", "view"):
        got = w.team.project_agents(w.a[who], w.pid)
        assert sorted(x["name"] for x in got) == ["tri", "veille"], who  # ni « perso » ni l'agent d'un non-membre
    assert _status(w.team.project_agents, w.a["out"], w.pid) == (404, "not_found")


def test_others_agents_show_no_secret_no_mission_no_io(w):
    [v] = [x for x in w.team.project_agents(w.a["view"], w.pid) if x["name"] == "veille"]
    assert set(v) == PUBLIC_KEYS
    assert v["owner"] == {"account_id": w.a["mem"].account_id, "email": "mem@example.org"}
    assert v["trigger"] == {"kind": "schedule", "cron": "0 7 * * *", "tz": "Europe/Zurich"}
    assert v["mine"] is False and v["can_disarm"] is False
    flat = repr(v)
    assert "BILLING_TOKEN" not in flat and "facturation" not in flat and "surveille" not in flat


def test_own_agents_keep_their_full_card(w):
    [v] = [x for x in w.team.project_agents(w.a["mem"], w.pid) if x["name"] == "veille"]
    assert v["mine"] is True and v["secrets"] == ["BILLING_TOKEN"] and v["instructions"]


def test_event_trigger_pattern_hidden_for_mail_and_webhooks_but_alerts_are_named(w):
    from nokker.team.service import public_trigger

    assert public_trigger({"kind": "event", "event": "mail:from:boss@example.com"}) == {"kind": "event",
                                                                                     "event": "mail"}
    assert public_trigger({"kind": "event", "event": "webhook:abc"}) == {"kind": "event", "event": "webhook"}
    assert public_trigger({"kind": "event", "event": "alert:Disk*"}) == {"kind": "event", "event": "alert:Disk*"}
    assert public_trigger({"kind": "once", "at": "2026-11-01T08:00:00+01:00"})["at"]


def test_only_the_owner_acts_except_the_project_owner_who_may_disarm(w):
    assert w.agents.store.get(w.veille.id).armed
    for who in ("maint", "view"):
        assert _status(w.team.disarm_agent, w.a[who], w.pid, w.veille.id) == (403, "forbidden")
    assert w.agents.store.get(w.veille.id).armed
    assert next(x for x in w.team.project_agents(w.a["owner"], w.pid) if x["name"] == "veille")["can_disarm"]
    got = w.team.disarm_agent(w.a["owner"], w.pid, w.veille.id)
    assert got["status"] == "paused" and not w.agents.store.get(w.veille.id).armed
    [ev] = w.team.audit(w.a["maint"], w.pid)["events"]
    assert ev["event"] == "agent_disarmed" and ev["detail"] == {
        "agent_id": w.veille.id, "name": "veille", "owner_account": w.a["mem"].account_id}
    # son propriétaire réarme comme d'habitude (l'owner du projet n'a pas d'autre geste) ; manuel ici : pas de moteur
    v = w.agents.store.get(w.veille.id)
    v.trigger = {"kind": "manual"}
    w.agents.store.save(v)
    w.agents.arm(w.veille.id, w.a["mem"].account_id)
    assert w.agents.store.get(w.veille.id).armed


def test_owner_of_the_agent_disarms_his_own_from_the_project_view(w):
    got = w.team.disarm_agent(w.a["maint"], w.pid, w.tri.id)
    assert got["mine"] is True and not w.agents.store.get(w.tri.id).armed


def test_unknown_or_foreign_agent_is_404(w):
    for aid in (w.perso.id, w.autre.id, "ag_000000000000"):
        assert _status(w.team.disarm_agent, w.a["owner"], w.pid, aid) == (404, "not_found")


def test_agents_of_a_member_who_left_disappear_from_the_view(w):
    w.team.remove(w.a["owner"], w.pid, w.a["mem"].account_id)
    assert [x["name"] for x in w.team.project_agents(w.a["owner"], w.pid)] == ["tri"]
