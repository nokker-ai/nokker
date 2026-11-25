"""Vrai Postgres pour `PgThreads`/`PgBubbles` (docs/BUBBLES.md) : migrations idempotentes, boîte de contexte,
version optimiste (409), listes partagées. Ignoré sans `NOKKER_TEST_PG_DSN` (comme test_pg_real.py)."""
from __future__ import annotations

import os
import uuid

import pytest

DSN = os.environ.get("NOKKER_TEST_PG_DSN")

pytestmark = pytest.mark.integration
requires_pg = pytest.mark.skipif(not DSN, reason="NOKKER_TEST_PG_DSN non posé — pas de Postgres jetable")


@pytest.fixture(autouse=True)
def _tenant():
    """Les stores Postgres lisent et écrivent dans le locataire courant (RLS, docs/TENANCY.md §4)."""
    from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

    with tenant_scope(INTERNAL_TENANT):
        yield


@requires_pg
def test_threads_round_trip_and_inbox():
    from nokker.threads.pg import PgThreads

    th = PgThreads(dsn=DSN)
    PgThreads(dsn=DSN).close()  # migration rejouée sans erreur
    t = th.create("Infra", "alice", members=["bruno"])
    got = th.get(t.id)
    assert got.title == "Infra" and got.owner == "alice" and got.members == ["bruno"]
    th.update(t.id, working=True, last_snippet="Disque à 91 %")
    assert th.get(t.id).working is True and th.get(t.id).last_snippet == "Disque à 91 %"
    assert t.id in [x.id for x in th.list_for("bruno")]
    th.push_context(t.id, "bloc 1")
    th.push_context(t.id, "bloc 2")
    assert th.drain_context(t.id) == ["bloc 1", "bloc 2"]
    assert th.drain_context(t.id) == []
    th.close()


@requires_pg
def test_bubbles_round_trip_version_and_shared():
    from nokker.bubbles.pg import PgBubbles
    from nokker.bubbles.types import VersionConflict

    bs = PgBubbles(dsn=DSN)
    tid, other = f"th_{uuid.uuid4().hex[:12]}", f"th_{uuid.uuid4().hex[:12]}"
    b = bs.create(thread_id=tid, kind="monitor", title="Disque", state={"value": 91})
    b2 = bs.update(b.id, status="working", if_version=1)
    assert b2.version == 2 and bs.get(b.id).status == "working"
    with pytest.raises(VersionConflict):
        bs.update(b.id, title="x", if_version=1)
    bs.update(b.id, shared_threads=[other], shared_members=["bruno"])
    assert [x.id for x in bs.list_shared_with_thread(other)] == [b.id]
    c = bs.clone(bs.get(b.id), thread_id=other, origin={"thread_id": tid, "bubble_id": b.id, "mode": "copy"})
    assert bs.get(c.id).origin["mode"] == "copy" and bs.get(c.id).version == 1
    n = bs.create(thread_id=None, kind="notify", title="Compta", shared_members=["alice"])
    assert n.id in [x.id for x in bs.list_open_notify()]
    bs.update(n.id, close=True)
    assert n.id not in [x.id for x in bs.list_open_notify()]
    assert [x.id for x in bs.list_thread(tid)] == [b.id]
    bs.close()


@requires_pg
def test_home_bubble_watcher_thread_updates_pg_bubbles_under_rls():
    """Le fil de fond des bulles maison (nokker/home/bubbles.py) met à jour une bulle Postgres sous RLS : sans
    `tenant_scope` dans le fil, NoTenant et « connexion perdue » en boucle (Lokkal rc.9.32)."""
    import threading
    import time
    from types import SimpleNamespace

    from nokker.bubbles.pg import PgBubbles
    from nokker.home.bubbles import HomeBubbleWatcher

    bs = PgBubbles(dsn=DSN)
    b = bs.create(thread_id=f"th_{uuid.uuid4().hex[:12]}", kind="home", title="Salon", state={"state": "off"})
    done = threading.Event()

    class Hub:
        bubbles = bs

        def update_from_kernel(self, bid, state):
            bs.update(bid, state=state)
            done.set()

    class Session:
        sent = False

        def subscribe_state_changed(self):
            pass

        def events(self, timeout):
            if not Session.sent:
                Session.sent = True
                yield {"event": {"event_type": "state_changed",
                                 "data": {"entity_id": "light.salon", "new_state": {"state": "on"}}}}
            else:
                time.sleep(0.05)

        def close(self):
            pass

    src = SimpleNamespace(hub=Hub(), _snap=None, client=SimpleNamespace(session=Session),
                          bubble_state=lambda snap, eid: {"entity_id": eid, "state": snap.states[eid]["state"]})
    w = HomeBubbleWatcher(source=src, idle_s=0.05, backoff_max_s=0.1)
    w.track("light.salon", b.id)
    try:
        assert done.wait(10), "bulle jamais mise à jour : NoTenant dans le fil de fond ?"
    finally:
        w.stop()
        w._untrack("light.salon", b.id)
    assert bs.get(b.id).state["state"] == "on"
    bs.close()
