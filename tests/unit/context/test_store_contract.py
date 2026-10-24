"""Test de CONTRAT des stockages `EpisodeStore` / `ReservoirStore` : mêmes scénarios sur les backends mémoire et
Postgres. Postgres est ignoré sans `NOKKER_TEST_DSN` (ex. postgresql://postgres:x@127.0.0.1:55432/postgres)."""
from __future__ import annotations

import os
import uuid

import pytest

from nokker.context.memory import MemoryEpisodeStore, MemoryReservoirStore
from nokker.context.types import DaySummary, Episode, ReservoirItem, ViewState

DSN = os.environ.get("NOKKER_TEST_DSN", "")


def _pg_skip() -> None:
    if not DSN:
        pytest.skip("NOKKER_TEST_DSN absent : backend Postgres ignoré")


@pytest.fixture(autouse=True)
def _tenant():
    """Les stores Postgres lisent et écrivent dans le locataire courant (RLS, docs/TENANCY.md §4)."""
    from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

    with tenant_scope(INTERNAL_TENANT):
        yield


@pytest.fixture(params=["memory", "pg"])
def episodes(request):
    if request.param == "memory":
        yield MemoryEpisodeStore()
        return
    _pg_skip()
    from nokker.context.pg import PgEpisodeStore

    store = PgEpisodeStore(DSN)
    yield store
    store.close()


@pytest.fixture(params=["memory", "pg"])
def reservoir(request):
    if request.param == "memory":
        yield MemoryReservoirStore()
        return
    _pg_skip()
    from nokker.context.pg import PgReservoirStore

    store = PgReservoirStore(DSN)
    yield store
    store.close()


@pytest.fixture
def tid() -> str:
    """Identifiant de fil unique : la base Postgres de test peut être partagée et non vidée."""
    return "t-" + uuid.uuid4().hex[:10]


def _ep(tid: str, seq: int, **kw) -> Episode:
    base = {"id": f"ep_{uuid.uuid4().hex[:10]}", "thread_id": tid, "seq": seq, "first_turn": seq * 10,
            "opened_at": 100.0 + seq}
    base.update(kw)
    return Episode(**base)


def _closed(tid: str, seq: int, **kw) -> Episode:
    return _ep(tid, seq, last_turn=seq * 10 + 9, closed_at=200.0 + seq, closed_reason="watermark",
               day="2026-10-19", **kw)


# --- épisodes ---------------------------------------------------------------------------------------------------

def test_episode_roundtrip_complet(episodes, tid):
    e = Episode(id="ep_" + uuid.uuid4().hex[:10], thread_id=tid, seq=0, first_turn=0, last_turn=7, title="Titre é",
                summary="Résumé", keywords=["a", "b"], raw_tokens=123, summary_tokens=12, opened_at=1.5,
                closed_at=2.25, closed_reason="topic", day="2026-10-19", turn_ids_sha256="ab" * 32, refined=True,
                bubble_id="bb_1", version=1)
    episodes.save(e)
    assert episodes.get(e.id).to_dict() == e.to_dict()
    assert episodes.get("ep_inconnu") is None


def test_episode_save_incremente_version(episodes, tid):
    e = _ep(tid, 0)
    episodes.save(e)
    assert episodes.get(e.id).version == 1
    e.title = "v2"
    episodes.save(e)
    assert e.version == 2
    got = episodes.get(e.id)
    assert (got.version, got.title) == (2, "v2")
    e.title = "v3"
    episodes.save(e)
    assert episodes.get(e.id).version == 3


def test_episode_courant(episodes, tid):
    assert episodes.current(tid) is None
    e0 = _closed(tid, 0)
    e1 = _ep(tid, 1)
    e2 = _ep(tid, 2)
    for e in (e1, e0, e2):
        episodes.save(e)
    assert episodes.current(tid).id == e2.id  # l'ouvert de seq maximal
    e2.closed_at, e2.closed_reason = 300.0, "manual"
    episodes.save(e2)
    assert episodes.current(tid).id == e1.id
    e1.closed_at, e1.closed_reason = 301.0, "marker"
    episodes.save(e1)
    assert episodes.current(tid) is None


def test_episode_list_ordre_et_closed_only(episodes, tid):
    e = [_closed(tid, 0), _closed(tid, 1), _ep(tid, 2)]
    for x in (e[2], e[0], e[1]):  # insertion dans le désordre
        episodes.save(x)
    assert [x.id for x in episodes.list(tid)] == [x.id for x in e]
    assert [x.id for x in episodes.list(tid, closed_only=True)] == [e[0].id, e[1].id]
    assert episodes.list("t-vide-" + tid) == []


def test_next_seq_par_fil(episodes, tid):
    other = tid + "-bis"
    assert episodes.next_seq(tid) == 0
    episodes.save(_closed(tid, 0))
    episodes.save(_ep(tid, 1))
    assert episodes.next_seq(tid) == 2
    assert episodes.next_seq(other) == 0  # fils isolés
    episodes.save(_ep(other, 0))
    assert episodes.next_seq(other) == 1
    assert episodes.next_seq(tid) == 2


def test_threads_with_open_episode(episodes, tid):
    a, b, c = tid + "-a", tid + "-b", tid + "-c"
    episodes.save(_ep(b, 0))
    episodes.save(_closed(c, 0))
    episodes.save(_ep(a, 0))
    episodes.save(_ep(a, 1))
    got = episodes.threads_with_open_episode()
    assert {a, b} <= set(got) and c not in got
    assert got == sorted(got) and len(got) == len(set(got))


def test_resumes_de_jour(episodes, tid):
    assert episodes.get_day(tid, "2026-10-18") is None
    assert episodes.list_days(tid) == []
    d2 = DaySummary(id="dy_2", thread_id=tid, day="2026-10-19", summary="deux", episode_ids=["ep_a", "ep_b"],
                    tokens=9, created_at=5.5)
    d1 = DaySummary(id="dy_1", thread_id=tid, day="2026-10-18", summary="un", episode_ids=["ep_c"], tokens=3,
                    created_at=4.5)
    episodes.save_day(d2)
    episodes.save_day(d1)
    assert episodes.get_day(tid, "2026-10-19").to_dict() == d2.to_dict()
    assert [d.day for d in episodes.list_days(tid)] == ["2026-10-18", "2026-10-19"]
    d2b = DaySummary(id="dy_2b", thread_id=tid, day="2026-10-19", summary="deux bis", episode_ids=["ep_a"],
                     tokens=4, created_at=6.5)
    episodes.save_day(d2b)  # même clé (fil, jour) : remplace
    assert len(episodes.list_days(tid)) == 2
    assert episodes.get_day(tid, "2026-10-19").to_dict() == d2b.to_dict()
    assert episodes.list_days(tid + "-autre") == []


def test_etat_de_vue_aller_retour(episodes, tid):
    assert episodes.get_state(tid) is None
    s = ViewState(thread_id=tid, episode_id="ep_x", base_turn=12, last_turn_at=1234.5, last_turn_index=40,
                  tokens_since_compaction=777, compaction_version=3,
                  shed={"5:call_1": "rs_a", "6:call_é": "rs_b"}, pinned_episodes=["ep_1", "ep_2"],
                  updated_at=1235.5, heads_shed_before=17)  # champ oublié côté Pg en revue le 20.10 : il fait l'aller-retour
    episodes.save_state(s)
    assert episodes.get_state(tid).to_dict() == s.to_dict()
    s.shed = {"9:c": "rs_z"}
    s.pinned_episodes = []
    s.episode_id = None
    s.compaction_version = 4
    episodes.save_state(s)
    got = episodes.get_state(tid)
    assert got.to_dict() == s.to_dict()
    assert got.shed == {"9:c": "rs_z"} and got.pinned_episodes == [] and got.episode_id is None
    assert episodes.get_state(tid + "-autre") is None


# --- réservoir --------------------------------------------------------------------------------------------------

def _item(tid: str, n: int, **kw) -> ReservoirItem:
    base = {"id": f"rs_{uuid.uuid4().hex[:8]}", "thread_id": tid, "kind": "tool_result", "name": "shell",
            "content": "x" * n, "preview": "x", "tokens": n, "created_at": 1000.0 + n}
    base.update(kw)
    return ReservoirItem(**base)


def test_reservoir_put_get_roundtrip(reservoir, tid):
    it = ReservoirItem(id="rs_" + uuid.uuid4().hex[:8], thread_id=tid, kind="episode_export", name="Titre",
                       content="contenu é\nmultiligne", preview="extrait", tokens=42, turn_index=7,
                       call_id="call_9", created_at=12.5, origin={"thread_id": "t-src", "episode_id": "ep_1"})
    reservoir.put(it)
    assert reservoir.get(it.id).to_dict() == it.to_dict()
    assert reservoir.get("rs_inconnu") is None
    sans_origine = _item(tid, 1)
    reservoir.put(sans_origine)
    got = reservoir.get(sans_origine.id)
    assert got.origin is None and got.turn_index is None and got.call_id is None


def test_reservoir_list_ordre_kind_limit(reservoir, tid):
    items = [_item(tid, n, kind="tool_result" if n % 2 else "document") for n in (5, 1, 4, 2, 3)]
    for it in items:
        reservoir.put(it)
    ordered = sorted(items, key=lambda i: (i.created_at, i.id))
    assert [i.id for i in reservoir.list(tid)] == [i.id for i in ordered]
    # `limit` : les DERNIERS, toujours en ordre croissant
    assert [i.id for i in reservoir.list(tid, limit=2)] == [i.id for i in ordered[-2:]]
    docs = [i for i in ordered if i.kind == "document"]
    assert [i.id for i in reservoir.list(tid, kind="document")] == [i.id for i in docs]
    assert [i.id for i in reservoir.list(tid, kind="document", limit=1)] == [docs[-1].id]
    assert reservoir.list(tid, kind="episode_export") == []


def test_reservoir_ordre_stable_sur_created_at_egal(reservoir, tid):
    a, b, c = (_item(tid, 1, id=f"rs_{k}_{tid}", created_at=50.0) for k in "bac")
    for it in (a, b, c):
        reservoir.put(it)
    assert [i.id for i in reservoir.list(tid)] == sorted(i.id for i in (a, b, c))


def test_reservoir_isolation_par_fil(reservoir, tid):
    autre = tid + "-autre"
    mine = _item(tid, 1, turn_index=3, call_id="c1")
    theirs = _item(autre, 2, turn_index=3, call_id="c1")
    reservoir.put(mine)
    reservoir.put(theirs)
    assert [i.id for i in reservoir.list(tid)] == [mine.id]
    assert [i.id for i in reservoir.list(autre)] == [theirs.id]
    assert reservoir.find(tid, 3, "c1").id == mine.id
    assert reservoir.find(autre, 3, "c1").id == theirs.id


def test_reservoir_find(reservoir, tid):
    it = _item(tid, 1, turn_index=4, call_id="call_a")
    reservoir.put(it)
    reservoir.put(_item(tid, 2, turn_index=4, call_id="call_b"))
    reservoir.put(_item(tid, 3))  # sans turn_index ni call_id
    assert reservoir.find(tid, 4, "call_a").id == it.id
    assert reservoir.find(tid, 5, "call_a") is None
    assert reservoir.find(tid, 4, "absent") is None
