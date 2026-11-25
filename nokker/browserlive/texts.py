"""Textes vus par le MODÈLE pour la vue navigateur en direct (relus à la relecture des textes ; copie dans docs/BROWSER-LIVE.md,
section « Textes vus par le modèle »). Tout le reste du lot n'est vu que par l'humain."""
from __future__ import annotations

POINT_TOOL = "browser_point"
POINT_DESCRIPTION = ("Montre du doigt un élément de la page à la personne qui suit le navigateur en direct (encadré sur "
                     "l'image), sans rien toucher : par exemple l'annonce qui correspond à sa demande. ref = référence "
                     "du DERNIER instantané ; why = pourquoi cet élément, en une phrase courte.")
POINT_REF = "Référence de l'élément (eN) dans le dernier browser_snapshot."
POINT_WHY = "Pourquoi tu le montres, pour la personne : une phrase, 120 caractères au plus, sans donnée saisie."

# Réponses de l'outil (une ligne, jamais le contenu de la page).
POINT_OK = "Montré à la personne : {label}."
POINT_NO_SESSION = "Aucune page ouverte dans ce fil : ouvre-la d'abord avec le navigateur."
POINT_UNKNOWN_REF = "Référence {ref} absente du dernier instantané : refais browser_snapshot et prends-en une."
POINT_SENSITIVE = "Élément sensible (mot de passe, carte, code) : je ne le montre pas."
POINT_NO_FRAME = "Pas d'image de la page à montrer pour l'instant : réessaie après le prochain geste."
POINT_NO_BOX = "Position de l'élément inconnue : impossible de le montrer sur l'image."
POINT_BAD_ARGS = "browser_point demande ref (eN) et why (une phrase)."
