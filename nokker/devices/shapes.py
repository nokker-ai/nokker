"""Formes exactes des réponses d'un appareil (P8, contrat app ↔ noyau), normalisées par LISTE BLANCHE : toute autre
clé est retirée (l'appareil ne peut pas se faire passer pour une autre source ni ajouter un champ que le noyau ne
connaît pas). 50 éléments au plus ; `notes` 300 caractères au plus ; textes sur une longueur bornée.

- `calendar.list` → événement `{title, start, end, all_day, location?, calendar, notes≤300}` ;
- `contacts.search` → contact `{name, emails[], phones[], org?}` ;
- `reminders.list` → rappel `{title, due?, done, list, notes≤300}`.

Le contenu reste une DONNÉE non fiable : il n'est jamais montré au modèle hors de la clôture de la famille.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

MAX_ITEMS = 50
NOTES_MAX = 300
TEXT_MAX = 200
OPS = ("calendar.list", "contacts.search", "reminders.list")
CAP_OF = {"calendar.list": "calendar.read", "contacts.search": "contacts.read", "reminders.list": "reminders.read"}


class ShapeError(ValueError):
    """Réponse hors forme : traitée comme `too_large` ou refusée (400)."""


def _text(v: Any, n: int = TEXT_MAX) -> str:
    s = "".join(c if c.isprintable() or c in "\n\t" else " " for c in str(v if v is not None else ""))
    return s.strip()[:n]


def _when(v: Any) -> str:
    """Date ou date-heure ISO 8601 ; rendue telle quelle si elle se relit, sinon ''."""
    s = _text(v, 40)
    if not s:
        return ""
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return s


def _strings(v: Any, n: int, each: int = 120) -> list[str]:
    if not isinstance(v, list):
        return []
    return [t for t in (_text(x, each) for x in v[:n]) if t]


def event(e: dict[str, Any]) -> dict[str, Any]:
    out = {"title": _text(e.get("title")), "start": _when(e.get("start")), "end": _when(e.get("end")),
           "all_day": e.get("all_day") is True, "calendar": _text(e.get("calendar"), 80),
           "notes": _text(e.get("notes"), NOTES_MAX)}
    if e.get("location"):
        out["location"] = _text(e.get("location"))
    return out


def contact(c: dict[str, Any]) -> dict[str, Any]:
    out = {"name": _text(c.get("name"), 120), "emails": _strings(c.get("emails"), 5),
           "phones": _strings(c.get("phones"), 5, 40)}
    if c.get("org"):
        out["org"] = _text(c.get("org"), 120)
    return out


def reminder(r: dict[str, Any]) -> dict[str, Any]:
    out = {"title": _text(r.get("title")), "done": r.get("done") is True, "list": _text(r.get("list"), 80),
           "notes": _text(r.get("notes"), NOTES_MAX)}
    if r.get("due"):
        out["due"] = _when(r.get("due"))
    return out


_BY_OP = {"calendar.list": event, "contacts.search": contact, "reminders.list": reminder}


def clean_items(op: str, items: Any) -> list[dict[str, Any]]:
    """Éléments d'une réponse → formes du contrat. Plus de 50 : `ShapeError` (traité comme `too_large`)."""
    if not isinstance(items, list):
        raise ShapeError("items : liste attendue")
    if len(items) > MAX_ITEMS:
        raise ShapeError("trop d'éléments")
    norm = _BY_OP[op]
    return [norm(x) for x in items if isinstance(x, dict)]


def clean_args(op: str, args: dict[str, Any]) -> dict[str, Any]:
    """Arguments d'une demande au téléphone, bornés : agenda sur 31 jours au plus, contacts `limit` ≤ 10."""
    if op == "calendar.list":
        return {"from": str(args["from"]), "to": str(args["to"]), **({"q": _text(args["q"], 80)} if args.get("q") else {})}
    if op == "contacts.search":
        return {"q": _text(args.get("q"), 80), "limit": max(1, min(10, int(args.get("limit") or 10)))}
    if op == "reminders.list":
        return {"include_done": bool(args.get("include_done", False)),
                **({"due_before": str(args["due_before"])} if args.get("due_before") else {})}
    raise ShapeError(f"opération inconnue : {op}")


__all__ = ["CAP_OF", "MAX_ITEMS", "NOTES_MAX", "OPS", "ShapeError", "clean_args", "clean_items", "contact", "event",
           "reminder"]
