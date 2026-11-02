"""Tous les textes de `nokker/showme/` que le modèle voit (description d'outil, résultats, refus, libellés des actions
de la bulle recopiés dans le tour de « Fait »), en un seul endroit : c'est cette liste qui part à la relecture avant
fusion (docs/SHOW-ME.md §6). Drapeau `show_me` éteint : aucun de ces textes n'existe pour le modèle."""
from __future__ import annotations

POINT_TOOL = "point_at"

POINT_DESCRIPTION = (
    "Montre au lieu de faire : pose un repère (grosse flèche et courte légende) sur un élément, sans rien cliquer ni "
    "saisir. Une seule cible : ref (élément du dernier instantané du navigateur), ui (élément de l'interface Nokker) "
    "ou image (image jointe au fil, point x/y en %). Pour guider pas à pas : step et steps ; la personne agit "
    "elle-même puis répond « Fait » dans la bulle.")
POINT_PARAMS = {
    "caption": "légende courte, à l'impératif (« Clique ici pour joindre le PDF »)",
    "ref": "référence eN lue dans browser_snapshot",
    "ui": "élément de l'interface Nokker",
    "image": "1 = dernière image jointe au fil, 2 = celle d'avant…",
    "x": "image : abscisse du point visé, en % de la largeur (0 à 100)",
    "y": "image : ordonnée du point visé, en % de la hauteur (0 à 100)",
    "step": "numéro de l'étape (guidage pas à pas)",
    "steps": "nombre total d'étapes, s'il est connu",
}
UI_TARGETS = ("composer", "attach", "send", "new_session", "sessions", "account", "bubbles")

# Résultats de point_at
POINTED = "repère posé sur {what} : « {caption} ». Rien n'a été cliqué ; la personne agit elle-même."
WHAT_REF = "« {label} » ({ref})"
WHAT_UI = "l'élément {ui} de l'interface"
WHAT_IMAGE = "l'image {n} à x={x} %, y={y} %"
STEP = " Étape {step} sur {steps}."
STEP_OPEN = " Étape {step}."
NO_TARGET = "REFUSÉ : donne une seule cible : ref, ui ou image"
NO_CAPTION = "REFUSÉ : argument manquant : caption"
BAD_REF = "REFUSÉ : ref invalide : {ref} (forme eN, lue dans browser_snapshot)"
NO_BROWSER = "REFUSÉ : aucun navigateur dans ce fil ; vise ui ou image"
STALE_REF = "REFUSÉ : {ref} absente de la page courante ; refais browser_snapshot"
BAD_UI = "REFUSÉ : ui inconnu : {ui}"
NO_IMAGE = "REFUSÉ : aucune image n°{n} dans ce fil"
BAD_POINT = "REFUSÉ : x et y vont de 0 à 100"
BAD_STEP = "REFUSÉ : step et steps vont de 1 à 20, step ≤ steps"

# Ajoutés par le hub au résultat de point_at
BUBBLE_SHOWN = "\n[bulle {id} (pointer) affichée dans le fil]"
BUBBLE_UPDATED = "\n[bulle {id} (pointer) mise à jour]"

# Titre de la bulle (recopié dans le tour qui suit un clic sur « Fait »)
TITLE = "Repère"
TITLE_STEP = "Étape {step} sur {steps}"
TITLE_STEP_OPEN = "Étape {step}"

# Actions de la bulle : la personne répond, le tour de fond porte le libellé (format existant des actions de bulle)
ACTIONS = [{"id": "done", "label": "Fait", "primary": True}, {"id": "lost", "label": "Je ne trouve pas"}]

ALL_MODEL_TEXTS = {
    "POINT_DESCRIPTION": POINT_DESCRIPTION, **{f"POINT_PARAMS.{k}": v for k, v in POINT_PARAMS.items()},
    "POINTED": POINTED, "WHAT_REF": WHAT_REF, "WHAT_UI": WHAT_UI, "WHAT_IMAGE": WHAT_IMAGE, "STEP": STEP,
    "STEP_OPEN": STEP_OPEN, "NO_TARGET": NO_TARGET, "NO_CAPTION": NO_CAPTION, "BAD_REF": BAD_REF,
    "NO_BROWSER": NO_BROWSER, "STALE_REF": STALE_REF, "BAD_UI": BAD_UI, "NO_IMAGE": NO_IMAGE, "BAD_POINT": BAD_POINT,
    "BAD_STEP": BAD_STEP, "BUBBLE_SHOWN": BUBBLE_SHOWN, "BUBBLE_UPDATED": BUBBLE_UPDATED, "TITLE": TITLE,
    "TITLE_STEP": TITLE_STEP, "TITLE_STEP_OPEN": TITLE_STEP_OPEN,
    "ACTIONS.done": ACTIONS[0]["label"], "ACTIONS.lost": ACTIONS[1]["label"],
}
