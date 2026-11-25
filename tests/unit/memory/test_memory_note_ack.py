"""L'accusé d'une note écrite est court et ne recopie pas la note (recette rc.9.30 : « tu préfères… » mot pour mot).
Le résultat de l'outil ne porte jamais le texte de la note et dit comment accuser ; la consigne est aussi dans la
description de l'outil et le préfixe mémoire."""
from pathlib import Path

from nokker.context import project_memory as PM

ACK = "accuse réception en quelques mots (« C'est noté. »), sans répéter la note."


def test_result_texts_carry_the_ack_instruction_not_the_note():
    assert PM.ACK_HINT == " ; " + ACK
    assert "tu préfères" not in PM.ACK_HINT and "{" not in PM.ACK_HINT


def test_description_and_prefix_ask_for_a_short_ack():
    assert "accuse réception en quelques mots, sans la répéter" in PM.MEMORY_NOTE_DESCRIPTION
    assert "accuse réception en quelques mots (« C'est noté. »), sans la répéter" in \
        Path(PM.MEMORY_PREFIX_PATH).read_text(encoding="utf-8")


def test_a_written_note_result_never_repeats_the_note_text(tmp_path):
    from nokker.context.project_memory import ProjectMemoryTools
    from tests.unit.context.test_project_memory import ctx, make_memory, make_repo

    repo = make_repo(tmp_path)
    pm, _ = make_memory(tmp_path)
    pm.open_session("th_1", repo, tmp_path)
    pm.core_block("th_1", 1, "je préfère pytest")
    r = ProjectMemoryTools(pm).call("memory_note", {"kind": "preference", "title": "Tests", "derived_from": "user",
                                                    "text": "préfère les tests en pytest, jamais unittest"}, ctx(turn=1))
    assert not r.is_error and r.content.startswith("noté : ") and r.content.endswith(PM.ACK_HINT)
    assert "pytest" not in r.content and "unittest" not in r.content
