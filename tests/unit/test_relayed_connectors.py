"""Connecteurs relayés (lot X20, docs/LOKKAL.md §6.7 et §7.12) : un connecteur posé chez nokker.ai sert dans une
session Lokkal, son identifiant ne quitte jamais nokker.ai.

Prouve, sur le vrai `make_handler` côté nokker.ai et la vraie source côté machine : l'identifiant n'apparaît ni dans
une réponse, ni dans le journal local, ni dans le contexte du fil ; une autre machine, un autre compte, un jeton révoqué
sont refusés ; un outil à effet sans carte approuvée n'est pas exécuté (et la carte ne sert qu'une fois) ; un envoi
externe et le destructif exigent une carte forte (X20b, tests/unit/test_relayed_strong.py) ; chaque appel est compté au
ledger et audité ; le résultat est caviardé ; hors ligne ou
non liée, une erreur claire et jamais un faux résultat ; en local, un outil `cloud` à effet demande toujours une carte."""
from __future__ import annotations

import json
import logging
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from nokker.api.server import make_handler
from nokker.approvals.crypto import args_sha256
from nokker.approvals.executor import ApprovalExecutor
from nokker.approvals.memory import InMemoryApprovals
from nokker.cloud import http as cloud_http
from nokker.cloud.http import CloudLink
from nokker.cloud.link import LinkService, Signer
from nokker.cloud.relay import ConnectorRelay, MemoryRelayAudit
from nokker.connectors.crypto import SecretBox
from nokker.connectors.store import MemoryConnectorStore
from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Decision, Effect, Gate, ToolCall, ToolResult, Verdict
from nokker.ledger.costs import InMemoryLedger
from nokker.local.relayed import RelayedConnectors
from nokker.qa.invariants import DeterministicInvariants
from nokker.qa.secrets import KnownSecrets
from tests.unit.api.test_server import make_kernel

APP_PASSWORD = "app-pass-QWERTZ-998877"  # l'identifiant du connecteur : ne doit JAMAIS arriver côté machine
VAULT_VALUE = "sk-live-relay-0123456789abcdef"  # valeur connue du coffre de nokker.ai : caviardée à la sortie


class FakeMail:
    """Faux connecteur mail (aucun réseau) : mêmes noms et effets que `MailConnector`, et un résultat qui CITE
    l'identifiant et une valeur du coffre, pour prouver le caviardage."""

    calls: list = []

    def __init__(self, settings, secret, locality=None):
        self.secret, self.locality = secret, locality

    def specs(self):
        def spec(name, effect):
            return ToolSpec(name=name, description=f"{name} de ta boîte", effect=effect, source="connector:mail",
                            parameters={"type": "object", "properties": {"q": {"type": "string"}}},
                            locality=self.locality)

        return [spec("mail_search", Effect.READ), spec("mail_draft", Effect.WRITE),
                spec("mail_send", Effect.SEND_EXTERNAL), spec("mail_trash", Effect.DESTRUCTIVE)]

    def call(self, name, arguments, ctx):
        FakeMail.calls.append((name, arguments, self.secret, ctx.channel))
        return ToolResult(call_id="", name=name, content=json.dumps(
            {"ok": name, "debug": f"login with {self.secret}", "token": VAULT_VALUE}))


def _fake_build(kind, settings, secret, locality=None):
    return FakeMail(settings, secret, locality)


def _redactor():
    known = KnownSecrets()
    known.add("RELAY_TEST_KEY", VAULT_VALUE)
    return DeterministicInvariants(known=known).redact_result


@pytest.fixture
def cloud():
    FakeMail.calls = []
    links = LinkService()
    store = MemoryConnectorStore(SecretBox(SecretBox.generate_key()))
    store.put("us_pierre", "mail", {"preset": "gmail", "address": "pierre@example.com"}, APP_PASSWORD)
    store.mark_ok("us_pierre", "mail")
    audit, ledger = MemoryRelayAudit(), InMemoryLedger()

    def record(tenant, machine, entry):
        ledger.record_verdict(f"{tenant}:lokkal-{machine}", 0, Verdict(
            gate=Gate.ACTION, decision=Decision.DENY if entry["outcome"] == "refused" else Decision.ALLOW, layer=1,
            rule=f"relay:{entry['connector']}:{entry['tool']}", reason=str(entry.get("code") or entry["outcome"]),
            subject=entry["audit_id"]))

    relay = ConnectorRelay(store=store, audit=audit, redact=_redactor(), record=record, build=_fake_build)
    cloud_http.configure(CloudLink(links=links, signer=Signer.generate(), identity=lambda h: None,
                                   tenant_of=lambda a: None, router_base="http://127.0.0.1:9", router_key=lambda: "",
                                   relay=relay))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), {}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def machine(account, tenant, name="atelier"):
        d = links.device(name)
        links.approve(d["user_code"], account, tenant)
        return links.token(d["device_code"])["machine_token"]

    yield {"port": httpd.server_address[1], "links": links, "audit": audit, "ledger": ledger, "store": store,
           "machine": machine, "base": f"http://127.0.0.1:{httpd.server_address[1]}"}
    cloud_http.configure(None)
    httpd.shutdown()


def _post(port, path, body, token):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _get(port, path, token):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _source(cloud, token, approvals=None, tmp_path=None):
    return RelayedConnectors(base_url=cloud["base"], token=lambda: token, approvals=approvals,
                             journal_path=(tmp_path / "relay-audit.jsonl") if tmp_path else None)


CTX = ToolContext(thread_id="th_local", turn_index=0, channel="api", sender="local-owner")


# ─────────────────────────────── liste ───────────────────────────────


def test_listing_shows_tools_and_schemas_never_the_credential(cloud):
    token = cloud["machine"]("us_pierre", "tn_pierre")
    status, raw = _get(cloud["port"], "/v1/gateway/connectors", token)
    assert status == 200 and APP_PASSWORD not in raw
    [c] = json.loads(raw)["connectors"]
    assert (c["id"], c["locality"], c["state"], c["label"]) == ("mail", "cloud", "ok", "pierre@example.com")
    tools = {t["name"]: t for t in c["tools"]}
    assert tools["mail_search"]["approval"] == "none" and tools["mail_draft"]["approval"] == "required"
    assert (tools["mail_send"]["approval"], tools["mail_send"]["class"]) == ("strong", "external_send")
    assert tools["mail_trash"]["relayed"] is True and tools["mail_trash"]["approval"] == "strong"  # X20b
    src = _source(cloud, token)
    assert src.sync()
    specs = {s.name: s for s in src.specs()}
    assert set(specs) == {"mail_search", "mail_draft", "mail_send", "mail_trash"}
    assert all(s.locality == "cloud" and s.source == "relay:mail" for s in specs.values())
    assert specs["mail_send"].effect is Effect.SEND_EXTERNAL and "relayé" in specs["mail_send"].description
    assert src.status()["connectors"] == [{"id": "mail", "kind": "mail", "label": "pierre@example.com",
                                           "locality": "cloud", "state": "ok",
                                           "tools": ["mail_search", "mail_draft", "mail_send", "mail_trash"]}]


def test_read_call_is_executed_with_the_kept_credential_redacted_counted_and_audited(cloud, tmp_path):
    token = cloud["machine"]("us_pierre", "tn_pierre")
    src = _source(cloud, token, tmp_path=tmp_path)
    src.sync()
    out = src.call("mail_search", {"q": "facture"}, CTX)
    assert not out.is_error and json.loads(out.content)["ok"] == "mail_search"
    assert APP_PASSWORD not in out.content and VAULT_VALUE not in out.content  # caviardé chez nokker.ai
    assert "***" in out.content and "[secret:RELAY_TEST_KEY]" in out.content
    assert FakeMail.calls == [("mail_search", {"q": "facture"}, APP_PASSWORD, "lokkal-relay")]  # identifiant de là-bas
    [row] = cloud["audit"].of_tenant("tn_pierre")
    assert row["outcome"] == "executed" and row["tool"] == "mail_search" and row["approval_id"] is None
    assert row["args_sha256"] == args_sha256({"q": "facture"}) and "facture" not in json.dumps(row)
    [v] = cloud["ledger"].fetch_verdicts()
    assert v.thread_id.startswith("tn_pierre:lokkal-mc_") and v.rule == "relay:mail:mail_search" \
        and v.decision == "allow" and v.subject == row["audit_id"]
    [line] = (tmp_path / "relay-audit.jsonl").read_text().splitlines()
    entry = json.loads(line)
    assert entry["outcome"] == "executed" and entry["audit_id"] == row["audit_id"] and "facture" not in line
    assert oct((tmp_path / "relay-audit.jsonl").stat().st_mode & 0o777) == "0o600"


def test_effect_tool_without_approval_is_never_executed(cloud, tmp_path):
    token = cloud["machine"]("us_pierre", "tn_pierre")
    status, raw = _post(cloud["port"], "/v1/gateway/connectors/mail/call",
                        {"tool": "mail_draft", "arguments": {"q": "x"}}, token)
    body = json.loads(raw)
    assert status == 403 and body["error"]["code"] == "approval_required" and FakeMail.calls == []
    [row] = cloud["audit"].of_tenant("tn_pierre")
    assert row["outcome"] == "refused" and row["code"] == "approval_required" and body["audit_id"] == row["audit_id"]
    assert cloud["ledger"].fetch_verdicts()[0].decision == "deny"
    # Côté machine : sans carte approuvée, rien ne part (pas même une requête).
    approvals = InMemoryApprovals()
    src = _source(cloud, token, approvals=approvals, tmp_path=tmp_path)
    src.sync()
    out = src.call("mail_send", {"q": "x"}, CTX)
    assert out.is_error and "carte approuvée" in out.content
    assert len(cloud["audit"].of_tenant("tn_pierre")) == 1 and FakeMail.calls == []
    # Une carte qui existe mais pour d'autres arguments : refusée ici aussi.
    card = _card(approvals, "mail_send", {"q": "autre"})
    out = src.call("mail_send", {"q": "x"}, ToolContext(thread_id="th_local", turn_index=0, channel="api",
                                                        approval_id=card.id))
    assert out.is_error and "ne correspond pas" in out.content and FakeMail.calls == []


def _card(approvals, tool, arguments, approve=True):
    from nokker.core.turn import Turn, TurnInput

    turn = Turn(thread_id="th_local", index=0, input=TurnInput(thread_id="th_local", user_text="go"),
                sender="local-owner")
    card = approvals.request(turn, ToolCall(id="c1", name=tool, arguments=arguments),
                             Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="permissions:relayed_connector",
                                     strength="standard"), None)
    return approvals.decide_local(card.id, "approve", "local-owner") if approve else card


def test_approval_proof_bound_to_arguments_and_single_use(cloud):
    """Écriture interne (brouillon) : la preuve de carte locale suffit (inchangé par X20b), liée aux arguments, une
    fois."""
    token = cloud["machine"]("us_pierre", "tn_pierre")
    args = {"q": "bonjour"}
    proof = {"id": "ap_123", "args_sha256": args_sha256({"q": "autre chose"}), "decided_via": "cli"}
    status, raw = _post(cloud["port"], "/v1/gateway/connectors/mail/call",
                        {"tool": "mail_draft", "arguments": args, "approval": proof}, token)
    assert status == 403 and json.loads(raw)["error"]["code"] == "approval_mismatch" and FakeMail.calls == []
    proof["args_sha256"] = args_sha256(args)
    status, raw = _post(cloud["port"], "/v1/gateway/connectors/mail/call",
                        {"tool": "mail_draft", "arguments": args, "approval": proof}, token)
    assert status == 200 and len(FakeMail.calls) == 1 and json.loads(raw)["class"] == "internal_write"
    status, raw = _post(cloud["port"], "/v1/gateway/connectors/mail/call",
                        {"tool": "mail_draft", "arguments": args, "approval": proof}, token)
    assert status == 409 and json.loads(raw)["error"]["code"] == "approval_reused" and len(FakeMail.calls) == 1
    executed = [r for r in cloud["audit"].of_tenant("tn_pierre") if r["outcome"] == "executed"]
    assert executed[0]["approval_id"] == "ap_123" and executed[0]["approval"]["decided_via"] == "cli"


def test_destructive_without_strong_card_and_unknown_are_refused(cloud):
    token = cloud["machine"]("us_pierre", "tn_pierre")
    proof = {"id": "ap_9", "args_sha256": args_sha256({})}
    for tool in ("mail_trash", "mail_send"):  # carte locale seule : refusée (X20b), rien n'est exécuté
        status, raw = _post(cloud["port"], "/v1/gateway/connectors/mail/call",
                            {"tool": tool, "arguments": {}, "approval": proof}, token)
        err = json.loads(raw)["error"]
        assert status == 403 and err["code"] == "strong_approval_required" and "téléphone" in err["message"]
    assert FakeMail.calls == []
    assert _post(cloud["port"], "/v1/gateway/connectors/mail/call", {"tool": "rm_rf", "arguments": {}}, token)[0] == 404
    assert _post(cloud["port"], "/v1/gateway/connectors/calendar/call",
                 {"tool": "calendar_events", "arguments": {}}, token)[0] == 404  # pas posé sur ce compte
    assert _post(cloud["port"], "/v1/gateway/connectors/mail/call", {"arguments": {}}, token)[0] == 400


def test_other_account_other_machine_revoked_token_are_refused(cloud):
    pierre = cloud["machine"]("us_pierre", "tn_pierre")
    paul = cloud["machine"]("us_paul", "tn_paul", name="portable")  # autre compte, sans connecteur
    status, raw = _get(cloud["port"], "/v1/gateway/connectors", paul)
    assert status == 200 and json.loads(raw) == {"connectors": []}
    status, _ = _post(cloud["port"], "/v1/gateway/connectors/mail/call",
                      {"tool": "mail_search", "arguments": {}}, paul)
    assert status == 404 and FakeMail.calls == []  # la boîte de Pierre n'est pas joignable par la machine de Paul
    assert _get(cloud["port"], "/v1/gateway/connectors", "pas-un-jeton")[0] == 401
    assert _post(cloud["port"], "/v1/gateway/connectors/mail/call", {"tool": "mail_search"}, "pas-un-jeton")[0] == 401
    cloud["links"].revoke(pierre)
    assert _get(cloud["port"], "/v1/gateway/connectors", pierre)[0] == 401
    assert _post(cloud["port"], "/v1/gateway/connectors/mail/call", {"tool": "mail_search"}, pierre)[0] == 401
    assert FakeMail.calls == []


def test_revoked_token_empties_the_local_tools(cloud):
    token = cloud["machine"]("us_pierre", "tn_pierre")
    changes = []
    src = _source(cloud, token)
    src.on_change = lambda: changes.append(1)
    src.sync()
    assert src.specs() and changes == [1]
    cloud["links"].revoke(token)
    out = src.call("mail_search", {}, CTX)
    assert out.is_error and "machine déliée ou révoquée" in out.content
    assert src.specs() == [] and changes == [1, 1]
    assert not src.sync() and src.specs() == []


def test_unlinked_and_offline_give_a_clear_error_never_a_fake_result(cloud, tmp_path):
    unlinked = _source(cloud, None)
    assert not unlinked.sync() and unlinked.specs() == [] and unlinked.status()["linked"] is False
    token = cloud["machine"]("us_pierre", "tn_pierre")
    src = _source(cloud, token, tmp_path=tmp_path)
    src.sync()
    with socket.socket() as s:  # un port fermé : nokker.ai injoignable
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    src.base_url = f"http://127.0.0.1:{dead}"
    out = src.call("mail_search", {"q": "x"}, CTX)
    assert out.is_error and "injoignable" in out.content and "aucun résultat" in out.content
    assert not src.sync() and src.status()["online"] is False
    assert [s.name for s in src.specs()] == ["mail_search", "mail_draft", "mail_send", "mail_trash"]  # la liste reste
    src.token = lambda: None  # machine déliée entre-temps
    out = src.call("mail_search", {"q": "x"}, CTX)
    assert out.is_error and "non liée" in out.content
    assert FakeMail.calls == []
    assert [json.loads(x)["outcome"] for x in (tmp_path / "relay-audit.jsonl").read_text().splitlines()] == ["offline"]


def test_relay_switched_off_on_nokker_ai_lists_nothing(cloud):
    token = cloud["machine"]("us_pierre", "tn_pierre")
    cloud_http._cloud.relay = None
    assert _get(cloud["port"], "/v1/gateway/connectors", token)[0] == 404
    src = _source(cloud, token)
    assert not src.sync() and src.specs() == []


# ─────────────────────────────── portes locales ───────────────────────────────


def _gates(mode, allow=()):
    from nokker.qa.gates import GatesV0
    from nokker.qa.permissions import LocalPermissions, Mode, PermissionGates, Rule

    return PermissionGates(inner=GatesV0(invariants=DeterministicInvariants()),
                           permissions=LocalPermissions(mode=Mode(mode), allow=[Rule.parse(r) for r in allow]))


def _turn(text="envoie-le, GO"):
    from nokker.core.turn import InputCheck, Turn

    return Turn(thread_id="th_local", index=0, input=InputCheck(text=text), sender="local-owner")


@pytest.mark.parametrize("mode", ["auto", "accept_edits", "default"])
def test_cloud_effect_tool_always_gets_a_card_whatever_the_mode_and_rules(mode):
    from nokker.local.approvals import suggested_rules
    from nokker.qa.permissions import RELAY_CARD_RULE

    gates = _gates(mode, allow=["mail_send", "mail_draft", "mail_trash"])
    for name, effect, strength in (("mail_send", Effect.SEND_EXTERNAL, "strong"), ("mail_draft", Effect.WRITE, "standard"),
                                   ("mail_trash", Effect.DESTRUCTIVE, "strong")):
        spec = ToolSpec(name=name, description="", parameters={}, effect=effect, locality="cloud")
        [v] = gates.action(ToolCall(id="c", name=name, arguments={"to": "a@b.ch"}), spec, _turn())
        assert v.decision is Decision.HITL and v.rule == RELAY_CARD_RULE and v.strength == strength
        assert suggested_rules(name, {}, v.strength, v.rule) == []  # une carte par appel, pas de règle durable
    read = ToolSpec(name="mail_search", description="", parameters={}, effect=Effect.READ, locality="cloud")
    assert gates.action(ToolCall(id="c", name="mail_search", arguments={}), read, _turn())[0].decision \
        is Decision.ALLOW
    local = ToolSpec(name="mail_draft", description="", parameters={}, effect=Effect.WRITE, locality="local")
    if mode == "auto":  # un connecteur posé DANS Lokkal suit les règles locales ordinaires
        assert gates.action(ToolCall(id="c", name="mail_draft", arguments={}), local, _turn())[0].decision \
            is Decision.ALLOW


def test_plan_mode_and_bulk_stay_refused_for_cloud_tools():
    plan = _gates("plan")
    spec = ToolSpec(name="mail_send", description="", parameters={}, effect=Effect.SEND_EXTERNAL, locality="cloud")
    assert plan.action(ToolCall(id="c", name="mail_send", arguments={}), spec, _turn())[0].decision is Decision.DENY
    bulk = ToolSpec(name="mail_bulk", description="", parameters={}, effect=Effect.SEND_BULK, locality="cloud")
    assert _gates("auto").action(ToolCall(id="c", name="mail_bulk", arguments={}), bulk, _turn())[0].decision \
        is Decision.DENY


# ─────────────────────────────── bout en bout ───────────────────────────────


def test_end_to_end_card_then_relay_and_the_credential_never_reaches_the_machine(cloud, tmp_path, caplog):
    """Le modèle de la session Lokkal veut écrire un brouillon (écriture interne) : carte (mode auto compris), décision du
    propriétaire, exécution par la carte → appel relayé avec sa preuve → exécuté chez nokker.ai. Ni le contexte du fil,
    ni le journal local, ni les journaux du processus ne voient l'identifiant. (L'envoi, carte forte signée au
    téléphone : tests/unit/test_relayed_strong.py.)"""
    from nokker.core.loop import Kernel
    from nokker.core.ports import Completion, RouteDecision
    from nokker.core.turn import Message, Tier, TurnInput, Usage
    from nokker.store.pg import MemoryStore
    from nokker.tools.registry import Registry
    from tests.unit.api.test_approvals_routes import ReplayingContext
    from tests.unit.api.test_server import NoEscalator, NullMem

    caplog.set_level(logging.DEBUG)
    token = cloud["machine"]("us_pierre", "tn_pierre")
    approvals = InMemoryApprovals()
    src = _source(cloud, token, approvals=approvals, tmp_path=tmp_path)
    src.sync()
    args = {"q": "rendez-vous jeudi"}

    class Pol:
        def choose(self, state):
            return RouteDecision(tier=Tier.SHIP, reasoning=False, reason="t")

    class Ship:
        def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
            if not any(m.get("role") == "tool" for m in messages) and "[Approbation]" not in messages[0]["content"]:
                tc = ToolCall(id="c1", name="mail_draft", arguments=args)
                return Completion(message=Message(role="assistant", tool_calls=[tc]), usage=Usage(prompt=5),
                                  model="t", latency_s=0.0)
            return Completion(message=Message(role="assistant", content="envoyé"), usage=Usage(prompt=5), model="t",
                              latency_s=0.0)

    inv = DeterministicInvariants()
    store = MemoryStore()
    kernel = Kernel(store=store, context=ReplayingContext(), memory=NullMem(), invariants=inv,
                    gates=_gates("auto", allow=["mail_draft"]), escalator=NoEscalator(), policy=Pol(),
                    providers={Tier.SHIP: Ship()}, tools=Registry(sources=[src]), ledger=InMemoryLedger(),
                    approvals=approvals)
    kernel.run_turn(TurnInput(thread_id="th_local", user_text="écris à Anne", sender="local-owner"))
    assert FakeMail.calls == []  # mode auto ET règle allow : carte quand même, rien n'est parti
    [card] = approvals.pending()
    assert card.tool_name == "mail_draft" and card.args_sha256 == args_sha256(args) and card.strength == "standard"
    decided = approvals.decide_local(card.id, "approve", "local-owner")
    ApprovalExecutor(kernel=kernel, approvals=approvals).run(decided)
    assert [c[0] for c in FakeMail.calls] == ["mail_draft"] and FakeMail.calls[0][2] == APP_PASSWORD
    assert approvals.get(card.id).status == "executed"
    [row] = [r for r in cloud["audit"].of_tenant("tn_pierre") if r["outcome"] == "executed"]
    assert row["approval_id"] == card.id and row["approval"]["decided_by"] == "local-owner" \
        and row["args_sha256"] == card.args_sha256
    machine_side = json.dumps([t.__dict__ for t in store.history("th_local")], default=str) \
        + (tmp_path / "relay-audit.jsonl").read_text() + caplog.text + json.dumps(approvals.get(card.id).__dict__)
    assert "mail_draft" in machine_side and APP_PASSWORD not in machine_side and VAULT_VALUE not in machine_side


# ─────────────────────────────── localité et drapeau ───────────────────────────────


def test_connectors_carry_their_locality():
    from nokker.connectors import build_connector
    from nokker.connectors.provider import ConnectorProvider
    from nokker.connectors.service import Connectors

    store = MemoryConnectorStore(SecretBox(SecretBox.generate_key()))
    store.put("us_a", "calendar", {"mode": "ical"}, "https://cal.example.com/secret.ics")
    assert {s.locality for s in build_connector("calendar", {"mode": "ical"}, "https://cal.example.com/x.ics",
                                                locality="cloud").specs()} == {"cloud"}
    mail = build_connector("mail", {"preset": "gmail", "address": "a@example.com"}, "pw", locality="local")
    assert {s.locality for s in mail.specs()} == {"local"}
    assert {s.locality for s in build_connector("mail", {"preset": "gmail", "address": "a@example.com"},
                                                "pw").specs()} == {None}  # partagées, jamais modifiées en place
    [src] = ConnectorProvider(store, lambda t: "us_a", locality="local").sources_for("tn_a")
    assert {s.locality for s in src.specs()} == {"local"}
    cards = {c["kind"]: c for c in Connectors(store, tester=None, locality="local").cards("us_a")}
    assert cards["calendar"]["locality"] == "local" and cards["mail"] == {"kind": "mail", "state": "absent",
                                                                         "locality": "local"}
    assert Connectors(store, tester=None).cards("us_a")[0]["locality"] == "cloud"


def test_flag_is_off_in_code_and_on_in_the_lokkal_compose(tmp_path, monkeypatch):
    from pathlib import Path

    import yaml

    from nokker.local.api import LocalApi

    monkeypatch.setenv("NOKKER_LOCAL_SECRET_KEY", SecretBox.generate_key())
    base = {"state_dir": str(tmp_path), "token_path": str(tmp_path / "tok"), "code": {"backend": "local"}}
    assert LocalApi.from_config(base, 8742).relayed_enabled is False
    assert LocalApi.from_config({**base, "relayed_connectors": True}, 8742).relayed_enabled is True
    cfg = yaml.safe_load((Path(__file__).resolve().parents[2] / "deploy" / "lokkal" / "core-config.yaml").read_text())
    assert cfg["local"]["relayed_connectors"] is True
