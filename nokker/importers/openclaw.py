"""Import depuis OpenClaw : `~/.openclaw` et son espace de travail. Lecteur indépendant, écrit d'après la carte des
sources du Migrator réparé de hermes-nokker (`openclaw_to_hermes.py`, docs/CLAW-MIGRATE.md) : mêmes chemins, même
résolution de l'espace de travail, même règle de réconciliation ; écriture au format Nokker. Lecture seule ici.

Correspondances :
  - SOUL.md, AGENTS.md, USER.md, MEMORY.md de l'espace de travail et la mémoire du jour (`memory/*.md`) : notes de
    portée user (une par section ; une par fichier pour la mémoire du jour) ;
  - skills (espace de travail, `~/.openclaw/skills`, `.agents/skills`) : `<home>/skills/openclaw/<nom>` ;
  - config (`openclaw.json`, `clawdbot.json`, `moltbot.json`, JSON strict) : serveurs MCP http (`mcp.servers`) ;
    modèles et fournisseurs en rapport ; canaux (Telegram, Discord…) à relier depuis Nokker ; noms des variables
    d'`env` et de `.env` seulement ;
  - `exec-approvals.json` : motifs autorisés, jamais repris en silence (un allow élargit les droits) ;
  - `cron/` : format non lu (le Migrator l'archive brut) ; signalé, pas repris ;
  - jamais lus : `credentials/`, `devices/`, `identity/`, `memory/main.sqlite`, `workspace.zip`, profils
    d'authentification des agents.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from nokker.context.project_memory import MAX_TEXT, split_sections
from nokker.importers.base import Report, reconcile, short_description, split_frontmatter
from nokker.importers.claude_code import _scan_mcp

ORIGIN = "openclaw"
CONFIG_NAMES = ("openclaw.json", "clawdbot.json", "moltbot.json")
WORKSPACES = ("workspace", "workspace.default", "workspace-main", "workspace-assistant")
PERSONA = {"SOUL.md": "Personnalité (OpenClaw)", "AGENTS.md": "Consignes (OpenClaw)", "USER.md": "Profil",
           "MEMORY.md": "Mémoire"}
OWN_DOCS = ("IDENTITY.md", "TOOLS.md", "HEARTBEAT.md", "BOOTSTRAP.md")
NEVER_READ = ("credentials", "devices", "identity", "memory/main.sqlite", "workspace.zip")
RUNTIME = ("logs", "sessions", "delivery-queue", "canvas", "flows", "tasks", "todo.json")


def load_config(home: Path) -> tuple[Path | None, dict]:
    for name in CONFIG_NAMES:
        p = home / name
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if isinstance(data, dict):
                return p, data
    return None, {}


def workspaces(home: Path, config: dict) -> list[Path]:
    """Espaces de travail dans l'ordre du Migrator, plus `agents.defaults.workspace` s'il est hors du dossier."""
    out = [home / w for w in WORKSPACES if (home / w).is_dir()]
    custom = ((config.get("agents") or {}).get("defaults") or {}).get("workspace") if isinstance(config.get("agents"), dict) else None
    if isinstance(custom, str) and custom:
        c = Path(custom).expanduser().resolve()
        if c.is_dir() and home.resolve() not in (c, *c.parents):
            out.append(c)
    return out


def _first(spaces: list[Path], rel: str) -> Path | None:
    return next((w / rel for w in spaces if (w / rel).exists()), None)


def _note(report: Report, src: str, title: str, text: str) -> None:
    reason = "note de mémoire de portée utilisateur (rappelée dans tous les fils)"
    if len(text) > MAX_TEXT:
        reason += f" ; tronquée à {MAX_TEXT} caractères"
    report.add("user-note", src, "memory:user", "planned", reason, title=title[:120], text=text[:MAX_TEXT])


def _scan_persona(report: Report, spaces: list[Path]) -> None:
    for fname, label in PERSONA.items():
        f = _first(spaces, fname)
        if f is None:
            continue
        sections = split_sections(f.read_text(encoding="utf-8"))
        for title, text in sections:
            _note(report, f"{f}#{title}", label if title == "(préambule)" else f"{label} : {title}", text)
        if not sections:
            report.add("user-instructions", f, None, "skipped", "fichier vide")
    for fname in OWN_DOCS:
        f = _first(spaces, fname)
        if f is not None:
            report.add("doc", f, None, "skipped", "document propre à OpenClaw (identité, outils, battement) : pas repris")
    daily = _first(spaces, "memory")
    for f in sorted(daily.glob("*.md")) if daily is not None and daily.is_dir() else []:
        text = f.read_text(encoding="utf-8").strip()
        if text:
            _note(report, str(f), f"Mémoire du {f.stem}", text)
        else:
            report.add("user-instructions", f, None, "skipped", "fichier vide")


def _scan_skills(report: Report, home: Path, spaces: list[Path], user_home: Path, dst_root: Path) -> None:
    roots = [w / "skills" for w in spaces] + [home / "skills", user_home / ".agents" / "skills"] + [w / ".agents" / "skills" for w in spaces]
    seen: set[str] = set()
    for root in roots:
        for skill_md in sorted(root.glob("*/SKILL.md")) if root.is_dir() else []:
            folder = skill_md.parent
            meta, _ = split_frontmatter(skill_md.read_text(encoding="utf-8"))
            short, full = short_description(meta.get("description", ""))
            if folder.name in seen:
                report.add("skill", folder, None, "skipped", "un skill du même nom est déjà repris d'un autre dossier")
                continue
            seen.add(folder.name)
            if not short:
                report.add("skill", folder, None, "skipped", "SKILL.md sans description")
                continue
            target = dst_root / "openclaw" / folder.name
            status, reason = ("conflict", "un skill du même nom existe déjà") if target.exists() else ("planned", "")
            report.add("skill", folder, target, status, reason, name=meta.get("name") or folder.name, description=short, long_description=full)


def _env_names(report: Report, src: str, names: list[str]) -> None:
    for n in sorted(set(names)):
        report.add("secret-name", f"{src}#{n}", None, "skipped", "valeur jamais copiée : à ressaisir dans le coffre de Nokker si c'est un secret")


def _scan_config(report: Report, path: Path, data: dict) -> None:
    report.add("source-file", path, None, "skipped", "lu clé par clé : voir les lignes de ce fichier")
    servers = (data.get("mcp") or {}).get("servers") if isinstance(data.get("mcp"), dict) else None
    if isinstance(servers, dict):
        _scan_mcp(report, path, {k: v for k, v in servers.items() if not (isinstance(v, dict) and v.get("enabled") is False)})
    agents = data.get("agents") if isinstance(data.get("agents"), dict) else {}
    model = (agents.get("defaults") or {}).get("model") if isinstance(agents.get("defaults"), dict) else None
    providers = (data.get("models") or {}).get("providers") if isinstance(data.get("models"), dict) else None
    if model or providers:
        shown = {"model": model if isinstance(model, (str, dict)) else None,
                 "providers": {k: {kk: v.get(kk) for kk in ("baseUrl", "api", "apiType", "type") if kk in v}
                               for k, v in providers.items() if isinstance(v, dict)} if isinstance(providers, dict) else {}}
        report.add("model", f"{path}#models", None, "skipped",
                   "Nokker choisit son modèle (ou un base_url compatible OpenAI dans sa config) : à régler à la main ; clés d'API non reprises", model=shown)
    for ch in sorted((data.get("channels") or {}) if isinstance(data.get("channels"), dict) else {}):
        report.add("channel", f"{path}#channels.{ch}", None, "skipped",
                   "canal à relier depuis Nokker (Mon compte : Telegram ; jetons jamais repris)")
    env = data.get("env")
    if isinstance(env, dict):
        names = [k for k in env if k != "vars" and isinstance(env[k], str)]
        if isinstance(env.get("vars"), dict):
            names += list(env["vars"])
        _env_names(report, f"{path}#env", names)
    for key in sorted(set(data) - {"mcp", "models", "channels", "env", "agents"}):
        report.add("setting", f"{path}#{key}", None, "unsupported", "réglage propre à OpenClaw")
    if agents:
        report.add("setting", f"{path}#agents", None, "unsupported", "réglages d'agent propres à OpenClaw (modèle repris au rapport)")


def _scan_exec_approvals(report: Report, path: Path) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report.add("permission", path, None, "error", "JSON illisible")
        return
    patterns = sorted({str(e.get("pattern")) for a in ((data.get("agents") or {}).values() if isinstance(data, dict) else [])
                       if isinstance(a, dict) for e in (a.get("allowlist") or []) if isinstance(e, dict) and e.get("pattern")})
    report.add("permission", path, None, "skipped", "commandes autorisées sans demander : un allow élargit les droits, à reprendre à la main",
               rules=[f"terminal({p})" for p in patterns])


def scan(home: Path, openclaw_home: Path | None = None, nokker_home: Path | None = None) -> Report:
    oc = openclaw_home or home / ".openclaw"
    report = Report(ORIGIN)
    if not oc.is_dir():
        return report
    cfg_path, cfg = load_config(oc)
    spaces = workspaces(oc, cfg)
    _scan_persona(report, spaces)
    _scan_skills(report, oc, spaces, home, (nokker_home or home / ".nokker") / "skills")
    if cfg_path is not None:
        _scan_config(report, cfg_path, cfg)
    for name in CONFIG_NAMES:
        if (oc / name).is_file() and oc / name != cfg_path:
            report.add("settings", oc / name, None, "skipped", "configuration de secours non lue (la première lisible fait foi)")
    if (oc / ".env").is_file():
        report.add("source-file", oc / ".env", None, "skipped", "lu ligne par ligne : voir les lignes de ce fichier")
        _env_names(report, str(oc / ".env"), [m.group(1) for m in re.finditer(r"(?m)^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", (oc / ".env").read_text(encoding="utf-8"))])
    if (oc / "exec-approvals.json").is_file():
        _scan_exec_approvals(report, oc / "exec-approvals.json")
    if (oc / "cron").exists():
        report.add("cron", oc / "cron", None, "unsupported", "format des tâches d'OpenClaw non lu : à recréer comme agents dans Nokker")
    for rel in NEVER_READ:
        if (oc / rel).exists():
            report.add("sensitive", oc / rel, None, "skipped", "jamais lu : identifiants ou données sensibles")
    for prof in sorted(oc.glob("agents/*/agent/auth-profiles.json")):
        report.add("sensitive", prof, None, "skipped", "jamais lu : profils d'authentification (clés d'API)")
    for rel in RUNTIME:
        if (oc / rel).exists():
            report.add("runtime", oc / rel, None, "skipped", "données de fonctionnement d'OpenClaw")
    for sess in sorted(oc.glob("agents/*/sessions")):
        report.add("runtime", sess, None, "skipped", "historique des sessions")
    for it in list(report.items):
        if "#" in it.source:
            base = it.source.split("#", 1)[0]
            if base not in report.sources():
                report.add("source-file", base, None, "skipped", "lu par sections : voir les lignes de ce fichier")
    reconcile(report, [oc, *[w for w in spaces if oc.resolve() not in (w.resolve(), *w.resolve().parents)]])
    return report
