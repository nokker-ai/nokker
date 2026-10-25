"""nokker/tools/skills.py — compétences (« skills ») de Nokker Lokkal (docs/LOKKAL-CLI.md §3.1, LOKKAL.md §7.4).

Une compétence est un dossier `<famille>/<skill>/SKILL.md` (frontmatter `name`, `description`, `platforms`…) avec,
au besoin, `references/`, `templates/`, `assets/`, `scripts/`. Le modèle voit un index compact, charge un mode
d'emploi par `skill_view`, et peut en installer / retirer (carte d'approbation, effet DESTRUCTIVE).

Reprise de la bibliothèque d'agent amont au niveau de ses BRIQUES (règle produit du 16.10), comme `code.py` : `agent.skill_utils.parse_frontmatter`,
`tools.skills_guard` (scan, politique d'installation), `tools.skills_hub_install` (quarantaine, installation) et
les sources `GitHubSource` / `UrlSource` / `OptionalSkillSource`. Jamais ses fonctions-outils (`skills_tool` lit sa
config, installe des dépendances et prétraite les gabarits) : ici JAMAIS de prétraitement de gabarit, JAMAIS de
shell, JAMAIS d'installation de dépendances.

Garde en ÉCHEC FERMÉ : toute compétence hors des racines de confiance passe par `scan_skill_cached` ; `dangerous` =
invisible (journalisé), `caution` = visible avec un avertissement. Sans l'amont on ne peut pas scanner : ces
compétences sont invisibles et `skill_list` le dit.

Skills du DÉPÔT (`<dépôt>/.nokker/skills`, `ProjectSkills`, drapeau `local.project_skills`) : ils viennent avec le
code, donc d'un tiers possible — même règle que les hooks du dépôt (nokker/tools/hooks.py). Invisibles tant que
l'utilisateur n'a pas déclaré de confiance CE dossier et CETTE empreinte de l'arbre (`trusted_project_skills: [{root,
sha256}]` dans SES réglages, jamais dans ceux du dépôt) ; empreinte recalculée à chaque lecture du catalogue (un skill
réécrit pendant la session, par le modèle en `accept_edits` par exemple, redevient invisible jusqu'à une nouvelle
confiance) ; jamais en mode plan ; puis le garde habituel (ils ne sont jamais dans une racine de confiance). Portée :
le fil de la session ouverte dans ce dépôt (cwd sous sa racine git) et ses sous-agents `<fil>/sa-<id>`.

L'amont n'est importé qu'à l'usage, dans un try : le module se charge sans lui (CI unitaire). `HERMES_HOME` est posé
sur `$NOKKER_HOME` avant tout import.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult

log = logging.getLogger("nokker.skills")

MAX_FILE_BYTES = 256 * 1024
MAX_LINKED_FILES = 200
SUPPORT_DIRS = ("references", "templates", "assets", "scripts")
_EXCLUDED_DIRS = {".git", ".github", ".hub", ".scan-cache", "node_modules", "__pycache__"}
_PLATFORMS = {"macos": "darwin", "linux": "linux", "windows": "win32"}
PROJECT_SKILLS_DIR = (".nokker", "skills")
PROJECT_TRUST_KEY = "trusted_project_skills"
MAX_PROJECT_FILES = 500  # au-delà, l'arbre des skills du dépôt est ignoré en entier (empreinte non calculée)
MAX_PROJECT_BYTES = 4 * 1024 * 1024
SUBAGENT_MARK = "/sa-"  # fil d'un sous-agent : `<fil parent>/sa-<id>` (nokker/tools/delegate.py)

_SPECS: tuple[tuple[str, str, Effect, dict[str, Any]], ...] = (
    ("skill_list", "Liste les compétences disponibles (nom, famille, description), filtrables par famille.",
     Effect.READ, {"type": "object", "properties": {
         "category": {"type": "string", "description": "famille à lister (ex. software-development)"}}}),
    ("skill_view", "Lit le mode d'emploi d'une compétence, ou un de ses fichiers liés (references/, templates/…).",
     Effect.READ, {"type": "object", "required": ["name"], "properties": {
         "name": {"type": "string", "description": "nom de la compétence (ou famille/nom)"},
         "file": {"type": "string", "description": "fichier lié à lire, relatif au dossier de la compétence"}}}),
    ("skill_install", "Installe une compétence : github:<owner>/<repo>[/<chemin>], URL https vers un SKILL.md, "
     "ou official/<famille>/<nom>. Scannée avant installation.", Effect.DESTRUCTIVE, {
         "type": "object", "required": ["source"], "properties": {
             "source": {"type": "string"},
             "category": {"type": "string", "description": "famille d'accueil (dossier), optionnelle"}}}),
    ("skill_remove", "Retire une compétence installée par l'utilisateur (jamais une compétence fournie).",
     Effect.DESTRUCTIVE, {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}),
)
SKILL_TOOLS = tuple(name for name, *_ in _SPECS)


# ─────────────────────────────── amont (paresseux) ────────────────────────────────


def _guard_module() -> Any | None:
    try:
        from tools import skills_guard  # amont (dépendance épinglée)
    except Exception:  # noqa: BLE001 — absent (CI unitaire) : pas de scan possible
        return None
    return skills_guard


def _clean(text: Any) -> str:
    """Texte venu d'une brique : le nom du moteur ne doit jamais atteindre le modèle."""
    return re.sub(r"(?i)hermes", "nokker", str(text))


def _fallback_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """YAML minimal (clé: valeur sur une ligne, listes [a, b]) quand l'amont est absent."""
    content = content.removeprefix("﻿")
    m = re.search(r"\n---\s*\n", content[3:]) if content.startswith("---") else None
    if not m:
        return {}, content
    fm: dict[str, Any] = {}
    for line in content[3:m.start() + 3].splitlines():
        if ":" not in line or line[:1].isspace() or line.lstrip().startswith("#"):
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            fm[key.strip()] = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
        else:
            fm[key.strip()] = value.strip("'\"")
    return fm, content[m.end() + 3:]


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    try:
        from agent.skill_utils import parse_frontmatter as upstream_parse
    except Exception:  # noqa: BLE001
        return _fallback_frontmatter(content)
    try:
        return upstream_parse(content)
    except Exception:  # noqa: BLE001
        return _fallback_frontmatter(content)


def _platform_ok(fm: dict[str, Any]) -> bool:
    plats = fm.get("platforms")
    if not plats:
        return True
    plats = plats if isinstance(plats, list) else [plats]
    return any(sys.platform.startswith(_PLATFORMS.get(str(p).lower().strip(), str(p).lower().strip()))
               for p in plats)


# ─────────────────────────────── skills du dépôt ───────────────────────────────


def project_skills_sha256(skills_dir: Path) -> tuple[str | None, int, str | None]:
    """(empreinte, nombre de skills, raison du refus) de l'arbre `skills_dir`. L'empreinte couvre TOUT l'arbre
    (chemins relatifs et contenus ; un lien symbolique compte par sa cible, sans être suivi) : un fichier lié changé,
    ajouté ou retiré change l'empreinte. Arbre trop gros (`MAX_PROJECT_FILES`, `MAX_PROJECT_BYTES`) : refusé."""
    h = hashlib.sha256()
    files = size = 0
    for cur, dirs, names in os.walk(skills_dir):  # sans suivre les liens
        dirs.sort()
        rel_cur = Path(cur).relative_to(skills_dir)
        for n in sorted(names) + [d for d in dirs if os.path.islink(os.path.join(cur, d))]:
            full = os.path.join(cur, n)
            rel = (rel_cur / n).as_posix()
            files += 1
            if files > MAX_PROJECT_FILES:
                return None, 0, f"trop de fichiers (plus de {MAX_PROJECT_FILES})"
            if os.path.islink(full):
                h.update(b"L\0" + rel.encode() + b"\0" + os.readlink(full).encode() + b"\n")
                continue
            try:
                data = Path(full).read_bytes()
            except OSError as e:
                return None, 0, f"fichier illisible {rel} : {e.strerror or e}"
            size += len(data)
            if size > MAX_PROJECT_BYTES:
                return None, 0, f"trop volumineux (plus de {MAX_PROJECT_BYTES // (1024 * 1024)} Mio)"
            h.update(b"F\0" + rel.encode() + b"\0" + hashlib.sha256(data).hexdigest().encode() + b"\n")
    return h.hexdigest(), len(SkillToolSource._skill_dirs(skills_dir)), None


@dataclass
class _ProjectState:
    root: str  # racine du dépôt (chemin réel)
    dir: Path  # <root>/.nokker/skills
    project: str = "absent"  # absent | trusted | untrusted | invalid
    sha256: str | None = None  # empreinte lue (à l'ouverture, ou au dernier changement constaté)
    count: int = 0
    notice: str | None = None


@dataclass
class ProjectSkills:
    """Les skills du dépôt de chaque fil de session. `user_settings` : réglages de l'utilisateur (où la confiance
    s'écrit) ; `mode_of(thread_id)` : mode de permission du fil (plan → aucun skill du dépôt ; illisible → aucun) ;
    `emit(nom, données)` publie `skill.untrusted`."""

    user_settings: Path | None = None
    mode_of: Callable[[str], Any] | None = None
    emit: Callable[[str, dict[str, Any]], Any] | None = None
    _threads: dict[str, _ProjectState] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @staticmethod
    def repo_root(cwd: str | Path, mount_root: str | Path | None) -> str:
        """Racine git de `cwd` sans dépasser la racine montée (comme les fichiers d'instructions, lot X1), sinon cwd."""
        from nokker.context.project_memory import find_git_root

        found = find_git_root(cwd, mount_root or cwd)
        return os.path.realpath(found if found is not None else cwd)

    def _user(self) -> dict[str, Any]:
        from nokker.tools.hooks import read_settings

        try:
            return read_settings(self.user_settings)
        except ValueError:
            return {}

    def open_session(self, thread_id: str, cwd: str | Path, mount_root: str | Path | None = None) -> dict[str, Any]:
        """Lit l'arbre `.nokker/skills` du dépôt du fil et l'état de confiance ; rend `skills` de la réponse
        d'ouverture."""
        from nokker.tools.hooks import is_trusted

        root = self.repo_root(cwd, mount_root)
        st = _ProjectState(root=root, dir=Path(root, *PROJECT_SKILLS_DIR))
        parent = st.dir.parent
        if parent.is_symlink() or st.dir.is_symlink():
            st.project, st.notice = "invalid", ("skills du dépôt ignorés : .nokker/skills est un lien symbolique "
                                                "(il pourrait sortir du dépôt)")
        elif st.dir.is_dir():
            sha, count, why = project_skills_sha256(st.dir)
            if why:
                st.project, st.notice = "invalid", f"skills du dépôt ignorés (.nokker/skills) : {why}"
            elif count:
                st.sha256, st.count = sha, count
                if is_trusted(self._user(), root, sha, PROJECT_TRUST_KEY):  # type: ignore[arg-type]
                    st.project = "trusted"
                else:
                    st.project, st.notice = "untrusted", self._untrusted_notice(st, changed=False)
        with self._lock:
            self._threads[thread_id] = st
        if st.project == "untrusted":
            self._emit_untrusted(thread_id, st)
        return self.status(thread_id) or {}

    @staticmethod
    def _untrusted_notice(st: _ProjectState, changed: bool) -> str:
        what = ("Les skills de ce dépôt ont été modifiés depuis la confiance" if changed else
                f"Ce dépôt déclare {st.count} skill(s) dans .nokker/skills")
        return (f"{what} (empreinte {(st.sha256 or '')[:12]}…) : ils sont IGNORÉS tant que tu ne leur fais pas "
                "confiance. Relis-les, puis POST /v1/local/skills/trust {thread_id, sha256} (jeton local), ou "
                f"`python -m nokker.cli skills trust --sha256 {st.sha256}` dans le dossier. Un arbre modifié devra "
                "être de nouveau accepté.")

    def _emit_untrusted(self, thread_id: str, st: _ProjectState) -> None:
        if self.emit is None:
            return
        try:
            self.emit("skill.untrusted", {"thread_id": thread_id, "root": st.root, "sha256": st.sha256,
                                          "count": st.count, "notice": st.notice})
        except Exception:  # noqa: BLE001 — un événement manqué ne casse jamais un appel
            log.debug("emit skill.untrusted a levé", exc_info=True)

    def status(self, thread_id: str) -> dict[str, Any] | None:
        st = self._threads.get(thread_id)
        if st is None:
            return None
        return {"enabled": True, "project": st.project, "root": st.root, "sha256": st.sha256, "count": st.count,
                "notice": st.notice}

    def trust(self, thread_id: str, sha256: str) -> tuple[int, dict[str, Any]]:
        """Le propriétaire fait confiance aux skills du dépôt de CE fil, tels qu'il les a relus (`sha256` = celle
        rendue à l'ouverture ou par le dernier avis de changement)."""
        from nokker.tools.hooks import trust_project_hooks

        st = self._threads.get(thread_id)
        if st is None or st.project not in ("untrusted", "trusted"):
            return 404, {"error": {"message": "pas de skills de dépôt sur ce fil"}}
        if sha256 != st.sha256:
            return 409, {"error": {"message": "empreinte différente de celle lue : relis les skills du dépôt"}}
        if self.user_settings is None:
            return 503, {"error": {"message": "pas de réglages de l'utilisateur où écrire la confiance"}}
        trust_project_hooks(self.user_settings, st.root, sha256, key=PROJECT_TRUST_KEY)
        with self._lock:
            st.project, st.notice = "trusted", None
        return 200, {"thread_id": thread_id, "root": st.root, "sha256": sha256, "trusted": True}

    def close(self, thread_id: str) -> None:
        with self._lock:
            self._threads.pop(thread_id, None)

    def visible_dir(self, thread_id: str | None) -> Path | None:
        """Le dossier des skills du dépôt à charger pour CE fil, ou None : fil inconnu, pas de confiance, mode plan
        (ou illisible), ou arbre changé depuis la confiance (il redevient non fiable, avec un avis)."""
        if not thread_id:
            return None
        base = thread_id.split(SUBAGENT_MARK, 1)[0]
        st = self._threads.get(base)
        if st is None or st.project != "trusted":
            return None
        if self.mode_of is not None:
            from nokker.qa.permissions import Mode

            try:
                if self.mode_of(base) is Mode.PLAN:
                    return None
            except Exception:  # noqa: BLE001 — mode illisible : échec fermé
                return None
        if st.dir.is_symlink() or st.dir.parent.is_symlink() or not st.dir.is_dir():
            return None
        sha, count, why = project_skills_sha256(st.dir)
        if sha == st.sha256:
            return st.dir
        with self._lock:
            if st.project != "trusted":
                return None
            if why:
                st.project, st.sha256, st.notice = "invalid", None, f"skills du dépôt ignorés (.nokker/skills) : {why}"
            else:
                st.sha256, st.count = sha, count
                st.project, st.notice = "untrusted", self._untrusted_notice(st, changed=True)
        if st.project == "untrusted":
            self._emit_untrusted(base, st)
        return None


# ─────────────────────────────── catalogue ───────────────────────────────


@dataclass
class _Entry:
    name: str
    category: str
    description: str
    source: str  # project | user | builtin
    path: Path
    trusted: bool
    root: Path
    warning: str | None = None


@dataclass
class SkillToolSource:
    """`ToolSource` des compétences.

    roots : racines par précédence (projet > utilisateur > fournies). trusted_roots : racines exemptées du scan
    (compétences fournies). home : `$NOKKER_HOME`, défaut `~/.nokker` ; la racine utilisateur est `home/skills`
    (seule où l'on installe et retire). audit : reçoit un dict par install / refus / retrait / compétence masquée.
    project : skills des dépôts par fil (`ProjectSkills`), en tête de la précédence, jamais de confiance d'office."""

    roots: list[Path] = field(default_factory=list)
    trusted_roots: list[Path] = field(default_factory=list)
    home: Path | None = None
    audit: Callable[[dict[str, Any]], None] | None = None
    core: bool = False
    project: ProjectSkills | None = None
    _cache: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.home = Path(self.home or os.environ.get("NOKKER_HOME") or "~/.nokker").expanduser()
        os.environ.setdefault("HERMES_HOME", str(self.home))  # AVANT tout import de l'amont
        self.roots = [Path(r).expanduser() for r in self.roots] or [self.user_root]
        self.trusted_roots = [Path(r).expanduser() for r in self.trusted_roots]
        if self.audit is None:
            self.audit = lambda ev: log.info("%s", json.dumps(ev, ensure_ascii=False, default=str))

    @property
    def user_root(self) -> Path:
        return Path(self.home) / "skills"

    def specs(self) -> list[ToolSpec]:
        return [ToolSpec(name=n, description=d, parameters=p, effect=e, core=self.core, source="builtin:skills")
                for n, d, e, p in _SPECS]

    def _emit(self, **event: Any) -> None:
        try:
            self.audit(event)  # type: ignore[misc]
        except Exception:  # noqa: BLE001 — l'audit ne casse jamais un appel
            log.exception("audit en échec")

    # ── parcours ──
    def _label(self, root: Path) -> str:
        if self._same(root, self.user_root):
            return "user"
        if any(self._same(root, t) for t in self.trusted_roots):
            return "builtin"
        return "project"

    @staticmethod
    def _same(a: Path, b: Path) -> bool:
        return os.path.realpath(a) == os.path.realpath(b)

    def _is_trusted(self, root: Path) -> bool:
        return any(self._same(root, t) for t in self.trusted_roots)

    @staticmethod
    def _skill_dirs(root: Path) -> list[Path]:
        found: list[Path] = []
        if not root.is_dir():
            return found
        for cur, dirs, files in os.walk(root):  # sans suivre les liens : un lien ne fait pas entrer un skill
            has = "SKILL.md" in files
            dirs[:] = sorted(d for d in dirs if d not in _EXCLUDED_DIRS and not d.startswith(".")
                             and not (has and d in SUPPORT_DIRS))
            if has:
                found.append(Path(cur))
        return found

    @staticmethod
    def _signature(skill_dir: Path) -> str:
        h = hashlib.sha1()
        for cur, dirs, files in os.walk(skill_dir):
            dirs.sort()
            for f in sorted(files):
                try:
                    st = os.lstat(os.path.join(cur, f))
                except OSError:
                    continue
                h.update(f"{os.path.join(cur, f)}:{st.st_mtime_ns}:{st.st_size}".encode())
        return h.hexdigest()

    def _verdict(self, skill_dir: Path, sig: str) -> tuple[str, str]:
        """(verdict, résumé) — `unscannable` sans l'amont (donc masqué)."""
        key = ("verdict", str(skill_dir))
        hit = self._cache.get(key)
        if hit and hit[0] == sig:
            return hit[1]
        guard = _guard_module()
        if guard is None:
            out = ("unscannable", "")
        else:
            try:
                result, _ = guard.scan_skill_cached(skill_dir, source="community",
                                                    cache_dir=Path(self.home) / ".scan-cache")
                out = (str(result.verdict), _clean(getattr(result, "summary", "") or result.verdict))
            except Exception as e:  # noqa: BLE001 — échec fermé : on ne montre pas ce qu'on n'a pas pu juger
                log.warning("scan en échec pour %s : %s", skill_dir, e)
                out = ("unscannable", "")
        self._cache[key] = (sig, out)
        return out

    def _roots_for(self, thread_id: str | None) -> list[Path]:
        """Racines du fil : celle des skills de son dépôt si elle est visible maintenant (confiance, empreinte, mode),
        puis les racines fixes."""
        proj = self.project.visible_dir(thread_id) if self.project is not None else None
        return ([proj] if proj is not None else []) + list(self.roots)

    def _project_ok(self, entry: _Entry, thread_id: str | None) -> bool:
        """Un skill du dépôt lu : l'arbre a-t-il encore l'empreinte de confiance APRÈS la lecture ?"""
        if self.project is None or entry.source != "project":
            return True
        d = self.project.visible_dir(thread_id)
        return d is not None and self._same(d, entry.root)

    def _catalog(self, thread_id: str | None = None) -> tuple[list[_Entry], dict[str, list[_Entry]], dict[str, Any]]:
        """(entrées visibles dédoublonnées par précédence, collisions nom → entrées masquées, infos)."""
        roots = self._roots_for(thread_id)
        with self._lock:
            entries: list[_Entry] = []
            info = {"unscannable": 0, "dangerous": 0}
            for root in roots:
                trusted, label = self._is_trusted(root), self._label(root)
                for d in self._skill_dirs(root):
                    try:
                        text = (d / "SKILL.md").read_text(encoding="utf-8-sig", errors="replace")
                    except OSError:
                        continue
                    fm, _ = parse_frontmatter(text)
                    if not _platform_ok(fm):
                        continue
                    rel = d.relative_to(root).parts
                    entry = _Entry(name=str(fm.get("name") or d.name).strip(),
                                   category=rel[0] if len(rel) > 1 else "",
                                   description=" ".join(str(fm.get("description") or "").split()),
                                   source=label, path=d, trusted=trusted, root=root)
                    if not trusted:
                        verdict, summary = self._verdict(d, self._signature(d))
                        if verdict == "unscannable":
                            info["unscannable"] += 1
                            continue
                        if verdict == "dangerous":
                            info["dangerous"] += 1
                            self._emit(event="skill_hidden", name=entry.name, path=str(d), verdict=verdict,
                                       summary=summary)
                            continue
                        if verdict == "caution":
                            entry.warning = f"compétence à vérifier avant usage : {summary}"
                    entries.append(entry)
            visible: dict[str, _Entry] = {}
            shadowed: dict[str, list[_Entry]] = {}
            for e in entries:
                if e.name in visible:
                    shadowed.setdefault(e.name, []).append(e)
                else:
                    visible[e.name] = e
            return list(visible.values()), shadowed, info

    # ── appels ──
    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def out(payload: Any, error: bool = False) -> ToolResult:
            text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            return ToolResult(call_id="", name=name, content=text, is_error=error)

        if name not in SKILL_TOOLS:
            return out(f"outil inconnu : {name}", True)
        try:
            a = arguments or {}
            tid = getattr(ctx, "thread_id", None)
            if name == "skill_list":
                return out(self.list_skills(a.get("category"), tid))
            if name == "skill_view":
                payload, err = self.view(str(a["name"]), a.get("file"), tid)
                return out(payload, err)
            if name == "skill_install":
                payload, err = self.install(str(a["source"]), a.get("category"))
                return out(payload, err)
            payload, err = self.remove(str(a["name"]))
            return out(payload, err)
        except KeyError as e:
            return out(f"argument manquant : {e.args[0]}", True)
        except Exception as e:  # noqa: BLE001 — l'erreur revient au modèle, la boucle continue
            return out(_clean(f"{type(e).__name__}: {e}"), True)

    def list_skills(self, category: str | None = None, thread_id: str | None = None) -> dict[str, Any]:
        entries, _, info = self._catalog(thread_id)
        if category:
            entries = [e for e in entries if e.category == category]
        skills = []
        for e in sorted(entries, key=lambda e: (e.category, e.name)):
            item: dict[str, Any] = {"name": e.name, "category": e.category, "description": e.description,
                                    "source": e.source}
            if e.warning:
                item["warning"] = e.warning
            skills.append(item)
        result: dict[str, Any] = {"skills": skills}
        if info["unscannable"]:
            result["note"] = (f"{info['unscannable']} compétence(s) non fournie(s) masquée(s) : le scanner de "
                              "sécurité n'est pas disponible, elles ne sont pas affichées par prudence.")
        return result

    def _find(self, name: str, thread_id: str | None = None) -> tuple[_Entry | None, list[_Entry]]:
        entries, shadowed, _ = self._catalog(thread_id)
        name = name.strip()
        for e in entries:
            if e.name == name or (e.category and f"{e.category}/{e.name}" == name):
                return e, shadowed.get(e.name, [])
        return None, []

    @staticmethod
    def _linked_files(entry: _Entry) -> list[str]:
        base = os.path.realpath(entry.path)
        files: list[str] = []
        for sub in SUPPORT_DIRS:
            top = entry.path / sub
            if not top.is_dir():
                continue
            for cur, dirs, names in os.walk(top):
                dirs.sort()
                for n in sorted(names):
                    p = Path(cur) / n
                    real = os.path.realpath(p)
                    if not (real == base or real.startswith(base + os.sep)) or not os.path.isfile(real):
                        continue
                    files.append(p.relative_to(entry.path).as_posix())
                    if len(files) >= MAX_LINKED_FILES:
                        return files
        return files

    def view(self, name: str, file: str | None = None, thread_id: str | None = None) -> tuple[Any, bool]:
        entry, hidden = self._find(name, thread_id)
        if entry is None:
            return f"compétence introuvable : {name!r} (skill_list pour voir les noms)", True
        changed = (("REFUSÉ : les skills du dépôt ont changé pendant la lecture ; ils sont de nouveau ignorés "
                    "jusqu'à une nouvelle confiance de l'utilisateur"), True)
        if file:
            payload, err = self._read_linked(entry, str(file))
            return changed if not err and not self._project_ok(entry, thread_id) else (payload, err)
        text = (entry.path / "SKILL.md").read_text(encoding="utf-8-sig", errors="replace")
        if not self._project_ok(entry, thread_id):
            return changed
        _, body = parse_frontmatter(text)
        payload: dict[str, Any] = {"name": entry.name, "category": entry.category, "source": entry.source,
                                   "description": entry.description, "content": body.lstrip("\n"),
                                   "linked_files": self._linked_files(entry)}
        if entry.warning:
            payload["warning"] = entry.warning
        if hidden:
            payload["note"] = (f"plusieurs compétences portent ce nom ; celle de la racine « {entry.source} » "
                               f"l'emporte sur : {', '.join(sorted({h.source for h in hidden}))}.")
        return payload, False

    @staticmethod
    def _read_linked(entry: _Entry, file: str) -> tuple[Any, bool]:
        rel = file.replace("\\", "/")
        if rel.startswith("/") or ".." in rel.split("/") or "\x00" in rel:
            return f"REFUSÉ : chemin hors de la compétence ({file!r})", True
        base = os.path.realpath(entry.path)
        real = os.path.realpath(entry.path / rel)
        if not (real == base or real.startswith(base + os.sep)):
            return f"REFUSÉ : {file!r} sort du dossier de la compétence", True
        if not os.path.isfile(real):
            return f"fichier introuvable dans la compétence : {file!r}", True
        size = os.path.getsize(real)
        if size > MAX_FILE_BYTES:
            return f"fichier trop gros ({size} octets, max {MAX_FILE_BYTES})", True
        data = Path(real).read_bytes()
        if b"\x00" in data[:8192]:
            return f"fichier binaire refusé : {file!r}", True
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            return f"fichier non textuel refusé : {file!r}", True
        payload: dict[str, Any] = {"name": entry.name, "file": rel, "content": content}
        if entry.warning:
            payload["warning"] = entry.warning
        return payload, False

    # ── installation / retrait ──
    def install(self, source: str, category: str | None = None) -> tuple[Any, bool]:
        source = (source or "").strip()
        guard = _guard_module()
        if guard is None:
            self._emit(event="install_refused", source=source, reason="scanner indisponible")
            return ("installation impossible ici : le scanner de sécurité des compétences n'est pas disponible "
                    "(rien n'a été installé)", True)
        if source.startswith("github:"):
            ident, scan_source = source[len("github:"):].strip("/"), None
            if len(ident.split("/")) < 2 or ".." in ident.split("/"):
                return "source github invalide : github:<owner>/<repo>[/<chemin>]", True
            if ident.count("/") == 1:
                ident += "/"
            scan_source = ident.rstrip("/")
            kind = "github"
        elif source.startswith("https://"):
            ident, scan_source, kind = source, "url", "url"
        elif source.startswith("official/") and len(source.split("/")) >= 2 and ".." not in source.split("/"):
            ident, scan_source, kind = source, "official", "official"
        else:
            return "source non reconnue : github:<owner>/<repo>[/<chemin>], URL https, ou official/<famille>/<nom>", True
        try:
            from tools import skills_hub
            from tools.skills_hub_install import install_from_quarantine, quarantine_bundle

            if not self._same(Path(skills_hub._skills_dir()), self.user_root):
                return "installation refusée : le dossier d'état des compétences ne correspond pas à $NOKKER_HOME", True
            if kind == "github":
                from tools.skills_hub_github import GitHubAuth, GitHubSource
                src = GitHubSource(auth=GitHubAuth())
            elif kind == "url":
                from tools.skills_hub_sources import UrlSource
                src = UrlSource()
            else:
                from tools.skills_hub_github import GitHubAuth
                from tools.skills_hub_official import OptionalSkillSource
                src = OptionalSkillSource(auth=GitHubAuth())
            bundle = src.fetch(ident)
            if bundle is None:
                self._emit(event="install_refused", source=source, reason="introuvable")
                return f"compétence introuvable à cette source : {source}", True
            q = quarantine_bundle(bundle)
        except Exception as e:  # noqa: BLE001
            self._emit(event="install_refused", source=source, reason=_clean(e))
            return _clean(f"installation impossible : {type(e).__name__}: {e}"), True
        try:
            result, prov = guard.scan_skill_cached(q, source=scan_source, source_url=bundle.identifier,
                                                   cache_dir=Path(self.home) / ".scan-cache")
            allowed, reason = guard.should_allow_install(result)
            report = _clean(guard.format_scan_report(result))
            if allowed is not True:
                shutil.rmtree(q, ignore_errors=True)
                self._emit(event="install_refused", source=source, name=bundle.name, verdict=result.verdict,
                           trust=result.trust_level, reason=_clean(reason))
                return f"installation REFUSÉE par le scanner.\n{report}", True
            dest = install_from_quarantine(q, bundle.name, category or "", bundle, result, prov)
        except Exception as e:  # noqa: BLE001
            shutil.rmtree(q, ignore_errors=True)
            self._emit(event="install_refused", source=source, reason=_clean(e))
            return _clean(f"installation échouée : {type(e).__name__}: {e}"), True
        with self._lock:
            self._cache.clear()
        self._emit(event="installed", source=source, name=bundle.name, path=str(dest), verdict=result.verdict,
                   trust=result.trust_level)
        return {"installed": bundle.name, "path": str(dest), "verdict": result.verdict, "report": report}, False

    def remove(self, name: str) -> tuple[Any, bool]:
        entry, _ = self._find(name)
        user = self.user_root
        if entry is None or not self._same(entry.root, user):
            # une compétence fournie / de projet masquée par une compétence utilisateur homonyme reste retirable
            cands = [e for e in self._all_in(user) if e.name == name.strip()]
            entry = cands[0] if cands else None
        if entry is None or not self._same(entry.root, user):
            return (f"retrait refusé : {name!r} n'est pas une compétence installée par l'utilisateur "
                    f"(seule {user} est modifiable)"), True
        target = entry.path
        if target.is_symlink():
            return "retrait refusé : le dossier de la compétence est un lien symbolique", True
        real, base = os.path.realpath(target), os.path.realpath(user)
        if real == base or not real.startswith(base + os.sep):
            return "retrait refusé : chemin hors de la racine utilisateur", True
        shutil.rmtree(real)
        try:  # registre d'installation, au mieux
            from tools.skills_hub import HubLockFile
            HubLockFile().record_uninstall(entry.name)
        except Exception:  # noqa: BLE001
            pass
        with self._lock:
            self._cache.clear()
        self._emit(event="removed", name=entry.name, path=str(target))
        return {"removed": entry.name}, False

    def _all_in(self, root: Path) -> list[_Entry]:
        out = []
        for d in self._skill_dirs(root):
            try:
                fm, _ = parse_frontmatter((d / "SKILL.md").read_text(encoding="utf-8-sig", errors="replace"))
            except OSError:
                continue
            rel = d.relative_to(root).parts
            out.append(_Entry(str(fm.get("name") or d.name).strip(), rel[0] if len(rel) > 1 else "", "",
                              "user", d, False, root))
        return out

    # ── index pour le bloc du tour ──
    def index_block(self, limit_per_family: int = 4, desc_chars: int = 60, thread_id: str | None = None) -> str:
        """Une ligne par famille : `- famille : nom — description ; …`. Stable (trié), compact."""
        entries, _, _ = self._catalog(thread_id)
        if not entries:
            return ""
        fams: dict[str, list[_Entry]] = {}
        for e in entries:
            fams.setdefault(e.category or "général", []).append(e)
        lines = []
        for fam in sorted(fams):
            items = sorted(fams[fam], key=lambda e: e.name)
            shown = [f"{e.name} — {e.description[:desc_chars]}" if e.description else e.name
                     for e in items[:limit_per_family]]
            more = len(items) - limit_per_family
            lines.append(f"- {fam} : " + " ; ".join(shown) + (f" ; (+{more} autres)" if more > 0 else ""))
        return ("[Compétences — skill_view(\"<nom>\") charge le mode d'emploi ; skill_list(\"<famille>\") "
                "liste une famille]\n" + "\n".join(lines) + "\n[/Compétences]")


__all__ = ["PROJECT_TRUST_KEY", "SKILL_TOOLS", "ProjectSkills", "SkillToolSource", "parse_frontmatter",
           "project_skills_sha256"]
