"""Réponses fixes du noyau vues de la personne seule (BYOK non configuré, suites automatiques, carte en attente, annulation,
action répétée, libellé d'outil) en fr/en/de/it : français figé, autres langues selon la langue du tour."""
from __future__ import annotations

import re
import threading
from types import SimpleNamespace

import pytest

from nokker.approvals.rule_labels import LANGS, set_request_lang
from nokker.core import person_texts as ptx
from nokker.core.approval_repeat import APPROVAL_REPEAT_REPLY
from nokker.core.loop import (
    BYOK_NOT_CONFIGURED_REPLY,
    CANCEL_REPLY,
    FOLLOW_CHAIN_REPLY,
    HUMAN_TOOL_FALLBACK,
    HUMAN_TOOLS,
    PENDING_TURN_END,
    human_tool,
)
from nokker.core.ports import ToolResult  # noqa: F401
from nokker.core.turn import Message, TurnInput
from tests.unit.test_thread_serialization import _req, _server

KEYS = ("CANCEL", "NOTE_NOT_TRIED", "FOLLOW_CHAIN", "PENDING_END", "APPROVAL_REPEAT", "BYOK_NOT_CONFIGURED",
        "HUMAN_TOOL.terminal", "HUMAN_TOOL.desktop", "HUMAN_TOOL.file", "HUMAN_TOOL.mail", "HUMAN_TOOL.none")


def _holes(text: str) -> set[str]:
    return set(re.findall(r"\{(\w+)\}", text))


@pytest.mark.parametrize("key", KEYS)
def test_tables_have_four_languages_and_the_same_holes(key):
    table = ptx.ALL_TABLES[key]
    assert set(table) == set(LANGS) and all(v.strip() for v in table.values())
    assert _holes(table["en"]) == _holes(table["de"]) == _holes(table["it"]), key
    assert _holes(table["en"]) <= _holes(table["fr"]), key


def test_french_constants_are_the_original_texts_word_for_word():
    assert CANCEL_REPLY == "Annulé à ta demande."
    assert FOLLOW_CHAIN_REPLY == ("Je m'arrête : {n} suites automatiques d'affilée sans message de ta part. "
                                  "Écris-moi pour que je reprenne.")
    assert PENDING_TURN_END == ("En attente de ta validation : une carte ({what}) attend déjà ta décision. Je ne "
                                "propose pas d'autre action avec le même outil avant ta réponse ; je reprendrai avec le "
                                "résultat.")
    assert BYOK_NOT_CONFIGURED_REPLY == (
        "Ta formule n'inclut pas le modèle de Nokker : configure ton modèle (point d'accès, modèle et clé) dans Mon "
        "compte → Modèles, puis renvoie ton message.\n\n"
        "Your plan does not include Nokker's model: set up your model (endpoint, model and key) in My account → "
        "Models, then send your message again.")
    assert APPROVAL_REPEAT_REPLY == ("Je m'arrête : la même action « {name} » vient d'être validée {n} fois de suite "
                                     "avec le même résultat. Je ne propose pas une carte identique de plus ; écris-moi "
                                     "pour que je reprenne.")
    assert ptx.BYOK_NOT_CONFIGURED["en"] == BYOK_NOT_CONFIGURED_REPLY.split("\n\n")[1]


def test_human_tool_in_the_four_languages_and_french_by_default():
    names = list(HUMAN_TOOLS) + ["infra_lvextend", "mcp__x__y", "bad « name »"]
    for n in names:
        assert human_tool(n) == human_tool(n, "fr")
    assert human_tool("terminal", "de") == "Terminal" and human_tool("write_file", "de") == "Datei"
    assert human_tool("mail_send", "it") == "e-mail" and human_tool("desktop_act", "en") == "desktop"
    for lang in ("en", "de", "it"):
        assert human_tool("mcp__evil", lang) == ptx.HUMAN_TOOL["none"][lang] != HUMAN_TOOL_FALLBACK
        assert "mcp__evil" not in human_tool("mcp__evil", lang)


def test_no_menu_path_in_german_and_italian():
    for lang in ("de", "it"):
        assert "→" not in ptx.BYOK_NOT_CONFIGURED[lang]


def test_http_byok_not_configured_is_in_the_request_language(tmp_path):
    httpd, port, api, kernel, _approvals, _tools, _model, _ = _server(tmp_path)
    kernel._byok_route = lambda: SimpleNamespace(provider=None, blocked="x", from_thread=False)

    def say(thread, lang):
        h = {"X-Session-Id": thread, **({"Accept-Language": lang} if lang else {})}
        status, body = _req(port, "/v1/chat/completions", token=api.token, headers=h, timeout=20,
                            body={"messages": [{"role": "user", "content": "salut"}]})
        return status, str(body)

    try:
        status, body = say("th_byok_de", "de")
        assert status == 200 and ptx.BYOK_NOT_CONFIGURED["de"] in body and "Ta formule" not in body, body
        status, body = say("th_byok_it", "it-CH")
        assert ptx.BYOK_NOT_CONFIGURED["it"] in body and "Ta formule" not in body, body
        status, body = say("th_byok_fr", None)
        assert "Ta formule n'inclut pas" in body and "Your plan does not include" in body
        assert "Dein Tarif" not in body
    finally:
        httpd.shutdown()


# ── tour du noyau : suites automatiques, carte en attente, annulation, action répétée ──


def test_follow_chain_and_pending_card_end_texts_follow_the_turn_language():
    from tests.unit.test_loop_contract import kernel, tc

    k, *_ = kernel([Message("assistant", f"suite {i}") for i in range(10)])
    k.follow_chain_max = 1
    replies = [k.run_turn(TurnInput("t-chain-de", f"[suite {i}]", control="desktop_continue", lang="de")).reply
               for i in range(3)]
    assert replies[0] == "suite 0" and replies[2] == ptx.FOLLOW_CHAIN["de"].format(n=2)
    k, *_ = kernel([Message("assistant", f"suite {i}") for i in range(10)])
    k.follow_chain_max = 1
    r = [k.run_turn(TurnInput("t-chain-fr", f"[suite {i}]", control="desktop_continue")).reply for i in range(3)]
    assert r[2] == FOLLOW_CHAIN_REPLY.format(n=2)

    script = [Message("assistant", tool_calls=[tc("infra_lvextend", size=str(i))]) for i in range(1, 4)]
    for lang, what in (("de", "nicht benannte Aktion"), ("it", "azione senza nome"), ("en", "unnamed action")):
        k, *_ = kernel(script + [Message("assistant", "non appelé")])
        k.max_calls, k.stall_limit = 20, 6
        turn = k.run_turn(TurnInput(f"t-pend-{lang}", "agrandis", lang=lang))
        assert turn.reply == ptx.PENDING_END[lang].format(what=what), turn.reply
    k, *_ = kernel(script + [Message("assistant", "non appelé")])
    k.max_calls, k.stall_limit = 20, 6
    assert k.run_turn(TurnInput("t-pend-fr", "agrandis")).reply == PENDING_TURN_END.format(what="action non nommée")


def test_cancelled_turn_says_so_in_the_turn_language():
    from tests.unit.test_cancel_stream import Sink, SlowStream, _provider
    from tests.unit.test_pause_steer import WAIT, kernel

    for lang, want in (("de", "Auf deinen Wunsch abgebrochen."), (None, "Annulé à ta demande.")):
        go, sent = threading.Event(), threading.Event()
        k, _, _ = kernel([], provider=_provider(SlowStream(go, sent), []))
        out = {}
        th = threading.Thread(target=lambda k=k, out=out, lang=lang: out.setdefault("turn", k.run_turn(
            TurnInput(thread_id="th_c", user_text="écris", channel="cli", lang=lang), stream=Sink())))
        th.start()
        assert sent.wait(WAIT)
        k.cancel("th_c")
        go.set()
        th.join(WAIT)
        assert out["turn"].reply == want and out["turn"].error == "cancelled"


def test_repeated_approved_action_stops_in_the_request_language():
    from tests.unit.approvals.test_repeat_chain import build, chain

    k, ex, backend, _prov = build([{"size": "1"}])
    set_request_lang("de")
    try:
        turns = chain(k, ex, backend, 3)
    finally:
        set_request_lang(None)
    assert turns[-1].reply == ptx.APPROVAL_REPEAT["de"].format(name="infra_lvextend", n=3)
