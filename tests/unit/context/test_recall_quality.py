"""Régression du banc de reprise (19.10.2026, vrai modèle) : un fait planté AU MILIEU d'un épisode de remplissage qui
recoupe son vocabulaire doit survivre au résumé et être retrouvé — par le bloc des épisodes pertinents, par
`recall_context(query)` et par la recherche au niveau des tours."""
from __future__ import annotations

import pytest

from nokker.context.summarize import ExtractiveSummarizer
from nokker.context.text import salience
from nokker.context.types import ContextBudgets
from nokker.core.turn import InputCheck, Turn

from .harness import make_session

FACTS = [
    ("Le numéro du dossier de l'atelier est 4711.", "Quel est le numéro du dossier de l'atelier ?", "4711"),
    ("Le serveur de sauvegarde s'appelle Zébulon.", "Comment s'appelle le serveur de sauvegarde ?", "Zébulon"),
    ("Rendez-vous avec Mme Pasquier le 14 novembre à 9h30.", "Avec qui est mon rendez-vous du 14 novembre ?", "Pasquier"),
    ("Le budget du projet Aurore est de 38 500 CHF.", "Quel est le budget du projet Aurore ?", "38 500"),
    ("La salle de réunion réservée pour le comité est la B204.", "Quelle salle est réservée pour le comité ?", "B204"),
]
TOPICS = ["le serveur de test et la mise à jour des paquets", "le budget général du projet et les dépenses courantes",
          "un rendez-vous à déplacer et l'agenda de la semaine", "la sauvegarde nocturne du NAS et les disques"]


def _session(tmp_path):
    b = ContextBudgets(current_budget=6000, episodes_budget=1200, compaction_step=1500, shed_min_tokens=300,
                       reservoir_min_tokens=900)
    s = make_session(b, tmp_path=tmp_path, tool_size=3000)
    for day in range(len(FACTS)):
        s.clock.advance(26 * 3600)
        for k in range(14):
            if k == 5:
                s.say(FACTS[day][0])
            s.say(f"Parlons de {TOPICS[(day + k) % len(TOPICS)]} ; rien de précis à retenir ({day}.{k}).")
    s.clock.advance(3 * 3600)
    return s


def test_salience_prefers_fact_lines_over_chatter():
    assert salience("Le serveur de sauvegarde s'appelle Zébulon.") > salience("Parlons de la sauvegarde ordinaire.")
    assert salience("Rendez-vous avec Mme Pasquier le 14 novembre à 9h30.") >= 6
    assert salience("Quel est le budget du projet Aurore ?") <= 1


def test_extractive_summary_keeps_fact_from_the_middle_of_a_long_episode():
    turns = [Turn(thread_id="t", index=i, input=InputCheck(text=f"Parlons du serveur et du budget, rien de précis ({i})."))
             for i in range(20)]
    turns[9].input = InputCheck(text="Le serveur de sauvegarde s'appelle Zébulon.")
    res = ExtractiveSummarizer().summarize(turns)
    assert "Zébulon" in res.summary and "zébulon" in res.keywords


@pytest.mark.parametrize("fact,question,expected", FACTS)
def test_old_fact_reaches_the_prompt_or_recall(tmp_path, fact, question, expected):
    s = _session(tmp_path)
    s.say(question)
    prompt = "\n".join(m.get("content") or "" for m in s.provider.seen[-1])
    in_prompt = expected in prompt
    found_by_tool = any(expected in line for line in s.manager.recall(s.thread, query=question).split("\n"))
    assert in_prompt or found_by_tool, "le fait n'est ni dans le prompt ni retrouvable par recall_context(query)"


def test_turn_level_search_is_the_safety_net(tmp_path):
    s = _session(tmp_path)
    hits = s.manager.search_turns(s.thread, "serveur de sauvegarde Zébulon")
    assert hits and "Zébulon" in hits[0]


def test_noise_words_do_not_dilute_the_decisive_term(tmp_path):
    """« Qui dois-je voir en novembre, et à quelle heure ? » : seul « novembre » départage — les mots absents du fil
    (« qui », « dois-je »…) ne doivent pas faire passer le score sous le seuil (régression banc paraphrase)."""
    s = _session(tmp_path)
    block = s.manager.relevant_block(s.thread, "Qui dois-je voir en novembre, et à quelle heure ?",
                                     exclude=s.manager.layer3_ids(s.thread))
    assert "Pasquier" in block


def test_a_single_common_term_does_not_inject_noise(tmp_path):
    s = _session(tmp_path)
    assert s.manager.relevant_block(s.thread, "Et le serveur ?", exclude=s.manager.layer3_ids(s.thread)) == ""
