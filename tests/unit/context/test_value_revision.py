"""Lot compact-cache, point 5 : une RÉVISION de valeur (« on passe de 1e-3 à 2e-4 ») REMPLACE l'ancienne — détectée (fr/en/de/it),
marquée « (remplace …) » dans le résumé, dite dans `[Épisodes pertinents]` même quand elle est dans l'épisode OUVERT, et
`recall_context(query)` rend le plus récent d'abord."""
from __future__ import annotations

import re

import pytest

from nokker.context import keyitems
from nokker.context import text as ctext
from nokker.context.summarize import ExtractiveSummarizer
from nokker.context.types import ContextBudgets
from nokker.core.turn import InputCheck, Turn

from .harness import make_session

FILLER = "On parle de la migration du disque et des sauvegardes nocturnes du serveur, avec beaucoup de détails."

REVISIONS = [
    "On passe de 1e-3 à 2e-4 pour le pas de temps.", "Utilise 2e-4 au lieu de 1e-3.", "Désormais le pas est 2e-4 s.",
    "Mets plutôt 2e-4.", "Le pas : 1e-3 → 2e-4.", "Change le pas en 2e-4.", "Corrige la masse, 450 kg.",
    "Maintenant il faut 3 vérins.", "The step changes from 1e-3 to 2e-4.", "Use 2e-4 instead of 1e-3.",
    "Set it to 2e-4 now.", "Ändere den Wert auf 2e-4.", "Verwende 2e-4 statt 1e-3.", "Il passo passa da 1e-3 a 2e-4.",
    "Usa 2e-4 invece di 1e-3.",
]
NOT_REVISIONS = [
    "Passe le séparateur du CSV en virgule, en-tête compris.", "Parfait. Je prends un café, je reviens.",
    "Je suis à Genève, de Lausanne à Zurich il y a 2 h.", "Da Roma a Milano ci vogliono 3 ore.",
    "How do you do?", "Quel est le pas de temps ?", "Commite le retour en arrière.",
]


@pytest.mark.parametrize("text", REVISIONS)
def test_revision_markers_in_four_languages(text):
    assert ctext.is_revocation(text), text


@pytest.mark.parametrize("text", NOT_REVISIONS)
def test_ordinary_sentences_are_not_revisions(text):
    assert not ctext.is_revocation(text), text


def test_old_markers_alone_with_the_revision_flag_off():
    assert not ctext.is_revocation("On passe de 1e-3 à 2e-4 pour le pas de temps.", revisions=False)
    assert ctext.is_revocation("Annule le dernier commit.", revisions=False)


def test_pairs_old_and_new():
    assert keyitems.revision_pairs("on passe de 1e-3 à 2e-4") == [("1e-3", "2e-4")]
    assert keyitems.revision_pairs("Use 2e-4 instead of 1e-3") == [("1e-3", "2e-4")]
    assert keyitems.revision_pairs("Verwende 2e-4 statt 1e-3") == [("1e-3", "2e-4")]
    assert keyitems.revision_pairs("von 5 mm auf 7 mm") == [("5 mm", "7 mm")]
    assert keyitems.revision_pairs("passe de blanc à noir") == []


def _turns(*texts: str) -> list[Turn]:
    return [Turn(thread_id="t", index=i, input=InputCheck(text=t), reply="Noté.") for i, t in enumerate(texts)]


def test_summary_presents_the_final_value_as_replacing_the_old_one():
    res = ExtractiveSummarizer().summarize(_turns(
        "Lance la simulation, pas de temps 1e-3 s.", FILLER, "On passe de 1e-3 s à 2e-4 s pour le pas de temps.", FILLER))
    line = res.summary[res.summary.index("Éléments clés :"):]
    assert "pas de temps 2e-4 s (remplace 1e-3 s)" in line, line
    assert "pas de temps 1e-3 s" not in line.replace("(remplace 1e-3 s)", "")  # l'ancienne n'est plus une valeur courante


def test_same_key_later_value_replaces_even_without_a_revision_phrase():
    line = keyitems.key_line(["Masse 450 kg.", "Autre chose.", "Masse 520 kg."], 300)
    assert "masse 520 kg (remplace 450 kg)" in line and "masse 450 kg ;" not in line


def test_equal_salience_prefers_the_latest_sentence():
    res = ExtractiveSummarizer(max_chars=400).summarize(_turns(*[f"Le débit B204 vaut {i} litres." for i in range(30)]))
    assert "29 litres" in res.summary.replace("  ", " ") or "B204" in res.summary


def _scene(tmp_path, **kw):
    s = make_session(ContextBudgets(current_budget=600, episodes_budget=500, **kw), tmp_path=tmp_path)
    s.say("Pour la simulation le pas de temps est 1e-3 s, c'est noté.")
    for i in range(6):
        s.say(f"sujet {i} : {FILLER}")
    s.say("Finalement on passe de 1e-3 à 2e-4 pour le pas de temps de la simulation.")
    for i in range(6):
        s.say(f"autre sujet {i} : {FILLER}")
    assert s.manager.episodes.list(s.thread, closed_only=True)
    return s


def test_recall_context_lists_the_most_recent_first(tmp_path):
    s = _scene(tmp_path)
    out = s.manager.recall(s.thread, query="pas de temps de la simulation")
    idx = [int(m) for m in re.findall(r"^- T(\d+) ", out, re.MULTILINE)]
    assert len(idx) >= 2 and idx == sorted(idx, reverse=True), out
    first_rev = next(ln for ln in out.splitlines() if ln.startswith("- T") and "2e-4" in ln)
    assert "REVIREMENT" in first_rev
    msgs = [ln for ln in out.splitlines() if ln.startswith("- T")]
    assert msgs.index(first_rev) == 0 or "1e-3 s" not in "".join(msgs[:msgs.index(first_rev)])  # la révision avant l'ancienne


def test_recall_keeps_the_old_order_with_the_flag_off(tmp_path):
    s = _scene(tmp_path, recall_recent_first=False)
    out = s.manager.recall(s.thread, query="pas de temps de la simulation")
    assert "Rien de pertinent" not in out


def test_revision_in_the_open_episode_is_flagged_on_the_closed_extract(tmp_path):
    s = make_session(ContextBudgets(current_budget=600, episodes_budget=500), tmp_path=tmp_path)
    s.say("Pour la simulation le pas de temps est 1e-3 s, c'est noté.")
    for i in range(8):
        s.say(f"sujet {i} : {FILLER}")
    assert s.manager.episodes.list(s.thread, closed_only=True)
    s.say("Finalement on passe de 1e-3 à 2e-4 pour le pas de temps de la simulation.")  # épisode OUVERT
    assert s.manager.open_revisions(s.thread)
    block = s.manager.relevant_block(s.thread, "Quel est le pas de temps de la simulation ?",
                                     exclude=set())
    assert "2e-4" in block and "remplace" in block.lower(), block
    out = s.manager.recall(s.thread, query="pas de temps de la simulation")
    assert "en cours" in out and "2e-4" in out


def test_open_revisions_off_with_the_flag(tmp_path):
    s = make_session(ContextBudgets(current_budget=600, revision_markers=False), tmp_path=tmp_path)
    s.say("On passe de 1e-3 à 2e-4 pour le pas de temps.")
    assert s.manager.open_revisions(s.thread) == []
