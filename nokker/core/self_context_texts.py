"""Textes du bloc `[Environnement]` (`nokker/core/self_context.py`), vus du MODÈLE, en fr/en/de/it : une table par texte,
une entrée par langue (même mécanisme que `person_texts.py`). Les phrases françaises sont figées mot pour mot (empreintes
testées dans `tests/unit/core/test_self_context.py`). La langue est celle du tour, sinon celle du contexte, sinon `fr`.

Le bloc dit où tourne le fil (cloud, Lokkal, Lokkal lié depuis le cloud), par quel canal l'utilisateur écrit, et quelles
machines liées existent : jamais d'identifiant opaque, jamais une information inventée (une ligne inconnue est omise).
"""
from __future__ import annotations

from nokker.approvals.rule_labels import _l, current_lang, norm_lang


def pick(table: dict[str, str], lang: str | None = None) -> str:
    """Texte de `table` dans `lang` (sinon la langue du contexte, sinon `fr`)."""
    return table[norm_lang(lang) or current_lang()]


# Balises du bloc : fixes dans les quatre langues (le préfixe et la description de l'outil de session les citent).
OPEN = "[Environnement]"
CLOSE = "[/Environnement]"

# Canal : « Canal : <surface>. » ; la surface vient de `SURFACES`, jamais d'un texte libre.
CHANNEL = _l("Canal : {surface}.", "Channel: {surface}.", "Kanal: {surface}.", "Canale: {surface}.")
SURFACES: dict[str, dict[str, str]] = {
    "telegram": _l("Telegram", "Telegram", "Telegram", "Telegram"),
    "slack": _l("Slack", "Slack", "Slack", "Slack"),
    "whatsapp": _l("WhatsApp", "WhatsApp", "WhatsApp", "WhatsApp"),
    "voice": _l("conversation vocale", "voice conversation", "Sprachgespräch", "conversazione vocale"),
    "cli": _l("terminal (ligne de commande Nokker)", "terminal (Nokker command line)",
              "Terminal (Nokker-Kommandozeile)", "terminale (riga di comando di Nokker)"),
    "acp": _l("éditeur compatible ACP", "ACP-compatible editor", "ACP-kompatibler Editor", "editor compatibile ACP"),
    "vscode": _l("extension VS Code", "VS Code extension", "VS-Code-Erweiterung", "estensione VS Code"),
    "web": _l("application web", "web app", "Web-App", "app web"),
    "pwa": _l("application mobile (PWA)", "mobile app (PWA)", "mobile App (PWA)", "app mobile (PWA)"),
    "relay": _l("application Nokker (web ou mobile), par la passerelle nokker.app",
                "Nokker app (web or mobile), through the nokker.app gateway",
                "Nokker-App (Web oder mobil), über das Gateway nokker.app",
                "app Nokker (web o mobile), tramite il gateway nokker.app"),
}

# Valeurs admises de l'en-tête `X-Nokker-Surface` (liste fermée ; chacune est une clé de `SURFACES`).
HEADER_SURFACES = ("cli", "acp", "vscode", "web", "pwa")

# Endroit d'exécution : « Exécution : … ».
PLACE_CLOUD = _l("Exécution : cloud Nokker (nokker.ai), pas sur la machine de l'utilisateur.",
                 "Running on: Nokker cloud (nokker.ai), not on the user's machine.",
                 "Ausführung: Nokker-Cloud (nokker.ai), nicht auf dem Rechner der Person.",
                 "Esecuzione: cloud Nokker (nokker.ai), non sulla macchina dell'utente.")
PLACE_LOCAL = _l("Exécution : Lokkal sur {name}, la machine de l'utilisateur.",
                 "Running on: Lokkal on {name}, the user's machine.",
                 "Ausführung: Lokkal auf {name}, dem Rechner der Person.",
                 "Esecuzione: Lokkal su {name}, la macchina dell'utente.")
PLACE_LOCAL_BARE = _l("Exécution : Lokkal, sur la machine de l'utilisateur.",
                      "Running on: Lokkal, on the user's machine.",
                      "Ausführung: Lokkal, auf dem Rechner der Person.",
                      "Esecuzione: Lokkal, sulla macchina dell'utente.")
PLACE_LINKED = _l("Exécution : Lokkal lié depuis le cloud, sur {name} (atteint par nokker.ai).",
                  "Running on: Lokkal linked from the cloud, on {name} (reached through nokker.ai).",
                  "Ausführung: Lokkal, aus der Cloud verknüpft, auf {name} (über nokker.ai erreicht).",
                  "Esecuzione: Lokkal collegato dal cloud, su {name} (raggiunto tramite nokker.ai).")
PLACE_LINKED_BARE = _l("Exécution : Lokkal lié depuis le cloud, sur une machine liée (atteinte par nokker.ai).",
                       "Running on: Lokkal linked from the cloud, on a linked machine (reached through nokker.ai).",
                       "Ausführung: Lokkal, aus der Cloud verknüpft, auf einem verknüpften Rechner (über nokker.ai erreicht).",
                       "Esecuzione: Lokkal collegato dal cloud, su una macchina dell'account (raggiunta tramite nokker.ai).")

# Machines liées au compte (cloud seulement) : noms lisibles ; une machine sans nom lisible est « une machine liée ».
MACHINES_ONE = _l("Machine liée au compte : {names}.", "Machine linked to the account: {names}.",
                  "Mit dem Konto verknüpfter Rechner: {names}.", "Macchina collegata all'account: {names}.")
MACHINES_MANY = _l("Machines liées au compte : {names}.", "Machines linked to the account: {names}.",
                   "Mit dem Konto verknüpfte Rechner: {names}.", "Macchine collegate all'account: {names}.")
UNNAMED_ONE = _l("une machine liée", "a linked machine", "ein verknüpfter Rechner", "una macchina collegata")
UNNAMED_MANY = _l("{n} machines liées", "{n} linked machines", "{n} verknüpfte Rechner", "{n} macchine collegate")
MORE = _l("{k} autres", "{k} more", "{k} weitere", "{k} altre")
QUOTED = _l("«{name}»", "\"{name}\"", "«{name}»", "«{name}»")
