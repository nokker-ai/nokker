"""Carte forte d'une machine liée décidée dans la Mini App Telegram (lot X10, docs/LOKKAL.md §7.3 « attestation »), de bout
en bout et sans réseau externe : les vraies routes de nokker.ai (`make_handler` + `CloudLink`, boîte aux lettres en
mémoire), une machine liée (`RemoteApprovals`, noyau local en mémoire), un « téléphone » dont la clé P-256 signe
`<défi>.<décision>` et dont l'`initData` est signé par le jeton du bot.

Chemin : dépôt de la machine → preuve Telegram chez nokker.ai → attestation signée → relève par la machine → décision
appliquée (`decided_via` = `telegram-attested`).

Refus : `initData` faux, périmé, autre `telegram_user_id`, appareil d'un autre compte, de simple connexion, signature d'une
autre décision ou d'une autre clé ; attestation pour une autre machine, un autre compte, une autre carte, un autre défi,
une autre décision, expirée, rejouée (`jti`), signée par une autre clé, jeton d'accès pris pour une attestation,
drapeau `local.remote_approvals_attested: false`, carte prod ou de connecteur relayé ; la passkey (WebAuthn) marche
toujours ; le jeton de machine seul ne décide jamais."""
from __future__ import annotations

import base64
import json
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from nokker.api.server import make_handler
from nokker.approvals.errors import ProofInvalid
from nokker.approvals.memory import InMemoryApprovals
from nokker.cloud import http as cloud_http
from nokker.cloud.approvals import RelayedApprovalNotifier, RelayService
from nokker.cloud.attest import TelegramAttestor
from nokker.cloud.http import CloudLink
from nokker.cloud.link import ATTEST_TYP, LinkService, Signer, TokenError, challenge_sha256, verify_attestation
from nokker.core.turn import Decision, Gate, Verdict
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from tests.unit.api.test_server import make_kernel
from tests.unit.approvals.conftest import gen_keypair, sign_p1363, telegram_init_data
from tests.unit.approvals.test_memory_approvals import make_call, make_turn
from tests.unit.test_remote_approvals import MARIE, ORIGIN, PIERRE, SESS, FakeTenancy, World

BOT = "123456:test-bot-token"
TG_PIERRE, TG_MARIE, TG_STRANGER = 777, 888, 999
TG_OF = {str(TG_PIERRE): PIERRE, str(TG_MARIE): MARIE}


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    def send_with_button(self, chat, text, label, url):  # sendMessage seulement, jamais d'appel réel
        self.sent.append(("button", chat, text, label, url))

    def send_with_buttons(self, chat, text, buttons):
        self.sent.append(("buttons", chat, text, buttons))


class TgWorld(World):
    """Le monde de `test_remote_approvals` + un appareil Mini App relié au Telegram de Pierre et la clé de nokker.ai."""

    signer: Signer

    def setup_attest(self, signer: Signer) -> None:
        self.signer = signer
        self.tg_priv, spki = gen_keypair()
        dev = self.cloud_appr.enroll_device(PIERRE, "Mini App de Pierre", spki, telegram_user_id=str(TG_PIERRE))
        self.tg_device = self.cloud_appr.confirm_device_by_invitation(dev.id, 2)
        self.state["machine_id"], self.state["jwks"] = self.mid, signer.jwks()

    # ─── côté téléphone ───

    def init(self, tg_id: int = TG_PIERRE, **kw) -> str:
        return telegram_init_data(BOT, tg_id, **kw)

    def tg_proof(self, aid: str, decision: str = "approve", *, priv=None, device_id: str | None = None,
                 init: str | None = None, challenge: str | None = None) -> dict:
        challenge = challenge if challenge is not None else self.remote(aid)["challenge"]
        return {"device_id": device_id or self.tg_device.id,
                "signature": sign_p1363(priv or self.tg_priv, f"{challenge}.{decision}".encode()),
                "telegram_init_data": init or self.init()}

    def call_h(self, path, body=None, headers=None, sess=None):
        import urllib.error
        import urllib.request

        h = {"Content-Type": "application/json", **(headers or {})}
        if sess:
            h["Authorization"] = f"Bearer sess-{sess}"
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=h,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def mini_get(self, aid: str, init: str | None = None, mid: str | None = None):
        """Lecture de la carte par la Mini App : pas de session, l'initData fait foi."""
        return self.call_h(self.card_url(aid, mid), headers={"X-Telegram-Init-Data": init or self.init()})

    def mini_decide(self, aid: str, decision: str, proof: dict, init: str | None = None, sess: str | None = None,
                    mid: str | None = None):
        headers = {"X-Telegram-Init-Data": init or proof.get("telegram_init_data") or self.init()}
        return self.call_h(self.card_url(aid, mid) + "/decision", {"decision": decision, "proof": proof},
                           headers=headers, sess=sess)

    # ─── côté machine ───

    def relevees(self) -> list[dict]:
        status, body = self.call("/v1/gateway/approvals/decisions?wait=0", token=self.mt)
        assert status == 200
        return body["decisions"]

    def local_card(self, aid: str):
        with tenant_scope(INTERNAL_TENANT):
            return self.local.get(aid)

    def attest(self, aid: str, *, machine: str | None = None, account: str = PIERRE, decision: str = "approve",
               challenge: str | None = None, approval_id: str | None = None, signer: Signer | None = None,
               now: float | None = None, ttl_s: int = 120, device_id: str | None = None) -> str:
        challenge = challenge if challenge is not None else self.worker._cards[aid]
        return (signer or self.signer).attestation_token(
            account=account, machine=machine or self.mid, approval_id=approval_id or aid,
            challenge_sha256=challenge_sha256(challenge), decision=decision, device_id=device_id or self.tg_device.id,
            ttl_s=ttl_s, now=now)

    def forward(self, aid: str) -> None:
        """Une vraie preuve Telegram passe chez nokker.ai (carte `forwarded`) ; la machine reçoit ensuite, à la place de
        l'attestation de nokker.ai, celle que le test lui tend (relais fautif, jeton volé, rejeu…)."""
        assert self.mini_decide(aid, "approve", self.tg_proof(aid))[0] == 202

    def handle(self, aid: str, attestation: str, decision: str = "approve") -> str:
        """La machine reçoit cette décision relevée (comme `poll_once`, sans réseau de relève)."""
        return self.worker.handle(self.mt, {"approval_id": aid, "decision": decision,
                                            "proof": {"attestation": attestation, "device_id": self.tg_device.id,
                                                      "via": "telegram"}})


@pytest.fixture
def world():
    cloud_appr = InMemoryApprovals()
    links = LinkService()
    signer = Signer.generate()
    bot = FakeBot()
    notifier = RelayedApprovalNotifier(tenancy=FakeTenancy(), bot=bot, app_url=ORIGIN,
                                       mini_app_url=f"{ORIGIN}/miniapp/approve.html")
    cloud_http.configure(CloudLink(
        links=links, signer=signer,
        identity=lambda h: SESS.get(h.headers.get("Authorization", "")),
        tenant_of=lambda acc: {PIERRE: "tn_pierre", MARIE: "tn_marie"}.get(acc),
        router_base="http://127.0.0.1:9/v1", router_key=lambda: "x",
        approvals_relay=RelayService(), devices_of=lambda acc: cloud_appr.list_devices(acc),
        notify_approval=notifier,
        attestor=TelegramAttestor(signer=signer, bot_token=BOT, account_of=lambda tg: TG_OF.get(tg),
                                  devices_of=lambda acc: cloud_appr.list_devices(acc))))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), {}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    w = TgWorld(httpd.server_address[1], links, cloud_appr, [])
    w.setup_attest(signer)
    w.bot = bot
    yield w
    cloud_http.configure(None)
    httpd.shutdown()


def _deposit(w: TgWorld, rule: str = "effect:destructive", **args) -> str:
    with tenant_scope(INTERNAL_TENANT):
        card = w.local.request(make_turn(), make_call(**args),
                               Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=rule, reason="sans GO",
                                       strength="strong"), None)
    assert w.worker.sync_once() == 1
    return card.id


def _claims(token: str) -> tuple[dict, dict]:
    head, body, _ = token.split(".")

    def dec(s):
        return json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))

    return dec(head), dec(body)


def _pending_everywhere(w: TgWorld, aid: str) -> None:
    assert w.local_card(aid).status == "pending"
    assert w.remote(aid)["status"] == "pending"
    assert w.relevees() == []


# ═════════════════════════════════ chemin nominal ═════════════════════════════════


def test_telegram_miniapp_approves_machine_card_end_to_end(world):
    w = world
    aid = _deposit(w, path="/srv/share", size="50G")
    # 1. la Mini App lit la carte avec le seul initData (aucune session)
    status, card = w.mini_get(aid)
    assert status == 200 and card["approval_id"] == aid and card["status"] == "pending" and card["strength"] == "strong"
    # 2. elle signe le défi déposé par la machine et poste la preuve Telegram
    proof = w.tg_proof(aid, "approve")
    assert set(proof) == {"device_id", "signature", "telegram_init_data"}
    status, out = w.mini_decide(aid, "approve", proof)
    assert (status, out) == (202, {"status": "forwarded"})
    assert w.remote(aid)["status"] == "forwarded"
    # 3. la relève de la machine porte l'attestation (et non la preuve brute)
    (d,) = w.relevees()
    assert d["approval_id"] == aid and d["decision"] == "approve"
    assert set(d["proof"]) == {"attestation", "device_id", "via", "jti"} and d["proof"]["via"] == "telegram"
    head, claims = _claims(d["proof"]["attestation"])
    assert head["typ"] == ATTEST_TYP and head["alg"] == "ES256" and head["kid"] == w.signer.kid
    assert claims["typ"] == ATTEST_TYP and claims["iss"] == ORIGIN and claims["aud"] == w.mid
    assert claims["sub"] == PIERRE and claims["approval_id"] == aid and claims["decision"] == "approve"
    assert claims["via"] == "telegram" and claims["device_id"] == w.tg_device.id
    assert claims["challenge_sha256"] == challenge_sha256(card["challenge"])
    assert claims["args_sha256"] == json.loads(card["challenge"])["args_sha256"]
    assert 0 < claims["exp"] - claims["iat"] <= 120 and claims["jti"] == d["proof"]["jti"]
    # 4. la machine vérifie, décide et applique
    assert w.worker.poll_once(0) == ["approved"]
    w.wait_applied(1)
    local = w.local_card(aid)
    assert local.status in ("approved", "executed") and local.decided_via == "telegram-attested"
    assert w.applied == [aid]
    assert w.remote(aid)["status"] == "approved"
    # la jti est gardée dans l'état local (persistant) jusqu'à son expiration
    assert claims["jti"] in w.state["attest_jti"]


def test_telegram_miniapp_reject_end_to_end(world):
    w = world
    aid = _deposit(w, path="/srv/share")
    status, _ = w.mini_decide(aid, "reject", w.tg_proof(aid, "reject"))
    assert status == 202
    assert w.worker.poll_once(0) == ["rejected"]
    assert w.local_card(aid).status == "rejected" and w.local_card(aid).decided_via == "telegram-attested"
    assert w.remote(aid)["status"] == "rejected"


def test_session_plus_telegram_proof_also_works(world):
    w = world
    aid = _deposit(w, path="/srv/share")
    status, _ = w.mini_decide(aid, "approve", w.tg_proof(aid), sess="pierre")
    assert status == 202
    assert w.worker.poll_once(0) == ["approved"]


def test_notification_opens_the_card_in_the_mini_app(world):
    w = world
    aid = _deposit(w, path="/srv/share")
    deadline = time.time() + 2
    while not w.bot.sent and time.time() < deadline:
        time.sleep(0.02)
    kind, chat, text, buttons = w.bot.sent[0]
    assert kind == "buttons" and chat == "4242" and "atelier" in text
    assert buttons[0]["web_app"]["url"] == f"{ORIGIN}/miniapp/approve.html?machine={w.mid}&approval={aid}"
    assert buttons[1]["url"] == f"{ORIGIN}/approve/{w.mid}/{aid}"


# ═════════════════════════════ refus chez nokker.ai ═════════════════════════════


def test_refused_by_nokker_ai(world):
    w = world
    aid = _deposit(w, path="/srv/share")
    good = w.tg_proof(aid)

    # initData falsifié (mauvais jeton de bot), périmé
    forged = telegram_init_data("999:autre-bot", TG_PIERRE)
    assert w.mini_decide(aid, "approve", {**good, "telegram_init_data": forged}, sess="pierre")[0] == 401
    stale = w.init(auth_date=int(time.time()) - 3600)
    assert w.mini_decide(aid, "approve", {**good, "telegram_init_data": stale}, sess="pierre")[0] == 401
    # sans initData ni session : rien ne s'identifie ; un Telegram non relié non plus
    no_init = {k: v for k, v in good.items() if k != "telegram_init_data"}
    assert w.call_h(w.card_url(aid) + "/decision", {"decision": "approve", "proof": no_init})[0] == 401
    assert w.mini_get(aid, init=w.init(TG_STRANGER))[0] == 401
    # autre telegram_user_id que celui de l'appareil (initData valide d'un autre Telegram, session de Pierre)
    other = w.init(TG_STRANGER)
    assert w.mini_decide(aid, "approve", {**good, "telegram_init_data": other}, sess="pierre")[0] == 403
    # appareil d'un autre compte (celui de Marie, relié à SON Telegram) sur la machine de Pierre
    priv_m, spki_m = gen_keypair()
    dev_m = w.cloud_appr.confirm_device_by_invitation(
        w.cloud_appr.enroll_device(MARIE, "Mini App de Marie", spki_m, telegram_user_id=str(TG_MARIE)).id, 3)
    chal = w.remote(aid)["challenge"]
    proof_m = {"device_id": dev_m.id, "signature": sign_p1363(priv_m, f"{chal}.approve".encode()),
               "telegram_init_data": w.init(TG_MARIE)}
    assert w.mini_decide(aid, "approve", proof_m, sess="pierre")[0] == 403
    # (et Marie, avec son propre initData, ne voit pas la machine de Pierre : 404)
    assert w.mini_get(aid, init=w.init(TG_MARIE))[0] == 404
    # appareil inconnu
    assert w.mini_decide(aid, "approve", {**good, "device_id": "inconnu"}, sess="pierre")[0] == 403
    # signature d'une autre décision, d'une autre clé, d'un autre défi
    assert w.mini_decide(aid, "approve", w.tg_proof(aid, "reject"), sess="pierre")[0] == 403
    assert w.mini_decide(aid, "approve", w.tg_proof(aid, priv=gen_keypair()[0]), sess="pierre")[0] == 403
    assert w.mini_decide(aid, "approve", w.tg_proof(aid, challenge='{"x":1}'), sess="pierre")[0] == 403
    # décision inconnue, champs manquants
    assert w.mini_decide(aid, "peut-etre", good, sess="pierre")[0] == 400
    assert w.mini_decide(aid, "approve", {"telegram_init_data": w.init(), "device_id": w.tg_device.id},
                         sess="pierre")[0] == 400
    # carte inconnue
    assert w.mini_decide("nope", "approve", good, sess="pierre")[0] == 404
    _pending_everywhere(w, aid)  # rien n'est parti, la carte attend toujours un appareil légitime
    assert w.mini_decide(aid, "approve", good)[0] == 202  # et la bonne preuve passe encore


def test_login_only_and_pending_devices_cannot_attest(world):
    w = world
    aid = _deposit(w, path="/srv/share")
    chal = w.remote(aid)["challenge"]
    for confirm in ("email", "pending"):
        priv, spki = gen_keypair()
        dev = w.cloud_appr.enroll_device(PIERRE, confirm, spki, telegram_user_id=str(TG_PIERRE))
        if confirm == "email":
            w.cloud_appr.confirm_device_by_email_code(dev.id)
        proof = {"device_id": dev.id, "signature": sign_p1363(priv, f"{chal}.approve".encode()),
                 "telegram_init_data": w.init()}
        assert w.mini_decide(aid, "approve", proof)[0] == 403
    _pending_everywhere(w, aid)


def test_no_attestor_closes_the_telegram_path(world):
    w = world
    aid = _deposit(w, path="/srv/share")
    cloud_http._cloud.attestor = None
    assert w.mini_decide(aid, "approve", w.tg_proof(aid), sess="pierre")[0] == 404
    assert w.mini_get(aid)[0] == 401
    _pending_everywhere(w, aid)


def test_card_already_decided_is_not_attested(world):
    w = world
    aid = _deposit(w, path="/srv/share")
    proof = w.tg_proof(aid)
    assert w.mini_decide(aid, "approve", proof)[0] == 202
    assert w.mini_decide(aid, "approve", proof)[0] == 409  # déjà transmise : pas de seconde attestation
    assert len(w.relevees()) == 1


# ═════════════════════════════ refus chez la machine ═════════════════════════════


def test_machine_accepts_a_valid_attestation_once(world):
    w = world
    aid = _deposit(w, path="/srv/share")
    w.forward(aid)
    token = w.attest(aid)
    assert w.handle(aid, token) == "approved"
    w.wait_applied(1)
    assert w.local_card(aid).decided_via == "telegram-attested"
    assert w.handle(aid, token) == "pending"  # jti rejoué : refusé, la carte n'est pas ré-appliquée
    assert w.applied == [aid]


@pytest.mark.parametrize("case", ["machine", "account", "card", "challenge", "decision", "expired", "forged", "access",
                                  "typ", "wrong_ttl"])
def test_machine_refuses_bad_attestations(world, case):
    w = world
    aid = _deposit(w, path="/srv/share")
    other = _deposit(w, path="/srv/autre")
    w.forward(aid)
    decision = "approve"
    if case == "machine":
        token = w.attest(aid, machine="mc_" + "0" * 16)
    elif case == "account":
        token = w.attest(aid, account=MARIE)
    elif case == "card":
        token = w.attest(aid, approval_id=other)
    elif case == "challenge":
        token = w.attest(aid, challenge=w.worker._cards[other])
    elif case == "decision":
        token, decision = w.attest(aid, decision="reject"), "approve"  # signée « reject », relevée « approve »
    elif case == "expired":
        token = w.attest(aid, now=time.time() - 600)
    elif case == "forged":  # autre clé, même kid : la signature ne vérifie pas contre le JWKS du lien
        token = w.attest(aid, signer=Signer.generate(kid=w.signer.kid))
    elif case == "access":  # un jeton d'accès de nokker.ai n'est pas une attestation
        token = w.signer.access_token(PIERRE, "tn_pierre", machine=w.mid)
    elif case == "typ":  # bonnes claims, mais l'en-tête dit JWT
        _, claims = _claims(w.attest(aid))
        token = w.signer.sign(claims)
    else:  # durée de vie > 120 s
        _, claims = _claims(w.attest(aid))
        token = w.signer.sign({**claims, "exp": claims["iat"] + 3600}, typ=ATTEST_TYP)
    assert w.handle(aid, token, decision) == "pending"
    assert w.local_card(aid).status == "pending" and w.local_card(other).status == "pending"
    assert w.applied == []
    # la carte revient en attente chez nokker.ai avec la raison : le bon appareil peut encore signer
    view = w.remote(aid)
    assert view["status"] == "pending" and "preuve refusée par la machine" in (view["error"] or "")
    assert w.handle(aid, w.attest(aid)) == "approved"  # une bonne attestation passe ensuite


def test_replayed_jti_is_refused_and_remembered_until_expiry(world):
    w = world
    now = w.now[0]
    w.worker._burn_jti("jti-1", now + 100)
    assert "jti-1" in w.state["attest_jti"]
    with pytest.raises(ProofInvalid):
        w.worker._burn_jti("jti-1", now + 100)
    w.now[0] = now + 101  # expirée : sortie de la mémoire (elle serait refusée pour son âge de toute façon)
    w.worker._burn_jti("jti-2", w.now[0] + 100)
    assert "jti-1" not in w.state["attest_jti"] and "jti-2" in w.state["attest_jti"]


def test_flag_disabled_refuses_every_attestation(world):
    w = world
    aid = _deposit(w, path="/srv/share")
    w.worker.attested = False  # local.remote_approvals_attested: false
    status, _ = w.mini_decide(aid, "approve", w.tg_proof(aid))
    assert status == 202  # nokker.ai ignore ce réglage local
    assert w.worker.poll_once(0) == ["pending"]
    assert w.local_card(aid).status == "pending" and w.applied == []
    view = w.remote(aid)
    assert view["status"] == "pending" and "remote_approvals_attested" in view["error"]
    w.worker.attested = True  # réactivé : une nouvelle preuve passe
    assert w.mini_decide(aid, "approve", w.tg_proof(aid))[0] == 202
    assert w.worker.poll_once(0) == ["approved"]


@pytest.mark.parametrize("rule", ["ops:prod", "ops:prod:ssh", "pages:publish", "permissions:relayed_connector"])
def test_attestation_never_decides_device_key_floor_cards(world, rule):
    w = world
    aid = _deposit(w, rule=rule, path="/srv/share")
    w.forward(aid)
    assert w.handle(aid, w.attest(aid)) == "pending"
    assert w.local_card(aid).status == "pending" and w.applied == []
    assert "clé d'appareil" in w.remote(aid)["error"] or "passkey" in w.remote(aid)["error"]


def test_attestation_after_machine_restart_uses_a_live_nonce_only(world):
    """Défi de nokker.ai (carte en base après redémarrage) : le nonce doit être encore réservé ICI pour CETTE carte."""
    w = world
    aid = _deposit(w, path="/srv/share")
    w.forward(aid)
    challenge = w.worker._cards[aid]
    token = w.attest(aid, challenge=challenge)
    w.worker._cards.clear()  # redémarrage : le défi déposé n'est plus en mémoire
    d = {"approval_id": aid, "decision": "approve", "challenge": challenge,
         "proof": {"attestation": token, "device_id": w.tg_device.id, "via": "telegram"}}
    assert w.worker.handle(w.mt, d) == "approved"


# ═════════════════ rien d'autre ne change : passkey, jeton de machine seul ═════════════════


def test_direct_webauthn_still_works(world):
    w = world
    aid = _deposit(w, path="/srv/share")
    status, _ = w.decide(aid, "approve", w.sign(aid))
    assert status == 202
    (d,) = w.relevees()
    assert "attestation" not in d["proof"] and "webauthn_signature" in d["proof"]
    assert w.worker.poll_once(0) == ["approved"]
    assert w.local_card(aid).decided_via == "webauthn"


def test_machine_token_alone_never_decides(world):
    w = world
    aid = _deposit(w, path="/srv/share")
    # le jeton de machine ne passe pas la porte du compte, avec ou sans preuve
    no_init = {k: v for k, v in w.tg_proof(aid).items() if k != "telegram_init_data"}
    assert w.call(w.card_url(aid) + "/decision", {"decision": "approve", "proof": no_init}, token=w.mt)[0] == 401
    assert w.call(w.card_url(aid), token=w.mt)[0] == 401
    assert w.call(w.card_url(aid) + "/decision", {"decision": "approve", "proof": {"attestation": "x"}},
                  token=w.mt)[0] == 401
    # ni une attestation fabriquée sans la clé de nokker.ai
    assert w.mini_decide(aid, "approve", w.tg_proof(aid))[0] == 202  # seule une vraie preuve fait partir une décision
    bogus = w.attest(aid, signer=Signer.generate(kid=w.signer.kid))
    assert w.handle(aid, bogus) == "pending"
    # ni une preuve sans attestation ni signature d'appareil
    assert w.worker.handle(w.mt, {"approval_id": aid, "decision": "approve", "proof": {"via": "telegram"}}) == "pending"
    assert w.local_card(aid).status == "pending" and w.applied == []


# ═════════════════════════ l'attestation, vue par la vérification pure ═════════════════════════


def test_verify_attestation_contract():
    signer = Signer.generate()
    jwks, ch = signer.jwks(), '{"approval_id":"a1","aud":"nokker-approval"}'
    kw = dict(machine="mc_" + "1" * 16, account="us_x", approval_id="a1", challenge=ch)

    def mint(**over):
        base = dict(account="us_x", machine=kw["machine"], approval_id="a1", challenge_sha256=challenge_sha256(ch),
                    decision="approve", device_id="d1")
        return signer.attestation_token(**{**base, **over})

    claims = verify_attestation(mint(), jwks, issuer=signer.issuer, **kw)
    assert claims["via"] == "telegram" and claims["decision"] == "approve"
    assert claims["exp"] - claims["iat"] == 120
    assert signer.attestation_token(account="us_x", machine=kw["machine"], approval_id="a1",
                                    challenge_sha256="0", decision="approve", device_id="d", ttl_s=9999)
    _, capped = _claims(mint(ttl_s=9999))
    assert capped["exp"] - capped["iat"] == 120  # jamais plus de 120 s, quoi qu'on demande
    for bad in (dict(machine="mc_" + "2" * 16), dict(account="us_y"), dict(approval_id="a2"),
                dict(challenge='{"approval_id":"a1","aud":"nokker-approval","nonce":"autre"}')):
        with pytest.raises(TokenError):
            verify_attestation(mint(), jwks, issuer=signer.issuer, **{**kw, **bad})
    with pytest.raises(TokenError):
        verify_attestation(mint(), jwks, issuer="https://autre.example", **kw)
    with pytest.raises(TokenError):
        verify_attestation(mint(now=time.time() - 300), jwks, issuer=signer.issuer, **kw)
    with pytest.raises(TokenError):
        verify_attestation("pas.un.jeton", jwks, issuer=signer.issuer, **kw)
    with pytest.raises(TokenError):
        verify_attestation(mint(), {}, issuer=signer.issuer, **kw)
