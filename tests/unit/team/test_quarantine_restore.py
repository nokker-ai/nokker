"""Quarantaine des notes d'équipe (docs/LOKKAL.md §7.23) : un owner ou un maintainer remet une version remplacée ou
oubliée en version courante ; la version courante part en quarantaine à son tour (rien n'est effacé), les machines la
tirent au passage suivant, et le geste est journalisé."""
from __future__ import annotations

import pytest

from nokker.team.model import TeamError, note_sha
from nokker.team.service import Team
from nokker.team.store import MemoryTeamStore
from nokker.tenancy.store import MemoryTenancy


@pytest.fixture
def w():
    tenancy = MemoryTenancy()
    a = {k: tenancy.create_account(f"{k}@example.org") for k in ("owner", "maint", "mem", "view")}
    team = Team(store=MemoryTeamStore(), tenancy=tenancy, mirror_allowed=lambda acc: True)
    pid = team.create(a["owner"], {"name": "Plateforme"})["id"]
    team.patch(a["owner"], pid, {"mirror": True})
    for who, role in (("maint", "maintainer"), ("mem", "member"), ("view", "viewer")):
        team.accept(a[who], team.invite(a["owner"], pid, {"email": f"{who}@example.org", "role": role})["id"])

    class W:
        pass

    x = W()
    x.__dict__.update(team=team, a=a, pid=pid)
    return x


def note(text, **kw):
    return {"name": "fact-ci", "kind": "fact", "title": "CI", "text": text, "locality": "both", **kw}


def _status(fn, *args):
    with pytest.raises(TeamError) as e:
        fn(*args)
    return e.value.status, e.value.code


def conflict(w):
    """mem écrit v1 ; maint écrit v2 sans connaître v1 : v2 gagne, v1 part en quarantaine."""
    w.team.push_notes(w.a["mem"], w.pid, "mc_mem", [note("La CI tourne sur la forge.", links=["fact-runner"],
                                                         origin="session th_1")])
    w.team.push_notes(w.a["maint"], w.pid, "mc_maint", [note("La CI tourne ailleurs.")])
    [q] = w.team.quarantined(w.a["maint"], w.pid)["quarantine"]
    return q


def current(w):
    [n] = [x for x in w.team.notes(w.a["view"], w.pid, None)["notes"] if x["name"] == "fact-ci"]
    return n


def test_restore_makes_the_kept_version_current_and_quarantines_the_current_one(w):
    q = conflict(w)
    assert q["text"] == "La CI tourne sur la forge." and q["restored_at"] is None and isinstance(q["id"], int)
    before = current(w)
    got = w.team.restore_note(w.a["maint"], w.pid, q["id"])["note"]
    assert got["text"] == "La CI tourne sur la forge." and got["links"] == ["fact-runner"]
    assert got["origin"] == "session th_1" and got["author_account"] == w.a["mem"].account_id
    assert got["sha"] == note_sha("fact", "CI", "La CI tourne sur la forge.", False)
    now = current(w)
    assert now["text"] == "La CI tourne sur la forge." and now["seq"] > before["seq"]  # les machines la tirent
    # rien n'est effacé : la version remplacée par la restauration est dans la quarantaine, l'ancienne y reste
    rows = w.team.quarantined(w.a["owner"], w.pid)["quarantine"]
    assert [r["text"] for r in rows] == ["La CI tourne sur la forge.", "La CI tourne ailleurs."]
    assert rows[0]["restored_by"] == w.a["maint"].account_id and rows[0]["restored_at"]
    assert rows[1]["reason"].startswith("remplacée par la restauration")
    [ev] = w.team.audit(w.a["owner"], w.pid)["events"]
    assert ev["event"] == "note_restored" and ev["actor_account"] == w.a["maint"].account_id
    assert ev["detail"] == {"name": "fact-ci", "quarantine_id": q["id"], "sha": got["sha"]}
    assert "La CI" not in repr(ev)  # le journal ne recopie pas le texte d'une note


def test_restoring_back_and_forth_keeps_every_version(w):
    q = conflict(w)
    w.team.restore_note(w.a["owner"], w.pid, q["id"])
    newer = w.team.quarantined(w.a["owner"], w.pid)["quarantine"][-1]
    w.team.restore_note(w.a["owner"], w.pid, newer["id"])
    assert current(w)["text"] == "La CI tourne ailleurs."
    assert len(w.team.quarantined(w.a["owner"], w.pid)["quarantine"]) == 3


def test_a_forgotten_note_comes_back(w):
    w.team.push_notes(w.a["mem"], w.pid, "mc_mem", [note("Déploiement le mardi.")])
    sha = note_sha("fact", "CI", "Déploiement le mardi.", False)
    w.team.push_notes(w.a["mem"], w.pid, "mc_mem", [{"name": "fact-ci", "kind": "fact", "deleted": True,
                                                    "locality": "both", "base_sha": sha}])
    assert current(w)["deleted"] is True
    [q] = w.team.quarantined(w.a["maint"], w.pid)["quarantine"]
    assert q["reason"] == "oubliée par un membre du projet"
    w.team.restore_note(w.a["maint"], w.pid, q["id"])
    assert current(w)["deleted"] is False and current(w)["text"] == "Déploiement le mardi."
    assert len(w.team.quarantined(w.a["maint"], w.pid)["quarantine"]) == 1  # une pierre tombale n'a rien à garder


def test_rights_and_errors(w):
    q = conflict(w)
    for who in ("mem", "view"):
        assert _status(w.team.restore_note, w.a[who], w.pid, q["id"]) == (403, "forbidden")
    assert _status(w.team.restore_note, w.a["maint"], w.pid, 999) == (404, "not_found")
    assert _status(w.team.restore_note, w.a["maint"], w.pid, "abc") == (404, "not_found")
    w.team.restore_note(w.a["maint"], w.pid, q["id"])
    assert _status(w.team.restore_note, w.a["maint"], w.pid, q["id"]) == (409, "already_current")
    other = w.team.create(w.a["maint"], {"name": "Autre"})["id"]  # l'id d'une version d'un autre projet : inconnue
    assert _status(w.team.restore_note, w.a["maint"], other, q["id"]) == (404, "not_found")
