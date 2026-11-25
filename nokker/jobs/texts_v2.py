"""Textes du pack Emploi vus par le modèle (docs/JOBS-PACK.md §5.9, §5.11, §5.12), FIGÉS le 05.11.2026.

Copie mot pour mot du document : un test compare chaque description, chaque modèle d'erreur et chaque déclencheur au
document, et l'empreinte `texts_v2_sha256()` fige l'ensemble (schémas compris). Tout changement repasse par la
relecture des textes.

Règles des textes rendus : jamais « je », jamais de pronom genré (le prénom) ; jamais le nom du service emploi.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

_S, _I, _B = "string", "integer", "boolean"


def _obj(props: dict[str, dict[str, Any]], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


_PERSON = {"type": _S, "description": "membre du foyer : prénom ou id em_… ; facultatif pour soi"}
_OFFER = {"type": _S, "description": "ref d'une offre (em_…|offer:…)"}
_CV = {"type": _S, "description": "ref d'un CV : em_…|cv:… (version du service) ou cvl_… (version retouchée)"}
_SINCE = {"type": _S, "format": "date", "description": "date AAAA-MM-JJ"}
_INSTR = {"type": _S, "description": "ce que la personne demande de changer, dans ses mots"}

# §5.11 : description de base (copie exacte) + schéma de paramètres.
TOOLS: dict[str, tuple[str, dict[str, Any]]] = {
    "jobs_matches": (
        "Offres d'emploi en Suisse pour un membre du foyer : ses matchs (par défaut) ou une recherche (query). Chaque "
        "offre porte une ref pour jobs_offer, jobs_cv, jobs_letter et jobs_apply. Hors de Suisse : web_search pour "
        "trouver l'offre, puis jobs_offer url=.",
        _obj({"person": _PERSON,
              "query": {"type": _S, "description": "recherche plein texte dans les offres suisses, au lieu des matchs"},
              "min_score": {"type": _I, "minimum": 0, "maximum": 100},
              "max": {"type": _I, "minimum": 1, "maximum": 20, "description": "défaut 10"}})),
    "jobs_offer": (
        "Lit une offre (offer) ou importe une offre d'emploi d'un autre site par son url : texte complet, canal de "
        "dépôt (portail, e-mail ou manuel), forces et écarts du profil, historique avec cette entreprise.",
        _obj({"person": _PERSON, "offer": _OFFER,
              "url": {"type": _S, "description": "page d'une offre d'emploi hors des matchs (exactement offer OU url)"}})),
    "jobs_cv_attach": (
        "Rattache un CV partagé dans le fil (attachment) au profil emploi d'un membre du foyer (person, obligatoire). "
        "Demande d'abord à la personne à qui appartient ce CV ; seul le CV d'un membre du foyer est rattaché, jamais "
        "celui d'une autre personne (candidat, ami, collègue).",
        _obj({"attachment": {"type": _S, "description": "nom du fichier joint au fil"},
              "person": {"type": _S, "description": "membre du foyer à qui appartient ce CV (prénom ou id em_…)"},
              "owner_confirmed": {"type": _B, "description": "vrai seulement après un cv_owner_check, quand la "
                                                             "personne a confirmé"}},
             ["attachment", "person"])),
    "jobs_cv": (
        "Le CV d'un membre : version adaptée à une offre (offer), relecture (cv), ou retouche demandée (cv + "
        "instructions) qui crée une nouvelle version et son PDF. Signale tout fait absent du CV de base.",
        _obj({"person": _PERSON, "offer": _OFFER, "cv": _CV, "instructions": _INSTR})),
    "jobs_letter": (
        "Lettre de motivation pour une offre (offer), ou retouche d'une lettre (letter + instructions). Faits tirés du "
        "CV seulement ; signale tout fait nouveau.",
        _obj({"person": _PERSON, "offer": _OFFER, "cv": _CV,
              "language": {"type": _S, "description": "fr, de, it ou en ; défaut : langue de l'offre"},
              "letter": {"type": _S, "description": "ref d'une lettre (lt_…)"}, "instructions": _INSTR})),
    "jobs_prep": (
        "Préparation d'entretien pour une offre ou une candidature : questions probables (questions) ou dossier "
        "complet (pack).",
        _obj({"person": _PERSON, "offer": _OFFER,
              "application": {"type": _S, "description": "ref d'une candidature (em_…|app:…)"},
              "depth": {"type": _S, "enum": ["questions", "pack"]}}, ["depth"])),
    "jobs_apply": (
        "Dépose UNE candidature (offre, CV, lettre) depuis la machine de la personne, sur le portail de l'employeur "
        "ou par e-mail. Passe par une carte d'approbation qui montre tout ce qui sera envoyé. Les questions sans "
        "réponse reviennent groupées : les poser à la personne, ne jamais les deviner. Seul chemin pour envoyer une "
        "candidature, y compris par e-mail.",
        _obj({"person": _PERSON, "offer": _OFFER, "cv": _CV, "letter": {"type": _S},
              "answers": {"type": "object"}}, ["offer", "cv"])),
    "jobs_applications": (
        "Les candidatures d'un membre et leur état (envoyée, entretien, refus, offre, embauche, sans réponse), avec "
        "la preuve de dépôt.",
        _obj({"person": _PERSON,
              "status": {"type": _S, "enum": ["sent", "interview", "rejected", "offer", "hired", "no_answer", "all"]},
              "since": _SINCE, "max": {"type": _I, "minimum": 1, "maximum": 50, "description": "défaut 20"}})),
    "jobs_update": (
        "Enregistre l'état d'une candidature (réponse reçue, entretien, refus avec motif) ou un dépôt fait par la "
        "personne elle-même.",
        _obj({"person": _PERSON, "application": {"type": _S, "description": "ref d'une candidature (em_…|app:…)"},
              "offer": {"type": _S, "description": "ref d'une offre, pour un dépôt fait par la personne"},
              "result": {"type": _S, "enum": ["sent", "interview", "rejected", "offer", "hired", "no_answer"]},
              "date": {"type": _S, "format": "date", "description": "date AAAA-MM-JJ ; défaut aujourd'hui"},
              "proof": {"type": _S, "enum": ["confirmation_page", "ack_mail", "mail_sent", "declared"]},
              "reason": {"type": _S, "maxLength": 250, "description": "motif d'un refus"}}, ["result"])),
    "jobs_report": (
        "Relevé mensuel des recherches d'emploi pour l'ORP (format job-room, une ligne par candidature prouvée) ; avec "
        "enter=true, saisit dans job-room les lignes manquantes, sous carte forte. Ne transmet jamais le relevé à "
        "l'ORP : la personne le valide elle-même dans job-room.",
        _obj({"person": _PERSON, "month": {"type": _S, "description": "mois AAAA-MM"},
              "enter": {"type": _B, "description": "vrai : saisir dans job-room les lignes manquantes (carte forte) ; "
                                                   "défaut faux : le relevé seul"}}, ["month"])),
}

# Outils de la source cloud `JobsSource` (L0, L1, relevé seul). `jobs_apply` (L2) est servi par Lokkal seulement
# (`nokker/jobs/apply/tool.py`, sites connectés requis).
OFFERED = ("jobs_matches", "jobs_offer", "jobs_cv_attach", "jobs_cv", "jobs_letter", "jobs_prep",
           "jobs_applications", "jobs_update", "jobs_report")

# Outils dont la description porte la liste des personnes (format 068).
WITH_PEOPLE = ("jobs_matches", "jobs_cv_attach", "jobs_applications", "jobs_report")

# Ligne des personnes, en fin des descriptions de WITH_PEOPLE (format 068) ; la ref du CV de base y figure pour qu'un
# nouveau fil puisse l'utiliser sans jobs_cv_attach (relecture des textes, 06.11).
PEOPLE: dict[str, str] = {
    "one": " Personne : {items}.",
    "many": " Personnes : {items}.",
    "sep": " ; ",
    "with_cv": "{name} · {profile} ({access}, CV rattaché : {profile}|cv:{cv})",
    "without_cv": "{name} · {profile} ({access}, sans CV)",
    "mine": "à toi",
    "shared": "partagé",
}

# §5.11 : suffixe de la description `mail_send` quand le pack est allumé.
MAIL_SEND_SUFFIX = "Pour envoyer une candidature, utilise jobs_apply."

# §5.12 : modèles des erreurs, « code : texte ». `{…}` = valeur rendue (les valeurs de tiers passent par `inline`).
ERRORS: dict[str, str] = {
    "cv_forms": "argument_invalide : jobs_cv prend offer, OU cv, OU cv + instructions.",
    "attach_person": "argument_invalide : jobs_cv_attach demande person (à qui appartient ce CV ?).",
    "many_people": "argument_invalide : plusieurs personnes accessibles ; précise person ({names}).",
    "needs_letter": "argument_invalide : le canal de cette offre demande une lettre ; jobs_letter offer={offer}.",
    "pack_inactif": "pack_inactif : le pack Emploi n'est pas activé pour {person} ; {person} peut l'activer dans Packs.",
    "cv_absent": "cv_absent : aucun CV rattaché au profil emploi {de_person} ; demande-lui de partager son CV dans le "
                 "fil.",
    "cv_owner_check": "cv_owner_check : ce CV est au nom de « {name} », le profil est celui {de_person} ; demande à qui "
                      "il appartient avant de le rattacher ; rien n'a été conservé.",
    "cv_tiers": "cv_tiers : le pack Emploi sert aux membres du foyer ; le CV d'une autre personne n'est pas rattaché ; "
                "rien n'a été envoyé.",
    "person_unknown": "person_unknown : personne inconnue « {name} ». Personnes valides : {valid}.",
    "person_ambiguous": "person_ambiguous : « {name} » désigne plusieurs profils emploi : {ids} ; précise l'id.",
    "person_forbidden": "person_forbidden : le profil emploi {de_person} n'est pas partagé avec toi ; rien n'a été "
                        "fait.",
    "mineur": "mineur : le dépôt de candidature n'est pas ouvert aux profils mineurs ; rien n'a été fait.",
    "hors_suisse": "hors_suisse : jobs_matches ne couvre que la Suisse ; hors de Suisse : web_search pour trouver "
                   "l'offre, puis jobs_offer url=.",
    "ref_unknown": "ref_unknown : référence inconnue ou expirée ; relance jobs_matches ou jobs_applications.",
    "service_indisponible": "service_indisponible : le service emploi ne répond pas ; rien n'a été fait, réessaie dans "
                            "quelques minutes.",
    "quota": "quota : quota Nokker {de_person} atteint pour aujourd'hui (reprise demain 00:00) ; rien n'a été fait.",
    "offer_closed": "offer_closed : l'offre n'est plus en ligne (page 404 ou « poste pourvu ») ; rien n'a été envoyé.",
    "already_applied": "already_applied : {person} a déjà postulé à cette offre le {date} (preuve : {proof}) ; rien n'a "
                       "été envoyé.",
    "canal_manuel": "canal_manuel : cette offre ne se dépose que sur {portal} ; prépare les pièces (jobs_cv, "
                    "jobs_letter), la personne dépose elle-même, puis jobs_update.",
    "letter_incomplete": "letter_incomplete : la lettre contient encore « [à compléter] » ({field}) ; rien n'a été "
                         "envoyé.",
    "recipe_broken": "recipe_broken : le dépôt sur {portal} ne reconnaît plus la page (étape : {step}) ; rien n'a été "
                     "envoyé.",
    "stale": "stale : le formulaire a changé depuis la carte ({change}) ; rien n'a été envoyé ; nouvelle carte à "
             "valider.",
    "unconfirmed": "unconfirmed : le dépôt chez {company} n'a pas pu être confirmé ; il n'est pas renvoyé ; la personne "
                   "vérifie sur le portail.",
    "sites_absent": "sites_absent : le dépôt se fait depuis Lokkal et cette machine n'a pas les sites connectés ; rien "
                    "n'a été fait.",
}

# Textes du lot L0-L1 hors §5.12 (docs/JOBS-PACK.md §15.4), relus et figés le 05.11 : dans l'empreinte.
LOT_ERRORS = {
    "attachment": "argument_invalide : fichier joint « {name} » introuvable dans ce fil ; demande à la personne de le "
                  "partager à nouveau.",
    "offer_or_url": "argument_invalide : jobs_offer prend offer OU url.",
    "letter_forms": "argument_invalide : jobs_letter prend offer, OU letter, OU letter + instructions.",
    "prep_forms": "argument_invalide : jobs_prep prend offer OU application.",
    "update_forms": "argument_invalide : jobs_update prend application OU offer.",
    "bad_value": "argument_invalide : {field} invalide ({expected}).",
    "edit": "argument_invalide : retouche impossible ({why}) ; rien n'a été changé.",
    "edit_off": "edit_indisponible : la retouche n'est pas disponible sur cette instance ; rien n'a été changé.",
    "not_deposited": "not_deposited : aucun dépôt enregistré pour cette candidature ; « sans réponse » suit un dépôt "
                     "(jobs_update result=sent d'abord).",
    "after_deposit": "argument_invalide : {field} ne s'applique qu'à un dépôt (result=sent).",
    "enter": "jobroom_indisponible : la saisie dans job-room n'est pas encore ouverte ; rien n'a été saisi ; le relevé "
             "seul est disponible (jobs_report sans enter).",
}

# Lot L2 (dépôt par Lokkal, §6) : textes de `jobs_apply` rendus au modèle, hors §5.12 (à relire).
# Les libellés de portail (questions, champs) sont des textes de TIERS : clôturés (`jobs:portal:question`).
APPLY: dict[str, str] = {
    "preparing": "preparing : préparation du dépôt chez {company} sur {portal} en cours ; rien n'a été envoyé ; dis-le "
                 "à la personne et rappelle jobs_apply avec les mêmes arguments à son prochain message (questions ou "
                 "carte).",
    "questions": "questions : le formulaire {portal} {de_company} demande {reponses} ; pose-les toutes à "
                 "{person} en une fois, ne les devine jamais, puis rappelle jobs_apply avec answers={{id: réponse}}. "
                 "Rien n'a été envoyé.",
    "question": "- {id} · {kind}{required}{options}{sensitive} :",
    "required": ", obligatoire",
    "options": ", choix : {options}",
    "sensitive": ", donnée sensible (la personne peut refuser de répondre)",
    "pending_approval": "pending_approval : carte déposée pour {person} : candidature chez {company} ({title}) sur "
                        "{portal}, {account} ; {champs}, {pieces}, {consentements} montrés sur la carte. Rien n'est parti : le dépôt se fait à l'approbation (carte valable 24 h).",
    "pending_account": "pending_approval : carte forte déposée pour {person} : {portal} demande un compte chez {company} ; "
                       "un compte sera créé avec l'adresse {username_masque} (mot de passe généré dans le coffre de "
                       "la machine, jamais montré). À l'approbation : création du compte puis relevé du formulaire, sans "
                       "rien envoyer ; les questions ou la carte du dépôt suivent. Rien n'est parti.",
    "account_guest": "en invité",
    "account_login": "compte {username}",
    "account_create": "nouveau compte {username}",
    "account_made": "Compte créé chez {portal} pour {company} ({username}) ; mot de passe gardé dans le coffre de la "
                    "machine. ",
    "done": "Fait : candidature déposée chez {company} pour {title} sur {portal} ({account}). Preuve : {proof}. Déjà "
            "enregistrée (pas de jobs_update). ref : {ref}",
    # dépôt fait, enregistrement chez le service en échec : la phrase « Déjà enregistrée » ne serait pas vraie
    "done_pending": "Fait : candidature déposée chez {company} pour {title} sur {portal} ({account}). Preuve : {proof}. "
                    "ref : {ref}",
    "proof_page": "page « {heading} » capturée",
    "proof_ack": " + accusé reçu {time}",
    "to_sheet": "Réponses à proposer pour la fiche de candidature de {person} (rien n'est enregistré sans son accord) : "
                "{ids}.",
    "username": "argument_invalide : aucune adresse de candidature dans la fiche {de_person} ; demande-la (answers="
                "{{\"email\": …}}).",
    "plan_arg": "argument_invalide : jobs_apply ne prend ni plan ni empreinte ; le plan vient du relevé du formulaire "
                "et de la carte.",
    "email_channel": "canal_indisponible : cette offre se dépose par e-mail, et le dépôt par e-mail n'est pas encore "
                     "ouvert dans jobs_apply ; rien n'a été envoyé ; prépare les pièces (jobs_cv, jobs_letter), la "
                     "personne envoie elle-même, puis jobs_update result=sent.",
    "card_gone": "argument_invalide : carte d'approbation introuvable, expirée ou modifiée ; rien n'a été envoyé ; "
                 "relance jobs_apply pour une nouvelle carte.",
    "letter_ref": "argument_invalide : letter prend une ref de lettre (lt_…) ; jobs_letter offer={offer}.",
}
# Accords (singulier : 0 ou 1 ; pluriel au-delà) des comptes rendus par `jobs_apply`.
PLURALS: dict[str, tuple[str, str]] = {
    "reponses": ("{n} réponse inconnue", "{n} réponses inconnues"),
    "champs": ("{n} champ", "{n} champs"),
    "pieces": ("{n} pièce", "{n} pièces"),
    "consentements": ("{n} consentement", "{n} consentements"),
}


def count(key: str, n: int, table: dict[str, Any] | None = None) -> str:
    """« 1 réponse inconnue » / « 2 réponses inconnues » ; `table` : formes de la carte (`CARD`, hors empreinte)."""
    return (table if table is not None else PLURALS)[key][0 if n <= 1 else 1].format(n=n)


# Carte d'approbation (lue par la personne sur son téléphone ou l'écran, jamais par le modèle).
CARD: dict[str, str] = {
    "head": "Candidature au nom de {person} : {company} — {title} ({location}).",
    "where": "Où : {portal} ({account}) · {url}",
    "piece": "Pièce : {role} « {name} » · {pages} · sha256 {sha}{facts}",
    "pages": ("{n} page", "{n} pages"),
    "facts": " · faits absents du CV de base : {facts}",
    "fields": "Champs remplis :",
    "field": "- {label} : {value}{source}",
    "empty": "- {label} : laissé vide",
    "answers": "Réponses de screening :",
    "consents": "Consentements :",
    "consent_on": "- [x] {label}{beyond}",
    "consent_off": "- [ ] {label} (non donné)",
    "beyond": {"retention": " — au-delà de la candidature : conservation de plus de 12 mois",
               "ai": " — au-delà de la candidature : outils d'IA du recruteur",
               "transfer": " — au-delà de la candidature : transfert hors de Suisse et de l'UE"},
    "strong": "Carte forte : {why}.",
    "strong_why": {"account": "un compte sera créé chez le portail avec ton adresse",
                   "consent": "consentement au-delà de la candidature", "differs:salary": "prétention différente de la fiche",
                   "differs:permit": "permis différent de la fiche"},
    "account": "Un compte sera créé chez {portal} pour {company} avec l'adresse {username} ; mot de passe généré dans le "
               "coffre de la machine. Rien n'est envoyé à l'employeur : le formulaire sera relevé ensuite.",
    "warning": "Avertissement : {n} candidatures chez {company} cette année.",
    "expires": "Valable 24 h. Rien n'est envoyé avant ton approbation.",
}

# Raisons d'une retouche refusée : liste fermée (jamais le texte d'une exception).
EDIT_WHY: dict[str, str] = {"unclear": "demande non comprise", "empty": "texte vide", "missing": "section introuvable",
                            "other": "demande non applicable"}
# Import d'une offre mise en file par la collecte (résultat, pas une erreur).
IMPORT_PENDING = ("Offre en cours d'import pour {person} ; elle sera trouvable dans quelques minutes par jobs_matches "
                  "query=<intitulé du poste>.")

# §5.9 : déclencheurs de la famille Emploi (liste figée, remplacée le 06.11 : 34 entrées). Jamais seuls : « job »,
# « application », « apply », « entretien », « emploi », « Stelle », « positions ».
TRIGGERS: tuple[str, ...] = (
    "offre d'emploi", "offres d'emploi", "recherche d'emploi", "recherches d'emploi", "chercher un emploi",
    "chercher du travail", "postuler", "candidature", "candidatures", "lettre de motivation", "CV",
    "entretien d'embauche", "entretien pour un poste", "ORP", "RAV", "job-room", "cover letter", "job application",
    "job applications", "job offer", "job offers", "job search", "Bewerbung", "Bewerbungen", "Anschreiben",
    "Lebenslauf", "Arbeitsbemühungen", "Stellenangebot", "Stellenangebote", "Vorstellungsgespräch",
    "offerta di lavoro", "candidatura", "lettera di motivazione", "colloquio di lavoro",
)
FAMILY_LABEL = "Emploi"


def texts_v2_sha256() -> str:
    """Empreinte des textes figés : descriptions et schémas, erreurs (§5.12 et du lot), raisons de retouche,
    message d'import, ligne des personnes, déclencheurs, suffixe de `mail_send`."""
    blob = json.dumps({"tools": TOOLS, "errors": ERRORS, "triggers": TRIGGERS, "mail_send": MAIL_SEND_SUFFIX,
                       "lot_errors": LOT_ERRORS, "edit_why": EDIT_WHY, "import_pending": IMPORT_PENDING,
                       "people": PEOPLE, "apply": APPLY, "plurals": PLURALS},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def de(name: str) -> str:
    """« de Camille », « d'Alex » : élision devant une voyelle ou un h."""
    low = name.lower()
    if low[:1] == "y":  # « de Yann », « de Yasmine » (y + voyelle = consonne), mais « d'Yves »
        return ("de " if low[1:2] in "aeiouàâäéèêëîïôöûüœ" and low[1:2] else "d'") + name
    return ("d'" if low[:1] in "aeiouhàâäéèêëîïôöûüœ" else "de ") + name


def err(key: str, **values: str) -> str:
    if "person" in values and "{de_person}" in ERRORS[key]:
        values = {**values, "de_person": de(values["person"])}
    return ERRORS[key].format(**values)


__all__ = ["APPLY", "CARD", "PLURALS", "count", "EDIT_WHY", "ERRORS", "FAMILY_LABEL", "IMPORT_PENDING", "LOT_ERRORS", "PEOPLE", "de", "MAIL_SEND_SUFFIX", "OFFERED", "TOOLS", "TRIGGERS", "WITH_PEOPLE", "err",
           "texts_v2_sha256"]
