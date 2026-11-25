"""Flux des bulles maison (nokker/home/bubbles.py) : le fil de fond tourne dans un locataire (Lokkal : le locataire
interne), comme les autres fils de fond locaux ; sans scope, les bulles Postgres (RLS) lèvent NoTenant et le fil
« perdait sa connexion » toutes les 10 s."""
from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace

from nokker.home.bubbles import HomeBubbleWatcher
from nokker.tenancy.context import INTERNAL_TENANT, current_tenant, tenant_scope


class _Bubble:
    is_open = True


class _Bubbles:
    """Backend qui exige le scope locataire, comme PgBubbles sous RLS."""

    def get(self, bid):
        current_tenant()  # NoTenant hors tenant_scope
        return _Bubble()


class _Hub:
    def __init__(self):
        self.bubbles = _Bubbles()
        self.updated = threading.Event()
        self.tenants: list[str] = []

    def update_from_kernel(self, bid, state):
        self.tenants.append(current_tenant())
        self.updated.set()


class _Session:
    def __init__(self, n):
        self.n = n

    def subscribe_state_changed(self):
        pass

    def events(self, timeout):
        if self.n[0] == 0:
            self.n[0] += 1
            yield {"event": {"event_type": "state_changed",
                             "data": {"entity_id": "light.salon", "new_state": {"state": "on", "attributes": {}}}}}
        else:
            time.sleep(0.05)

    def close(self):
        pass


def _source(hub):
    n = [0]
    return SimpleNamespace(
        hub=hub, _snap=None, client=SimpleNamespace(session=lambda: _Session(n)),
        bubble_state=lambda snap, eid: {"entity_id": eid, "state": snap.states[eid]["state"]})


def test_background_thread_runs_in_the_internal_tenant_without_error_loop(caplog):
    hub = _Hub()
    w = HomeBubbleWatcher(source=_source(hub), idle_s=0.05, backoff_max_s=0.1)
    caplog.set_level(logging.INFO, logger="nokker.home.bubbles")
    w.track("light.salon", "bb_1")  # appelé HORS de tout tenant_scope (le fil de fond ne l'hérite pas)
    try:
        assert hub.updated.wait(5), "la bulle n'a jamais été mise à jour"
    finally:
        w.stop()
        w._untrack("light.salon", "bb_1")
    assert hub.tenants and set(hub.tenants) == {INTERNAL_TENANT}
    assert not [r for r in caplog.records if "connexion perdue" in r.getMessage()]


def test_thread_keeps_the_tenant_of_the_caller_when_there_is_one():
    hub = _Hub()
    w = HomeBubbleWatcher(source=_source(hub), idle_s=0.05)
    with tenant_scope("tn_abc"):
        w.track("light.salon", "bb_1")
    try:
        assert hub.updated.wait(5)
    finally:
        w.stop()
        w._untrack("light.salon", "bb_1")
    assert set(hub.tenants) == {"tn_abc"}
