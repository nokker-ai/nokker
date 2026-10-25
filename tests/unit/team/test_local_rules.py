"""Règles d'un projet d'équipe appliquées sur la machine (lot X17) : elles ne font que restreindre, comme un
`.nokker/settings.json` de dépôt, et la dernière liste connue s'applique hors ligne."""
from __future__ import annotations

import json

import httpx

from nokker.core.turn import Decision, Effect, ToolCall
from nokker.local.team import STATE_KEY, TeamRules, restrict_session
from nokker.qa.permissions import LocalPermissions, Mode, Rule


def _call(name, **args):
    return ToolCall(id="c1", name=name, arguments=args)


def test_project_rules_never_widen():
    perms = LocalPermissions(mode=Mode.AUTO, allow=[Rule.parse("terminal(git push*)")])
    perms.restrict_with("t", {"mode": "auto", "allow": ["write_file(**)"], "ask": [], "deny": []})
    assert perms.mode_for("t") is Mode.AUTO  # un mode « auto » de projet n'élargit rien (et nokker.ai le refuse)
    perms.restrict_with("t", {"mode": "default", "deny": ["terminal(git push --force*)"], "ask": ["terminal(npm *)"]})
    assert perms.mode_for("t") is Mode.DEFAULT
    assert perms.decide(_call("terminal", command="git push --force origin"), Effect.WRITE, "t")[0] is Decision.DENY
    assert perms.decide(_call("terminal", command="npm publish"), Effect.WRITE, "t")[0] is Decision.HITL
    # l'allow de l'utilisateur reste à lui, hors de ce que le projet interdit
    assert perms.decide(_call("terminal", command="git push origin"), Effect.WRITE, "t")[0] is Decision.ALLOW
    # un autre fil n'est pas touché
    assert perms.mode_for("autre") is Mode.AUTO


def test_project_rules_stack_on_repo_rules_and_keep_the_strictest_mode(tmp_path):
    repo = tmp_path / ".nokker"
    repo.mkdir()
    (repo / "settings.json").write_text(json.dumps({"permissions": {"mode": "plan", "deny": ["write_file(.env)"]}}))
    perms = LocalPermissions(mode=Mode.AUTO)
    perms.restrict_for_thread("t", repo / "settings.json")
    perms.restrict_with("t", {"mode": "accept_edits", "deny": ["terminal(rm *)"]})
    assert perms.mode_for("t") is Mode.PLAN  # le plus strict des deux reste
    assert perms.decide(_call("write_file", path=".env"), Effect.WRITE, "t")[0] is Decision.DENY
    assert perms.decide(_call("terminal", command="rm -rf /"), Effect.DESTRUCTIVE, "t")[0] is Decision.DENY
    perms.restrict_with("t", {"deny": ["(((("]})  # illisible : ignorée, rien ne casse
    assert perms.mode_for("t") is Mode.PLAN


class FakeClient:
    def __init__(self, status=200, projects=None, fail=False):
        self.status, self.projects, self.fail, self.calls = status, projects or [], fail, 0

    def get(self, url, headers=None, timeout=None):
        self.calls += 1
        if self.fail:
            raise httpx.ConnectError("hors ligne")
        assert url.endswith("/v1/mirror/projects") and headers["Authorization"] == "Bearer mt"
        return httpx.Response(self.status, json={"projects": self.projects})


def _git_repo(tmp_path, url):
    root = tmp_path / "work"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text(f'[remote "origin"]\n\turl = {url}\n')
    return root


def test_session_in_a_declared_repo_gets_the_project_rules(tmp_path):
    proj = {"id": "pj_aaaaaaaaaaaa", "name": "P", "role": "member",
            "repos": [{"key": "github.com/acme/api", "memory_project": "p-x"}],
            "rules": {"mode": "default", "deny": ["terminal(git push --force*)"]}, "secret_names": [], "skills": [],
            "mirror": False}
    state, saved, now = {}, [], [1000.0]
    client = FakeClient(projects=[proj])
    team = TeamRules(base_url="https://app.nokker.ai", token=lambda: "mt", state=state, save=lambda: saved.append(1),
                     client=client, clock=lambda: now[0])
    root = _git_repo(tmp_path, "git@github.com:acme/api.git")
    perms = LocalPermissions(mode=Mode.AUTO)
    assert restrict_session(perms, team, "t1", str(root), str(tmp_path)) == ["pj_aaaaaaaaaaaa"]
    assert perms.mode_for("t1") is Mode.DEFAULT
    assert perms.decide(_call("terminal", command="git push --force"), Effect.WRITE, "t1")[0] is Decision.DENY
    # un autre dépôt : rien
    other = _git_repo(tmp_path / "o", "https://github.com/acme/web")
    assert restrict_session(perms, team, "t2", str(other), str(tmp_path / "o")) == []
    assert perms.mode_for("t2") is Mode.AUTO
    assert client.calls == 1  # cache de 5 min
    # hors ligne après expiration du cache : la dernière liste s'applique encore
    now[0] += 600
    client.fail = True
    assert restrict_session(perms, team, "t3", str(root), str(tmp_path)) == ["pj_aaaaaaaaaaaa"]
    assert state[STATE_KEY]["projects"][0]["id"] == "pj_aaaaaaaaaaaa"
    # machine déliée (401) : plus aucun projet
    client.fail, client.status = False, 401
    now[0] += 600
    assert restrict_session(perms, team, "t4", str(root), str(tmp_path)) == []


def test_no_token_or_no_remote_applies_nothing(tmp_path):
    team = TeamRules(base_url="https://app.nokker.ai", token=lambda: "", state={}, save=lambda: None,
                     client=FakeClient(fail=True))
    perms = LocalPermissions(mode=Mode.AUTO)
    (tmp_path / "plain").mkdir()
    assert restrict_session(perms, team, "t", str(tmp_path / "plain"), str(tmp_path)) == []
    assert perms.mode_for("t") is Mode.AUTO
