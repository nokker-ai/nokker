"""Approbations fortes depuis le téléphone (lot X10, docs/LOKKAL.md §7.3), de bout en bout et sans réseau externe :
les vraies routes de nokker.ai (`make_handler` + `CloudLink`, boîte aux lettres en mémoire), une machine liée dont le
noyau local (InMemoryApprovals) dépose sa carte forte, un « téléphone » qui signe une vraie assertion WebAuthn avec la
clé de son appareil, et le fil de la machine (`RemoteApprovals`) qui relève la preuve, la vérifie et applique la carte.

Cas de sécurité : preuve forgée (relais fautif, compte volé), appareil inconnu ou d'un autre compte, rejeu (même carte,
autre carte), preuve d'un autre défi ou d'une autre décision, mauvaise machine, mauvais compte, expiration (défi, carte),
double décision, clé logicielle sur carte forte, appareil de simple connexion, UV absent, mauvaise origine ou rpId, clé
d'appareil changée, jeton de machine seul."""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from nokker.api.server import make_handler
from nokker.approvals.memory import InMemoryApprovals
from nokker.cloud import http as cloud_http
from nokker.cloud.approvals import RelayedApprovalNotifier, RelayService
from nokker.cloud.http import CloudLink
from nokker.cloud.link import LinkService, Signer
from nokker.local.remote_approvals import RemoteApprovals
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from tests.unit.api.test_server import make_kernel
from tests.unit.approvals.conftest import gen_keypair, sign_p1363
from tests.unit.approvals.test_memory_approvals import make_call, make_turn, make_verdict

ORIGIN = "https://app.nokker.ai"
RP_ID = "app.nokker.ai"
PIERRE, MARIE = "us_pierre", "us_marie"
SESS = {"Bearer " + "sess-" + "pierre": PIERRE, "Bearer " + "sess-" + "marie": MARIE}  # jetons de test, morcelés


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def webauthn_proof(priv, device_id: str, challenge: str, decision: str, *, origin: str = ORIGIN, rp_id: str = RP_ID,
                   uv: bool = True) -> dict[str, str]:
    """Assertion WebAuthn telle que la rend le navigateur (apps/web `signDecisionWithPasskey`) : challenge de
    l'assertion = sha256("<challenge>.<decision>"), signature DER sur authenticatorData ‖ sha256(clientDataJSON)."""
    auth_data = hashlib.sha256(rp_id.encode()).digest() + bytes([0x01 | (0x04 if uv else 0)]) + (0).to_bytes(4, "big")
    cdj = json.dumps({"type": "webauthn.get", "origin": origin, "crossOrigin": False,
                      "challenge": _b64u(hashlib.sha256(f"{challenge}.{decision}".encode()).digest())},
                     separators=(",", ":")).encode()
    sig = priv.sign(auth_data + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
    return {"device_id": device_id, "webauthn_client_data_json": _b64u(cdj),
            "webauthn_authenticator_data": _b64u(auth_data), "webauthn_signature": _b64u(sig)}


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    def send_with_button(self, chat, text, label, url):  # sendMessage seulement, jamais d'appel réel
        self.sent.append((chat, text, label, url))


class FakeTenancy:
    def account(self, account_id):
        return type("A", (), {"lang": "fr"})()

    def identities_of(self, account_id, kind):
        return ["4242"] if (account_id, kind) == (PIERRE, "telegram") else []


class World:
    """nokker.ai (routes réelles) + une machine liée de Pierre + son noyau local + son fil d'approbations relayées."""

    def __init__(self, port: int, links: LinkService, cloud_appr: InMemoryApprovals, notified: list) -> None:
        self.port, self.links, self.cloud_appr, self.notified = port, links, cloud_appr, notified
        self.mid, self.mt = self.link_machine("atelier", PIERRE, "tn_pierre")
        self.now = [time.time()]
        self.local = InMemoryApprovals(clock=lambda: self.now[0])
        self.applied: list[str] = []
        self.priv, spki = gen_keypair()
        dev = cloud_appr.enroll_device(PIERRE, "iPhone de Pierre", spki)
        self.device = cloud_appr.confirm_device_by_invitation(dev.id, 1)  # appareil qui décide
        self.state: dict = {}
        self.worker = self.make_worker(self.mt)

    def link_machine(self, name: str, account: str, tenant: str) -> tuple[str, str]:
        d = self.links.device(name)
        self.links.approve(d["user_code"], account, tenant)
        tok = self.links.token(d["device_code"])
        return tok["machine_id"], tok["machine_token"]

    def make_worker(self, token: str, account: str = PIERRE) -> RemoteApprovals:
        def apply(a):
            self.applied.append(a.id)
            if a.status == "approved":
                self.local.mark_executed(a.id, "fait")

        return RemoteApprovals(base_url=f"http://127.0.0.1:{self.port}", origin=ORIGIN, token=lambda: token,
                               account=lambda: account, approvals=self.local, apply=apply, state=self.state,
                               client=httpx.Client(timeout=10.0), scrub=lambda s: s.replace("hunter2", "[secret]"),
                               clock=lambda: self.now[0])

    def strong_card(self, **args):
        with tenant_scope(INTERNAL_TENANT):
            return self.local.request(make_turn(), make_call(**args), make_verdict("strong"), None)

    def call(self, path, body=None, sess=None, token=None):
        headers = {"Content-Type": "application/json"}
        if sess:
            headers["Authorization"] = f"Bearer sess-{sess}"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def card_url(self, aid: str, mid: str | None = None) -> str:
        return f"/v1/link/machines/{mid or self.mid}/approvals/{aid}"

    def remote(self, aid: str, sess: str = "pierre", mid: str | None = None) -> dict:
        status, body = self.call(self.card_url(aid, mid), sess=sess)
        assert status == 200, body
        return body

    def decide(self, aid: str, decision: str, proof: dict, sess: str = "pierre", mid: str | None = None):
        return self.call(self.card_url(aid, mid) + "/decision", {"decision": decision, "proof": proof}, sess=sess)

    def sign(self, aid: str, decision: str = "approve", **kw) -> dict:
        return webauthn_proof(self.priv, self.device.id, self.remote(aid)["challenge"], decision, **kw)

    def wait_applied(self, n: int) -> None:
        deadline = time.time() + 5
        while len(self.applied) < n and time.time() < deadline:
            time.sleep(0.02)

    def local_status(self, aid: str) -> str:
        with tenant_scope(INTERNAL_TENANT):
            return self.local.get(aid).status


@pytest.fixture
def world():
    cloud_appr = InMemoryApprovals()
    links = LinkService()
    notified: list = []
    bot = FakeBot()
    notifier = RelayedApprovalNotifier(tenancy=FakeTenancy(), bot=bot, app_url=ORIGIN)

    def notify(machine, card):
        notified.append((machine.id, card.approval_id))
        notifier(machine, card)

    cloud_http.configure(CloudLink(
        links=links, signer=Signer.generate(),
        identity=lambda h: SESS.get(h.headers.get("Authorization", "")),
        tenant_of=lambda acc: {PIERRE: "tn_pierre", MARIE: "tn_marie"}.get(acc),
        router_base="http://127.0.0.1:9/v1", router_key=lambda: "x",
        approvals_relay=RelayService(), devices_of=lambda acc: cloud_appr.list_devices(acc), notify_approval=notify))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), {}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    w = World(httpd.server_address[1], links, cloud_appr, notified)
    w.bot = bot
    yield w
    cloud_http.configure(None)
    httpd.shutdown()


def _deposit(w: World, **args) -> str:
    card = w.strong_card(**args)
    assert w.worker.sync_once() == 1
    return card.id


# ═════════════════════════════════ chemin nominal ═════════════════════════════════


def test_phone_approves_strong_card_end_to_end(world):
    w = world
    aid = _deposit(w, path="/srv/share", size="50G", note="mot de passe hunter2")
    r = w.remote(aid)
    assert r["status"] == "pending" and r["strength"] == "strong" and r["approval_id"] == aid
    assert r["title"] == "infra_lvextend : /srv/share" and r["expires_at"].endswith("+00:00")
    assert json.loads(r["challenge"])["approval_id"] == aid
    status, listing = w.call(f"/v1/link/machines/{w.mid}/approvals", sess="pierre")
    assert status == 200 and [a["approval_id"] for a in listing["approvals"]] == [aid]
    deadline = time.time() + 2
    while not w.bot.sent and time.time() < deadline:
        time.sleep(0.02)
    chat, text, label, url = w.bot.sent[0]  # Telegram : sendMessage avec lien vers la page de décision
    assert chat == "4242" and "atelier" in text and url == f"{ORIGIN}/approve/{w.mid}/{aid}"

    assert w.decide(aid, "approve", w.sign(aid)) == (202, {"status": "forwarded"})
    assert w.remote(aid)["status"] == "forwarded"
    assert w.applied == []  # nokker.ai n'a rien décidé : rien ne s'exécute avant la vérification par la machine
    assert w.worker.poll_once(0) == ["approved"]
    w.wait_applied(1)
    assert w.applied == [aid] and w.local_status(aid) == "executed"
    deadline = time.time() + 2
    while w.remote(aid).get("outcome") is None and time.time() < deadline:
        time.sleep(0.02)
    final = w.remote(aid)
    assert final["status"] == "approved" and final["outcome"] == "executed"
    with tenant_scope(INTERNAL_TENANT):
        card = w.local.get(aid)
    assert card.decided_by == "local-owner" and card.decided_via == "webauthn"
    ev = [e for e in w.local._events if e[0] == aid and e[1] == "approved"][0][2]
    assert ev["remote"] is True and ev["device_id"] == w.device.id and ev["account"] == PIERRE
    assert w.notified == [(w.mid, aid)]


def test_phone_rejects_and_reason_is_scrubbed(world):
    w = world
    card = w.strong_card(command="rm -rf /srv/data")
    with tenant_scope(INTERNAL_TENANT):
        w.local._approvals[card.id].reason = "efface avec le mot de passe hunter2"
    assert w.worker.sync_once() == 1
    assert "hunter2" not in json.dumps(w.remote(card.id))  # caviardé par la machine avant de partir
    assert w.decide(card.id, "reject", w.sign(card.id, "reject"))[0] == 202
    assert w.worker.poll_once(0) == ["rejected"]
    assert w.remote(card.id)["status"] == "rejected" and w.local_status(card.id) == "rejected"


def test_card_decided_locally_while_relayed_reports_its_status(world):
    w = world
    aid = _deposit(w)
    with tenant_scope(INTERNAL_TENANT):
        w.local.decide_local(aid, "reject", "local-owner")
    w.worker.sync_once()
    assert w.remote(aid)["status"] == "rejected"
    assert w.decide(aid, "approve", w.sign(aid))[0] == 409  # plus rien à signer


def test_only_strong_cards_are_relayed_and_unlinked_machine_sends_nothing(world):
    w = world
    with tenant_scope(INTERNAL_TENANT):
        w.local.request(make_turn(), make_call(), make_verdict("standard"), None)
    assert w.worker.sync_once() == 0
    w.strong_card()
    assert w.make_worker("").sync_once() == 0  # pas de jeton de machine : rien ne part
    assert w.make_worker(w.mt, account=None).sync_once() == 0


# ═════════════════════════════════ sécurité ═════════════════════════════════


def test_forged_proof_is_refused_and_does_not_burn_the_challenge(world):
    """Relais fautif ou compte volé : une preuve signée par une autre clé, au nom du vrai appareil, est refusée par la
    machine ; la carte revient à `pending` avec la raison, et l'appareil légitime peut encore signer."""
    w = world
    aid = _deposit(w)
    thief, _ = gen_keypair()
    forged = webauthn_proof(thief, w.device.id, w.remote(aid)["challenge"], "approve")
    assert w.decide(aid, "approve", forged)[0] == 202
    assert w.worker.poll_once(0) == ["pending"]
    r = w.remote(aid)
    assert r["status"] == "pending" and "signature WebAuthn invalide" in r["error"]
    assert w.applied == [] and w.local_status(aid) == "pending"
    assert w.decide(aid, "approve", w.sign(aid))[0] == 202
    assert w.worker.poll_once(0) == ["approved"]
    w.wait_applied(1)
    assert w.applied == [aid]


def test_stolen_session_with_own_key_or_unknown_device_cannot_approve(world):
    w = world
    aid = _deposit(w)
    thief, _ = gen_keypair()
    for device_id in ("dev-inconnu", w.device.id + "-x"):
        w.decide(aid, "approve", webauthn_proof(thief, device_id, w.remote(aid)["challenge"], "approve"))
        assert w.worker.poll_once(0) == ["pending"]
        assert "appareil inconnu" in w.remote(aid)["error"]
    # appareil d'un AUTRE compte, même enrôlé et approuvé sur nokker.ai : pas dans la liste de la machine de Pierre
    priv_m, spki_m = gen_keypair()
    dev_m = w.cloud_appr.confirm_device_by_invitation(w.cloud_appr.enroll_device(MARIE, "Marie", spki_m).id, 2)
    w.decide(aid, "approve", webauthn_proof(priv_m, dev_m.id, w.remote(aid)["challenge"], "approve"))
    assert w.worker.poll_once(0) == ["pending"] and "appareil inconnu" in w.remote(aid)["error"]
    assert w.applied == [] and w.local_status(aid) == "pending"


def test_machine_token_alone_never_decides(world):
    """Le jeton de machine n'ouvre pas les routes du compte, et une « décision » sans preuve valable n'est jamais
    appliquée par la machine, même si nokker.ai (fautif) la lui rend."""
    w = world
    aid = _deposit(w)
    assert w.call(w.card_url(aid) + "/decision", {"decision": "approve", "proof": {}}, token=w.mt)[0] == 401
    assert w.decide(aid, "approve", {})[0] == 400  # sans preuve : refusé dès nokker.ai
    for d in ({"approval_id": aid, "decision": "approve", "proof": {}},
              {"approval_id": aid, "decision": "approve", "proof": {"device_id": w.device.id}}):
        assert w.worker.handle(w.mt, d) == "pending"
    assert w.applied == [] and w.local_status(aid) == "pending"


def test_replay_same_card_and_other_card(world):
    w = world
    aid = _deposit(w)
    proof = w.sign(aid)
    w.decide(aid, "approve", proof)
    assert w.worker.poll_once(0) == ["approved"]
    w.wait_applied(1)
    # nokker.ai ne rouvre pas une carte décidée ; un nokker.ai fautif qui rejoue la décision : appliquée une seule fois
    assert w.decide(aid, "approve", proof)[0] == 409
    assert w.worker.handle(w.mt, {"approval_id": aid, "decision": "approve", "proof": proof}) == "approved"
    time.sleep(0.1)
    assert w.applied == [aid]
    # la même preuve, présentée pour une AUTRE carte : défi d'une autre approbation
    other = _deposit(w)
    w.decide(other, "approve", proof)
    assert w.worker.poll_once(0) == ["pending"]
    assert "challenge clientDataJSON" in w.remote(other)["error"] and w.local_status(other) == "pending"


def test_proof_of_another_challenge_or_another_decision(world):
    w = world
    aid = _deposit(w)
    ch = json.loads(w.remote(aid)["challenge"])
    stale = json.dumps({**ch, "nonce": "ancien-nonce"}, sort_keys=True, separators=(",", ":"))
    w.decide(aid, "approve", webauthn_proof(w.priv, w.device.id, stale, "approve"))
    assert w.worker.poll_once(0) == ["pending"]  # signé sur un défi qui n'est pas celui déposé
    w.decide(aid, "approve", w.sign(aid, "reject"))  # un refus signé, présenté comme une approbation
    assert w.worker.poll_once(0) == ["pending"]
    assert w.applied == [] and w.local_status(aid) == "pending"


def test_wrong_machine_and_wrong_account(world):
    w = world
    aid = _deposit(w)
    mid2, mt2 = w.link_machine("portable", PIERRE, "tn_pierre")
    assert w.call(w.card_url(aid, mid2), sess="pierre")[0] == 404  # la carte n'est pas sur cette machine
    assert w.decide(aid, "approve", w.sign(aid), mid=mid2)[0] == 404
    assert w.call(w.card_url(aid), sess="marie")[0] == 404  # machine d'un autre compte : 404, pas d'oracle
    assert w.call(f"/v1/link/machines/{w.mid}/approvals", sess="marie")[0] == 404
    assert w.decide(aid, "approve", w.sign(aid), sess="marie")[0] == 404
    w.decide(aid, "approve", w.sign(aid))
    other_local, other_applied = InMemoryApprovals(), []
    other = RemoteApprovals(base_url=f"http://127.0.0.1:{w.port}", origin=ORIGIN, token=lambda: mt2,
                            account=lambda: PIERRE, approvals=other_local, apply=other_applied.append,
                            client=httpx.Client(timeout=10.0))
    assert other.poll_once(0) == []  # l'autre machine ne relève pas les décisions de celle-ci
    assert w.call(f"/v1/gateway/approvals/{aid}/result", {"status": "approved"}, token=mt2)[0] == 404
    # une décision rejouée vers une machine qui n'a pas cette carte : refusée, jamais appliquée
    assert other.handle(mt2, {"approval_id": aid, "decision": "approve", "proof": w.sign(aid)}) == "expired"
    assert other_applied == [] and w.applied == [] and w.local_status(aid) == "pending"
    assert w.worker.poll_once(0) == ["approved"]


def test_expired_challenge_and_expired_card(world):
    w = world
    aid = _deposit(w)
    proof = w.sign(aid)
    w.decide(aid, "approve", proof)
    w.now[0] += w.worker.relay_ttl_s + 1  # défi relayé échu (1 h) : la preuve ne passe plus
    assert w.worker.poll_once(0) == ["pending"]
    assert "nonce" in w.remote(aid)["error"] and w.local_status(aid) == "pending"
    w.now[0] += 24 * 3600  # la carte elle-même a expiré
    w.decide(aid, "approve", proof)  # nokker.ai a encore une carte « pending » à son horloge réelle
    assert w.worker.poll_once(0) == ["expired"]
    assert w.remote(aid)["status"] == "expired" and w.applied == []


def test_expired_on_nokker_ai_refuses_the_decision(world):
    w = world
    aid = _deposit(w)
    relay = cloud_http._cloud.approvals_relay
    row = relay.store.rows[(w.mid, aid)]
    row.expires_at = time.time() - 1
    assert w.remote(aid)["status"] == "expired"
    assert w.decide(aid, "approve", w.sign(aid))[0] == 409


def test_double_decision_is_refused_while_forwarded(world):
    w = world
    aid = _deposit(w)
    assert w.decide(aid, "approve", w.sign(aid))[0] == 202
    assert w.decide(aid, "reject", w.sign(aid, "reject"))[0] == 409  # une seule preuve en vol
    assert w.worker.poll_once(0) == ["approved"]
    w.wait_applied(1)
    assert w.worker.poll_once(0) == [] and w.applied == [aid]


def test_device_rules_same_as_local(world):
    """Clé logicielle P1363 (sans biométrie) sur carte forte, appareil de simple connexion, UV absent, origine ou rpId
    d'un autre site : refusés, comme en local."""
    w = world
    aid = _deposit(w)
    ch = w.remote(aid)["challenge"]
    soft = {"device_id": w.device.id, "signature": sign_p1363(w.priv, f"{ch}.approve".encode())}
    login_priv, login_spki = gen_keypair()
    login_dev = w.cloud_appr.confirm_device_by_email_code(w.cloud_appr.enroll_device(PIERRE, "PC", login_spki).id)
    for proof, why in ((soft, "clé logicielle refusée"),
                       (webauthn_proof(login_priv, login_dev.id, ch, "approve"), "simple connexion"),
                       (webauthn_proof(w.priv, w.device.id, ch, "approve", uv=False), "flag UV"),
                       (webauthn_proof(w.priv, w.device.id, ch, "approve", origin="https://atelier.nokker.app"),
                        "origine clientDataJSON inattendue"),
                       (webauthn_proof(w.priv, w.device.id, ch, "approve", rp_id="nokker.app"), "rpIdHash")):
        assert w.decide(aid, "approve", proof)[0] == 202
        assert w.worker.poll_once(0) == ["pending"], why
        assert why in w.remote(aid)["error"]
    assert w.applied == [] and w.local_status(aid) == "pending"
    status, devices = w.call("/v1/gateway/devices", token=w.mt)
    assert status == 200 and {d["device_id"]: d["can_decide"] for d in devices["devices"]} == {
        w.device.id: True, login_dev.id: False}
    assert all("private" not in json.dumps(d) for d in devices["devices"])


def test_device_key_changed_on_nokker_ai_is_refused(world):
    """La machine épingle la clé d'un appareil à sa première vue : un nokker.ai qui rend une autre clé pour le même
    appareil (la sienne) ne fait rien approuver."""
    w = world
    aid = _deposit(w)
    w.decide(aid, "approve", w.sign(aid, "reject"))  # une première relève voit l'appareil (preuve refusée ici)
    assert w.worker.poll_once(0) == ["pending"] and w.device.id in w.state["device_pins"]
    evil, evil_spki = gen_keypair()
    w.cloud_appr._devices[w.device.id].public_key_spki = evil_spki
    w.worker._devices_at = 0.0  # cache échu : relu chez nokker.ai
    w.decide(aid, "approve", webauthn_proof(evil, w.device.id, w.remote(aid)["challenge"], "approve"))
    assert w.worker.poll_once(0) == ["pending"]
    assert "clé de l'appareil changée" in w.remote(aid)["error"] and w.applied == []


def test_deposit_validation_and_limits(world):
    w = world
    base = {"approval_id": "a1", "title": "t", "reason": "r", "strength": "strong", "challenge": "{}",
            "expires_at": time.time() + 60}
    assert w.call("/v1/gateway/approvals", base, token="faux")[0] == 401
    assert w.call("/v1/gateway/approvals", base, token=w.mt)[0] == 201
    assert w.call("/v1/gateway/approvals", base, token=w.mt)[0] == 200  # redépôt : nouveau défi, pas de 2e notification
    for bad in ({"approval_id": "../x"}, {"strength": "faible"}, {"expires_at": time.time() - 1},
                {"expires_at": time.time() + 3 * 86400}, {"challenge": ""}, {"title": ""}):
        assert w.call("/v1/gateway/approvals", {**base, **bad}, token=w.mt)[0] == 400, bad
    long = w.call("/v1/gateway/approvals", {**base, "approval_id": "a2", "reason": "x" * 5000}, token=w.mt)
    assert long[0] == 201 and len(w.remote("a2")["reason"]) == 600
    assert w.call("/v1/gateway/approvals/a1/result", {"status": "pending"}, token=w.mt)[0] == 409
    assert w.call("/v1/gateway/approvals/a1/result", {"status": "approved", "outcome": "boom"}, token=w.mt)[0] == 400
    assert w.call("/v1/gateway/approvals/a1/result", {"status": "rejected"}, token=w.mt)[0] == 200
    assert w.call("/v1/gateway/approvals", base, token=w.mt)[0] == 409  # décidée : on ne la rouvre pas
    assert w.call("/v1/gateway/approvals/a1/result", {"status": "approved"}, token=w.mt)[0] == 409
    deadline = time.time() + 2
    while len(w.notified) < 2 and time.time() < deadline:
        time.sleep(0.02)
    assert w.notified == [(w.mid, "a1"), (w.mid, "a2")]


def test_long_poll_wakes_on_decision(world):
    w = world
    aid = _deposit(w)
    proof = w.sign(aid)
    out: list = []
    t = threading.Thread(target=lambda: out.append(w.worker.poll_once(10)))
    t0 = time.monotonic()
    t.start()
    time.sleep(0.3)
    w.decide(aid, "approve", proof)
    t.join(5)
    assert out == [["approved"]] and time.monotonic() - t0 < 5


def test_telegram_notifier_without_pairing_sends_nothing():
    bot = FakeBot()
    card = type("C", (), {"account": MARIE, "machine_id": "mc_0123456789abcdef", "approval_id": "a1",
                          "title": "t", "reason": ""})()
    RelayedApprovalNotifier(tenancy=FakeTenancy(), bot=bot)(type("M", (), {"name": "x"})(), card)
    assert bot.sent == []


def test_background_threads_deposit_on_new_card_and_apply(world):
    """Comme `serve` : le notificateur du noyau en chaîne avec le fil de la machine ; une carte forte posée part sans
    attendre le passage suivant, la preuve est relevée en fond et la carte appliquée."""
    from nokker.approvals.notify import ChainNotifier, NullNotifier

    w = world
    w.worker.scan_s, w.worker.poll_wait_s = 30.0, 1.0
    w.local.notifier = ChainNotifier((NullNotifier(), w.worker))
    w.worker.start()
    try:
        aid = w.strong_card().id
        deadline = time.time() + 5
        while w.call(w.card_url(aid), sess="pierre")[0] != 200 and time.time() < deadline:
            time.sleep(0.05)
        assert w.decide(aid, "approve", w.sign(aid))[0] == 202
        w.wait_applied(1)
        assert w.applied == [aid] and w.local_status(aid) == "executed"
    finally:
        w.worker.stop()


# ═══════════════════════ ce que voient les clients locaux (éditeurs) ═══════════════════════


def test_a_deposited_strong_card_is_marked_forwarded_and_announced_once(world):
    """Un éditeur (VS Code, ACP) doit dire VRAI : « attend ton téléphone » seulement si la carte est partie chez
    nokker.ai, sinon « à décider dans le terminal ». Le noyau le signale par carte : `forwarded()` et l'événement
    `approval.forwarded {approval_id, thread_id}`, publié au dépôt (une fois)."""
    w = world
    seen: list = []
    w.local.on_event = lambda kind, data: seen.append((kind, data))
    card = w.strong_card()
    unlinked = w.make_worker("")
    assert unlinked.sync_once() == 0 and not unlinked.forwarded(card.id)  # machine non liée : rien ne part
    assert not w.worker.forwarded(card.id)
    assert w.worker.sync_once() == 1
    assert w.worker.forwarded(card.id)
    w.worker.sync_once()  # déjà déposée : pas de second événement
    forwarded = [d for k, d in seen if k == "approval.forwarded"]
    assert forwarded == [{"approval_id": card.id, "thread_id": card.thread_id}]


def test_local_approvals_list_says_which_strong_cards_went_to_the_phone(tmp_path):
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
        with tenant_scope(INTERNAL_TENANT):
            strong = approvals.request(make_turn(), make_call(), make_verdict("strong"), None)
            standard = approvals.request(make_turn(), make_call(), make_verdict("standard"), None)

        def listed() -> dict[str, bool]:
            status, body = _req(port, "/v1/local/approvals", token=api.token)
            assert status == 200
            return {a["id"]: a["forwarded"] for a in body["approvals"]}

        assert listed() == {strong.id: False, standard.id: False}  # pas de relais (machine non liée)
        api.remote_approvals = type("Relay", (), {"forwarded": lambda self, aid: aid == strong.id})()
        assert listed() == {strong.id: True, standard.id: False}
    finally:
        httpd.shutdown()
