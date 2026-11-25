"""De bout en bout, SANS `Accept-Language` (client terminal) et sans langue de compte (Lokkal) : vrai noyau, vraie boucle,
vrai moteur de contexte avec bloc d'environnement, faux fournisseur qui capte les messages envoyés au modèle. Tout ce que
le noyau y injecte (date, environnement, attente de carte, refus) est dans la langue du message de la personne."""
from __future__ import annotations

from datetime import datetime

import pytest

from nokker.api.server import self_context_hook
from nokker.approvals import rule_labels
from nokker.context.engine import ZURICH, ContextEngineV0
from nokker.core.loop import Kernel
from nokker.core.ports import Completion
from nokker.core.turn import Message, Tier, TurnInput, Usage
from tests.unit.test_loop_contract import Esc, GatesFake, Inv, LedgerFake, Mem, Pol, StoreFake, Tools, tc

EN = "Hello, can you extend the disk and send the promo to the whole list please? Thanks for your help."
FR = "Bonjour, peux-tu agrandir le disque et envoyer la promo à toute la liste s'il te plaît ? Merci pour ton aide."
DE = "Hallo, kannst du bitte die Festplatte vergrössern und die Aktion an die ganze Liste senden? Danke für deine Hilfe."
IT = "Ciao, puoi per favore estendere il disco e inviare la promozione a tutta la lista? Grazie per il tuo aiuto."
NOW = datetime(2026, 11, 18, 14, 5, tzinfo=ZURICH)

# Par langue : libellé de date, jour, ligne d'exécution, canal, attente de carte, refus.
EXPECT = {
    "fr": ("[Date et heure actuelles]", "mercredi 18 novembre 2026", "Exécution : Lokkal", "Canal : terminal",
           "EN ATTENTE D'APPROBATION", "Refusé"),
    "en": ("[Current date and time]", "Wednesday, November 18, 2026", "Running on: Lokkal", "Channel: terminal",
           "AWAITING HUMAN APPROVAL", "Refused"),
    "de": ("[Aktuelles Datum und Uhrzeit]", "Mittwoch, 18. November 2026", "Ausführung: Lokkal", "Kanal: Terminal",
           "WARTET AUF FREIGABE", "Abgelehnt"),
    "it": ("[Data e ora attuali]", "mercoledì 18 novembre 2026", "Esecuzione: Lokkal", "Canale: terminale",
           "IN ATTESA DI APPROVAZIONE", "Rifiutato"),
}


class Capture:
    """Faux fournisseur : capte les messages de chaque appel ; 1er appel de chaque tour = deux outils (carte + refus)."""

    def __init__(self):
        self.bodies: list[list[dict]] = []

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        self.bodies.append([dict(m) for m in messages])
        msg = Message("assistant", tool_calls=[tc("infra_lvextend"), tc("send_bulk_email")]) \
            if messages[-1]["role"] != "tool" else Message("assistant", "ok")
        return Completion(message=msg, usage=Usage(prompt=100), model="capture", latency_s=0.01)


def _lokkal(tmp_path, account_lang=None):
    prefix = tmp_path / "prefix.md"
    prefix.write_text("Tu es Atlas.")
    inv, tools = Inv(), Tools()
    engine = ContextEngineV0(tools=tools, prefix_path=str(prefix), now_fn=lambda: NOW)
    engine.self_context = self_context_hook(local_api=object())  # une machine Lokkal, sans compte
    provider = Capture()
    k = Kernel(store=StoreFake(), context=engine, memory=Mem(), invariants=inv, gates=GatesFake(inv), escalator=Esc(),
               policy=Pol(), providers={Tier.SHIP: provider, Tier.DEEP: provider}, tools=tools, ledger=LedgerFake())
    k.account_lang = (lambda sender: account_lang) if account_lang else None
    return k, provider


@pytest.fixture(autouse=True)
def _no_header(monkeypatch):
    monkeypatch.delenv("NOKKER_LANG", raising=False)
    rule_labels.set_request_lang(None, None)
    yield
    rule_labels.set_request_lang(None, None)


def _seen(provider):
    """Messages `user` (dernier de chaque appel) et résultats d'outils de tout ce qui est parti au modèle."""
    users = [next(m["content"] for m in reversed(b) if m["role"] == "user") for b in provider.bodies]
    tools = [m["content"] for b in provider.bodies for m in b if m["role"] == "tool"]
    return users, tools


def _inp(text, **kw):
    return TurnInput("th1", text, channel="api", sender=None, surface="cli", **kw)


def _assert_all_in(lang, provider):
    label, day, place, channel, pending, refused = EXPECT[lang]
    users, tools = _seen(provider)
    assert users and tools
    for u in users:
        assert u.startswith(f"{label}\n{day}, 14:05") and place in u and channel in u
    assert any(t.startswith(pending) for t in tools) and any(t.startswith(refused) for t in tools)
    for other, (olabel, _d, oplace, ochannel, opending, orefused) in EXPECT.items():
        if other == lang:
            continue
        blob = "\n".join([*users, *tools])
        assert olabel not in blob and oplace not in blob and ochannel not in blob
        assert opending not in blob and not any(t.startswith(orefused) for t in tools), other


@pytest.mark.parametrize("lang,text", [("en", EN), ("fr", FR), ("de", DE), ("it", IT)])
def test_no_header_no_account_the_whole_turn_follows_the_message(tmp_path, lang, text):
    k, provider = _lokkal(tmp_path)
    turn = k.run_turn(_inp(text))
    assert turn.lang_seen == lang
    _assert_all_in(lang, provider)


def test_a_short_second_turn_keeps_the_language(tmp_path):
    k, provider = _lokkal(tmp_path)
    k.run_turn(_inp(EN))
    n = len(provider.bodies)
    turn = k.run_turn(_inp("ok"))
    assert turn.lang_seen == ""  # rien reconnu dans « ok » : la langue du fil reste celle du tour 1
    second_head = next(m["content"] for m in reversed(provider.bodies[n]) if m["role"] == "user")
    assert second_head.startswith("[Current date and time]\nWednesday, November 18, 2026") and "Running on: Lokkal" in second_head
    assert k.resolve_lang("th1", None) == "en"  # la suite d'une carte décidée la résout comme un tour ordinaire


def test_the_person_changing_language_is_followed_and_the_old_head_is_replayed_as_sent(tmp_path):
    k, provider = _lokkal(tmp_path)
    k.run_turn(_inp(FR))
    k.run_turn(_inp(DE))
    users = next(b for b in reversed(provider.bodies))
    heads = [m["content"] for m in users if m["role"] == "user"]
    assert heads[0].startswith("[Date et heure actuelles]\nmercredi") and heads[-1].startswith("[Aktuelles Datum und Uhrzeit]\nMittwoch")


def test_accept_language_beats_the_detection(tmp_path):
    k, provider = _lokkal(tmp_path)
    rule_labels.set_request_lang("de-CH", None)
    k.run_turn(_inp(EN, lang=rule_labels.request_lang(), lang_recognized=True))
    _assert_all_in("de", provider)


def test_the_account_language_beats_the_detection(tmp_path):
    k, provider = _lokkal(tmp_path, account_lang="it")
    k.run_turn(TurnInput("th1", EN, channel="api", sender="us_a", surface="cli"))
    _assert_all_in("it", provider)


def test_a_lokkal_without_account_and_a_short_first_message_uses_the_installation(tmp_path, monkeypatch):
    monkeypatch.setenv("NOKKER_LANG", "en")
    k, provider = _lokkal(tmp_path)
    k.run_turn(_inp("ok"))
    users, _ = _seen(provider)
    assert users[0].startswith("[Current date and time]\n")
