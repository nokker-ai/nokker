"""Projets d'équipe vus par une machine liée (lot X17, docs/LOKKAL.md §7.15) : drapeau `local.team`, éteint par
défaut. Le pendant machine de `GET /v1/mirror/projects` (nokker/team/http.py).

`TeamRules` garde la dernière liste reçue (état local `team_projects`, persisté) et la relit au plus toutes les
`max_age_s` à l'ouverture d'une session. À l'ouverture d'une session dans un dépôt dont la clé (mémoire de projet X1,
`hôte/chemin`) est déclarée par un projet, les règles de ce projet RESTREIGNENT le fil (`restrict_with`, comme un
`.nokker/settings.json` de dépôt). nokker.ai injoignable : la dernière liste connue s'applique (un projet ne perd pas
ses restrictions parce que la machine est hors ligne) ; aucune liste connue : rien de plus strict que l'utilisateur.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("nokker.local.team")
STATE_KEY = "team_projects"


@dataclass
class TeamRules:
    base_url: str  # https://app.nokker.ai
    token: Callable[[], str]  # jeton de machine
    state: dict[str, Any]  # setup.state
    save: Callable[[], None]
    max_age_s: float = 300.0
    timeout_s: float = 3.0
    client: Any = None
    clock: Callable[[], float] = field(default=time.time)
    last_error: Exception | None = field(default=None, init=False, repr=False)  # dernière relecture ratée (réseau)

    def projects(self) -> list[dict[str, Any]]:
        cached = self.state.get(STATE_KEY) or {}
        if self.clock() - float(cached.get("at") or 0) >= self.max_age_s:
            self.refresh()
            cached = self.state.get(STATE_KEY) or {}
        return list(cached.get("projects") or [])

    def refresh(self) -> bool:
        token = self.token()
        if not token:
            return False
        try:
            c = self.client or httpx
            r = c.get(self.base_url.rstrip("/") + "/v1/mirror/projects",
                      headers={"Authorization": f"Bearer {token}"}, timeout=self.timeout_s)
        except Exception as e:  # noqa: BLE001 — hors ligne : la dernière liste connue reste
            logger.info("projets d'équipe : nokker.ai injoignable (%s), dernière liste gardée", type(e).__name__)
            self.last_error = e
            return False
        self.last_error = None
        if r.status_code in (401, 404):  # machine déliée, ou équipe éteinte chez nokker.ai : plus aucun projet
            self.state[STATE_KEY] = {"at": self.clock(), "projects": []}
            self.save()
            return True
        if r.status_code != 200:
            return False
        try:
            projects = [p for p in (r.json().get("projects") or []) if isinstance(p, dict)]
        except ValueError:
            return False
        keep = ("id", "name", "role", "repos", "rules", "secret_names", "skills", "mirror", "can_push_notes")
        self.state[STATE_KEY] = {"at": self.clock(), "projects": [{k: p.get(k) for k in keep} for p in projects]}
        self.save()
        return True

    def for_repo(self, repo_key: str) -> list[dict[str, Any]]:
        """Projets qui déclarent ce dépôt (clé `hôte/chemin`, sans le préfixe `remote:`)."""
        return [p for p in self.projects()
                if any(isinstance(r, dict) and r.get("key") == repo_key for r in (p.get("repos") or []))]


def restrict_session(perms: Any, team: TeamRules, thread_id: str, cwd: str, mount_root: str) -> list[str]:
    """Applique au fil les règles des projets qui déclarent le dépôt de `cwd` ; rend les ids des projets appliqués."""
    from nokker.context.project_memory import project_key

    try:
        key, _, _ = project_key(cwd, mount_root)
    except OSError:
        return []
    if not key.startswith("remote:"):
        return []  # sans remote, pas de clé partageable avec les autres membres
    applied = []
    for p in team.for_repo(key[len("remote:"):]):
        perms.restrict_with(thread_id, p.get("rules") or {})
        applied.append(str(p.get("id")))
    return applied


__all__ = ["STATE_KEY", "TeamRules", "restrict_session"]
