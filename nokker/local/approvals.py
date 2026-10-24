"""Décisions d'approbation locales de Nokker Lokkal (docs/LOKKAL.md §7.4.3).

Sur la machine, le **jeton local** prouve le propriétaire (seul lui le lit) : la CLI décide une carte sans signature
d'appareil. Ce n'est vrai qu'en local : une carte décidée à distance (passerelle, §7.3) exige toujours la clé
d'appareil.

Portées : `once` (cette carte) ; `session` (règle `allow` en mémoire pour CE fil, jusqu'au redémarrage) ; `always`
(règle écrite dans les réglages de l'UTILISATEUR, jamais dans le `.nokker/` du dépôt). Les règles proposées sont
calculées ICI à partir de l'appel (`suggested_rules`) : le client choisit parmi elles, il n'en écrit pas. Une carte
`strong` (destructif) n'a que `once`.
"""
from __future__ import annotations

import json
import os
import posixpath
from pathlib import Path
from typing import Any

FILE_TOOLS = ("write_file", "patch", "read_file", "search_files")


def suggested_rules(tool: str, arguments: dict[str, Any], strength: str | None) -> list[str]:
    if strength == "strong":
        return []
    if tool == "terminal":
        cmd = " ".join(str(arguments.get("command", "")).split())
        if not cmd:
            return []
        words = cmd.split(" ")
        out = [f"terminal({cmd})"]
        if len(words) > 2:
            out.append(f"terminal({' '.join(words[:2])}*)")
        return out
    if tool in FILE_TOOLS and arguments.get("path"):
        path = posixpath.normpath(str(arguments["path"]))
        parent = posixpath.dirname(path)
        out = [f"{tool}({path})"]
        if parent and parent not in (".", "/"):
            out.append(f"{tool}({parent}/**)")
        return out
    return [tool]


def append_user_rule(settings_path: str | Path, rule: str) -> bool:
    """Ajoute `rule` aux `allow` des réglages de l'utilisateur (dédoublonné à l'identique). True si écrite."""
    p = Path(settings_path).expanduser()
    data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    perms = data.setdefault("permissions", {})
    allow = perms.setdefault("allow", [])
    if rule in allow:
        return False
    allow.append(rule)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)
    return True


__all__ = ["append_user_rule", "suggested_rules"]
