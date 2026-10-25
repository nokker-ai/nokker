"""Ce que tu signes (lot X20c, docs/LOKKAL.md §7.3 et §7.12) : le résumé d'une carte forte de connecteur relayé, rendu
PAR nokker.ai à partir des arguments que la machine a déposés avec la carte, et que nokker.ai a d'abord confrontés à
l'empreinte du défi (`args_sha256`). Le titre et le motif de la machine ne sont plus ce qu'on signe : un voleur du jeton
de machine peut écrire « lire mes mails » en titre, le résumé, lui, dit « envoyer un e-mail à x@… ».

    summarize(tool, arguments) → {tool, class, headline, lines: [{key, label, value, items?, length?, truncated?,
                                                                  redacted?}], erased: false}

Règles d'affichage (la page de décision les suit, docs/LOKKAL.md §7.12 point 8) :
  - aucun argument n'est caché : les champs connus de l'outil d'abord, dans un ordre fixe, puis tous les autres sous
    leur nom brut ; au-delà de `MAX_LINES`, une dernière ligne dit combien il en reste ;
  - les destinataires et les invités sont TOUS listés (`items`, jusqu'à `MAX_ITEMS`, puis « + N ») ;
  - le corps d'un mail et la description d'un événement sont un extrait (`MAX_EXCERPT` car.), avec leur longueur ;
  - caviardage (`nokker/qa/secrets.py::redact`) AVANT la coupe, pour qu'un secret coupé en deux ne passe pas : texte
    humain (objet, corps, description, titre, lieu) avec les replis « mot de passe : » et entropie ; adresses et
    identifiants avec les seuls motifs précis (une adresse à l'air aléatoire reste lisible : c'est elle qu'il faut
    voir) ; `redacted: true` sur une ligne caviardée ;
  - caractères de contrôle, invisibles et de direction (U+202E…) retirés, blancs ramenés à une espace : une valeur ne
    peut pas se faire passer pour une autre ligne ni se retourner à l'écran.
"""
from __future__ import annotations

import json
import re
from typing import Any

from nokker.qa.secrets import KnownSecrets, redact

MAX_VALUE = 200  # une valeur courte (objet, adresse, date, identifiant)
MAX_EXCERPT = 280  # extrait du corps d'un mail ou de la description d'un événement
MAX_ITEMS = 20  # destinataires, invités montrés un par un
MAX_LINES = 24
# Contrôles (sauf tab et sauts de ligne, ramenés à une espace plus bas), largeur nulle, marques et forçages de
# direction, BOM : jamais affichés.
_INVISIBLE = re.compile("[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e"
                        "\u2060-\u2069\ufeff]")
_SPLIT_ADDR = re.compile(r"[,;]")

# Libellés des champs connus (clé stable `key` pour la traduction côté page).
LABELS = {
    "to": "À", "cc": "Cc", "bcc": "Cci", "subject": "Objet", "body": "Message", "in_reply_to": "En réponse au message",
    "message_id": "Message", "destination": "Vers", "reason": "Motif donné par l'assistant",
    "event_id": "Événement", "summary": "Titre", "start": "Début", "end": "Fin", "location": "Lieu",
    "attendees": "Invités", "description": "Description", "calendar": "Agenda",
}
HUMAN = frozenset({"subject", "body", "summary", "description", "location", "reason"})  # caviardage complet
EXCERPT = frozenset({"body", "description"})
ADDRESSES = frozenset({"to", "cc", "bcc", "attendees"})
# Ordre des champs par outil ; un outil absent est rendu champ par champ, sous les noms bruts.
FIELDS: dict[str, tuple[str, ...]] = {
    "mail_send": ("to", "cc", "bcc", "subject", "body", "in_reply_to"),
    "mail_draft": ("to", "cc", "bcc", "subject", "body", "in_reply_to"),
    "mail_trash": ("message_id", "reason"),
    "mail_move": ("message_id", "destination", "reason"),
    "calendar_create": ("summary", "start", "end", "location", "attendees", "description", "calendar"),
    "calendar_update": ("event_id", "summary", "start", "end", "location", "attendees", "description", "calendar"),
    "calendar_delete": ("event_id", "calendar"),
}


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return ", ".join(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _clean(text: str, known: KnownSecrets | None, *, weak: bool, limit: int) -> tuple[str, int, bool]:
    """(texte affichable, longueur avant la coupe, caviardé ?). Caviardage d'abord, puis nettoyage, puis la coupe."""
    out = redact(text, known, weak=weak)
    shown = " ".join(_INVISIBLE.sub("", out.text).split())
    length = len(shown)
    if length > limit:
        shown = shown[:limit - 1].rstrip() + "…"
    return shown, length, bool(out.names)


def _addresses(value: Any) -> list[str]:
    raw = value if isinstance(value, list) else _SPLIT_ADDR.split(value) if isinstance(value, str) else [value]
    return [a for a in (_as_text(x).strip() for x in raw) if a]


def _line(key: str, value: Any, known: KnownSecrets | None, *, label: str | None = None) -> dict[str, Any]:
    line: dict[str, Any] = {"key": key, "label": label or LABELS.get(key, key)}
    if key in ADDRESSES:
        items, redacted = [], False
        for a in _addresses(value):
            shown, _, red = _clean(a, known, weak=False, limit=MAX_VALUE)
            items.append(shown)
            redacted = redacted or red
        line["items"] = items[:MAX_ITEMS] + ([f"+ {len(items) - MAX_ITEMS}"] if len(items) > MAX_ITEMS else [])
        line["value"] = ", ".join(line["items"])
        line["count"] = len(items)
    else:
        limit = MAX_EXCERPT if key in EXCERPT else MAX_VALUE
        shown, length, redacted = _clean(_as_text(value), known, weak=key in HUMAN or key not in LABELS, limit=limit)
        line["value"] = shown
        if length > limit:
            line["truncated"], line["length"] = True, length
    if redacted:
        line["redacted"] = True
    return line


def _headline(tool: str, arguments: dict[str, Any]) -> str:
    n_to = sum(len(_addresses(arguments.get(k))) for k in ("to", "cc", "bcc") if arguments.get(k))
    n_inv = len(_addresses(arguments["attendees"])) if arguments.get("attendees") else 0
    if tool == "mail_send":
        return f"Envoyer un e-mail en ton nom à {_plural(n_to, 'destinataire', 'destinataires')}"
    if tool == "mail_draft":
        return "Créer un brouillon d'e-mail (rien n'est envoyé)"
    if tool == "mail_trash":
        return "Mettre un message à la corbeille"
    if tool == "mail_move":
        from nokker.tools.effects import is_trash_destination

        if is_trash_destination(arguments.get("destination")):
            return "Mettre un message à la corbeille"
        return "Déplacer un message de ta boîte"
    if tool in ("calendar_create", "calendar_update"):
        verb = "Créer" if tool == "calendar_create" else "Modifier"
        return (f"{verb} un événement avec {_plural(n_inv, 'invité', 'invités')}" if n_inv
                else f"{verb} un événement dans ton agenda")
    if tool == "calendar_delete":
        return "Supprimer un événement de ton agenda"
    return f"Outil « {tool[:60]} » (non classé chez nokker.ai : traité comme un envoi externe)"


def summarize(tool: str, arguments: dict[str, Any], known: KnownSecrets | None = None) -> dict[str, Any]:
    """Résumé de l'appel `tool(arguments)` tel que nokker.ai le montre avant la signature. `arguments` sont ceux dont
    l'empreinte a été vérifiée contre le défi : ce sont exactement ceux qui partiront à l'appel."""
    from nokker.cloud.relay import call_class

    order = FIELDS.get(tool, ())
    keys = [k for k in order if k in arguments and arguments[k] not in (None, "", [])]
    keys += sorted(k for k in arguments if k not in order and arguments[k] not in (None, "", []))
    lines = [_line(k, arguments[k], known) for k in keys[:MAX_LINES]]
    if len(keys) > MAX_LINES:
        rest = len(keys) - MAX_LINES
        lines.append({"key": "_more", "label": "Autres arguments", "value": _plural(rest, "autre", "autres")})
    cls = call_class(tool, arguments)
    return {"tool": tool, "class": cls.value, "headline": _headline(tool, arguments), "lines": lines, "erased": False}


def machine_summary(tool: str | None) -> dict[str, Any]:
    """Carte LOCALE (arguments restés sur la machine, §6.7) : nokker.ai n'a rien à rendre ; la page montre le titre et
    le motif de la machine, marqués « décrit par ta machine », et l'empreinte des arguments."""
    return {"tool": tool, "class": None, "headline": None, "lines": [], "erased": False}


def erased(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    """Après la décision ou l'expiration : on garde l'outil, la classe et la phrase de tête (aucune donnée de
    l'appel), les lignes sont effacées avec les arguments."""
    if not summary or summary.get("erased") or summary.get("class") is None:  # carte locale : rien à effacer
        return summary
    return {**summary, "lines": [], "erased": True}


__all__ = ["FIELDS", "LABELS", "MAX_EXCERPT", "erased", "machine_summary", "summarize"]
