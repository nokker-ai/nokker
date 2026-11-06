"""Drapeau `route_relay_voice` (route v1, classeur par règles) : un résumé ou un relais d'un document reste sur le palier
de la voix (conversation) malgré des mots techniques ; une vraie demande de code reste du code."""
from __future__ import annotations

import dataclasses

import pytest

from nokker.config import NokkerConfig
from nokker.route.classify import TaskClass, classify, classify_rules

DE_ALONE = "Du arbeitest allein, ohne Menschen, der antwortet: stell keine Fragen, bring es zu Ende."
RAPPORT_DE = ("Der Build-Agent hat seinen Bericht in rapport-build.md abgelegt. Fass ihn mir in drei Sätzen zusammen, "
              "mit den genauen Zahlen, nichts gerundet.\n\n" + DE_ALONE)
RELAIS = [
    RAPPORT_DE,
    "Fasse den Bericht des Build-Agents zusammen.",
    "Le sous-agent de build a laissé son rapport dans rapport-build.md. Résume-le-moi en trois phrases.",
    "Que dit le rapport du commit d'hier sur la migration ?",
    "Was sagt der Bericht über den Build?",
    "Summarize the CI build log for me.",
    "What does the migration report say about the build?",
    "Riassumi il rapporto della build di stanotte.",
    "Cosa dice il diff del commit 4f2a?",
]
CODE = [
    "corrige le bug du build",
    "écris un script qui relance le build",
    "fix the build",
    "Résume le diff puis corrige le bug de parsing.",
    "Summarize what parse_date() does and fix it.",
    "Résume app.py",                       # fichier source : du code
    "```py\nprint(1)\n```\nrésume ce code",  # bloc de code
    "summarize the diff and fix the failing test",
]


@pytest.mark.parametrize("text", RELAIS)
def test_resume_ou_relais_reste_conversation(text):
    c = classify_rules(text, relay_voice=True)
    assert c.task is TaskClass.CONVERSATION and c.sure, c
    assert classify(text, relay_voice=True).task is TaskClass.CONVERSATION


def test_rapport_de_partait_en_code_sans_le_drapeau():
    assert classify_rules(RAPPORT_DE).task is TaskClass.CODE
    assert classify_rules(RELAIS[2]).task is TaskClass.CODE


@pytest.mark.parametrize("text", CODE)
def test_vraie_demande_de_code_reste_code(text):
    assert classify_rules(text, relay_voice=True).task is TaskClass.CODE
    assert classify_rules(text).task is TaskClass.CODE


@pytest.mark.parametrize("text", ["merci !", "Quel temps fait-il demain à Lausanne ?", "et les tests ?",
                                  "ajoute une fonction slugify", "rédige un mail au client"])
def test_eteint_rien_ne_change_hors_relais(text):
    for prev in (None, TaskClass.CODE, TaskClass.CONVERSATION):
        assert classify_rules(text, prev, relay_voice=True) == classify_rules(text, prev)


def test_suite_de_code_qui_demande_un_resume():
    # Après un tour de code, « résume ce que tu as fait » est un relais : conversation avec le drapeau.
    assert classify_rules("résume ce que tu as fait", TaskClass.CODE).task is TaskClass.CODE
    assert classify_rules("résume ce que tu as fait", TaskClass.CODE, relay_voice=True).task is TaskClass.CONVERSATION


def test_drapeau_eteint_par_defaut_et_cable():
    from nokker.api.server import build_policy

    assert NokkerConfig.__dataclass_fields__["route_relay_voice"].default is False
    base = NokkerConfig(route={"policy": "v1", "classifier": "rules", "code_to_deep": True})
    assert build_policy(base, {}).relay_voice is False
    assert build_policy(dataclasses.replace(base, route_relay_voice=True), {}).relay_voice is True
