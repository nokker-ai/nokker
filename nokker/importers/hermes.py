"""Import depuis Hermes : le dossier `~/.hermes` (ou `$HERMES_HOME`). Lecture seule ici ; l'écriture passe par `apply`.

Correspondances :
  - `SOUL.md` (écrit par l'utilisateur) et `memories/USER.md`, `memories/MEMORY.md` (entrées séparées par « § ») :
    notes de mémoire de portée user (`ProjectMemory.put_imported`), une par section ou par entrée ;
  - `skills/<famille>/<skill>/SKILL.md` : même format que Nokker, copiés dans `<home>/skills/<famille>/<skill>` ;
  - `config.yaml` : serveurs MCP (`mcp_servers`, http seulement, comme pour Claude Code) ; modèle et fournisseur en
    rapport (Nokker n'a pas de registre multi-fournisseurs) ; le reste signalé ;
  - `.env` : NOMS seulement, jamais les valeurs ;
  - `cron/jobs.json` : agents Nokker proposés en BROUILLON en pause (déclencheur `schedule` quand l'horaire est une
    expression cron), créés par le noyau local quand il est joint, sinon proposés dans le rapport ;
  - historique, sessions, journaux, caches : données de fonctionnement, laissées.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from nokker.context.project_memory import MAX_TEXT, split_sections
from nokker.importers.base import Report, reconcile, short_description, split_frontmatter
from nokker.importers.claude_code import _scan_mcp

ORIGIN = "hermes"
ENTRY_DELIMITER = "\n§\n"
RUNTIME = ("state.db", "state.db-wal", "state.db-shm", "sessions", "logs", "cache", "checkpoints", "audio_cache",
           "image_cache", "kanban.db", "migration", "bin", "node_modules")
_CRON = re.compile(r"^\S+(\s+\S+){4}$")


def _note(report: Report, src: str, title: str, text: str) -> None:
    reason = "note de mémoire de portée utilisateur (rappelée dans tous les fils)"
    if len(text) > MAX_TEXT:
        reason += f" ; tronquée à {MAX_TEXT} caractères"
    report.add("user-note", src, "memory:user", "planned", reason, title=title[:120], text=text[:MAX_TEXT])


def _scan_memories(report: Report, hermes: Path) -> None:
    soul = hermes / "SOUL.md"
    if soul.is_file():
        for title, text in split_sections(soul.read_text(encoding="utf-8")):
            _note(report, f"{soul}#{title}", "Personnalité (Hermes)" if title == "(préambule)" else title, text)
    for fname, label in (("USER.md", "Profil"), ("MEMORY.md", "Mémoire")):
        f = hermes / "memories" / fname
        if not f.is_file():
            continue
        entries = [e.strip() for e in f.read_text(encoding="utf-8").split(ENTRY_DELIMITER) if e.strip()]
        for n, e in enumerate(entries, 1):
            first = e.splitlines()[0].lstrip("#-* ").strip()
            _note(report, f"{f}#{n}", f"{label} : {first[:80]}", e)
        if not entries:
            report.add("user-instructions", f, None, "skipped", "fichier vide")


def _scan_skills(report: Report, hermes: Path, dst_root: Path) -> None:
    root = hermes / "skills"
    for skill_md in sorted(root.glob("*/*/SKILL.md")) if root.is_dir() else []:
        folder = skill_md.parent
        meta, _ = split_frontmatter(skill_md.read_text(encoding="utf-8"))
        short, full = short_description(meta.get("description", ""))
        if not short:
            report.add("skill", folder, None, "skipped", "SKILL.md sans description")
            continue
        target = dst_root / folder.parent.name / folder.name
        status, reason = ("conflict", "un skill du même nom existe déjà") if target.exists() else ("planned", "")
        report.add("skill", folder, target, status, reason, name=meta.get("name") or folder.name, description=short, long_description=full)
    for extra in sorted(root.iterdir()) if root.is_dir() else []:
        if extra.is_file():
            report.add("skill-index", extra, None, "skipped", "index des skills propre à Hermes, recalculé par Nokker")


def _yaml(path: Path) -> dict | None:
    try:
        import yaml

        v = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # YAML illisible : signalé, jamais d'arrêt de l'import
        return None
    return v if isinstance(v, dict) else None


def _scan_config(report: Report, path: Path) -> None:
    data = _yaml(path)
    if data is None:
        if path.exists():
            report.add("settings", path, None, "error", "YAML illisible")
        return
    report.add("source-file", path, None, "skipped", "lu clé par clé : voir les lignes de ce fichier")
    servers = data.get("mcp_servers")
    if isinstance(servers, dict):
        _scan_mcp(report, path, servers)
    model = data.get("model")
    if model:
        shown = model if isinstance(model, str) else {k: v for k, v in model.items() if k in ("default", "provider", "base_url")} if isinstance(model, dict) else str(model)
        report.add("model", f"{path}#model", None, "skipped",
                   "Nokker choisit son modèle (ou un base_url compatible OpenAI dans sa config) : à régler à la main", model=shown)
    for key in sorted(set(data) - {"mcp_servers", "model"}):
        report.add("setting", f"{path}#{key}", None, "unsupported", "réglage propre à Hermes")


def _scan_env(report: Report, path: Path) -> None:
    if not path.is_file():
        return
    report.add("source-file", path, None, "skipped", "lu clé par clé : voir les lignes de ce fichier")
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m:
            report.add("secret-name", f"{path}#{m.group(1)}", None, "skipped",
                       "valeur jamais copiée : à ressaisir dans le coffre de Nokker si c'est un secret")


def agent_draft(job: dict) -> tuple[dict | None, str]:
    """Brouillon d'agent Nokker (en pause) depuis une tâche cron de Hermes, ou (None, raison)."""
    prompt = str(job.get("prompt") or "").strip()
    if not prompt:
        return None, "tâche sans prompt (script ou skill seul) : pas d'équivalent direct"
    sched = job.get("schedule")
    expr = sched.get("expr") or sched.get("value") if isinstance(sched, dict) else sched
    expr = str(expr or "").strip()
    if not _CRON.match(expr):
        return None, f"horaire « {expr or '?'} » : seule une expression cron se reprend telle quelle"
    name = re.sub(r"[^a-z0-9-]+", "-", str(job.get("name") or job.get("id") or "tache").lower()).strip("-")[:40] or "tache"
    # Déclencheur `schedule` : le noyau crée l'agent en pause d'office (nokker/agents/hub.py create_agent), à armer.
    return {"name": name, "role": "Tâche planifiée reprise de Hermes", "instructions": prompt,
            "trigger": {"kind": "schedule", "cron": expr, "tz": str(job.get("timezone") or "UTC")}}, ""


def _scan_cron(report: Report, path: Path) -> None:
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        report.add("cron", path, None, "error", "JSON illisible")
        return
    report.add("source-file", path, None, "skipped", "lu tâche par tâche : voir les lignes de ce fichier")
    jobs = data.get("jobs", []) if isinstance(data, dict) else data if isinstance(data, list) else []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        src = f"{path}#{job.get('id') or job.get('name') or '?'}"
        draft, why = agent_draft(job)
        if draft is None:
            report.add("cron", src, None, "unsupported", why)
        else:
            report.add("cron", src, "agents", "planned", "agent en brouillon, en pause : à relire puis activer dans Nokker", draft=draft)


def scan(home: Path, hermes_home: Path | None = None, nokker_home: Path | None = None) -> Report:
    hermes = hermes_home or home / ".hermes"
    report = Report(ORIGIN)
    if not hermes.is_dir():
        return report
    _scan_memories(report, hermes)
    _scan_skills(report, hermes, (nokker_home or home / ".nokker") / "skills")
    _scan_config(report, hermes / "config.yaml")
    _scan_env(report, hermes / ".env")
    _scan_cron(report, hermes / "cron" / "jobs.json")
    for name in RUNTIME:
        if (hermes / name).exists():
            report.add("runtime", hermes / name, None, "skipped", "données de fonctionnement de Hermes")
    for it in list(report.items):
        if "#" in it.source:
            base = it.source.split("#", 1)[0]
            if base not in report.sources():
                report.add("source-file", base, None, "skipped", "lu par sections : voir les lignes de ce fichier")
    reconcile(report, [hermes])
    return report
