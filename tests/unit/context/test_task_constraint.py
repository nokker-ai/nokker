"""Régression du banc X9 « never clear » (bench/never_clear/RESULTS.md §10.3, docs/NEVER-CLEAR.md §13) : une EXIGENCE
posée au fil d'une tâche (« modeles.py … réexporte total_ht et total_ttc pour ne rien casser chez les appelants »)
doit rester visible quand, 35 tours et plusieurs épisodes plus tard, l'utilisateur demande un « nettoyage : imports
inutilisés, code mort » — une demande qui ne partage aucun mot avec l'exigence. Au tour 124 du rejeu `v1fix`, le
modèle a retiré la réexportation : dans le dépôt, rien ne l'utilisait plus (les appelants qu'elle protège sont
dehors), et rien dans son contexte ne la rappelait. Scène rejouée sans modèle."""
from __future__ import annotations

from nokker.context import text as ctext
from nokker.context.summarize import ExtractiveSummarizer
from nokker.context.text import salience
from nokker.context.types import ContextBudgets
from nokker.core.turn import InputCheck, Turn

from .harness import make_session
from .test_revocation import CannedProvider

# Textes du scénario X9 (bench/never_clear/scenario.py), tours 89 à 93 puis 124.
TASK_10 = ("Tâche 10, refactorisation : sors les calculs de modeles.py. Crée factura/calculs.py avec total_ht et "
           "total_ttc ; modeles.py ne garde que les classes, mais réexporte total_ht et total_ttc pour ne rien casser "
           "chez les appelants.")
REQUIREMENT = "réexporte total_ht et total_ttc pour ne rien casser chez les appelants"
TASK_10_FOLLOW = ["Attention aux imports circulaires entre modeles, calculs et tva.",
                  "Déplace aussi les tests correspondants dans tests/test_calculs.py, comme le veut notre règle.",
                  "Lance toute la suite.", "Commite."]
CLEANUP = "Ok, on a le temps. Fais une passe de nettoyage : imports inutilisés, code mort."
LATER = [  # tours 95 à 123 du scénario, en épisodes : la tâche 10 sort de la couche 3 par récence
    ["Me revoilà. Ajoute une docstring de module à chaque fichier de factura/ qui n'en a pas.", "Commite."],
    [("Tâche 11 : les avoirs. factura/avoir.py, creer_avoir(facture, numero) : rend une NOUVELLE Facture avec le "
     "numéro donné."), "Test : l'avoir d'une facture de 3150 centimes a un total HT de -3150.", "Commite."],
    [("Tâche 12 : exporter_csv sur une liste vide doit rendre seulement la ligne d'en-tête. Vérifie, et corrige si "
     "besoin."), "Des tests pour les deux.", "Commite."],
    ["Tâche 13 : un résumé lisible. factura/resume.py, resumer(facture) qui rend un texte.", "Commite."],
    ["Comment s'appelle déjà notre client pilote, et il est où ?", "C'est quand, déjà, la mise en prod ?"],
]


def _scene(tmp_path, budgets: ContextBudgets | None = None):
    s = make_session(budgets or ContextBudgets(current_budget=10 ** 6, episodes_budget=1500), tmp_path=tmp_path,
                     provider=CannedProvider())
    for block in ([TASK_10, *TASK_10_FOLLOW], *LATER):
        for text in block:
            s.say(text)
        s.manager.new_episode(s.thread)
    return s


def test_constraint_markers():
    """Exigences de PRÉSERVATION (ce qui doit survivre aux changements suivants), pas les spécifications ordinaires
    (« -1233 doit donner -1235 », que les tests du projet portent déjà) ni les revirements (section à part)."""
    is_constraint = ctext.is_constraint
    for text in (TASK_10, "Rien d'autre ne doit changer de comportement.",
                 "La facture d'origine ne doit pas être modifiée.",
                 "D'accord : garde le comportement actuel, mais ajoute un test.",
                 "Garde la compatibilité avec l'ancienne API.", "X doit rester importable depuis Y.",
                 "Keep it backward compatible, don't break callers."):
        assert is_constraint(text), text
    for text in ("Et 0 centime, ça donne quoi ? Ça doit afficher 0.00.",
                 "Lance toute la suite : l'arrondi a pu casser des tests de total_ttc.",
                 "Le port 8000 est déjà pris sur la machine de test. Garde-le en tête, on en reparlera.",
                 "Une remise négative aussi doit être refusée.", CLEANUP, "Commite.",
                 "Ah non, elle ouvre toujours les fichiers dans Excel. Reviens au point-virgule."):
        assert not is_constraint(text), text
    # la seule proposition qui porte l'exigence, pas toute la phrase de la tâche
    assert ctext.constraint_clauses(TASK_10) == ("modeles.py ne garde que les classes, mais réexporte total_ht et "
                                                 "total_ttc pour ne rien casser chez les appelants.")


def test_extractive_summary_keeps_the_requirement():
    """Sans nom propre ni nombre, la proposition de l'exigence disparaissait du résumé de l'épisode (note
    `ep-bc3b8bd40d5e` du rejeu : « Tâche 10, refactorisation : sors les calculs de modeles.py. » seulement)."""
    sentence = ctext.sentences(TASK_10)[1]
    assert salience(sentence) > salience("Déplace aussi les tests correspondants dans tests/test_calculs.py.")
    turns = [Turn(thread_id="t", index=88 + i, input=InputCheck(text=text))
             for i, text in enumerate([TASK_10, *TASK_10_FOLLOW])]
    assert REQUIREMENT in ExtractiveSummarizer().summarize(turns).summary


def test_requirement_is_visible_at_the_cleanup(tmp_path):
    s = _scene(tmp_path)
    s.say(CLEANUP)
    prompt = "\n".join(str(m.get("content") or "") for m in s.provider.seen[-1])
    # Avant le correctif : l'exigence n'est nulle part dans ce que voit le modèle (ni couche 3, ni épisodes
    # pertinents, ni messages retrouvés) — comme au tour 124 du rejeu.
    assert REQUIREMENT in prompt
    system = s.provider.seen[-1][0]["content"]
    # Couche 3 (toujours présente, ne dépend d'aucune recherche) : l'exigence, son tour, et la consigne qui dit
    # qu'un code qui la porte peut sembler inutilisé.
    assert "[Mémoire de la session]" in system
    sec = system[system.index("Exigences posées"):]
    assert REQUIREMENT in sec
    t10 = next(t.index for t in s.store.history(s.thread) if t.input.text == TASK_10)
    assert f"T{t10} " in sec
    assert "inutilisé" in sec and "revirement" in sec
    # ni la phrase « Crée factura/calculs.py… » ni les relances ne sont recopiées : une ligne par exigence
    assert sec.count("Crée factura/calculs.py") == 0


def test_requirement_then_revocation_order(tmp_path):
    """Une exigence annulée plus tard reste listée (rien n'est effacé), mais AVANT les revirements, qui font foi."""
    s = _scene(tmp_path)
    s.say("Finalement, plus besoin de réexporter : supprime la réexportation de modeles.py.")
    s.say("Commite.")
    s.manager.new_episode(s.thread)
    s.say(CLEANUP)
    system = s.provider.seen[-1][0]["content"]
    assert system.index("Exigences posées") < system.index("Revirements de la session")
    assert "plus besoin de réexporter" not in system[system.index("Exigences posées"):system.index("Revirements")]


def test_no_requirement_no_section(tmp_path):
    s = make_session(ContextBudgets(current_budget=10 ** 6, episodes_budget=1500), tmp_path=tmp_path,
                     provider=CannedProvider())
    for block in LATER:
        for text in block:
            s.say(text)
        s.manager.new_episode(s.thread)
    s.say(CLEANUP)
    assert "Exigences posées" not in s.provider.seen[-1][0]["content"]


def test_requirements_stay_under_the_session_memory_budget(tmp_path):
    b = ContextBudgets(current_budget=10 ** 6, episodes_budget=600)
    s = make_session(b, tmp_path=tmp_path, provider=CannedProvider())
    for i in range(12):
        s.say(f"Tâche {i} : le module m{i} doit rester importable depuis l'ancien chemin pour ne rien casser. " * 3)
        s.manager.new_episode(s.thread)
    block = s.manager.memory_block(s.thread)
    assert len(block) <= b.episodes_budget * 4 + 200
    assert "T11 " in block and "T0 " not in block  # les plus récentes d'abord gardées
