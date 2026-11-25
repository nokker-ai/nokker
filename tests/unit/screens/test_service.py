"""V7 : brouillon par le modèle, aperçu sans données du foyer, publication par la personne sous carte forte, mur."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nokker.bubbles.producers import Producers
from nokker.core.ports import ToolContext
from nokker.core.turn import Effect, ToolCall
from nokker.screens import texts as T
from nokker.screens import wiring
from nokker.screens.service import PUBLISH_RULE, PUBLISH_TOOL, ScreenService, spec_sha
from nokker.screens.store import MemoryScreenStore

SPEC = {"title": "Courses", "blocks": [{"type": "checklist", "items": [{"text": "pain"}, {"text": "lait"}]},
                                      {"type": "next_event"}]}


class Cards:
    def __init__(self):
        self.cards = []

    def request(self, turn, call, verdict, note):
        c = SimpleNamespace(id=f"ap_{len(self.cards) + 1}", arguments=call.arguments, call=call, verdict=verdict,
                            status="pending", turn=turn)
        self.cards.append(c)
        return c


@pytest.fixture
def svc():
    s = ScreenService(store=MemoryScreenStore(), approvals=Cards(), clock=lambda: 1_800_000_000.0)
    s.tenant = lambda: "t_a"
    return s


def ctx(sender="alice", thread="th_1"):
    return ToolContext(thread_id=thread, turn_index=0, channel="cli", sender=sender)


def draft(svc, args=None, **kw):
    r = svc.tool_source().call(T.TOOL, args or SPEC, ctx(**kw))
    return r


def test_tool_is_read_and_off_by_default():
    spec = ScreenService(store=MemoryScreenStore()).tool_source().specs()[0]
    assert spec.name == "screen_draft" and spec.effect is Effect.READ and spec.core is False
    assert wiring.configure({}) is None and wiring.configure({"screens": {"enabled": True}}) is None  # glance exigé
    assert wiring.configure({"screens": {"enabled": True}, "glance": {"enabled": True}}) is not None
    wiring.configure({})


def test_draft_preview_publish_and_wall(svc):
    r = draft(svc)
    assert not r.is_error and "Publier sur le mur" in r.content
    sid = r.content.split("écran ", 1)[1].split(" ", 1)[0]
    st = Producers(screen_state=lambda i: svc.bubble_state("t_a", i)).produce(
        ToolCall(id="c1", name="screen_draft", arguments={}), None, r)
    assert st.kind == "screen" and st.state["status"] == "draft" and st.state["actions"][0]["id"] == "publish"
    assert "<script" not in st.state["html"] and "Sur le mur : ton prochain" in st.state["html"]
    assert svc.for_wall("t_a", "alice", None) is None  # rien publié
    # la personne publie : carte FORTE, rien n'est publié avant la décision
    out = svc.bubble_action("t_a", st.state, "publish", "alice")
    card = svc.approvals.cards[0]
    assert out["status"] == "pending" and out["actions"] == []
    assert card.call.name == PUBLISH_TOOL and card.verdict.rule == PUBLISH_RULE and card.verdict.strength == "strong"
    assert svc.for_wall("t_a", "alice", None) is None
    card.status = "approved"
    assert "publié" in svc.publish_handler()(card)
    wall = svc.for_wall("t_a", "alice", {"start": "2026-11-09T09:00:00Z", "all_day": False})
    assert wall["id"] == sid and wall["version"] == 1 and "pain" in wall["html"] and "Rendez-vous" in wall["html"]
    assert svc.bubble_state("t_a", sid)["status"] == "published"


def test_retouch_keeps_the_published_version_until_a_new_card(svc):
    sid = draft(svc).content.split("écran ", 1)[1].split(" ", 1)[0]
    svc.bubble_action("t_a", {"screen_id": sid}, "publish", "alice")
    svc.approvals.cards[0].status = "approved"
    svc.publish_handler()(svc.approvals.cards[0])
    r = draft(svc, {**SPEC, "title": "Courses de lundi", "screen_id": sid})
    assert "version 2" in r.content
    assert svc.for_wall("t_a", "alice", None)["title"] == "Courses"  # le mur montre toujours la version publiée
    assert svc.bubble_state("t_a", sid)["status"] == "draft"  # la v2 attend sa propre carte


def test_card_for_an_old_version_publishes_nothing(svc):
    sid = draft(svc).content.split("écran ", 1)[1].split(" ", 1)[0]
    svc.bubble_action("t_a", {"screen_id": sid}, "publish", "alice")
    draft(svc, {**SPEC, "title": "Autre", "screen_id": sid})  # retouché pendant que la carte attend
    card = svc.approvals.cards[0]
    card.status = "approved"
    assert "rien n'a été publié" in svc.publish_handler()(card)
    assert svc.for_wall("t_a", "alice", None) is None


def test_refused_card_and_unpublish(svc):
    sid = draft(svc).content.split("écran ", 1)[1].split(" ", 1)[0]
    svc.bubble_action("t_a", {"screen_id": sid}, "publish", "alice")
    svc.approvals.cards[0].status = "refused"
    assert svc.publish_handler()(svc.approvals.cards[0]) == ""
    assert svc.for_wall("t_a", "alice", None) is None
    svc.bubble_action("t_a", {"screen_id": sid}, "publish", "alice")
    svc.approvals.cards[1].status = "approved"
    svc.publish_handler()(svc.approvals.cards[1])
    out = svc.bubble_action("t_a", {"screen_id": sid}, "unpublish", "alice")
    assert out["status"] == "draft" and svc.for_wall("t_a", "alice", None) is None


def test_one_published_screen_per_account(svc):
    ids = []
    for title in ("Un", "Deux"):
        sid = draft(svc, {**SPEC, "title": title}).content.split("écran ", 1)[1].split(" ", 1)[0]
        svc.bubble_action("t_a", {"screen_id": sid}, "publish", "alice")
        svc.approvals.cards[-1].status = "approved"
        svc.publish_handler()(svc.approvals.cards[-1])
        ids.append(sid)
    assert svc.for_wall("t_a", "alice", None)["id"] == ids[1]
    assert svc.bubble_state("t_a", ids[0])["status"] == "draft"


def test_only_the_author_publishes_and_ids_stay_in_their_thread(svc):
    sid = draft(svc).content.split("écran ", 1)[1].split(" ", 1)[0]
    with pytest.raises(PermissionError):
        svc.bubble_action("t_a", {"screen_id": sid}, "publish", "bob")
    assert draft(svc, {**SPEC, "screen_id": sid}, sender="bob").is_error
    assert draft(svc, {**SPEC, "screen_id": sid}, thread="th_2").is_error
    assert draft(svc, {**SPEC, "screen_id": "scr_000000000000"}).is_error
    svc.tenant = lambda: "t_b"
    assert svc.bubble_state("t_b", sid) is None  # autre locataire : rien


def test_invalid_spec_lists_what_to_fix(svc):
    r = draft(svc, {"title": "x", "blocks": [{"type": "iframe", "src": "https://evil.example.com"}]})
    assert r.is_error and r.content.startswith("REFUSÉ") and "type inconnu" in r.content
    assert svc.approvals.cards == []


def test_spec_sha_is_stable():
    assert spec_sha({"b": 1, "a": [1]}) == spec_sha({"a": [1], "b": 1})


def test_screen_bubble_one_per_screen_and_publish_through_the_hub(svc):
    from nokker.bubbles.hub import BubbleHub
    from nokker.bubbles.memory import InMemoryBubbles
    from nokker.core.turn import InputCheck, Turn
    from nokker.threads.memory import InMemoryThreads

    hub = BubbleHub(threads=InMemoryThreads(), bubbles=InMemoryBubbles(), publish=lambda *a: None,
                    spawn=lambda fn: fn())
    svc.tenant = lambda: ""
    hub.screens = svc
    hub.producers.screen_state = lambda sid: svc.bubble_state("", sid)
    svc.on_change = hub.screen_changed
    th = hub.create_thread("Mur", "alice")
    turn = Turn(thread_id=th.id, index=0, input=InputCheck(text="fais-moi un écran"), sender="alice")
    c = ctx(thread=th.id)
    res = svc.tool_source().call(T.TOOL, SPEC, c)
    hub.tool_executed(turn, ToolCall(id="c1", name="screen_draft", arguments=SPEC), None, res)
    b = next(x for x in hub.bubbles.list_thread(th.id) if x.kind == "screen")
    sid = b.state["screen_id"]
    res2 = svc.tool_source().call(T.TOOL, {**SPEC, "title": "Courses v2", "screen_id": sid}, c)
    extra = hub.tool_executed(turn, ToolCall(id="c2", name="screen_draft", arguments={}), None, res2)
    assert extra == T.BUBBLE_UPDATED.format(id=b.id, version=2)
    assert len([x for x in hub.bubbles.list_thread(th.id) if x.kind == "screen"]) == 1
    out = hub.action(b.id, "alice", "publish")
    assert out["screen"]["status"] == "pending" and "html" not in out["screen"]
    card = svc.approvals.cards[0]
    card.status = "approved"
    svc.publish_handler()(card)
    assert hub.bubbles.get(b.id).state["status"] == "published"


def test_card_says_who_sees_it_and_mentions_the_event_only_when_shown(svc):
    draft(svc, {"title": "Sans agenda", "blocks": [{"type": "text", "text": "Bonjour"}]})
    sid = svc.store._rows and next(iter(svc.store._rows.values())).screen_id
    svc.bubble_action("t_a", {"screen_id": sid}, "publish", "alice")
    reason = svc.approvals.cards[-1].verdict.reason
    assert "Tous ceux qui voient le mur le verront." in reason and "rendez-vous" not in reason
    r = draft(svc, {"title": "Avec agenda", "blocks": [{"type": "group", "blocks": [{"type": "next_event"}]}]})
    sid2 = r.content.split("écran ", 1)[1].split(" ", 1)[0]
    svc.bubble_action("t_a", {"screen_id": sid2}, "publish", "alice")
    assert T.CARD_EVENT.strip() in svc.approvals.cards[-1].verdict.reason


def test_frame_links_are_signed_short_lived_and_bounded(svc):
    now = [1_800_000_000.0]
    svc.clock = lambda: now[0]
    url = svc.frame_url_for("<p>x</p>")
    key = url.split("/v1/screens/frame/", 1)[1].split("?", 1)[0]
    q = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
    assert svc.frame_html(key, q["exp"], q["sig"]) == "<p>x</p>"
    with pytest.raises(LookupError):
        svc.frame_html(key, q["exp"], "0" * 32)
    with pytest.raises(LookupError):
        svc.frame_html(key, str(int(q["exp"]) + 60), q["sig"])  # échéance changée : signature fausse
    now[0] += 301
    with pytest.raises(LookupError):
        svc.frame_html(key, q["exp"], q["sig"])  # échu
    for _ in range(300):
        svc.frame_url_for("y")
    assert len(svc._frames) <= 256


def test_preview_frame_is_the_preview_and_only_for_the_author(svc):
    sid = draft(svc).content.split("écran ", 1)[1].split(" ", 1)[0]
    out = svc.preview_frame_url("t_a", sid, "alice")
    key = out["url"].split("/v1/screens/frame/", 1)[1].split("?", 1)[0]
    assert "Sur le mur : ton prochain" in svc._frames[key][0] and out["version"] == 1
    with pytest.raises(LookupError):
        svc.preview_frame_url("t_a", sid, "bob")
    with pytest.raises(LookupError):
        svc.preview_frame_url("t_a", sid, None)  # hors Lokkal : pas de « tout écran de la machine »
    svc.local = True
    assert svc.preview_frame_url("t_a", sid, None)["version"] == 1  # jeton local de la machine (Lokkal)


def test_turn_without_sender_is_the_local_owner(svc):
    svc.local = True  # Lokkal
    r = svc.tool_source().call(T.TOOL, SPEC, ToolContext(thread_id="th_1", turn_index=0, channel="local", sender=None))
    sid = r.content.split("écran ", 1)[1].split(" ", 1)[0]
    assert svc.store.get("t_a", sid).account_id == "local-owner"
    svc.bubble_action("t_a", {"screen_id": sid}, "publish", None)
    svc.approvals.cards[-1].status = "approved"
    svc.publish_handler()(svc.approvals.cards[-1])
    assert svc.for_wall("t_a", "local-owner", None)["id"] == sid


def test_identity_fallbacks_exist_on_lokkal_only_and_match_cast():
    """(a) hors Lokkal, aucun repli : sans expéditeur ni locataire, rien n'est écrit, lu ni publié ; (b) les replis
    vivent dans ScreenService.member/tenant seulement ; (c) même membre que cast et que le jeton local."""
    from nokker.cast.service import DEFAULT_MEMBER
    from nokker.local.api import LOCAL_OWNER
    from nokker.screens.service import LOCAL_MEMBER, NoIdentity
    from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

    assert LOCAL_MEMBER == DEFAULT_MEMBER == LOCAL_OWNER
    cloud = ScreenService(store=MemoryScreenStore(), approvals=Cards())
    assert cloud.local is False and wiring.configure({"screens": {"enabled": True}, "glance": {"enabled": True}}).local is False
    wiring.configure({})
    with pytest.raises(NoIdentity):
        cloud.tenant()  # hors de tout locataire : jamais l'interne
    with pytest.raises(NoIdentity):
        cloud.member(None)
    r = cloud.tool_source().call(T.TOOL, SPEC, ToolContext(thread_id="th", turn_index=0, channel="api", sender=None))
    assert r.is_error and r.content == T.OFF and cloud.store._rows == {}
    with tenant_scope("tn_x"):
        r = cloud.tool_source().call(T.TOOL, SPEC, ToolContext(thread_id="th", turn_index=0, channel="api", sender=None))
        assert r.is_error and cloud.store._rows == {}  # un locataire, mais personne : refusé quand même
        ok = cloud.tool_source().call(T.TOOL, SPEC, ToolContext(thread_id="th", turn_index=0, channel="api", sender="ac_1"))
        sid = ok.content.split("écran ", 1)[1].split(" ", 1)[0]
        assert cloud.store.get("tn_x", sid).account_id == "ac_1"
        with pytest.raises(PermissionError):
            cloud.bubble_action("tn_x", {"screen_id": sid}, "publish", None)
        with pytest.raises(LookupError):
            cloud.preview_frame_url("tn_x", sid, None)  # « tout écran de la machine » : Lokkal seulement
        assert cloud.for_wall("tn_x", None, None) is None
    lokkal = ScreenService(store=MemoryScreenStore(), local=True)
    assert lokkal.tenant() == INTERNAL_TENANT and lokkal.member(None) == LOCAL_OWNER
    with tenant_scope("tn_y"):
        assert lokkal.tenant() == "tn_y"  # un locataire posé l'emporte toujours, même sur Lokkal


def test_the_fallback_is_wired_only_for_lokkal():
    import inspect

    from nokker.api import server

    src = inspect.getsource(server)
    calls = [line.strip() for line in src.splitlines() if "screens_wiring.configure(" in line]
    assert len(calls) == 1  # un seul montage du service
    i = src.index("screens_wiring.configure(")
    assert "if local_api is not None:" in src[max(0, i - 400):i] and "local=True" in src[i:i + 200]
