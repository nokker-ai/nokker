"""Textes des canaux (Slack/WhatsApp : `channels.common.TEXTS` ; Telegram : `telegram.adapter.TEXTS`) en fr/en/de/it :
français et anglais inchangés (empreintes), de/it complets avec les mêmes trous, et la langue suit l'expéditeur."""
from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace

import pytest

from nokker.channels import common
from nokker.channels.common import ChannelCore, Inbound, MemoryPairing, card_for, texts
from nokker.core.turn import Turn
from nokker.telegram import adapter as tg
from nokker.telegram.adapter import TelegramAdapter
from nokker.telegram.pairing import MemoryPairing as TgPairing
from nokker.tenancy import MemoryTenancy
from tests.unit.telegram.test_adapter import FakeBot, msg


def _sha(d: dict) -> str:
    return hashlib.sha256(json.dumps(d, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def _holes(text: str) -> set[str]:
    return set(re.findall(r"\{(\w+)\}", text))


def test_french_and_english_channel_texts_are_unchanged():
    assert (_sha(common.TEXTS["fr"]), _sha(common.TEXTS["en"])) == ("ba5e3b0dff27d85f", "07a8039d4d8b00f9")
    # Telegram : + question_other / question_closed (API hôte, question à choix), textes fr/en de la branche host-api.
    assert (_sha(tg.TEXTS["fr"]), _sha(tg.TEXTS["en"])) == ("ac50341c3285cce5", "0ef2e4812331efc0")


@pytest.mark.parametrize("texts_", [common.TEXTS, tg.TEXTS], ids=["slack-whatsapp", "telegram"])
def test_tables_are_complete_in_four_languages_with_the_same_holes(texts_):
    assert set(texts_) == {"fr", "en", "de", "it"}
    for lang in ("de", "it"):
        assert set(texts_[lang]) == set(texts_["fr"]) == set(texts_["en"])
        for key, text in texts_[lang].items():
            assert text.strip() and _holes(text) == _holes(texts_["en"][key]) == _holes(texts_["fr"][key]), (lang, key)
            assert text != texts_["fr"][key] and text != texts_["en"][key] or key == "open", (lang, key)


def test_texts_picks_by_two_letter_code_and_falls_back_to_french():
    assert texts("de") is common.TEXTS["de"] and texts("it-CH") is common.TEXTS["it"] and texts("EN") is common.TEXTS["en"]
    assert texts(None) is common.TEXTS["fr"] and texts("pt") is common.TEXTS["fr"] and texts("fr") is common.TEXTS["fr"]


def test_pairing_command_and_pair_hint_are_kept_in_every_language():
    for lang in ("fr", "en", "de", "it"):
        assert "pair CODE" in common.TEXTS[lang]["unpaired"]
        assert "/start CODE" in tg.TEXTS[lang]["unpaired"]
    assert "/feedback" in tg.TEXTS["de"]["feedback_empty"] and "/feedback" in tg.TEXTS["it"]["feedback_empty"]


def _tg(lang_code, text="salut", paired=False):
    tenancy, bot = MemoryTenancy(), FakeBot()
    if paired:
        acc = tenancy.create_account("a@x.ch")
        tenancy.bind_identity(acc.account_id, "telegram", "111")
    seen = []
    adapter = TelegramAdapter(tenancy=tenancy, pairing=TgPairing(), bot=bot, run_turn=lambda inp: seen.append(inp) or
                              Turn(thread_id=inp.thread_id, index=0, input=None, reply="ok"))
    adapter.handle_update = lambda u: adapter._process(u["message"])
    adapter.handle_update(msg(111, text, lang=lang_code))
    return bot.sent[-1][1]


def test_telegram_texts_follow_the_client_language():
    assert _tg("de-CH") == tg.TEXTS["de"]["unpaired"] and _tg("it") == tg.TEXTS["it"]["unpaired"]
    assert _tg("fr-CH") == tg.TEXTS["fr"]["unpaired"] and _tg("en") == tg.TEXTS["en"]["unpaired"]
    assert _tg("pt") == tg.TEXTS["en"]["unpaired"]  # langue de client inconnue : anglais, comme avant
    assert _tg("de", "/start ZZZZZZ") == tg.TEXTS["de"]["bad_code"]
    assert _tg("it", "", paired=True) == tg.TEXTS["it"]["text_only"]


def test_telegram_error_and_feedback_texts_in_german():
    tenancy, bot = MemoryTenancy(), FakeBot()
    acc = tenancy.create_account("a@x.ch")
    tenancy.bind_identity(acc.account_id, "telegram", "111")

    def boom(inp):
        raise RuntimeError("x")

    adapter = TelegramAdapter(tenancy=tenancy, pairing=TgPairing(), bot=bot, run_turn=boom,
                              feedback=SimpleNamespace(submit=lambda *a: "f1"))
    adapter.handle_update = lambda u: adapter._process(u["message"])
    adapter.handle_update(msg(111, "salut", lang="de"))
    assert bot.sent[-1][1] == tg.TEXTS["de"]["error"]
    adapter.handle_update(msg(111, "/feedback super", lang="de"))
    assert bot.sent[-1][1] == tg.TEXTS["de"]["feedback_ok"]
    adapter.handle_update(msg(111, "/feedback", lang="it"))
    assert bot.sent[-1][1] == tg.TEXTS["it"]["feedback_ok"]


def _core(lang_of_account=None):
    ten, sent = MemoryTenancy(), []
    acc = ten.create_account("a@x.ch")
    if lang_of_account:
        ten.set_lang(acc.account_id, lang_of_account)
    ten.bind_identity(acc.account_id, "slack", "T1:U1")
    core = ChannelCore(channel="slack", display_name="Slack", tenancy=ten, pairing=MemoryPairing(),
                       run_turn=lambda inp: Turn(thread_id=inp.thread_id, index=0, input=None, reply="ok"),
                       scrub=lambda t: t)
    return core, sent


def test_slack_and_whatsapp_texts_follow_the_sender_language_then_the_account_language():
    core, sent = _core()
    for lang in ("de", "it", None):
        core.process(Inbound(channel="slack", identity="T9:U9", conversation="c", thread_key="k", text="hi",
                             reply=sent.append, lang=lang))
    assert sent == [common.TEXTS["de"]["unpaired"].format(name="Slack"),
                    common.TEXTS["it"]["unpaired"].format(name="Slack"),
                    common.TEXTS["fr"]["unpaired"].format(name="Slack")]
    core, sent = _core("de")
    core.process(Inbound(channel="slack", identity="T1:U1", conversation="c", thread_key="k", text="",
                         reply=sent.append, voice=object()))
    assert sent == [common.TEXTS["de"]["voice_off"]]


def test_approval_card_is_worded_in_the_account_language():
    approval = SimpleNamespace(tool_name="mail_send", reason="", strength="strong", id="ap1")
    for lang in ("de", "it"):
        c = card_for(approval, lang, "https://app.nokker.ai", lambda t: t)
        assert common.TEXTS[lang]["card"].format(tool="mail_send") in c.text and common.TEXTS[lang]["card_strong"] in c.text
        assert c.label == common.TEXTS[lang]["open"] and c.url.endswith("/approvals/ap1") and c.strong
