"""Cartes fortes des connecteurs relayés (lot X20b, docs/LOKKAL.md §7.12) : un envoi externe (ou un destructif) relayé
n'est exécuté par nokker.ai que sur une carte FORTE signée par un appareil du compte, et c'est nokker.ai qui vérifie
lui-même la signature avant d'exécuter.

Monde de test sans réseau externe : les vraies routes de nokker.ai (`make_handler` + `CloudLink` : boîte aux lettres X10
et relais X20 en mémoire), une machine liée de Pierre (noyau local InMemoryApprovals, `RemoteApprovals`,
`RelayedConnectors`), un « téléphone » qui signe une vraie assertion WebAuthn, et un attaquant qui n'a QUE le jeton de
machine (il lit la boîte aux lettres comme la machine, il poste directement à la route d'appel).

Cas : jeton volé + carte locale ; preuve d'un autre appel, d'autres arguments, d'un autre outil, d'un autre compte, d'un
appareil de simple connexion, sans UV, forgée, rejouée (même machine, autre machine), expirée ; vérification non
configurée ; bonne preuve = un envoi, une fois ; écriture interne inchangée ; classes d'outils ; carte forte relayée
non approuvable par le jeton local."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import httpx
import pytest

from nokker.api.server import make_handler
from nokker.approvals.crypto import args_sha256
from nokker.approvals.executor import ApprovalExecutor
from nokker.approvals.memory import InMemoryApprovals
from nokker.cloud import http as cloud_http
from nokker.cloud.approvals import MemoryRelayStore, RelayService
from nokker.cloud.http import CloudLink
from nokker.cloud.link import LinkService, Signer
from nokker.cloud.relay import (
    TOOL_CLASSES,
    ConnectorRelay,
    MemoryRelayAudit,
    ToolClass,
    tool_class,
    tool_view,
)
from nokker.connectors.crypto import SecretBox
from nokker.connectors.store import MemoryConnectorStore
from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Decision, Effect, Gate, InputCheck, ToolCall, Turn, Verdict
from nokker.local.relayed import RelayedConnectors
from nokker.local.remote_approvals import RemoteApprovals
from nokker.qa.permissions import RELAY_CARD_RULE
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from tests.unit.api.test_server import make_kernel
from tests.unit.approvals.conftest import gen_keypair
from tests.unit.test_relayed_connectors import APP_PASSWORD, FakeMail, _fake_build, _redactor
from tests.unit.test_remote_approvals import ORIGIN, webauthn_proof

PIERRE, MARIE = "us_pierre", "us_marie"
TENANTS = {PIERRE: "tn_pierre", MARIE: "tn_marie"}
SESS = {"Bearer " + "sess-" + "pierre": PIERRE, "Bearer " + "sess-" + "marie": MARIE}  # jetons de test, morcelés
ARGS = {"q": "à Anne : rendez-vous jeudi 14 h"}
CALL = "/v1/gateway/connectors/mail/call"


class World:
    def __init__(self, port: int, links: LinkService, cloud_appr: InMemoryApprovals, relay: ConnectorRelay,
                 audit: MemoryRelayAudit) -> None:
        self.port, self.links, self.cloud_appr, self.relay, self.audit = port, links, cloud_appr, relay, audit
        self.base = f"http://127.0.0.1:{port}"
        self.mid, self.mt = self.link_machine("atelier", PIERRE)
        self.local = InMemoryApprovals()
        self.priv, spki = gen_keypair()
        self.device = cloud_appr.confirm_device_by_invitation(
            cloud_appr.enroll_device(PIERRE, "iPhone de Pierre", spki).id, 1)  # appareil qui décide
        self.state: dict = {}
        self.applied: list[str] = []
        self.apply = self._mark  # remplacé par l'exécuteur réel dans le bout en bout
        self.worker = RemoteApprovals(base_url=self.base, origin=ORIGIN, token=lambda: self.mt,
                                      account=lambda: PIERRE, approvals=self.local, apply=lambda a: self.apply(a),
                                      state=self.state, client=httpx.Client(timeout=10.0))
        self.src = RelayedConnectors(base_url=self.base, token=lambda: self.mt, approvals=self.local,
                                     signed=self.worker.signed_proof)

    def _mark(self, a) -> None:
        self.applied.append(a.id)

    def link_machine(self, name: str, account: str) -> tuple[str, str]:
        d = self.links.device(name)
        self.links.approve(d["user_code"], account, TENANTS[account])
        tok = self.links.token(d["device_code"])
        return tok["machine_id"], tok["machine_token"]

    def http(self, method: str, path: str, body=None, *, sess: str | None = None, token: str | None = None):
        headers = {"Content-Type": "application/json"}
        if sess:
            headers["Authorization"] = f"Bearer sess-{sess}"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self.base + path, headers=headers, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    # ── machine de Pierre : une carte forte relayée, déposée chez nokker.ai ──

    def strong_card(self, tool: str = "mail_send", arguments: dict | None = None):
        turn = Turn(thread_id="th_local", index=0, input=InputCheck(text="écris à Anne"), sender="local-owner")
        with tenant_scope(INTERNAL_TENANT):
            card = self.local.request(turn, ToolCall(id="c1", name=tool, arguments=dict(arguments or ARGS)),
                                      Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=RELAY_CARD_RULE,
                                              strength="strong"), None)
        assert self.worker.sync_once() == 1
        return card

    # ── le téléphone de Pierre ──

    def challenge(self, aid: str, mid: str | None = None, sess: str = "pierre") -> str:
        status, body = self.http("GET", f"/v1/link/machines/{mid or self.mid}/approvals/{aid}", sess=sess)
        assert status == 200, body
        return body["challenge"]

    def phone_decides(self, aid: str, proof: dict | None = None, *, decision: str = "approve", mid: str | None = None,
                      sess: str = "pierre", **kw) -> int:
        proof = proof or webauthn_proof(self.priv, self.device.id, self.challenge(aid, mid, sess), decision, **kw)
        return self.http("POST", f"/v1/link/machines/{mid or self.mid}/approvals/{aid}/decision",
                         {"decision": decision, "proof": proof}, sess=sess)[0]

    # ── l'attaquant : le jeton de machine seul ──

    def mailbox(self, token: str | None = None) -> dict[str, dict]:
        """Ce que la machine (ou un voleur de son jeton) relève chez nokker.ai : les preuves transmises."""
        status, body = self.http("GET", "/v1/gateway/approvals/decisions?wait=0", token=token or self.mt)
        assert status == 200, body
        return {d["approval_id"]: {"challenge": d["challenge"], "decision": d["decision"], **d["proof"]}
                for d in body["decisions"]}

    def post_call(self, aid: str, signed: dict | None, *, tool: str = "mail_send", arguments: dict | None = None,
                  digest_of: dict | None = None, token: str | None = None):
        arguments = dict(ARGS if arguments is None else arguments)
        approval = {"id": aid, "args_sha256": args_sha256(arguments if digest_of is None else digest_of),
                    "decided_via": "webauthn", "decided_by": "local-owner", "strength": "strong"}
        if signed is not None:
            approval["signed"] = signed
        return self.http("POST", CALL, {"tool": tool, "arguments": arguments, "approval": approval},
                         token=token or self.mt)

    def back_to_pending(self, aid: str, mid: str | None = None, token: str | None = None) -> None:
        """Ce que la machine fait d'une preuve qu'elle refuse : la carte se rouvre chez nokker.ai."""
        assert self.http("POST", f"/v1/gateway/approvals/{aid}/result", {"status": "pending", "error": "refusée"},
                         token=token or self.mt)[0] == 200


@pytest.fixture
def world():
    FakeMail.calls = []
    cloud_appr = InMemoryApprovals(account_tenant=TENANTS.get)
    links = LinkService()
    store = MemoryConnectorStore(SecretBox(SecretBox.generate_key()))
    for account in (PIERRE, MARIE):
        store.put(account, "mail", {"preset": "gmail", "address": f"{account[3:]}@example.com"}, APP_PASSWORD)
        store.mark_ok(account, "mail")
    cards, audit = MemoryRelayStore(), MemoryRelayAudit()
    relay = ConnectorRelay(store=store, audit=audit, redact=_redactor(), build=_fake_build, verifier=cloud_appr,
                           devices_of=lambda acc: cloud_appr.list_devices(acc), cards=cards, origin=ORIGIN)
    cloud_http.configure(CloudLink(
        links=links, signer=Signer.generate(), identity=lambda h: SESS.get(h.headers.get("Authorization", "")),
        tenant_of=TENANTS.get, router_base="http://127.0.0.1:9/v1", router_key=lambda: "x", relay=relay,
        approvals_relay=RelayService(store=cards), devices_of=lambda acc: cloud_appr.list_devices(acc)))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), {}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    w = World(httpd.server_address[1], links, cloud_appr, relay, audit)
    assert w.src.sync()
    yield w
    cloud_http.configure(None)
    httpd.shutdown()


def _refused(resp, why: str | None = None) -> None:
    status, body = resp
    assert status == 403 and body["error"]["code"] == "strong_approval_required", body
    assert "téléphone" in body["error"]["message"]
    if why is not None:
        assert why in body["error"]["message"], body["error"]["message"]


# ═════════════════════════════ jeton de machine volé ═════════════════════════════


def test_stolen_machine_token_with_a_local_card_cannot_send(world):
    """Le voleur du jeton a une carte locale « approuvée » (preuve non signée, X20) : nokker.ai refuse l'envoi."""
    w = world
    _refused(w.post_call("ap_vole", None), "sans signature")
    card = w.strong_card()  # la même chose depuis la machine : carte forte approuvée au jeton local
    with tenant_scope(INTERNAL_TENANT):
        w.local.decide_local(card.id, "approve", "local-owner")
    out = w.src.call("mail_send", ARGS, ToolContext(thread_id="th_local", turn_index=0, channel="api",
                                                    approval_id=card.id))
    assert out.is_error and "signer sur ton téléphone" in out.content and "rien n'a été envoyé" in out.content
    _refused(w.post_call(card.id, None))
    assert FakeMail.calls == []
    assert all(r["outcome"] == "refused" and r["code"] == "strong_approval_required" for r in w.audit.rows)


# ═════════════════════════════ bout en bout : la bonne preuve ═════════════════════════════


def test_phone_signed_card_sends_once_end_to_end(world):
    """La session Lokkal veut envoyer un mail : carte FORTE (mode auto et règle allow compris), déposée chez nokker.ai,
    signée au téléphone (vraie assertion WebAuthn), vérifiée par la machine, exécutée par l'exécuteur : l'appel relayé
    porte la signature, nokker.ai la vérifie et envoie UNE fois. Le rejeu par le voleur du jeton est refusé."""
    from nokker.core.loop import Kernel
    from nokker.core.ports import Completion, RouteDecision
    from nokker.core.turn import Message, Tier, TurnInput, Usage
    from nokker.ledger.costs import InMemoryLedger
    from nokker.qa.invariants import DeterministicInvariants
    from nokker.store.pg import MemoryStore
    from nokker.tools.registry import Registry
    from tests.unit.api.test_approvals_routes import ReplayingContext
    from tests.unit.api.test_server import NoEscalator, NullMem
    from tests.unit.test_relayed_connectors import _gates

    w = world

    class Pol:
        def choose(self, state):
            return RouteDecision(tier=Tier.SHIP, reasoning=False, reason="t")

    class Ship:
        def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
            if not any(m.get("role") == "tool" for m in messages) and "[Approbation]" not in messages[0]["content"]:
                return Completion(message=Message(role="assistant", tool_calls=[
                    ToolCall(id="c1", name="mail_send", arguments=ARGS)]), usage=Usage(prompt=5), model="t",
                    latency_s=0.0)
            return Completion(message=Message(role="assistant", content="envoyé"), usage=Usage(prompt=5), model="t",
                              latency_s=0.0)

    kernel = Kernel(store=MemoryStore(), context=ReplayingContext(), memory=NullMem(),
                    invariants=DeterministicInvariants(), gates=_gates("auto", allow=["mail_send"]),
                    escalator=NoEscalator(), policy=Pol(), providers={Tier.SHIP: Ship()},
                    tools=Registry(sources=[w.src]), ledger=InMemoryLedger(), approvals=w.local)
    done = threading.Event()

    def execute(decided):
        ApprovalExecutor(kernel=kernel, approvals=w.local).run(decided)
        done.set()

    w.apply = execute
    kernel.run_turn(TurnInput(thread_id="th_local", user_text="écris à Anne", sender="local-owner"))
    assert FakeMail.calls == []
    with tenant_scope(INTERNAL_TENANT):
        [card] = w.local.pending()
    assert card.tool_name == "mail_send" and card.strength == "strong" and card.rule == RELAY_CARD_RULE
    assert w.worker.sync_once() == 1
    challenge = json.loads(w.challenge(card.id))
    assert challenge["tool"] == "mail_send" and challenge["args_sha256"] == args_sha256(ARGS)  # outil lié au défi
    assert w.phone_decides(card.id) == 202
    assert FakeMail.calls == []  # nokker.ai n'a rien exécuté sur la seule transmission de la preuve
    stolen = w.mailbox()[card.id]  # ce que voit aussi le voleur du jeton
    assert w.worker.poll_once(0) == ["approved"]
    assert done.wait(5)
    assert [c[0] for c in FakeMail.calls] == ["mail_send"] and FakeMail.calls[0][1] == ARGS
    with tenant_scope(INTERNAL_TENANT):
        assert w.local.get(card.id).status == "executed"
    [row] = [r for r in w.audit.rows if r["outcome"] == "executed"]
    assert row["approval_id"] == card.id and row["approval"]["verified"] == "webauthn"
    assert row["approval"]["device_id"] == w.device.id and row["strong_nonce"] == challenge["nonce"]
    assert stolen["webauthn_signature"] not in json.dumps(w.audit.rows)  # la signature n'est jamais journalisée
    # Rejeu : la même carte, la même signature, par le voleur du jeton ou par la machine elle-même.
    status, body = w.post_call(card.id, stolen)
    assert status == 409 and body["error"]["code"] == "approval_reused"
    out = w.src.call("mail_send", ARGS, ToolContext(thread_id="th_local", turn_index=0, channel="api",
                                                    approval_id=card.id))
    assert out.is_error and FakeMail.calls == [("mail_send", ARGS, APP_PASSWORD, "lokkal-relay")]


# ═════════════════════════════ une preuve qui ne porte pas sur CET appel ═════════════════════════════


def test_proof_of_another_call_arguments_tool_or_signature_is_refused_and_burns_nothing(world):
    w = world
    card = w.strong_card()
    other = w.strong_card(arguments={"q": "à Paul : tout est annulé"})
    assert w.phone_decides(card.id) == 202 and w.phone_decides(other.id) == 202
    box = w.mailbox()
    signed = box[card.id]
    _refused(w.post_call(card.id, signed, arguments={"q": "à Paul : virement urgent"}), "empreinte")
    _refused(w.post_call(card.id, signed, arguments={"q": "à Paul : virement urgent"}, digest_of=ARGS),
             "ne porte pas ces arguments")
    _refused(w.post_call(card.id, signed, tool="mail_trash"), "outil")
    _refused(w.post_call(other.id, signed), "ce défi")  # la signature d'une carte pour l'appel d'une autre
    _refused(w.post_call(card.id, box[other.id]), "ce défi")
    _refused(w.post_call(card.id, {**signed, "decision": "reject"}), "n'approuve pas")
    forged_priv, _ = gen_keypair()
    forged = webauthn_proof(forged_priv, w.device.id, signed["challenge"], "approve")
    _refused(w.post_call(card.id, {**signed, **forged}), "transmise par la page")
    _refused(w.post_call("ap_" + "0" * 30, signed), "ce défi")
    assert FakeMail.calls == []
    # Rien de tout cela n'a brûlé la carte : la bonne preuve passe, une fois.
    status, body = w.post_call(card.id, signed)
    assert status == 200 and body["class"] == "external_send" and len(FakeMail.calls) == 1


def test_forged_signature_forwarded_through_the_page_is_refused(world):
    """Un compte volé (session) fait transmettre une preuve forgée au nom du vrai appareil : nokker.ai la vérifie et la
    refuse ; la carte rouverte, l'appareil légitime signe et l'envoi passe."""
    w = world
    card = w.strong_card()
    forged_priv, _ = gen_keypair()
    assert w.phone_decides(card.id, webauthn_proof(forged_priv, w.device.id, w.challenge(card.id), "approve")) == 202
    _refused(w.post_call(card.id, w.mailbox()[card.id]), "signature refusée")
    w.back_to_pending(card.id)
    assert w.phone_decides(card.id) == 202
    assert w.post_call(card.id, w.mailbox()[card.id])[0] == 200 and len(FakeMail.calls) == 1


# ═════════════════════════════ appareil : autre compte, simple connexion, UV ═════════════════════════════


def test_proof_from_another_account_is_refused(world):
    """Marie (autre compte, sa machine, sa session) recopie la carte et la signature de Pierre chez elle : l'appareil
    n'est pas un appareil de SON compte. Et la machine de Marie ne peut pas utiliser la carte de Pierre."""
    w = world
    card = w.strong_card()
    assert w.phone_decides(card.id) == 202
    signed = w.mailbox()[card.id]
    mid_m, mt_m = w.link_machine("portable", MARIE)
    _refused(w.post_call(card.id, signed, token=mt_m), "ce défi")
    exp = json.loads(signed["challenge"])["exp"]
    status, _ = w.http("POST", "/v1/gateway/approvals", {"approval_id": card.id, "title": "mail_send",
                                                          "strength": "strong", "expires_at": exp,
                                                          "challenge": signed["challenge"], "arguments": ARGS},
                       token=mt_m)  # déposée avec ses arguments (X20c) : c'est l'appareil qui coince
    assert status == 201
    proof = {k: signed[k] for k in ("device_id", "webauthn_client_data_json", "webauthn_authenticator_data",
                                    "webauthn_signature")}
    assert w.phone_decides(card.id, proof, mid=mid_m, sess="marie") == 202
    _refused(w.post_call(card.id, w.mailbox(mt_m)[card.id], token=mt_m), "appareil inconnu du compte")
    assert FakeMail.calls == []


def test_login_only_device_and_missing_uv_are_refused(world):
    w = world
    card = w.strong_card()
    login_priv, login_spki = gen_keypair()
    login = w.cloud_appr.confirm_device_by_email_code(w.cloud_appr.enroll_device(PIERRE, "Navigateur", login_spki).id)
    assert w.phone_decides(card.id, webauthn_proof(login_priv, login.id, w.challenge(card.id), "approve")) == 202
    _refused(w.post_call(card.id, w.mailbox()[card.id]), "simple connexion")
    w.back_to_pending(card.id)
    assert w.phone_decides(card.id, uv=False) == 202  # le bon appareil, mais sans vérification de l'utilisateur
    _refused(w.post_call(card.id, w.mailbox()[card.id]), "UV")
    w.back_to_pending(card.id)
    assert w.phone_decides(card.id) == 202
    assert w.post_call(card.id, w.mailbox()[card.id])[0] == 200 and len(FakeMail.calls) == 1


# ═════════════════════════════ rejeu et expiration ═════════════════════════════


def test_signature_replayed_on_another_machine_of_the_account_is_refused(world):
    """Une signature déjà servie sur l'atelier, recopiée sur une autre machine du même compte (même id de carte, même
    défi, déposés et transmis là-bas) : le nonce du défi ne sert qu'une fois, partout."""
    w = world
    card = w.strong_card()
    assert w.phone_decides(card.id) == 202
    signed = w.mailbox()[card.id]
    assert w.post_call(card.id, signed)[0] == 200
    mid2, mt2 = w.link_machine("serveur", PIERRE)
    exp = json.loads(signed["challenge"])["exp"]
    assert w.http("POST", "/v1/gateway/approvals", {"approval_id": card.id, "title": "mail_send", "strength": "strong",
                                                    "expires_at": exp, "challenge": signed["challenge"],
                                                    "arguments": ARGS}, token=mt2)[0] == 201
    proof = {k: v for k, v in signed.items() if k not in ("challenge", "decision")}
    assert w.phone_decides(card.id, proof, mid=mid2) == 202
    status, body = w.post_call(card.id, w.mailbox(mt2)[card.id], token=mt2)
    assert status == 409 and body["error"]["code"] == "approval_reused" and len(FakeMail.calls) == 1


def test_expired_challenge_and_unconfigured_verification_are_refused(world):
    w = world
    card = w.strong_card()
    assert w.phone_decides(card.id) == 202
    signed = w.mailbox()[card.id]
    exp = json.loads(signed["challenge"])["exp"]
    w.relay.clock = lambda: exp + 1
    _refused(w.post_call(card.id, signed), "expiré")
    w.relay.clock = time.time
    for missing in ("verifier", "devices_of", "cards", "origin"):  # un seul manque : rien ne passe
        saved = getattr(w.relay, missing)
        setattr(w.relay, missing, None)
        _refused(w.post_call(card.id, signed), "non configurée")
        setattr(w.relay, missing, saved)
    assert FakeMail.calls == []
    assert w.post_call(card.id, signed)[0] == 200 and len(FakeMail.calls) == 1


# ═════════════════════════════ écriture interne inchangée ═════════════════════════════


def test_internal_write_still_runs_on_the_local_card(world):
    w = world
    turn = Turn(thread_id="th_local", index=0, input=InputCheck(text="brouillon"), sender="local-owner")
    with tenant_scope(INTERNAL_TENANT):
        card = w.local.request(turn, ToolCall(id="c1", name="mail_draft", arguments=ARGS),
                               Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=RELAY_CARD_RULE,
                                       strength="standard"), None)
        w.local.decide_local(card.id, "approve", "local-owner")
        out = w.src.call("mail_draft", ARGS, ToolContext(thread_id="th_local", turn_index=0, channel="api",
                                                         approval_id=card.id))
    assert not out.is_error and [c[0] for c in FakeMail.calls] == ["mail_draft"]
    [row] = w.audit.rows
    assert row["outcome"] == "executed" and row["approval"]["decided_via"] == "local-token" \
        and row["strong_nonce"] is None and "verified" not in row["approval"]  # journalisée comme en X20


# ═════════════════════════════ classes d'outils ═════════════════════════════


def test_every_connector_tool_is_classified_and_unclassified_means_external_send():
    from nokker.connectors import build_connector

    mail = build_connector("mail", {"preset": "gmail", "address": "a@example.com"}, "pw")
    cal = build_connector("calendar", {"mode": "caldav", "url": "https://cal.example.com/dav/", "username": "a"}, "pw")
    names = {s.name for s in mail.specs()} | {s.name for s in cal.specs()}
    assert names <= set(TOOL_CLASSES)  # chaque outil réel est classé explicitement
    specs = {s.name: s for s in [*mail.specs(), *cal.specs()]}
    assert tool_class(specs["mail_search"], {}) is ToolClass.READ
    assert tool_class(specs["mail_draft"], {}) is ToolClass.INTERNAL_WRITE
    assert tool_class(specs["mail_send"], {}) is ToolClass.EXTERNAL_SEND
    assert tool_class(specs["mail_trash"], {}) is ToolClass.DESTRUCTIVE
    create = specs["calendar_create"]
    assert tool_class(create, {"summary": "dentiste"}) is ToolClass.INTERNAL_WRITE  # son propre agenda
    assert tool_class(create, {"summary": "revue", "attendees": ["anne@example.com"]}) is ToolClass.EXTERNAL_SEND
    assert tool_view(create)["send_if"] == ["attendees"] and tool_view(create)["approval"] == "required"
    unknown = ToolSpec(name="mail_forward", description="", parameters={}, effect=Effect.WRITE)
    assert tool_class(unknown, {}) is ToolClass.EXTERNAL_SEND and tool_view(unknown)["approval"] == "strong"
    assert tool_view(unknown)["effect"] == "send_external"  # la machine en fera une carte forte
    weak = ToolSpec(name="mail_draft", description="", parameters={}, effect=Effect.SEND_EXTERNAL)
    assert tool_class(weak, {}) is ToolClass.EXTERNAL_SEND  # l'effet déclaré ne s'affaiblit jamais par la table
    bulk = ToolSpec(name="mail_bulk", description="", parameters={}, effect=Effect.SEND_BULK)
    assert tool_class(bulk, {}) is None and tool_view(bulk)["approval"] == "not_relayable"


def test_machine_makes_a_strong_card_for_an_event_with_attendees():
    from tests.unit.test_relayed_connectors import _gates, _turn

    src = RelayedConnectors(base_url="http://127.0.0.1:9", token=lambda: None)
    spec = ToolSpec(name="calendar_create", description="", parameters={"type": "object"}, effect=Effect.WRITE)
    specs, _, _ = src._parse({"connectors": [{"id": "calendar", "tools": [tool_view(spec)]}]})
    [relayed] = specs
    assert relayed.effect_for({"summary": "x"}) is Effect.WRITE
    assert relayed.effect_for({"summary": "x", "attendees": ["anne@example.com"]}) is Effect.SEND_EXTERNAL
    gates = _gates("auto")
    [own] = gates.action(ToolCall(id="c", name="calendar_create", arguments={"summary": "x"}), relayed, _turn())
    [invite] = gates.action(ToolCall(id="c", name="calendar_create",
                                     arguments={"summary": "x", "attendees": ["anne@example.com"]}), relayed, _turn())
    assert (own.strength, invite.strength) == ("standard", "strong")


# ═════════════════════════════ machine : jeton local ═════════════════════════════


def test_local_token_cannot_approve_a_relayed_strong_card(tmp_path):
    from tests.unit.api.test_approvals_routes import make_kernel_with_approvals
    from tests.unit.test_local_api import _api, _req

    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    api = _api(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: "local-owner"}, approvals=approvals,
                                                               local_api=api))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        turn = Turn(thread_id="th_1", index=0, input=InputCheck(text="écris"), channel="api", sender="local-owner")
        card = approvals.request(turn, ToolCall(id="c1", name="mail_send", arguments=ARGS),
                                 Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=RELAY_CARD_RULE,
                                         strength="strong"), None)
        status, body = _req(port, f"/v1/local/approvals/{card.id}/decide", token=api.token,
                            body={"decision": "approve", "scope": "once"})
        assert status == 403 and body["error"]["code"] == "strong_approval_required"
        assert "téléphone" in body["error"]["message"] and approvals.get(card.id).status == "pending"
        status, _ = _req(port, f"/v1/local/approvals/{card.id}/decide", token=api.token,
                         body={"decision": "reject", "scope": "once"})
        assert status == 200 and approvals.get(card.id).status == "rejected"  # refuser reste possible en local
    finally:
        httpd.shutdown()
