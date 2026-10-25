"""Miroir, sur un vrai Postgres sous RLS (rôles de deploy/postgres/roles.sql, noyau en `nokker_app`) : notes de projet
d'équipe (écriture atomique sous verrou du projet, la dernière reçue gagne, l'autre gardée, curseur sans trou sous
écritures concurrentes, lecteur refusé, note `local` refusée) et pages copiées (isolation par locataire, plafond par
compte, purge). Ignoré sans `NOKKER_TEST_PG_DSN`."""
from __future__ import annotations

import threading

import pytest

from tests.integration.test_rls import A, B, pg, requires_pg  # noqa: F401 — fixture (base neuve, rôles, migrations)
from tests.integration.test_team_rls import MEM, OUT, OWNER, VIEW, Accounts

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def team(pg):  # noqa: F811
    from nokker.team.service import Team
    from nokker.team.store import PgTeamStore
    from nokker.tenancy.context import tenant_scope

    t = Team(store=PgTeamStore(pg["app"], migrate_now=False), tenancy=Accounts(OWNER, MEM, VIEW, OUT),
             mirror_allowed=lambda acc: True)
    with tenant_scope(OWNER.tenant_id):
        pid = t.create(OWNER, {"name": "Miroir", "repos": ["github.com/acme/miroir"]})["id"]
        t.patch(OWNER, pid, {"mirror": True})
        inv_m = t.invite(OWNER, pid, {"email": MEM.email, "role": "member"})
        inv_v = t.invite(OWNER, pid, {"email": VIEW.email, "role": "viewer"})
    with tenant_scope(MEM.tenant_id):
        t.accept(MEM, inv_m["id"])
    with tenant_scope(VIEW.tenant_id):
        t.accept(VIEW, inv_v["id"])
    return t, pid


def _note(name, text, **kw):
    return {"name": name, "kind": "fact", "title": "T", "text": text, "locality": "both", **kw}


def _push(t, who, pid, notes, machine="mc_x"):
    from nokker.tenancy.context import tenant_scope

    with tenant_scope(who.tenant_id):
        return t.push_notes(who, pid, machine, notes)


def _pull(t, who, pid, since=None):
    from nokker.tenancy.context import tenant_scope

    with tenant_scope(who.tenant_id):
        return t.notes(who, pid, since)


@requires_pg
def test_team_notes_conflict_forget_and_barriers_on_pg(team):
    from nokker.team.model import TeamError
    from nokker.tenancy.context import tenant_scope

    t, pid = team
    assert _push(t, MEM, pid, [_note("fact-ci", "Forgejo")])["accepted"] == 1
    base = _pull(t, OWNER, pid)["notes"][0]["sha"]
    assert _push(t, OWNER, pid, [_note("fact-ci", "Forgejo Actions", base_sha=base)])["quarantined"] == 0
    r = _push(t, MEM, pid, [_note("fact-ci", "GitHub", base_sha=base)])  # base dépassée, reçue après : gagne
    assert r == {"accepted": 1, "skipped": 0, "quarantined": 1, "refused": 0}
    with tenant_scope(OWNER.tenant_id):
        q = t.quarantined(OWNER, pid)["quarantine"]
    assert [x["text"] for x in q] == ["Forgejo Actions"]
    cur = _pull(t, VIEW, pid)["notes"][0]
    assert cur["text"] == "GitHub" and cur["author_account"] == MEM.account_id
    r = _push(t, MEM, pid, [{"name": "fact-ci", "kind": "fact", "deleted": True, "locality": "both",
                             "base_sha": cur["sha"]}])
    assert r["accepted"] == 1 and _pull(t, VIEW, pid)["notes"][0]["deleted"]
    with tenant_scope(OWNER.tenant_id):
        assert t.quarantined(OWNER, pid)["quarantine"][-1]["text"] == "GitHub"
    # barrières : note `local` jamais gardée ; un lecteur ne pousse rien ; un inconnu ne voit pas le projet
    assert _push(t, MEM, pid, [_note("fact-local", "maison", locality="local")])["refused"] == 1
    assert "fact-local" not in {n["name"] for n in _pull(t, OWNER, pid)["notes"]}
    for who, status in ((VIEW, 403), (OUT, 404)):
        with pytest.raises(TeamError) as e:
            _push(t, who, pid, [_note("fact-v", "x")])
        assert e.value.status == status


@requires_pg
def test_concurrent_writers_keep_every_version_and_the_cursor_never_skips(team):
    """Quatre machines écrivent en même temps (même note et notes différentes) pendant qu'un lecteur tire par curseur :
    aucune version perdue (la quarantaine garde les remplacées), aucune écriture sautée par le curseur."""
    from nokker.tenancy.context import tenant_scope

    t, pid = team
    _push(t, OWNER, pid, [_note("fact-hot", "v0")])
    base = _pull(t, OWNER, pid)["notes"][-1]["sha"]
    with tenant_scope(OWNER.tenant_id):
        before = len(t.quarantined(OWNER, pid)["quarantine"])
    done = threading.Event()
    seen: dict[str, int] = {}

    def reader():
        cursor = None
        while True:
            last = done.is_set()
            got = _pull(t, VIEW, pid, cursor)
            for n in got["notes"]:
                seen[n["name"]] = n["seq"]
            cursor = got["cursor"]
            if last:
                return

    def writer(i):
        who = OWNER if i % 2 == 0 else MEM
        for k in range(15):
            _push(t, who, pid, [_note(f"fact-w{i}-{k}", f"écrit par {i}"),
                                _note("fact-hot", f"chaud {i}-{k}", base_sha=base)], machine=f"mc_{i}")

    rt = threading.Thread(target=reader)
    rt.start()
    ws = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    [w.start() for w in ws]
    [w.join() for w in ws]
    done.set()
    rt.join()
    final = {n["name"]: n for n in _pull(t, VIEW, pid)["notes"]}
    expected = {f"fact-w{i}-{k}" for i in range(4) for k in range(15)}
    assert expected <= set(seen) and seen["fact-hot"] == final["fact-hot"]["seq"]  # rien de sauté
    with tenant_scope(OWNER.tenant_id):
        kept = [q["text"] for q in t.quarantined(OWNER, pid)["quarantine"][before:] if q["name"] == "fact-hot"]
    # 60 écritures sur la base v0 : la première reçue est une mise à jour simple, chacune des 59 suivantes remplace la
    # précédente, gardée en quarantaine ; aucune des 60 versions ne disparaît
    assert len(kept) == 59 and final["fact-hot"]["text"] not in kept
    assert len({*kept, final["fact-hot"]["text"]}) == 60


@requires_pg
def test_mirror_pages_isolated_quota_and_purge_on_pg(pg):  # noqa: F811
    import psycopg

    from nokker.cloud.mirror_pages import QuotaExceeded
    from nokker.cloud.pg import PgMirrorPages
    from nokker.tenancy.context import tenant_scope

    s = PgMirrorPages(pg["app"])
    row = {"title": "Bilan", "version": 1, "size": 10, "sha256": "s", "html": b"<p>bilan</p>",
           "page_updated_at": 1.0, "mirrored_at": 2.0}
    with tenant_scope(A):
        assert s.put(A, "mc_a", "pg_aaaaaaaaaaaaaaaa", row, quota=25) == 10
        assert s.put(A, "mc_a", "pg_aaaaaaaaaaaaaaaa", {**row, "version": 2}, quota=25) == 10  # remplacée
        assert s.put(A, "mc_a2", "pg_bbbbbbbbbbbbbbbb", row, quota=25) == 20  # plafond du COMPTE, toutes machines
        with pytest.raises(QuotaExceeded):
            s.put(A, "mc_a", "pg_cccccccccccccccc", row, quota=25)
        assert s.get(A, "mc_a", "pg_aaaaaaaaaaaaaaaa")["version"] == 2
    with tenant_scope(B):
        assert s.used(B) == 0 and s.of_machine(B, "mc_a") == [] and s.get(B, "mc_a", "pg_aaaaaaaaaaaaaaaa") is None
        assert not s.delete(B, "mc_a", "pg_aaaaaaaaaaaaaaaa")
        assert s.put(B, "mc_b", "pg_dddddddddddddddd", row, quota=25) == 10  # le plafond de A ne compte pas
    with tenant_scope(A):
        assert [r["page_id"] for r in s.of_machine(A, "mc_a")] == ["pg_aaaaaaaaaaaaaaaa"]
        assert "html" not in s.of_machine(A, "mc_a")[0]
        assert s.erase(A, "mc_a") == 1 and s.used(A) == 10
    # B écrit au nom de A : refusé par la RLS
    from nokker.store.conn import connect

    with tenant_scope(B), connect(pg["app"], autocommit=True) as conn, \
            pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("INSERT INTO lokkal_mirror_pages (machine_id, page_id, tenant_id, version, size, sha256, "
                     "html, page_updated_at, mirrored_at) VALUES ('mc', 'pg_eeeeeeeeeeeeeeee', %s, 1, 1, 's', "
                     "'\\x00', 1, 1)", (A,))
