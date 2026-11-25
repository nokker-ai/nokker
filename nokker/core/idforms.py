"""Identifiants que le noyau génère : table unique (C7f), lue par `scrub_internal_ids` et par le test de recensement
(tests/unit/test_internal_id_census.py).

Un identifiant interne n'a pas à être lu par la personne dans un motif d'escalade écrit par le modèle. Chaque famille
est reconnue à sa FORME EXACTE (préfixe + alphabet + longueur du générateur), jamais au seul préfixe : `ag_total`,
`th_count`, `bg_color` restent intacts. Les formes hexadécimales exigent un chiffre (un mot comme « th_deadbeef » n'est
pas un identifiant tiré au hasard : p(sans chiffre) < 1e-5 pour 12 caractères).

`SCRUBBED` : (préfixe, alphabet, longueur) — retiré du texte vu par la personne.
`SAFE` : préfixe -> pourquoi la personne peut le lire (ou pourquoi il n'arrive jamais jusqu'à elle). Un NOUVEAU préfixe
généré dans nokker/ qui n'est dans aucune des deux tables fait échouer le test de recensement : on le classe.
"""
from __future__ import annotations

import re

HEX = "0-9a-f"
B64URL = "A-Za-z0-9_-"
ALNUM10 = "a-z0-9"

# (préfixe, classe de caractères, longueur exacte | None, minimum quand la longueur est libre)
SCRUBBED: tuple[tuple[str, str, int | None], ...] = (
    ("us_", HEX, 12),    # compte (nokker/tenancy/store.py)
    ("ag_", HEX, 12),    # agent Crew
    ("ar_", HEX, 16),    # passage d'un agent Crew
    ("gl_", HEX, 16),    # objectif
    ("sh_", HEX, 20),    # partage de fil
    ("pg_", HEX, 16),    # page publiée
    ("app_", B64URL, 16),  # application MCP
    ("bg_", HEX, 8),     # processus d'arrière-plan
    ("st_", HEX, 16),    # consigne de pilotage en cours de tour
    ("ho_", HEX, 16),    # passage de relais
    ("mc_", HEX, 16),    # machine liée
    ("mp_", HEX, 16),    # note en attente de validation
    ("lj_", HEX, 16),    # tâche d'apprentissage
    ("pt_", HEX, 16),    # tâche téléphone
    ("dp_", HEX, 16),    # écran ambiant
    ("scr_", HEX, 12),   # écran du mur
    ("ex_", HEX, 20),    # export de compte
    ("pc_", HEX, 12), ("pp_", HEX, 12),  # carte et proposition de plan
    ("as_", HEX, 12), ("in_", HEX, 12),  # source et incident
    ("fd_", HEX, 12),    # passage d'un dossier
    ("dr_", HEX, 20),    # requête d'appareil
    ("rc_", HEX, 16),    # audit de relais
    ("pfg_", HEX, 16),   # garde de sortie réseau
    ("bs_", HEX, 16), ("bf_", B64URL, 16),  # session et image du navigateur en direct
    ("cs_", HEX, 16),    # session de code
    ("dk_", HEX, 16), ("dt_", HEX, 16), ("ds_", HEX, 16),  # bureau : nœud, tâche, étape
    ("cast_", HEX, 12), ("cr_", HEX, 12),  # diffusion et sa demande
    ("ep_", HEX, 12), ("rs_", HEX, 12), ("dy_", HEX, 12),  # contexte : épisode, réservoir, jour
    ("fh_", HEX, 16),    # crochet de forge
    ("iv_", HEX, 16), ("pj_", HEX, 12),  # invitation et projet d'équipe
    ("md_", HEX, 16),    # mandat de paiement
    ("rq_", HEX, 16),    # demande de site
    ("sc_", HEX, 16),    # connexion SSO
    ("sg_", HEX, 20),    # suggestion de règle d'approbation
    ("pr_", HEX, 16),    # preuve de mandat de paiement
    ("oa_", "abcdefghijkmnpqrstuvwxyz23456789", 10),  # session OAuth d'un connecteur
    ("cli-", HEX, 8), ("code-", HEX, 8), ("mcp-", HEX, 12),  # fils ouverts par la CLI et le serveur MCP
    ("lk-", HEX, 24),    # fil miroir d'une session locale
    ("qn_", HEX, 16),    # question à choix de `clarify` (API hôte, nokker/tools/clarify.py)
    # secrets de portée de service : jamais à écrire, quelle que soit la longueur
    ("dn_", B64URL, None), ("nkb_", B64URL, None), ("whsec_", B64URL, None),
)
SECRET_MIN = 20

# Préfixe -> raison de ne pas le caviarder.
SAFE: dict[str, str] = {
    "acc_": "identifiant d'appareil ACCORD (docs/ACCORD-DEVICE.md) : montré à la personne dans l'app (liste des "
            "appareils) et écrit au ledger (`via`), jamais dans un texte du modèle",
    "call_": "identifiant d'appel d'outil : n'est jamais montré ni cité (client et journal seulement)",
    "direct-": "identifiant d'appel d'outil d'un geste direct du propriétaire : jamais dans un texte de modèle",
    "app-": "identifiant d'appel d'outil d'une application : idem",
    "xc-": "identifiant d'appel d'outil d'un appel étranger : idem",
    "del-": "identifiant d'appel d'outil de la suppression de compte : idem",
    "zone-": "identifiant d'appel d'outil d'un scénario de zone : idem",
    "grant-": "identifiant d'appel d'outil d'un octroi de connecteur : idem",
    "phone-": "identifiant d'appel d'outil d'un enrôlement téléphone : idem",
    "wall-": "identifiant d'appel d'outil du mur : idem",
    "tr-": "étiquette de banc d'amélioration (nokker/improve) : jamais une donnée de la personne",
    "label-": "clé de score d'un banc d'amélioration : idem",
    "nokker-": "frontière d'un corps multipart et nom de conteneur : jamais vu du modèle",
    "nokker-sbx-": "nom de bac à sable : jamais vu du modèle",
    "fc-": "UID de l'agenda familial : montré à la personne dans SON agenda, c'est une donnée d'elle",
    "fc_": "brouillon d'agenda familial : montré à la personne pour validation",
    "mg-": "code court montré à l'opérateur à la place d'une adresse : fait pour être lu",
    "em_": "référence de profil emploi : montrée à la personne dans ses listes (10 car. alphanumériques)",
    "cvl_": "référence de CV adapté : idem",
    "lt_": "référence de lettre : idem",
    "hp_": "projet du foyer : affiché au foyer",
    "ht_": "tâche du foyer : affichée au foyer",
    "pd_": "brouillon ou document du foyer : affiché au foyer",
    "px_": "export de papiers du foyer : le lien est remis à la personne",
    "ev_": "identifiant d'évènement dérivé du texte (mur du foyer) : pas aléatoire, montré au foyer",
    "att_": "pièce jointe de la personne : dérivée du contenu, ne désigne rien d'autre que son fichier",
    "ca_": "identifiant de calendrier d'appareil dérivé : jamais vu du modèle",
    "t-": "pseudonyme de fil dans un export de traces : fait pour sortir",
    "mb_": "référence de connecteur courrier : montrée sur la carte Connecteurs",
    "ct_": "référence de connecteur contacts : idem", "fl_": "référence de connecteur fichiers : idem",
    "tk_": "référence de connecteur tâches : idem",
    "s-": "clé d'index mémoire dérivée d'un fil : jamais montrée ni citée par un outil",
    "p-": "clé de mémoire de projet dérivée : idem",
    "anon-": "UID d'évènement de calendrier sans UID, dérivé du résumé : c'est la donnée de la personne",
    "codex-": "nom de fichier de note de projet (date + titre) : lisible par construction",
    "pl_": "plan de candidature : affiché à la personne sur sa carte de validation (12 car. alphanumériques)",
}

# Forme souple historique (C7e) : le locataire, le fil et la bulle de l'instance courante portent aussi des noms choisis
# (`tn_ninabot`) ; ils restent retirés dès 3 caractères alphanumériques, sauf `th_`/`bu_`/`ap_` qui exigent un chiffre
# (`th_count` est un nom de code de la personne, `th_9f8e7d6c` un fil).
_LEGACY = r"tn_[A-Za-z0-9]{3,}|(?:th|bu|ap)_(?=[A-Za-z]*\d)[A-Za-z0-9]{3,}"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"  # carte d'approbation, appareil
_SUBAGENT = r"sa-(?=[0-9a-f]*\d)[0-9a-f]{8}"  # `<fil parent>/sa-<id>` : l'id du sous-agent


def _form(prefix: str, chars: str, n: int | None) -> str:
    body = (f"[{chars}]{{{SECRET_MIN},}}" if n is None
            else (f"(?=[a-f]*\\d)[{chars}]{{{n}}}" if chars == HEX else f"[{chars}]{{{n}}}"))
    return re.escape(prefix) + body


ID_PATTERN = (r"\[approval:[^\]]*\]|(?:/" + _SUBAGENT + r")(?![A-Za-z0-9_])|(?<![A-Za-z0-9_])(?:" + "|".join(
    [_LEGACY, _UUID, _SUBAGENT] + [_form(p, c, n) for p, c, n in SCRUBBED]) + r")(?![A-Za-z0-9_])")
INTERNAL_ID = re.compile(ID_PATTERN)


def scrubbed_prefixes() -> set[str]:
    return {p for p, _, _ in SCRUBBED} | {"sa-", "tn_", "th_", "bu_", "ap_"}
