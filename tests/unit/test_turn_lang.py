"""Langue d'un tour (lot turn-lang) : `TurnInput.lang` posée à chaque point d'entrée, le noyau la pose pour la durée du tour,
la langue du compte puis la dernière langue reconnue du fil servent hors requête, les textes de panne existent en
fr/en/de/it, et le contexte envoyé au modèle ne change pas d'un octet. Vrais serveurs, vrai noyau, faux modèles."""
from __future__ import annotations

import re
import threading

import pytest

from nokker.api.server import account_lang_of
from nokker.approvals.rule_labels import (
    LANGS,
    current_lang,
    lang_scope,
    norm_lang,
    request_lang,
    resolve_lang,
    rule_label,
    set_request_lang,
)
from nokker.core import model_texts as mt
from nokker.core import person_texts as ptx
from nokker.core.loop import (
    _FAILURE_LABELS,
    BYOK_ACTIONS,
    BYOK_DOWN_REPLY,
    THREAD_MODEL_DOWN_REPLY,
    byok_down_reply,
    failure_person_text,
    person_failure_label,
    thread_model_down_reply,
)
from nokker.core.turn import Message, Tier, Turn, TurnInput
from nokker.qa.escalate import EscalatorV0, cannot_continue_text
from nokker.store.pg import turn_from_dict, turn_to_dict
from nokker.tenancy import MemoryTenancy
from nokker.tenancy.context import scoped_thread_id
from tests.unit.agents.conftest import make_world
from tests.unit.test_escalation_lang import RULE, _EscOnSwitch
from tests.unit.test_thread_serialization import _pending_card, _req, _server, _wait

DE = rule_label(RULE, "de")
IT = rule_label(RULE, "it")
FR = rule_label(RULE, "fr")
RETRY_FR, RETRY_DE = ptx.RETRY["fr"], ptx.RETRY["de"]


class Boom:
    """Modèle en panne d'amont (503)."""

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        raise RuntimeError("HTTP 503 de https://amont.example/v1")


# ─────────────────────────────── tables ───────────────────────────────


def _holes(text: str) -> set[str]:
    return set(re.findall(r"\{(\w+)\}", text))


@pytest.mark.parametrize("key", sorted(ptx.ALL_TABLES))
def test_every_table_has_the_four_languages_and_the_same_placeholders(key):
    table = ptx.ALL_TABLES[key]
    assert set(table) == set(LANGS)
    assert all(isinstance(v, str) and v.strip() for v in table.values())
    # l'anglais, l'allemand et l'italien disent la même chose avec les mêmes trous ; le français peut doubler (fr puis en)
    assert _holes(table["en"]) == _holes(table["de"]) == _holes(table["it"]), key
    assert _holes(table["en"]) <= _holes(table["fr"]), key


def test_failure_label_table_covers_the_nine_classes_and_keeps_the_frozen_french_and_english():
    assert set(ptx.FAILURE_LABELS) == set(_FAILURE_LABELS) and len(_FAILURE_LABELS) == 9
    for cls, (fr, en) in _FAILURE_LABELS.items():
        assert ptx.FAILURE_LABELS[cls]["fr"] == fr and ptx.FAILURE_LABELS[cls]["en"] == en
        assert ptx.BYOK_ACTION[ptx.BYOK_ACTION_OF[cls]]["fr"] == BYOK_ACTIONS[cls][0]
        assert ptx.BYOK_ACTION[ptx.BYOK_ACTION_OF[cls]]["en"] == BYOK_ACTIONS[cls][1]


@pytest.mark.parametrize("lang", ["en", "de", "it"])
def test_each_failure_class_is_said_in_the_language_without_french(lang):
    for cls in ptx.FAILURE_LABELS:
        exc = {"timeout": TimeoutError(), "key": RuntimeError("HTTP 401 de x"), "denied": RuntimeError("HTTP 403 de x"),
               "service": RuntimeError("HTTP 503 de x"), "limit": RuntimeError("HTTP 429 de x"),
               "model": RuntimeError("HTTP 404 de x : model absent"),
               "quota": RuntimeError('HTTP 403 de x : {"error":{"code":"quota_exceeded"}}'), "unreachable": ConnectionError("x"),
               "other": ValueError("x")}[cls]
        why, why_en = person_failure_label(exc, lang)
        assert why == ptx.FAILURE_LABELS[cls][lang] and why_en == ptx.FAILURE_LABELS[cls]["en"]
        reply = byok_down_reply("Mon modèle", exc, lang)
        byok_why = ptx.BYOK_QUOTA_WHY[lang] if cls == "quota" else why  # quota du fournisseur : motif BYOK propre
        assert "Mon modèle" in reply and byok_why in reply and "\n\n" not in reply
        assert ptx.BYOK_ACTION[ptx.BYOK_ACTION_OF[cls]][lang] in reply
        assert thread_model_down_reply("Mon modèle", exc, lang) == ptx.THREAD_MODEL_DOWN[lang].format(
            name="Mon modèle", why=why, why_en=why_en)


def test_french_texts_are_unchanged_word_for_word():
    exc = RuntimeError("HTTP 503 de x")
    assert byok_down_reply("M", exc, "fr") == byok_down_reply("M", exc) == BYOK_DOWN_REPLY.replace(
        "Vérifie le point d'accès et la clé dans Mon compte → Modèles, ou repasse sur le modèle de Nokker.",
        "Réessaie dans un moment, ou repasse sur le modèle de Nokker.").replace(
        "Check the endpoint and key in My account → Models, or switch back to Nokker's model.",
        "Try again in a moment, or switch back to Nokker's model.").format(
        name="M", why="service indisponible", why_en="service unavailable")
    assert thread_model_down_reply("M", exc, "fr") == THREAD_MODEL_DOWN_REPLY.format(
        name="M", why="service indisponible", why_en="service unavailable")
    assert cannot_continue_text("x", "fr") == "Je ne peux pas continuer : x." and cannot_continue_text("", "fr") == \
        "Je ne peux pas continuer."
    assert failure_person_text({"code": "x", "message": "m"}, "fr") == "Une erreur est survenue."


def test_neutral_and_cannot_continue_texts_follow_the_language():
    assert failure_person_text({"code": "x", "message": "brut"}, "de") == "Es ist ein Fehler aufgetreten."
    assert failure_person_text({"code": "x", "message": "brut"}, "it") == "Si è verificato un errore."
    assert failure_person_text({"code": "x", "message": "brut"}, "en") == "Something went wrong."
    assert failure_person_text({"code": "byok_not_configured",
                                "message": "aucun modèle configuré pour ta formule BYOK"}, "de").startswith("kein Modell")
    assert failure_person_text({"code": "no_model", "message": "lien avec jeton"}, "de") == "lien avec jeton"
    assert failure_person_text({"person_message": "déjà dit", "code": "x"}, "de") == "déjà dit"
    assert cannot_continue_text("a", "de") == "Ich kann nicht weitermachen: a."
    assert cannot_continue_text("", "it") == "Non posso continuare."
    with lang_scope("en"):
        assert cannot_continue_text("a") == "I can't continue: a."


# ─────────────────────────────── contextvar et résolution ───────────────────────────────


def test_lang_scope_sets_then_restores_and_resolve_order():
    assert current_lang() == "fr"
    with lang_scope("de"):
        assert current_lang() == "de"
        with lang_scope(None):
            assert current_lang() == "de"
    assert current_lang() == "fr"
    assert resolve_lang(explicit="en", account="de", thread="it") == "en"
    assert resolve_lang(account="de", thread="it") == "de"
    assert resolve_lang(thread="it") == "it"
    assert resolve_lang() == "fr" and resolve_lang(explicit="xx", account="zz") == "fr"
    assert norm_lang("de-CH") == "de" and norm_lang("pt") is None


def test_request_lang_is_none_without_header_and_set_with_one():
    set_request_lang(None, None)
    assert request_lang() is None and current_lang() == "fr"
    set_request_lang("it-IT,it;q=0.9", None)
    assert request_lang() == "it"
    set_request_lang(None, None)


def test_account_lang_reads_a_posed_language_only():
    ten = MemoryTenancy()
    acc = ten.create_account("a@x.ch")
    fn = account_lang_of(ten)
    assert acc.lang is None and fn(acc.account_id) is None  # rien posé : NULL (migration 089)
    ten.set_lang(acc.account_id, "de")
    assert fn(acc.account_id) == "de" and fn("local-owner") is None and fn("us_inconnu") is None
    assert ten.set_lang(acc.account_id, "fr").lang == "fr" and fn(acc.account_id) == "fr"  # fr posé : un choix
    assert ten.set_lang(acc.account_id, None).lang is None and fn(acc.account_id) is None  # effacé : pas posée
    for lang in ("en", "it"):
        assert ten.set_lang(acc.account_id, lang).lang == lang
    assert ten.set_lang("us_inconnu", None) is None
    with pytest.raises(ValueError):
        ten.set_lang(acc.account_id, "")
    with pytest.raises(ValueError):
        ten.set_lang(acc.account_id, "pt")
    assert account_lang_of(None) is None


def test_turn_roundtrip_keeps_the_seen_language_only_when_set():
    t = Turn(thread_id="t", index=0, input=__import__("nokker.core.turn", fromlist=["InputCheck"]).InputCheck(text="x"),
             lang="de", lang_seen="de")
    d = turn_to_dict(t)
    assert d["lang"] == "de" and turn_from_dict(d).lang_seen == "de" and turn_from_dict(d).lang == ""
    t.lang_seen = ""
    assert "lang" not in turn_to_dict(t) and turn_from_dict(turn_to_dict(t)).lang_seen == ""


# ─────────────────────────────── le vrai serveur ───────────────────────────────


def _chat_lang(port, token, thread, text, lang=None):
    headers = {"X-Session-Id": thread, **({"Accept-Language": lang} if lang else {})}
    return _req(port, "/v1/chat/completions", token=token, headers=headers, timeout=20,
                body={"messages": [{"role": "user", "content": text}]})


def test_http_thread_remembers_the_last_recognized_language_and_background_turns_find_it(tmp_path):
    httpd, port, api, kernel, _approvals, _tools, model, _ = _server(tmp_path)
    model.release.set()
    kernel.gates = _EscOnSwitch(kernel.gates)
    try:
        status, body = _chat_lang(port, api.token, "th_tl1", "bonjour", "de-CH,de;q=0.9")
        assert status == 200 and DE in str(body) and FR not in str(body)
        assert [t.lang_seen for t in kernel.store.history("th_tl1")] == ["de"]
        # même fil, requête SANS en-tête : la dernière langue reconnue (de) sert
        status, body = _chat_lang(port, api.token, "th_tl1", "encore", None)
        assert status == 200 and DE in str(body) and FR not in str(body), body
        assert [t.lang_seen for t in kernel.store.history("th_tl1")] == ["de", ""]  # rien reconnu : rien mémorisé
        # tâche de fond d'un fil de, hors requête : le noyau relit le fil
        done = threading.Event()
        out: list = []

        def background():
            out.append(kernel.run_turn(TurnInput(thread_id="th_tl1", user_text="fond", channel="goal")))
            done.set()

        threading.Thread(target=background).start()
        assert done.wait(10) and DE in out[0].reply and out[0].lang == "de"
        # autre fil, aucune langue nulle part : français
        status, body = _chat_lang(port, api.token, "th_tl2", "bonjour", None)
        assert status == 200 and FR in str(body) and DE not in str(body)
        assert request_lang() is None  # le contexte de ce test n'a rien gardé
    finally:
        httpd.shutdown()


def test_http_card_suite_without_header_uses_the_thread_language(tmp_path):
    httpd, port, api, kernel, approvals, _tools, model, _ = _server(tmp_path)
    model.release.set()
    kernel.gates = _EscOnSwitch(kernel.gates, only_suite=True)
    thread = "th_tl3"
    try:
        out: list = []
        out.append(_chat_lang(port, api.token, thread, "agrandis le volume", "de"))
        card = _pending_card(approvals, thread)
        status, _b = _req(port, f"/v1/local/approvals/{card.id}/decide", token=api.token,
                          body={"decision": "approve", "scope": "once"})  # aucun en-tête de langue
        assert status in (200, 202)
        assert _wait(lambda: len(kernel.store.history(thread)) == 2)
        suite = kernel.store.history(thread)[1]
        text = " ".join(m.content or "" for m in suite.messages) + (suite.reply or "")
        assert DE in text and FR not in text, text
        assert suite.lang_seen == ""  # une suite de carte n'est pas un message de la personne
    finally:
        httpd.shutdown()


def test_http_account_language_serves_turns_without_any_header(tmp_path):
    httpd, port, api, kernel, _approvals, _tools, model, _ = _server(tmp_path)
    model.release.set()
    kernel.gates = _EscOnSwitch(kernel.gates)
    kernel.account_lang = lambda sender: "it"
    try:
        status, body = _chat_lang(port, api.token, "th_tl4", "ciao", None)
        assert status == 200 and IT in str(body) and FR not in str(body), body
        status, body = _chat_lang(port, api.token, "th_tl5", "hallo", "de")  # ce que la personne écrit gagne
        assert DE in str(body) and IT not in str(body)
    finally:
        httpd.shutdown()


def test_http_model_down_is_told_in_the_language(tmp_path):
    httpd, port, api, kernel, _approvals, _tools, _model, _ = _server(tmp_path)
    kernel.providers = {Tier.SHIP: Boom()}
    kernel.escalator = EscalatorV0()
    try:
        status, body = _chat_lang(port, api.token, "th_tl6", "salut", "de")
        text = str(body)
        assert status == 200 and "Ich kann nicht weitermachen" in text and "Dienst nicht verfügbar" in text
        assert RETRY_DE in text and RETRY_FR not in text and "Je ne peux pas" not in text
        status, body = _chat_lang(port, api.token, "th_tl7", "salut", None)
        assert "Je ne peux pas continuer" in str(body) and RETRY_FR in str(body) and "service indisponible" in str(body)
    finally:
        httpd.shutdown()


# ─────────────────────────────── agents, objectifs, canaux ───────────────────────────────


def test_agent_run_resolves_the_account_language_at_submission(tmp_path):
    w = make_world(tmp_path)
    w.kernel.account_lang = lambda sender: "de" if sender == "alice" else None
    w.model.complete = Boom().complete
    a = w.agents.create_from_body({"name": "Veille", "role": "scout", "instructions": "Cherche."}, "alice")
    w.agents.run_now(a.id, "alice")
    run = w.runs(a.id)[0]
    turn = w.kernel.store.history(run.thread_id)[0]
    assert turn.lang == "de" and turn.lang_seen == ""  # langue du compte, pas reconnue sur un message humain
    assert ptx.FAILURE_LABELS["service"]["de"] in turn.reply and "service indisponible" not in turn.reply


def test_agent_run_without_account_language_or_thread_is_french(tmp_path):
    w = make_world(tmp_path)
    w.model.complete = Boom().complete
    a = w.agents.create_from_body({"name": "Veille", "role": "scout", "instructions": "Cherche."}, "alice")
    w.agents.run_now(a.id, "alice")
    turn = w.kernel.store.history(w.runs(a.id)[0].thread_id)[0]
    assert turn.lang == "fr" and "service indisponible" in turn.reply


def test_goal_turn_carries_the_resolved_language(tmp_path):
    from tests.unit.goals.test_outcomes_goal import drain, kernel_with, service

    svc = service("C1: 1 | non")
    k, _ = kernel_with([Message("assistant", "ok")], svc)
    k.account_lang = lambda sender: "de"
    svc.start("t1", "alice", "objectif", criteria=["c"])
    seen = {}
    svc.run_turn = lambda rk, inp: seen.update(lang=inp.lang, recognized=inp.lang_recognized)
    drain(svc)
    assert seen == {"lang": "de", "recognized": False}


def test_telegram_language_code_is_the_turn_language_and_is_remembered(tmp_path):
    from nokker.telegram.adapter import TelegramAdapter
    from nokker.telegram.pairing import MemoryPairing
    from tests.unit.telegram.test_adapter import FakeBot, msg

    w = make_world(tmp_path)
    w.model.complete = Boom().complete
    tenancy, bot = MemoryTenancy(), FakeBot()
    acc = tenancy.create_account("a@x.ch")
    tenancy.bind_identity(acc.account_id, "telegram", "111")
    adapter = TelegramAdapter(tenancy=tenancy, pairing=MemoryPairing(), run_turn=w.kernel.run_turn, bot=bot)
    adapter.handle_update = lambda u: adapter._process(u["message"])
    adapter.handle_update(msg(111, "salut", lang="de-CH"))
    assert "Modell von Nokker antwortet nicht" in bot.sent[-1][1] and ptx.FAILURE_LABELS["service"]["de"] in bot.sent[-1][1]
    (turn,) = w.kernel.store.history(scoped_thread_id(acc.tenant_id, "tg-111"))
    assert turn.lang == "de" and turn.lang_seen == "de"
    # langue de client inconnue : le noyau retrouve la dernière langue reconnue du fil (de)
    adapter.handle_update(msg(111, "salut", lang="pt"))
    assert "Modell von Nokker antwortet nicht" in bot.sent[-1][1]
    assert [t.lang_seen for t in w.kernel.store.history(scoped_thread_id(acc.tenant_id, "tg-111"))] == ["de", ""]


def test_channel_message_language_is_passed_to_the_turn_and_unset_stays_open():
    from nokker.channels.common import ChannelCore, Inbound, MemoryPairing

    ten, sent, seen = MemoryTenancy(), [], []
    acc = ten.create_account("a@x.ch")
    ten.bind_identity(acc.account_id, "slack", "T1:U1")

    def run_turn(inp):
        seen.append((inp.lang, inp.lang_recognized))
        return Turn(thread_id=inp.thread_id, index=0, input=None, reply="ok")

    core = ChannelCore(channel="slack", display_name="Slack", tenancy=ten, pairing=MemoryPairing(), run_turn=run_turn,
                       scrub=lambda t: t)
    for lang in ("de", None):
        core.process(Inbound(channel="slack", identity="T1:U1", conversation="c", thread_key="k", text="hi",
                             reply=sent.append, lang=lang))
    assert seen == [("de", True), (None, False)]  # sans langue annoncée : le noyau résout (compte, fil, fr)


# ─────────────────────────────── le contexte du modèle ne change pas ───────────────────────────────


_DATE_BLOCK = re.compile(r"\[[^\]\n]+\]\n[^\n]*\(Europe/Zurich\) — ISO [^\n]*\n\[/Date\]")


_LANG_LINE = re.compile("\n\n(?:" + "|".join(re.escape(v) for v in mt.ALL_TABLES["context.reply_language"].values()) + ")")


def _without_date(calls):
    """Messages envoyés au modèle, l'en-tête de date (libellé et jour dans la langue du tour : lot 5a) masqué : tout le reste
    (préfixe, outils, historique, message) ne dépend pas de la langue. La ligne de langue du tour (`context.reply_language`, en-tête
    figé, jamais le préfixe) est masquée aussi (absente sans signal de langue, présente avec)."""
    return [[{**m, "content": _LANG_LINE.sub("", _DATE_BLOCK.sub("[date]", m["content"]))
              if isinstance(m.get("content"), str) else m.get("content")}
             for m in call] for call in calls]


def test_context_sent_to_the_model_is_identical_whatever_the_language(tmp_path):
    seen = {}
    for lang in ("fr", "de"):
        (tmp_path / lang).mkdir()
        w = make_world(tmp_path / lang, [("tool", "web_search", {"q": "x"}), ("text", "ok")])
        w.kernel.run_turn(TurnInput(thread_id="t1", user_text="bonjour", sender="alice", lang=lang,
                                    lang_recognized=True))
        w.kernel.run_turn(TurnInput(thread_id="t1", user_text="et ensuite", sender="alice", lang=lang))
        seen[lang] = (w.model.seen, [c.prefix_sha256 for t in w.kernel.store.history("t1") for c in t.calls])
    assert _without_date(seen["fr"][0]) == _without_date(seen["de"][0]) and seen["fr"][1] == seen["de"][1] and seen["fr"][0]


def test_prompt_prefix_is_independent_of_the_request_language(tmp_path):
    msgs = {}
    for lang in (None, "de"):
        (tmp_path / str(lang)).mkdir()
        w = make_world(tmp_path / str(lang), [("text", "ok")])
        set_request_lang(lang, None)
        try:
            w.kernel.run_turn(TurnInput(thread_id="t1", user_text="bonjour", sender="alice"))
        finally:
            set_request_lang(None, None)
        msgs[lang] = w.model.seen
    assert _without_date(msgs[None]) == _without_date(msgs["de"])


def test_kernel_restores_the_language_after_the_turn(tmp_path):
    w = make_world(tmp_path, [("text", "ok")])
    w.kernel.run_turn(TurnInput(thread_id="t1", user_text="bonjour", sender="alice", lang="it"))
    assert current_lang() == "fr"


# ─────────────────────── accounts.lang NULL : « fr » posé ≠ « rien posé » (migration 089) ───────────────────────


def test_account_creation_without_language_is_null_and_reads_as_french():
    ten = MemoryTenancy()
    assert ten.create_account("n@x.ch").lang is None
    assert ten.create_account("f@x.ch", "fr").lang is None  # « fr » de l'invitation ou du navigateur : le défaut, pas un choix
    assert ten.create_account("e@x.ch", "en").lang == "en"
    assert ten.create_account("d@x.ch", "de").lang == "de" and ten.create_account("i@x.ch", "it").lang == "it"
    assert ten.create_account("p@x.ch", "pt").lang is None


def test_kernel_resolve_lang_explicit_fr_account_beats_the_thread_but_null_does_not():
    from nokker.core.loop import Kernel

    k = Kernel.__new__(Kernel)
    k.account_lang = None
    k.thread_lang = lambda thread_id: "de"
    ten = MemoryTenancy()
    nul = ten.create_account("nul@x.ch")
    fr = ten.create_account("fr@x.ch")
    ten.set_lang(fr.account_id, "fr")
    k.account_lang = account_lang_of(ten)
    assert k.resolve_lang("th", nul.account_id) == "de"  # pas posée : le fil décide
    assert k.resolve_lang("th", fr.account_id) == "fr"  # fr posé : il garde fr
    assert k.resolve_lang("th", fr.account_id, explicit="it") == "it"  # ce que la personne écrit gagne toujours
    assert k.resolve_lang("th", None) == "de"


def _acct_http(tmp_path, tenancy, accounts):
    """Vrai serveur, vrai noyau : un jeton par compte (le `sender` est l'identifiant `us_…`)."""
    from http.server import ThreadingHTTPServer

    from nokker.api.events import EventBus
    from nokker.api.server import build_hub, make_handler
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.config import NokkerConfig
    from nokker.core.ports import RouteDecision
    from nokker.local.api import LOCAL_OWNER
    from tests.unit.api.test_approvals_routes import make_kernel_with_approvals
    from tests.unit.test_local_api import _api
    from tests.unit.test_thread_serialization import HistoryContext, SlowModel

    approvals = InMemoryApprovals()
    kernel, _tools = make_kernel_with_approvals(approvals)
    model = SlowModel()
    model.release.set()
    kernel.context = HistoryContext()
    kernel.providers = {Tier.SHIP: model}

    class Pol:
        def choose(self, state):
            return RouteDecision(tier=Tier.SHIP, reasoning=False, reason="t")

    kernel.policy = Pol()
    kernel.gates = _EscOnSwitch(kernel.gates)
    kernel.account_lang = account_lang_of(tenancy)
    hub = build_hub(NokkerConfig(web_search_enabled=False, approvals_enabled=False), EventBus())
    hub.kernel = kernel
    kernel.observer = hub
    api = _api(tmp_path)
    keys = {api.token: LOCAL_OWNER, **{f"tok-{a.account_id}": a.account_id for a in accounts}}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, keys, approvals=approvals, local_api=api, hub=hub,
                                                               tenancy=tenancy))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port, kernel


def test_http_explicit_french_account_keeps_french_but_unset_account_follows_what_it_writes(tmp_path):
    ten = MemoryTenancy()
    fr = ten.create_account("fr@x.ch")
    ten.set_lang(fr.account_id, "fr")
    nul = ten.create_account("nul@x.ch")
    httpd, port, _k = _acct_http(tmp_path, ten, [fr, nul])
    try:
        # compte fr explicite, message en allemand (en-tête de requête absent, fil déjà reconnu en de) : fr
        s, b = _chat_lang(port, f"tok-{fr.account_id}", "th_fr1", "hallo", "de-CH,de;q=0.9")
        assert s == 200 and DE in str(b), b  # ce que la personne écrit (en-tête) l'emporte sur tout, y compris sur fr
        s, b = _chat_lang(port, f"tok-{fr.account_id}", "th_fr1", "encore", None)
        assert s == 200 and FR in str(b) and DE not in str(b), b  # sans en-tête : fr posé prime sur le fil de
        # compte sans langue : le fil (de) sert
        s, b = _chat_lang(port, f"tok-{nul.account_id}", "th_nul1", "hallo", "de-CH,de;q=0.9")
        assert s == 200 and DE in str(b)
        s, b = _chat_lang(port, f"tok-{nul.account_id}", "th_nul1", "encore", None)
        assert s == 200 and DE in str(b) and FR not in str(b), b
    finally:
        httpd.shutdown()


def test_migration_089_is_idempotent_and_guarded():
    from nokker.store.pg import migration_files

    sql = next(p for p in migration_files() if p.name == "089_account_lang_null.sql").read_text(encoding="utf-8")
    assert "DROP NOT NULL" in sql and "SET DEFAULT NULL" in sql and "lang = 'fr'" in sql
    assert "is_nullable" in sql  # garde : un fr posé après la migration n'est jamais effacé au redémarrage


def test_german_account_reaches_mail_and_stripe_locale_without_breaking_them():
    from nokker.billing.stripe import StripeClient  # noqa: F401 — import de contrôle

    ten = MemoryTenancy()
    de = ten.create_account("d2@x.ch", "de")
    assert de.lang == "de" and (de.lang or "fr") == "de"
    from nokker.onboarding.emailcode import EmailCodes

    assert EmailCodes._mail_lang(type("S", (), {"_invitation_lang": lambda self, e: None})(), de, de.email, "en") == "en"
