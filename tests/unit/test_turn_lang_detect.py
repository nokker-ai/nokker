"""Langue d'un tour SANS `Accept-Language` ni langue de compte (client terminal, Lokkal) : le message de la personne la
dit. Ordre : requête > compte > message détecté > fil > `NOKKER_LANG` > fr. Un message court (« ok ») ne bascule rien."""
from __future__ import annotations

import pytest

from nokker.approvals import rule_labels
from nokker.core.lang_detect import detect_lang
from nokker.core.turn import Message, TurnInput
from tests.unit.test_loop_contract import kernel

EN = "Hello, can you check the logs and tell me why the build is failing on my machine please?"
FR = "Bonjour, peux-tu regarder les journaux et me dire pourquoi la compilation échoue sur ma machine ?"
DE = "Hallo, kannst du bitte die Protokolle prüfen und mir sagen, warum der Build auf meinem Rechner fehlschlägt?"
IT = "Ciao, puoi controllare i log e dirmi perché la compilazione non riesce sul mio computer per favore?"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("NOKKER_LANG", raising=False)
    rule_labels.set_request_lang(None, None)
    yield
    rule_labels.set_request_lang(None, None)


@pytest.mark.parametrize("text,lang", [(EN, "en"), (FR, "fr"), (DE, "de"), (IT, "it"),
                                       ("yes please", "en"), ("ja bitte", "de"), ("sì grazie", "it")])
def test_a_clear_message_is_detected(text, lang):
    assert detect_lang(text) == lang


@pytest.mark.parametrize("text", ["ok", "oui", "merci", "Bonjour", "thanks", "", "   ", "42", "```python\nprint('hello you')\n```",
                                  "https://example.com/the/and/you", "ok, merci"])
def test_a_short_or_ambiguous_message_is_not_detected(text):
    assert detect_lang(text) is None


def test_code_and_links_do_not_count():
    assert detect_lang("Peux-tu corriger ça ?\n```\nthe and you are with for this that have\n```") == "fr"


def _k():
    k, *_ = kernel([Message("assistant", "ok")] * 8)
    return k


@pytest.mark.parametrize("text,lang", [(EN, "en"), (FR, "fr"), (DE, "de"), (IT, "it")])
def test_no_header_no_account_follows_the_message(text, lang):
    assert _k().resolve_lang("t1", None, text=text) == lang


def test_a_short_message_keeps_the_language_of_the_thread():
    k = _k()
    turn = k.run_turn(TurnInput("t1", EN))
    assert turn.lang_seen == "en"
    assert k.resolve_lang("t1", None, text="ok") == "en"
    turn2 = k.run_turn(TurnInput("t1", "ok"))
    assert turn2.lang_seen == ""  # rien de reconnu : la langue du fil n'est pas réécrite
    assert k.resolve_lang("t1", None, text="oui") == "en"


def test_the_person_is_followed_when_she_changes_language_mid_thread():
    k = _k()
    assert k.run_turn(TurnInput("t1", EN)).lang_seen == "en"
    assert k.run_turn(TurnInput("t1", FR)).lang_seen == "fr"
    assert k.resolve_lang("t1", None, text="ok") == "fr"
    assert k.resolve_lang("t1", None, text=DE) == "de"


def test_a_short_first_message_falls_back_to_the_installation_then_french(monkeypatch):
    k = _k()
    assert k.resolve_lang("t1", None, text="ok") == "fr"
    monkeypatch.setenv("NOKKER_LANG", "en")
    assert k.resolve_lang("t1", None, text="ok") == "en"
    assert k.resolve_lang("t1", None, text=FR) == "fr"  # le message prime sur l'installation
    monkeypatch.setenv("NOKKER_LANG", "zz")
    assert k.resolve_lang("t1", None, text="ok") == "fr"


def test_an_established_thread_beats_the_installation_for_a_short_message(monkeypatch):
    k = _k()
    k.run_turn(TurnInput("t1", EN))
    monkeypatch.setenv("NOKKER_LANG", "fr")
    assert k.resolve_lang("t1", None, text="ok") == "en"


def test_the_account_language_beats_the_detection_and_the_request_beats_the_account():
    k = _k()
    k.account_lang = lambda sender: "de"
    assert k.resolve_lang("t1", "us_a", text=EN) == "de"
    assert k.resolve_lang("t1", None, text=EN) == "en"  # pas d'expéditeur : pas de compte
    assert k.resolve_lang("t1", "us_a", explicit="it", text=EN) == "it"
    rule_labels.set_request_lang("fr-CH", None)
    assert k.resolve_lang("t1", "us_a", text=EN) == "fr"


def test_a_control_turn_text_is_neither_detected_nor_remembered():
    k = _k()
    turn = k.run_turn(TurnInput("t1", EN, control="goal_continue"))
    assert turn.lang_seen == ""


def test_the_pure_order():
    r = rule_labels.resolve_lang
    assert r() == "fr"
    assert r(installation="de") == "de"
    assert r(thread="it", installation="de") == "it"
    assert r(detected="en", thread="it", installation="de") == "en"
    assert r(account="de", detected="en") == "de"
    assert r(explicit="it", account="de", detected="en") == "it"
