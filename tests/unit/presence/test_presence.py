"""Vue « Qui fait quoi » : statuts dérivés, fuite entre locataires, lecture seule, drapeau éteint, événements."""
from __future__ import annotations

import time
from types import SimpleNamespace

from nokker.api import server
from nokker.api.events import EventBus
from nokker.bubbles.hub import BubbleHub
from nokker.bubbles.memory import InMemoryBubbles
from nokker.code_sessions.store import CodeSession, MemoryCodeStore
from nokker.config import NokkerConfig
from nokker.core.ports import Approval
from nokker.goals.store import Goal
from nokker.presence import http as presence_http
from nokker.presence.service import PresenceService
from nokker.presence.tracker import ActivityTracker
from nokker.tenancy.context import scoped_thread_id, tenant_scope
from nokker.threads.memory import InMemoryThreads
from tests.unit.house.conftest import make_world  # noqa: F401 — fixture `w`
from tests.unit.house.conftest import w  # noqa: F401


def _hub():
    return BubbleHub(threads=InMemoryThreads(), bubbles=InMemoryBubbles(), publish=None, scrub=lambda s: s)


def _card(thread_id, tool="mail_envoyer"):
    return Approval(id="ap1", thread_id=thread_id, turn_index=0, channel="web", tool_name=tool, arguments={},
                    args_sha256="x", strength="standard", rule="r", created_at=time.time(), expires_at=time.time() + 600)


class _Cards:
    def __init__(self, cards):
        self.cards = cards

    def pending(self, user=None):
        return list(self.cards)


class _Goals:
    def __init__(self, goals):
        self.goals = goals

    def current(self, thread_id):
        return next((g for g in self.goals if g.thread_id == thread_id), None)


def _setup(tenant="tn_a", account="us_a"):
    hub = _hub()
    tid = scoped_thread_id(tenant, "th1")
    with tenant_scope(tenant):
        hub.threads.create("Courrier", account, thread_id=tid)
    return hub, tid


def test_statuses_derived_from_existing_state():
    hub, tid = _setup()
    tracker = ActivityTracker()
    svc = PresenceService(hub=hub, tracker=tracker)
    with tenant_scope("tn_a"):
        assert svc.view("us_a")["entries"] == []  # un fil au repos n'est pas listé
        hub.threads.update(tid, working=True, last_snippet="je lis le courrier")
        (e,) = svc.view("us_a")["entries"]
        assert e["status"] == "working" and e["activity"] == {"code": "snippet", "text": "je lis le courrier"}
        tracker.observe("tool.call", {"thread_id": tid, "name": "mail_lire", "_tenant": "tn_a"})
        assert svc.view("us_a")["entries"][0]["activity"] == {"code": "tool", "tool": "mail_lire"}
        tracker.observe("turn.paused", {"thread_id": tid, "_tenant": "tn_a"})
        assert svc.view("us_a")["entries"][0]["status"] == "paused"
        svc.approvals = _Cards([_card(tid)])
        v = svc.view("us_a")
        assert v["entries"][0]["status"] == "awaiting_approval" and v["next_waiting"] == tid
        assert v["entries"][0]["activity"] == {"code": "approval", "tool": "mail_envoyer"} and v["counts"]["awaiting_approval"] == 1
        svc.approvals = None
        svc.goals = _Goals([Goal(id="g1", thread_id=tid, owner="us_a", text="ranger la boîte", status="blocked")])
        e = svc.view("us_a")["entries"][0]
        assert e["status"] == "blocked" and e["goal"] == {"text": "ranger la boîte", "status": "blocked"}


def test_idle_code_session_listed_and_subagent_has_parent():
    hub, tid = _setup()
    sub = tid + "/sa-1"
    with tenant_scope("tn_a"):
        hub.threads.create("enfant", "us_a", thread_id=sub)
        hub.threads.update(sub, working=True)
        store = MemoryCodeStore()
        store.put(CodeSession(id="cs1", tenant_id="tn_a", user_id="us_a", thread_id=tid, root_rel="r", mode="auto",
                              last_used_at=time.time()))
        svc = PresenceService(hub=hub, code=SimpleNamespace(store=store))
        kinds = {e["id"]: e for e in svc.view("us_a")["entries"]}
    assert kinds[tid]["kind"] == "code" and kinds[tid]["status"] == "idle"
    assert kinds[sub]["kind"] == "subagent" and kinds[sub]["parent"] == tid


def test_no_leak_between_tenants():
    hub, tid_a = _setup("tn_a", "us_a")
    tid_b = scoped_thread_id("tn_b", "th9")
    with tenant_scope("tn_b"):
        hub.threads.create("SECRET-B", "us_b", thread_id=tid_b)
        hub.threads.update(tid_b, working=True, last_snippet="CANARI-B")
        store = MemoryCodeStore()
        store.put(CodeSession(id="cs9", tenant_id="tn_b", user_id="us_b", thread_id=tid_b, root_rel="r", mode="auto"))
    with tenant_scope("tn_a"):
        hub.threads.update(tid_a, working=True)
        svc = PresenceService(hub=hub, code=SimpleNamespace(store=store), approvals=_Cards([]),
                              goals=_Goals([Goal(id="g", thread_id=tid_b, owner="us_b", text="CANARI-B")]))
        out = str(svc.view("us_a"))
    assert "CANARI-B" not in out and "SECRET-B" not in out and tid_b not in out


def test_household_member_is_read_only_and_house_agents_listed(w):  # noqa: F811
    tracker = ActivityTracker()
    svc = PresenceService(room=w.room, tracker=tracker)
    with tenant_scope(w.adult.tenant_id):
        v = svc.view(w.adult.account_id)
    assert v["viewer"] == {"household": True, "payer": False, "read_only": True}
    assert {e["id"] for e in v["entries"] if e["kind"] == "house"} >= {"house:intendant", "house:maison"}
    with tenant_scope(w.pay.tenant_id):
        assert svc.view(w.pay.account_id)["viewer"]["read_only"] is False
    with tenant_scope(w.lone.tenant_id):
        assert svc.view(w.lone.account_id)["viewer"] == {"household": False, "payer": False, "read_only": False}
    with tenant_scope(w.child.tenant_id):  # un enfant ne voit pas l'agent réservé aux adultes
        assert "house:courrier" not in {e["id"] for e in svc.view(w.child.account_id)["entries"]}


def test_house_agent_state_follows_events(w):  # noqa: F811
    tracker = ActivityTracker()
    svc = PresenceService(room=w.room, tracker=tracker)
    hh = w.room.member(w.pay.account_id) if False else None  # noqa: F841
    with tenant_scope(w.pay.tenant_id):
        hid = w.room.member(w.pay.account_id).household_id
        tracker.observe("house.agent", {"household_id": hid, "agent": "intendant", "state": "working"})
        e = next(x for x in svc.view(w.pay.account_id)["entries"] if x["id"] == "house:intendant")
        assert e["status"] == "working"
        tracker.observe("house.agent", {"household_id": hid, "agent": "intendant", "state": "idle"})
        e = next(x for x in svc.view(w.pay.account_id)["entries"] if x["id"] == "house:intendant")
        assert e["status"] == "idle"


def test_presence_changed_event_on_transition_only():
    bus = EventBus()
    hub, tid = _setup()
    tr = ActivityTracker(audience_of=hub.thread_audience)
    tr.publish = bus.publish
    bus.observers.append(tr.observe)
    q = bus.subscribe()
    with tenant_scope("tn_a"):
        bus.publish("turn.started", {"thread_id": tid})
        bus.publish("tool.call", {"thread_id": tid, "name": "x"})  # même statut : pas d'événement
        bus.publish("turn.done", {"thread_id": tid})
    got = []
    while not q.empty():
        ev = q.get()
        if ev.event_type == "presence.changed":
            got.append((ev.data["status"], ev.data["_audience"], ev.data["_tenant"]))
    assert got == [("working", ["us_a"], "tn_a"), ("idle", ["us_a"], "tn_a")]


def test_flag_off_no_route_no_observer():
    assert NokkerConfig().who_does_what is False
    bus = EventBus()
    assert server.configure_presence(NokkerConfig(), SimpleNamespace(), events=bus) is None
    assert not presence_http.enabled() and bus.observers == []
    h = SimpleNamespace(_json=lambda *a: (_ for _ in ()).throw(AssertionError()))
    assert presence_http.handle(h, "GET", "/v1/presence", SimpleNamespace(account_id="us_a")) is False


def test_flag_on_route_is_get_only():
    hub, tid = _setup()
    bus = EventBus()
    try:
        svc = server.configure_presence(NokkerConfig(who_does_what=True), SimpleNamespace(), events=bus, hub=hub)
        assert svc is not None and len(bus.observers) == 1
        sent = []
        h = SimpleNamespace(_json=lambda code, body: sent.append((code, body)))
        acc = SimpleNamespace(account_id="us_a")
        with tenant_scope("tn_a"):
            assert presence_http.handle(h, "GET", "/v1/presence", acc) is True
            assert presence_http.handle(h, "POST", "/v1/presence", acc) is True
        assert sent[0][0] == 200 and "entries" in sent[0][1] and sent[1][0] == 405
    finally:
        presence_http.configure(None)


def test_no_french_label_in_the_view(w):  # noqa: F811
    """Le noyau expose des codes et des valeurs brutes : aucune phrase ni libellé de statut écrits en français."""
    hub, tid = _setup()
    with tenant_scope("tn_a"):
        hub.threads.update(tid, working=True)
        svc = PresenceService(hub=hub, approvals=_Cards([_card(tid)]), tracker=ActivityTracker(),
                              goals=_Goals([Goal(id="g", thread_id=tid, owner="us_a", text="x", status="blocked")]))
        v = svc.view("us_a")
    assert "status_label" not in str(v) and "attend" not in str(v) and "bloqué" not in str(v)
    assert v["entries"][0]["activity"] == {"code": "approval", "tool": "mail_envoyer"}
