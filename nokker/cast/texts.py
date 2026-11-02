"""Tous les textes de `nokker/cast/` que le modèle voit (descriptions d'outils, résultats, refus), en un seul endroit :
c'est cette liste qui part à la relecture avant fusion (docs/SCREEN-CAST.md §8). Rien d'autre du module n'entre dans
le contexte du modèle ; les textes affichés à l'écran ou dans la bulle ne sont pas ici."""
from __future__ import annotations

LIST_DESCRIPTION = (
    "Liste les écrans du foyer trouvés sur le réseau local (télé, Chromecast, Google TV, Apple TV) : identifiant, "
    "nom, pièce, type, ce qu'ils savent afficher, et s'ils sont déjà approuvés. À appeler avant show_on_screen.")
LIST_REFRESH = "relancer la découverte (quelques secondes) au lieu de reprendre la dernière liste"

SHOW_DESCRIPTION = (
    "Montre une page sur un écran du foyer : diagramme (SVG), tableau, image du dossier de travail, carte, diaporama "
    "de résultats ou page web en lecture. Elle est servie sur le réseau local seulement, par un lien éphémère, et "
    "rendue aussi en bulle dans le chat. Le premier affichage sur un écran, ou sur l'écran personnel d'un autre membre "
    "du foyer, attend une approbation. Avec cast_id : met à jour l'affichage en cours, ou l'arrête (stop).")
SHOW_PARAMS = {
    "screen": "identifiant, nom ou pièce de l'écran (voir list_screens)",
    "kind": "ce qui est montré",
    "title": "titre court de la page",
    "svg": "diagram : le SVG (sans script ni ressource externe)",
    "columns": "table : intitulés des colonnes",
    "rows": "table : lignes (liste de cellules, ou {cells, private})",
    "path": "image : fichier du dossier de travail (png, jpg, gif, webp)",
    "lat": "map : latitude du centre",
    "lon": "map : longitude du centre",
    "zoom": "map : zoom de 1 à 19",
    "markers": "map : points {lat, lon, label, private}",
    "slides": "slides : diapositives {title, text, image, private} ; image = fichier du dossier de travail",
    "interval_s": "slides : secondes par diapositive",
    "url": "web : la page à montrer en lecture (texte extrait)",
    "private": "true : contenu privé, jamais affiché sur un écran ; private sur un élément le masque seul",
    "cast_id": "met à jour l'affichage en cours au lieu d'en ouvrir un autre",
    "stop": "true avec cast_id : arrête l'affichage",
}

# Résultats de list_screens
LIST_EMPTY = "aucun écran trouvé sur le réseau local"
LIST_HEAD = "écrans du foyer :"
LIST_LINE = "- {id} « {name} » ({room}, {kind}) : affiche {caps} ; {state}"
CAPS = {"page": "pages", "image": "images"}
STATE_APPROVED = "approuvé"
STATE_NEW = "pas encore approuvé (le premier affichage demandera une approbation)"
STATE_MINE = " ; ton écran personnel"
STATE_OTHER = " ; écran personnel d'un autre membre (son affichage demandera un accord)"
STATE_UNPAIRED = " ; AirPlay pas encore appairé"

# Résultats de show_on_screen
UNKNOWN_SCREEN = "écran inconnu : {screen} (list_screens pour la liste)"
AMBIGUOUS_SCREEN = "plusieurs écrans correspondent à {screen} : {names} ; donne l'identifiant"
MISSING_SCREEN = "argument manquant : screen"
PRIVATE_REFUSED = "REFUSÉ : contenu marqué privé, il ne s'affiche jamais sur un écran"
REFUSED = "REFUSÉ : {why}"
IMAGES_ONLY = "« {name} » ({kind}) n'affiche que des images : kind={what} impossible sur cet écran"
NEEDS_PAIRING = ("« {name} » (AirPlay) n'est pas appairé : l'appairage se fait depuis les réglages de la machine, avec "
                 "le code affiché sur la télé")
PENDING_SCREEN = ("affichage {cast_id} en attente : premier affichage sur l'écran « {name} », à approuver dans la "
                  "bulle. Rien n'est affiché pour l'instant.")
PENDING_CONSENT = ("affichage {cast_id} en attente : « {name} » est l'écran personnel d'un autre membre du foyer ; "
                   "l'accord du membre dont c'est le contenu est demandé dans la bulle. Rien n'est affiché pour "
                   "l'instant.")
PENDING_UPDATED = "affichage {cast_id} toujours en attente d'approbation ; son contenu est remplacé."
SHOWN = ("affiché sur « {name} » ({room}) : affichage {cast_id}, lien local éphémère valable {minutes} min. Aussi en "
         "bulle dans le chat ; cast_id pour le mettre à jour ou l'arrêter.")
MASKED = " {n} élément(s) privé(s) ou secret(s) masqué(s)."
UPDATED = "affichage {cast_id} mis à jour sur « {name} »."
STOPPED = "affichage {cast_id} arrêté."
NO_CAST = "affichage inconnu ou terminé : {cast_id}"
FAILED = "échec de la diffusion sur « {name} » : {error}"
BLOCK = "<!-- nokker:cast -->{data}<!-- /nokker:cast -->"

# Motifs de refus du rendu (après « REFUSÉ : »)
WHY_KIND = "kind inconnu : {kind} (diagram, table, image, map, slides, web)"
WHY_MISSING = "argument manquant : {name}"
WHY_SVG = "SVG refusé : {reason}"
WHY_SVG_UNREADABLE = "illisible"
WHY_SVG_DOCTYPE = "DOCTYPE ou entité interdits"
WHY_SVG_ROOT = "la racine doit être <svg>"
WHY_TOO_MANY = "trop de {what} ({n} au plus)"
WHY_NO_ROOT = "aucun dossier de travail pour ce fil"
WHY_OUTSIDE = "{path} est hors du dossier de travail"
WHY_NOT_FOUND = "fichier introuvable : {path}"
WHY_IMAGE_FORMAT = "format d'image non pris en charge : {path} (png, jpg, gif, webp)"
WHY_IMAGE_SIZE = "image trop lourde : {path} ({mb} Mo au plus)"
WHY_PAGE_SIZE = "page trop lourde ({mb} Mo au plus)"
WHY_COORDS = "coordonnées invalides"
WHY_WEB = "page web inaccessible : {url}"
WHY_WEB_EMPTY = "aucun texte lisible sur cette page : {url}"

# Ajouté par le hub au résultat d'un appel qui met à jour une bulle `cast` existante
BUBBLE_UPDATED = "\n[bulle {id} (cast) mise à jour]"
