"""Le modèle tutoie dans ses réponses libres mais vouvoyait dès qu'il parlait de la mémoire (recette rc.9.30 : « C'est
noté : vous préférez… »). Tout ce que le modèle reçoit sur le chemin mémoire (descriptions et paramètres des outils,
résultats renvoyés, gabarit d'injection, préfixe mémoire) est figé sans « vous », « votre », « vos » (mots entiers,
insensible à la casse) ; les notes, elles, se rédigent à la forme neutre."""
import json
import re
from pathlib import Path

from nokker.context import project_memory as PM
from nokker.memory import long_note as LN
from tests.unit.context.test_project_memory import ctx, make_memory, make_repo

FORMAL = re.compile(r"\b(vous|votre|vos)\b", re.IGNORECASE)


def _texts() -> list[str]:
    tools = PM.ProjectMemoryTools(PM.ProjectMemory(root="/nonexistent"))
    out = [json.dumps([(s.name, s.description, s.parameters) for s in tools.specs()], ensure_ascii=False)]
    out += [PM.PENDING_RESULT, PM.NOT_WRITTEN, PM.STORAGE_ERROR, PM.SCRUBBED, PM.ACK_HINT, PM.ALREADY_RESULT,
            PM.REPLACED_RESULT, PM.ERR_REPLACES_UNKNOWN, PM.ERR_REPLACES_KIND, PM.GUARD]
    out += [LN.DESCRIPTION, json.dumps(LN.PARAMETERS, ensure_ascii=False), *LN.ALL_MODEL_TEXTS]
    out.append(Path(PM.MEMORY_PREFIX_PATH).read_text(encoding="utf-8"))
    return out


def test_memory_texts_never_say_vous(tmp_path):
    for t in _texts():
        assert not FORMAL.search(t), t[:120]


def test_injection_template_and_results_never_say_vous(tmp_path):
    repo = make_repo(tmp_path)
    pm, _ = make_memory(tmp_path)
    pm.open_session("th_1", repo, tmp_path)
    tools = PM.ProjectMemoryTools(pm)
    pm.core_block("th_1", 1, "je préfère pytest")
    written = tools.call("memory_note", {"kind": "preference", "title": "Tests", "derived_from": "user",
                                         "text": "préfère les tests en pytest"}, ctx(turn=1))
    again = tools.call("memory_note", {"kind": "preference", "title": "Tests", "derived_from": "user",
                                       "text": "préfère les tests en pytest"}, ctx(turn=1))
    core = pm.core_block("th_1", 2, "suite")
    for t in (written.content, again.content, core):
        assert not FORMAL.search(t), t


def test_note_text_is_asked_in_neutral_form_and_prefix_tutoies():
    assert "à la forme neutre, sans pronom de la deuxième personne" in PM.MEMORY_NOTE_PARAMETERS["properties"]["text"]["description"]
    prefix = Path(PM.MEMORY_PREFIX_PATH).read_text(encoding="utf-8")
    assert "tutoie la personne (« tu préfères… », « tu m'as dit… »)" in prefix
