"""(b) Le rappel d'un tour ne s'accumule pas dans l'historique rejoué (suite du banc « décisions révisées »)."""
from __future__ import annotations

from nokker.context.engine import ContextEngineV0, strip_recall_block
from nokker.context.types import ContextBudgets
from nokker.core.turn import InputCheck, Turn

from .harness import make_session

RECALL = "[Rappel mémoire]\nport api : 9090 (valeur en vigueur depuis le 2026-11-22)\n[/Rappel]"
HEAD = f"Nous sommes le 22.11.2026.\n\n{RECALL}\n\nContexte reçu."


def _turn(i: int, head: str) -> Turn:
    t = Turn(thread_id="t", index=i, input=InputCheck(text=f"question {i}"), head=head, reply="ok")
    return t



# ── (b) ──
def test_strip_removes_only_the_recall_block():
    out = strip_recall_block(HEAD)
    assert "Rappel" not in out and out == "Nous sommes le 22.11.2026.\n\nContexte reçu."
    assert strip_recall_block("Nous sommes le 22.11.2026.") == "Nous sommes le 22.11.2026."
    assert strip_recall_block(f"{RECALL}") == ""


def test_past_turns_are_replayed_without_their_recall_but_the_current_turn_keeps_it():
    past = _turn(1, HEAD)
    assert "[Rappel mémoire]" not in ContextEngineV0.past_user(past, strip_recall=True)
    assert "question 1" in ContextEngineV0.past_user(past, strip_recall=True)
    assert "[Rappel mémoire]" in ContextEngineV0.past_user(past)  # le tour courant (format = contrat) : inchangé


def test_recall_headers_do_not_accumulate_across_turns(tmp_path):
    s = make_session(ContextBudgets(), tmp_path=tmp_path)
    for i in range(4):
        s.say(f"question {i}")
        for t in s.store.history(s.thread):
            if not t.head.count("[Rappel mémoire]"):
                t.head = f"Date.\n\n{RECALL}\n\nfin"  # un rappel dans l'en-tête figé de chaque tour
    s.say("dernière question")
    last = s.provider.seen[-1]
    users = [m["content"] for m in last if m["role"] == "user" and isinstance(m["content"], str)]
    assert sum("[Rappel mémoire]" in u for u in users) <= 1  # au plus celui du tour courant, jamais un par tour passé


def test_window_tokens_do_not_count_the_replay_of_past_recall(tmp_path):
    s = make_session(ContextBudgets(), tmp_path=tmp_path)
    s.say("bonjour")
    t = s.store.history(s.thread)[0]
    base = s.manager._turn_tokens(t)
    t.head = "Date.\n\n[Rappel mémoire]\n" + "ligne de rappel très longue. " * 200 + "\n[/Rappel]\n\nfin"
    assert s.manager._turn_tokens(t) < base + 40  # le rappel (~1 k jetons) n'est pas compté


