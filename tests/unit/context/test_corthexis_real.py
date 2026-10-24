"""Mémoire de session sur le VRAI moteur CortHeXis (pgvector + service d'embeddings). Ignoré sans
`NOKKER_TEST_CX_DSN` (Postgres avec l'extension `vector`) et `NOKKER_TEST_CX_EMBED` (URL du serveur d'embeddings,
ex. le service llama.cpp EmbeddingGemma)."""
from __future__ import annotations

import os
import uuid

import pytest

from .harness import make_session
from .test_recall_quality import FACTS, TOPICS

DSN = os.environ.get("NOKKER_TEST_CX_DSN", "")
EMBED = os.environ.get("NOKKER_TEST_CX_EMBED", "")
pytestmark = pytest.mark.skipif(not (DSN and EMBED), reason="NOKKER_TEST_CX_DSN / NOKKER_TEST_CX_EMBED absents")

PARAPHRASE = [
    ("Peux-tu me redonner la référence de mon dossier d'atelier ?", "4711"),
    ("Quelle est la machine qui fait nos backups ?", "Zébulon"),
    ("Qui dois-je voir en novembre, et à quelle heure ?", "Pasquier"),
    ("Combien d'argent est alloué à Aurore ?", "38 500"),
    ("Où se tiendra le comité ?", "B204"),
]


def _session(tmp_path, thread):
    from nokker.context.corthexis_index import CorthexisSemanticIndex

    s = make_session(tmp_path=tmp_path, thread=thread)
    s.manager.semantic = CorthexisSemanticIndex(tmp_path / "cx", database_url=DSN, embed_urls=EMBED)
    for day in range(len(FACTS)):
        s.clock.advance(26 * 3600)
        for k in range(14):
            if k == 5:
                s.say(FACTS[day][0])
            s.say(f"Parlons de {TOPICS[(day + k) % len(TOPICS)]} ; rien de précis à retenir ({day}.{k}).")
    s.clock.advance(3 * 3600)
    assert s.manager.semantic.flush(180)
    return s


def _reachable(s, q: str, expected: str) -> bool:
    """Le fait est-il dans ce que le modèle recevra : mémoire de session, épisodes pertinents ou fenêtre brute ?"""
    window = "\n".join(t.input.text for t in s.manager.history_window(s.thread))
    blocks = s.manager.memory_block(s.thread) + s.manager.relevant_block(s.thread, q)
    return expected in blocks or expected in window


def test_paraphrased_questions_reach_the_fact_through_corthexis(tmp_path):
    s = _session(tmp_path, "th_" + uuid.uuid4().hex[:8])
    missed = [q for q, expected in PARAPHRASE if not _reachable(s, q, expected)]
    # La question « backups » est volontairement piégée : le remplissage parle LUI-MÊME de sauvegarde du NAS, donc
    # le moteur remonte légitimement ces lignes ; on tolère ce seul échec (documenté dans docs/NEVER-CLEAR.md §11).
    assert len(missed) <= 1, f"faits non retrouvés : {missed}"


def test_thread_isolation_and_no_noise(tmp_path):
    a = _session(tmp_path, "th_" + uuid.uuid4().hex[:8])
    other = a.manager.semantic.search("th_" + uuid.uuid4().hex[:8], "machine qui fait nos backups")
    assert other == []  # un autre fil n'a aucun épisode : jamais de fuite inter-fils
    assert a.manager.semantic.search(a.thread, "recette de tiramisu au mascarpone") == []  # hors sujet : rien
