"""`BubbleHub` (docs/BUBBLES.md §1-§4) : droits, version/409, transfert move/copy + contexte injecté,
partage en lecture seule, sessions qui se parlent, notify (première action / 10 min)."""
from __future__ import annotations

import pytest

from nokker.bubbles.context import SUMMARY_MAX_CHARS, render_for_context
from nokker.bubbles.types import Bubble, BubbleError, BubbleForbidden, BubbleNotFound, VersionConflict
from nokker.core.turn import InputCheck, Turn
from nokker.threads.types import ThreadError, ThreadForbidden, ThreadNotFound


def _two_threads(hub):
    a = hub.create_thread("Infra", "alice")
    b = hub.create_thread("Compta", "alice")
    return a, b


# ─────────────── fils ───────────────

def test_thread_ids_and_listing_by_membership(hub, clock):
    a = hub.create_thread("Infra", "alice")
    clock.t += 1
    b = hub.create_thread("Perso Bruno", "bruno")
    assert a.id.startswith("th_") and len(a.id) == 15
    assert [t.id for t in hub.list_threads("alice")] == [a.id]
    hub.patch_thread(b.id, "bruno", members=["alice"])
    assert [t.id for t in hub.list_threads("alice")] == [b.id, a.id]  # updated_at desc


def test_only_owner_changes_members_and_members_must_be_known(hub):
    t = hub.create_thread("Infra", "alice")
    with pytest.raises(ThreadError):
        hub.patch_thread(t.id, "alice", members=["mallory"])
    hub.patch_thread(t.id, "alice", members=["bruno"])
    with pytest.raises(ThreadForbidden):
        hub.patch_thread(t.id, "bruno", members=[])
    hub.patch_thread(t.id, "bruno", title="Infra partagée")  # un membre change le titre
    assert hub.threads.get(t.id).title == "Infra partagée"


def test_only_owner_deletes_thread_and_its_bubbles_are_closed(hub, rec):
    t = hub.create_thread("Infra", "alice")
    hub.patch_thread(t.id, "alice", members=["bruno"])
    b = hub.create_bubble(t.id, "alice", kind="timer", title="Pause", state={"label": "pause"})
    with pytest.raises(ThreadForbidden):
        hub.delete_thread(t.id, "bruno")
    hub.delete_thread(t.id, "alice")
    assert hub.threads.get(t.id) is None
    assert [x.id for x in hub.list_threads("alice")] == []
    assert hub.bubbles.require(b.id).closed_at is not None
    with pytest.raises(ThreadNotFound):
        hub.delete_thread(t.id, "alice")


def test_non_member_cannot_read_thread(hub):
    t = hub.create_thread("Infra", "alice")
    with pytest.raises(ThreadForbidden):
        hub.list_bubbles(t.id, "bruno")
    with pytest.raises(ThreadNotFound):
        hub.list_bubbles("th_inconnu", "alice")


def test_unknown_session_id_creates_thread_with_provisional_title(hub, rec):
    t = hub.ensure_thread_for_turn("session-hermes-42", "alice", "x" * 100)
    assert t.id == "session-hermes-42" and t.owner == "alice" and t.title == "x" * 48
    assert rec.of("thread.updated")
    assert hub.ensure_thread_for_turn("session-hermes-42", "alice", "autre").title == "x" * 48
    with pytest.raises(ThreadForbidden):
        hub.ensure_thread_for_turn("session-hermes-42", "bruno", "intrus")
    hub.ensure_thread_for_turn("session-hermes-42", None, "serveur ouvert")  # dev : pas de contrôle


# ─────────────── bulles : droits, version ───────────────

def test_create_patch_version_and_409(hub, rec):
    t = hub.create_thread("Infra", "alice")
    b = hub.create_bubble(t.id, "alice", kind="timer", title="Pause", state={"label": "pause"})
    assert b.version == 1 and b.created_by == "alice"
    b2 = hub.patch_bubble(b.id, "alice", status="working", if_version=1)
    assert b2.version == 2 and b2.status == "working"
    with pytest.raises(VersionConflict) as exc:
        hub.patch_bubble(b.id, "alice", title="x", if_version=1)
    assert exc.value.current == 2
    assert rec.types().count("bubble.updated") == 1


def test_client_cannot_create_notify_or_unknown_kind(hub):
    t = hub.create_thread("Infra", "alice")
    for kind in ("notify", "widget"):
        with pytest.raises(BubbleError):
            hub.create_bubble(t.id, "alice", kind=kind)


def test_non_member_sees_404_and_shared_member_is_read_only(hub):
    t = hub.create_thread("Infra", "alice")
    b = hub.create_bubble(t.id, "alice", kind="monitor", title="Disque")
    with pytest.raises(BubbleNotFound):
        hub.get_bubble(b.id, "bruno")
    hub.share(b.id, "alice", members=["bruno"])
    got, readonly = hub.get_bubble(b.id, "bruno")
    assert got.id == b.id and readonly is True
    with pytest.raises(BubbleForbidden):
        hub.patch_bubble(b.id, "bruno", title="pirate")
    with pytest.raises(BubbleForbidden):
        hub.close_bubble(b.id, "bruno")
    with pytest.raises(BubbleError):
        hub.share(b.id, "alice", members=["inconnu"])


def test_shared_with_thread_listed_as_shared(hub):
    a, b = _two_threads(hub)
    bu = hub.create_bubble(a.id, "alice", kind="monitor", title="Disque")
    hub.share(bu.id, "alice", threads=[b.id])
    items = hub.list_bubbles(b.id, "alice")
    assert [(x.id, shared) for x, shared in items] == [(bu.id, True)]
    bruno_t = hub.create_thread("Bruno", "bruno")
    with pytest.raises(ThreadForbidden):  # on ne pousse pas une bulle dans un fil qu'on ne voit pas
        hub.share(bu.id, "alice", threads=[bruno_t.id])


def test_close_keeps_bubble_readable(hub, rec):
    t = hub.create_thread("Infra", "alice")
    b = hub.create_bubble(t.id, "alice", kind="timer")
    hub.close_bubble(b.id, "alice")
    assert hub.list_bubbles(t.id, "alice") == []
    assert [x.id for x, _ in hub.list_bubbles(t.id, "alice", include_closed=True)] == [b.id]
    assert rec.of("bubble.closed") == [{"bubble_id": b.id, "thread_id": t.id, "_audience": ["alice"]}]


# ─────────────── transfert ───────────────

def test_move_changes_thread_and_injects_context_both_sides(hub, rec):
    a, b = _two_threads(hub)
    bu = hub.create_bubble(a.id, "alice", kind="monitor", title="Disque hote21",
                           state={"target": "hote21:/srv", "metric": "disk", "value": 91, "unit": "%"})
    out = hub.transfer(bu.id, "alice", b.id, "move")
    moved = hub.bubbles.get(bu.id)
    assert out["new_bubble_id"] == bu.id and moved.thread_id == b.id
    assert moved.origin == {"thread_id": a.id, "bubble_id": bu.id, "mode": "move"}
    assert hub.list_bubbles(a.id, "alice") == []
    recv = hub.context_for(b.id)
    assert recv == ["[Bulle reçue de « Infra » — monitor « Disque hote21 »] hote21:/srv disk = 91%"]
    assert hub.context_for(b.id) == []  # injecté une seule fois
    assert hub.context_for(a.id) == ["[Bulle envoyée vers « Compta » — monitor « Disque hote21 »]"]
    types = rec.types()
    assert types.index("bubble.transferred") < len(types) - 1
    assert rec.of("bubble.transferred")[0]["mode"] == "move"
    assert "bubble.updated" in types


def test_copy_creates_new_bubble_source_stays(hub, rec):
    a, b = _two_threads(hub)
    bu = hub.create_bubble(a.id, "alice", kind="search", title="Libérer de l'espace",
                           state={"query": "libérer espace lvm", "results": [{"url": "https://x.org", "title": "X"}]})
    out = hub.transfer(bu.id, "alice", b.id, "copy")
    assert out["new_bubble_id"] != bu.id
    assert hub.bubbles.get(bu.id).thread_id == a.id
    new = hub.bubbles.get(out["new_bubble_id"])
    assert new.thread_id == b.id and new.origin["mode"] == "copy" and new.version == 1
    assert hub.context_for(a.id) == []  # copy : rien côté source
    assert hub.context_for(b.id)[0].startswith("[Bulle reçue de « Infra » — search")
    created = [d["bubble"]["id"] for d in rec.of("bubble.created")]
    assert out["new_bubble_id"] in created


def test_transfer_requires_membership_of_destination(hub):
    a = hub.create_thread("Infra", "alice")
    other = hub.create_thread("Bruno", "bruno")
    bu = hub.create_bubble(a.id, "alice", kind="timer")
    with pytest.raises(ThreadForbidden):
        hub.transfer(bu.id, "alice", other.id, "copy")
    with pytest.raises(BubbleError):
        hub.transfer(bu.id, "alice", a.id, "move")
    with pytest.raises(BubbleError):
        hub.transfer(bu.id, "alice", other.id, "teleport")


def test_render_for_context_is_bounded_and_scrubbed():
    b = Bubble(id="bu_1", thread_id="th_a", kind="script", title="deploy",
               state={"command": "echo S3CR3T-VALUE " + "y" * 2000, "log_tail": ["ok"] * 10})
    from tests.unit.bubbles.conftest import scrub

    text = render_for_context(b, "Infra", scrub)
    head = "[Bulle reçue de « Infra » — script « deploy »] "
    assert text.startswith(head)
    assert len(text) - len(head) <= SUMMARY_MAX_CHARS
    assert "S3CR3T-VALUE" not in text and "[secret:TEST]" in text


# ─────────────── sessions qui se parlent ───────────────

def test_send_message_injects_context_event_and_notify(hub, rec):
    a, b = _two_threads(hub)
    out = hub.send_message(a.id, b.id, "la facture est payée", sender="alice")
    assert out == {"delivered": True, "ask": False}
    assert hub.context_for(b.id) == ["[Message de la session « Infra »] la facture est payée"]
    msg = rec.of("session.message")[0]
    assert msg["from_thread"] == a.id and msg["to_thread"] == b.id and msg["text"] == "la facture est payée"
    notif = rec.of("bubble.created")[-1]["bubble"]
    assert notif["kind"] == "notify" and notif["thread_id"] is None
    assert notif["state"]["from_thread"] == a.id and notif["state"]["from_title"] == "Infra"


def test_send_message_refused_to_non_member_thread(hub):
    a = hub.create_thread("Infra", "alice")
    other = hub.create_thread("Bruno", "bruno")
    with pytest.raises(ThreadForbidden):
        hub.send_message(a.id, other.id, "coucou", sender="alice")


class FakeKernel:
    def __init__(self, reply="Oui, payée le 12."):
        self.reply = reply
        self.inputs = []

        class _S:
            def history(self, tid):
                return []
        self.store = _S()

    def run_turn(self, inp):
        self.inputs.append(inp)
        t = Turn(thread_id=inp.thread_id, index=0, input=InputCheck(text=inp.user_text), channel=inp.channel,
                 sender=inp.sender)
        t.reply = self.reply
        return t


def test_ask_runs_turn_in_destination_and_returns_reply_to_source(hub, rec):
    a, b = _two_threads(hub)
    hub.kernel = FakeKernel()
    out = hub.send_message(a.id, b.id, "la facture est payée ?", ask=True, sender="alice")
    assert out["ask"] is True
    (inp,) = hub.kernel.inputs
    assert inp.thread_id == b.id and inp.channel == "session" and inp.sender == "alice"
    assert inp.user_text == "[Message de la session « Infra »] la facture est payée ?"
    assert hub.context_for(b.id) == []  # pas de doublon : le bloc est l'entrée du tour
    assert hub.context_for(a.id) == ["[Réponse de la session « Compta »] Oui, payée le 12."]
    msgs = rec.of("session.message")
    assert [(m["from_thread"], m["to_thread"]) for m in msgs] == [(a.id, b.id), (b.id, a.id)]


def test_ask_disallowed_from_session_turn(hub):
    a, b = _two_threads(hub)
    hub.kernel = FakeKernel()
    out = hub.send_message(a.id, b.id, "x", ask=True, sender="alice", allow_ask=False)
    assert out["ask"] is False and hub.kernel.inputs == []


def test_notify_closed_on_first_action(hub, rec):
    a, b = _two_threads(hub)
    hub.send_message(a.id, b.id, "regarde", sender="alice")
    notif_id = rec.of("bubble.created")[-1]["bubble"]["id"]
    with pytest.raises(BubbleNotFound):
        hub.get_bubble(notif_id, "bruno")
    out = hub.action(notif_id, "alice", "goto")
    assert out == {"closed": True, "goto": a.id}
    assert hub.bubbles.get(notif_id).closed_at is not None
    assert rec.of("bubble.closed")[-1]["bubble_id"] == notif_id


def test_notify_expires_after_10_minutes(hub, clock, rec):
    a, b = _two_threads(hub)
    hub.send_message(a.id, b.id, "regarde", sender="alice")
    notif_id = rec.of("bubble.created")[-1]["bubble"]["id"]
    clock.t += 599
    assert hub.sweep_expired() == 0
    clock.t += 2
    assert hub.sweep_expired() == 1
    assert hub.bubbles.get(notif_id).closed_at is not None


def test_list_notifications_for_recipient_only(hub):
    a, b = _two_threads(hub)
    hub.create_thread("Bruno", "bruno")
    hub.send_message(a.id, b.id, "regarde", sender="alice")
    assert [n.kind for n in hub.list_notifications("alice")] == ["notify"]
    assert hub.list_notifications("bruno") == []  # pas destinataire
    assert hub.list_notifications(None) == []
    hub.action(hub.list_notifications("alice")[0].id, "alice", "goto")
    assert hub.list_notifications("alice") == []  # fermée -> plus listée


class FakeAgentsHub:
    def __init__(self):
        self.created: list[dict] = []

    def create_agent(self, *, name, role, model, tools, created_by):
        self.created.append({"name": name, "role": role, "model": model, "tools": tools,
                             "created_by": created_by})
        from nokker.agents.types import Agent

        return Agent(id="ag_test00001", name=name, role=role, model=model, tools=tools,
                    created_by=created_by)


def test_agent_draft_activation_creates_agent_closes_bubble_and_publishes(hub, rec):
    t = hub.create_thread("Infra", "alice")
    b = hub.create_bubble(t.id, "alice", kind="agent_draft",
                          state={"name": "Veille", "role": "scout", "model": "ship", "tools": ["web_search"]},
                          actions=[{"id": "activate", "label": "Activer", "primary": True}])
    agents_hub = FakeAgentsHub()
    hub.agents_hub = agents_hub
    out = hub.action(b.id, "alice", "activate")
    assert out["activated"] is True and out["agent"]["id"] == "ag_test00001"
    assert agents_hub.created == [{"name": "Veille", "role": "scout", "model": "ship",
                                   "tools": ["web_search"], "created_by": "alice"}]
    assert hub.bubbles.get(b.id).closed_at is not None
    with pytest.raises(BubbleNotFound):
        hub.action(b.id, "alice", "autre")


def test_agent_draft_activation_without_agents_hub_is_refused(hub):
    t = hub.create_thread("Infra", "alice")
    b = hub.create_bubble(t.id, "alice", kind="agent_draft", state={"name": "Veille", "role": "scout"})
    with pytest.raises(BubbleError):
        hub.action(b.id, "alice", "activate")


def test_bubble_action_becomes_a_turn_never_direct(hub):
    t = hub.create_thread("Infra", "alice")
    b = hub.bubbles.create(thread_id=t.id, kind="monitor", title="Disque",
                           actions=[{"id": "purge", "label": "Proposer une purge", "primary": True}])
    hub.kernel = FakeKernel()
    out = hub.action(b.id, "alice", "purge")
    assert out == {"accepted": True, "thread_id": t.id}
    (inp,) = hub.kernel.inputs
    assert inp.thread_id == t.id and "Proposer une purge" in inp.user_text and inp.sender == "alice"
    with pytest.raises(BubbleNotFound):
        hub.action(b.id, "alice", "inconnue")


# ─────────────── outils du modèle ───────────────

def test_bubble_update_tool_limited_to_current_thread(hub):
    from nokker.bubbles.tools import BubbleToolSource
    from nokker.core.ports import ToolContext

    a, b = _two_threads(hub)
    bu = hub.create_bubble(a.id, "alice", kind="timer", title="t")
    src = BubbleToolSource(hub=hub)
    ok = src.call("bubble_update", {"bubble_id": bu.id, "status": "done"}, ToolContext(a.id, 0, "api", "alice"))
    assert not ok.is_error and hub.bubbles.get(bu.id).status == "done"
    ko = src.call("bubble_update", {"bubble_id": bu.id, "title": "x"}, ToolContext(b.id, 0, "api", "alice"))
    assert ko.is_error and "fil courant" in ko.content


def test_session_send_tool_scope_and_no_ask_from_session_channel(hub):
    from nokker.bubbles.tools import BubbleToolSource
    from nokker.core.ports import ToolContext

    a, b = _two_threads(hub)
    other = hub.create_thread("Bruno", "bruno")
    hub.kernel = FakeKernel()
    src = BubbleToolSource(hub=hub)
    ko = src.call("session_send", {"to_thread": other.id, "text": "x"}, ToolContext(a.id, 0, "api", "alice"))
    assert ko.is_error
    ok = src.call("session_send", {"to_thread": b.id, "text": "x", "ask": True},
                  ToolContext(a.id, 0, "session", "alice"))
    assert not ok.is_error and '"ask": false' in ok.content and hub.kernel.inputs == []


def test_agent_draft_tool_creates_bubble_with_activate_action_and_clamps_fill(hub):
    from nokker.bubbles.tools import BubbleToolSource
    from nokker.core.ports import ToolContext

    t = hub.create_thread("Infra", "alice")
    src = BubbleToolSource(hub=hub)
    out = src.call("agent_draft", {"name": "Veille", "role": "scout", "model": "ship",
                                    "tools": ["web_search"], "fill": 5},
                   ToolContext(t.id, 0, "api", "alice"))
    assert not out.is_error
    bubbles = hub.bubbles.list_thread(t.id)
    assert len(bubbles) == 1 and bubbles[0].kind == "agent_draft"
    assert bubbles[0].state == {"name": "Veille", "role": "scout", "model": "ship",
                                "tools": ["web_search"], "fill": 1.0}  # fill borné à 1
    assert bubbles[0].actions == [{"id": "activate", "label": "Activer", "primary": True}]
    # pas membre du fil -> refusé, pas de carte en plus
    ko = src.call("agent_draft", {"name": "x", "role": "y"}, ToolContext(t.id, 0, "api", "bruno"))
    assert ko.is_error
    assert len(hub.bubbles.list_thread(t.id)) == 1


def test_bubble_tools_declared_read():
    from nokker.core.turn import Effect
    from nokker.tools.effects import DEFAULT_EFFECTS

    assert DEFAULT_EFFECTS["bubble_update"] is Effect.READ
    assert DEFAULT_EFFECTS["session_send"] is Effect.READ
    assert DEFAULT_EFFECTS["agent_draft"] is Effect.READ


# ─────────────── audience des événements ───────────────

def test_event_audience_is_members_and_shared(hub, rec):
    t = hub.create_thread("Infra", "alice")
    b = hub.create_bubble(t.id, "alice", kind="timer")
    assert rec.of("bubble.created")[-1]["_audience"] == ["alice"]
    hub.share(b.id, "alice", members=["bruno"])
    assert rec.of("bubble.updated")[-1]["_audience"] == ["alice", "bruno"]


def test_bubble_show_creates_clickable_preview_card(hub):
    """Alice 19.10 : « affiche-le moi dans une bulle » — carte d'aperçu (OpenGraph), pas de navigateur."""
    from nokker.bubbles.tools import BubbleToolSource
    from nokker.core.ports import ToolContext

    t = hub.create_thread("Infra", "alice")

    class Previews:
        def preview(self, url):
            return {"url": url, "title": "Jeanne Dupont (@jdupont) • Instagram photos and videos",
                    "description": "124 Followers, 78 Following, 12 Posts", "image": "/v1/previews/img/abc"}

    src = BubbleToolSource(hub=hub, previews=Previews())
    res = src.call("bubble_show", {"url": "voir https://www.instagram.com/jdupont/"}, ToolContext(t.id, 0, "api", "alice"))
    assert not res.is_error
    b = hub.bubbles.require(__import__("json").loads(res.content)["bubble_id"])
    assert b.kind == "media" and b.status == "done" and b.state["mime"] == "text/html"
    assert b.state["url"] == "https://www.instagram.com/jdupont/"
    assert b.state["preview"]["description"].startswith("124 Followers")
    assert b.title.startswith("Jeanne Dupont")


def test_bubble_show_without_preview_marks_failed(hub):
    from nokker.bubbles.tools import BubbleToolSource
    from nokker.core.ports import ToolContext

    t = hub.create_thread("Infra", "alice")
    src = BubbleToolSource(hub=hub)
    res = src.call("bubble_show", {"url": "https://example.org/"}, ToolContext(t.id, 0, "api", "alice"))
    b = hub.bubbles.require(__import__("json").loads(res.content)["bubble_id"])
    assert b.status == "failed"
    assert src.call("bubble_show", {"url": "pas une url"}, ToolContext(t.id, 0, "api", "alice")).is_error


def test_bubble_update_tool_merges_state_instead_of_replacing(hub):
    from nokker.bubbles.tools import BubbleToolSource
    from nokker.core.ports import ToolContext

    t = hub.create_thread("Infra", "alice")
    bu = hub.create_bubble(t.id, "alice", kind="media", title="x",
                           state={"url": "https://www.instagram.com/jdupont/", "mime": "text/html", "poster": "/v1/p"})
    BubbleToolSource(hub=hub).call("bubble_update", {"bubble_id": bu.id, "state": {"display": "note"}},
                                   ToolContext(t.id, 0, "api", "alice"))
    st = hub.bubbles.get(bu.id).state
    assert st["url"] == "https://www.instagram.com/jdupont/" and st["poster"] == "/v1/p" and st["display"] == "note"
