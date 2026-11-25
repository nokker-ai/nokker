"""Langue du message d'attente d'une carte (18.11.2026, rc.9.38 : invite anglaise, carte posée, attente rédigée en
français). Drapeau `reply_language_tail` allumé : le texte d'attente (résultat d'outil) se termine par un rappel de langue
dans la langue du tour, et la ligne de langue de l'en-tête suit cette langue ; éteint, rien ne change. Carte tranchée
PENDANT le tour : pas de texte d'attente, pas de rappel. Suite d'une carte (`APPROVAL_CONTINUE`, sans message de la
personne) : la langue du FIL, pas le français par défaut."""
from __future__ import annotations

import threading

import pytest

from nokker.approvals.executor import ApprovalExecutor
from nokker.approvals.memory import InMemoryApprovals
from nokker.approvals.rule_labels import current_lang
from nokker.context.reply_language import TAIL_LINE, tail_line
from nokker.core import model_texts as mt
from nokker.core.ports import Completion
from nokker.core.turn import APPROVAL_CONTINUE, Message, ToolResult, TurnInput, Usage
from tests.unit.test_loop_approvals import make_kernel, tc

WAITING = {"fr": "J'attends ta décision sur la carte.", "en": "I'm waiting for your decision on the card.",
           "de": "Ich warte auf deine Entscheidung auf der Karte.", "it": "Aspetto la tua decisione sulla scheda."}


class LangModel:
    """Faux fournisseur : premier appel → l'outil ; ensuite, le message d'attente dans la langue que DEMANDE la fin du
    dernier résultat d'outil (rappel `gate.pending_lang`), sinon dans la langue du préfixe (français) : le défaut observé."""

    def __init__(self, first):
        self.first, self.n, self.seen_lang, self.seen_tail = first, 0, [], []

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        self.n += 1
        self.seen_lang.append(current_lang())
        self.seen_tail.append(tail_line())
        if self.n == 1 and self.first is not None:
            return Completion(message=self.first, usage=Usage(prompt=10), model="m", latency_s=0.0)
        tool_texts = [m["content"] for m in messages if m["role"] == "tool"]
        asked = next((lang for lang, line in mt.PENDING_LANG.items() if tool_texts and tool_texts[-1].endswith(line)),
                     "fr")
        return Completion(message=Message("assistant", WAITING[asked]), usage=Usage(prompt=10), model="m",
                          latency_s=0.0)


def _kernel(first, *, tail=True, wait_s=0.0):
    backend = InMemoryApprovals()
    k, tools = make_kernel([], approvals=backend)
    k.context.reply_language_tail = tail
    model = LangModel(first)
    k.providers = {next(iter(k.providers)): model}
    if wait_s:
        executor = ApprovalExecutor(kernel=k, approvals=backend)
        k.approval_inline, k.approval_wait_s = executor.settle_in_turn, wait_s
    return k, tools, backend, model


FIRST = Message("assistant", tool_calls=[tc("infra_lvextend", size="150G")])


@pytest.mark.parametrize("lang", ["en", "de", "it", "fr"])
def test_waiting_message_follows_the_language_of_the_turn(lang):
    k, tools, backend, model = _kernel(FIRST)
    turn = k.run_turn(TurnInput("fil", "grow the volume", lang=lang, lang_recognized=True))
    pending = [m.content for m in turn.messages if m.role == "tool"]
    assert tools.called == [] and len(pending) == 1 and len(backend.pending()) == 1
    assert pending[0].startswith(mt.marks(mt.PENDING, head=" (")[mt.LANGS.index(lang)])  # début reconnu inchangé
    assert pending[0].endswith(" " + mt.PENDING_LANG[lang])
    assert turn.reply == WAITING[lang]
    assert model.seen_tail[-1] == mt.REPLY_LANG_TAIL[lang]  # la ligne de l'en-tête suit la langue du tour


def test_flag_off_changes_nothing():
    k, _tools, _b, model = _kernel(FIRST, tail=False)
    turn = k.run_turn(TurnInput("fil", "grow the volume", lang="en", lang_recognized=True))
    pending = [m.content for m in turn.messages if m.role == "tool"]
    assert pending == [mt.PENDING["en"].format(why=pending[0].split("(", 1)[1].split(")", 1)[0],
                                               approval_id=turn.approval_ids[0])]
    assert not any(pending[0].endswith(line) for line in mt.PENDING_LANG.values())
    assert turn.reply == WAITING["fr"]  # le défaut d'origine, sans le rappel


def test_french_tail_line_is_the_original_line_byte_for_byte():
    assert mt.REPLY_LANG_TAIL["fr"] == TAIL_LINE  # le français (et son empreinte de préfixe de tour) ne bouge pas


@pytest.mark.parametrize("lang", ["en", "de", "it"])
def test_card_decided_during_the_turn_has_no_waiting_text_and_no_reminder(lang):
    k, tools, backend, model = _kernel(FIRST, wait_s=3.0)

    def call(n, a, ctx):
        tools.called.append(n)
        return ToolResult(call_id="", name=n, content="Volume grown to 150G.")

    tools.call = call
    executor = ApprovalExecutor(kernel=k, approvals=backend)
    k.approval_inline = executor.settle_in_turn
    box: dict = {}
    t = threading.Thread(target=lambda: box.setdefault("turn", k.run_turn(
        TurnInput("fil", "grow the volume", lang=lang, lang_recognized=True))), daemon=True)
    t.start()
    import time

    end = time.monotonic() + 5
    while not backend.pending() and time.monotonic() < end:
        time.sleep(0.01)
    card = backend.pending()[0]
    backend.decide_local(card.id, "approve", "local-owner")
    executor.hand_off(card.id)
    t.join(10)
    texts = [m.content for m in box["turn"].messages if m.role == "tool"]
    assert texts == ["Volume grown to 150G."] and tools.called == ["infra_lvextend"]
    assert not any(line in x for x in texts for line in mt.PENDING_LANG.values())


@pytest.mark.parametrize("lang", ["en", "de", "it"])
def test_card_continuation_finds_the_language_of_the_thread_not_french(lang):
    k, _tools, _b, model = _kernel(FIRST)
    k.run_turn(TurnInput("fil", "grow the volume", lang=lang, lang_recognized=True))  # le fil retient la langue
    model.first, model.n = None, 1  # la suite : une réponse, sans nouvel outil
    k.run_turn(TurnInput("fil", "[suite d'une carte]", control=APPROVAL_CONTINUE))  # aucun message de la personne
    assert model.seen_lang[-1] == lang
    assert model.seen_tail[-1] == mt.REPLY_LANG_TAIL[lang]
