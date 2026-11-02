"""Routes `/v1/threads/{id}/goal` et `metadata.cli_control` de /v1/chat/completions (docs/OUTCOMES-GOAL.md) — vraies
requêtes HTTP en boucle locale, stores en mémoire, faux modèle et faux correcteur."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from nokker.api.events import EventBus
from nokker.api.server import make_handler
from nokker.bubbles.hub import BubbleHub
from nokker.bubbles.memory import InMemoryBubbles
from nokker.goals.service import CHECK_NO_GRADE, GoalService
from nokker.goals.store import MemoryGoals
from nokker.qa.outcomes import OutcomeGrader
from nokker.threads.memory import InMemoryThreads
from tests.unit.api.test_server import _Server, make_kernel

KEYS = {"key-own": "owner1", "key-out": "outsider1"}


class Transport:
    def __init__(self):
        self.bodies = []

    def post(self, body):
        self.bodies.append(body)
        return {"choices": [{"message": {"content": "C1: 2 | ok"}}], "model": "correcteur-x", "usage": {}}


def start(goals_on: bool):
    bus = EventBus()
    users = frozenset(KEYS.values())
    hub = BubbleHub(threads=InMemoryThreads(), bubbles=InMemoryBubbles(), publish=bus.publish, known_users=users,
                    spawn=lambda fn: fn())
    kernel = make_kernel("réponse du modèle")
    kernel.observer = hub
    hub.kernel = kernel
    if goals_on:
        svc = GoalService(store=MemoryGoals(), grader=OutcomeGrader(transport=Transport()), outcomes_enabled=True,
                          goals_enabled=True, submit=lambda *a: None)
        svc.kernel = kernel
        kernel.goals = svc
    handler = make_handler(kernel, KEYS, None, approvals=None, events=bus, hub=hub, known_users=users)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    return _Server(httpd=httpd, thread=th, port=httpd.server_address[1]), kernel


def call(srv, method, path, body=None, key="key-own", headers=None):
    h = {"Content-Type": "application/json", "Authorization": f"Bearer {key}", **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(srv.url(path), data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, json.loads(raw) if raw else None


def chat(srv, tid, text, control=None):
    body = {"model": "nokker", "messages": [{"role": "user", "content": text}]}
    if control:
        body["metadata"] = {"cli_control": control}
    return call(srv, "POST", "/v1/chat/completions", body, headers={"X-Session-Id": tid})


def test_goal_routes_and_cli_control():
    srv, kernel = start(True)
    try:
        st, t = call(srv, "POST", "/v1/threads", {"title": "but"})
        tid = t["id"]
        st, out = call(srv, "POST", f"/v1/threads/{tid}/goal", {"text": "le test passe", "driver": "client",
                                                                 "criteria": ["le test passe"], "budget": {"tokens": 1000}})
        assert st == 201 and out["goal"]["status"] == "active" and out["goal"]["rubric"][0]["text"] == "le test passe"
        assert out["goal"]["budget"]["tokens"] == 1000
        assert call(srv, "GET", f"/v1/threads/{tid}/goal", key="key-out")[0] in (403, 404)
        assert call(srv, "POST", f"/v1/threads/{tid}/goal", {"criteria": []})[0] == 400
        # metadata.cli_control = goal_check : réponse du correcteur, aucun appel modèle
        st, out = chat(srv, tid, "[Vérification du but]", control="goal_check")
        assert st == 200 and out["choices"][0]["message"]["content"] == CHECK_NO_GRADE
        st, out = chat(srv, tid, "corrige")  # tour tapé : modèle, puis note
        assert out["choices"][0]["message"]["content"] == "réponse du modèle"
        st, out = call(srv, "GET", f"/v1/threads/{tid}/goal")
        assert out["goal"]["status"] == "done" and out["goal"]["grades"][0]["grade"]["passed"] is True
        assert call(srv, "POST", f"/v1/threads/{tid}/goal/stop")[0] == 409  # plus rien de vivant
    finally:
        srv.stop()


def test_flags_off_no_route_and_metadata_ignored():
    srv, kernel = start(False)
    try:
        st, t = call(srv, "POST", "/v1/threads", {"title": "but"})
        assert call(srv, "GET", f"/v1/threads/{t['id']}/goal")[0] == 404
        st, out = chat(srv, t["id"], "[Vérification du but]", control="goal_check")
        assert out["choices"][0]["message"]["content"] == "réponse du modèle"
    finally:
        srv.stop()
