"""Textes des écrans du mur (lot V7). Ceux vus par le MODÈLE (description et paramètres de `screen_draft`, résultats,
refus, ligne du hub) passent par la relecture des textes du modèle avant le gel ; la raison de la carte est vue par la
PERSONNE. Drapeau `ambient.screens.enabled` éteint : aucun de ces textes n'existe pour le modèle."""
from __future__ import annotations

TOOL = "screen_draft"
DESCRIPTION = (
    "Compose un écran pour le mur du foyer avec le catalogue de composants de Nokker : titres, textes, listes, cases "
    "à cocher, chiffres, notes, horloge, prochain rendez-vous, groupes en colonnes. Tu ne donnes que la description "
    "(JSON) ; Nokker écrit la page lui-même, sans HTML ni style de ta part. L'écran s'affiche en aperçu dans une bulle ; "
    "pour le retoucher, rappelle l'outil avec le même screen_id et la description complète. Seule la personne le publie "
    "sur le mur, par le bouton de la bulle puis une carte. N'invente ni chiffre ni rendez-vous : un chiffre vient de la "
    "personne ou d'un résultat d'outil de ce fil, l'agenda vient du composant next_event. Le mur est vu par tous ceux "
    "qui passent dans la pièce : n'y mets jamais de secret (mot de passe, code, numéro de carte) ni ce que la personne "
    "n'a pas demandé d'afficher.")
BLOCKS_DESCRIPTION = (
    "composants, dans l'ordre (16 au plus, 40 en tout avec ceux des groupes). Types et champs : "
    "heading {text} ; text {text} ; list {title?, items: [textes]} ; checklist {title?, items: [{text, done}]} "
    "(ce qui se coche : courses, tâches, à faire) ; "
    "stat {label, value} ; note {text, tone?: info | warn} ; clock {} ; next_event {} ; divider {} ; "
    "group {title?, blocks: [composants, sans group]}. Avec layout columns, le premier niveau n'a que des group "
    "(trois au plus).")
PARAMETERS = {
    "type": "object", "required": ["title", "blocks"], "properties": {
        "title": {"type": "string", "description": "titre de l'écran (60 caractères au plus)"},
        "theme": {"type": "string", "enum": ["dark", "light"], "description": "dark (défaut, lisible de loin) ou light"},
        "layout": {"type": "string", "enum": ["stack", "columns"],
                   "description": "stack (défaut) : les composants l'un sous l'autre ; columns : un group par colonne"},
        "blocks": {"type": "array", "description": BLOCKS_DESCRIPTION, "items": {"type": "object"}},
        "screen_id": {"type": "string", "description": "pour retoucher un écran déjà composé dans ce fil"}}}

RESULT = ("écran {screen_id} « {title} », version {version}, en aperçu dans le chat. Pour le changer, rappelle "
          "screen_draft avec screen_id={screen_id} et la description complète. La personne le publie elle-même sur "
          "le mur (bouton « Publier sur le mur » de la bulle, puis une carte).")
INVALID = "REFUSÉ : l'écran ne suit pas le catalogue. À corriger :\n{problems}"
UNKNOWN = "REFUSÉ : écran inconnu dans ce fil ({screen_id}) ; sans screen_id, un nouvel écran est créé."
OFF = "Les écrans du mur ne sont pas activés ici."
BUBBLE_UPDATED = "\n[bulle {id} (screen) mise à jour : version {version}]"

# Vu par la PERSONNE
CARD_REASON = ("Publier l'écran « {title} » (version {version}) sur ton mur. Tous ceux qui voient le mur le "
               "verront.{event} Il remplace l'écran publié avant ; tu pourras le retirer à tout moment depuis la bulle "
               "de l'écran.")
# Ajoutée seulement si l'écran a un composant next_event (le titre ne sort que si le mur montre les titres, show_brief)
CARD_EVENT = " Si ton mur est réglé pour montrer tes rendez-vous, le titre du prochain y sera affiché."
PUBLISH_LABEL = "Publier sur le mur"

ALL_MODEL_TEXTS = (DESCRIPTION, BLOCKS_DESCRIPTION, PARAMETERS["properties"]["title"]["description"],
                   PARAMETERS["properties"]["theme"]["description"], PARAMETERS["properties"]["layout"]["description"],
                   PARAMETERS["properties"]["screen_id"]["description"], RESULT, INVALID, UNKNOWN, OFF, BUBBLE_UPDATED)
