"""Robustesse du miroir côté machine (docs/LOKKAL.md §7.23) : attente croissante après un échec, état visible par canal
(dernier envoi, retard, erreur, prochain essai), file d'envoi persistante (un redémarrage reprend ce qui attend),
resynchronisation complète, et routes locales (`GET /v1/local/mirror`, `POST /v1/local/mirror/resync`,
`GET|PUT /v1/local/mirror/pages…`). Aucun réseau : nokker.ai simulé par httpx.MockTransport."""
from __future__ import annotations

import json

import httpx

from nokker.core.turn import InputCheck, Turn
from nokker.local.mirror import BACKOFF_BASE_S, BACKOFF_CAP_S, MirrorWorker, backoff_s
from nokker.pages.store import PageStore
from nokker.store.pg import MemoryStore
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


class Cloud:
    def __init__(self) -> None:
        self.down = False
        self.calls: list[tuple[str, str]] = []
        self.turns: list[dict] = []
        self.notes: dict[str, dict] = {}
        self.pages: dict[str, str] = {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("coupé", request=req)
        path = req.url.path
        self.calls.append((req.method, path))
        body = json.loads(req.content) if req.content else {}
        if path == "/v1/mirror/turns" and req.method == "POST":
            self.turns.extend(body["turns"])
            return httpx.Response(200, json={"accepted": len(body["turns"])})
        if path == "/v1/mirror/turns":
            return httpx.Response(200, json={"turns": [], "cursor": None})
        if path == "/v1/mirror/notes" and req.method == "GET":
            return httpx.Response(200, json={"notes": [{"project": k.split("/")[0], "name": k.split("/")[1]}
                                                       for k in self.notes]})
        if path == "/v1/mirror/notes":
            for n in body.get("notes") or []:
                self.notes[f"{n['project']}/{n['name']}"] = n
            for f in body.get("forget") or []:
                self.notes.pop(f"{f['project']}/{f['name']}", None)
            return httpx.Response(200, json={"accepted": 1})
        if path == "/v1/mirror/pages":
            return httpx.Response(200, json={"pages": [{"page_id": p, "sha256": s} for p, s in self.pages.items()],
                                             "used": 0, "quota": 10})
        if path.startswith("/v1/mirror/pages/"):
            pid = path.rsplit("/", 1)[1]
            if req.method == "DELETE":
                return httpx.Response(200, json={"deleted": self.pages.pop(pid, None) is not None})
            self.pages[pid] = body["sha256"]
            return httpx.Response(200, json={"stored": True, "used": 1, "quota": 10})
        return httpx.Response(404, json={})


def _turn(thread, i, text):
    return Turn(thread_id=thread, index=i, input=InputCheck(text=text), channel="api", reply=text)


def _worker(cloud, state, clock, **kw):
    store = kw.pop("store", None) or MemoryStore()
    return MirrorWorker(store=store, base_url="https://app.nokker.ai", token=lambda: "mt", state=state,
                        save=lambda: None, client=httpx.Client(transport=httpx.MockTransport(cloud)), clock=clock,
                        **kw), store


def test_backoff_grows_and_caps():
    assert backoff_s(0) == 0 and backoff_s(1) == BACKOFF_BASE_S and backoff_s(2) == 2 * BACKOFF_BASE_S
    assert backoff_s(30) == BACKOFF_CAP_S


def test_failed_channel_waits_then_recovers_and_state_is_visible():
    cloud, clock, state = Cloud(), Clock(), {}
    w, store = _worker(cloud, state, clock)
    with tenant_scope(INTERNAL_TENANT):
        store.append(_turn("th_1", 0, "bonjour"))
    cloud.down = True
    w.tick()
    st = w.status()
    ch = st["channels"]["turns_push"]
    assert ch["failures"] == 1 and ch["last_error"]["kind"] == "ConnectError" and ch["retry_in_s"] == BACKOFF_BASE_S
    assert st["pending"]["turns"] == 1 and st["behind_s"] is None and st["last_sent_at"] is None
    assert "app.nokker.ai" not in json.dumps(st) and "mt" not in json.dumps(ch)  # ni URL ni jeton dans l'état
    cloud.down = False
    n_calls = len(cloud.calls)
    w.tick()  # attente pas échue : aucun essai
    assert len(cloud.calls) == n_calls
    clock.t += BACKOFF_BASE_S
    w.tick()
    st = w.status()
    assert st["channels"]["turns_push"]["failures"] == 0 and st["channels"]["turns_push"]["last_error"] is None
    assert st["pending"]["turns"] == 0 and st["behind_s"] == 0 and st["last_sent_at"] == clock.t
    assert [t["thread_id"] for t in cloud.turns] == ["th_1"]
    # échecs répétés : l'attente double
    cloud.down = True
    with tenant_scope(INTERNAL_TENANT):
        store.append(_turn("th_1", 1, "encore"))
    for _ in range(3):
        clock.t += 10_000
        w.tick()
    assert w.status()["channels"]["turns_push"]["retry_in_s"] == 4 * BACKOFF_BASE_S
    clock.t += 60
    assert w.status()["behind_s"] == round(clock.t - st["last_sent_at"])  # retard : depuis le dernier envoi réussi


def test_queue_survives_a_restart(tmp_path):
    """La file d'envoi est l'état persistant : ce qui attendait au moment de l'arrêt part après le redémarrage."""
    cloud, clock, state = Cloud(), Clock(), {}
    pages = PageStore(tmp_path / "pages")
    p = pages.create("th_1", "a.html", "A", b"<p>a</p>")
    w, store = _worker(cloud, state, clock, pages=pages)
    w.set_page_mirror(p.page_id, True)
    cloud.down = True
    assert w.push_pages_once() == 0
    saved = json.loads(json.dumps(state))  # setup.json relu au démarrage
    cloud.down = False
    w2, _ = _worker(cloud, saved, clock, pages=PageStore(tmp_path / "pages"), store=store)
    assert w2.status()["pending"]["pages"] == 1
    assert w2.push_pages_once() == 1 and cloud.pages == {p.page_id: p.sha256}


def test_resync_resends_missing_and_purges_what_the_machine_no_longer_has(tmp_path):
    from tests.unit.test_handoffs import _user_and_project_notes

    pm, _ = _user_and_project_notes(tmp_path)
    cloud, clock, state = Cloud(), Clock(), {}
    pages = PageStore(tmp_path / "pages")
    p = pages.create("th_1", "a.html", "A", b"<p>a</p>")
    w, store = _worker(cloud, state, clock, notes=pm, pages=pages)
    with tenant_scope(INTERNAL_TENANT):
        store.append(_turn("th_1", 0, "bonjour"))
    w.set_page_mirror(p.page_id, True)
    w.push_once(), w.push_notes_once(), w.push_pages_once()
    # nokker.ai a perdu une partie, et garde des restes que la machine n'a plus
    cloud.notes.clear()
    cloud.notes["u-local/feedback-orpheline-000000"] = {}
    cloud.pages = {"pg_ffffffffffffffff": "x"}
    cloud.turns.clear()
    assert w.push_once() == 0 and w.push_notes_once() == 0 and w.push_pages_once() == 0  # rien ne le voit
    assert w.request_resync() and w.status()["resync"]["requested"]
    w.tick()
    w._wake_all = True
    w.tick()
    assert [t["thread_id"] for t in cloud.turns] == ["th_1"]
    assert "u-local/feedback-orpheline-000000" not in cloud.notes and len(cloud.notes) == 1
    assert cloud.pages == {p.page_id: p.sha256}
    st = w.status()
    assert not st["resync"]["requested"] and st["resync"]["last_at"] == clock.t
    # en pause (copies effacées par le propriétaire), pas de resynchronisation
    state["mirror_paused"] = True
    assert not w.request_resync()


def test_resync_failure_is_retried_with_backoff(tmp_path):
    cloud, clock, state = Cloud(), Clock(), {}
    pages = PageStore(tmp_path / "pages")
    w, _ = _worker(cloud, state, clock, pages=pages)
    w.request_resync()
    cloud.down = True
    w.tick()
    assert w.status()["resync"]["requested"] and w.status()["channels"]["resync"]["failures"] == 1
    cloud.down = False
    clock.t += BACKOFF_BASE_S
    w.tick()
    assert not w.status()["resync"]["requested"]


# ─────────────────────────────── routes locales ───────────────────────────────


def test_local_routes(tmp_path):
    from nokker.local.api import LocalApi

    cloud, clock, state = Cloud(), Clock(), {}
    pages = PageStore(tmp_path / "pages")
    p = pages.create("th_1", "a.html", "A", b"<p>a</p>")
    api = LocalApi.__new__(LocalApi)
    api.mirror = None
    assert api._mirror("GET", "/v1/local/mirror", {}) == (200, {"enabled": False})
    assert api._mirror("POST", "/v1/local/mirror/resync", {})[0] == 404
    w, _ = _worker(cloud, state, clock, pages=pages)
    api.mirror = w
    st, view = api._mirror("GET", "/v1/local/mirror/pages", {})
    assert st == 200 and view["pages"][0]["state"] == "off"
    assert api._mirror("PUT", f"/v1/local/mirror/pages/{p.page_id}", {"mirror": "oui"})[0] == 400
    assert api._mirror("PUT", "/v1/local/mirror/pages/pg_ffffffffffffffff", {"mirror": True})[0] == 404
    st, got = api._mirror("PUT", f"/v1/local/mirror/pages/{p.page_id}", {"mirror": True})
    assert st == 200 and got == {"page_id": p.page_id, "mirror": True}
    assert api._mirror("GET", "/v1/local/mirror/pages", {})[1]["pages"][0]["state"] == "pending"
    assert api._mirror("POST", "/v1/local/mirror/resync", {})[0] == 202
    st, status = api._mirror("GET", "/v1/local/mirror", {})
    assert st == 200 and status["resync"]["requested"] and "channels" in status and "pending" in status
    state["mirror_paused"] = True
    assert api._mirror("POST", "/v1/local/mirror/resync", {})[0] == 409
