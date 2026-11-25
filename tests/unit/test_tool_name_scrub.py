"""Nom technique d'un outil dans la réponse (core/tool_name_scrub.py) : jamais lu par la personne, sauf contextes protégés."""
from __future__ import annotations

import pytest

from nokker.core import person_texts as ptx
from nokker.core import tool_name_scrub as ns
from nokker.core.turn import Effect, TurnInput
from tests.unit.test_note_claims import guard_world

NAMES = ({"screen_draft", "calendar_create", "srv__memory_write", "terminal", "clarify"}, False)


def scrub(text, user="il fait sombre dans le salon", lang="fr", tools=NAMES):
    return ns.scrub(text, tools, user, lang)


def test_a_catalog_tool_name_is_replaced_in_plain_text():
    assert scrub("J'ai screen_draft.") == "J'ai cette action."
    assert scrub("J'ai créé l'événement avec calendar_create et srv__memory_write.") == \
        "J'ai créé l'événement avec cette action et cette action."


@pytest.mark.parametrize("lang", ["fr", "en", "de", "it"])
def test_neutral_wording_in_four_languages(lang):
    assert scrub("x screen_draft y", lang=lang) == f"x {ptx.TOOL_NAME_NEUTRAL[lang]} y"


@pytest.mark.parametrize("text", [
    "Utilise `screen_draft` pour ça.", "Voici : « screen_draft ».", 'Le champ "calendar_create" existe.',
    "```\ncalendar_create(x)\n```", "Voir docs/screen_draft.md", "Le fichier screen_draft.py", "Un mot : terminal.",
    "Je clarify rien.", "Aucun nom d'outil ici, juste du texte_libre.", "un_outil_inconnu reste tel quel"])
def test_protected_or_unknown_text_is_left_alone(text):
    assert scrub(text) == text


def test_a_name_the_person_wrote_is_left_alone():
    assert scrub("calendar_create est un outil.", user="explique calendar_create") == "calendar_create est un outil."


def test_code_session_is_left_alone():
    tools = ({"screen_draft", "write_file"}, True)
    assert scrub("J'ai screen_draft.", tools=tools) == "J'ai screen_draft."


def test_kernel_cleans_the_final_reply_and_the_history():
    k = guard_world("J'ai screen_draft.", specs={"screen_draft": Effect.READ, "calendar_create": Effect.WRITE})
    k.tools.index = lambda: [("screen_draft", "d"), ("calendar_create", "d")]
    t = k.run_turn(TurnInput(thread_id="th_s", user_text="il fait sombre dans le salon"))
    assert t.reply == "J'ai cette action."
    assert [m.content for m in t.messages if m.role == "assistant"][-1] == "J'ai cette action."
