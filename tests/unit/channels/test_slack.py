"""Canal Slack : signature v0 et anti-rejeu, appairage, DM et mentions, fils, rejeu dédoublonné, isolation, cartes."""
from __future__ import annotations

import json
import time

from nokker.approvals.memory import InMemoryApprovals
from nokker.channels.common import ChannelCardNotifier
from nokker.channels.slack import OPEN_CARD_ACTION, SlackApi, sign, verify_signature
from nokker.tenancy.context import tenant_of_thread

from tests.unit.channels.helpers import SECRET_IN_REPLY, TEAM, FakeTranscriber, World, card_for_account, scrub

SECRET = "signing-secret-de-test"


# ─────────────── signature ───────────────

def test_valid_signature_is_accepted():
    body, ts = b'{"type":"event_callback"}', int(time.time())
    assert verify_signature(SECRET, str(ts), sign(SECRET, ts, body), body)


def test_invalid_signature_is_refused():
    body, ts = b'{"type":"event_callback"}', int(time.time())
    assert not verify_signature(SECRET, str(ts), sign("autre-secret", ts, body), body)
    assert not verify_signature(SECRET, str(ts), sign(SECRET, ts, body), body + b" ")  # corps modifié
    assert not verify_signature(SECRET, str(ts), "", body)
    assert not verify_signature("", str(ts), sign("", ts, body), body)  # canal sans secret : tout refusé
    assert not verify_signature(SECRET, "pas-un-nombre", sign(SECRET, ts, body), body)


def test_replayed_request_outside_five_minutes_is_refused():
    body, old = b'{"type":"event_callback"}', int(time.time()) - 301
    assert not verify_signature(SECRET, str(old), sign(SECRET, old, body), body)
    future = int(time.time()) + 301
    assert not verify_signature(SECRET, str(future), sign(SECRET, future, body), body)


# ─────────────── appairage et tours ───────────────

def test_unpaired_user_gets_instructions_and_no_turn():
    w = World()
    w.slack_event("U1", "salut")
    assert w.turns == [] and "pair CODE" in w.slack_api.posts[-1][1]


def test_pairing_then_dm_turn_runs_in_the_account_tenant():
    w = World()
    w.slack_pair(w.a, "U1")
    assert "relié" in w.slack_api.posts[-1][1]
    assert w.tenancy.account_for_identity("slack", f"{TEAM}:U1") == w.a
    w.slack_event("U1", "bonjour")
    (inp, tenant), = w.turns
    assert tenant == w.a.tenant_id and inp.sender == w.a.account_id and inp.channel == "slack"
    assert tenant_of_thread(inp.thread_id) == w.a.tenant_id and inp.thread_id.endswith(f"sl-{TEAM}-D1")
    assert w.slack_api.posts[-1] == ("D1", "réponse à bonjour", None)


def test_pairing_code_single_use_and_identity_cannot_join_two_accounts():
    w = World()
    code, _ = w.slack.core.pairing.issue(w.a.account_id)
    w.slack_event("U1", f"pair {code}")
    w.slack_event("U2", f"appairer {code.lower()}")
    assert "pas valable" in w.slack_api.posts[-1][1]
    code_b, _ = w.slack.core.pairing.issue(w.b.account_id)
    w.slack_event("U1", f"/start {code_b}")
    assert "autre compte" in w.slack_api.posts[-1][1]
    assert w.tenancy.account_for_identity("slack", f"{TEAM}:U1") == w.a


def test_pairing_is_refused_in_a_channel():
    w = World()
    code, _ = w.slack.core.pairing.issue(w.a.account_id)
    w.slack_event("U1", f"<@UBOT> pair {code}", kind="app_mention", channel="C1")
    assert w.tenancy.account_for_identity("slack", f"{TEAM}:U1") is None
    assert "message privé" in w.slack_api.posts[-1][1]
    w.slack_event("U1", f"pair {code}")  # le code n'a pas été consommé : il marche encore en privé
    assert w.tenancy.account_for_identity("slack", f"{TEAM}:U1") == w.a


def test_mention_in_a_channel_answers_in_the_slack_thread_and_maps_one_thread():
    w = World()
    w.slack_pair(w.a, "U1")
    w.slack_event("U1", "<@UBOT> résume ce fil", kind="app_mention", channel="C9", ts="1700.1")
    w.slack_event("U1", "<@UBOT> et la suite ?", kind="app_mention", channel="C9", thread_ts="1700.1")
    (i1, _), (i2, _) = w.turns
    assert i1.user_text == "résume ce fil" and i1.thread_id == i2.thread_id
    assert i1.thread_id.endswith(f"sl-{TEAM}-C9-1700.1")
    assert [p[2] for p in w.slack_api.posts[-2:]] == ["1700.1", "1700.1"]


def test_two_accounts_in_the_same_slack_thread_have_two_nokker_threads():
    w = World()
    w.slack_pair(w.a, "U1")
    w.slack_pair(w.b, "U2")
    w.slack_event("U1", "<@UBOT> a", kind="app_mention", channel="C9", ts="1700.1")
    w.slack_event("U2", "<@UBOT> b", kind="app_mention", channel="C9", thread_ts="1700.1")
    (ia, ta), (ib, tb) = w.turns
    assert ta == w.a.tenant_id and tb == w.b.tenant_id and ia.thread_id != ib.thread_id
    assert tenant_of_thread(ib.thread_id) == w.b.tenant_id


def test_dm_thread_is_its_own_nokker_thread():
    w = World()
    w.slack_pair(w.a, "U1")
    w.slack_event("U1", "racine")
    w.slack_event("U1", "dans le fil", thread_ts="1700.5")
    (i1, _), (i2, _) = w.turns
    assert i1.thread_id != i2.thread_id and i2.thread_id.endswith("-1700.5")
    assert w.slack_api.posts[-1][2] == "1700.5"


def test_bots_edits_and_other_workspaces_are_ignored():
    w = World()
    w.slack_pair(w.a, "U1")
    n = len(w.slack_api.posts)
    w.slack_event("U1", "édité", subtype="message_changed")
    w.slack_event("U1", "d'un bot", bot_id="B1")
    w.slack_event("UBOT", "moi-même")
    w.slack_event("U1", "autre équipe", team="T0OTHER")
    w.slack_event("U1", "canal sans mention", channel="C1", channel_type="channel")
    assert w.turns == [] and len(w.slack_api.posts) == n


def test_replayed_event_is_processed_once():
    w = World()
    w.slack_pair(w.a, "U1")
    first = w.slack_event("U1", "une fois", event_id="EvSAME")
    w.slack.handle_event(first)  # réessai Slack (X-Slack-Retry-Num) : même event_id
    w.slack_event("U1", "une fois", event_id="EvSAME")
    assert len(w.turns) == 1


def test_secrets_are_scrubbed_from_what_goes_out():
    w = World(reply=f"voici la clé {SECRET_IN_REPLY}")
    w.slack_pair(w.a, "U1")
    w.slack_event("U1", "donne la clé")
    assert SECRET_IN_REPLY not in w.slack_api.posts[-1][1]


def test_rate_limit_warns_once_then_drops():
    w = World(rate=(1.0, 2))
    w.slack_pair(w.a, "U1")  # 1 jeton
    for i in range(5):
        w.slack_event("U1", f"m{i}")
    assert len(w.turns) == 1
    assert sum("Doucement" in p[1] for p in w.slack_api.posts) == 1


def test_voice_clip_is_transcribed_when_voice_is_on_and_refused_otherwise():
    clip = {"files": [{"mimetype": "audio/webm", "url_private_download": "https://files.slack.com/x", "size": 10}],
            "subtype": "file_share"}
    w = World(transcriber=FakeTranscriber("note dictée"))
    w.slack_pair(w.a, "U1")
    w.slack_event("U1", "", **clip)
    assert w.turns[-1][0].user_text == "note dictée" and w.slack_api.downloads == ["https://files.slack.com/x"]
    off = World()
    off.slack_pair(off.a, "U1")
    off.slack_event("U1", "", **clip)
    assert off.turns == [] and off.slack_api.downloads == [] and "vocales" in off.slack_api.posts[-1][1]


def test_file_download_never_leaves_slack_hosts():
    calls = []
    api = SlackApi(token=lambda: "xoxb-test", transport=lambda *a: calls.append(a) or (200, {}, b"x"))
    for url in ("https://evil.example/x", "http://files.slack.com/x", "https://files.slack.com.evil.example/x"):
        try:
            api.download(url, 100)
        except ValueError:
            pass
        else:
            raise AssertionError(url)
    assert calls == []


# ─────────────── cartes ───────────────

def test_card_goes_to_the_owner_dm_only_with_a_link_button():
    w = World()
    w.slack_pair(w.a, "U1")
    w.slack_pair(w.b, "U2")
    approvals = InMemoryApprovals()
    approvals.tester_notifier = ChannelCardNotifier(tenancy=w.tenancy, channel="slack", send_card=w.slack.send_card,
                                                    app_url="https://app.nokker.ai", scrub=scrub)
    card = card_for_account(w.a, approvals)
    (user, sent), = w.slack_api.cards
    assert user == "U1" and sent.url == f"https://app.nokker.ai/approvals/{card.id}" and sent.strong
    assert "FORTE" in sent.text and "files_delete" in sent.text and "/x" not in sent.text  # jamais les arguments


def test_card_message_has_only_a_url_button():
    sent = []
    api = SlackApi(token=lambda: "xoxb-test",
                   transport=lambda m, u, h, b: sent.append(json.loads(b)) or (200, {}, b'{"ok":true}'))
    from nokker.channels.common import Card

    api.post_card("U1", Card(text="t", label="Ouvrir", url="https://app.nokker.ai/approvals/x", strong=True))
    (payload,) = sent
    buttons = [e for b in payload["blocks"] if b["type"] == "actions" for e in b["elements"]]
    assert buttons == [{"type": "button", "action_id": OPEN_CARD_ACTION, "url": "https://app.nokker.ai/approvals/x",
                        "text": {"type": "plain_text", "text": "Ouvrir"}}]


def test_strong_card_is_never_decided_from_slack_even_with_a_forged_action():
    w = World()
    w.slack_pair(w.a, "U1")
    approvals = InMemoryApprovals()
    card = card_for_account(w.a, approvals)
    for action in ("approve", "reject", OPEN_CARD_ACTION):
        w.slack.handle_interaction({"type": "block_actions", "team": {"id": TEAM}, "user": {"id": "U1"},
                                    "actions": [{"action_id": action, "value": card.id}]})
    assert approvals.get(card.id).status == "pending" and approvals.get(card.id).decided_by is None


def test_slack_api_retries_once_on_429_and_raises_on_error():
    seq = [(429, {"Retry-After": "2"}, b""), (200, {}, b'{"ok":true}')]
    waits = []
    api = SlackApi(token=lambda: "xoxb-test", transport=lambda *a: seq.pop(0), sleep=waits.append)
    api.post("D1", "x")
    assert waits == [2.0] and seq == []
    bad = SlackApi(token=lambda: "xoxb-test", transport=lambda *a: (200, {}, b'{"ok":false,"error":"not_in_channel"}'))
    try:
        bad.post("C1", "x")
    except RuntimeError as e:
        assert "not_in_channel" in str(e)
    else:
        raise AssertionError
