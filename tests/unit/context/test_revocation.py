"""Régression du banc X9 « never clear » (bench/never_clear/RESULTS.md §5.1) : une consigne ANNULÉE ne doit pas revenir
comme si elle était en cours. Scène du banc, sans modèle : décision A → autre chose → « annule A, reviens à B »
(exprimé par référence, sans le vocabulaire de A) → autre chose → question sur A, plusieurs épisodes plus tard.
Le contexte assemblé doit porter la décision EN VIGUEUR (B), dans l'ordre, et dire qu'elle remplace A."""
from __future__ import annotations

import re

from nokker.context import text as ctext
from nokker.context.summarize import ExtractiveSummarizer
from nokker.context.text import salience
from nokker.context.types import ContextBudgets
from nokker.core.ports import Completion
from nokker.core.turn import InputCheck, Message, Turn, Usage

from .harness import make_session

DECISION_A = "La comptable a un nouveau logiciel : passe le séparateur du CSV en virgule, en-tête compris."
REVOKE_A = ("Ah non, je viens de l'avoir au téléphone : elle ouvre toujours les fichiers dans Excel. Reviens au "
            "point-virgule. Tu te souviens pourquoi on l'avait choisi ?")
FAC_TASK = ("Tâche 3, ticket FAC-217 : l'arrondi suisse aux 5 centimes. Crée factura/arrondi.py et fais en sorte que "
            "total_ttc rende le total arrondi aux 5 centimes.")
FAC_REVOKE = ("Mauvaise nouvelle : la boulangerie ne veut finalement pas de FAC-217. Annule ce changement-là, et "
              "seulement celui-là : tout le reste de la journée doit rester.")
QUESTION = "Pourquoi le CSV est en point-virgule, déjà ? La comptable me redemande."
README = ("Écris dans le README une section « Décisions » qui liste les décisions prises aujourd'hui : le port du "
          "serveur, le séparateur du CSV, les PDF et l'arrondi aux 5 centimes.")
FILLERS = [
    "Tâche 9 : prochain_numero doit ignorer les numéros mal formés comme BROUILLON.",
    "Reproduis d'abord le plantage avec un test.",
    "Lance toute la suite.",
    "Commite.",
    "Tâche 10 : sors les calculs de modeles.py dans calculs.py, attention aux imports circulaires.",
    "Ajoute une docstring de module à chaque fichier qui n'en a pas.",
    "Tâche 11 : les avoirs, creer_avoir(facture, numero) avec des quantités négatives.",
    "Qui relit nos PR, déjà ?",
]
REPLIES = {  # conclusion du modèle, par message : c'est elle qui nomme ce qui a été annulé
    DECISION_A: "Séparateur CSV passé en virgule dans factura/export.py, tests mis à jour, commit d221d3b.",
    REVOKE_A: "Retour au point-virgule : factura/export.py écrit de nouveau avec ';' (Excel en réglages suisses).",
    FAC_TASK: "factura/arrondi.py créé, total_ttc arrondi aux 5 centimes, commit f3bb560.",
    FAC_REVOKE: "FAC-217 annulé : factura/arrondi.py supprimé, total_ttc n'arrondit plus aux 5 centimes.",
}


class CannedProvider:
    """Répond la conclusion prévue pour les messages de la scène, « Fait. » sinon ; garde les prompts."""

    def __init__(self) -> None:
        self.seen: list[list[dict]] = []

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        self.seen.append([dict(m) for m in messages])
        last = next(m for m in reversed(messages) if m["role"] == "user")["content"]
        reply = next((r for q, r in REPLIES.items() if last.endswith(q)), "Fait.")
        return Completion(message=Message(role="assistant", content=reply), usage=Usage(prompt=100, completion=10),
                          model="scripted", latency_s=0.001)


def _scene(tmp_path):
    """Six épisodes clos : FAC-217 · A (virgule) · autre chose · annulations (A, puis FAC-217) · autre chose ×2.
    Les deux derniers épisodes seulement sont en couche 3 : l'annulation n'y est pas par récence."""
    s = make_session(ContextBudgets(current_budget=10 ** 6, episodes_budget=1500), tmp_path=tmp_path,
                     provider=CannedProvider())
    for block in ([FAC_TASK, "Lance toute la suite.", "Commite."],
                  [DECISION_A, "Lance les tests de l'export.", "Commite."],
                  FILLERS[:4],
                  [REVOKE_A, "Commite le retour en arrière.", FAC_REVOKE, "Lance toute la suite."],
                  FILLERS[4:6],
                  FILLERS[6:]):
        for text in block:
            s.say(text)
        s.manager.new_episode(s.thread)
    return s


def _prompt(s) -> str:
    return "\n".join(str(m.get("content") or "") for m in s.provider.seen[-1])


def test_revocation_markers():
    is_revocation = ctext.is_revocation
    assert is_revocation(REVOKE_A) and is_revocation(FAC_REVOKE)
    assert is_revocation("Finalement on garde l'ancien format.") and is_revocation("Annule le dernier commit.")
    for text in ("Parfait. Je prends un café, je reviens.", "Tu as des questions ? Si non, réponds juste « non ».",
                 "Commite le retour en arrière.", "Une remise ne dépasse jamais 30 %.", DECISION_A, QUESTION):
        assert not is_revocation(text), text


def test_extractive_summary_keeps_the_revocation_sentence():
    """« Reviens au point-virgule. » n'a ni nom propre ni nombre : sans poids de révocation, le résumé la perdait."""
    assert salience("Reviens au point-virgule.") > salience("Tu te souviens pourquoi on l'avait choisi ?")
    turns = [Turn(thread_id="t", index=i, input=InputCheck(text=text)) for i, text in enumerate(
        ["Lance les tests de l'export.", "Commite.", REVOKE_A, "Lance toute la suite du projet Factura en CI."])]
    assert "Reviens au point-virgule." in ExtractiveSummarizer().summarize(turns).summary


def test_cancelled_decision_does_not_come_back_as_current(tmp_path):
    s = _scene(tmp_path)
    s.say(QUESTION)
    prompt = _prompt(s)
    system = s.provider.seen[-1][0]["content"]
    head = s.provider.seen[-1][-1]["content"]

    # 1. l'annulation est dans le contexte, avec la conclusion du modèle qui nomme ce qu'elle annule
    assert "Reviens au point-virgule" in prompt
    assert "Retour au point-virgule" in prompt
    # 2. elle est dite comme EN VIGUEUR et comme remplaçant ce qui précède (couche 3, toujours présente)
    rev = system[system.index("Revirements"):]
    assert "remplace" in rev.lower() and "fait foi" in rev
    t_revoke = next(t.index for t in s.store.history(s.thread) if t.input.text == REVOKE_A)
    t_decision = next(t.index for t in s.store.history(s.thread) if t.input.text == DECISION_A)
    assert f"T{t_revoke}" in rev and t_revoke > t_decision
    # 3. dans le bloc du tour, l'ancienne décision (si elle y est) vient AVANT son annulation, et le bloc renvoie au
    #    revirement plus récent
    rel = head[head.index("[Épisodes pertinents]"):head.index("[/Épisodes]")]
    if "en virgule" in rel:
        assert f"T{t_revoke}" in rel[rel.index("en virgule"):], rel
        assert "remplace" in rel[rel.index("en virgule"):].lower()
    # épisodes et messages retrouvés sont rendus en ordre chronologique (et plus par score)
    episodes = [int(m) for m in re.findall(r"^- ep_\w+ \(#\d+, T(\d+)", rel, re.MULTILINE)]
    messages = [int(m) for m in re.findall(r"^- T(\d+) ", rel, re.MULTILINE)]
    assert episodes and episodes == sorted(episodes), rel
    assert messages == sorted(messages), rel


def test_both_cancellations_are_visible_for_a_multi_topic_request(tmp_path):
    """Tour 127 du banc : le README « Décisions » donnait la virgule et l'arrondi aux 5 centimes EN VIGUEUR."""
    s = _scene(tmp_path)
    s.say(README)
    prompt = _prompt(s)
    assert "Reviens au point-virgule" in prompt
    assert "ne veut finalement pas de FAC-217" in prompt
    assert "n'arrondit plus aux 5 centimes" in prompt


def test_no_revocation_no_section(tmp_path):
    s = make_session(ContextBudgets(current_budget=10 ** 6, episodes_budget=1500), tmp_path=tmp_path,
                     provider=CannedProvider())
    for text in [DECISION_A, *FILLERS]:
        s.say(text)
    s.manager.new_episode(s.thread)
    s.say(QUESTION)
    assert "Revirements" not in _prompt(s)


def test_revocations_stay_under_the_session_memory_budget(tmp_path):
    b = ContextBudgets(current_budget=10 ** 6, episodes_budget=300)
    s = make_session(b, tmp_path=tmp_path, provider=CannedProvider())
    for i in range(12):
        s.say(f"Finalement, annule la règle numéro {i} et reviens à l'ancienne version du module m{i}. " * 4)
        s.manager.new_episode(s.thread)
    block = s.manager.memory_block(s.thread)
    assert len(block) <= b.episodes_budget * 4 + 200
    assert "T11" in block  # les plus récents d'abord gardés
