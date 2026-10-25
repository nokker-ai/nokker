"""Skills du dépôt dans les sessions de code multi-utilisateur (docs/LOKKAL.md §7.28, même modèle que §7.21).

- Lus dans le dépôt cloné de LA session (`<racine de session>/.nokker/skills`, par le noyau sur le volume monté chez
  lui), jamais dans un autre dossier ; portée : le fil de la session et ses sous-agents `<fil>/sa-<id>`.
- Contenu non fiable : invisibles tant que le PROPRIÉTAIRE de la session n'a pas fait confiance à cet arbre (empreinte de
  `nokker/tools/skills.py::project_skills_sha256`), `POST /v1/code/sessions/{cs}/skills/trust {sha256}` ; confiance
  rangée chez l'utilisateur, `<state_dir>/<tenant>/<user>/settings.json` (`trusted_project_skills`, à côté de celle des
  hooks), jamais dans le dépôt. Empreinte recalculée à chaque lecture (un arbre réécrit par le modèle redevient non
  fiable), jamais en mode plan, puis le garde habituel (scan, échec fermé).
- Plus strict qu'en Lokkal : un arbre qui contient UN lien symbolique est refusé (`invalid`) — le noyau lit ces fichiers
  sur le volume partagé, un lien le ferait lire hors du dépôt (autre session, fichier du noyau).
- `skill_list` / `skill_view` portent les mêmes noms que les outils des packs du web (`WebPackTools`) : sur le fil d'une
  session, ceux du dépôt d'abord, puis ceux du compte (`fallback`) qu'ils ne masquent pas ; sur tout autre fil, ceux du
  compte tels quels.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import ToolResult
from nokker.tenancy.context import NoTenant
from nokker.tenancy.resolve import is_account
from nokker.tools.skills import _SPECS as _SKILL_SPECS
from nokker.tools.skills import ProjectSkills, SkillToolSource, _ProjectState

logger = logging.getLogger("nokker.code_sessions.skills")

SOURCE = "builtin:code-skills"
TOOLS = ("skill_list", "skill_view")
_TOOL_SPECS = tuple(ToolSpec(name=n, description=d, parameters=p, effect=e, core=False, source=SOURCE)
                    for n, d, e, p in _SKILL_SPECS if n in TOOLS)
_SYMLINK = "skills du dépôt ignorés : .nokker/skills contient un lien symbolique (il pourrait sortir du dépôt)"


def has_symlink(folder: Path) -> bool:
    for cur, dirs, files in os.walk(folder):  # sans suivre les liens
        if any(os.path.islink(os.path.join(cur, n)) for n in (*dirs, *files)):
            return True
    return False


@dataclasses.dataclass
class CodeProjectSkills(ProjectSkills):
    """`ProjectSkills` d'UNE session : réglages de son propriétaire, avis qui nomme la route de la session, chemins
    rendus tels que le bac à sable les voit (`mount`), arbre avec un lien symbolique refusé."""

    session_id: str = ""
    root_dir: str = ""  # racine de la session côté noyau
    mount: str = "/workspace"

    def open_session(self, thread_id: str, cwd: str | Path, mount_root: str | Path | None = None) -> dict[str, Any]:
        out = super().open_session(thread_id, cwd, mount_root)
        st = self._threads.get(thread_id)
        if st is not None and st.project in ("trusted", "untrusted") and has_symlink(st.dir):
            with self._lock:
                st.project, st.notice = "invalid", _SYMLINK
            return self.status(thread_id) or out
        return out

    def visible_dir(self, thread_id: str | None) -> Path | None:
        d = super().visible_dir(thread_id)
        if d is None or not has_symlink(d):
            return d
        st = self._threads.get(str(thread_id).split("/sa-", 1)[0])
        if st is not None:
            with self._lock:
                st.project, st.notice = "invalid", _SYMLINK
        return None

    def _untrusted_notice(self, st: _ProjectState, changed: bool) -> str:  # type: ignore[override]
        what = ("Les skills de ce dépôt ont été modifiés depuis la confiance" if changed else
                f"Ce dépôt déclare {st.count} skill(s) dans .nokker/skills")
        return (f"{what} (empreinte {(st.sha256 or '')[:12]}…) : ils sont IGNORÉS tant que tu ne leur fais pas "
                f"confiance. Relis-les, puis POST /v1/code/sessions/{self.session_id}/skills/trust {{sha256}}. Un "
                "arbre modifié devra être de nouveau accepté.")

    def _emit_untrusted(self, thread_id: str, st: _ProjectState) -> None:
        if self.emit is None:
            return
        try:
            self.emit("skill.untrusted", {"thread_id": thread_id, "session_id": self.session_id,
                                          "root": self._shown(st.root), "sha256": st.sha256, "count": st.count,
                                          "notice": st.notice})
        except Exception:  # noqa: BLE001 — un événement manqué ne casse jamais un appel
            logger.debug("emit skill.untrusted a levé", exc_info=True)

    def status(self, thread_id: str) -> dict[str, Any] | None:
        out = super().status(thread_id)
        if out is not None:
            out["root"] = self._shown(out.get("root"))
        return out

    def trust(self, thread_id: str, sha256: str) -> tuple[int, dict[str, Any]]:
        if self.visible_dir(thread_id) is None:  # relu juste avant : un lien ajouté depuis l'ouverture est refusé
            st = self._threads.get(thread_id)
            if st is not None and st.dir.is_dir() and has_symlink(st.dir):
                with self._lock:
                    st.project, st.notice = "invalid", _SYMLINK
        status, body = super().trust(thread_id, sha256)
        if status == 200:
            body["root"] = self._shown(body.get("root"))
        return status, body

    def _shown(self, root: Any) -> Any:
        """Chemin côté noyau → chemin vu dans le bac à sable : le client ne voit jamais l'arborescence du volume."""
        if not isinstance(root, str) or not self.root_dir:
            return root
        base = os.path.realpath(self.root_dir)
        if root == base:
            return self.mount
        if root.startswith(base + os.sep):
            return self.mount + "/" + os.path.relpath(root, base).replace(os.sep, "/")
        return self.mount


def make_project_skills(*, state_dir: str, tenant: str, user: str, session_id: str, root: str, mount: str,
                        mode_of: Callable[[str], Any] | None, emit: Callable[[str, dict[str, Any]], Any] | None
                        ) -> tuple[CodeProjectSkills, SkillToolSource]:
    """Les skills du dépôt d'une session et leur catalogue : AUCUNE autre racine (pas de skills « utilisateur » sur le
    disque du noyau : ceux du compte sont les packs du web) ; cache du scanner chez l'utilisateur (parti avec
    `nokker erase`). `HERMES_HOME` du processus n'est jamais changé ici."""
    user_dir = Path(state_dir) / tenant / user
    project = CodeProjectSkills(user_settings=user_dir / "settings.json", mode_of=mode_of, emit=emit,
                                session_id=session_id, root_dir=root, mount=mount)
    home = user_dir / "skills-scan"
    had = "HERMES_HOME" in os.environ
    catalog = SkillToolSource(home=home, roots=[home / "aucun"], project=project)
    if not had:
        os.environ.pop("HERMES_HOME", None)
    return project, catalog


class CodeSessionSkills:
    """`ToolSource` de `skill_list` / `skill_view` pour les comptes : sur le fil d'une session de code (ou d'un de ses
    sous-agents), du locataire courant et pour son propriétaire, les skills du dépôt puis ceux du compte ; ailleurs,
    ceux du compte seuls (`fallback()` : la source des packs du web, ou None)."""

    core = False

    def __init__(self, sessions: Any, fallback: Callable[[], Any] | None = None) -> None:
        self.sessions = sessions
        self.fallback = fallback

    def specs(self) -> list[ToolSpec]:
        return list(_TOOL_SPECS)

    def _fallback(self) -> Any | None:
        fb = self.fallback() if self.fallback is not None else None
        return fb if fb is not None and fb is not self else None

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def out(payload: Any, error: bool = False) -> ToolResult:
            text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            return ToolResult(call_id="", name=name, content=text, is_error=error)

        from nokker.code_sessions.service import NO_SESSION

        if name not in TOOLS:
            return out(f"outil inconnu : {name}", True)
        try:
            found = self.sessions.skills_for_thread(ctx.thread_id)
        except NoTenant:
            found = None
        fb = self._fallback()
        if found is None:
            return fb.call(name, arguments, ctx) if fb is not None else out(NO_SESSION, True)
        s, project, catalog = found
        if ctx.sender is not None and is_account(ctx.sender) and ctx.sender != s.user_id:
            return out("REFUSÉ : cette session de code appartient à un autre utilisateur", True)
        a = arguments or {}
        tid = ctx.thread_id
        try:
            if name == "skill_list":
                listed = catalog.list_skills(a.get("category"), tid) if project is not None else {"skills": []}
                if project is not None and listed["skills"] and project.visible_dir(tid) is None:
                    return out("REFUSÉ : les skills du dépôt ont changé pendant la lecture ; ils sont de nouveau "
                               "ignorés jusqu'à une nouvelle confiance", True)
                if fb is not None:
                    r = fb.call(name, arguments, ctx)
                    if not r.is_error:
                        mine = {x["name"] for x in listed["skills"]}
                        extra = json.loads(r.content or "{}")
                        listed["skills"] = [*listed["skills"], *[x for x in extra.get("skills") or []
                                                                 if x.get("name") not in mine]]
                        if extra.get("note"):
                            listed["note"] = " ".join(filter(None, (listed.get("note"), extra["note"])))
                return out(listed)
            if "name" not in a:
                return out("argument manquant : name", True)
            entry = catalog._find(str(a["name"]), tid)[0] if catalog is not None else None
            if entry is None:  # pas un skill du dépôt (visible) : ceux du compte
                return fb.call(name, arguments, ctx) if fb is not None else \
                    out(f"compétence introuvable : {a['name']!r} (skill_list pour voir les noms)", True)
            payload, err = catalog.view(str(a["name"]), a.get("file"), tid)
            return out(payload, err)
        except Exception as e:  # noqa: BLE001 — l'erreur revient au modèle, la boucle continue
            logger.warning("skills du dépôt de %s : %s", s.id, e, exc_info=True)
            return out(f"{type(e).__name__}: {e}", True)


__all__ = ["CodeProjectSkills", "CodeSessionSkills", "SOURCE", "has_symlink", "make_project_skills"]
