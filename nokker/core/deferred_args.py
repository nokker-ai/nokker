"""Appel DIRECT d'un outil différé dont le schéma n'était pas chargé (`core/loop.py::_tool_step`).

Le modèle appelle parfois un outil non cœur par son nom sans en avoir le schéma (l'index ne porte que les noms) et INVENTE les
arguments (cas réel : `maison {"entity_id": …, "attribut": "state"}` → « maison : demande vide », sans dire ce qui est attendu).
Quand le schéma n'était PAS chargé et qu'un champ `required` manque ou est vide, le noyau rend une erreur qui cite les champs
attendus (nom, type, description courte) et charge le schéma pour la suite du tour. Aucun effet : ni porte, ni verdict, ni exécution.
Requis présents (même avec des clés inconnues) : comportement inchangé ; schéma déjà chargé : inchangé (l'erreur est celle de la source).

TEXTES VUS PAR LE MODÈLE (à relire et geler) : `TEMPLATE`, `FIELD`, `FIELD_DESC`, `FIELD_ENUM`, `SEPARATOR`."""
from __future__ import annotations

from typing import Any

from nokker.core import model_texts as mt

TEMPLATE = mt.DEFERRED_ARGS["fr"]  # figés ; autres langues : `model_texts` (langue du tour)
FIELD = "{name} ({type})"
FIELD_DESC = mt.DEFERRED_FIELD_DESC["fr"]
FIELD_ENUM = mt.DEFERRED_FIELD_ENUM["fr"]
SEPARATOR = " ; "
DESC_MAX = 160


def _empty(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, (list, dict)) and not v)


def missing_required(parameters: dict[str, Any] | None, arguments: dict[str, Any] | None) -> list[str]:
    """Champs `required` du schéma absents ou vides dans `arguments` (dans l'ordre du schéma)."""
    required = (parameters or {}).get("required") or []
    args = arguments if isinstance(arguments, dict) else {}
    return [str(n) for n in required if _empty(args.get(n))]


def _short(text: str) -> str:
    t = " ".join(str(text).split())
    return t if len(t) <= DESC_MAX else t[: DESC_MAX - 1].rstrip() + "…"


def deferred_args_error(tool: str, parameters: dict[str, Any] | None, arguments: dict[str, Any] | None) -> str | None:
    """Le texte d'erreur (champs REQUIS manquants ou vides cités avec leur type et leur description), ou None si rien ne manque."""
    if not missing_required(parameters, arguments):
        return None
    props = (parameters or {}).get("properties") or {}
    fields = []
    for name in (parameters or {}).get("required") or []:
        p = props.get(name) if isinstance(props.get(name), dict) else {}
        kind = p.get("type")
        kind = "|".join(map(str, kind)) if isinstance(kind, list) else str(kind or mt.pick(mt.DEFERRED_KIND))
        item = FIELD.format(name=name, type=kind)
        if p.get("description"):
            item = mt.pick(mt.DEFERRED_FIELD_DESC).format(field=item, description=_short(p["description"]))
        if isinstance(p.get("enum"), list) and p["enum"]:
            item = mt.pick(mt.DEFERRED_FIELD_ENUM).format(field=item, values=" | ".join(map(str, p["enum"][:8])))
        fields.append(item)
    return mt.pick(mt.DEFERRED_ARGS).format(tool=tool, fields=SEPARATOR.join(fields))


__all__ = ["DESC_MAX", "FIELD", "FIELD_DESC", "FIELD_ENUM", "SEPARATOR", "TEMPLATE", "deferred_args_error", "missing_required"]
