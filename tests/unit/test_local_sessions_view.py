"""`GET /v1/local/sessions` enrichi (docs/LOKKAL.md §7.4.1) et alias `resume` de l'ouverture : titre, date du dernier
tour, tour en cours, cartes en attente — en plus de `{thread_id, cwd}`, rien d'enlevé pour les clients actuels."""
from __future__ import annotations

import threading
from datetime import datetime
from http.server import ThreadingHTTPServer

from nokker.approvals.memory import InMemoryApprovals
from nokker.api.events import EventBus
from nokker.api.server import make_handler
from nokker.bubbles.hub import BubbleHub
from nokker.bubbles.memory import InMemoryBubbles
from nokker.local.api import LOCAL_OWNER
from nokker.store.pg import MemoryStore
from nokker.threads.memory import InMemoryThreads
from tests.unit.test_local_api import _api, _card, _req


def _serve(api, kernel, **kw):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: LOCAL_OWNER}, local_api=api, **kw))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def test_session_list_keeps_old_fields_and_adds_defaults_without_a_server(tmp_path):
    api = _api(tmp_path)
    auth = f"Bearer {api.token}"
    tid = api.handle("POST", "/v1/local/sessions", auth, {"cwd": str(tmp_path / "projets" / "app")})[1]["thread_id"]
    row = api.handle("GET", "/v1/local/sessions", auth, {})[1]["sessions"][0]
    assert row["thread_id"] == tid and row["cwd"] == str((tmp_path / "projets" / "app").resolve())
    assert row["title"] is None and row["last_turn_at"] is None
    assert row["working"] is False and row["pending_approvals"] == 0


def test_session_list_has_title_last_turn_working_and_pending_cards(tmp_path):
    from tests.unit.api.test_server import make_kernel

    api = _api(tmp_path)
    approvals = InMemoryApprovals()
    kernel = make_kernel("ok")
    bus = EventBus()
    hub = BubbleHub(threads=InMemoryThreads(), bubbles=InMemoryBubbles(), publish=bus.publish,
                    known_users=frozenset(), spawn=lambda fn: fn())
    kernel.observer, hub.kernel = hub, kernel
    httpd, port = _serve(api, kernel, approvals=approvals, events=bus, hub=hub)
    try:
        cwd = str(tmp_path / "projets" / "app")
        tid = _req(port, "/v1/local/sessions", token=api.token, body={"cwd": cwd})[1]["thread_id"]
        other = _req(port, "/v1/local/sessions", token=api.token, body={"cwd": cwd})[1]["thread_id"]
        rows = {r["thread_id"]: r for r in _req(port, "/v1/local/sessions", token=api.token)[1]["sessions"]}
        assert rows[tid]["title"] is None and rows[tid]["last_turn_at"] is None  # pas encore de tour
        from nokker.core.turn import TurnInput

        kernel.run_turn(TurnInput(thread_id=tid, user_text="corrige le calcul des remises", sender=LOCAL_OWNER))
        _card(approvals, tid, "npm test")
        _card(approvals, f"{tid}/sa-abc", "make")  # carte d'un sous-agent du fil : comptée avec lui
        lock = kernel.turn_locks.get(tid)
        lock.acquire()  # un tour tourne sur le fil
        try:
            rows = {r["thread_id"]: r for r in _req(port, "/v1/local/sessions", token=api.token)[1]["sessions"]}
        finally:
            lock.release()
        row = rows[tid]
        assert set(row) >= {"thread_id", "cwd", "title", "last_turn_at", "working", "pending_approvals"}
        assert row["title"] == "corrige le calcul des remises"
        assert datetime.fromisoformat(row["last_turn_at"]).tzinfo is not None
        assert row["working"] is True and row["pending_approvals"] == 2
        assert rows[other]["working"] is False and rows[other]["pending_approvals"] == 0
        again = {r["thread_id"]: r for r in _req(port, "/v1/local/sessions", token=api.token)[1]["sessions"]}
        assert again[tid]["working"] is False  # le tour fini
    finally:
        httpd.shutdown()


def test_memory_store_dates_the_last_turn():
    from nokker.core.turn import InputCheck, Turn

    s = MemoryStore()
    assert s.last_turn_at("th") is None
    s.append(Turn(thread_id="th", index=0, input=InputCheck(text="a")))
    first = s.last_turn_at("th")
    s.append(Turn(thread_id="th", index=1, input=InputCheck(text="b")))
    assert first is not None and s.last_turn_at("th") >= first and s.last_turn_at("th").tzinfo is not None


def test_open_accepts_resume_as_an_alias_of_thread_id(tmp_path):
    api = _api(tmp_path)
    auth = f"Bearer {api.token}"
    cwd = str(tmp_path / "projets" / "app")
    assert api.handle("POST", "/v1/local/sessions", auth, {"cwd": cwd, "resume": "th_reprise1"})[1]["thread_id"] \
        == "th_reprise1"
    assert api.handle("POST", "/v1/local/sessions", auth, {"cwd": cwd, "thread_id": "th_direct1"})[1]["thread_id"] \
        == "th_direct1"
    # les deux, différents : ambigu, refusé (jamais un fil choisi au hasard)
    status, body = api.handle("POST", "/v1/local/sessions", auth, {"cwd": cwd, "thread_id": "th_a", "resume": "th_b"})
    assert status == 400 and "resume" in body["error"]["message"]
    # les deux, égaux : accepté
    assert api.handle("POST", "/v1/local/sessions", auth, {"cwd": cwd, "thread_id": "th_c", "resume": "th_c"})[0] == 201
    # vide ou non chaîne : un nouveau fil, comme sans le champ
    assert api.handle("POST", "/v1/local/sessions", auth, {"cwd": cwd, "resume": ""})[1]["thread_id"].startswith("th_")
    assert api.handle("POST", "/v1/local/sessions", auth, {"cwd": cwd, "resume": 12})[0] == 400
    sessions = {r["thread_id"] for r in api.handle("GET", "/v1/local/sessions", auth, {})[1]["sessions"]}
    assert {"th_reprise1", "th_direct1", "th_c"} <= sessions and "th_a" not in sessions
