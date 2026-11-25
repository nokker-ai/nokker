"""`GET /v1/threads/{fil}` (et bulles, export) : un fil CONNU sans message (session de code ouverte avant son premier
tour) rend 200 vide ; un fil inconnu ou d'un autre locataire, le même 404 ; la lecture n'écrit rien."""
from __future__ import annotations

import pytest

import threading

from nokker.api.events import EventBus
from nokker.api.server import make_handler
from nokker.bubbles.hub import BubbleHub
from nokker.bubbles.memory import InMemoryBubbles
from http.server import ThreadingHTTPServer

from nokker.core.turn import TurnInput
from nokker.local.access_sessions import AccessSessions
from nokker.local.api import LOCAL_OWNER
from nokker.tenancy.context import tenant_scope
from nokker.threads.memory import InMemoryThreads
from nokker.threads.types import ThreadNotFound
from tests.unit.api.test_server import make_kernel
from tests.unit.test_local_api import _api, _req

# Autres utilisateurs authentifiés (clés statiques) : un autre utilisateur du locataire interne et un membre du foyer
# (un compte `us_…` est un autre locataire : 404 par `_tenant_ok`, couvert plus bas). Les portées `owner`, `present` et
# `kiosk` sont de VRAIES sessions d'accès (`AccessSessions.mint` puis `exchange`, docs/KIOSK.md §3).
OTHERS = ("alice", "membre_foyer")
SCOPES = ("owner", "present", "kiosk")


class _World:
    def __init__(self, tmp_path):
        self.api = _api(tmp_path)
        self.kernel = make_kernel("ok")
        self.hub = BubbleHub(threads=InMemoryThreads(), bubbles=InMemoryBubbles(), publish=EventBus().publish,
                             known_users=frozenset(), spawn=lambda fn: fn())
        self.kernel.observer, self.hub.kernel = self.hub, self.kernel
        self.api.access_sessions = AccessSessions(tmp_path / "access-sessions.json")
        self.tokens = {}
        for scope in SCOPES:
            self.tokens[scope] = self.api.access_sessions.exchange(self.api.access_sessions.mint(scope)["code"])["token"]
        keys = {self.api.token: LOCAL_OWNER, **{f"k-{who}": who for who in OTHERS}}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            self.kernel, keys, local_api=self.api, hub=self.hub, known_users=frozenset(OTHERS)))
        self.port = self.httpd.server_address[1]
        self.api.allowed_hosts = frozenset({f"127.0.0.1:{self.port}"})
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.cwd = str(tmp_path / "projets" / "app")

    def get(self, path, who=None):
        if who is None:
            token = self.api.token
        else:
            token = self.tokens.get(who) or f"k-{who}"
        return _req(self.port, path, token=token)

    def open_session(self):
        return _req(self.port, "/v1/local/sessions", token=self.api.token, body={"cwd": self.cwd})[1]["thread_id"]

    def rows(self):
        return (len(self.hub.threads.list_for(LOCAL_OWNER)), len(self.kernel.store.history("x")),
                len(self.hub.bubbles.list_thread("x")))


@pytest.fixture
def w(tmp_path):
    world = _World(tmp_path)
    try:
        yield world
    finally:
        world.httpd.shutdown()


def test_known_session_without_message_is_200_and_empty(w):
    tid = w.open_session()
    before = (w.hub.threads.get(tid), list(w.kernel.store.history(tid)), len(w.hub.threads.list_for(LOCAL_OWNER)))
    status, body = w.get(f"/v1/threads/{tid}")
    assert status == 200
    assert body["messages"] == [] and body["bubbles"] == []
    assert body["thread"]["id"] == tid
    assert set(body["thread"]) >= {"id", "title", "owner", "members", "created_at", "updated_at", "working",
                                   "unread", "bubble_count", "last_snippet"}
    assert w.get(f"/v1/threads/{tid}/bubbles") == (200, [])
    assert w.get(f"/v1/threads/{tid}/export?format=json")[0] == 200
    # lecture pure : ni ligne de fil, ni tour, ni fil listé de plus
    after = (w.hub.threads.get(tid), list(w.kernel.store.history(tid)), len(w.hub.threads.list_for(LOCAL_OWNER)))
    assert before == after == (None, [], 0)


def test_unknown_thread_is_404_and_same_body_as_foreign(w):
    st1, unknown = w.get("/v1/threads/th_inconnu")
    st2, foreign = w.get("/v1/threads/autre-locataire:th_inconnu")
    assert st1 == st2 == 404
    assert set(unknown) == set(foreign) == {"error"}
    assert w.get("/v1/threads/th_inconnu/bubbles")[0] == 404
    assert w.get("/v1/threads/th_inconnu/export?format=json")[0] == 404


def test_foreign_tenant_thread_is_not_found_even_if_known(w):
    w.hub.session_known = lambda tid, user: True  # le noyau le « connaît » : le locataire décide quand même
    with tenant_scope("tn_testeur"):
        with pytest.raises(ThreadNotFound) as foreign:  # fil de la machine (interne) lu par un testeur
            w.hub.read_thread("th_x", None)
        with pytest.raises(ThreadNotFound) as own:  # fil du testeur lui-même : pas de lecture synthétique
            w.hub.read_thread("tn_testeur:th_x", None)
        w.hub.session_known = lambda tid, user: False
        with pytest.raises(ThreadNotFound) as unknown:
            w.hub.read_thread("th_x", None)
    assert type(foreign.value) is type(own.value) is type(unknown.value)
    assert str(foreign.value) == str(unknown.value) == "th_x"


def test_non_empty_thread_is_unchanged(w):
    tid = w.open_session()
    w.kernel.run_turn(TurnInput(thread_id=tid, user_text="bonjour", sender=LOCAL_OWNER))
    status, body = w.get(f"/v1/threads/{tid}")
    assert status == 200
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert body["messages"][0]["content"] == "bonjour"


def test_stored_turn_without_hub_row_is_known(w):
    w.kernel.run_turn(TurnInput(thread_id="th_range", user_text="salut", sender=LOCAL_OWNER))
    w.hub.threads.delete("th_range")  # sans ligne au hub, tour rangé seul
    assert w.get("/v1/threads/th_range")[0] == 200


def test_hub_thread_still_requires_membership(w):
    t = w.hub.threads.create("fil", "alice")
    assert w.get(f"/v1/threads/{t.id}")[0] == 403  # inchangé : fil du hub, appelant non membre


def test_neighbour_routes_without_get_stay_absent(w):
    tid = w.open_session()
    assert w.get(f"/v1/threads/{tid}/messages")[0] == 404  # pas de GET /messages (POST seulement)


@pytest.mark.parametrize("who", OTHERS)
@pytest.mark.parametrize("suffix", ["", "/bubbles", "/export?format=json"])
def test_known_empty_thread_is_404_for_other_users(w, who, suffix):
    tid = w.open_session()
    st, body = w.get(f"/v1/threads/{tid}{suffix}", who=who)
    ust, unknown = w.get(f"/v1/threads/th_inconnu{suffix}", who=who)
    assert st == ust == 404
    assert body["error"]["message"].replace(tid, "X") == unknown["error"]["message"].replace("th_inconnu", "X")
    assert w.get(f"/v1/threads/{tid}{suffix}")[0] == 200  # le propriétaire local, lui, lit


@pytest.mark.parametrize("suffix", ["", "/bubbles", "/export?format=json"])
def test_owner_access_session_reads_the_known_empty_thread(w, suffix):
    tid = w.open_session()
    assert w.get(f"/v1/threads/{tid}{suffix}", who="owner")[0] == 200


@pytest.mark.parametrize("who", ["present", "kiosk"])
@pytest.mark.parametrize("suffix", ["", "/bubbles", "/export?format=json"])
def test_present_and_kiosk_scopes_never_read_the_known_empty_thread(w, who, suffix):
    tid = w.open_session()
    st, _ = w.get(f"/v1/threads/{tid}{suffix}", who=who)
    assert st in (403, 404)  # 403 : la porte de portée de `parse_request` ; 404 : le refus du fil lui-même
    assert st != 200


@pytest.mark.parametrize("suffix", ["", "/bubbles"])
def test_present_gets_the_same_404_as_an_unknown_thread(w, suffix):
    """`present` passe la porte de portée sur ces routes : seul le refus explicite du fil l'arrête."""
    tid = w.open_session()
    st, body = w.get(f"/v1/threads/{tid}{suffix}", who="present")
    ust, unknown = w.get(f"/v1/threads/th_inconnu{suffix}", who="present")
    assert st == ust == 404
    assert body["error"]["message"].replace(tid, "X") == unknown["error"]["message"].replace("th_inconnu", "X")


def test_scope_refusal_is_what_stops_present(w, monkeypatch):
    """Garde-fou : sans le refus par portée, `present` (identité du propriétaire) lirait le fil ; ce test échoue alors."""
    tid = w.open_session()
    assert w.get(f"/v1/threads/{tid}", who="present")[0] == 404
    real = w.hub.read_thread
    monkeypatch.setattr(w.hub, "read_thread", lambda t, u, *, owner_scope=False: real(t, u, owner_scope=True))
    assert w.get(f"/v1/threads/{tid}", who="present")[0] == 200  # le contrôle neutralisé : la fuite apparaît


def test_stored_turn_thread_is_not_readable_by_another_user(w):
    w.kernel.run_turn(TurnInput(thread_id="th_range", user_text="salut", sender=LOCAL_OWNER))
    w.hub.threads.delete("th_range")
    assert w.get("/v1/threads/th_range", who="alice")[0] == 404
    assert w.get("/v1/threads/th_range")[0] == 200
