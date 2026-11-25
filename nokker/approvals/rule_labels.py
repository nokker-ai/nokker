"""Libellés humains des règles d'approbation (lot V3 « Agent qui montre », docs/BROWSER-LIVE.md §1).

`Approval.rule` est un identifiant technique (`effect:send_external`, `household:guardian`…). La carte en montre un
libellé court, localisé fr/en/de/it, lu par l'HUMAIN (jamais envoyé au modèle). Une seule table, ici ; une règle
inconnue retombe sur son identifiant (`rule_label` ne lève jamais).

Résolution, dans l'ordre :
1. la règle exacte ;
2. la règle sans son suffixe de contexte (`:relayed`, `:sous-agent`, `:session-code`, `:papiers`, `:mention:<fam>`) :
   le libellé de base, suivi d'une précision quand le suffixe en a une ;
3. le plus long préfixe connu, segment par segment (`jobs:apply:<ats>` → `jobs:apply`, `relay:<c>:<t>` → `relay`) ;
4. l'identifiant brut.

`rule_source(rule)` : `{"kind": "system"}` pour toutes les règles d'aujourd'hui. Le genre `household` est réservé à une
règle NOMMÉE PAR LE FOYER ; il n'en existe pas encore dans le noyau (les règles `household:*` sont des règles du système
appliquées au foyer, pas des règles écrites par lui).

Langue de la requête : `set_request_lang(accept_language, path)` (posé par le serveur à chaque requête, `?lang=` puis
`Accept-Language`) ; défaut `fr`.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
from urllib.parse import parse_qs, urlsplit

LANGS = ("fr", "en", "de", "it")
DEFAULT_LANG = "fr"
_LANG: contextvars.ContextVar[str] = contextvars.ContextVar("nokker_rule_label_lang", default=DEFAULT_LANG)
# Langue que la requête en cours PORTE elle-même (`?lang=` ou `Accept-Language`) ; None = la requête n'en dit rien (`_LANG`
# vaut alors le défaut). Sert à distinguer « la personne a écrit en de » de « repli fr ».
_EXPLICIT: contextvars.ContextVar[str | None] = contextvars.ContextVar("nokker_request_lang_explicit", default=None)


def _l(fr: str, en: str, de: str, it: str) -> dict[str, str]:
    return {"fr": fr, "en": en, "de": de, "it": it}


# ── table unique ──
LABELS: dict[str, dict[str, str]] = {
    # effets (qa/invariants.py)
    "effect:send_external": _l("Envoi vers l'extérieur", "Sending outside", "Versand nach aussen", "Invio all'esterno"),
    "effect:send_external:allowlist": _l("Envoi vers l'extérieur", "Sending outside", "Versand nach aussen",
                                         "Invio all'esterno"),
    "effect:send_bulk": _l("Envoi en masse", "Bulk sending", "Massenversand", "Invio di massa"),
    "effect:spend": _l("Dépense", "Spending", "Ausgabe", "Spesa"),
    "effect:secret": _l("Accès à un secret", "Access to a secret", "Zugriff auf ein Geheimnis", "Accesso a un segreto"),
    "effect:destructive": _l("Action irréversible", "Irreversible action", "Unumkehrbare Aktion", "Azione irreversibile"),
    "effect:destructive:go": _l("Action irréversible demandée par écrit", "Irreversible action asked in writing",
                                "Schriftlich verlangte unumkehrbare Aktion", "Azione irreversibile chiesta per iscritto"),
    "effect:destructive:unauthorized_sender": _l("Action irréversible demandée par une autre personne",
                                                 "Irreversible action asked by someone else",
                                                 "Unumkehrbare Aktion, von jemand anderem verlangt",
                                                 "Azione irreversibile chiesta da un'altra persona"),
    "effect:write": _l("Modification", "Change", "Änderung", "Modifica"),
    "effect:read": _l("Lecture", "Reading", "Lesen", "Lettura"),
    # foyer
    "family:calendar": _l("Calendrier familial", "Family calendar", "Familienkalender", "Calendario di famiglia"),
    "family:project": _l("Projet du foyer", "Household project", "Haushaltsprojekt", "Progetto di famiglia"),
    "wall:publish": _l("Publication d'un écran sur le mur", "Publishing a screen on the wall",
                       "Veröffentlichung eines Bildschirms an der Wand", "Pubblicazione di una schermata sulla parete"),
    "desktop:enroll": _l("Liaison d'un bureau à Nokker", "Linking a desktop to Nokker",
                         "Verknüpfung eines Desktops mit Nokker", "Collegamento di un desktop a Nokker"),
    "household:guardian": _l("Profil mineur — l'accord du représentant légal est requis",
                             "Minor profile — needs the guardian's OK",
                             "Minderjähriges Profil — braucht das OK der gesetzlichen Vertretung",
                             "Profilo minorenne — serve l'OK del rappresentante legale"),
    "household:suspended": _l("Profil suspendu", "Profile suspended", "Profil gesperrt", "Profilo sospeso"),
    "household": _l("Règle du foyer", "Household rule", "Haushaltsregel", "Regola della famiglia"),
    "house:strong": _l("Action sensible dans la salle Maison", "Sensitive action in the House room",
                       "Heikle Aktion im Hausraum", "Azione sensibile nella stanza Casa"),
    "house:acl": _l("Hors des droits du profil", "Beyond the profile's rights", "Über die Profilrechte hinaus",
                    "Oltre i diritti del profilo"),
    "house:suspended": _l("Profil suspendu", "Profile suspended", "Profil gesperrt", "Profilo sospeso"),
    "paper:payment": _l("Paiement d'un papier", "Paying a bill", "Zahlung eines Schreibens", "Pagamento di un documento"),
    "paper:calendar": _l("Date d'un papier dans l'agenda", "Bill date in the calendar", "Termin aus einem Schreiben",
                         "Data di un documento in agenda"),
    "paper:share": _l("Partage d'un papier", "Sharing a document", "Teilen eines Schreibens", "Condivisione di un documento"),
    "paper:error": _l("Papier à relire", "Document to check", "Schreiben zu prüfen", "Documento da verificare"),
    # paiements
    "payments:to_pay": _l("Paiement", "Payment", "Zahlung", "Pagamento"),
    "payments:card_field": _l("Champ de carte bancaire", "Bank card field", "Kartenfeld", "Campo della carta"),
    "payments:checkout": _l("Page de paiement", "Checkout page", "Zahlungsseite", "Pagina di pagamento"),
    "payments:duplicate": _l("Paiement peut-être en double", "Possibly duplicate payment", "Möglicherweise doppelte Zahlung",
                             "Pagamento forse doppio"),
    "payments:invalid": _l("Paiement à vérifier", "Payment to check", "Zahlung zu prüfen", "Pagamento da verificare"),
    "domaine:montant:carte-a-payer": _l("Paiement", "Payment", "Zahlung", "Pagamento"),
    # permissions (qa/permissions.py)
    "permissions:relayed_connector": _l("Action relayée par nokker.ai", "Action relayed by nokker.ai",
                                        "Über nokker.ai weitergeleitete Aktion", "Azione inoltrata da nokker.ai"),
    "permissions:relayed_strong": _l("Action sensible demandée à distance", "Sensitive action asked remotely",
                                     "Heikle Aktion aus der Ferne", "Azione sensibile chiesta a distanza"),
    "permissions:go_sandbox_only": _l("Accord écrit limité au bac à sable", "Written go limited to the sandbox",
                                      "Schriftliches OK nur in der Sandbox", "Via libera scritto solo nella sandbox"),
    "permissions:local_connector_strong": _l("Action sensible sur ta boîte mail", "Sensitive action on your mailbox",
                                             "Heikle Aktion in deinem Postfach", "Azione sensibile sulla tua casella"),
    "permissions:no_code_session": _l("Hors session de code", "Outside a code session", "Ausserhalb einer Code-Sitzung",
                                      "Fuori da una sessione di codice"),
    "permissions:hardline": _l("Action protégée par une règle stricte", "Action covered by a strict rule",
                               "Durch eine strenge Regel geschützte Aktion", "Azione protetta da una regola rigida"),
    "permissions": _l("Mode de permissions", "Permission mode", "Berechtigungsmodus", "Modalità dei permessi"),
    "relay:strong": _l("Action sensible relayée", "Sensitive relayed action", "Heikle weitergeleitete Aktion",
                       "Azione sensibile inoltrata"),
    "relay": _l("Action relayée", "Relayed action", "Weitergeleitete Aktion", "Azione inoltrata"),
    "provenance": _l("Contenu d'origine non fiable", "Untrusted content", "Inhalt unsicherer Herkunft",
                     "Contenuto di origine non affidabile"),
    "masked_value": _l("Donnée masquée sur l'appareil", "Value masked on the device", "Auf dem Gerät maskierter Wert",
                       "Valore mascherato sul dispositivo"),
    "readonly": _l("Lecture seule", "Read-only", "Nur lesen", "Sola lettura"),
    "mcp:read_tools_write_names": _l("Outil déclaré en lecture qui écrit", "Read tool that writes",
                                     "Lesewerkzeug, das schreibt", "Strumento di lettura che scrive"),
    "mcp:stdio_server": _l("Serveur MCP lancé dans le bac à sable", "MCP server run in the sandbox",
                           "MCP-Server in der Sandbox", "Server MCP avviato nella sandbox"),
    "mcp:required_unavailable": _l("Serveur MCP indispensable injoignable", "Required MCP server unreachable",
                                   "Unverzichtbarer MCP-Server nicht erreichbar", "Server MCP indispensabile non raggiungibile"),
    "connector:grant": _l("Accès à un compte connecté", "Access to a connected account", "Zugriff auf ein verbundenes Konto",
                          "Accesso a un account collegato"),
    "account:delete": _l("Suppression du compte", "Account deletion", "Kontolöschung", "Eliminazione dell'account"),
    # maison et domotique
    "home:strong": _l("Action sensible dans la maison", "Sensitive home action", "Heikle Aktion im Haus",
                      "Azione sensibile in casa"),
    "home:unasked": _l("Tu n'as pas demandé d'agir sur la maison : confirme avant que j'agisse",
                       "You didn't ask me to act on your home: confirm before I do",
                       "Du hast mich nicht gebeten, etwas im Haus zu steuern: Bestätige die Karte, bevor ich handle.",
                       "Non mi hai chiesto di intervenire sulla casa: conferma la scheda prima che io agisca."),
    "home:member": _l("Action réservée à un membre", "Member-only action", "Nur für Mitglieder", "Azione riservata ai membri"),
    "home:deny": _l("Commande de la maison non permise", "Home control not allowed", "Haussteuerung nicht erlaubt",
                    "Comando della casa non consentito"),
    "home_watch:strong": _l("Veille du foyer : action sensible", "Home watch: sensitive action",
                            "Hausüberwachung: heikle Aktion", "Sorveglianza: azione sensibile"),
    "home_watch:read": _l("Veille du foyer : lecture", "Home watch: reading", "Hausüberwachung: Lesen",
                          "Sorveglianza: lettura"),
    "home_watch:member": _l("Veille du foyer : réservé aux membres", "Home watch: members only",
                            "Hausüberwachung: nur Mitglieder", "Sorveglianza: solo membri"),
    "home_watch:deny": _l("Veille du foyer : non permis", "Home watch: not allowed", "Hausüberwachung: nicht erlaubt",
                          "Sorveglianza: non consentito"),
    # publications, opérations, appareils
    "pages:publish": _l("Publication d'une page", "Publishing a page", "Veröffentlichung einer Seite",
                        "Pubblicazione di una pagina"),
    "ops:prod": _l("Action sur la production", "Production action", "Aktion in der Produktion", "Azione in produzione"),
    "ops:trust_host": _l("Nouvelle machine de confiance", "New trusted host", "Neuer vertrauenswürdiger Host",
                         "Nuovo host di fiducia"),
    "phone:enroll": _l("Ajout d'un téléphone", "Adding a phone", "Telefon hinzufügen", "Aggiunta di un telefono"),
    "improve:proposal": _l("Amélioration proposée", "Proposed improvement", "Vorgeschlagene Verbesserung",
                           "Miglioramento proposto"),
    "learning:card": _l("Nouvelle compétence apprise", "New learned skill", "Neu gelernte Fähigkeit",
                        "Nuova competenza appresa"),
    "learning:refused": _l("Compétence non retenue", "Skill not kept", "Fähigkeit nicht übernommen",
                           "Competenza non tenuta"),
    "jobs:apply:scan": _l("Candidature : vérification de la page", "Application: page check", "Bewerbung: Seitenprüfung",
                          "Candidatura: controllo della pagina"),
    "jobs:apply": _l("Envoi d'une candidature", "Sending a job application", "Bewerbung senden", "Invio di una candidatura"),
    "zone:scenario": _l("Scénario de zone", "Zone scenario", "Zonen-Szenario", "Scenario di zona"),
    "approvals:auto_review": _l("Relecture automatique", "Automatic review", "Automatische Prüfung", "Revisione automatica"),
    "agent:no_written_go": _l("Agent sans accord écrit", "Agent without written go", "Agent ohne schriftliches OK",
                              "Agente senza via libera scritto"),
    "agent:auto_approve": _l("Agent : accord automatique", "Agent: auto-approval", "Agent: automatische Zustimmung",
                             "Agente: approvazione automatica"),
    "agent:tool_not_declared": _l("Agent : outil non déclaré", "Agent: undeclared tool", "Agent: nicht deklariertes Werkzeug",
                                  "Agente: strumento non dichiarato"),
    "agent:event_write": _l("Agent : écriture sur événement", "Agent: write on event", "Agent: Schreiben bei Ereignis",
                            "Agente: scrittura su evento"),
    "agent:arm_write_rules": _l("Agent : règles d'écriture", "Agent: write rules", "Agent: Schreibregeln",
                                "Agente: regole di scrittura"),
    "brief:mail_delivery": _l("Envoi du résumé par mail", "Brief sent by email", "Zusammenfassung per Mail",
                              "Invio del riepilogo per email"),
    # verdicts du tour (pas des cartes, mais un identifiant connu garde un libellé)
    "turn": _l("Tour", "Turn", "Runde", "Turno"),
    "router": _l("Routage du modèle", "Model routing", "Modell-Routing", "Instradamento del modello"),
    "judge": _l("Avis du juge", "Judge's opinion", "Urteil des Prüfers", "Parere del giudice"),
    "judge-nul": _l("Avis du juge", "Judge's opinion", "Urteil des Prüfers", "Parere del giudice"),
    "loop": _l("Déroulement du tour", "Turn flow", "Rundenablauf", "Svolgimento del turno"),
    "limits": _l("Plafond d'usage", "Usage limit", "Nutzungsgrenze", "Limite di utilizzo"),
    "project_budget": _l("Budget du projet", "Project budget", "Projektbudget", "Budget del progetto"),
    "memory": _l("Mémoire", "Memory", "Gedächtnis", "Memoria"),
    "images": _l("Images", "Images", "Bilder", "Immagini"),
    "byok": _l("Clé de modèle personnelle", "Own model key", "Eigener Modellschlüssel", "Chiave del modello personale"),
}

# Précision ajoutée au libellé de base pour un suffixe de contexte (vide = rien à ajouter).
SUFFIXES: dict[str, dict[str, str]] = {
    ":relayed": _l(" (demandé à distance)", " (asked remotely)", " (aus der Ferne)", " (chiesto a distanza)"),
    ":sous-agent": _l(" (sous-agent)", " (sub-agent)", " (Unteragent)", " (sotto-agente)"),
    ":session-code": _l(" (session de code)", " (code session)", " (Code-Sitzung)", " (sessione di codice)"),
    ":papiers": _l(" (papiers)", " (documents)", " (Schreiben)", " (documenti)"),
    ":nothing": _l("", "", "", ""),
    ":invalid": _l(" (à revoir)", " (to review)", " (zu prüfen)", " (da rivedere)"),
    ":error": _l(" (erreur)", " (error)", " (Fehler)", " (errore)"),
}


# Une carte d'approbation n'est pas un refus : elle DEMANDE l'accord de la personne. Ces règles-là disent ce qui est
# protégé, suivi de « ton accord est requis » (relecture de la vidéo V3, 07.11). Le front affiche le libellé seul, ou
# « Demande ton accord : {label} » pour les autres — jamais « Bloqué par : ».
ASK = _l(" — ton accord est requis", " — needs your OK", " — braucht dein OK", " — serve il tuo OK")
ASKING = frozenset({
    "effect:send_external", "effect:send_external:allowlist", "effect:send_bulk", "effect:spend", "effect:secret",
    "effect:destructive", "effect:destructive:go", "effect:destructive:unauthorized_sender", "effect:write",
    "family:calendar", "family:project", "wall:publish", "desktop:enroll", "paper:payment", "paper:calendar", "paper:share", "payments:to_pay", "payments:card_field",
    "payments:checkout", "domaine:montant:carte-a-payer", "permissions:relayed_connector", "permissions:relayed_strong",
    "permissions:go_sandbox_only", "permissions:local_connector_strong", "permissions", "relay:strong", "relay",
    "provenance", "masked_value", "connector:grant", "account:delete", "home:strong", "home_watch:strong",
    "house:strong", "pages:publish", "ops:prod", "ops:trust_host", "phone:enroll", "improve:proposal", "learning:card",
    "jobs:apply", "jobs:apply:scan", "agent:no_written_go", "mcp:read_tools_write_names", "mcp:stdio_server",
    "readonly"})


def _lang(lang: str | None) -> str:
    lang = (lang or _LANG.get() or DEFAULT_LANG)[:2].lower()
    return lang if lang in LANGS else DEFAULT_LANG


def _lookup(rule: str) -> dict[str, str] | None:
    """Libellés de la règle (exacte, puis plus long préfixe connu), « ton accord est requis » ajouté pour une règle
    qui pose une carte (`ASKING`)."""
    key = rule if rule in LABELS else None
    if key is None:
        parts = rule.split(":")
        for n in range(len(parts) - 1, 0, -1):  # plus long préfixe connu
            if ":".join(parts[:n]) in LABELS:
                key = ":".join(parts[:n])
                break
    if key is None:
        return None
    if key in ASKING:
        return {lg: LABELS[key][lg] + "{ctx}" + ASK[lg] for lg in LANGS}
    return {lg: LABELS[key][lg] + "{ctx}" for lg in LANGS}


# Règles de domaine (`domaine:<drapeaux triés joints par +>`, qa/gates.py::domain_verdict) : le libellé se compose, un
# drapeau après l'autre. Chaque drapeau : (forme en tête de phrase, forme après un autre) par langue ; en allemand,
# seul l'adjectif prend une minuscule (« Steuerfrage und finanzielle Verpflichtung »). Un drapeau hors de cette table :
# aucun libellé (repli sur la règle, que l'appelant ne montre pas).
DOMAIN_FLAGS: dict[str, dict[str, tuple[str, str]]] = {
    "fiscal": {"fr": ("Question fiscale", "question fiscale"), "en": ("Tax question", "tax question"),
               "de": ("Steuerfrage", "Steuerfrage"), "it": ("Questione fiscale", "questione fiscale")},
    "juridique": {"fr": ("Question juridique", "question juridique"), "en": ("Legal question", "legal question"),
                  "de": ("Rechtsfrage", "Rechtsfrage"), "it": ("Questione legale", "questione legale")},
    "montant": {"fr": ("Montant engageant", "montant engageant"), "en": ("Financial commitment", "financial commitment"),
                "de": ("Finanzielle Verpflichtung", "finanzielle Verpflichtung"),
                "it": ("Impegno di spesa", "impegno di spesa")},
}
_AND = {"fr": "et", "en": "and", "de": "und", "it": "e"}


def _domain_label(rule: str, lg: str) -> str | None:
    if not rule.startswith("domaine:") or rule in LABELS:
        return None
    flags = rule[len("domaine:"):].split("+")
    if not flags or any(f not in DOMAIN_FLAGS for f in flags):
        return None
    parts = [DOMAIN_FLAGS[f][lg][0 if i == 0 else 1] for i, f in enumerate(flags)]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + f" {_AND[lg]} {parts[-1]}"


def rule_label(rule: str | None, lang: str | None = None) -> str:
    """Libellé humain de `rule` dans `lang` (défaut : langue de la requête, puis `fr`) ; repli : `rule` lui-même."""
    rule = rule or ""
    lg = _lang(lang)
    composed = _domain_label(rule, lg)
    if composed is not None:
        return composed
    if ":mention:" in rule:
        base = rule.split(":mention:", 1)[0]
        hit = _lookup(base)
        if hit is not None:
            return hit[lg].replace("{ctx}", "")
    for suffix, extra in SUFFIXES.items():
        if rule.endswith(suffix) and rule not in LABELS:
            hit = _lookup(rule[: -len(suffix)])
            if hit is not None:
                return hit[lg].replace("{ctx}", extra[lg])
    hit = _lookup(rule)
    return hit[lg].replace("{ctx}", "") if hit is not None else rule


def rule_labels(rule: str | None) -> dict[str, str]:
    """Les quatre langues d'un coup (le client choisit)."""
    return {lg: rule_label(rule, lg) for lg in LANGS}


def rule_source(rule: str | None) -> dict[str, str]:
    """`{"kind": "system"}` : aucune règle nommée par le foyer n'existe encore dans le noyau (voir l'en-tête)."""
    return {"kind": "system"}


def set_request_lang(accept_language: str | None, path: str | None = None) -> None:
    """Langue de la requête en cours : `?lang=xx` d'abord, puis la première langue connue d'`Accept-Language`."""
    lang = None
    if path and "?" in path:
        q = parse_qs(urlsplit(path).query).get("lang")
        if q and q[0][:2].lower() in LANGS:
            lang = q[0][:2].lower()
    if lang is None and accept_language:
        for part in accept_language.split(","):
            code = part.split(";")[0].strip()[:2].lower()
            if code in LANGS:
                lang = code
                break
    _LANG.set(lang or DEFAULT_LANG)
    _EXPLICIT.set(lang)


def norm_lang(lang: str | None) -> str | None:
    """`fr`/`en`/`de`/`it` (deux premières lettres, sans casse), sinon None."""
    code = (str(lang or "")[:2]).lower()
    return code if code in LANGS else None


def request_lang() -> str | None:
    """Langue que la requête en cours porte elle-même, ou None."""
    return _EXPLICIT.get()


def current_lang() -> str:
    """Langue des textes fixes vus de la personne dans le contexte courant (`fr` par défaut)."""
    return _LANG.get()


@contextlib.contextmanager
def lang_scope(lang: str | None):
    """Pose la langue des textes fixes pour la durée du bloc, puis la restaure (le noyau l'ouvre une fois par tour)."""
    token = _LANG.set(norm_lang(lang) or _LANG.get())
    try:
        yield
    finally:
        _LANG.reset(token)


def installation_lang() -> str | None:
    """Langue de l'installation (`NOKKER_LANG`, fr/en/de/it), ou None."""
    return norm_lang(os.environ.get("NOKKER_LANG"))


def resolve_lang(*, explicit: str | None = None, account: str | None = None, detected: str | None = None,
                 thread: str | None = None, installation: str | None = None) -> str:
    """Langue d'un tour, dans l'ordre : celle que la personne vient d'écrire (`explicit` : requête, `language_code`),
    celle de son compte quand elle y est posée, celle DÉTECTÉE dans son message (`core/lang_detect.py`, signal net
    seulement), la dernière reconnue sur le fil (un message court ou ambigu ne la fait pas basculer), celle de
    l'installation (`NOKKER_LANG`), puis `fr`."""
    return (norm_lang(explicit) or norm_lang(account) or norm_lang(detected) or norm_lang(thread)
            or norm_lang(installation) or DEFAULT_LANG)


__all__ = ["LABELS", "LANGS", "SUFFIXES", "current_lang", "installation_lang", "lang_scope", "norm_lang", "request_lang", "resolve_lang",
           "rule_label", "rule_labels", "rule_source", "set_request_lang"]
