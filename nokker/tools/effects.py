"""nokker/tools/effects.py — table d'effets par défaut (PLAN-P1 §2.3 étape 5) + enveloppe secrète.

Un outil SANS effet déclaré est traité comme `Effect.WRITE` (défaut prudent — déjà la valeur
par défaut de `ToolSpec.effect` dans `nokker/core/ports.py`).

Ce module fournit :
  1. `DEFAULT_EFFECTS` : table par défaut pour les outils connus du banc (`openclaw-eval/tasks.py`)
     et du canari (calendar_*, mail_*, send_*, infra_*, kanban_*, memory_*, browser_*…) ;
  2. `EffectsTable` : la table ci-dessus + surcharge par config (name → Effect) ;
  3. `redact_tool_result` / `SecretScrubbingToolSource` : la transformation « SECRET → poignée »
     promise par `qa/invariants.py` (effet SECRET = ALLOW, mais le résultat ne doit JAMAIS
     entrer en clair dans le contexte/le store).
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.core.ports import ToolContext, ToolSource, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.qa.secrets import KnownSecrets, redact

# ─────────────────────────────── table par défaut ───────────────────────────────
#
# Dérivée d'un banc interne (ids des outils stubés) et du canari. Un nom absent de cette table → WRITE (défaut prudent).
# Les entrées `send_email_reply`, `send_email_forward` et `send_telegram_message` ne portent aucun outil local : ce sont des
# noms de banc et des noms que les modèles produisent. Elles restent comme FILET fail-closed : un outil MCP ou de skill
# homonyme garde l'effet SEND_EXTERNAL (allowlist, liste fermée du relecteur) au lieu de retomber sur WRITE. Ne pas les retirer.

DEFAULT_EFFECTS: dict[str, Effect] = {
    # calendar_* (nina-calendar / workspace MCP)
    "calendar_list": Effect.READ,
    "calendar_events": Effect.READ,
    "calendar_create": Effect.WRITE,
    "calendar_update": Effect.WRITE,
    "calendar_delete": Effect.DESTRUCTIVE,
    # mail_*
    "mail_search": Effect.READ,
    "mail_read": Effect.READ,
    "mail_draft": Effect.WRITE,
    # docs/TOOLS-MAIL.md (18.10.2026) : classer / déplacer = réversible (WRITE, sans GO) ; corbeille = DESTRUCTIVE
    # (carte d'approbation forte), même si Gmail la garde 30 jours.
    "mail_move": Effect.WRITE,
    "mail_trash": Effect.DESTRUCTIVE,
    "mail_send": Effect.SEND_EXTERNAL,
    # send_* (gateway / adaptateurs de canal)
    "send_email_reply": Effect.SEND_EXTERNAL,
    "send_email_forward": Effect.SEND_EXTERNAL,  # transfert interne : l'allowlist doit inclure le destinataire
    "send_bulk_email": Effect.SEND_BULK,
    "send_message": Effect.SEND_EXTERNAL,
    "send_telegram_message": Effect.SEND_EXTERNAL,
    # infra_* (actions à effet sur l'hôte — invariant 4 du préfixe)
    "infra_disk_usage": Effect.READ,
    "infra_gpu_info": Effect.READ,  # inventaire matériel réel (clinfo -l, nvidia-smi -L) : lecture, avant tout plan GPU
    "infra_lvextend": Effect.DESTRUCTIVE,
    "infra_lvreduce": Effect.DESTRUCTIVE,
    "infra_restart": Effect.DESTRUCTIVE,
    "infra_deploy": Effect.DESTRUCTIVE,
    "infra_dns_update": Effect.DESTRUCTIVE,
    "infra_secret_rotate": Effect.DESTRUCTIVE,
    "infra_gpu_load_test": Effect.DESTRUCTIVE,  # charge GPU sans GO = interdit (invariant 4)
    # kanban_*
    "kanban_create_task": Effect.WRITE,
    "kanban_update_task": Effect.WRITE,
    "kanban_delete_task": Effect.DESTRUCTIVE,
    # memory_* (sokkan-memory / CortHeXis)
    "memory_search": Effect.READ,
    "memory_get": Effect.READ,
    "memory_write": Effect.WRITE,
    # browser_* (navigateur partagé)
    "browser_navigate": Effect.READ,
    # Lecture seule (21.10.2026) : sans entrée ici, le défaut prudent WRITE les privait du dédoublonnage des lectures.
    "browser_snapshot": Effect.READ,
    "browser_read_text": Effect.READ,
    "browser_find": Effect.READ,
    "browser_take_screenshot": Effect.READ,
    "browser_console_messages": Effect.READ,
    "browser_network_requests": Effect.READ,
    "browser_status": Effect.READ,
    # V3 « Agent qui montre » : désigne un élément sur l'image en direct, aucun geste (nokker/browserlive/point.py)
    "browser_point": Effect.READ,
    "browser_click": Effect.WRITE,
    "browser_fill": Effect.WRITE,
    "browser_submit": Effect.SEND_EXTERNAL,
    # Carnet d'incidents (24.10, recommandations retenues) : du code arbitraire dans un Chrome connecté (C2), un fichier
    # local envoyé à un site (C3) ; `browser_handle_dialog` : par argument, ci-dessous (F1).
    "browser_evaluate": Effect.DESTRUCTIVE,
    "browser_file_upload": Effect.SEND_EXTERNAL,
    # outils qui renvoient une valeur de secret en clair (banc `credentiels-pas-de-fuite`)
    "grafana_get_credentials": Effect.SECRET,
    "vaultwarden_get": Effect.SECRET,
    # sites_* (sites connectés, docs/CONNECTED-SITES.md §8 ; lots L0/L1 : connexion et lecture seulement)
    "sites_connect": Effect.WRITE,
    "sites_list": Effect.READ,
    "sites_read": Effect.READ,
    "sites_search": Effect.READ,
    # web_* (recherche web sans clé, tools/web.py — P1, 18.10.2026)
    "web_search": Effect.READ,
    "web_fetch": Effect.READ,
    # bulles et sessions (nokker/bubbles/tools.py — P1, docs/BUBBLES.md) : périmètre borné par le hub
    # (fil courant / fils dont l'utilisateur du tour est membre)
    "bubble_update": Effect.READ,
    "session_send": Effect.READ,
    # Crew (docs/BUBBLES.md §9.4) : propose une carte, ne crée AUCUN agent par lui-même (l'activation,
    # elle, passe par la route HTTP — jamais par cet outil).
    "agent_draft": Effect.READ,
}


# Noms de la corbeille selon le fournisseur : libellé système Gmail `TRASH`, dossiers IMAP usuels (`Trash`, `Deleted
# Items` d'Outlook, `Deleted Messages` d'iCloud, `Éléments supprimés`…), quelques langues. Comparés au DERNIER morceau
# du chemin (`[Gmail]/Corbeille`, `INBOX.Trash`), sans casse ; « Corbeille à papier » ou « Trash-notes » n'en sont pas.
# Un nom absent d'ici mais que le serveur marque `\Trash` est refusé par le connecteur mail lui-même (`mail_move`).
TRASH_NAMES = frozenset({"trash", "corbeille", "bin", "deleted", "deleted items", "deleted messages",
                         "éléments supprimés", "elements supprimes", "papierkorb", "gelöschte elemente", "cestino",
                         "posta eliminata", "papelera", "elementos eliminados", "lixeira", "lixo"})


def is_trash_destination(destination: Any) -> bool:
    """`destination` de `mail_move` (`label:<nom>` ou nom nu) qui désigne la corbeille du fournisseur."""
    dest = str(destination or "").strip()
    if dest.lower().startswith("label:"):
        dest = dest[6:]
    dest = dest.strip().strip("\"'").strip()
    last = re.split(r"[/.]", dest)[-1] if dest else ""
    return " ".join(last.split()).casefold() in TRASH_NAMES


def mail_move_effect(arguments: dict) -> Effect:
    """`label:TRASH` = la corbeille par l'outil de rangement (le serveur workspace accepte tout libellé existant,
    système compris) : même porte que `mail_trash` (carnet d'incidents B2, 24.10). Même règle pour le connecteur mail
    du compte (`connectors/mail.py`) et pour sa classe chez nokker.ai quand il est relayé (`cloud/relay.py`)."""
    return Effect.DESTRUCTIVE if is_trash_destination((arguments or {}).get("destination")) else Effect.WRITE


def _calendar_effect(arguments: dict) -> Effect:
    """Avec `attendees`, le serveur ENVOIE les invitations (sendUpdates=all) : envoi externe, allowlist comprise
    (carnet d'incidents C4, 24.10). Sans invités, une écriture dans l'agenda."""
    return Effect.SEND_EXTERNAL if arguments.get("attendees") else Effect.WRITE


def _dialog_effect(arguments: dict) -> Effect:
    """Accepter un confirm()/prompt() = valider ce que la page demande (« Confirmer le paiement de 499 CHF ? ») :
    carte forte (carnet d'incidents F1, 24.10). Refuser ou fermer : sans effet."""
    return Effect.DESTRUCTIVE if arguments.get("accept") is not False else Effect.WRITE


# Effet qui dépend des ARGUMENTS d'un outil MCP (`ToolSpec.effect_of`) ; l'effet de la table reste le repli.
ARGUMENT_EFFECTS: dict[str, Callable[[dict], Effect]] = {
    "mail_move": mail_move_effect,
    "calendar_create": _calendar_effect,
    "calendar_update": _calendar_effect,
    "browser_handle_dialog": _dialog_effect,
}


# ─────────────────────────────── où va le texte d'un appel (garde `masked_value`, P9b) ───────────────────────────────
#
# L'effet dit ce qu'un appel CHANGE ; la garde des valeurs masquées sur l'appareil (`nokker/redact/guard.py`) doit aussi
# savoir OÙ son texte part. Trois compléments nommés, lus avec l'effet de CET appel :
#   - saisie dans une page ou un service tiers, même classée WRITE : les gestes navigateur du modèle qui tapent ou
#     choisissent (`payments/gates.py::BROWSER_INPUT`, même liste que les règles de paiement), la connexion d'un site
#     (`sites_connect` tape l'identifiant sur le site), et tout outil MCP d'une app tierce qui déclare WRITE (un outil
#     MCP absent de la table ci-dessus : ceux de la table sont des connecteurs du compte, classés ici) ;
#   - effet WRITE qui reste DANS le fil : `delegate` (sous-agent sur `<fil>/sa-<id>`, compte rendu rendu au fil, chacun
#     de ses appels passant par la même garde) ; tout autre WRITE persiste hors du fil (mémoire, fichiers, brouillons,
#     agenda, kanban, notes, points de reprise…) ;
#   - classés READ mais qui emportent leur texte hors du fil : `session_send` (écrit à un autre fil), `agent_draft`
#     (consignes d'un agent Crew qui vivra hors du fil), `memory_note` (note proposée en quarantaine : persiste au-delà du fil
#     dès qu'elle est validée ; effet READ voulu, la validation de la personne est la carte — nokker/memory/long_note.py).
THIRD_PARTY_SITE_INPUT = frozenset({"sites_connect"})
THREAD_SCOPED = frozenset({"delegate"})
LEAVES_THREAD = frozenset({"session_send", "agent_draft", "memory_note"})


def third_party_input(name: str, spec: Any, effect: Effect) -> bool:
    """Vrai si l'appel (effet `effect`) saisit du texte dans une page ou un service tiers (voir ci-dessus)."""
    from nokker.payments.gates import BROWSER_INPUT

    if name in BROWSER_INPUT or name in THIRD_PARTY_SITE_INPUT:
        return True
    source = str(getattr(spec, "source", "") or "")
    return effect is Effect.WRITE and source.startswith("mcp:") and name not in DEFAULT_EFFECTS


def persists_beyond_thread(name: str, effect: Effect, declared: Effect | None = None) -> bool:
    """Vrai si l'appel garde son texte au-delà du fil courant (voir ci-dessus). `effect` : effet de CET appel ;
    `declared` : effet déclaré de l'outil (un `calendar_create` avec invités est un envoi ET une écriture gardée)."""
    return (Effect.WRITE in (effect, declared) and name not in THREAD_SCOPED) or name in LEAVES_THREAD


# ─────────────────────────────── outil sans effet déclaré : ressemble-t-il à un envoi ? ───────────────────────────────
#
# Un outil MCP ou de skill absent de la table et des surcharges n'a aucun effet déclaré. Avant, tout tombait sur WRITE ;
# un « slack_post_message » ou un « send_sms » inconnu partait donc sans allowlist ni carte d'envoi. Le nom (découpé en
# mots, camelCase compris) et les noms de paramètres du schéma sont lus (la description, texte du serveur, ne l'est pas) : s'ils annoncent un envoi, l'effet est
# SEND_EXTERNAL (fermé par défaut : sans destinataire lisible dans la liste, une carte). Liste large : un faux positif
# coûte une carte, un faux négatif un envoi sans carte. Une surcharge `effect_overrides` ou une entrée de la table gagne.

_WORDS = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
_SEND_STRONG = frozenset({"send", "reply", "forward", "publish", "notify", "sms", "tweet", "broadcast", "dispatch",
                          "transmit"})
_SEND_WEAK = frozenset({"mail", "email", "message", "msg", "post", "dm", "outbox"})
_READ_FIRST = frozenset({"get", "list", "read", "search", "find", "fetch", "query", "count", "describe", "show",
                         "view", "check", "status", "lookup", "browse", "inspect", "peek", "preview"})
_RECIPIENT_PROPS = frozenset({"recipient", "recipients", "cc", "bcc", "reply_to", "to_email", "to_emails", "to_address",
                              "destinataire", "destinataires", "chat_id", "phone", "phone_number", "channel_id"})
_BODY_PROPS = frozenset({"body", "text", "message", "content", "subject", "html"})


def _words(text: str) -> list[str]:
    out: list[str] = []
    for w in _WORDS.findall(str(text or "")):
        w = w.lower()
        out.append(w[:-1] if w.endswith("s") and len(w) > 3 else w)
    return out


def looks_like_send(name: str, description: str = "", parameters: dict[str, Any] | None = None) -> bool:
    """Vrai si le nom ou les paramètres d'un outil sans effet déclaré annoncent un envoi vers l'extérieur."""
    words = _words(name)
    read_first = bool(words) and words[0] in _READ_FIRST
    if _SEND_STRONG & set(words):
        return True
    if read_first:
        return False
    if _SEND_WEAK & set(words):
        return True
    props = (parameters or {}).get("properties") if isinstance(parameters, dict) else None
    keys = {str(k).lower() for k in props} if isinstance(props, dict) else set()
    if keys & _RECIPIENT_PROPS or ("to" in keys and keys & _BODY_PROPS):
        return True
    return False


@dataclass
class EffectsTable:
    """Table par défaut + surcharge (config). Un outil absent des deux → WRITE."""

    overrides: dict[str, Effect] = field(default_factory=dict)

    @classmethod
    def load(cls, overrides: dict[str, str | Effect] | None = None) -> "EffectsTable":
        resolved: dict[str, Effect] = {}
        for name, value in (overrides or {}).items():
            resolved[name] = value if isinstance(value, Effect) else Effect(value)
        return cls(overrides=resolved)

    def effect_for(self, name: str) -> Effect:
        if name in self.overrides:
            return self.overrides[name]
        return DEFAULT_EFFECTS.get(name, Effect.WRITE)

    def annotate(self, spec: ToolSpec) -> ToolSpec:
        """Renvoie `spec` avec son effet résolu par cette table (surcharge > défaut > WRITE).
        À utiliser par le registre réel (F2 `tools/registry.py`) quand une source d'outils (ex.
        un serveur MCP) ne déclare pas d'effet lui-même."""
        import dataclasses

        effect = self.effect_for(spec.name)
        classify = ARGUMENT_EFFECTS.get(spec.name) if spec.name not in self.overrides else None
        if classify is None:
            if spec.name in self.overrides or spec.name in DEFAULT_EFFECTS:
                return dataclasses.replace(spec, effect=effect, effect_inferred=None)
            # Aucun effet déclaré : un envoi probable est fermé par défaut, le reste reste WRITE (carte au 1er usage).
            if looks_like_send(spec.name, spec.description, spec.parameters):
                return dataclasses.replace(spec, effect=Effect.SEND_EXTERNAL, effect_inferred="send")
            return dataclasses.replace(spec, effect=effect, effect_inferred="unknown")
        return dataclasses.replace(spec, effect=effect, effect_of=classify, effect_inferred=None)


# ─────────────────────────────── transformation SECRET → poignée ───────────────────────────────


def redact_tool_result(result: ToolResult, known: KnownSecrets | None = None) -> ToolResult:
    """Caviarde le contenu d'un `ToolResult` (valeurs connues + motifs, cf. `qa/secrets.py`).

    **Depuis `main` e076d54, le chemin de garantie principal est
    `qa/invariants.py::DeterministicInvariants.redact_result`** — appelé par
    `core/loop.py::_tool_step` sur CHAQUE résultat d'outil, avant le contexte et le store. Cette
    fonction reste une seconde ligne de défense indépendante (ex. un `ToolSource` qui voudrait
    se protéger lui-même avant même d'atteindre le noyau, ou un test qui exerce un `ToolSource`
    isolément) — redondante avec le noyau en usage normal, pas un point de passage obligatoire.
    """
    red = redact(result.content, known)
    merged_handles = list(dict.fromkeys([*result.secret_handles, *red.names]))
    return ToolResult(call_id=result.call_id, name=result.name, content=red.text, is_error=result.is_error,
                      reservoir_id=result.reservoir_id, secret_handles=merged_handles)


@dataclass
class SecretScrubbingToolSource:
    """Enveloppe un `ToolSource` : tout résultat d'un outil d'effet `SECRET` est transformé en
    poignée avant d'être renvoyé par `.call()`.

    **Défense en profondeur, pas le mécanisme principal** : depuis `main` e076d54, le noyau
    (`core/loop.py::_tool_step`) appelle déjà `Invariants.redact_result` sur CHAQUE résultat
    d'outil (effet `SECRET` ou non), avant le contexte et le store — la garantie ne dépend donc
    plus de cette enveloppe. Elle reste utile pour un `ToolSource` utilisé HORS du `Kernel` (ex.
    un appel direct dans un test, un outil exposé par un autre chemin), ou comme second verrou
    si un `ToolSpec` est mal étiqueté ET que `redact_result` ne connaît pas la valeur (coffre
    vide pour ce secret précis) — dans ce cas précis, aucune des deux couches n'attrape la fuite,
    cf. docs/INVARIANTS.md §2.
    """

    inner: ToolSource
    known: KnownSecrets = field(default_factory=KnownSecrets)
    effects: EffectsTable = field(default_factory=EffectsTable)

    def specs(self) -> list[ToolSpec]:
        return [self.effects.annotate(s) for s in self.inner.specs()]

    def turn_done(self, thread_id: str) -> None:
        hook = getattr(self.inner, "turn_done", None)
        if callable(hook):
            hook(thread_id)

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        result = self.inner.call(name, arguments, ctx)
        if self.effects.effect_for(name) is Effect.SECRET:
            return redact_tool_result(result, self.known)
        return result


__all__ = ["ARGUMENT_EFFECTS", "DEFAULT_EFFECTS", "LEAVES_THREAD", "THIRD_PARTY_SITE_INPUT", "THREAD_SCOPED",
           "EffectsTable", "SecretScrubbingToolSource", "looks_like_send", "persists_beyond_thread", "redact_tool_result",
           "third_party_input"]
