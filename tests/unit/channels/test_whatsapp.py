"""Canal WhatsApp : signature Meta, challenge GET, appairage, notes vocales, fenêtre de 24 h, rejeu, isolation, cartes."""
from __future__ import annotations

import json

from nokker.approvals.memory import InMemoryApprovals
from nokker.channels.common import ChannelCardNotifier
from nokker.channels.whatsapp import WINDOW_S, WhatsAppApi, sign, verify_challenge, verify_signature
from nokker.tenancy.context import tenant_of_thread

from tests.unit.channels.helpers import SECRET_IN_REPLY, FakeTranscriber, World, card_for_account, scrub

APP_SECRET = "app-secret-de-test"


def test_meta_signature_valid_and_invalid():
    body = b'{"object":"whatsapp_business_account"}'
    assert verify_signature(APP_SECRET, sign(APP_SECRET, body), body)
    assert not verify_signature(APP_SECRET, sign("autre", body), body)
    assert not verify_signature(APP_SECRET, sign(APP_SECRET, body), body + b"x")
    assert not verify_signature(APP_SECRET, "", body)
    assert not verify_signature(APP_SECRET, "sha1=abc", body)
    assert not verify_signature("", sign("", body), body)


def test_get_challenge():
    ok = {"hub.mode": ["subscribe"], "hub.verify_token": ["jeton-verif"], "hub.challenge": ["1158201444"]}
    assert verify_challenge(ok, "jeton-verif") == "1158201444"
    assert verify_challenge({**ok, "hub.verify_token": ["faux"]}, "jeton-verif") is None
    assert verify_challenge({**ok, "hub.mode": ["unsubscribe"]}, "jeton-verif") is None
    assert verify_challenge(ok, "") is None  # canal sans jeton : jamais vérifié


def test_unpaired_number_gets_instructions_and_no_turn():
    w = World()
    w.wa_message("41790000001", "salut")
    assert w.turns == [] and "pair CODE" in w.wa_api.texts[-1][1]


def test_pairing_then_turns_stay_in_each_account():
    w = World()
    w.wa_pair(w.a, "41790000001")
    w.wa_pair(w.b, "41790000002")
    w.wa_message("41790000001", "bonjour")
    w.wa_message("41790000002", "hello")
    (ia, ta), (ib, tb) = w.turns
    assert ta == w.a.tenant_id and ia.sender == w.a.account_id and ia.channel == "whatsapp"
    assert tb == w.b.tenant_id and tenant_of_thread(ib.thread_id) == w.b.tenant_id and ia.thread_id != ib.thread_id
    assert w.wa_api.texts[-1] == ("41790000002", "réponse à hello")


def test_code_single_use_and_number_cannot_join_two_accounts():
    w = World()
    code, _ = w.wa.core.pairing.issue(w.a.account_id)
    w.wa_message("41790000001", f"pair {code}")
    w.wa_message("41790000003", f"pair {code}")
    assert "pas valable" in w.wa_api.texts[-1][1]
    code_b, _ = w.wa.core.pairing.issue(w.b.account_id)
    w.wa_message("41790000001", f"pair {code_b}")
    assert "autre compte" in w.wa_api.texts[-1][1]
    assert w.tenancy.account_for_identity("whatsapp", "41790000001") == w.a


def test_other_phone_number_and_status_updates_are_ignored():
    w = World()
    w.wa_pair(w.a, "41790000001")
    n = len(w.wa_api.texts)
    w.wa_message("41790000001", "pour un autre numéro", phone_id="999")
    w.wa.handle({"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "1234567890"}, "statuses": [{"id": "wamid.x", "status": "read"}]}}]}]})
    assert w.turns == [] and len(w.wa_api.texts) == n


def test_replayed_message_is_processed_once():
    w = World()
    w.wa_pair(w.a, "41790000001")
    p = w.wa_message("41790000001", "une fois", wamid="wamid.SAME")
    w.wa.handle(p)
    w.wa.handle(json.loads(json.dumps(p)))
    assert len(w.turns) == 1


def test_stale_message_is_ignored():
    w = World()
    w.wa_pair(w.a, "41790000001")
    w.wa_message("41790000001", "vieux", ts=w.clock[0] - 2 * 24 * 3600)
    assert w.turns == []


def test_voice_note_goes_through_stt():
    stt = FakeTranscriber("rappelle-moi demain")
    w = World(transcriber=stt)
    w.wa_pair(w.a, "41790000001")
    w.wa_message("41790000001", kind="audio", audio={"id": "MEDIA1", "mime_type": "audio/ogg; codecs=opus",
                                                      "voice": True})
    assert w.wa_api.media_calls == ["MEDIA1"] and stt.calls == 1
    assert w.turns[-1][0].user_text == "rappelle-moi demain"


def test_voice_note_without_stt_is_refused_without_download():
    w = World()
    w.wa_pair(w.a, "41790000001")
    w.wa_message("41790000001", kind="audio", audio={"id": "MEDIA1", "mime_type": "audio/ogg"})
    assert w.turns == [] and w.wa_api.media_calls == [] and "vocales" in w.wa_api.texts[-1][1]


def test_secrets_are_scrubbed_from_what_goes_out():
    w = World(reply=f"clé : {SECRET_IN_REPLY}")
    w.wa_pair(w.a, "41790000001")
    w.wa_message("41790000001", "donne")
    assert SECRET_IN_REPLY not in w.wa_api.texts[-1][1]


def test_media_url_never_leaves_meta_hosts():
    calls = []

    def transport(method, url, headers, body):
        calls.append(url)
        return 200, {}, json.dumps({"url": "https://evil.example/m", "mime_type": "audio/ogg"}).encode()

    api = WhatsAppApi(phone_number_id="1", token=lambda: "tok", transport=transport)
    try:
        api.media("MEDIA1", 1000)
    except ValueError:
        pass
    else:
        raise AssertionError
    assert len(calls) == 1  # la métadonnée seulement, jamais l'URL étrangère avec le jeton


# ─────────────── cartes et fenêtre de 24 h ───────────────

def _notifier(w):
    approvals = InMemoryApprovals()
    approvals.tester_notifier = ChannelCardNotifier(tenancy=w.tenancy, channel="whatsapp", send_card=w.wa.send_card,
                                                    app_url="https://app.nokker.ai", scrub=scrub)
    return approvals


def test_card_in_window_is_a_link_button_to_the_owner_only():
    w = World()
    w.wa_pair(w.a, "41790000001")
    w.wa_pair(w.b, "41790000002")
    card = card_for_account(w.a, _notifier(w))
    (to, body, label, url), = w.wa_api.ctas
    assert to == "41790000001" and url == f"https://app.nokker.ai/approvals/{card.id}" and "FORTE" in body


def test_card_outside_the_window_is_not_sent_free_form():
    w = World()
    w.wa_pair(w.a, "41790000001")
    w.clock[0] += WINDOW_S  # 24 h sans message de l'utilisateur
    approvals = _notifier(w)
    card_for_account(w.a, approvals)
    assert w.wa_api.ctas == [] and w.wa_api.templates == [] and w.wa.skipped_cards == 1
    w.wa.card_template = {"name": "nokker_carte", "lang": "fr"}
    card = card_for_account(w.a, approvals)
    (to, name, lang, params, suffix), = w.wa_api.templates
    assert to == "41790000001" and name == "nokker_carte" and suffix == card.id


def test_forged_reply_button_never_decides_a_card():
    w = World()
    w.wa_pair(w.a, "41790000001")
    approvals = _notifier(w)
    card = card_for_account(w.a, approvals)
    for kind, extra in (("interactive", {"interactive": {"type": "button_reply",
                                                         "button_reply": {"id": f"approve:{card.id}", "title": "OK"}}}),
                        ("button", {"button": {"payload": f"approve:{card.id}", "text": "Approuver"}})):
        w.wa_message("41790000001", kind=kind, **extra)
    assert approvals.get(card.id).status == "pending" and w.turns == []
    assert "app web" in w.wa_api.texts[-1][1]


def test_api_retries_once_on_429():
    seq = [(429, {"Retry-After": "1"}, b""), (200, {}, b"{}")]
    waits, bodies = [], []

    def transport(method, url, headers, body):
        bodies.append(json.loads(body))
        return seq.pop(0)

    api = WhatsAppApi(phone_number_id="1", token=lambda: "tok", transport=transport, sleep=waits.append)
    api.send_cta("41790000001", "carte", "Ouvrir la carte", "https://app.nokker.ai/approvals/x")
    assert waits == [1.0] and len(bodies) == 2
    action = bodies[0]["interactive"]["action"]
    assert bodies[0]["interactive"]["type"] == "cta_url" and action["parameters"]["display_text"] == "Ouvrir la carte"
