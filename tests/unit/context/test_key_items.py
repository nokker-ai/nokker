"""Lot compact-cache, point 4 : le résumé d'épisode garde les NOMS DE FICHIERS et les VALEURS (ligne « Éléments clés »), et
l'invitation de `[Mémoire de la session]` dit de chercher avec recall_context avant de répondre « je ne sais pas »."""
from __future__ import annotations

import re

import pytest

from nokker.approvals.rule_labels import LANGS, lang_scope
from nokker.context import keyitems
from nokker.context.consolidate import ConsolidationReport, Consolidator
from nokker.context.summarize import ExtractiveSummarizer
from nokker.context.types import ContextBudgets, SummaryResult
from nokker.core import model_texts as mt
from nokker.core.turn import InputCheck, Message, ToolCall, Turn

from .harness import make_session

FILLER = "On parle de la migration du disque et des sauvegardes nocturnes du serveur, avec beaucoup de détails."


def _turns(n: int = 60, file_at: int = 31) -> list[Turn]:
    out = []
    for i in range(n):
        t = Turn(thread_id="t", index=i, input=InputCheck(text=f"Question {i} : {FILLER} Réf. {i * 7 % 13}."))
        if i == file_at:
            t.messages.append(Message(role="assistant", content="Je lis le modèle.", tool_calls=[
                ToolCall(id="c1", name="read_file", arguments={"path": "mon_modele_v3.mbd"})]))
        t.reply = f"Réponse {i} : rien de précis."
        out.append(t)
    return out


def test_file_name_in_the_middle_of_an_episode_is_in_the_summary():
    res = ExtractiveSummarizer().summarize(_turns())
    assert "mon_modele_v3.mbd" in res.summary
    assert "Éléments clés :" in res.summary


def test_the_old_summary_missed_it_and_the_flag_restores_it():
    res = ExtractiveSummarizer(key_items=False).summarize(_turns())
    assert "mon_modele_v3.mbd" not in res.summary and "Éléments clés" not in res.summary


def test_summary_and_key_line_stay_within_their_budgets():
    turns = _turns(80)
    for i in range(0, 80, 3):  # beaucoup d'éléments : fichiers, identifiants, valeurs
        turns[i].input = InputCheck(text=f"Ouvre fichier_{i}.mbd, masse {i + 100} kg, vérin HV-{i}/35-450, pas {i}e-3 s.")
    res = ExtractiveSummarizer().summarize(turns)
    assert len(res.summary) <= 1300 + 1
    line = res.summary[res.summary.index("Éléments clés :"):]
    assert len(line) <= 300 and line.endswith(".")


def test_items_are_deduplicated_and_chronological():
    line = keyitems.key_line(["Ouvre a_run.mbd.", "Puis b_run.mbd et a_run.mbd de nouveau.", "Enfin c_run.csv."], 300)
    assert line == "Éléments clés : a_run.mbd ; b_run.mbd ; c_run.csv."


def test_not_a_file_domains_numbers_versions():
    line = keyitems.key_line(["Voir ninabot.ch, la version 3.14 et v1.2, puis la page example.com."], 300)
    assert line == ""


def test_identifiers_and_values_with_units():
    line = keyitems.key_line(["Le vérin HV-63/35-450 pèse 450 kg, course 120 mm, tension 24 V.", "ep_2171a3188290"], 300)
    for part in ("HV-63/35-450", "450 kg", "120 mm", "24 V", "ep_2171a3188290"):
        assert part in line, line


def test_key_line_is_bounded_and_keeps_heavy_items_first_when_short_of_room():
    texts = [f"valeur {i} : {i}00 kg" for i in range(40)] + ["Le fichier important_modele.mbd."]
    line = keyitems.key_line(texts, 120)
    assert len(line) <= 120 and "important_modele.mbd" in line  # un fichier pèse plus qu'une valeur ordinaire


def test_labels_in_four_languages():
    for lang in LANGS:
        line = keyitems.key_line(["Ouvre a_run.mbd."], 300, lang=lang)
        assert line.startswith(mt.KEY_ITEMS_LABEL[lang]) and "a_run.mbd" in line


# ───────────── l'invitation de [Mémoire de la session] ─────────────


def _closed_session(tmp_path, **kw):
    s = make_session(ContextBudgets(current_budget=400, episodes_budget=500, **kw), tmp_path=tmp_path)
    for i in range(12):
        s.say(f"sujet {i} : {FILLER}")
    assert s.manager.episodes.list(s.thread, closed_only=True)
    return s


def test_invitation_is_direct_and_in_the_language_of_the_turn(tmp_path):
    s = _closed_session(tmp_path)
    for lang in LANGS:
        with lang_scope(lang):
            block = s.manager.memory_block(s.thread)
        assert block.startswith("[Mémoire de la session]\n" + mt.SESSION_MEMORY_HINT[lang]), lang
    assert "recall_context avec ce détail" in mt.SESSION_MEMORY_HINT["fr"] and "ne devine pas" in mt.SESSION_MEMORY_HINT["fr"]
    for lang in LANGS:
        assert "recall_context" in mt.SESSION_MEMORY_HINT[lang]


def test_old_invitation_with_the_flag_off(tmp_path):
    s = _closed_session(tmp_path, direct_recall_hint=False)
    assert "AVANT de répondre que tu ne sais pas" in s.manager.memory_block(s.thread)


def test_key_line_survives_the_llm_refinement(tmp_path):
    s = make_session(ContextBudgets(current_budget=400, episodes_budget=500), tmp_path=tmp_path)
    s.say("Ouvre le fichier mon_modele_v3.mbd, pas de temps 1e-3 s.")
    for i in range(10):
        s.say(f"sujet {i} : {FILLER}")

    class Refiner:
        def summarize(self, turns, previous=None):
            return SummaryResult(title="T", summary="Résumé raffiné sans le nom.", keywords=[], facts=[], model="m")

    Consolidator(s.manager, refiner=Refiner())._thread(s.thread, s.clock(), ConsolidationReport())
    first = s.manager.episodes.list(s.thread, closed_only=True)[0]
    assert first.refined and "mon_modele_v3.mbd" in first.summary


def test_recall_finds_the_file_name_after_the_episode_closed(tmp_path):
    s = make_session(ContextBudgets(current_budget=400, episodes_budget=500), tmp_path=tmp_path)
    s.say("Lis mon_modele_v3.mbd, le modèle de référence avec les renforts.")
    for i in range(10):
        s.say(f"sujet {i} : {FILLER}")
    out = s.manager.recall(s.thread, query="fichier du modèle de référence renforts")
    assert re.search(r"mon_modele_v3\.mbd", out), out


@pytest.mark.parametrize("lang", LANGS)
def test_all_new_texts_exist_in_four_languages(lang):
    for t in (mt.KEY_ITEMS_LABEL, mt.KEY_ITEM_REPLACES, mt.SESSION_MEMORY_HINT, mt.RECALL_REVISION):
        assert t[lang].strip()
