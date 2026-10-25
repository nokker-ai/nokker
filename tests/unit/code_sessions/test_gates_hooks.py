"""Portes et hooks d'une session de code multi-utilisateur : les mêmes qu'en Lokkal (mode du fil, restrictions du dépôt
et des projets d'équipe, cartes, hooks du dépôt après confiance), seulement sur les fils de session ; tout autre fil
garde les portes du noyau."""
from __future__ import annotations

import json
import os

import pytest

from nokker.code_sessions.config import CodeConfig
from nokker.code_sessions.service import CodeError, CodeSessions
from nokker.code_sessions.store import MemoryCodeStore
from nokker.core.ports import ToolContext
from nokker.core.turn import Decision, Gate, ToolCall, Turn, Verdict
from nokker.tenancy.context import tenant_scope
from nokker.core.turn import InputCheck
from tests.unit.code_sessions.conftest import code_block

TA = "tn_aaaaaaaaaaaa"
ANA, BEN = "us_aaaaaaaaaaa1", "us_aaaaaaaaaaa2"
REPO = "https://git.example.org/team/app.git"


class _Inv:
    def check_action(self, call, spec, turn):
        return Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="inv")


class Inner:
    code_session = None

    def __init__(self) -> None:
        self.invariants = _Inv()
        self.seen: list[str] = []

    def action(self, call, spec, turn):
        self.seen.append(call.name)
        return [Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="inner", strength="strong")]

    def switch(self, reply, turn, tier):
        return []


def _settings(block):
    def cloner(url, dest, token, **_):
        os.makedirs(os.path.join(dest, ".nokker"))
        with open(os.path.join(dest, ".nokker", "settings.json"), "w") as f:
            json.dump(block, f)
    return cloner


@pytest.fixture
def make(cluster, fake_upstream):
    made = []

    def _make(cloner=None, team_rules=None, **over):
        cfg = CodeConfig.from_config(code_block(cluster.workspace_root, **over), lokkal=False)
        s = CodeSessions(cfg=cfg, store=MemoryCodeStore(), cloner=cloner or (lambda *a, **k: None),
                         team_rules=team_rules)
        made.append(s)
        return s
    yield _make
    for s in made:
        s.close_all()


def _decide(gates, svc, thread, name, args):
    spec = svc.tools().spec(name) if hasattr(svc.tools(), "spec") else next(
        (s for s in svc.tools().specs() if s.name == name), None)
    turn = Turn(thread_id=thread, index=1, input=InputCheck(text=""), channel="api", sender=ANA)
    with tenant_scope(TA):
        return gates.action(ToolCall(id="c1", name=name, arguments=args), spec, turn)


def _session(svc, body=None, user=ANA):
    with tenant_scope(TA):
        s = svc.create(user, body or {})
    return s, f"{TA}:{s['id']}"


def test_mode_of_the_thread(make):
    svc = make()
    inner = Inner()
    gates = svc.gates(inner)
    _, t_edit = _session(svc)
    _, t_def = _session(svc, {"mode": "default"})
    _, t_plan = _session(svc, {"mode": "plan"})
    assert _decide(gates, svc, t_edit, "write_file", {"path": "a.py", "content": ""})[0].decision is Decision.ALLOW
    v = _decide(gates, svc, t_def, "write_file", {"path": "a.py", "content": ""})[0]
    assert v.decision is Decision.HITL and v.rule == "permissions:default"
    assert _decide(gates, svc, t_plan, "write_file", {"path": "a.py", "content": ""})[0].decision is Decision.DENY
    assert _decide(gates, svc, t_plan, "read_file", {"path": "a.py"})[0].decision is Decision.ALLOW
    # destructif : les invariants du noyau décident (carte forte), comme en Lokkal
    v = _decide(gates, svc, t_edit, "terminal", {"command": "python3 -m pytest"})[0]
    assert v.rule == "inner" and inner.seen == ["terminal"]
    # un fil hors session : les portes du noyau seules
    assert _decide(gates, svc, f"{TA}:th_plain", "write_file", {"path": "a", "content": ""})[0].rule == "inner"
    # domaine consultatif dans une session de code seulement
    with tenant_scope(TA):
        assert inner.code_session(t_edit) is True and inner.code_session(f"{TA}:th_plain") is False


def test_repo_and_team_rules_only_restrict(make):
    team_calls = []

    def team_rules(account, repo_url):
        team_calls.append((account, repo_url))
        return [{"deny": ["write_file(secrets/**)"], "mode": "default"}]

    svc = make(cloner=_settings({"permissions": {"deny": ["terminal(git push*)"], "allow": ["terminal(*)"],
                                                 "mode": "auto"}}), team_rules=team_rules)
    gates = svc.gates(Inner())
    _, t = _session(svc, {"repo": {"url": REPO}})
    assert team_calls == [(ANA, REPO)]
    assert _decide(gates, svc, t, "terminal", {"command": "git push origin main"})[0].decision is Decision.DENY
    assert _decide(gates, svc, t, "write_file", {"path": "secrets/k", "content": ""})[0].decision is Decision.DENY
    # mode « default » de l'équipe (plus strict que accept_edits) ; l'« auto » et l'« allow » du dépôt ignorés
    assert _decide(gates, svc, t, "write_file", {"path": "a.py", "content": ""})[0].decision is Decision.HITL
    assert _decide(gates, svc, t, "terminal", {"command": "ls"})[0].decision is Decision.HITL


def test_symlinked_repo_settings_are_ignored(make, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"permissions": {"deny": ["read_file"]}}))

    def cloner(url, dest, token, **_):
        os.makedirs(os.path.join(dest, ".nokker"))
        os.symlink(outside, os.path.join(dest, ".nokker", "settings.json"))

    svc = make(cloner=cloner)
    gates = svc.gates(Inner())
    _, t = _session(svc, {"repo": {"url": REPO}})
    assert _decide(gates, svc, t, "read_file", {"path": "a"})[0].decision is Decision.ALLOW


def test_repo_hooks_run_in_the_sandbox_after_trust(make, cluster):
    hooks = {"hooks": {"pre_tool": [{"match": "write_file", "command": "./check.sh"}]}}
    svc = make(cloner=_settings(hooks), hooks=True)
    s, t = _session(svc, {"repo": {"url": REPO}})
    assert s["hooks"]["project"] == "untrusted" and s["hooks"]["sha256"]
    tools = svc.wrap_hooks(svc.tools())
    ctx = ToolContext(thread_id=t, turn_index=1, channel="api", sender=ANA)
    with tenant_scope(TA):
        assert not tools.call("write_file", {"path": "a.txt", "content": "1"}, ctx).is_error
    assert not any("check.sh" in c for p in cluster.pods for c in p.commands)  # pas de confiance : ignoré
    with tenant_scope(TA):
        with pytest.raises(CodeError) as e:
            svc.trust_hooks(BEN, s["id"], s["hooks"]["sha256"])  # pas sa session
        assert e.value.status == 404
        with pytest.raises(CodeError) as e:
            svc.trust_hooks(ANA, s["id"], "0" * 64)
        assert e.value.status == 409
        assert svc.trust_hooks(ANA, s["id"], s["hooks"]["sha256"])["trusted"] is True
        assert not tools.call("write_file", {"path": "b.txt", "content": "2"}, ctx).is_error
    assert any("check.sh" in c for c in cluster.pods[-1].commands)
    # la confiance est rangée chez l'utilisateur, hors de toute racine de session
    trust_file = os.path.join(svc.cfg.state_dir, TA, ANA, "settings.json")
    assert os.path.isfile(trust_file) and not trust_file.startswith(os.path.join(cluster.workspace_root, TA))


def test_hooks_flag_off_reads_nothing(make):
    svc = make(cloner=_settings({"hooks": {"pre_tool": [{"command": "x"}]}}))
    s, _ = _session(svc, {"repo": {"url": REPO}})
    assert "hooks" not in s
