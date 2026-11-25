"""Détecteur INTER-TOURS de cartes identiques sur la chaîne `APPROVAL_CONTINUE`.

Le noyau ne voyait la répétition que PAR TOUR (`pending_tools`, 1 carte par outil et par tour) : un humain ou un script
qui valide « approuver une fois » la même action 85 fois de suite enchaînait 85 tours (machine de test, 12.11).

L'exécuteur d'approbations enregistre ici chaque carte exécutée (outil + empreinte des arguments + empreinte du
résultat) ; `Kernel._tool_step` consulte `blocked()` AVANT de poser une carte : à `max` exécutions identiques de suite
sur le fil, la suivante n'est pas posée et le tour conclut par `APPROVAL_REPEAT_REPLY`, lue par la personne seule (aucun
message ajouté dans le contexte du modèle). État par fil, en mémoire ; remis à zéro dès qu'une action différente (ou un
résultat différent) est exécutée, qu'une carte est refusée, ou qu'un vrai message de la personne arrive.
"""
from __future__ import annotations

import hashlib
import json
import threading

from nokker.core import model_texts as mt
from nokker.core import person_texts as ptx

# Texte vu par la PERSONNE seule (réponse du tour, jamais envoyée au modèle). À relire.
APPROVAL_REPEAT_REPLY = ptx.APPROVAL_REPEAT["fr"]  # figé (empreinte testée) ; autres langues : `person_texts`
# Réponse d'outil vue par le MODÈLE pour l'appel non posé (texte vrai : aucune carte n'est en attente) ; à geler.
APPROVAL_REPEAT_SKIPPED = mt.APPROVAL_REPEAT_SKIPPED["fr"]  # figé ; autres langues : `model_texts`
_NAME_MAX = 60


def safe_name(name: str) -> str:
    """Nom d'outil (MCP possible) rendu inoffensif pour une phrase : sans guillemets « » ni retours ligne, ~60 car."""
    clean = " ".join(str(name).replace("«", "").replace("»", "").split())
    return clean if len(clean) <= _NAME_MAX else clean[: _NAME_MAX - 1] + "…"

APPROVAL_REPEAT_RULE = "loop:approval_repeat"
APPROVAL_REPEAT_MAX = 3
_MAX_THREADS = 5000


def args_key(arguments: dict) -> str:
    return hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
                          .encode("utf-8")).hexdigest()


class ApprovalRepeat:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, tuple[tuple[str, str, str], int]] = {}

    def record(self, thread_id: str, tool: str, arguments: dict, result: str) -> int:
        """Une carte vient d'être exécutée ; rend le nombre d'exécutions identiques de suite (outil, arguments, résultat)."""
        sig = (tool, args_key(arguments), hashlib.sha256((result or "").encode("utf-8")).hexdigest())
        with self._lock:
            prev = self._state.get(thread_id)
            n = prev[1] + 1 if prev is not None and prev[0] == sig else 1
            self._state[thread_id] = (sig, n)
            if len(self._state) > _MAX_THREADS:  # borne mémoire : on oublie les plus anciens fils
                for k in list(self._state)[:_MAX_THREADS // 2]:
                    self._state.pop(k, None)
            return n

    def reset(self, thread_id: str) -> None:
        with self._lock:
            self._state.pop(thread_id, None)

    def blocked(self, thread_id: str, tool: str, arguments: dict, maximum: int = APPROVAL_REPEAT_MAX) -> int:
        """Nombre d'exécutions identiques de suite si la carte demandée serait la suivante au-delà du plafond, sinon 0.
        (Le résultat de la future carte est inconnu : on compare outil + arguments ; un résultat différent a déjà remis
        la chaîne à 1 à l'enregistrement.)"""
        if maximum <= 0:
            return 0
        with self._lock:
            prev = self._state.get(thread_id)
        if prev is not None and prev[1] >= maximum and prev[0][:2] == (tool, args_key(arguments)):
            return prev[1]
        return 0
