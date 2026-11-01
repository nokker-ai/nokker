"""Import depuis Cursor : règles du dépôt (`.cursorrules`, `.cursor/rules/*.mdc`) et serveurs MCP (`.cursor/mcp.json`
du dépôt, `~/.cursor/mcp.json`). Les règles sont réunies dans un `NOKKER.md` à la racine du dépôt, que Nokker lit
déjà comme mémoire de projet (nokker/context/project_memory.py) : une section par règle, les `globs` d'une règle
`.mdc` notés en tête de sa section (Nokker n'applique pas une consigne selon le fichier ouvert). Un `NOKKER.md`
existant n'est jamais écrasé (`conflict`). Lecture seule ici ; l'écriture passe par `apply`.
"""
from __future__ import annotations

from pathlib import Path

from nokker.importers.base import Report, reconcile, split_frontmatter
from nokker.importers.claude_code import _read_json, _scan_mcp

ORIGIN = "cursor"
TARGET = "NOKKER.md"
RUNTIME_DIRS = ("extensions", "logs", "User", "CachedData", "projects")


def rules_markdown(project: Path) -> tuple[str, list[Path], list[Path]]:
    """Le texte du NOKKER.md proposé, les fichiers de règles qu'il reprend et ceux, vides, qu'il laisse."""
    parts: list[str] = []
    used: list[Path] = []
    empty: list[Path] = []
    legacy = project / ".cursorrules"
    if legacy.is_file():
        body = legacy.read_text(encoding="utf-8").strip()
        if body:
            parts.append(f"## Règles du projet (.cursorrules)\n\n{body}")
            used.append(legacy)
        else:
            empty.append(legacy)
    rules_dir = project / ".cursor" / "rules"
    for mdc in sorted(rules_dir.rglob("*.mdc")) if rules_dir.is_dir() else []:
        meta, body = split_frontmatter(mdc.read_text(encoding="utf-8"))
        body = body.strip()
        if not body:
            empty.append(mdc)
            continue
        head = [f"## {meta.get('description') or mdc.stem}"]
        if meta.get("globs"):
            head.append(f"_Dans Cursor, cette règle s'appliquait aux fichiers : `{meta['globs']}`._")
        parts.append("\n\n".join([*head, body]))
        used.append(mdc)
    if not parts:
        return "", [], empty
    return "# Consignes reprises de Cursor\n\n" + "\n\n".join(parts) + "\n", used, empty


def scan(home: Path, project: Path | None = None, project_out: Path | None = None) -> Report:
    """`project_out` : dossier où écrire NOKKER.md à la place du dépôt (commande de l'hôte : le dépôt reste monté en
    lecture seule, la CLI recopie ensuite le fichier) ; la présence d'un NOKKER.md se juge toujours dans le dépôt."""
    report = Report(ORIGIN)
    roots: list[Path] = []
    user_mcp = _read_json(home / ".cursor" / "mcp.json")
    if user_mcp and isinstance(user_mcp.get("mcpServers"), dict):
        _scan_mcp(report, home / ".cursor" / "mcp.json", user_mcp["mcpServers"])
        report.add("source-file", home / ".cursor" / "mcp.json", None, "skipped", "lu clé par clé : voir les lignes de ce fichier")
    if (home / ".cursor").is_dir():
        for d in RUNTIME_DIRS:
            if (home / ".cursor" / d).exists():
                report.add("runtime", home / ".cursor" / d, None, "skipped", "données de fonctionnement de Cursor")
        roots.append(home / ".cursor")
    if project is not None:
        text, used, empty = rules_markdown(project)
        exists = (project / TARGET).exists()
        target = (project_out or project) / TARGET
        for f in empty:
            report.add("rules", f, None, "skipped", "règle vide")
        for f in used:
            if exists or target.exists():
                report.add("rules", f, target, "conflict", f"{TARGET} existe déjà : règles à fusionner à la main")
            else:
                report.add("rules", f, target, "planned", "réunies dans NOKKER.md, lu par Nokker comme mémoire de projet")
        if used and not exists and not target.exists():
            report.items[-1].detail["content"] = text  # le texte complet, une fois, sur la dernière ligne
        pm = _read_json(project / ".cursor" / "mcp.json")
        if pm and isinstance(pm.get("mcpServers"), dict):
            _scan_mcp(report, project / ".cursor" / "mcp.json", pm["mcpServers"])
            report.add("source-file", project / ".cursor" / "mcp.json", None, "skipped", "lu clé par clé : voir les lignes de ce fichier")
        if (project / ".cursor").is_dir():
            roots.append(project / ".cursor")
    reconcile(report, roots)
    return report
