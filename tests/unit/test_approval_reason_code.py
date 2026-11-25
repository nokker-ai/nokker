"""`reason_code` stable sur la carte d'approbation, à côté du texte du motif (besoin de la CLI Lokkal : ne plus
reconnaître le texte exact). Chaque motif des permissions locales a son code ; les textes restent ceux d'avant."""
from __future__ import annotations

import pytest

from nokker.api.server import approval_to_json
from nokker.approvals.memory import InMemoryApprovals
from nokker.core.ports import ToolSpec
from nokker.core.turn import Decision, Effect, Gate, InputCheck, ToolCall, Turn, Verdict
from nokker.qa import permissions as P
from nokker.qa.permissions import LocalPermissions, Mode, PermissionGates, Rule


class _Inner:
    class invariants:
        @staticmethod
        def check_action(call, spec, turn):
            return Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="ok")

    def action(self, call, spec, turn):
        return [Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="effect:destructive", reason="sans GO",
                        strength="strong")]


def _turn(channel="api"):
    return Turn(thread_id="th", index=0, input=InputCheck(text="x"), channel=channel)


def _run(perms, call, spec=None, turn=None, **gates_kw):
    gates = PermissionGates(inner=_Inner(), permissions=perms, **gates_kw)
    return gates.action(call, spec, turn or _turn())[0]


WRITE = ToolCall(id="1", name="terminal", arguments={"command": "make"})
EDIT = ToolCall(id="2", name="write_file", arguments={"path": "a.py"})


def test_default_mode_write_card():
    v = _run(LocalPermissions(mode=Mode.DEFAULT), WRITE)
    assert v.decision is Decision.HITL and v.code == "default_write"
    assert v.reason == "mode default : écriture demandée" and v.rule == "permissions:default"


def test_accept_edits_command_card():
    v = _run(LocalPermissions(mode=Mode.ACCEPT_EDITS), WRITE)
    assert v.decision is Decision.HITL and v.code == "accept_edits_write"
    assert v.reason == "mode accept_edits : écriture demandée"


def test_accept_edits_file_edit_is_allowed_without_code():
    v = _run(LocalPermissions(mode=Mode.ACCEPT_EDITS), EDIT)
    assert v.decision is Decision.ALLOW and v.code is None and v.reason == "mode accept_edits : écriture dans le dossier"


def test_ask_rule_card():
    v = _run(LocalPermissions(mode=Mode.AUTO, ask=[Rule.parse("terminal(make*)")]), WRITE)
    assert v.decision is Decision.HITL and v.code == "ask_rule" and v.reason == "règle ask terminal(make*)"


def test_project_ask_rule_card():
    perms = LocalPermissions(mode=Mode.AUTO)
    perms.thread_project["th"] = (None, [Rule.parse("terminal")], [])
    v = _run(perms, WRITE)
    assert v.code == "ask_rule" and v.reason == "règle ask terminal()"


def test_desktop_write_card():
    v = _run(LocalPermissions(mode=Mode.AUTO), ToolCall(id="3", name="desktop_act", arguments={}))
    assert v.decision is Decision.HITL and v.code == "desktop_write"
    assert v.reason == "mode auto : écriture sur le bureau demandée"


def test_relayed_connector_card():
    spec = ToolSpec(name="crm_update", description="d", parameters={}, effect=Effect.WRITE, locality="cloud")
    v = _run(LocalPermissions(mode=Mode.AUTO), ToolCall(id="4", name="crm_update", arguments={}), spec)
    assert v.code == "relayed_connector" and v.rule == P.RELAY_CARD_RULE
    assert v.reason == "outil d'un compte nokker.ai (connecteur relayé) : chaque appel à effet passe par une carte"


def test_local_connector_strong_card():
    spec = ToolSpec(name="mail_send", description="d", parameters={}, effect=Effect.SEND_EXTERNAL, locality="local",
                    source="connector:mail")
    v = _run(LocalPermissions(mode=Mode.AUTO), ToolCall(id="5", name="mail_send", arguments={}), spec)
    assert v.code == "local_connector_strong" and v.rule == P.LOCAL_STRONG_RULE and v.strength == "strong"
    assert v.reason == ("boîte mail de la machine : un envoi ou une mise à la corbeille passe par une carte forte")


def test_relayed_strong_card():
    spec = ToolSpec(name="rm_all", description="d", parameters={}, effect=Effect.DESTRUCTIVE)
    v = _run(LocalPermissions(mode=Mode.AUTO), ToolCall(id="6", name="rm_all", arguments={}), spec, _turn("relay"))
    assert v.code == "relayed_strong" and v.rule == P.RELAYED_STRONG_RULE
    assert v.reason == "action demandée depuis le web : carte forte à signer sur ton téléphone"


def test_go_sandbox_card():
    spec = ToolSpec(name="ssh_run", description="d", parameters={}, effect=Effect.DESTRUCTIVE)
    v = _run(LocalPermissions(mode=Mode.AUTO), ToolCall(id="7", name="ssh_run", arguments={"command": "x"}), spec,
             go_sandbox=lambda _tid: True)
    assert v.code == "go_sandbox_only" and v.rule == P.GO_SANDBOX_RULE
    assert v.reason == "action destructive hors du bac à sable d'une session de code : un GO écrit ne suffit pas"


def test_refusal_codes_unchanged():
    assert _run(LocalPermissions(mode=Mode.PLAN), WRITE).code == "plan_mode"
    assert _run(LocalPermissions(mode=Mode.AUTO, deny=[Rule.parse("terminal")]), WRITE).code == "deny_rule"
    assert _run(LocalPermissions(mode=Mode.AUTO), ToolCall(id="8", name="terminal", arguments={"command": "x"}),
                session_tools=frozenset({"terminal"}), code_session=lambda _t: False).code == "no_code_session"


def test_decision_without_own_card_has_no_code():
    # destructif en mode auto : les invariants décident (carte `effect:destructive`), pas de code des permissions
    spec = ToolSpec(name="rm_all", description="d", parameters={}, effect=Effect.DESTRUCTIVE)
    v = _run(LocalPermissions(mode=Mode.AUTO), ToolCall(id="9", name="rm_all", arguments={}), spec)
    assert v.rule == "effect:destructive" and v.code is None


def test_card_carries_code_and_api_exposes_it():
    backend = InMemoryApprovals()
    v = _run(LocalPermissions(mode=Mode.ACCEPT_EDITS), WRITE)
    card = backend.request(_turn(), WRITE, v, None)
    assert card.reason_code == "accept_edits_write"
    j = approval_to_json(backend.get(card.id))
    assert j["reason_code"] == "accept_edits_write" and j["reason"] == "mode accept_edits : écriture demandée"


def test_card_without_known_reason_exposes_null():
    backend = InMemoryApprovals()
    v = Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="effect:destructive", reason="sans GO",
                strength="strong")
    card = backend.request(_turn(), WRITE, v, None)
    j = approval_to_json(card)
    assert "reason_code" in j and j["reason_code"] is None


@pytest.mark.parametrize("code", [P.ASK_MATCHED, P.DEFAULT_WRITE, P.ACCEPT_EDITS_WRITE, P.DESKTOP_WRITE,
                                  P.RELAYED_CONNECTOR, P.LOCAL_CONNECTOR_STRONG, P.RELAYED_STRONG, P.GO_SANDBOX_ONLY])
def test_codes_are_snake_case_ascii(code):
    assert code.isascii() and code == code.lower() and code.replace("_", "").isalpha()


def test_pg_encoding_carries_code_in_its_own_column_not_in_arguments():
    from nokker.approvals import pg

    backend = InMemoryApprovals()
    card = backend.request(_turn(), WRITE, _run(LocalPermissions(mode=Mode.DEFAULT), WRITE), None)
    enc = pg._enc_approval(card)
    assert enc["reason_code"] == "default_write" and enc["arguments"] == WRITE.arguments
    row = (card.id, card.thread_id, card.turn_index, card.channel, card.tool_name, dict(enc["arguments"]),
           card.args_sha256, card.strength, card.rule, card.reason, card.advice, card.status, card.created_at,
           card.expires_at, None, None, None, None, None, card.call_id, None, enc["reason_code"], None, enc["target"])
    back = pg._row_to_approval(row)
    assert back.reason_code == "default_write" and back.arguments == WRITE.arguments
    legacy = pg._row_to_approval(row[:-3] + (None, None, None))
    assert legacy.reason_code is None and legacy.arguments == WRITE.arguments


def test_migration_087_is_a_nullable_idempotent_column():
    from nokker.store.pg import migration_files

    f = next(p for p in migration_files() if p.name == "087_approval_reason_code.sql")
    sql = f.read_text(encoding="utf-8")
    stmts = [ln for ln in sql.splitlines() if not ln.lstrip().startswith("--") and ln.strip()]
    assert stmts == ["ALTER TABLE approvals ADD COLUMN IF NOT EXISTS reason_code text;"]
