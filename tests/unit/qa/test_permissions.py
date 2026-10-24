"""Permissions locales de Nokker Lokkal (nokker/qa/permissions.py, L1) : modes, règles, ce qu'un dépôt peut dire, et
leur place devant les portes du noyau (le juge n'est plus appelé sur un appel couvert)."""
from __future__ import annotations

import json

import pytest

from nokker.core.ports import ToolSpec
from nokker.core.turn import Decision, Effect, Gate, ToolCall, Verdict
from nokker.qa.permissions import LocalPermissions, Mode, PermissionGates, Rule


def call(name, **a):
    return ToolCall(id=f"c-{name}", name=name, arguments=a)


@pytest.mark.parametrize("mode,name,effect,expected", [
    (Mode.DEFAULT, "read_file", Effect.READ, Decision.ALLOW),
    (Mode.DEFAULT, "patch", Effect.WRITE, Decision.HITL),
    (Mode.DEFAULT, "terminal", Effect.WRITE, Decision.HITL),
    (Mode.ACCEPT_EDITS, "patch", Effect.WRITE, Decision.ALLOW),
    (Mode.ACCEPT_EDITS, "write_file", Effect.WRITE, Decision.ALLOW),
    (Mode.ACCEPT_EDITS, "terminal", Effect.WRITE, Decision.HITL),
    (Mode.AUTO, "terminal", Effect.WRITE, Decision.ALLOW),
    (Mode.AUTO, "terminal", Effect.DESTRUCTIVE, None),  # les invariants décident (carte forte)
    (Mode.PLAN, "read_file", Effect.READ, Decision.ALLOW),
    (Mode.PLAN, "patch", Effect.WRITE, Decision.DENY),
    (Mode.PLAN, "terminal", Effect.DESTRUCTIVE, Decision.DENY),
])
def test_modes(mode, name, effect, expected):
    assert LocalPermissions(mode=mode).decide(call(name, path="a.py", command="x"), effect)[0] is expected


def test_rules_order_deny_then_ask_then_allow():
    perms = LocalPermissions(mode=Mode.AUTO, allow=[Rule.parse("terminal(git push*)")],
                             ask=[Rule.parse("terminal(npm publish*)")], deny=[Rule.parse("terminal(git push --force*)")])
    assert perms.decide(call("terminal", command="git push --force origin"), Effect.DESTRUCTIVE)[0] is Decision.DENY
    assert perms.decide(call("terminal", command="git push origin main"), Effect.DESTRUCTIVE)[0] is Decision.ALLOW
    assert perms.decide(call("terminal", command="npm publish"), Effect.WRITE)[0] is Decision.HITL


def test_path_rules_and_dotdot():
    perms = LocalPermissions(mode=Mode.DEFAULT, allow=[Rule.parse("write_file(src/**)")])
    assert perms.decide(call("write_file", path="src/a/b.py"), Effect.WRITE)[0] is Decision.ALLOW
    assert perms.decide(call("write_file", path="./src/b.py"), Effect.WRITE)[0] is Decision.ALLOW
    assert perms.decide(call("write_file", path="./../src/b.py"), Effect.WRITE)[0] is Decision.HITL
    v4a = "*** Begin Patch\n*** Update File: src/a.py\n*** Add File: docs/x.md\n*** End Patch"
    perms = LocalPermissions(mode=Mode.DEFAULT, allow=[Rule.parse("patch(src/**)")])
    assert perms.decide(call("patch", mode="patch", patch=v4a), Effect.WRITE)[0] is Decision.HITL  # docs/ non couvert


def test_bad_rule_is_refused():
    with pytest.raises(ValueError):
        Rule.parse("terminal(")


def test_project_settings_can_only_restrict(tmp_path):
    user = tmp_path / "user.json"
    proj = tmp_path / "proj.json"
    user.write_text(json.dumps({"permissions": {"mode": "auto", "allow": ["terminal(make*)"]}}))
    proj.write_text(json.dumps({"permissions": {"mode": "accept_edits", "allow": ["terminal(curl*)"],
                                                "deny": ["terminal(rm*)"]}}))
    perms = LocalPermissions.load(user_settings=user, project_settings=proj)
    assert perms.mode is Mode.ACCEPT_EDITS  # plus strict que auto : retenu
    assert [r.pattern for r in perms.allow] == ["make*"]  # l'allow du dépôt est ignoré
    assert [r.pattern for r in perms.deny] == ["rm*"]
    proj.write_text(json.dumps({"permissions": {"mode": "auto"}}))
    user.write_text(json.dumps({"permissions": {"mode": "plan"}}))
    assert LocalPermissions.load(user_settings=user, project_settings=proj).mode is Mode.PLAN  # jamais plus large


class Inv:
    def check_action(self, c, spec, turn):
        eff = spec.effect if spec else Effect.WRITE
        d = {Effect.SEND_BULK: Decision.DENY, Effect.DESTRUCTIVE: Decision.HITL}.get(eff, Decision.ALLOW)
        return Verdict(gate=Gate.ACTION, decision=d, layer=1, rule=f"effect:{eff.value}")


class Inner:
    def __init__(self):
        self.invariants, self.judged = Inv(), []

    def action(self, c, spec, turn):
        self.judged.append(c.name)
        return [self.invariants.check_action(c, spec, turn), Verdict(gate=Gate.ACTION, decision=Decision.ALLOW,
                                                                      layer=2, rule="judge")]

    def switch(self, reply, turn, tier):
        return []


def test_gates_skip_the_judge_on_covered_calls_and_keep_invariants():
    inner = Inner()
    gates = PermissionGates(inner=inner, permissions=LocalPermissions(mode=Mode.AUTO))
    v = gates.action(call("patch", path="a.py"), ToolSpec("patch", "p", {}, Effect.WRITE), None)
    assert [x.decision for x in v] == [Decision.ALLOW] and inner.judged == []  # pas de juge
    v = gates.action(call("terminal", command="rm -rf build"), ToolSpec("terminal", "t", {}, Effect.DESTRUCTIVE), None)
    assert v[0].decision is Decision.HITL and inner.judged == ["terminal"]  # le destructif suit les portes du noyau
    bulk = PermissionGates(inner=inner, permissions=LocalPermissions(mode=Mode.AUTO, allow=[Rule.parse("mailer")]))
    v = bulk.action(call("mailer"), ToolSpec("mailer", "m", {}, Effect.SEND_BULK), None)
    assert v[0].decision is Decision.DENY  # une règle allow n'ouvre pas l'envoi de masse
    plan = PermissionGates(inner=inner, permissions=LocalPermissions(mode=Mode.PLAN))
    v = plan.action(call("patch", path="a.py"), ToolSpec("patch", "p", {}, Effect.WRITE), None)
    assert v[0].decision is Decision.DENY and v[0].rule == "permissions:plan"


def test_hardline_is_denied_without_a_card(monkeypatch):
    from nokker.tools import code

    monkeypatch.setattr(code, "hardline_reason", lambda c: "efface la racine" if c == "rm -rf /" else None)
    gates = PermissionGates(inner=Inner(), permissions=LocalPermissions(mode=Mode.AUTO, allow=[Rule.parse("terminal")]))
    v = gates.action(call("terminal", command="rm -rf /"), ToolSpec("terminal", "t", {}, Effect.DESTRUCTIVE), None)
    assert v[0].decision is Decision.DENY and v[0].rule == "permissions:hardline"  # même une règle allow n'y fait rien
