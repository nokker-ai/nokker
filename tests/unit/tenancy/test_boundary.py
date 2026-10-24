"""Frontière HTTP multi-locataire (docs/TENANCY.md §2-§3, §5) — vraies requêtes en boucle locale, stores en
mémoire, `MemoryTenancy`. Deux testeurs A et B (sessions d'appareil `us_…`) et l'interne (clé d'API de Alice) :
B ne voit rien de A ni de l'interne, et inversement ; un objet d'un autre locataire répond 404."""
from __future__ import annotations

import dataclasses
import json
import socket
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from typing import Any

import pytest

from nokker.agents.hub import AgentsHub
from nokker.agents.memory import InMemoryAgents
from nokker.api.events import EventBus
from nokker.api.server import make_handler
from nokker.approvals.memory import InMemoryApprovals
from nokker.bubbles.hub import BubbleHub
from nokker.bubbles.memory import InMemoryBubbles
from nokker.tenancy.context import INTERNAL_TENANT, current_tenant_or_none
from nokker.tenancy.store import Account, MemoryTenancy
from nokker.threads.memory import InMemoryThreads
from tests.unit.api.test_approvals_routes import make_kernel_with_approvals
from tests.unit.api.test_server import _Server
from tests.unit.approvals.conftest import gen_keypair, sign_p1363, telegram_init_data

KEYS = {"key-alice": "alice"}
BOT = "123456:FAKE-BOT-TOKEN"
INTERNAL_USERS = frozenset({"alice", "bruno"})


class RecordingMemory:
    """Rappel mémoire qui note le locataire vu DANS le fil du pool de rappel."""

    def __init__(self) -> None:
        self.seen: list[str | None] = []

    def recall(self, q, k=5):
        self.seen.append(current_tenant_or_none())
        return []

    def propose(self, t, f):
        pass


@dataclass
class World:
    srv: _Server
    hub: BubbleHub
    approvals: InMemoryApprovals
    bus: EventBus
    kernel: Any
    tenancy: MemoryTenancy
    a: Account
    b: Account
    tokens: dict[str, str] = field(default_factory=dict)
    devices: dict[str, tuple[str, Any]] = field(default_factory=dict)

    def call(self, method, path, body=None, who="alice", headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        token = KEYS_BY_WHO.get(who) or self.tokens.get(who) or who
        if token:
            h["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.srv.url(path), data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, (json.loads(raw) if raw else None)

    def chat(self, text, session, who="alice"):
        return self.call("POST", "/v1/chat/completions",
                         {"model": "nokker", "messages": [{"role": "user", "content": text}]}, who=who,
                         headers={"X-Session-Id": session})


KEYS_BY_WHO = {"alice": "key-alice"}


def _session_for(approvals, user):
    priv, spki = gen_keypair()
    d = approvals.enroll_device(user=user, name=f"appareil {user}", public_key_spki=spki)
    approvals.confirm_device_cli(d.code)
    ch = approvals.login_challenge(d.id)
    s = approvals.create_session(d.id, ch.canonical(), sign_p1363(priv, f"{ch.canonical()}.login".encode()))
    return s.token, d.id, priv


@pytest.fixture
def w():
    tenancy = MemoryTenancy()
    a = tenancy.create_account("a@example.org")
    b = tenancy.create_account("b@example.org")
    bus = EventBus()
    approvals = InMemoryApprovals(on_event=bus.publish)
    hub = BubbleHub(threads=InMemoryThreads(), bubbles=InMemoryBubbles(), publish=bus.publish,
                    known_users=INTERNAL_USERS, spawn=lambda fn: fn())
    agents = AgentsHub(store=InMemoryAgents(), publish=bus.publish)
    hub.agents_hub = agents
    kernel, _ = make_kernel_with_approvals(approvals)
    kernel.memory = RecordingMemory()
    kernel.observer = hub
    hub.kernel = kernel
    hub.scrub = kernel.invariants.scrub_output
    handler = make_handler(kernel, KEYS, None, approvals=approvals, events=bus, hub=hub, agents=agents,
                           known_users=INTERNAL_USERS, tenancy=tenancy)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    world = World(srv=_Server(httpd=httpd, thread=th, port=httpd.server_address[1]), hub=hub, approvals=approvals,
                  bus=bus, kernel=kernel, tenancy=tenancy, a=a, b=b)
    for name, acc in (("A", a), ("B", b)):
        token, dev_id, priv = _session_for(approvals, acc.account_id)
        world.tokens[name] = token
        world.devices[name] = (dev_id, priv)
    try:
        yield world
    finally:
        world.srv.stop()


def _thread_ids(w, who):
    st, body = w.call("GET", "/v1/threads", who=who)
    assert st == 200
    return sorted(t["id"] for t in body)


# ─────────────────────────── fils ───────────────────────────


def test_same_session_id_gives_distinct_threads(w):
    for who in ("A", "B", "alice"):
        assert w.chat(f"bonjour de {who}", "s-1", who=who)[0] == 200
    ta, tb = f"{w.a.tenant_id}:s-1", f"{w.b.tenant_id}:s-1"
    assert _thread_ids(w, "A") == [ta]
    assert _thread_ids(w, "B") == [tb]
    assert _thread_ids(w, "alice") == ["s-1"]  # l'interne garde ses identifiants (données du canari)
    store = w.kernel.store
    assert [t.input.text for t in store.history(ta)] == ["bonjour de A"]
    assert [t.input.text for t in store.history(tb)] == ["bonjour de B"]
    assert [t.input.text for t in store.history("s-1")] == ["bonjour de alice"]
    assert {t.sender for t in store.history(ta)} == {w.a.account_id}


def test_other_tenant_thread_is_404_never_403(w):
    w.chat("secret de A", "s-1", who="A")
    ta = f"{w.a.tenant_id}:s-1"
    for who in ("B", "alice"):
        assert w.call("GET", f"/v1/threads/{ta}", who=who)[0] == 404
        assert w.call("GET", f"/v1/threads/{ta}/bubbles", who=who)[0] == 404
        assert w.call("PATCH", f"/v1/threads/{ta}", {"title": "x"}, who=who)[0] == 404
        assert w.call("DELETE", f"/v1/threads/{ta}", who=who)[0] == 404
        assert w.call("POST", f"/v1/threads/{ta}/bubbles", {"kind": "monitor", "title": "x"}, who=who)[0] == 404
    # URL encodée : même résultat.
    assert w.call("GET", f"/v1/threads/{ta.replace(':', '%3A')}", who="B")[0] == 404
    # L'interne qui vise le fil d'un testeur par X-Session-Id : 404, rien n'est écrit chez A.
    assert w.chat("intrus", ta, who="alice")[0] == 404
    # B qui rejoue l'identifiant de A écrit dans SON espace, pas dans le fil de A.
    assert w.chat("intrus", ta, who="B")[0] == 200
    assert [t.input.text for t in w.kernel.store.history(ta)] == ["secret de A"]
    st, one = w.call("GET", f"/v1/threads/{ta}", who="A")
    assert st == 200 and [m["content"] for m in one["messages"] if m["role"] == "user"] == ["secret de A"]


def test_created_thread_is_namespaced_and_listed_only_in_its_tenant(w):
    st, t = w.call("POST", "/v1/threads", {"title": "Projet A"}, who="A")
    assert st == 201 and t["id"].startswith(f"{w.a.tenant_id}:th_")
    st, t_alice = w.call("POST", "/v1/threads", {"title": "Infra"})
    assert st == 201 and t_alice["id"].startswith("th_")
    assert _thread_ids(w, "A") == [t["id"]]
    assert _thread_ids(w, "B") == []
    assert _thread_ids(w, "alice") == [t_alice["id"]]


def test_members_and_sharing_never_cross_tenants(w):
    _, ta = w.call("POST", "/v1/threads", {"title": "A"}, who="A")
    assert w.call("PATCH", f"/v1/threads/{ta['id']}", {"members": ["alice"]}, who="A")[0] == 400
    assert w.call("PATCH", f"/v1/threads/{ta['id']}", {"members": [w.b.account_id]}, who="A")[0] == 400
    _, tn = w.call("POST", "/v1/threads", {"title": "N"})
    assert w.call("PATCH", f"/v1/threads/{tn['id']}", {"members": [w.a.account_id]})[0] == 400
    assert w.call("PATCH", f"/v1/threads/{tn['id']}", {"members": ["bruno"]})[0] == 200
    _, bu = w.call("POST", f"/v1/threads/{ta['id']}/bubbles", {"kind": "monitor", "title": "n"}, who="A")
    assert w.call("POST", f"/v1/bubbles/{bu['id']}/share", {"members": [w.b.account_id]}, who="A")[0] == 400
    assert w.call("POST", f"/v1/bubbles/{bu['id']}/share", {"threads": [tn["id"]]}, who="A")[0] == 404
    assert w.call("POST", f"/v1/bubbles/{bu['id']}/transfer", {"to_thread": tn["id"], "mode": "copy"},
                  who="A")[0] == 404
    st, users = w.call("GET", "/v1/users", who="A")
    assert st == 200 and [u["id"] for u in users] == [w.a.account_id]
    assert sorted(u["id"] for u in w.call("GET", "/v1/users")[1]) == ["alice", "bruno"]


# ─────────────────────────── bulles et notifications ───────────────────────────


def test_other_tenant_bubble_and_notification_are_404(w):
    _, t1 = w.call("POST", "/v1/threads", {"title": "Un"}, who="A")
    _, t2 = w.call("POST", "/v1/threads", {"title": "Deux"}, who="A")
    st, bu = w.call("POST", f"/v1/threads/{t1['id']}/bubbles", {"kind": "monitor", "title": "privé"}, who="A")
    assert st == 201
    st, _ = w.call("POST", f"/v1/threads/{t2['id']}/messages", {"from_thread": t1["id"], "text": "coucou"},
                   who="A")
    assert st == 200
    st, notifs = w.call("GET", "/v1/notifications", who="A")
    assert st == 200 and len(notifs) == 1
    notif = notifs[0]["id"]
    for who in ("B", "alice"):
        assert w.call("GET", "/v1/notifications", who=who)[1] == []
        for bid in (bu["id"], notif):
            assert w.call("GET", f"/v1/bubbles/{bid}", who=who)[0] == 404
            assert w.call("PATCH", f"/v1/bubbles/{bid}", {"title": "x"}, who=who)[0] == 404
            assert w.call("POST", f"/v1/bubbles/{bid}/close", {}, who=who)[0] == 404
            assert w.call("POST", f"/v1/bubbles/{bid}/actions/goto", {}, who=who)[0] == 404
    assert w.call("GET", f"/v1/bubbles/{bu['id']}", who="A")[0] == 200


# ─────────────────────────── approbations et appareils ───────────────────────────


def test_approvals_listed_and_reachable_only_in_their_tenant(w):
    w.chat("le disque est plein", "s-1", who="A")
    w.chat("le disque est plein", "s-1", who="alice")
    st, cards_a = w.call("GET", "/v1/approvals", who="A")
    assert st == 200 and [c["thread_id"] for c in cards_a] == [f"{w.a.tenant_id}:s-1"]
    assert [c["thread_id"] for c in w.call("GET", "/v1/approvals")[1]] == ["s-1"]
    assert w.call("GET", "/v1/approvals", who="B")[1] == []
    card = cards_a[0]["id"]
    for who in ("B", "alice"):
        assert w.call("GET", f"/v1/approvals/{card}", who=who)[0] == 404
        assert w.call("POST", f"/v1/approvals/{card}/challenge", {}, who=who)[0] == 404
        assert w.call("POST", f"/v1/approvals/{card}/decision", {"decision": "reject"}, who=who)[0] == 404
    assert w.call("GET", f"/v1/approvals/{card}", who="A")[0] == 200


def test_device_of_b_cannot_decide_card_of_a(w):
    w.chat("le disque est plein", "s-1", who="A")
    card = w.call("GET", "/v1/approvals", who="A")[1][0]["id"]
    st, ch = w.call("POST", f"/v1/approvals/{card}/challenge", {}, who="A")
    assert st == 200
    # Carte `strong` : appareil de B avec preuve complète (signature + initData Telegram), pour que seul le
    # contrôle de locataire du décideur la refuse.
    w.approvals.telegram_bot_token = BOT
    priv_b, spki = gen_keypair()
    dev_b = w.approvals.enroll_device(user=w.b.account_id, name="Mini App B", public_key_spki=spki,
                                      telegram_user_id="4242")
    w.approvals.confirm_device_cli(dev_b.code)
    sig = sign_p1363(priv_b, f"{ch['challenge']}.reject".encode())
    st, err = w.call("POST", f"/v1/approvals/{card}/decision",
                     {"decision": "reject", "device_id": dev_b.id, "challenge": ch["challenge"], "signature": sig,
                      "init_data": telegram_init_data(BOT, 4242)}, who="A")
    assert st == 403 and "non autorisé" in err["error"]["message"]
    assert w.call("GET", f"/v1/approvals/{card}", who="A")[1]["status"] == "pending"


def test_devices_listed_and_deleted_only_in_their_tenant(w):
    dev_a, _ = w.devices["A"]
    st, devs = w.call("GET", "/v1/devices", who="B")
    assert st == 200 and [d["id"] for d in devs] == [w.devices["B"][0]]
    assert w.call("DELETE", f"/v1/devices/{dev_a}", who="B")[0] == 404
    assert w.call("DELETE", f"/v1/devices/{dev_a}", who="alice")[0] == 404
    assert w.approvals.get_device(dev_a) is not None


def test_tester_device_opens_session_in_its_tenant(w):
    priv, spki = gen_keypair()
    d = w.approvals.enroll_device(user=w.a.account_id, name="2e appareil", public_key_spki=spki)
    w.approvals.confirm_device_cli(d.code)
    st, ch = w.call("POST", f"/v1/devices/{d.id}/session-challenge", {}, who="")
    assert st == 200
    sig = sign_p1363(priv, f"{ch['challenge']}.login".encode())
    st, sess = w.call("POST", "/v1/sessions", {"device_id": d.id, "challenge": ch["challenge"], "signature": sig},
                      who="")
    assert st == 200 and sess["user"] == w.a.account_id
    w.tokens["A2"] = sess["token"]
    assert w.chat("depuis le 2e appareil", "s-9", who="A2")[0] == 200
    assert _thread_ids(w, "A") == [f"{w.a.tenant_id}:s-9"]


# ─────────────────────────── agents ───────────────────────────


def test_agents_listed_only_in_their_tenant(w):
    st, ag = w.call("POST", "/v1/agents", {"name": "Veilleur", "role": "veille"}, who="A")
    assert st == 201
    assert [x["id"] for x in w.call("GET", "/v1/agents", who="A")[1]] == [ag["id"]]
    assert w.call("GET", "/v1/agents", who="B")[1] == []
    assert w.call("GET", "/v1/agents")[1] == []


# ─────────────────────────── expéditeur inconnu ───────────────────────────


def test_unknown_or_suspended_sender_is_403(w):
    # Appareil d'un utilisateur hors config et hors comptes : session valide, mais aucun locataire.
    token, _, _ = _session_for(w.approvals, "eve")
    w.tokens["eve"] = token
    assert w.chat("salut", "s-1", who="eve")[0] == 403
    assert w.call("GET", "/v1/threads", who="eve")[0] == 403
    assert w.call("GET", "/v1/approvals", who="eve")[0] == 403
    assert w.call("GET", "/v1/agents", who="eve")[0] == 403
    # Compte suspendu après l'ouverture de sa session : plus rien.
    w.tenancy.accounts[w.b.account_id] = dataclasses.replace(w.b, status="suspended")
    assert w.chat("salut", "s-1", who="B")[0] == 403
    assert w.call("GET", "/v1/threads", who="B")[0] == 403
    assert w.call("GET", "/v1/notifications", who="B")[0] == 403
    assert w.kernel.store.history(f"{w.b.tenant_id}:s-1") == []


# ─────────────────────────── contexte propagé ───────────────────────────


def test_tenant_reaches_recall_pool_and_stream_worker(w):
    w.chat("quel temps fait-il", "s-1", who="A")
    req = urllib.request.Request(
        w.srv.url("/v1/chat/completions"), method="POST",
        data=json.dumps({"model": "nokker", "stream": True,
                         "messages": [{"role": "user", "content": "et demain"}]}).encode(),
        headers={"Authorization": f"Bearer {w.tokens['B']}", "X-Session-Id": "s-1",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode()
    assert "[DONE]" in body
    w.chat("et chez nous", "s-2")
    assert w.kernel.memory.seen == [w.a.tenant_id, w.b.tenant_id, INTERNAL_TENANT]
    assert [t.input.text for t in w.kernel.store.history(f"{w.b.tenant_id}:s-1")] == ["et demain"]


def _drain_events(w, who):
    token = KEYS_BY_WHO.get(who) or w.tokens[who]
    req = urllib.request.Request(w.srv.url("/v1/events"),
                                 headers={"Authorization": f"Bearer {token}", "Last-Event-ID": "0"})
    resp = urllib.request.urlopen(req, timeout=1.5)
    chunks = []
    try:
        while True:
            line = resp.readline().decode("utf-8", errors="replace")
            if not line:
                break
            chunks.append(line)
    except (TimeoutError, socket.timeout):
        pass
    finally:
        resp.close()
    return "".join(chunks)


def test_events_never_leave_their_tenant(w):
    w.chat("le disque est plein", "s-1", who="A")
    w.chat("le disque est plein", "s-1", who="alice")
    seen_a, seen_b, seen_n = _drain_events(w, "A"), _drain_events(w, "B"), _drain_events(w, "alice")
    ta = f"{w.a.tenant_id}:s-1"
    assert "approval.requested" in seen_a and ta in seen_a
    assert "approval.requested" in seen_n and ta not in seen_n
    assert ta not in seen_b and "approval.requested" not in seen_b and "event:" not in seen_b
