"""Chemins LUS par chaque importeur, relatifs au dossier personnel : la commande de l'hôte ne monte que ceux-là, en
lecture seule, dans un conteneur jetable (`docker compose run --rm`), jamais tout le dossier de l'agent. `never` :
ce qui ne doit JAMAIS être monté (identifiants), même si un chemin parent l'était. Le dossier d'un projet
(`--project`) est monté à part par la commande (règles Cursor, `.claude/` du dépôt)."""
from __future__ import annotations

DIRS = {".claude/skills", ".claude/agents", ".cursor/rules", ".hermes/memories", ".hermes/skills", ".openclaw/workspace",
        ".openclaw/workspace.default", ".openclaw/workspace-main", ".openclaw/workspace-assistant", ".openclaw/skills",
        ".agents/skills"}


def listing(source: str) -> dict:
    """Contrat de la CLI de l'hôte : chemins relatifs (jamais absolus ni `..`), `kind` file|dir ; un chemin absent
    sur la machine reste listé (la CLI l'ignore) ; rien de `never` n'est listé."""
    def entries(paths: list[str]) -> list[dict[str, str]]:
        return [{"path": p, "kind": "dir" if p in DIRS else "file"} for p in paths]

    spec = READ_PATHS[source]
    return {"source": source, "paths": entries(spec["home"]), "project_paths": entries(spec["project"])}


READ_PATHS: dict[str, dict[str, list[str]]] = {
    "claude-code": {
        "home": [".claude/CLAUDE.md", ".claude/settings.json", ".claude/settings.local.json", ".claude/skills",
                 ".claude/agents", ".claude.json"],
        "project": ["CLAUDE.md", ".claude/settings.json", ".claude/settings.local.json", ".claude/skills",
                    ".claude/agents", ".mcp.json"],
        "never": [".claude/.credentials.json"],
    },
    "cursor": {
        "home": [".cursor/mcp.json"],
        "project": [".cursorrules", ".cursor/rules", ".cursor/mcp.json", "NOKKER.md"],  # NOKKER.md : conflit jugé dans le dépôt
        "never": [],
    },
    "hermes": {
        "home": [".hermes/SOUL.md", ".hermes/memories", ".hermes/skills", ".hermes/config.yaml", ".hermes/cron/jobs.json"],
        "project": [],
        "never": [".hermes/.env", ".hermes/auth.json", ".hermes/state.db"],
    },
    "openclaw": {
        "home": [".openclaw/openclaw.json", ".openclaw/clawdbot.json", ".openclaw/moltbot.json", ".openclaw/workspace",
                 ".openclaw/workspace.default", ".openclaw/workspace-main", ".openclaw/workspace-assistant",
                 ".openclaw/skills", ".openclaw/exec-approvals.json", ".agents/skills"],
        "project": [],
        "never": [".openclaw/.env", ".openclaw/credentials", ".openclaw/devices", ".openclaw/identity",
                  ".openclaw/agents", ".openclaw/memory/main.sqlite", ".openclaw/workspace.zip"],
    },
}
