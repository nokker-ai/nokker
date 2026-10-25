"""Équipe (lot X17) : droits du service, sur le magasin en mémoire. Le même service tourne sur Postgres
(tests/integration/test_team_rls.py), où la RLS refait le contrôle d'accès en base."""
from __future__ import annotations

import pytest

from nokker.agents.hub import AgentsHub
from nokker.agents.memory import InMemoryAgents
from nokker.team.model import TeamError, clean_rules
from nokker.team.service import Team
from nokker.team.store import MemoryTeamStore
from nokker.tenancy.store import MemoryTenancy


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def w():
    tenancy = MemoryTenancy()
    accs = {k: tenancy.create_account(f"{k}@example.org") for k in ("owner", "maint", "mem", "view", "out")}
    clock = Clock()
    agents = AgentsHub(InMemoryAgents())
    team = Team(store=MemoryTeamStore(), tenancy=tenancy, agents=agents, mirror_allowed=lambda acc: True,
                scrub=lambda s: s.replace("sk-SECRET123", "[caviardé]"), clock=clock)
    p = team.create(accs["owner"], {"name": "Plateforme", "repos": ["git@github.com:acme/api.git"]})
    for who, role in (("maint", "maintainer"), ("mem", "member"), ("view", "viewer")):
        inv = team.invite(accs["owner"], p["id"], {"email": f"{who}@example.org", "role": role})
        team.accept(accs[who], inv["id"])

    class W:
        pass

    x = W()
    x.__dict__.update(team=team, a=accs, pid=p["id"], clock=clock, agents=agents, tenancy=tenancy)
    return x


def _status(fn, *args):
    with pytest.raises(TeamError) as e:
        fn(*args)
    return e.value.status, e.value.code


def test_owner_creates_and_members_see_with_their_role(w):
    for who, role in (("owner", "owner"), ("maint", "maintainer"), ("mem", "member"), ("view", "viewer")):
        got = w.team.get(w.a[who], w.pid)
        assert got["role"] == role and got["name"] == "Plateforme"
        assert [p["id"] for p in w.team.list_projects(w.a[who])["projects"]] == [w.pid]
    repo = w.team.get(w.a["mem"], w.pid)["repos"][0]
    assert repo["key"] == "github.com/acme/api" and repo["memory_project"].startswith("p-")
    roles = {m["email"]: m["role"] for m in w.team.members(w.a["view"], w.pid)["members"]}
    assert roles == {"owner@example.org": "owner", "maint@example.org": "maintainer",
                     "mem@example.org": "member", "view@example.org": "viewer"}


def test_project_lives_in_owner_tenant(w):
    p = w.team.store.project(w.a["owner"], w.pid)
    assert p.tenant_id == w.a["owner"].tenant_id != w.a["mem"].tenant_id


def test_non_member_gets_404_everywhere_same_as_unknown(w):
    out, pid = w.a["out"], w.pid
    unknown = "pj_000000000000"
    calls = [
        (w.team.get, (out, pid)), (w.team.patch, (out, pid, {"name": "x"})), (w.team.delete, (out, pid)),
        (w.team.members, (out, pid)), (w.team.set_role, (out, pid, w.a["mem"].account_id, {"role": "viewer"})),
        (w.team.remove, (out, pid, w.a["mem"].account_id)), (w.team.invites, (out, pid)),
        (w.team.invite, (out, pid, {"email": "z@example.org"})), (w.team.skills, (out, pid)),
        (w.team.put_skill, (out, pid, "lint", {"source": "official/dev/lint"})), (w.team.rules, (out, pid)),
        (w.team.put_rules, (out, pid, {"deny": ["terminal(rm *)"]})), (w.team.secrets, (out, pid)),
        (w.team.put_secret, (out, pid, "DB_URL", {})), (w.team.notes, (out, pid, None)),
        (w.team.push_notes, (out, pid, "mc_x", [])), (w.team.project_agents, (out, pid)),
        (w.team.check_agent_project, (out, pid)), (w.team.quarantined, (out, pid)),
    ]
    for fn, args in calls:
        assert _status(fn, *args) == (404, "not_found"), fn.__name__
        assert _status(fn, *(args[0], unknown, *args[2:])) == (404, "not_found"), fn.__name__
    assert _status(w.team.get, out, "../etc") == (404, "not_found")
    assert w.team.list_projects(out)["projects"] == []
    assert w.team.machine_projects(out)["projects"] == []


def test_viewer_writes_nothing(w):
    v, pid = w.a["view"], w.pid
    writes = [
        (w.team.patch, (v, pid, {"name": "x"})), (w.team.delete, (v, pid)),
        (w.team.set_role, (v, pid, w.a["mem"].account_id, {"role": "viewer"})),
        (w.team.remove, (v, pid, w.a["mem"].account_id)), (w.team.invite, (v, pid, {"email": "z@example.org"})),
        (w.team.put_skill, (v, pid, "lint", {"source": "official/dev/lint"})), (w.team.delete_skill, (v, pid, "x")),
        (w.team.put_rules, (v, pid, {"deny": []})), (w.team.put_secret, (v, pid, "DB_URL", {})),
        (w.team.delete_secret, (v, pid, "DB_URL")), (w.team.check_agent_project, (v, pid)),
    ]
    before = w.team.get(w.a["owner"], pid)
    for fn, args in writes:
        assert _status(fn, *args)[0] == 403, fn.__name__
    w.team.patch(w.a["owner"], pid, {"mirror": True})
    assert _status(w.team.push_notes, v, pid, "mc_v", [_note("fact-a")]) == (403, "forbidden")
    after = w.team.get(w.a["owner"], pid)
    assert {**before, "mirror": True, "updated_at": None} == {**after, "updated_at": None}
    # mais il lit tout
    for fn in (w.team.get, w.team.members, w.team.skills, w.team.rules, w.team.secrets):
        fn(v, pid)
    w.team.notes(v, pid, None)


def test_role_ladder(w):
    pid = w.pid
    # member : pas le catalogue ni les règles
    assert _status(w.team.put_skill, w.a["mem"], pid, "lint", {"source": "official/dev/lint"})[0] == 403
    assert _status(w.team.put_rules, w.a["mem"], pid, {"deny": []})[0] == 403
    assert w.team.check_agent_project(w.a["mem"], pid) == pid
    # maintainer : catalogue, règles, secrets, dépôts ; pas les membres, ni le miroir, ni la suppression
    w.team.put_skill(w.a["maint"], pid, "lint", {"source": "github:acme/skills/lint", "description": "lint"})
    w.team.put_rules(w.a["maint"], pid, {"mode": "default", "deny": ["terminal(git push --force*)"]})
    w.team.patch(w.a["maint"], pid, {"repos": ["https://github.com/acme/web"], "budget": {"monthly": 50}})
    assert _status(w.team.patch, w.a["maint"], pid, {"mirror": True})[0] == 403
    assert _status(w.team.invite, w.a["maint"], pid, {"email": "z@example.org"})[0] == 403
    assert _status(w.team.set_role, w.a["maint"], pid, w.a["mem"].account_id, {"role": "viewer"})[0] == 403
    assert _status(w.team.delete, w.a["maint"], pid)[0] == 403
    # owner : rôles, retrait ; le propriétaire ne change pas de rôle et ne part pas
    w.team.set_role(w.a["owner"], pid, w.a["mem"].account_id, {"role": "viewer"})
    assert w.team.get(w.a["mem"], pid)["role"] == "viewer"
    assert _status(w.team.set_role, w.a["owner"], pid, w.a["owner"].account_id, {"role": "member"})[0] == 409
    assert _status(w.team.set_role, w.a["owner"], pid, w.a["mem"].account_id, {"role": "owner"})[0] == 400
    assert _status(w.team.remove, w.a["owner"], pid, w.a["owner"].account_id)[0] == 409
    w.team.remove(w.a["owner"], pid, w.a["mem"].account_id)
    assert _status(w.team.get, w.a["mem"], pid) == (404, "not_found")
    # un membre se retire lui-même
    w.team.remove(w.a["view"], pid, w.a["view"].account_id)
    assert _status(w.team.get, w.a["view"], pid)[0] == 404
    # suppression : plus personne ne le voit
    w.team.delete(w.a["owner"], pid)
    assert _status(w.team.get, w.a["maint"], pid)[0] == 404
    assert _status(w.team.get, w.a["owner"], pid)[0] == 404


def test_invitations(w):
    pid, o = w.pid, w.a["owner"]
    out = w.a["out"]
    assert _status(w.team.invite, o, pid, {"email": "mem@example.org"}) == (409, "already_member")
    assert _status(w.team.invite, o, pid, {"email": "owner@example.org"}) == (409, "already_member")
    assert _status(w.team.invite, o, pid, {"email": "out@example.org", "role": "owner"}) == (400, "bad_role")
    inv = w.team.invite(o, pid, {"email": "OUT@example.org", "role": "viewer"})
    assert "invitee_account" not in inv  # la réponse ne dit pas si l'adresse a un compte
    # une adresse sans compte reçoit la même réponse
    ghost = w.team.invite(o, pid, {"email": "nobody@example.org"})
    assert set(ghost) == set(inv)
    assert _status(w.team.invite, o, pid, {"email": "out@example.org"}) == (409, "already_invited")
    mine = w.team.my_invites(out)["invitations"]
    assert [i["project"] for i in mine] == [{"id": pid, "name": "Plateforme"}]
    # un autre compte ne peut ni voir ni accepter l'invitation d'out
    assert w.team.my_invites(w.a["mem"])["invitations"] == []
    assert _status(w.team.accept, w.a["mem"], inv["id"])[0] in (404, 409)
    # révoquée : plus acceptable
    w.team.revoke_invite(o, pid, inv["id"])
    assert _status(w.team.accept, out, inv["id"]) == (404, "not_found")
    inv2 = w.team.invite(o, pid, {"email": "out@example.org", "role": "viewer"})
    w.clock.t += 15 * 86400  # expirée
    assert _status(w.team.accept, out, inv2["id"]) == (404, "not_found")
    assert w.team.my_invites(out)["invitations"] == []
    inv3 = w.team.invite(o, pid, {"email": "out@example.org", "role": "viewer"})
    w.team.decline(out, inv3["id"])
    assert _status(w.team.accept, out, inv3["id"])[0] == 404
    inv4 = w.team.invite(o, pid, {"email": "out@example.org", "role": "viewer"})
    assert w.team.accept(out, inv4["id"])["role"] == "viewer"
    # un compte créé APRÈS l'invitation la trouve par son e-mail
    late = w.team.invite(o, pid, {"email": "late@example.org"})
    late_acc = w.tenancy.create_account("late@example.org")
    assert [i["id"] for i in w.team.my_invites(late_acc)["invitations"]] == [late["id"]]
    assert w.team.accept(late_acc, late["id"])["role"] == "member"


def test_secrets_are_names_only(w):
    pid, m = w.pid, w.a["maint"]
    for body in ({"value": "hunter2"}, {"secret": "x"}, {"token": "x", "description": "d"}, {"password": "x"}):
        assert _status(w.team.put_secret, m, pid, "DB_URL", body) == (400, "secret_value_refused")
    # même un non-membre reçoit le refus de la valeur, sans que la valeur soit jamais rangée
    assert _status(w.team.put_secret, w.a["out"], pid, "DB_URL", {"value": "x"}) == (400, "secret_value_refused")
    assert _status(w.team.put_secret, m, pid, "bad name", {}) == (400, "bad_name")
    w.team.put_secret(m, pid, "DB_URL", {"description": "base de préprod"})
    w.team.put_secret(m, pid, "SENTRY_DSN", {})
    assert w.team.secrets(w.a["view"], pid)["secrets"] == [
        {"name": "DB_URL", "description": "base de préprod"}, {"name": "SENTRY_DSN", "description": ""}]
    assert _status(w.team.patch, m, pid, {"secret_names": [{"name": "X", "value": "y"}]})[0] == 400
    stored = w.team.store.project(w.a["owner"], pid)
    assert "hunter2" not in repr(stored) and all(set(s) == {"name", "description"} for s in stored.secret_names)
    machine = w.team.machine_projects(w.a["mem"])["projects"][0]
    assert machine["secret_names"] == [{"name": "DB_URL", "description": "base de préprod"},
                                       {"name": "SENTRY_DSN", "description": ""}]
    w.team.delete_secret(m, pid, "SENTRY_DSN")
    assert [s["name"] for s in w.team.secrets(m, pid)["secrets"]] == ["DB_URL"]


def test_rules_can_only_restrict():
    assert clean_rules({"mode": "plan", "ask": ["terminal(npm *)"], "deny": ["write_file(.env)"]}) == {
        "mode": "plan", "ask": ["terminal(npm *)"], "deny": ["write_file(.env)"]}
    for widen in ({"allow": ["terminal(*)"]}, {"allow": []}, {"mode": "auto"}, {"thread_allow": ["x"]}):
        with pytest.raises(TeamError) as e:
            clean_rules(widen)
        assert e.value.code == "rules_widen"
    for bad in ({"deny": ["(((("]}, {"mode": "yolo"}, {"deny": "terminal"}, {"other": 1}, []):
        with pytest.raises(TeamError):
            clean_rules(bad)


def test_rules_route_refuses_widening(w):
    assert _status(w.team.put_rules, w.a["maint"], w.pid, {"allow": ["terminal(*)"]}) == (400, "rules_widen")
    assert w.team.rules(w.a["mem"], w.pid)["rules"] == {"ask": [], "deny": []}


def test_skills_catalog(w):
    pid, m = w.pid, w.a["maint"]
    for src in ("official/dev/lint", "github:acme/skills", "https://example.org/x/SKILL.md"):
        w.team.put_skill(m, pid, "s-" + str(len(src)), {"source": src})
    for bad in ("file:///etc/passwd", "github:../x", "https://u:p@example.org/x", "official/../x", ""):
        assert _status(w.team.put_skill, m, pid, "bad", {"source": bad})[0] == 400, bad
    assert _status(w.team.put_skill, m, pid, "Bad Name", {"source": "official/a/b"}) == (400, "bad_name")
    w.team.put_skill(m, pid, "deploy", {"source": "official/ops/deploy", "requires_local": True})
    names = [s["name"] for s in w.team.skills(w.a["view"], pid)["skills"]]
    assert "deploy" in names and len(names) == 4
    assert next(s for s in w.team.skills(w.a["view"], pid)["skills"] if s["name"] == "deploy")["requires_local"]
    w.team.delete_skill(m, pid, "deploy")
    assert _status(w.team.delete_skill, m, pid, "deploy")[0] == 404


def _note(name, text="La CI tourne sur Forgejo.", t=1_799_999_000.0, **kw):
    return {"name": name, "kind": "fact", "title": "CI", "text": text, "updated_at": t, **kw}


def test_notes_need_mirror_and_conflicts_go_to_quarantine(w):
    pid, mem, maint = w.pid, w.a["mem"], w.a["maint"]
    assert _status(w.team.push_notes, mem, pid, "mc_a", [_note("fact-ci")]) == (409, "mirror_off")
    assert _status(w.team.notes, mem, pid, None) == (409, "mirror_off")
    w.team.patch(w.a["owner"], pid, {"mirror": True})
    assert w.team.push_notes(mem, pid, "mc_a", [_note("fact-ci")]) == {"accepted": 1, "skipped": 0, "quarantined": 0}
    assert w.team.push_notes(mem, pid, "mc_a", [_note("fact-ci")])["skipped"] == 1  # renvoi idempotent
    pulled = w.team.notes(w.a["view"], pid, None)
    assert [n["name"] for n in pulled["notes"]] == ["fact-ci"]
    sha = pulled["notes"][0]["sha"]
    # le maintainer modifie la version qu'il a : pas de conflit
    r = w.team.push_notes(maint, pid, "mc_b", [_note("fact-ci", "CI sur Forgejo Actions.", t=1_799_999_100.0,
                                                     base_sha=sha)])
    assert r == {"accepted": 1, "skipped": 0, "quarantined": 0}
    # conflit : une version plus ancienne, sans la bonne base → quarantaine, la version du projet reste
    r = w.team.push_notes(mem, pid, "mc_a", [_note("fact-ci", "CI sur GitHub.", t=1_799_999_050.0, base_sha=sha)])
    assert r == {"accepted": 0, "skipped": 0, "quarantined": 1}
    assert w.team.store.note(mem, w.team.store.project(mem, pid), "fact-ci").text == "CI sur Forgejo Actions."
    # conflit : une version plus récente gagne, l'ancienne part en quarantaine
    r = w.team.push_notes(mem, pid, "mc_a", [_note("fact-ci", "CI sur Woodpecker.", t=1_799_999_200.0)])
    assert r == {"accepted": 1, "skipped": 0, "quarantined": 1}
    q = w.team.quarantined(maint, pid)["quarantine"]
    assert [x["text"] for x in q] == ["CI sur GitHub.", "CI sur Forgejo Actions."]
    assert _status(w.team.quarantined, mem, pid)[0] == 403
    # curseur : seules les notes écrites après
    cur = pulled["cursor"]
    after = w.team.notes(w.a["view"], pid, cur)["notes"]
    assert [n["text"] for n in after] == ["CI sur Woodpecker."]
    # effacement propagé : pierre tombale
    last = w.team.notes(mem, pid, None)["notes"][-1]
    w.team.push_notes(mem, pid, "mc_a", [{"name": "fact-ci", "kind": "fact", "deleted": True,
                                          "updated_at": 1_799_999_300.0, "base_sha": last["sha"]}])
    tomb = w.team.notes(mem, pid, None)["notes"][-1]
    assert tomb["deleted"] and "text" not in tomb


def test_notes_are_validated_and_scrubbed(w):
    pid, mem = w.pid, w.a["mem"]
    w.team.patch(w.a["owner"], pid, {"mirror": True})
    for bad in ({**_note("src-abc-1")}, {**_note("x"), "kind": "section"}, {**_note("x"), "scope": "user"},
                {**_note("x"), "text": "a" * 1501}, {**_note("x"), "updated_at": "hier"}, "texte"):
        assert _status(w.team.push_notes, mem, pid, "mc_a", [bad])[0] == 400, bad
    assert _status(w.team.push_notes, mem, pid, "mc_a", [_note(f"f-{i}") for i in range(201)])[0] == 413
    w.team.push_notes(mem, pid, "mc_a", [_note("fact-key", "la clé est sk-SECRET123")])
    assert w.team.notes(mem, pid, None)["notes"][0]["text"] == "la clé est [caviardé]"


def test_machine_view(w):
    pid = w.pid
    w.team.put_rules(w.a["maint"], pid, {"deny": ["terminal(rm -rf *)"]})
    w.team.put_skill(w.a["maint"], pid, "lint", {"source": "official/dev/lint"})
    view = w.team.machine_projects(w.a["view"])["projects"][0]
    assert view["rules"]["deny"] == ["terminal(rm -rf *)"] and view["skills"][0]["name"] == "lint"
    assert view["mirror"] is False and view["can_push_notes"] is False
    w.team.patch(w.a["owner"], pid, {"mirror": True})
    assert w.team.machine_projects(w.a["view"])["projects"][0]["mirror"] is True
    assert w.team.machine_projects(w.a["view"])["projects"][0]["can_push_notes"] is False
    assert w.team.machine_projects(w.a["mem"])["projects"][0]["can_push_notes"] is True
    w.team.mirror_allowed = lambda acc: False  # pas abonné : les notes ne passent pas par le miroir
    assert w.team.machine_projects(w.a["mem"])["projects"][0]["mirror"] is False


def test_project_agents_are_the_actors_own(w):
    pid = w.pid
    a1 = w.agents.create_agent(name="veille", role="r", model="", created_by=w.a["mem"].account_id, project=pid)
    w.agents.create_agent(name="autre", role="r", model="", created_by=w.a["mem"].account_id)
    w.agents.create_agent(name="maint", role="r", model="", created_by=w.a["maint"].account_id, project=pid)
    assert [a.id for a in w.team.project_agents(w.a["mem"], pid)] == [a1.id]


def test_limits(w):
    o = w.a["owner"]
    for i in range(19):
        w.team.create(o, {"name": f"p{i}"})
    assert _status(w.team.create, o, {"name": "trop"}) == (409, "too_many")
    assert _status(w.team.create, w.a["out"], {"name": ""})[0] == 400
    assert _status(w.team.patch, o, w.pid, {"budget": {"monthly": -1}})[0] == 400
    assert _status(w.team.patch, o, w.pid, {"budget": {"monthly": 10, "currency": "BTC"}})[0] == 400
    assert w.team.patch(o, w.pid, {"budget": {"monthly": "12.5", "currency": "chf"}})["budget"] == {
        "monthly": 12.5, "currency": "CHF"}
    assert _status(w.team.patch, o, w.pid, {"repos": ["pas une url"]})[0] == 400
    assert w.team.patch(o, w.pid, {"repos": ["https://tok:x@github.com/acme/api.git", "git@github.com:acme/api"]}
                        )["repos"] == [{"key": "github.com/acme/api",
                                        "memory_project": w.team.get(o, w.pid)["repos"][0]["memory_project"]}]
