"""Import depuis Claude Code : `~/.claude/` (réglages, skills, sous-agents, CLAUDE.md) et le dossier `.claude/` d'un
dépôt, plus les serveurs MCP (`.mcp.json`, `~/.claude.json`). Ce module LIT et dresse le rapport (`scan`) ; l'écriture
passe par `apply` (étape suivante), qui ne reprend que les éléments `planned`.

Correspondances (cibles décrites dans docs : skills nokker/tools/skills.py, hooks nokker/tools/hooks.py, MCP
nokker/tools/mcp_config.py, mémoire nokker/context/project_memory.py) :
  - CLAUDE.md du dépôt : déjà lu par Nokker (`native`) ; ~/.claude/CLAUDE.md : une note de mémoire de portée
    utilisateur par titre (montrées à blanc, écrites par `apply`) ;
  - sous-agents (`agents/*.md`) : skills de la famille `agents` (corps = prompt), utilisables par `delegate` ;
  - skills : `<home>/skills/claude-code/<nom>/` ou `<dépôt>/.nokker/skills/claude-code/<nom>/` (qui reste soumis à la
    confiance du dossier) ; description d'index ramenée à 60 caractères, le texte complet gardé dans le corps ;
  - hooks : PreToolUse → pre_tool, PostToolUse → post_tool, SessionStart → session_start, Stop → turn_end ; les
    autres événements n'ont pas d'équivalent ;
  - permissions : `deny` et `ask` repris (ils ne font que restreindre) ; un `allow` élargit les droits : à reprendre
    à la main, jamais en silence ;
  - MCP : serveurs http/sse proposés (entrée validée comme `mcp_server_add`, valeurs d'en-têtes remplacées par des
    références `secret:NOM`) ; un serveur stdio (commande lancée sur l'hôte) n'a pas d'équivalent ;
  - `env` : NOMS seulement, la valeur n'est jamais copiée.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from nokker.context.project_memory import MAX_TEXT, split_sections
from nokker.importers.base import Report, reconcile, short_description, split_frontmatter
from nokker.tools.mcp_config import McpConfigError, check_entry

ORIGIN = "claude-code"
FAMILY = "claude-code"

HOOK_EVENTS = {"PreToolUse": "pre_tool", "PostToolUse": "post_tool", "SessionStart": "session_start", "Stop": "turn_end"}
# Outil Claude Code → outil(s) Nokker de même effet (règles de permission et `match` des hooks).
TOOL_MAP: dict[str, tuple[str, ...]] = {
    "Bash": ("terminal",),
    "Edit": ("write_file", "patch"),
    "MultiEdit": ("write_file", "patch"),
    "Write": ("write_file",),
    "Read": ("read_file",),
}
# Données de fonctionnement de Claude Code (historique, caches) : rien à reprendre, et rien de perdu.
RUNTIME_DIRS = ("projects", "todos", "shell-snapshots", "statsig", "ide", "logs", "file-history", "session-env",
                "plans", "debug", "cache", "telemetry")


def translate_tool(spec: str) -> list[str] | None:
    """`Bash(npm test:*)` → [`terminal(npm test*)`] ; `Edit` → [`write_file`, `patch`] ; outil inconnu → None."""
    name, _, rest = spec.partition("(")
    mapped = TOOL_MAP.get(name.strip())
    if mapped is None:
        return None
    if not rest:
        return list(mapped)
    pattern = rest.rstrip(")").replace(":*", "*")
    return [f"{m}({pattern})" for m in mapped]


def translate_matcher(matcher: str) -> list[str] | None:
    """Matcher de hook (`Bash`, `Edit|Write`, `*`, vide) → motifs `match` Nokker ; un outil sans équivalent → None."""
    matcher = (matcher or "").strip()
    if matcher in ("", "*"):
        return ["*"]
    out: list[str] = []
    for part in matcher.split("|"):
        t = translate_tool(part)
        if t is None:
            return None
        out += [x for x in t if x not in out]
    return out


def _read_json(path: Path) -> dict | None:
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return v if isinstance(v, dict) else None


def _scan_skills(report: Report, src_root: Path, dst_root: Path, scope: str) -> None:
    if not src_root.is_dir():
        return
    for skill_md in sorted(src_root.glob("*/SKILL.md")):
        folder = skill_md.parent
        try:
            meta, _ = split_frontmatter(skill_md.read_text(encoding="utf-8"))
        except OSError as e:
            report.add("skill", folder, None, "error", f"illisible : {e.strerror}")
            continue
        name = meta.get("name") or folder.name
        short, full = short_description(meta.get("description", ""))
        if not short:
            report.add("skill", folder, None, "skipped", "SKILL.md sans description : Nokker ne saurait pas quand s'en servir.")
            continue
        target = dst_root / FAMILY / folder.name
        status, reason = ("conflict", "un skill du même nom existe déjà") if target.exists() else ("planned", "")
        if scope == "project" and status == "planned":
            reason = "à déclarer de confiance dans Nokker (skills du dépôt)"
        report.add("skill", folder, target, status, reason, name=name, description=short, long_description=full)


def _scan_hooks(report: Report, settings: Path, block: dict, scope: str) -> None:
    proposed: dict[str, list[dict]] = {}
    for event, groups in block.items():
        target_event = HOOK_EVENTS.get(event)
        if target_event is None:
            report.add("hook", f"{settings}#hooks.{event}", None, "unsupported", f"l'événement {event} n'a pas d'équivalent dans Nokker")
            continue
        for g in groups if isinstance(groups, list) else []:
            matches = translate_matcher(str(g.get("matcher", "")))
            if matches is None:
                report.add("hook", f"{settings}#hooks.{event}", None, "unsupported", f"outil sans équivalent : {g.get('matcher')}")
                continue
            for h in g.get("hooks") or []:
                if h.get("type", "command") != "command" or not str(h.get("command", "")).strip():
                    report.add("hook", f"{settings}#hooks.{event}", None, "unsupported", "seuls les hooks de type commande sont repris")
                    continue
                for m in matches:
                    e = {"match": m, "command": h["command"]}
                    if isinstance(h.get("timeout"), int):
                        e["timeout_s"] = h["timeout"]
                    proposed.setdefault(target_event, []).append(e)
    if proposed:
        # Un hook exécute une commande : jamais écrit en silence. Ceux d'un dépôt vont dans son .nokker/settings.json
        # (soumis à la confiance du dossier) ; ceux de l'utilisateur sont proposés, à coller par lui.
        status, reason = ("planned", "bloc .nokker/settings.json du dépôt, soumis à confiance") if scope == "project" else (
            "skipped", "hooks de l'utilisateur : bloc proposé à coller dans ses réglages Nokker, jamais écrit sans lui")
        target = settings.parent.parent / ".nokker" / "settings.json" if scope == "project" else None
        report.add("hooks", f"{settings}#hooks", target, status, reason, block=proposed)


def _scan_permissions(report: Report, settings: Path, perms: dict) -> None:
    for kind in ("deny", "ask", "allow"):
        for spec in perms.get(kind) or []:
            rules = translate_tool(str(spec))
            src = f"{settings}#permissions.{kind}"
            if rules is None:
                report.add("permission", src, None, "unsupported", f"outil sans équivalent : {spec}")
            elif kind == "allow":
                report.add("permission", src, None, "skipped", "un allow élargit les droits : à reprendre à la main", rules=rules, spec=spec)
            else:
                report.add("permission", src, "settings.permissions", "planned", "", rules=rules, rule_kind=kind, spec=spec)


def _scan_settings(report: Report, path: Path, scope: str) -> None:
    data = _read_json(path)
    if data is None:
        if path.exists():
            report.add("settings", path, None, "error", "JSON illisible")
        return
    report.add("source-file", path, None, "skipped", "lu clé par clé : voir les lignes de ce fichier")
    if isinstance(data.get("hooks"), dict):
        _scan_hooks(report, path, data["hooks"], scope)
    if isinstance(data.get("permissions"), dict):
        _scan_permissions(report, path, data["permissions"])
    for name in sorted((data.get("env") or {}) if isinstance(data.get("env"), dict) else {}):
        report.add("secret-name", f"{path}#env.{name}", None, "skipped", "valeur jamais copiée : à ressaisir dans le coffre de Nokker si c'est un secret")
    for key in sorted(set(data) - {"hooks", "permissions", "env"}):
        report.add("setting", f"{path}#{key}", None, "unsupported", "réglage propre à Claude Code")


def _secret_name(server: str, header: str) -> str:
    return re.sub(r"[^A-Z0-9_]", "_", f"MCP_{server}_{header}".upper())


def mcp_entry(name: str, cfg: dict) -> tuple[dict, list[str]]:
    """Entrée `mcp-servers.yaml` proposée (validée par `check_entry`, comme `mcp_server_add`) et les NOMS de secrets à
    poser. Aucune valeur d'en-tête n'est reprise : chacune devient une référence `secret:NOM` du coffre."""
    clean = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:40] or "serveur"
    raw: dict = {"name": clean, "url": cfg.get("url"), "purpose": f"importé ({name})"}
    secrets: list[str] = []
    headers: dict[str, str] = {}
    for key in (cfg.get("headers") or {}) if isinstance(cfg.get("headers"), dict) else {}:
        ref = _secret_name(clean, "token" if key.lower() == "authorization" else key)
        secrets.append(ref)
        if key.lower() == "authorization":
            raw["token"] = f"secret:{ref}"
        else:
            headers[key] = f"secret:{ref}"
    if headers:
        raw["headers"] = headers
    return check_entry(raw), secrets


def _scan_mcp(report: Report, path: Path, servers: dict) -> None:
    for name, cfg in sorted(servers.items()):
        src = f"{path}#mcpServers.{name}"
        if not isinstance(cfg, dict):
            report.add("mcp", src, None, "error", "entrée illisible")
        elif cfg.get("type") in ("http", "sse") or (cfg.get("url") and not cfg.get("command")):
            try:
                entry, secrets = mcp_entry(name, cfg)
            except McpConfigError as e:
                report.add("mcp", src, None, "unsupported", f"refusé par Nokker : {e}")
                continue
            reason = "ajout après confirmation, serveur par serveur, comme mcp_server_add ; actif au prochain redémarrage"
            if secrets:
                reason += " ; à poser ensuite : " + ", ".join(f"nokker secret set {n}" for n in secrets)
            report.add("mcp", src, "mcp-servers", "planned", reason, name=entry["name"], entry=entry, secret_names=secrets)
        else:
            report.add("mcp", src, None, "unsupported", "serveur stdio (commande lancée sur l'hôte) : pas d'équivalent dans Nokker")


def _scan_subagents(report: Report, folder: Path, dst_root: Path, scope: str) -> None:
    for agent in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        meta, body = split_frontmatter(agent.read_text(encoding="utf-8"))
        name = re.sub(r"[^a-z0-9-]+", "-", (meta.get("name") or agent.stem).lower()).strip("-") or agent.stem
        short, full = short_description(meta.get("description") or f"Sous-agent {name}")
        if not body.strip():
            report.add("subagent", agent, None, "skipped", "sous-agent sans prompt")
            continue
        target = dst_root / "agents" / name
        status = "conflict" if target.exists() else "planned"
        reason = "un skill du même nom existe déjà" if target.exists() else \
            "importé comme skill (Nokker n'a pas de sous-agent nommé) : utilisable par delegate"
        if scope == "project" and status == "planned":
            reason += " ; à déclarer de confiance (skills du dépôt)"
        report.add("subagent", agent, target, status, reason, name=name, description=short, long_description=full,
                   prompt=body.strip(), tools=meta.get("tools", ""), model=meta.get("model", ""))


def _scan_user_instructions(report: Report, path: Path) -> None:
    sections = split_sections(path.read_text(encoding="utf-8"))
    for title, text in sections:
        title = "Consignes générales" if title == "(préambule)" else title
        reason = "note de mémoire de portée utilisateur (rappelée dans tous les fils)"
        if len(text) > MAX_TEXT:
            reason += f" ; tronquée à {MAX_TEXT} caractères"
        report.add("user-note", f"{path}#{title}", "memory:user", "planned", reason, title=title, text=text[:MAX_TEXT])
    if not sections:
        report.add("user-instructions", path, None, "skipped", "fichier vide")


def scan(home: Path, project: Path | None = None, nokker_home: Path | None = None) -> Report:
    """Rapport à blanc : rien n'est écrit. `home` = dossier personnel, `project` = racine d'un dépôt (facultatif)."""
    report = Report(ORIGIN)
    claude = home / ".claude"
    user_skills_dst = (nokker_home or home / ".nokker") / "skills"

    if (claude / "CLAUDE.md").is_file():
        _scan_user_instructions(report, claude / "CLAUDE.md")
    _scan_skills(report, claude / "skills", user_skills_dst, "user")
    for f in ("settings.json", "settings.local.json"):
        _scan_settings(report, claude / f, "user")
    _scan_subagents(report, claude / "agents", user_skills_dst, "user")
    for d in RUNTIME_DIRS:
        if (claude / d).exists():
            report.add("runtime", claude / d, None, "skipped", "données de fonctionnement de Claude Code (historique, caches)")
    user_json = _read_json(home / ".claude.json")
    if user_json and isinstance(user_json.get("mcpServers"), dict):
        _scan_mcp(report, home / ".claude.json", user_json["mcpServers"])

    roots = [claude]
    if project is not None:
        pc = project / ".claude"
        if (project / "CLAUDE.md").is_file():
            report.add("project-instructions", project / "CLAUDE.md", project / "CLAUDE.md", "native", "déjà lu par Nokker (mémoire de projet)")
        _scan_skills(report, pc / "skills", project / ".nokker" / "skills", "project")
        for f in ("settings.json", "settings.local.json"):
            _scan_settings(report, pc / f, "project")
        _scan_subagents(report, pc / "agents", project / ".nokker" / "skills", "project")
        mcp = _read_json(project / ".mcp.json")
        if mcp and isinstance(mcp.get("mcpServers"), dict):
            _scan_mcp(report, project / ".mcp.json", mcp["mcpServers"])
        roots.append(pc)

    # Les sources lues par clé (`fichier#clé`) couvrent leur fichier pour la réconciliation.
    for it in list(report.items):
        if "#" in it.source:
            base = it.source.split("#", 1)[0]
            if base not in report.sources():
                report.add("source-file", base, None, "skipped", "lu clé par clé : voir les lignes de ce fichier")
    reconcile(report, roots)
    return report
