"""Textes fixes d'une panne ou d'un arrêt, vus de la PERSONNE, en fr/en/de/it : une table par texte, une entrée par langue
(comme `approvals/rule_labels.py::LABELS`). Les phrases françaises sont figées mot pour mot (empreintes testées dans
`tests/unit/test_failure_label.py`) ; en/de/it : tutoiement (fr « tu », de « du », it « tu », en « you »), phrases
courtes, noms d'outils inchangés.

La langue vient du tour (`TurnInput.lang`, posée par le noyau pour sa durée : `rule_labels.lang_scope`) ; un appel peut la
passer explicitement. Une langue inconnue retombe sur la langue du contexte, puis sur `fr`.
"""
from __future__ import annotations

from nokker.approvals.rule_labels import LANGS, _l, current_lang, norm_lang


def pick(table: dict[str, str], lang: str | None = None) -> str:
    """Texte de `table` dans `lang` (sinon la langue du contexte, sinon `fr`)."""
    return table[norm_lang(lang) or current_lang()]


# Classe de panne d'amont (`loop._FAILURE_LABELS`, 9 classes) : le motif, entre parenthèses dans les phrases ci-dessous.
FAILURE_LABELS: dict[str, dict[str, str]] = {
    "timeout": _l("délai dépassé", "timed out", "Zeitüberschreitung", "tempo scaduto"),
    "key": _l("clé refusée", "key rejected", "Schlüssel abgelehnt", "chiave rifiutata"),
    "denied": _l("accès refusé", "access denied", "Zugriff verweigert", "accesso negato"),
    "service": _l("service indisponible", "service unavailable", "Dienst nicht verfügbar", "servizio non disponibile"),
    "limit": _l("limite atteinte", "limit reached", "Limit erreicht", "limite raggiunto"),
    "model": _l("modèle introuvable", "model not found", "Modell nicht gefunden", "modello non trovato"),
    "unreachable": _l("point d'accès injoignable", "server unreachable", "Endpunkt nicht erreichbar",
                      "endpoint non raggiungibile"),
    "quota": _l("quota d'utilisation atteint", "usage quota reached", "Nutzungskontingent erreicht",
                "quota di utilizzo raggiunta"),
    "other": _l("erreur inattendue", "unexpected error", "unerwarteter Fehler", "errore imprevisto"),
}

# Motif BYOK de la classe `quota` : c'est le quota du FOURNISSEUR de la personne, pas celui de Nokker (`FAILURE_LABELS["quota"]`).
BYOK_QUOTA_WHY = _l("quota atteint chez ton fournisseur", "quota reached at your provider",
                    "Kontingent bei deinem Anbieter erreicht", "quota raggiunta presso il tuo fornitore")

# Panne d'amont côté personne : message d'erreur du tour, et motif de l'arrêt (suite de « Je ne peux pas continuer : »).
ERROR_DOWN = _l("Le modèle de Nokker ne répond pas ({why}).", "Nokker's model is not responding ({why}).",
                "Das Modell von Nokker antwortet nicht ({why}).", "Il modello di Nokker non risponde ({why}).")
REASON_DOWN = _l("le modèle de Nokker ne répond pas ({why})", "Nokker's model is not responding ({why})",
                 "Das Modell von Nokker antwortet nicht ({why})", "il modello di Nokker non risponde ({why})")
# Quota d'inférence atteint (403/429 `quota_exceeded` de l'amont) : PAS une panne, donc ni « ne répond pas » ni « réessaie ».
# Deux phrases : `{when}` est la date/heure de renouvellement lisible (`loop.quota_when`) ; sans `retry_at`, la variante
# « bientôt ». Le tour échoue une fois, avec cette phrase seule (aucun repli de palier, aucune escalade).
QUOTA_REACHED = _l(
    "Quota d'utilisation atteint. Il se renouvelle {when}. Tu peux aussi passer à une formule supérieure depuis ton compte.",
    "Usage quota reached. It renews {when}. You can also upgrade to a higher plan from your account.",
    "Nutzungskontingent erreicht. Es erneuert sich {when}. Du kannst in deinem Konto auch zu einem höheren Tarif wechseln.",
    "Quota di utilizzo raggiunta. Si rinnova {when}. Puoi anche passare a un piano superiore dal tuo account.")
QUOTA_REACHED_SOON = _l(
    "Quota d'utilisation atteint. Il se renouvelle bientôt. Tu peux aussi passer à une formule supérieure depuis ton compte.",
    "Usage quota reached. It renews soon. You can also upgrade to a higher plan from your account.",
    "Nutzungskontingent erreicht. Es erneuert sich bald. Du kannst in deinem Konto auch zu einem höheren Tarif wechseln.",
    "Quota di utilizzo raggiunta. Si rinnova a breve. Puoi anche passare a un piano superiore dal tuo account.")
# `{when}` : mois en toutes lettres (tables de `context/engine.py`), heure de Zurich comme les autres messages de plafond ;
# l'année n'est dite que si l'échéance tombe dans une autre année que l'heure courante (`QUOTA_WHEN_YEAR`).
QUOTA_WHEN = _l("le {day} {month} à {hour} (heure suisse)", "on {day} {month} at {hour} (Swiss time)",
                "am {day}. {month} um {hour} (Schweizer Zeit)", "il {day} {month} alle {hour} (ora svizzera)")
QUOTA_WHEN_YEAR = _l("le {day} {month} {year} à {hour} (heure suisse)", "on {day} {month} {year} at {hour} (Swiss time)",
                     "am {day}. {month} {year} um {hour} (Schweizer Zeit)",
                     "il {day} {month} {year} alle {hour} (ora svizzera)")
ERROR_NEUTRAL = _l("Une erreur est survenue.", "Something went wrong.", "Es ist ein Fehler aufgetreten.",
                   "Si è verificato un errore.")
RETRY = _l("Réessaie dans un moment.", "Try again in a moment.", "Versuche es in einem Moment noch einmal.",
           "Riprova tra un momento.")
# Session avec `mcp_required` : un serveur indispensable ne répond pas, le tour n'a pas eu lieu (sans appel du modèle). Aucun nom de serveur
# (les noms de la config sont internes : ils restent dans `error.servers` et l'événement hôte), ni adresse, ni jeton.
MCP_REQUIRED_UNAVAILABLE = _l(
    "Un service indispensable n'est pas joignable pour le moment. Réessaie dans un instant.",
    "A required service is not reachable at the moment. Try again in a moment.",
    "Ein unverzichtbarer Dienst ist im Moment nicht erreichbar. Versuche es gleich noch einmal.",
    "Un servizio indispensabile non è raggiungibile al momento. Riprova tra un istante.")
# Réponse coupée par la limite de longueur, relances épuisées, réflexion active : le texte coupé (fin de raisonnement) n'est
# jamais affiché ; le tour échoue avec cette phrase (`turn.failure.code` = `length_truncated`).
LENGTH_FAILED = _l(
    "La réponse a été interrompue : elle était trop longue. Reformule ou découpe ta demande.",
    "The answer was interrupted: it was too long. Rephrase or split your request.",
    "Die Antwort wurde unterbrochen: Sie war zu lang. Formuliere deine Anfrage um oder teile sie auf.",
    "La risposta è stata interrotta: era troppo lunga. Riformula o dividi la tua richiesta.")
# Repli Deep -> Ship : ligne d'état affichée À PART par le client (`turn.done.notice`), jamais dans la réponse du modèle.
DEEP_FALLBACK_NOTICE = _l(
    "Réponse en mode allégé : le mode approfondi est indisponible pour le moment.",
    "Answered in lightweight mode: in-depth mode is unavailable right now.",
    "Antwort im Kompaktmodus: Der ausführliche Modus ist gerade nicht verfügbar.",
    "Risposta in modalità ridotta: la modalità approfondita non è disponibile al momento.")
CANNOT_CONTINUE = _l("Je ne peux pas continuer : {why}.", "I can't continue: {why}.",
                     "Ich kann nicht weitermachen: {why}.", "Non posso continuare: {why}.")
CANNOT_CONTINUE_BARE = _l("Je ne peux pas continuer.", "I can't continue.", "Ich kann nicht weitermachen.",
                          "Non posso continuare.")

# Échecs dont le `message` est une phrase écrite pour la personne (`loop._PERSON_SAFE_CODES`) : texte français d'origine
# -> les trois autres langues. Un message inconnu (jeton, lien) reste tel quel : il est déjà construit par l'appelant.
SAFE_MESSAGES: dict[str, dict[str, str]] = {
    "aucun modèle configuré pour ta formule BYOK": _l(
        "aucun modèle configuré pour ta formule BYOK", "no model configured for your BYOK plan",
        "kein Modell für deinen BYOK-Tarif eingerichtet", "nessun modello configurato per il tuo piano BYOK"),
}

# Modèle choisi pour UN fil qui ne répond pas. Français : texte figé, suivi de l'anglais (comme avant) ; en/de/it : seule
# la langue de la personne.
THREAD_MODEL_DOWN = _l(
    "Le modèle « {name} » choisi pour cette conversation ne répond pas ({why}). Choisis-en un autre pour "
    "cette conversation, ou retire ce choix pour revenir au modèle habituel.\n\n"
    "The model \"{name}\" chosen for this conversation is not responding ({why_en}). Pick another model for "
    "this conversation, or clear the choice to go back to the usual model.",
    "The model \"{name}\" chosen for this conversation is not responding ({why}). Pick another model for "
    "this conversation, or clear the choice to go back to the usual model.",
    "Das für dieses Gespräch gewählte Modell «{name}» antwortet nicht ({why}). Wähle ein anderes Modell für dieses "
    "Gespräch oder entferne die Auswahl, um zum üblichen Modell zurückzukehren.",
    "Il modello «{name}» scelto per questa conversazione non risponde ({why}). Scegline un altro per questa "
    "conversazione, oppure togli la scelta per tornare al modello abituale.")

# Modèle BYOK de la personne qui ne répond pas : début commun, puis la phrase d'action de la classe de panne.
BYOK_HEAD = _l(
    "Ton modèle « {name} » ne répond pas ({why}). {action}\n\n"
    "Your model \"{name}\" is not responding ({why_en}). {action_en}",
    "Your model \"{name}\" is not responding ({why}). {action}",
    "Dein Modell «{name}» antwortet nicht ({why}). {action}",
    "Il tuo modello «{name}» non risponde ({why}). {action}")
BYOK_ACTION: dict[str, dict[str, str]] = {
    "check": _l("Vérifie le point d'accès et la clé dans Mon compte → Modèles, ou repasse sur le modèle de Nokker.",
                "Check the endpoint and key in My account → Models, or switch back to Nokker's model.",
                "Prüfe Endpunkt und Schlüssel in deinen Kontoeinstellungen oder wechsle zurück zum Modell von Nokker.",
                "Controlla l'endpoint e la chiave nelle impostazioni del tuo account, oppure torna al modello di Nokker."),
    "retry": _l("Réessaie dans un moment, ou repasse sur le modèle de Nokker.",
                "Try again in a moment, or switch back to Nokker's model.",
                "Versuche es in einem Moment noch einmal oder wechsle zurück zum Modell von Nokker.",
                "Riprova tra un momento, oppure torna al modello di Nokker."),
    "limit": _l("Réessaie plus tard, vérifie ta limite d'utilisation chez ton fournisseur, ou repasse sur le modèle de Nokker.",
                "Try again later, check your usage limit with your provider, or switch back to Nokker's model.",
                "Versuche es später noch einmal, prüfe das Nutzungslimit bei deinem Anbieter oder wechsle zurück zum "
                "Modell von Nokker.",
                "Riprova più tardi, controlla il limite di utilizzo presso il tuo fornitore, oppure torna al modello "
                "di Nokker."),
    "model": _l("Vérifie le nom du modèle dans Mon compte → Modèles, ou repasse sur le modèle de Nokker.",
                "Check the model name in My account → Models, or switch back to Nokker's model.",
                "Prüfe den Modellnamen in deinen Kontoeinstellungen oder wechsle zurück zum Modell von Nokker.",
                "Controlla il nome del modello nelle impostazioni del tuo account, oppure torna al modello di Nokker."),
}
# Classe de panne -> clé de `BYOK_ACTION`.
BYOK_ACTION_OF = {"key": "check", "denied": "check", "unreachable": "check", "other": "check",
                  "timeout": "retry", "service": "retry", "limit": "limit", "model": "model", "quota": "limit"}

# ── Escaladeurs (`qa/escalate.py`, `tenancy/resources.py`) ──
# Destinataire par défaut des escalades de l'instance interne (`NokkerConfig.escalation_addressee`, `EscalatorV0.addressee`) :
# traduit seulement quand c'est ce libellé exact ; un destinataire nommé (« Alice ») reste tel quel dans toutes les langues.
DEFAULT_ADDRESSEE = "l'équipe"
TRANSMIT = _l("Je transmets à {addressee} : {reason}.", "I'm passing this on to {addressee}: {reason}.",
              "Ich leite das an {addressee} weiter: {reason}.", "Lo inoltro a {addressee}: {reason}.")
TRANSMIT_BARE = _l("Je transmets à {addressee}.", "I'm passing this on to {addressee}.",
                   "Ich leite das an {addressee} weiter.", "Lo inoltro a {addressee}.")
# Même phrase pour le destinataire par défaut : « à il team » n'existe pas en italien, d'où une table entière.
TRANSMIT_TEAM = _l("Je transmets à l'équipe : {reason}.", "I'm passing this on to the team: {reason}.",
                   "Ich leite das an das Team weiter: {reason}.", "Lo inoltro al team: {reason}.")
TRANSMIT_TEAM_BARE = _l("Je transmets à l'équipe.", "I'm passing this on to the team.",
                        "Ich leite das an das Team weiter.", "Lo inoltro al team.")
# Nokker Lokkal : la personne est l'unique interlocuteur.
NEED_YOU = _l("J'ai besoin de toi pour continuer : {reason}.", "I need you to continue: {reason}.",
              "Ich brauche dich, um weiterzumachen: {reason}.", "Ho bisogno di te per continuare: {reason}.")
NEED_YOU_BARE = _l("J'ai besoin de toi pour continuer.", "I need you to continue.",
                   "Ich brauche dich, um weiterzumachen.", "Ho bisogno di te per continuare.")
# Chez un testeur : aucun destinataire interne nommé.
TENANT_STOP = _l(
    "Je m'arrête ici : cette décision te revient, ou à une personne compétente — {reason}.",
    "I'm stopping here: this decision is yours, or a qualified person's — {reason}.",
    "Ich halte hier an: Diese Entscheidung liegt bei dir oder bei einer zuständigen Person — {reason}.",
    "Mi fermo qui: questa decisione spetta a te o a una persona competente — {reason}.")
TENANT_STOP_BARE = _l(
    "Je m'arrête ici : cette décision te revient, ou à une personne compétente.",
    "I'm stopping here: this decision is yours, or a qualified person's.",
    "Ich halte hier an: Diese Entscheidung liegt bei dir oder bei einer zuständigen Person.",
    "Mi fermo qui: questa decisione spetta a te o a una persona competente.")

# ── Réponses du noyau vues de la personne seule (`core/loop.py`, `core/approval_repeat.py`) ──
CANCEL = _l("Annulé à ta demande.", "Cancelled at your request.", "Auf deinen Wunsch abgebrochen.",
            "Annullato su tua richiesta.")
# Fin d'une réponse partielle gardée à l'annulation (`keep_partial`) : UN texte pour la personne ET pour le modèle, car la
# réponse finale (texte partiel + cette ligne) est à la fois montrée à la personne et rangée, telle quelle, comme message de
# l'assistant rejoué au modèle dans l'historique : « à ta demande » serait faux pour le modèle, qui lit l'historique.
CANCEL_PARTIAL = _l("[Réponse interrompue à la demande de la personne.]", "[Reply interrupted at the person's request.]",
                    "[Antwort auf Wunsch der Person unterbrochen.]", "[Risposta interrotta su richiesta della persona.]")
# Tour arrêté par un plafond de l'agent ou de l'objectif (`Kernel.stop_check`) ; `{stop}` : le motif, écrit pour la personne.
STOPPED = _l("Je m'arrête : {stop}.", "I'm stopping: {stop}.", "Ich halte an: {stop}.", "Mi fermo: {stop}.")
# Réponse corrigée quand elle disait « c'est noté » alors qu'aucune note n'a pu être écrite (`core/note_claims.py`).
NOTE_FAILED = _l(
    "Je n'ai pas pu noter ça : rien n'a été enregistré. Redis-le-moi pour que je réessaie.",
    "I couldn't note that down: nothing was saved. Tell me again and I'll try once more.",
    "Ich konnte das nicht notieren: Es wurde nichts gespeichert. Sage es mir noch einmal, dann versuche ich es erneut.",
    "Non ho potuto annotarlo: non è stato salvato nulla. Ripetimelo e riprovo.",
)
# Même garde, mais AUCUN `memory_note` n'a été appelé dans le tour : rien n'a été tenté, donc ni « échec » ni « je réessaie ».
NOTE_NOT_TRIED = _l(
    "Je n'ai encore rien enregistré. Redemande-le-moi pour que je l'enregistre.",
    "I haven't saved anything yet. Ask me again so I can save it.",
    "Ich habe bisher nichts gespeichert. Bitte frag noch einmal, damit ich es speichere.",
    "Per ora non ho salvato nulla. Richiedimelo e lo salvo.",
)
# Réponse corrigée quand elle disait « c'est fait » alors qu'aucune action n'a abouti dans le tour (`core/action_claims.py`) :
# gabarit + motif (`ACTION_WHY`, clés de `tool_outcomes.Outcomes.reason`).
ACTION_NOT_DONE = _l(
    "Ce n'est pas encore fait : {why}.",
    "It isn't done yet: {why}.",
    "Das ist noch nicht erledigt: {why}.",
    "Non è ancora stato fatto: {why}.")
ACTION_WHY: dict[str, dict[str, str]] = {
    "pending": _l("une carte attend ta confirmation", "a card is waiting for your confirmation",
                  "Eine Karte wartet auf deine Bestätigung", "una scheda aspetta la tua conferma"),
    "refused": _l("l'action a été refusée", "the action was refused", "Die Aktion wurde abgelehnt",
                  "l'azione è stata rifiutata"),
    "failed": _l("l'action a échoué", "the action failed", "Die Aktion ist fehlgeschlagen",
                 "l'azione non è riuscita"),
    "none": _l("aucune action n'a été lancée", "no action was started", "Es wurde keine Aktion gestartet",
               "nessuna azione è stata avviata"),
}
# Remplace le nom technique d'un outil resté dans une réponse (`core/tool_name_scrub.py`) : la personne ne lit jamais un nom d'outil.
TOOL_NAME_NEUTRAL = _l("cette action", "that action", "diese Aktion", "quell'azione")
FOLLOW_CHAIN = _l(
    "Je m'arrête : {n} suites automatiques d'affilée sans message de ta part. Écris-moi pour que je reprenne.",
    "I'm stopping: {n} automatic follow-ups in a row without a message from you. Write to me and I'll pick it up again.",
    "Ich halte an: {n} automatische Folgeschritte hintereinander ohne Nachricht von dir. Schreibe mir, damit ich "
    "weitermache.",
    "Mi fermo: {n} passaggi automatici di fila senza un tuo messaggio. Scrivimi e riprendo.")
PENDING_END = _l(
    "En attente de ta validation : une carte ({what}) attend déjà ta décision. Je ne propose pas d'autre action "
    "avec le même outil avant ta réponse ; je reprendrai avec le résultat.",
    "Waiting for your approval: a card ({what}) is already waiting for your decision. I won't propose another action "
    "with the same tool before you answer; I'll continue with the result.",
    "Warte auf deine Freigabe: Eine Karte ({what}) wartet bereits auf deine Entscheidung. Ich schlage keine weitere "
    "Aktion mit demselben Werkzeug vor, bevor du geantwortet hast; ich mache dann mit dem Ergebnis weiter.",
    "In attesa della tua conferma: una scheda ({what}) attende già la tua decisione. Non propongo altre azioni con lo "
    "stesso strumento prima della tua risposta; riprenderò con il risultato.")
APPROVAL_REPEAT = _l(
    "Je m'arrête : la même action « {name} » vient d'être validée {n} fois de suite avec le même "
    "résultat. Je ne propose pas une carte identique de plus ; écris-moi pour que je reprenne.",
    "I'm stopping: the same action \"{name}\" has just been approved {n} times in a row with the same result. I won't "
    "propose another identical card; write to me and I'll pick it up again.",
    "Ich halte an: Dieselbe Aktion «{name}» wurde gerade {n}-mal hintereinander mit demselben Ergebnis freigegeben. Ich "
    "schlage keine weitere identische Karte vor; schreibe mir, damit ich weitermache.",
    "Mi fermo: la stessa azione «{name}» è appena stata approvata {n} volte di fila con lo stesso risultato. Non "
    "propongo un'altra scheda identica; scrivimi e riprendo.")
# Bulle d'une carte approuvée dont l'outil n'a jamais rendu la main (processus arrêté pendant l'exécution, carte `interrupted`,
# docs/APPROVALS.md) : le résultat est inconnu, la personne vérifie. Vu de la personne seulement (état de la bulle).
APPROVAL_INTERRUPTED = _l(
    "Interrompue : le résultat est inconnu, à vérifier.",
    "Interrupted: the outcome is unknown, please check.",
    "Unterbrochen: Ergebnis unbekannt, bitte prüfen.",
    "Interrotta: esito sconosciuto, da verificare.")
BYOK_NOT_CONFIGURED = _l(
    "Ta formule n'inclut pas le modèle de Nokker : configure ton modèle (point d'accès, modèle "
    "et clé) dans Mon compte → Modèles, puis renvoie ton message.\n\n"
    "Your plan does not include Nokker's model: set up your model (endpoint, model and key) "
    "in My account → Models, then send your message again.",
    "Your plan does not include Nokker's model: set up your model (endpoint, model and key) "
    "in My account → Models, then send your message again.",
    "Dein Tarif enthält das Modell von Nokker nicht: Richte dein eigenes Modell (Endpunkt, Modell und Schlüssel) in "
    "deinen Kontoeinstellungen ein und sende deine Nachricht dann noch einmal.",
    "Il tuo piano non include il modello di Nokker: configura il tuo modello (endpoint, modello e chiave) nelle "
    "impostazioni del tuo account, poi invia di nuovo il messaggio.")

# Outil -> libellé lisible par la personne (`loop.HUMAN_TOOLS` pour le français, figé). Clés : `terminal`, `desktop`, `file`,
# `mail`, `none` (repli : jamais le nom brut d'un outil).
HUMAN_TOOL = {
    "terminal": _l("terminal", "terminal", "Terminal", "terminale"),
    "desktop": _l("bureau", "desktop", "Desktop", "desktop"),
    "file": _l("fichier", "file", "Datei", "file"),
    "mail": _l("e-mail", "email", "E-Mail", "e-mail"),
    "none": _l("action non nommée", "unnamed action", "nicht benannte Aktion", "azione senza nome"),
}
HUMAN_TOOL_KIND = {"terminal": "terminal", "desktop_act": "desktop", "write_file": "file", "patch": "file",
                    **dict.fromkeys(("mail_search", "mail_read", "mail_draft", "mail_move", "mail_trash", "mail_send"),
                                    "mail")}

ALL_TABLES: dict[str, dict[str, str]] = {
    "BYOK_QUOTA_WHY": BYOK_QUOTA_WHY, "QUOTA_REACHED": QUOTA_REACHED, "QUOTA_REACHED_SOON": QUOTA_REACHED_SOON, "QUOTA_WHEN": QUOTA_WHEN, "QUOTA_WHEN_YEAR": QUOTA_WHEN_YEAR,
    "ERROR_DOWN": ERROR_DOWN, "REASON_DOWN": REASON_DOWN, "ERROR_NEUTRAL": ERROR_NEUTRAL, "RETRY": RETRY,
    "DEEP_FALLBACK_NOTICE": DEEP_FALLBACK_NOTICE, "CANNOT_CONTINUE": CANNOT_CONTINUE, "CANNOT_CONTINUE_BARE": CANNOT_CONTINUE_BARE,
    "THREAD_MODEL_DOWN": THREAD_MODEL_DOWN, "BYOK_HEAD": BYOK_HEAD,
    "TRANSMIT": TRANSMIT, "TRANSMIT_BARE": TRANSMIT_BARE, "TRANSMIT_TEAM": TRANSMIT_TEAM,
    "TRANSMIT_TEAM_BARE": TRANSMIT_TEAM_BARE, "NEED_YOU": NEED_YOU, "NEED_YOU_BARE": NEED_YOU_BARE,
    "TENANT_STOP": TENANT_STOP, "TENANT_STOP_BARE": TENANT_STOP_BARE, "CANCEL": CANCEL, "NOTE_NOT_TRIED": NOTE_NOT_TRIED, "FOLLOW_CHAIN": FOLLOW_CHAIN,
    "PENDING_END": PENDING_END, "APPROVAL_REPEAT": APPROVAL_REPEAT, "APPROVAL_INTERRUPTED": APPROVAL_INTERRUPTED, "BYOK_NOT_CONFIGURED": BYOK_NOT_CONFIGURED,
    **{f"HUMAN_TOOL.{k}": v for k, v in HUMAN_TOOL.items()},
    **{f"FAILURE_LABELS.{k}": v for k, v in FAILURE_LABELS.items()},
    **{f"BYOK_ACTION.{k}": v for k, v in BYOK_ACTION.items()},
    **{f"SAFE_MESSAGES.{i}": v for i, v in enumerate(SAFE_MESSAGES.values())},
}

__all__ = [
    "ALL_TABLES",
    "APPROVAL_INTERRUPTED",
    "APPROVAL_REPEAT",
    "BYOK_ACTION",
    "BYOK_ACTION_OF",
    "BYOK_HEAD",
    "BYOK_NOT_CONFIGURED",
    "BYOK_QUOTA_WHY",
    "CANCEL",
    "CANNOT_CONTINUE",
    "CANNOT_CONTINUE_BARE",
    "DEFAULT_ADDRESSEE",
    "ERROR_DOWN",
    "ERROR_NEUTRAL",
    "FAILURE_LABELS",
    "FOLLOW_CHAIN",
    "HUMAN_TOOL",
    "HUMAN_TOOL_KIND",
    "LANGS",
    "NEED_YOU",
    "NEED_YOU_BARE",
    "PENDING_END",
    "DEEP_FALLBACK_NOTICE",
    "QUOTA_REACHED",
    "QUOTA_REACHED_SOON",
    "QUOTA_WHEN",
    "QUOTA_WHEN_YEAR",
    "REASON_DOWN",
    "RETRY",
    "SAFE_MESSAGES",
    "TENANT_STOP",
    "TENANT_STOP_BARE",
    "THREAD_MODEL_DOWN",
    "TRANSMIT",
    "TRANSMIT_BARE",
    "TRANSMIT_TEAM",
    "TRANSMIT_TEAM_BARE",
    "pick",
]
