"""Routes fils/bulles de `api/server.py` (docs/BUBBLES.md §1-§4, §6) — vraies requêtes HTTP en boucle locale."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from nokker.agents.hub import AgentsHub
from nokker.agents.memory import InMemoryAgents
from nokker.api.events import EventBus
from nokker.api.server import make_handler
from nokker.bubbles.hub import BubbleHub
from nokker.bubbles.memory import InMemoryBubbles
from nokker.threads.memory import InMemoryThreads
from tests.unit.api.test_server import _Server, make_kernel, parse_sse

KEYS = {"key-alice": "alice", "key-bruno": "bruno"}


def start(reply="ok", *, with_agents=True):
    bus = EventBus()
    hub = BubbleHub(threads=InMemoryThreads(), bubbles=InMemoryBubbles(), publish=bus.publish,
                    known_users=frozenset(KEYS.values()), spawn=lambda fn: fn())
    agents_hub = AgentsHub(store=InMemoryAgents(), publish=bus.publish) if with_agents else None
    hub.agents_hub = agents_hub
    kernel = make_kernel(reply)
    kernel.observer = hub
    hub.kernel = kernel
    hub.scrub = kernel.invariants.scrub_output
    handler = make_handler(kernel, KEYS, None, approvals=None, events=bus, hub=hub, agents=agents_hub,
                           known_users=frozenset(KEYS.values()))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    return _Server(httpd=httpd, thread=th, port=httpd.server_address[1]), hub, bus


def call(srv, method, path, body=None, key="key-alice", headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    if key:
        h["Authorization"] = f"Bearer {key}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(srv.url(path), data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None)


def chat(srv, text, session=None, header="X-Session-Id", key="key-alice", stream=False):
    h = {header: session} if session else {}
    return call(srv, "POST", "/v1/chat/completions",
                {"model": "hermes-agent", "messages": [{"role": "user", "content": text}], "stream": stream},
                key=key, headers=h)


def test_auth_required():
    srv, _, _ = start()
    try:
        assert call(srv, "GET", "/v1/threads", key=None)[0] == 401
        assert call(srv, "GET", "/v1/threads", key="faux")[0] == 401
    finally:
        srv.stop()


def test_x_session_id_creates_thread_on_the_fly_and_keeps_hermes_header():
    srv, hub, _ = start("2 rendez-vous.")
    try:
        st, body = chat(srv, "Quels rendez-vous ai-je aujourd'hui et demain matin ? Merci beaucoup", "sess-1")
        assert st == 200 and body["choices"][0]["message"]["content"] == "2 rendez-vous."
        st, threads = call(srv, "GET", "/v1/threads")
        assert [t["id"] for t in threads] == ["sess-1"]
        assert threads[0]["title"] == "Quels rendez-vous ai-je aujourd'hui et demain ma"
        assert threads[0]["owner"] == "alice" and threads[0]["working"] is False
        st, _ = chat(srv, "suite", "sess-1", header="X-Hermes-Session-Id")
        st, one = call(srv, "GET", "/v1/threads/sess-1")
        assert [m["role"] for m in one["messages"]] == ["user", "assistant", "user", "assistant"]
        assert one["messages"][-1]["content"] == "2 rendez-vous."
        st, page = call(srv, "GET", "/v1/threads/sess-1?before=1&limit=50")
        assert [m["index"] for m in page["messages"]] == [0, 0]
        # un autre utilisateur ne peut pas écrire dans ce fil
        assert chat(srv, "intrus", "sess-1", key="key-bruno")[0] == 403
        assert call(srv, "GET", "/v1/threads/sess-1", key="key-bruno")[0] == 403
    finally:
        srv.stop()


def test_openai_sse_unchanged_with_hub():
    srv, _, _ = start("Bonjour Alice")
    try:
        body = json.dumps({"model": "hermes-agent", "messages": [{"role": "user", "content": "salut"}],
                           "stream": True}).encode()
        req = urllib.request.Request(srv.url("/v1/chat/completions"), data=body, method="POST",
                                     headers={"Authorization": "Bearer key-alice", "X-Hermes-Session-Id": "h-1",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            lines = [ln.decode() for ln in resp]
        content, usage, done = parse_sse(lines)
        assert content == "Bonjour Alice" and done and usage["prompt_tokens"] == 12
    finally:
        srv.stop()


def test_threads_crud_and_members():
    srv, _, _ = start()
    try:
        st, t = call(srv, "POST", "/v1/threads", {"title": "Infra"})
        assert st == 201 and t["id"].startswith("th_") and t["members"] == ["alice"]
        assert call(srv, "GET", "/v1/threads", key="key-bruno")[1] == []
        st, t2 = call(srv, "PATCH", f"/v1/threads/{t['id']}", {"title": "Infra hote21", "members": ["bruno"]})
        assert st == 200 and t2["title"] == "Infra hote21" and t2["members"] == ["alice", "bruno"]
        assert [x["id"] for x in call(srv, "GET", "/v1/threads", key="key-bruno")[1]] == [t["id"]]
        assert call(srv, "PATCH", f"/v1/threads/{t['id']}", {"members": ["mallory"]})[0] == 400
        assert call(srv, "GET", "/v1/threads/th_000000000000")[0] == 404
    finally:
        srv.stop()


def test_bubble_routes_version_409_transfer_share_close():
    srv, hub, _ = start()
    try:
        a = call(srv, "POST", "/v1/threads", {"title": "Infra"})[1]
        b = call(srv, "POST", "/v1/threads", {"title": "Compta"})[1]
        st, bu = call(srv, "POST", f"/v1/threads/{a['id']}/bubbles",
                      {"kind": "timer", "title": "Pause", "state": {"label": "pause"}})
        assert st == 201 and bu["version"] == 1 and bu["shared_with"] == {"members": [], "threads": []}
        st, bu2 = call(srv, "PATCH", f"/v1/bubbles/{bu['id']}", {"status": "working", "if_version": 1})
        assert st == 200 and bu2["version"] == 2
        st, err = call(srv, "PATCH", f"/v1/bubbles/{bu['id']}", {"title": "x", "if_version": 1})
        assert st == 409 and err["error"]["current_version"] == 2
        assert call(srv, "POST", f"/v1/threads/{a['id']}/bubbles", {"kind": "notify"})[0] == 400
        # partage lecture seule
        st, sh = call(srv, "POST", f"/v1/bubbles/{bu['id']}/share", {"members": ["bruno"]})
        assert st == 200 and sh["shared_with"]["members"] == ["bruno"]
        st, ro = call(srv, "GET", f"/v1/bubbles/{bu['id']}", key="key-bruno")
        assert st == 200 and ro["shared"] is True
        assert call(srv, "PATCH", f"/v1/bubbles/{bu['id']}", {"title": "x"}, key="key-bruno")[0] == 403
        # transfert copy puis move
        st, out = call(srv, "POST", f"/v1/bubbles/{bu['id']}/transfer", {"to_thread": b["id"], "mode": "copy"})
        assert st == 200 and out["new_bubble_id"] != bu["id"] and out["bubble"]["thread_id"] == b["id"]
        assert hub.context_for(b["id"])[0].startswith("[Bulle reçue de « Infra » — timer « Pause »]")
        st, out = call(srv, "POST", f"/v1/bubbles/{bu['id']}/transfer", {"to_thread": b["id"], "mode": "move"})
        assert st == 200 and out["new_bubble_id"] == bu["id"]
        assert call(srv, "GET", f"/v1/threads/{a['id']}/bubbles")[1] == []
        assert len(call(srv, "GET", f"/v1/threads/{b['id']}/bubbles")[1]) == 2
        st, closed = call(srv, "POST", f"/v1/bubbles/{bu['id']}/close", {})
        assert st == 200 and closed["closed_at"] is not None
        assert len(call(srv, "GET", f"/v1/threads/{b['id']}/bubbles?include_closed=1")[1]) == 2
        assert len(call(srv, "GET", f"/v1/threads/{b['id']}/bubbles")[1]) == 1
        assert call(srv, "GET", f"/v1/threads/{b['id']}")[1]["thread"]["bubble_count"] == 1
    finally:
        srv.stop()


def test_session_message_ask_and_notify_action():
    srv, hub, bus = start("Oui, payée.")
    try:
        a = call(srv, "POST", "/v1/threads", {"title": "Infra"})[1]
        b = call(srv, "POST", "/v1/threads", {"title": "Compta"})[1]
        st, out = call(srv, "POST", f"/v1/threads/{b['id']}/messages",
                       {"from_thread": a["id"], "text": "la facture est payée ?", "ask": True})
        assert st == 202 and out["ask"] is True
        assert hub.context_for(a["id"]) == ["[Réponse de la session « Compta »] Oui, payée."]
        notifs = [e.data["bubble"] for e in bus._buffer if e.event_type == "bubble.created"
                  and e.data["bubble"]["kind"] == "notify"]
        assert len(notifs) == 2 and all(n["thread_id"] is None for n in notifs)
        st, res = call(srv, "POST", f"/v1/bubbles/{notifs[0]['id']}/actions/goto", {})
        assert st == 200 and res == {"closed": True, "goto": a["id"]}
        other = call(srv, "POST", "/v1/threads", {"title": "Bruno"}, key="key-bruno")[1]
        st, _ = call(srv, "POST", f"/v1/threads/{other['id']}/messages", {"from_thread": a["id"], "text": "x"})
        assert st == 403
    finally:
        srv.stop()


def test_notifications_route_lists_open_notify_for_recipient_and_survives_a_reload():
    srv, hub, _ = start("Oui, payée.")
    try:
        a = call(srv, "POST", "/v1/threads", {"title": "Infra"})[1]
        b = call(srv, "POST", "/v1/threads", {"title": "Compta"})[1]
        assert call(srv, "GET", "/v1/notifications")[1] == []
        call(srv, "POST", f"/v1/threads/{b['id']}/messages", {"from_thread": a["id"], "text": "x"})
        st, notifs = call(srv, "GET", "/v1/notifications")
        assert st == 200 and len(notifs) == 1 and notifs[0]["kind"] == "notify" and notifs[0]["thread_id"] is None
        # un "rechargement de page" (nouvel appel) retrouve la même bulle, pas perdue
        assert call(srv, "GET", "/v1/notifications")[1][0]["id"] == notifs[0]["id"]
        assert call(srv, "GET", "/v1/notifications", key="key-bruno")[1] == []  # pas destinataire
        assert call(srv, "GET", "/v1/notifications", key=None)[0] == 401
        call(srv, "POST", f"/v1/bubbles/{notifs[0]['id']}/actions/goto", {})
        assert call(srv, "GET", "/v1/notifications")[1] == []  # fermée -> plus listée
    finally:
        srv.stop()


def test_users_route_lists_known_users_with_name_and_initials():
    srv, _, _ = start()
    try:
        st, users = call(srv, "GET", "/v1/users")
        assert st == 200
        assert {"id": "alice", "name": "Alice", "initials": "AL"} in users
        assert {"id": "bruno", "name": "Bruno", "initials": "BR"} in users
        assert call(srv, "GET", "/v1/users", key=None)[0] == 401
    finally:
        srv.stop()


def test_agents_routes_crud_minimal_and_agent_created_event():
    srv, _, bus = start()
    try:
        assert call(srv, "GET", "/v1/agents")[1] == []
        st, a = call(srv, "POST", "/v1/agents", {"name": "Veille", "role": "scout", "model": "ship",
                                                  "tools": ["web_search"]})
        assert st == 201 and a["id"].startswith("ag_") and a["status"] == "armed"
        assert [x["id"] for x in call(srv, "GET", "/v1/agents")[1]] == [a["id"]]
        assert call(srv, "POST", "/v1/agents", {"role": "scout"})[0] == 400  # name manquant
        created = [e.data["agent"] for e in bus._buffer if e.event_type == "agent.created"]
        assert created == [a]
        assert call(srv, "GET", "/v1/agents", key=None)[0] == 401
    finally:
        srv.stop()


def test_agents_routes_absent_when_disabled():
    srv, _, _ = start(with_agents=False)
    try:
        assert call(srv, "GET", "/v1/agents")[0] == 404
        assert call(srv, "POST", "/v1/agents", {"name": "x", "role": "y"})[0] == 404
    finally:
        srv.stop()


def test_agent_draft_tool_and_activation_end_to_end():
    srv, hub, bus = start("D'accord.")
    try:
        t = call(srv, "POST", "/v1/threads", {"title": "Crew"})[1]
        bu = hub.bubbles.create(thread_id=t["id"], kind="agent_draft", title="Veille",
                                state={"name": "Veille", "role": "scout", "model": "ship", "tools": []},
                                actions=[{"id": "activate", "label": "Activer", "primary": True}])
        st, out = call(srv, "POST", f"/v1/bubbles/{bu.id}/actions/activate", {})
        assert st == 200 and out["activated"] is True and out["agent"]["name"] == "Veille"
        assert hub.bubbles.get(bu.id).closed_at is not None
        created = [e.data["agent"] for e in bus._buffer if e.event_type == "agent.created"]
        assert created == [out["agent"]]
    finally:
        srv.stop()


def _read_events(srv, key, stop_after_s=1.5, last_event_id=None):
    h = {"Authorization": f"Bearer {key}"}
    if last_event_id is not None:
        h["Last-Event-ID"] = str(last_event_id)
    req = urllib.request.Request(srv.url("/v1/events"), headers=h)
    resp = urllib.request.urlopen(req, timeout=stop_after_s)
    out = []
    try:
        while True:
            line = resp.readline().decode()
            if line.startswith("event:"):
                out.append(line.split(":", 1)[1].strip())
            elif line.startswith("data:"):
                data = json.loads(line[5:])
                assert "_audience" not in data
    except Exception:  # noqa: BLE001 — fin de lecture au délai
        pass
    return out


def test_events_filtered_by_membership():
    srv, _, bus = start("ok")
    try:
        chat(srv, "bonjour", "fil-alice")
        chat(srv, "bonjour", "fil-bruno", key="key-bruno")
        alice = _read_events(srv, "key-alice", last_event_id=0)
        bruno = _read_events(srv, "key-bruno", last_event_id=0)
        n_alice = sum(1 for e in bus._buffer if "alice" in e.data.get("_audience", []))
        assert len(alice) == n_alice and "turn.started" in alice and "turn.done" in alice
        assert len(bruno) == len(bus._buffer) - n_alice
        assert {"turn.started", "turn.delta", "turn.done", "thread.updated"} <= set(bruno)
    finally:
        srv.stop()


def test_events_live_stream_receives_turn_events():
    srv, _, _ = start("ok")
    try:
        got = []

        def reader():
            got.extend(_read_events(srv, "key-alice", stop_after_s=2.0))

        t = threading.Thread(target=reader)
        t.start()
        time.sleep(0.3)
        chat(srv, "bonjour", "fil-live")
        t.join(timeout=5)
        assert "turn.started" in got and "turn.done" in got
    finally:
        srv.stop()


def test_routes_absent_without_hub():
    kernel = make_kernel()
    handler = make_handler(kernel, KEYS, None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    srv = _Server(httpd=httpd, thread=th, port=httpd.server_address[1])
    try:
        assert call(srv, "GET", "/v1/threads")[0] == 404
        assert call(srv, "PATCH", "/v1/bubbles/bu_x", {})[0] == 404
        assert call(srv, "GET", "/v1/notifications")[0] == 404
        assert call(srv, "GET", "/v1/users") == (200, [])  # indépendant de hub, known_users vide ici
        assert chat(srv, "salut", "s-1")[0] == 200
    finally:
        srv.stop()
