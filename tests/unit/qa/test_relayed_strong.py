"""X10 : une action destructive demandée depuis le WEB (tour arrivé par le relais `<nom>.nokker.app`, jeton de passerelle
signé par nokker.ai) exige TOUJOURS une carte forte signée au téléphone. Le GO textuel (« lance exactement : rm -rf X »)
ne vaut que pour un tour local ; ni une règle allow, ni le mode auto ne lèvent la carte d'un tour relayé.

Les gates sont celles de Lokkal (server.py : PermissionGates autour de GatesV0 + DeterministicInvariants, mode
accept_edits) ; le dernier test monte le chemin réel : requête HTTP relayée avec un jeton de passerelle, puis tour."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from nokker.api.server import make_handler
from nokker.core.loop import Kernel
from nokker.core.ports import ToolSpec
from nokker.core.turn import (
    RELAY_CHANNEL, Decision, Effect, Gate, InputCheck, Message, Tier, ToolCall, ToolResult, Turn,
)
from nokker.ledger.costs import InMemoryLedger
from nokker.local.api import LOCAL_OWNER
from nokker.qa.gates import GatesV0
from nokker.qa.invariants import Allowlist, DeterministicInvariants, GoAuthorizedSenders
from nokker.qa.permissions import GO_SANDBOX_RULE, RELAYED_STRONG_RULE, LocalPermissions, Mode, PermissionGates, Rule
from nokker.store.pg import MemoryStore
from tests.unit.api.test_server import FixedContext, NoEscalator, NullMem, ShipOnlyPolicy
from tests.unit.test_local_api import _api

GO_TEXT = "lance exactement : rm -rf recette-x10"
CMD = {"command": "rm -rf recette-x10"}
TERMINAL = ToolSpec("terminal", "commande", {}, Effect.DESTRUCTIVE)


def invariants() -> DeterministicInvariants:
    return DeterministicInvariants(go_senders=GoAuthorizedSenders(frozenset({LOCAL_OWNER})),
                                   allowlist=Allowlist.load(domains=["example.org"]))


def lokkal_gates(mode=Mode.ACCEPT_EDITS, allow=()) -> PermissionGates:
    """Comme server.py : PermissionGates(inner=GatesV0(invariants), permissions=LocalPermissions…)."""
    return PermissionGates(inner=GatesV0(invariants=invariants()),
                           permissions=LocalPermissions(mode=mode, allow=[Rule.parse(r) for r in allow]))


def turn_of(channel: str, text: str = GO_TEXT) -> Turn:
    return Turn(thread_id="th_x10", index=0, input=InputCheck(text=text), channel=channel, sender=LOCAL_OWNER)


def verdict(gates, channel, call=None, spec=TERMINAL, text=GO_TEXT):
    return gates.action(call or ToolCall(id="c1", name=spec.name, arguments=CMD), spec, turn_of(channel, text))[0]


def test_local_turn_keeps_the_textual_go():
    v = verdict(lokkal_gates(), "api")
    assert v.decision is Decision.ALLOW and v.rule == "effect:destructive:go"


def test_relayed_turn_gets_a_strong_card_despite_the_textual_go():
    v = verdict(lokkal_gates(), RELAY_CHANNEL)
    assert v.decision is Decision.HITL and v.strength == "strong" and v.rule == RELAYED_STRONG_RULE


@pytest.mark.parametrize("mode,allow", [
    (Mode.AUTO, ()),
    (Mode.ACCEPT_EDITS, ("terminal",)),
    (Mode.AUTO, ("terminal(rm*)",)),
])
def test_relayed_turn_card_survives_auto_mode_and_allow_rules(mode, allow):
    gates = lokkal_gates(mode, allow)
    assert verdict(gates, "api").decision is Decision.ALLOW  # le local est inchangé
    v = verdict(gates, RELAY_CHANNEL)
    assert v.decision is Decision.HITL and v.strength == "strong" and v.rule == RELAYED_STRONG_RULE


def test_relayed_turn_still_refused_by_deny_rules_and_plan_mode():
    assert verdict(lokkal_gates(Mode.PLAN), RELAY_CHANNEL).decision is Decision.DENY
    denying = PermissionGates(inner=GatesV0(invariants=invariants()),
                              permissions=LocalPermissions(deny=[Rule.parse("terminal(rm*)")]))
    assert verdict(denying, RELAY_CHANNEL).decision is Decision.DENY


def test_invariants_alone_never_give_a_relayed_turn_a_go_for_destructive_send_or_spend():
    inv = invariants()
    call = ToolCall(id="c1", name="terminal", arguments=CMD)
    v = inv.check_action(call, TERMINAL, turn_of(RELAY_CHANNEL))
    assert v.decision is Decision.HITL and v.strength == "strong" and v.rule == "effect:destructive:relayed"
    assert inv.check_action(call, TERMINAL, turn_of("api")).rule == "effect:destructive:go"
    send = ToolSpec("send_mail", "mail", {}, Effect.SEND_EXTERNAL)
    mail = ToolCall(id="c2", name="send_mail", arguments={"to": "alice@example.org"})
    assert inv.check_action(mail, send, turn_of("api")).decision is Decision.ALLOW  # allowlist, en local
    v = inv.check_action(mail, send, turn_of(RELAY_CHANNEL))
    assert v.decision is Decision.HITL and v.strength == "strong"
    spend = ToolSpec("buy", "achat", {}, Effect.SPEND)
    v = inv.check_action(ToolCall(id="c3", name="buy", arguments={"sku": "x"}), spend, turn_of(RELAY_CHANNEL, "vas-y x"))
    assert v.decision is Decision.HITL and v.strength == "strong"
    bulk = ToolSpec("blast", "masse", {}, Effect.SEND_BULK)
    assert inv.check_action(ToolCall(id="c4", name="blast", arguments={"a": "b"}), bulk,
                            turn_of(RELAY_CHANNEL)).decision is Decision.DENY  # le refus reste un refus


# ─────────────── chemin réel : requête relayée (jeton de passerelle signé) → tour → verdict ───────────────


class OneToolThenDone:
    """Le modèle demande `terminal rm -rf recette-x10`, puis conclut."""

    def __init__(self):
        self.n = 0

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        from nokker.core.ports import Completion
        from nokker.core.turn import Usage

        self.n += 1
        msg = Message("assistant", tool_calls=[ToolCall(id=f"c{self.n}", name="terminal", arguments=CMD)]) \
            if self.n % 2 == 1 else Message("assistant", "fait")
        return Completion(message=msg, usage=Usage(prompt=5, completion=2), model="scripted", latency_s=0.0)


class Terminal:
    def __init__(self):
        self.called = []

    def core(self):
        return []

    def spec(self, name):
        return TERMINAL if name == "terminal" else None

    def find(self, query, limit=5):
        return []

    def index(self):
        return [("terminal", "commande")]

    def call(self, name, arguments, ctx):
        self.called.append((name, arguments, ctx.channel))
        return ToolResult(call_id="", name=name, content="ok")


@pytest.fixture
def lokkal(tmp_path):
    from nokker.cloud.link import Signer
    from nokker.local.setup import LocalSetup, LocalVault

    api = _api(tmp_path)
    inv = invariants()
    tools, store = Terminal(), MemoryStore()
    kernel = Kernel(store=store, context=FixedContext(), memory=NullMem(), invariants=inv, gates=lokkal_gates(),
                    escalator=NoEscalator(), policy=ShipOnlyPolicy(),
                    providers={Tier.SHIP: OneToolThenDone()}, tools=tools, ledger=InMemoryLedger())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: LOCAL_OWNER}, local_api=api))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    signer = Signer.generate()
    api.setup = LocalSetup(state_path=tmp_path / "setup.json", vault=LocalVault(tmp_path / "v.bin"), detect=lambda: [])
    api.setup.state.update(jwks=signer.jwks(), account="us_pierre", machine_id="mc_1")

    class Up:
        state = "up"
        secret = "secret-du-tunnel"

    api.relay_host, api.tunnel = "pierre.nokker.app", Up()
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    def chat(token, session, relayed, tunnel_only=False):
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "X-Session-Id": session}
        if relayed and not tunnel_only:
            headers["X-Nokker-Relay"] = "1"
        if tunnel_only:  # arrivée par notre tunnel sans le marqueur du relais (en-tête perdu) : web quand même
            headers["X-Nokker-Tunnel"] = Up.secret
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", headers=headers, method="POST",
                                     data=json.dumps({"messages": [{"role": "user", "content": GO_TEXT}]}).encode())
        if relayed:
            req.add_unredirected_header("Host", "pierre.nokker.app")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    yield api, signer, tools, store, chat
    httpd.shutdown()
    t.join(timeout=5)


def last_turn(store):
    return [t for ts in store._by_thread.values() for t in ts][-1]


def test_relayed_chat_with_a_gateway_token_gets_a_strong_card_not_the_go(lokkal):
    api, signer, tools, store, chat = lokkal
    assert chat(signer.access_token("us_pierre", "tn_pierre"), "th_relayed", relayed=True) == 200
    turn = last_turn(store)
    assert (turn.channel, turn.sender) == (RELAY_CHANNEL, LOCAL_OWNER)
    verdicts = [v for v in turn.verdicts if v.gate is Gate.ACTION]
    assert [(v.decision, v.strength, v.rule) for v in verdicts] == [(Decision.HITL, "strong", RELAYED_STRONG_RULE)]
    assert tools.called == []  # rien n'a tourné : la carte attend le téléphone


def test_a_request_from_our_tunnel_is_relayed_even_without_the_relay_marker(lokkal):
    api, signer, tools, store, chat = lokkal
    # le jeton local présenté par le tunnel est refusé, comme s'il venait du relais
    assert chat(api.token, "th_tunnel", relayed=True, tunnel_only=True) == 403
    assert chat(signer.access_token("us_pierre", "tn_pierre"), "th_tunnel", relayed=True, tunnel_only=True) == 200
    turn = last_turn(store)
    assert turn.channel == RELAY_CHANNEL and tools.called == []


def test_local_chat_keeps_the_textual_go(lokkal):
    api, signer, tools, store, chat = lokkal
    assert chat(api.token, "th_local", relayed=False) == 200
    turn = last_turn(store)
    assert (turn.channel, turn.sender) == ("api", LOCAL_OWNER)
    verdicts = [v for v in turn.verdicts if v.gate is Gate.ACTION]
    assert [(v.decision, v.rule) for v in verdicts] == [(Decision.ALLOW, "effect:destructive:go")]
    assert tools.called == [("terminal", CMD, "api")]


# ─────────────── en local, le GO textuel ne vaut que dans le bac à sable d'une session de code ───────────────


def sandbox_gates(sessions=("th_x10",)) -> PermissionGates:
    """Lokkal : `go_sandbox` = fil de session de code ET backend autre que « local » (server.py)."""
    return PermissionGates(inner=GatesV0(invariants=invariants()), permissions=LocalPermissions(mode=Mode.ACCEPT_EDITS),
                           go_sandbox=lambda tid: tid in sessions)


def test_local_go_in_a_code_session_sandbox_is_allowed():
    for name in ("terminal", "process_start"):
        spec = ToolSpec(name, "commande", {}, Effect.DESTRUCTIVE)
        v = verdict(sandbox_gates(), "api", ToolCall(id="c", name=name, arguments={"command": "rm -rf build"}), spec,
                    text="lance exactement : rm -rf build")
        assert v.decision is Decision.ALLOW and v.rule == "effect:destructive:go", name


def test_local_go_outside_the_sandbox_is_still_a_strong_card():
    ssh = ToolSpec("ssh_run", "commande distante", {}, Effect.DESTRUCTIVE)
    ssh_call = ToolCall(id="c", name="ssh_run", arguments={"host": "web1", "command": "rm -rf build"})
    text = "lance exactement : rm -rf build sur web1"
    v = verdict(sandbox_gates(), "api", ssh_call, ssh, text)  # même sur un fil de session de code
    assert v.decision is Decision.HITL and v.strength == "strong" and v.rule == GO_SANDBOX_RULE
    mcp = ToolSpec("mcp_mail_delete", "supprime un mail", {}, Effect.DESTRUCTIVE)
    v = verdict(sandbox_gates(), "api", ToolCall(id="c", name="mcp_mail_delete", arguments={"id": "m1"}), mcp,
                "vas-y supprime m1")
    assert v.decision is Decision.HITL and v.strength == "strong"
    # terminal sur un fil hors session de code
    other = turn_of("api", "lance exactement : rm -rf build")
    other.thread_id = "th_hors_session"
    v = sandbox_gates().action(ToolCall(id="c", name="terminal", arguments={"command": "rm -rf build"}), TERMINAL,
                               other)[0]
    assert v.decision is Decision.HITL and v.strength == "strong" and v.rule == GO_SANDBOX_RULE


def test_relayed_terminal_in_a_code_session_is_still_a_strong_card():
    v = verdict(sandbox_gates(), RELAY_CHANNEL, ToolCall(id="c", name="terminal", arguments={"command": "rm -rf build"}),
                text="lance exactement : rm -rf build")
    assert v.decision is Decision.HITL and v.strength == "strong" and v.rule == RELAYED_STRONG_RULE


def test_code_tools_on_a_thread_without_code_session_are_refused_before_any_card():
    """26.10 : une carte forte signée au téléphone a échoué à l'exécution (« pas de session de code sur ce fil »). Un
    outil de session de code sur un fil sans session est refusé tout de suite, sans carte."""
    from nokker.qa.gates import GatesV0

    gates = PermissionGates(inner=GatesV0(invariants=invariants()), permissions=LocalPermissions(mode=Mode.AUTO),
                            code_session=lambda tid: tid == "th_session", session_tools=frozenset({"terminal"}))
    call = ToolCall(id="c1", name="terminal", arguments={"command": "rm -rf build"})
    out = gates.action(call, TERMINAL, Turn(thread_id="th_chat", index=1, input=InputCheck(text=GO_TEXT),
                                            channel=RELAY_CHANNEL, sender=LOCAL_OWNER))
    assert [(v.decision, v.rule) for v in out] == [(Decision.DENY, "permissions:no_code_session")]
    ok = gates.action(call, TERMINAL, Turn(thread_id="th_session", index=1, input=InputCheck(text=GO_TEXT),
                                           channel=RELAY_CHANNEL, sender=LOCAL_OWNER))
    assert ok[0].rule != "permissions:no_code_session"
