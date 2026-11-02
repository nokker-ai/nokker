"""Écriture d'un rapport d'import : seuls les éléments `planned` sont repris. Jamais d'écrasement : une cible apparue
entre le scan et l'écriture devient `conflict` ; un fichier de réglages est sauvegardé avant sa première modification.
  - skills et sous-agents (en skills `agents/<nom>`) copiés ou écrits, liens symboliques refusés ;
  - NOKKER.md des règles Cursor en création exclusive ; `deny`/`ask` ajoutés aux réglages de l'utilisateur ;
  - hooks d'un DÉPÔT : bloc `hooks` de son `.nokker/settings.json`, qui reste soumis à `trust_project_hooks` ;
  - serveurs MCP : même validation et même fichier que `mcp_server_add`, chacun seulement après `confirm(item)` (la
    personne, au terminal, serveur par serveur) ; sans confirmation, rien n'est ajouté ;
  - notes de mémoire de portée user : `ProjectMemory.put_imported` (appel local seulement) ;
  - tâches planifiées (Hermes) : agents créés par `create_agent(brouillon)` (le noyau les crée en pause) ; sans
    noyau joint, le brouillon reste au rapport.
Ce qu'on ne sait pas écrire passe en `skipped` avec la raison : rien n'est laissé « prévu » sans l'être.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from typing import Any, Callable

from nokker.importers.base import ImportItem, Report
from nokker.tools.mcp_config import MAX_SERVERS, McpConfigError, _write as write_mcp, check_entry, load_user_servers


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _skill(it: ImportItem) -> None:
    src, dst = Path(it.source), Path(it.target or "")
    if dst.exists():
        it.status, it.reason = "conflict", "un skill du même nom est apparu entre le scan et l'écriture"
        return
    if any(p.is_symlink() for p in [src, *src.rglob("*")]):
        it.status, it.reason = "error", "lien symbolique dans le skill : refusé (il pourrait sortir de son dossier)"
        return
    shutil.copytree(src, dst)
    md = dst / "SKILL.md"
    text = md.read_text(encoding="utf-8")
    end = text.find("\n---", 3)
    head = [ln for ln in text[3:end].splitlines() if not ln.startswith("description:")]
    body = text[end + 4:].lstrip("\n")
    if it.detail.get("long_description"):
        body = f"> {it.detail['long_description']}\n\n{body}"
    md.write_text("---\n" + "\n".join([*head, f"description: {it.detail['description']}"]).strip("\n") + "\n---\n" + body, encoding="utf-8")
    it.status, it.reason = "imported", it.reason


def _subagent(it: ImportItem) -> None:
    dst = Path(it.target or "")
    if dst.exists():
        it.status, it.reason = "conflict", "un skill du même nom est apparu entre le scan et l'écriture"
        return
    d = it.detail
    notes = [f"> {d['long_description']}"] if d.get("long_description") else []
    if d.get("tools") or d.get("model"):
        notes.append(f"_Dans Claude Code : outils `{d.get('tools') or 'tous'}`, modèle `{d.get('model') or 'par défaut'}`._")
    dst.mkdir(parents=True)
    (dst / "SKILL.md").write_text("\n".join(["---", f"name: {d['name']}", f"description: {d['description']}", "---",
                                             *notes, "", d["prompt"], ""]), encoding="utf-8")
    it.status = "imported"


def _project_hooks(it: ImportItem, backup_dir: Path) -> None:
    target = Path(it.target or "")
    data = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}
    if data.get("hooks"):
        it.status, it.reason = "conflict", f"{target} a déjà un bloc hooks : à fusionner à la main"
        return
    if target.is_file():
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup_dir / "projet-settings.json.avant-import")
    data["hooks"] = it.detail["block"]
    _atomic_json(target, data)
    it.status, it.reason = "imported", "inactif tant que vous ne déclarez pas ce dossier de confiance (nokker hooks trust)"


def _mcp(items: list[ImportItem], path: Path, confirm: Callable[[ImportItem], bool] | None) -> None:
    for it in items:
        if confirm is None or not confirm(it):
            it.status, it.reason = "skipped", "non confirmé : aucun serveur MCP n'est ajouté sans votre accord"
            continue
        servers = load_user_servers(path)
        name = it.detail["entry"]["name"]
        if any(s["name"] == name for s in servers):
            it.status, it.reason = "conflict", f"un serveur MCP s'appelle déjà {name!r}"
            continue
        if len(servers) >= MAX_SERVERS:
            it.status, it.reason = "error", f"{MAX_SERVERS} serveurs ajoutés au plus"
            continue
        try:
            write_mcp(path, [*servers, check_entry(it.detail["entry"])])
        except McpConfigError as e:
            it.status, it.reason = "error", str(e)
            continue
        it.status = "imported"


def _notes(items: list[ImportItem], memory: Any) -> None:
    for it in items:
        if memory is None:
            it.status, it.reason = "skipped", "mémoire de projet désactivée dans Nokker (local.project_memory)"
            continue
        note = memory.put_imported(it.detail["title"], it.detail["text"], it.source.split("#", 1)[0])
        it.status, it.reason = ("imported", "") if note is not None else ("error", "note refusée par la mémoire")


def _codex_memory(items: list[ImportItem], memory: Any) -> None:
    """Lot 30 d : mémoire générée par Codex écrite comme DONNÉES (jamais en portée user), seulement si le drapeau
    `local.import_codex_memory` est allumé ; sinon elle reste au rapport."""
    if memory is None or not getattr(memory, "import_data", False):
        return
    for it in items:
        notes = memory.put_import_data(it.detail.get("entries") or [], it.source)
        it.status = "imported"
        it.reason = f"importée comme données ({len(notes)} entrées), défaisable en un geste ; jamais comme consignes"
        it.detail["notes"] = [n.name for n in notes]


def _rules(items: list[ImportItem]) -> None:
    with_content = next((i for i in items if "content" in i.detail), None)
    if with_content is None:
        return
    target = Path(with_content.target or "")
    try:
        with open(target, "x", encoding="utf-8") as f:  # création exclusive : jamais d'écrasement
            f.write(with_content.detail["content"])
    except FileExistsError:
        for i in items:
            i.status, i.reason = "conflict", f"{target.name} est apparu entre le scan et l'écriture"
        return
    for i in items:
        i.status = "imported"


def _permissions(items: list[ImportItem], settings: Path, backup_dir: Path) -> None:
    if not items:
        return
    data = json.loads(settings.read_text(encoding="utf-8")) if settings.is_file() else {}
    if settings.is_file():
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(settings, backup_dir / f"{settings.name}.avant-import")
    perms = data.setdefault("permissions", {})
    for it in items:
        dest = perms.setdefault(it.detail["rule_kind"], [])
        for rule in it.detail["rules"]:
            if rule not in dest:
                dest.append(rule)
        it.status = "imported"
    _atomic_json(settings, data)


def _agents(items: list[ImportItem], create_agent: Callable[[dict], None] | None) -> None:
    for it in items:
        if create_agent is None:
            it.status, it.reason = "skipped", "noyau local non joint : brouillon d'agent proposé dans le rapport, à créer depuis Nokker"
            continue
        try:
            create_agent(it.detail["draft"])
        except Exception as e:  # refus du noyau (cron, nom pris…) : rapporté, l'import continue
            it.status, it.reason = "error", f"refusé par le noyau : {e}"[:300]
            continue
        it.status, it.reason = "imported", "agent créé en pause : à relire puis armer"


def apply(report: Report, *, user_settings: Path, backup_dir: Path, mcp_path: Path | None = None,
          confirm: Callable[[ImportItem], bool] | None = None, memory: Any = None,
          create_agent: Callable[[dict], None] | None = None) -> Report:
    planned = [i for i in report.items if i.status == "planned"]
    for it in planned:
        try:
            if it.kind == "skill":
                _skill(it)
            elif it.kind in ("subagent", "prompt"):
                _subagent(it)
            elif it.kind == "hooks":
                _project_hooks(it, backup_dir)
        except OSError as e:
            it.status, it.reason = "error", f"écriture en échec : {e.strerror}"
    _rules([i for i in planned if i.kind == "rules"])
    _permissions([i for i in planned if i.kind == "permission"], user_settings, backup_dir)
    mcp = [i for i in planned if i.kind == "mcp"]
    if mcp and mcp_path is not None:
        _mcp(mcp, mcp_path, confirm)
    _notes([i for i in planned if i.kind == "user-note"], memory)
    _codex_memory([i for i in report.items if i.kind == "codex-memory" and i.status == "skipped"], memory)
    _agents([i for i in planned if i.kind == "cron"], create_agent)
    for it in report.items:  # filet : rien ne reste « prévu » après apply
        if it.status == "planned":
            it.status, it.reason = "skipped", f"pas d'écriture pour {it.kind} dans cette version"
    return report
