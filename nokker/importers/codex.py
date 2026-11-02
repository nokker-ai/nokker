"""Import depuis Codex (OpenAI) : `$CODEX_HOME` (défaut `~/.codex`), `~/.agents/skills` et, pour un dépôt, ses
`AGENTS.md`, `.codex/` et `.agents/skills`. Lecture seule ici ; l'écriture passe par `apply`.

Sources vérifiées sur le code de Codex (dépôt openai/codex, 11.2026) et sa doc officielle :
  - consignes : `$CODEX_HOME/AGENTS.md` et `AGENTS.override.md` (globales) → notes de portée user ; l'`AGENTS.md` d'un
    dépôt est déjà lu par Nokker (`native`) ; un `AGENTS.override.md` de dépôt n'a pas d'équivalent ;
  - skills : `~/.agents/skills` (emplacement actuel), `$CODEX_HOME/skills` (ancien, encore lu par Codex), et dans un
    dépôt `.agents/skills` et `.codex/skills` ; le cache des skills livrés avec Codex (`skills/.system`) est laissé ;
  - prompts personnalisés `$CODEX_HOME/prompts/*.md` : Codex ne les lit plus ; repris comme skills (corps = prompt) ;
  - mémoire (`memories/` = v1, `memories_v2/` = Memory v2, mêmes noms de fichiers) : `memory_summary.md`, `MEMORY.md`,
    `raw_memories.md`, `rollout_summaries/`, `skills/`, `extensions/ad_hoc/notes/`. Ce sont des fichiers GÉNÉRÉS par
    le modèle de Codex à partir des conversations (pas écrits par la personne) : montrés au rapport, jamais écrits en
    mémoire de portée user sans décision ;
  - `config.toml` (utilisateur, et `.codex/config.toml` d'un dépôt) : serveurs MCP http (`mcp_servers.<nom>.url`,
    jetons et en-têtes en références `secret:NOM`, jamais une valeur), stdio sans équivalent ; modèle, fournisseurs,
    bac à sable et politique d'approbation au rapport ; noms des variables d'environnement seulement ;
  - jamais lus : `auth.json` (identifiants), `sessions/`, `history.jsonl`, `log/`.
"""
from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

from nokker.context.project_memory import MAX_TEXT, split_sections
from nokker.importers.base import Report, reconcile, short_description, split_frontmatter
from nokker.tools.mcp_config import McpConfigError, check_entry

ORIGIN = "codex"
FAMILY = "codex"
MEMORY_DIRS = ("memories", "memories_v2")
NEVER_READ = ("auth.json", ".credentials.json")
MEMORY_REASON = ("mémoire générée par le modèle de Codex à partir des conversations : montrée ici, pas reprise sans "
                 "décision")
RUNTIME = ("sessions", "archived_sessions", "history.jsonl", "log", "logs", "cache", "tmp", "version.json", "models_cache.json", "shell_snapshots")


def codex_home(home: Path) -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else home / ".codex"


def _note(report: Report, src: str, title: str, text: str) -> None:
    reason = "note de mémoire de portée utilisateur (rappelée dans tous les fils)"
    if len(text) > MAX_TEXT:
        reason += f" ; tronquée à {MAX_TEXT} caractères"
    report.add("user-note", src, "memory:user", "planned", reason, title=title[:120], text=text[:MAX_TEXT])


def _scan_instructions(report: Report, ch: Path) -> None:
    for fname, label in (("AGENTS.md", "Consignes (Codex)"), ("AGENTS.override.md", "Consignes prioritaires (Codex)")):
        f = ch / fname
        if not f.is_file():
            continue
        sections = split_sections(f.read_text(encoding="utf-8"))
        for title, text in sections:
            _note(report, f"{f}#{title}", label if title == "(préambule)" else f"{label} : {title}", text)
        if not sections:
            report.add("user-instructions", f, None, "skipped", "fichier vide")


def _scan_skill_root(report: Report, root: Path, dst_root: Path, scope: str, seen: set[str]) -> None:
    for skill_md in sorted(root.glob("*/SKILL.md")) if root.is_dir() else []:
        folder = skill_md.parent
        if folder.name.startswith("."):
            continue
        meta, _ = split_frontmatter(skill_md.read_text(encoding="utf-8"))
        short, full = short_description(meta.get("description", ""))
        if folder.name in seen:
            report.add("skill", folder, None, "skipped", "un skill du même nom est déjà repris d'un autre dossier")
            continue
        seen.add(folder.name)
        if not short:
            report.add("skill", folder, None, "skipped", "SKILL.md sans description : Nokker ne saurait pas quand s'en servir.")
            continue
        target = dst_root / FAMILY / folder.name
        status, reason = ("conflict", "un skill du même nom existe déjà") if target.exists() else ("planned", "")
        if scope == "project" and status == "planned":
            reason = "à déclarer de confiance dans Nokker (skills du dépôt)"
        report.add("skill", folder, target, status, reason, name=meta.get("name") or folder.name, description=short, long_description=full)
    system = root / ".system"
    if system.is_dir():
        report.add("runtime", system, None, "skipped", "skills livrés avec Codex : laissés, Nokker a les siens")


def _scan_prompts(report: Report, ch: Path, dst_root: Path) -> None:
    for p in sorted((ch / "prompts").glob("*.md")) if (ch / "prompts").is_dir() else []:
        meta, body = split_frontmatter(p.read_text(encoding="utf-8"))
        if not body.strip():
            report.add("prompt", p, None, "skipped", "prompt vide")
            continue
        name = re.sub(r"[^a-z0-9-]+", "-", p.stem.lower()).strip("-") or "prompt"
        short, full = short_description(meta.get("description") or f"Commande /{p.stem} reprise de Codex")
        target = dst_root / FAMILY / name
        if target.exists():
            report.add("prompt", p, target, "conflict", "un skill du même nom existe déjà")
            continue
        report.add("prompt", p, target, "planned", "prompt personnalisé repris comme skill (Codex ne les lit plus)",
                   name=name, description=short, long_description=full, prompt=body.strip())


def _file_date(p: Path) -> str:
    """Date d'origine d'une entrée : le préfixe horodaté d'une note ad hoc (`AAAA-MM-JJTHH-MM-SS-…`), sinon la date de
    dernière modification du fichier."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})T", p.name)
    if m:
        return m.group(1)
    import datetime as dt

    return dt.datetime.fromtimestamp(p.stat().st_mtime, dt.UTC).strftime("%Y-%m-%d")


def memory_entries(text: str, date: str, default_title: str) -> list[dict]:
    """Une entrée par puce de premier niveau, ou par paragraphe, dans chaque section (titre = la section)."""
    out: list[dict] = []
    for title, body in split_sections(text):
        title = default_title if title == "(préambule)" else title
        chunks: list[str] = []
        for block in re.split(r"\n\s*\n", body):
            lines = block.splitlines()
            if lines and all(re.match(r"\s*([-*]|\d+\.)\s+", ln) or ln.startswith((" ", "\t")) for ln in lines):
                cur: list[str] = []
                for ln in lines:
                    if re.match(r"([-*]|\d+\.)\s+", ln) and cur:
                        chunks.append("\n".join(cur))
                        cur = []
                    cur.append(ln)
                if cur:
                    chunks.append("\n".join(cur))
            elif block.strip():
                chunks.append(block)
        for c in chunks:
            c = re.sub(r"^\s*([-*]|\d+\.)\s+", "", c.strip())
            if c:
                first = " ".join(c.split())[:60]
                out.append({"title": f"{title} : {first}" if title else first, "text": c, "date": date})
    return out


def _scan_memories(report: Report, ch: Path) -> None:
    for d in MEMORY_DIRS:
        root = ch / d
        if not root.is_dir():
            continue
        version = "v2" if d.endswith("_v2") else "v1"
        for fname in ("memory_summary.md", "MEMORY.md"):
            f = root / fname
            if f.is_file() and f.read_text(encoding="utf-8").strip():
                text = f.read_text(encoding="utf-8").strip()
                report.add("codex-memory", f, None, "skipped", MEMORY_REASON, version=version, preview=text[:MAX_TEXT],
                           length=len(text), entries=memory_entries(text, _file_date(f), "Mémoire Codex"))
        notes = root / "extensions" / "ad_hoc" / "notes"
        for f in sorted(notes.glob("*")) if notes.is_dir() else []:
            if f.is_file():
                text = f.read_text(encoding="utf-8").strip()
                report.add("codex-memory", f, None, "skipped", MEMORY_REASON, version=version, preview=text[:MAX_TEXT],
                           length=len(text), entries=[{"title": f"Note Codex : {' '.join(text.split())[:60]}", "text": text,
                                                       "date": _file_date(f)}] if text else [])
        for sub in ("raw_memories.md", "rollout_summaries", "skills"):
            if (root / sub).exists():
                report.add("runtime", root / sub, None, "skipped", "données de fonctionnement de Codex (entrées de la mémoire, résumés de conversations)")


def mcp_entry(name: str, cfg: dict) -> tuple[dict, list[str]]:
    """Entrée `mcp-servers.yaml` (validée par `check_entry`, comme `mcp_server_add`) et les NOMS de secrets à poser.
    Codex nomme déjà ses secrets par variable d'environnement (`bearer_token_env_var`, `env_http_headers`) : ces noms
    sont repris tels quels ; une valeur écrite en clair (`http_headers`) devient une référence à poser."""
    clean = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:40] or "serveur"
    raw: dict = {"name": clean, "url": cfg.get("url"), "purpose": f"importé ({name})"}
    secrets: list[str] = []
    if isinstance(cfg.get("bearer_token_env_var"), str) and cfg["bearer_token_env_var"]:
        ref = re.sub(r"[^A-Z0-9_]", "_", cfg["bearer_token_env_var"].upper())
        raw["token"] = f"secret:{ref}"
        secrets.append(ref)
    headers: dict[str, str] = {}
    for key, env_name in (cfg.get("env_http_headers") or {}).items() if isinstance(cfg.get("env_http_headers"), dict) else []:
        ref = re.sub(r"[^A-Z0-9_]", "_", str(env_name).upper())
        headers[key] = f"secret:{ref}"
        secrets.append(ref)
    for key in (cfg.get("http_headers") or {}) if isinstance(cfg.get("http_headers"), dict) else []:
        ref = re.sub(r"[^A-Z0-9_]", "_", f"MCP_{clean}_{key}".upper())
        if key.lower() == "authorization":
            raw["token"] = f"secret:{ref}"
        else:
            headers[key] = f"secret:{ref}"
        secrets.append(ref)
    if headers:
        raw["headers"] = headers
    return check_entry(raw), secrets


def _scan_config(report: Report, path: Path) -> None:
    if not path.is_file():
        return
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        report.add("settings", path, None, "error", "TOML illisible")
        return
    report.add("source-file", path, None, "skipped", "lu clé par clé : voir les lignes de ce fichier")
    for name, cfg in sorted((data.get("mcp_servers") or {}).items()):
        src = f"{path}#mcp_servers.{name}"
        if not isinstance(cfg, dict):
            report.add("mcp", src, None, "error", "entrée illisible")
        elif cfg.get("enabled") is False:
            report.add("mcp", src, None, "skipped", "serveur désactivé dans Codex : laissé")
        elif cfg.get("url"):
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
            for env_name in sorted((cfg.get("env") or {}) if isinstance(cfg.get("env"), dict) else {}):
                report.add("secret-name", f"{src}.env.{env_name}", None, "skipped",
                           "valeur jamais copiée : à ressaisir dans le coffre de Nokker si c'est un secret")
    if data.get("model") or data.get("model_provider") or data.get("model_providers"):
        providers = data.get("model_providers") if isinstance(data.get("model_providers"), dict) else {}
        shown = {"model": data.get("model"), "model_provider": data.get("model_provider"),
                 "providers": {k: {kk: v.get(kk) for kk in ("name", "base_url", "wire_api", "env_key") if kk in v}
                               for k, v in providers.items() if isinstance(v, dict)}}
        report.add("model", f"{path}#model", None, "skipped",
                   "Nokker choisit son modèle (ou un base_url compatible OpenAI dans sa config) : à régler à la main", model=shown)
    for key in ("sandbox_mode", "approval_policy", "sandbox_workspace_write"):
        if key in data:
            report.add("sandbox", f"{path}#{key}", None, "skipped",
                       "bac à sable et approbations de Codex : Nokker a ses propres modes et cartes, à régler dans Nokker",
                       value=data[key] if not isinstance(data[key], dict) else {k: v for k, v in data[key].items() if k != "env"})
    sep = data.get("shell_environment_policy")
    if isinstance(sep, dict) and isinstance(sep.get("set"), dict):
        for env_name in sorted(sep["set"]):
            report.add("secret-name", f"{path}#shell_environment_policy.set.{env_name}", None, "skipped",
                       "valeur jamais copiée : à ressaisir dans le coffre de Nokker si c'est un secret")
    known = {"mcp_servers", "model", "model_provider", "model_providers", "sandbox_mode", "approval_policy",
             "sandbox_workspace_write", "shell_environment_policy"}
    for key in sorted(set(data) - known):
        report.add("setting", f"{path}#{key}", None, "unsupported", "réglage propre à Codex")


def scan(home: Path, project: Path | None = None, codex_dir: Path | None = None, nokker_home: Path | None = None) -> Report:
    report = Report(ORIGIN)
    ch = codex_dir or codex_home(home)
    user_skills = (nokker_home or home / ".nokker") / "skills"
    seen: set[str] = set()
    roots: list[Path] = []
    if ch.is_dir():
        _scan_instructions(report, ch)
        _scan_skill_root(report, home / ".agents" / "skills", user_skills, "user", seen)
        _scan_skill_root(report, ch / "skills", user_skills, "user", seen)
        _scan_prompts(report, ch, user_skills)
        _scan_memories(report, ch)
        _scan_config(report, ch / "config.toml")
        for name in NEVER_READ:
            if (ch / name).exists():
                report.add("sensitive", ch / name, None, "skipped", "jamais lu : identifiants ou données sensibles")
        for name in RUNTIME:
            if (ch / name).exists():
                report.add("runtime", ch / name, None, "skipped", "données de fonctionnement de Codex (historique, journaux)")
        roots.append(ch)
    elif (home / ".agents" / "skills").is_dir():
        _scan_skill_root(report, home / ".agents" / "skills", user_skills, "user", seen)
    if project is not None:
        if (project / "AGENTS.md").is_file():
            report.add("project-instructions", project / "AGENTS.md", project / "AGENTS.md", "native", "déjà lu par Nokker (mémoire de projet)")
        if (project / "AGENTS.override.md").is_file():
            report.add("project-instructions", project / "AGENTS.override.md", None, "unsupported",
                       "AGENTS.override.md n'a pas d'équivalent : à fusionner à la main dans AGENTS.md ou NOKKER.md")
        pseen: set[str] = set()
        for root in (project / ".agents" / "skills", project / ".codex" / "skills"):
            _scan_skill_root(report, root, project / ".nokker" / "skills", "project", pseen)
        _scan_config(report, project / ".codex" / "config.toml")
        if (project / ".codex").is_dir():
            roots.append(project / ".codex")
    for it in list(report.items):
        if "#" in it.source:
            base = it.source.split("#", 1)[0]
            if base not in report.sources():
                report.add("source-file", base, None, "skipped", "lu par sections : voir les lignes de ce fichier")
    reconcile(report, roots)
    return report
