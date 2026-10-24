"""Schémas d'outils compactés à la source, sans perte pour le modèle (plan breakthrough du 21.10.2026, chantier B).

Retiré : `$schema`, `title` (doublon du nom de la propriété), `additionalProperties: false` (le modèle n'invente pas de
champ pour autant, le serveur refuse toujours), `default: null` ; `anyOf: [T, {type: null}]` (champ facultatif, forme
pydantic) devient T — facultatif se lit déjà dans `required`. Mesuré sur les 37 outils MCP du canari : ≈ 14 % des
caractères des paramètres.
"""
from __future__ import annotations

from typing import Any


def compact_schema(node: Any) -> Any:
    if isinstance(node, list):
        return [compact_schema(x) for x in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in ("$schema", "title"):
            continue
        if key == "additionalProperties" and value is False:
            continue
        if key == "default" and value is None:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: compact_schema(sub) for name, sub in value.items()}  # « title » peut être un nom de champ
            continue
        out[key] = compact_schema(value)
    variants = out.get("anyOf")
    if isinstance(variants, list):
        kept = [v for v in variants if not (isinstance(v, dict) and v.get("type") == "null")]
        if len(kept) == 1 and isinstance(kept[0], dict) and len(kept) < len(variants):
            merged = {k: v for k, v in out.items() if k != "anyOf"}
            merged.update({k: v for k, v in kept[0].items() if k not in merged})
            return merged
    return out


__all__ = ["compact_schema"]
