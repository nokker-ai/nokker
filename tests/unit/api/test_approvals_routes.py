"""Routes Approbations fortes de `api/server.py` (docs/APPROVALS.md §5, docs/CLIENT-API.md) — vraies
requêtes HTTP en boucle locale (même esprit que `test_server.py`) : enrôlement d'appareil, défi,
décision signée, exécution + continuation, sessions, SSE `/v1/events`, fichiers statiques `/miniapp/*`."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from nokker.api.events import EventBus
from nokker.api.server import make_handler
from nokker.approvals.memory import InMemoryApprovals
from nokker.core.ports import ToolSpec
from nokker.core.turn import Effect
from nokker.store.pg import MemoryStore
from tests.unit.api.test_server import NoEscalator, NullMem, _Server, make_kernel as make_chat_kernel
from tests.unit.approvals.conftest import gen_keypair, sign_p1363


class ReplayingContext:
    """Comme `FixedContext` (test_server.py) mais rejoue `turn.messages` — nécessaire ici : le modèle
    scripté (`ScriptedShip`, plus bas) décide s'il doit émettre un `tool_call` ou répondre en se basant
    sur la présence d'un message `tool` DÉJÀ dans la conversation assemblée (sinon il boucle sans fin
    sur le même appel, `FixedContext` ne rejouant jamais rien)."""

    def assemble(self, turn, history, recall, loaded, tier):
        from nokker.core.ports import Assembled

        msgs = [{"role": "user", "content": turn.input.text}]
        msgs += [m.to_openai() for m in turn.messages]
        return Assembled(messages=msgs, tools=[], prefix_sha256="fixed")

    def admit_result(self, r):
        return r


class RecordingInvariants:
    """Comme `AllowInvariants` (test_server.py) mais avec un vrai `check_action`/`redact_result` pour
    exercer le chemin de l'`ApprovalExecutor` (qui appelle `redact_result`)."""

    def check_input(self, text):
        from nokker.core.turn import InputCheck

        return InputCheck(text=text)

    def check_action(self, call, spec, turn):
        from nokker.core.turn import Decision, Gate, Verdict

        return Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="allow-all-test")

    def redact_result(self, result, spec):
        return result

    def scrub_output(self, text):
        return text


class DestructiveTools:
    def __init__(self):
        self.called = []
        self.specs = {"infra_lvextend": ToolSpec("infra_lvextend", "lvm", {}, Effect.DESTRUCTIVE)}

    def core(self):
        return []

    def spec(self, n):
        return self.specs.get(n)

    def find(self, q, limit=5):
        return []

    def index(self):
        return []

    def call(self, n, a, ctx):
        from nokker.core.turn import ToolResult

        self.called.append(n)
        return ToolResult(call_id="", name=n, content="Volume agrandi à 150G.")


def make_kernel_with_approvals(approvals):
    from nokker.core.loop import Kernel
    from nokker.core.ports import RouteDecision
    from nokker.core.turn import Decision, Gate, Message, Tier, Verdict
    from nokker.ledger.costs import InMemoryLedger

    class Pol:
        def choose(self, state):
            return RouteDecision(tier=Tier.SHIP, reasoning=False, reason="t")

    class HitlInvariants(RecordingInvariants):
        def check_action(self, call, spec, turn):
            if spec and spec.effect is Effect.DESTRUCTIVE:
                return Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="effect:destructive",
                              reason="sans GO", strength="strong")
            return Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="allow")

    class HitlGates:
        def __init__(self, inv):
            self.inv = inv

        def action(self, call, spec, turn):
            return [self.inv.check_action(call, spec, turn)]

        def switch(self, reply, turn, tier):
            return [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=1, rule="ok")]

    class ScriptedShip:
        """Premier appel d'un tour (aucun message `tool` encore dans la conversation assemblée) :
        appelle `infra_lvextend` (déclenche le HITL). Tout appel suivant DANS CE MÊME TOUR
        (continuation après la carte posée, ou après décision) : "fait". Basé sur le contenu de
        `messages`, pas un compteur global — une instance est RÉUTILISÉE par plusieurs tours (fils)
        dans ces tests, et chaque tour doit retrouver ce comportement indépendamment des autres."""

        def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
            has_tool_result = any(m.get("role") == "tool" for m in messages)
            if not has_tool_result:
                tc = ToolCall(id="c1", name="infra_lvextend", arguments={"path": "/srv/share", "size": "50G"})
                return Completion(message=Message(role="assistant", tool_calls=[tc]), usage=Usage(prompt=5),
                                  model="t", latency_s=0.0)
            return Completion(message=Message(role="assistant", content="fait"), usage=Usage(prompt=5),
                              model="t", latency_s=0.0)

    from nokker.core.ports import Completion
    from nokker.core.turn import ToolCall, Usage

    tools = DestructiveTools()
    inv = HitlInvariants()
    kernel = Kernel(store=MemoryStore(), context=ReplayingContext(), memory=NullMem(), invariants=inv,
                    gates=HitlGates(inv), escalator=NoEscalator(), policy=Pol(),
                    providers={Tier.SHIP: ScriptedShip()}, tools=tools,
                    ledger=InMemoryLedger(), approvals=approvals)
    return kernel, tools


def start_full_server(kernel, approvals=None, events=None, api_keys=None, telegram_bot_token=None,
                      telegram_ids=None, miniapp_dir=None):
    handler = make_handler(kernel, api_keys or {}, None, approvals=approvals, events=events,
                           telegram_bot_token=telegram_bot_token, telegram_ids=telegram_ids,
                           miniapp_dir=miniapp_dir)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return _Server(httpd=httpd, thread=thread, port=httpd.server_address[1])


def _get(srv, path, bearer=None, headers=None):
    h = dict(headers or {})
    if bearer:
        h["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(srv.url(path), headers=h)
    return urllib.request.urlopen(req, timeout=5)


def _post(srv, path, body, bearer=None, headers=None):
    h = dict(headers or {})
    h["Content-Type"] = "application/json"
    if bearer:
        h["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(srv.url(path), data=json.dumps(body).encode(), method="POST", headers=h)
    return urllib.request.urlopen(req, timeout=5)


def _delete(srv, path, bearer=None):
    req = urllib.request.Request(srv.url(path), method="DELETE",
                                 headers={"Authorization": f"Bearer {bearer}"} if bearer else {})
    return urllib.request.urlopen(req, timeout=5)


def _http_error_status(fn):
    try:
        fn()
        return None
    except urllib.error.HTTPError as e:
        return e.code


# ═══════════════════════════ désactivé par défaut ═══════════════════════════


def test_approvals_routes_404_when_approvals_not_configured():
    kernel = make_chat_kernel()
    srv = start_full_server(kernel, approvals=None)
    try:
        status = _http_error_status(lambda: _get(srv, "/v1/approvals"))
        assert status == 404
    finally:
        srv.stop()


# ═══════════════════════════ appareils ═══════════════════════════


def test_device_enrollment_and_cli_confirm_then_listed_for_owner():
    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    srv = start_full_server(kernel, approvals=approvals, api_keys={"key-alice": "alice"})
    try:
        _, spki = gen_keypair()
        resp = _post(srv, "/v1/devices", {"public_key_spki": spki, "name": "laptop"}, bearer="key-alice")
        data = json.loads(resp.read())
        assert data["status"] == "pending"
        device_id, code = data["id"], data["code"]

        approvals.confirm_device_cli(code)

        resp2 = _get(srv, "/v1/devices", bearer="key-alice")
        listed = json.loads(resp2.read())
        assert len(listed) == 1 and listed[0]["id"] == device_id and listed[0]["status"] == "approved"
    finally:
        srv.stop()


def test_device_enrollment_without_identity_is_401():
    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    srv = start_full_server(kernel, approvals=approvals, api_keys={"key-alice": "alice"})
    try:
        _, spki = gen_keypair()
        status = _http_error_status(lambda: _post(srv, "/v1/devices", {"public_key_spki": spki, "name": "x"}))
        assert status == 401
    finally:
        srv.stop()


def test_delete_device_by_non_owner_is_403():
    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    srv = start_full_server(kernel, approvals=approvals, api_keys={"key-alice": "alice", "key-bruno": "bruno"})
    try:
        _, spki = gen_keypair()
        resp = _post(srv, "/v1/devices", {"public_key_spki": spki, "name": "x"}, bearer="key-alice")
        device_id = json.loads(resp.read())["id"]
        status = _http_error_status(lambda: _delete(srv, f"/v1/devices/{device_id}", bearer="key-bruno"))
        assert status == 403
    finally:
        srv.stop()


def test_delete_device_by_owner_succeeds():
    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    srv = start_full_server(kernel, approvals=approvals, api_keys={"key-alice": "alice"})
    try:
        _, spki = gen_keypair()
        resp = _post(srv, "/v1/devices", {"public_key_spki": spki, "name": "x"}, bearer="key-alice")
        device_id = json.loads(resp.read())["id"]
        resp2 = _delete(srv, f"/v1/devices/{device_id}", bearer="key-alice")
        assert json.loads(resp2.read())["deleted"] is True
        assert approvals.get_device(device_id) is None
    finally:
        srv.stop()


# ═══════════════════════════ chemin heureux : carte -> défi -> signature -> exécution ═══════════════════════════


def test_full_approval_happy_path_over_http():
    """Niveau `strong` (`docs/APPROVALS.md` §1, durcissement P1 18.10.2026) : la signature d'appareil
    DOIT être accompagnée d'un `init_data` Telegram valide (Mini App) — une clé logicielle seule ne
    suffit plus (`test_decision_strong_with_software_key_signature_alone_is_403` plus bas)."""
    from tests.unit.approvals.conftest import telegram_init_data

    telegram_bot_token = "123456:FAKE-BOT-TOKEN"
    telegram_user_id = 1000000001
    approvals = InMemoryApprovals(telegram_bot_token=telegram_bot_token)
    kernel, tools = make_kernel_with_approvals(approvals)
    srv = start_full_server(kernel, approvals=approvals, api_keys={"key-alice": "alice"},
                            telegram_bot_token=telegram_bot_token)
    try:
        priv, spki = gen_keypair()
        init_data = telegram_init_data(telegram_bot_token, telegram_user_id)
        resp = _post(srv, "/v1/devices", {"public_key_spki": spki, "name": "iPhone", "init_data": init_data},
                    bearer="key-alice")
        device = json.loads(resp.read())
        approvals.confirm_device_cli(device["code"])

        from nokker.core.turn import TurnInput

        kernel.run_turn(TurnInput("fil-1", "le disque est plein sur hote21", sender="alice"))
        assert tools.called == []
        pending = json.loads(_get(srv, "/v1/approvals", bearer="key-alice").read())
        assert len(pending) == 1
        approval_id = pending[0]["id"]
        assert pending[0]["strength"] == "strong"
        assert pending[0]["tool_name"] == "infra_lvextend"

        one = json.loads(_get(srv, f"/v1/approvals/{approval_id}", bearer="key-alice").read())
        assert one["status"] == "pending"

        ch = json.loads(_post(srv, f"/v1/approvals/{approval_id}/challenge", {}, bearer="key-alice").read())
        sig = sign_p1363(priv, f"{ch['challenge']}.approve".encode("utf-8"))
        decided = json.loads(_post(srv, f"/v1/approvals/{approval_id}/decision",
                                   {"decision": "approve", "device_id": device["id"], "challenge": ch["challenge"],
                                    "signature": sig, "init_data": init_data}, bearer="key-alice").read())
        assert decided["status"] == "executed"
        assert tools.called == ["infra_lvextend"]
        assert decided["result_summary"]
    finally:
        srv.stop()


def test_decision_strong_with_software_key_signature_alone_is_403():
    """Même parcours que le chemin heureux, mais SANS `init_data` Telegram : signature d'une clé
    logicielle seule sur une approbation `strong` → 403, l'outil n'est jamais appelé."""
    approvals = InMemoryApprovals()
    kernel, tools = make_kernel_with_approvals(approvals)
    srv = start_full_server(kernel, approvals=approvals, api_keys={"key-alice": "alice"})
    try:
        priv, spki = gen_keypair()
        resp = _post(srv, "/v1/devices", {"public_key_spki": spki, "name": "iPhone"}, bearer="key-alice")
        device = json.loads(resp.read())
        approvals.confirm_device_cli(device["code"])

        from nokker.core.turn import TurnInput

        kernel.run_turn(TurnInput("fil-1", "le disque est plein sur hote21", sender="alice"))
        approval_id = json.loads(_get(srv, "/v1/approvals", bearer="key-alice").read())[0]["id"]
        ch = json.loads(_post(srv, f"/v1/approvals/{approval_id}/challenge", {}, bearer="key-alice").read())
        sig = sign_p1363(priv, f"{ch['challenge']}.approve".encode("utf-8"))

        status = _http_error_status(lambda: _post(
            srv, f"/v1/approvals/{approval_id}/decision",
            {"decision": "approve", "device_id": device["id"], "challenge": ch["challenge"], "signature": sig},
            bearer="key-alice"))
        assert status == 403
        assert tools.called == []
    finally:
        srv.stop()


def test_decision_with_bad_signature_is_403_and_tool_never_called():
    approvals = InMemoryApprovals()
    kernel, tools = make_kernel_with_approvals(approvals)
    srv = start_full_server(kernel, approvals=approvals, api_keys={"key-alice": "alice"})
    try:
        _, spki = gen_keypair()
        resp = _post(srv, "/v1/devices", {"public_key_spki": spki, "name": "iPhone"}, bearer="key-alice")
        device = json.loads(resp.read())
        approvals.confirm_device_cli(device["code"])

        from nokker.core.turn import TurnInput

        kernel.run_turn(TurnInput("fil-2", "le disque est plein", sender="alice"))
        approval_id = json.loads(_get(srv, "/v1/approvals", bearer="key-alice").read())[0]["id"]
        ch = json.loads(_post(srv, f"/v1/approvals/{approval_id}/challenge", {}, bearer="key-alice").read())

        status = _http_error_status(lambda: _post(
            srv, f"/v1/approvals/{approval_id}/decision",
            {"decision": "approve", "device_id": device["id"], "challenge": ch["challenge"], "signature": "bad"},
            bearer="key-alice"))
        assert status == 403
        assert tools.called == []
    finally:
        srv.stop()


def test_unknown_approval_id_is_404():
    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    srv = start_full_server(kernel, approvals=approvals, api_keys={"key-alice": "alice"})
    try:
        status = _http_error_status(lambda: _get(srv, "/v1/approvals/inexistant", bearer="key-alice"))
        assert status == 404
    finally:
        srv.stop()


# ═══════════════════════════ fichiers statiques Mini App ═══════════════════════════


def test_miniapp_allowlisted_file_is_served(tmp_path):
    (tmp_path / "style.css").write_text("body{color:red}")
    kernel = make_chat_kernel()
    srv = start_full_server(kernel, approvals=InMemoryApprovals(), miniapp_dir=tmp_path)
    try:
        resp = _get(srv, "/miniapp/style.css")
        assert resp.status == 200
        assert resp.read() == b"body{color:red}"
        assert "text/css" in resp.headers.get("Content-Type", "")
    finally:
        srv.stop()


def test_miniapp_non_allowlisted_file_is_404_even_if_present(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "gen-signature-vector.mjs").write_text("// x")
    (tmp_path / "approve-core.test.mjs").write_text("// x")
    kernel = make_chat_kernel()
    srv = start_full_server(kernel, approvals=InMemoryApprovals(), miniapp_dir=tmp_path)
    try:
        for name in ("package.json", "gen-signature-vector.mjs", "approve-core.test.mjs"):
            status = _http_error_status(lambda n=name: _get(srv, f"/miniapp/{n}"))
            assert status == 404, name
    finally:
        srv.stop()


def test_miniapp_missing_allowlisted_file_is_404(tmp_path):
    kernel = make_chat_kernel()
    srv = start_full_server(kernel, approvals=InMemoryApprovals(), miniapp_dir=tmp_path)
    try:
        status = _http_error_status(lambda: _get(srv, "/miniapp/approve.html"))
        assert status == 404
    finally:
        srv.stop()


# ═══════════════════════════ sessions + SSE ═══════════════════════════


def test_login_session_round_trip_over_http():
    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    srv = start_full_server(kernel, approvals=approvals, api_keys={"key-alice": "alice"})
    try:
        priv, spki = gen_keypair()
        resp = _post(srv, "/v1/devices", {"public_key_spki": spki, "name": "iPhone"}, bearer="key-alice")
        device = json.loads(resp.read())
        approvals.confirm_device_cli(device["code"])

        ch = approvals.login_challenge(device["id"])
        sig = sign_p1363(priv, f"{ch.canonical()}.login".encode("utf-8"))
        resp2 = _post(srv, "/v1/sessions", {"device_id": device["id"], "challenge": ch.canonical(),
                                            "signature": sig})
        session = json.loads(resp2.read())
        assert session["user"] == "alice"

        # Le jeton de session authentifie maintenant les routes Approbations (sans clé Bearer statique).
        pending = json.loads(_get(srv, "/v1/approvals", bearer=session["token"]).read())
        assert pending == []
    finally:
        srv.stop()


def test_session_challenge_route_over_http_then_create_session():
    """`POST /v1/devices/{id}/session-challenge` (trou de contrat relevé par `nokker-apps`, comblé ici)
    — pas de `GET`/`POST /v1/sessions` préalable requis, pas d'identité à présenter : c'est CE défi,
    une fois signé, qui établit l'identité."""
    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    srv = start_full_server(kernel, approvals=approvals, api_keys={"key-alice": "alice"})
    try:
        priv, spki = gen_keypair()
        resp = _post(srv, "/v1/devices", {"public_key_spki": spki, "name": "iPhone"}, bearer="key-alice")
        device = json.loads(resp.read())
        approvals.confirm_device_cli(device["code"])

        ch_resp = _post(srv, f"/v1/devices/{device['id']}/session-challenge", {})
        ch = json.loads(ch_resp.read())
        assert ch["challenge"] and ch["nonce"]

        sig = sign_p1363(priv, f"{ch['challenge']}.login".encode("utf-8"))
        resp2 = _post(srv, "/v1/sessions", {"device_id": device["id"], "challenge": ch["challenge"],
                                            "signature": sig})
        session = json.loads(resp2.read())
        assert session["user"] == "alice"
    finally:
        srv.stop()


def _chat(srv, bearer):
    body = json.dumps({"model": "nokker", "messages": [{"role": "user", "content": "salut"}]}).encode()
    req = urllib.request.Request(srv.url("/v1/chat/completions"), data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {bearer}",
                                          "X-Session-Id": "th_web_test"})
    return urllib.request.urlopen(req, timeout=10)


def test_web_enrollment_without_identity_stays_pending_until_confirmed_then_session_chats(monkeypatch):
    """§3.1bis : un navigateur DÉCLARE son utilisateur ; pending = aucun droit (pas de session) ; confirmé par
    l'hôte, sa session ouvre /v1/chat/completions comme une clé d'API."""
    import nokker.api.server as server_mod

    monkeypatch.setattr(server_mod, "_anon_enrolls", [])
    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    srv = start_full_server(kernel, approvals=approvals, api_keys={"key-alice": "alice"})
    try:
        priv, spki = gen_keypair()
        resp = _post(srv, "/v1/devices", {"public_key_spki": spki, "name": "web", "user": "alice"})
        device = json.loads(resp.read())
        assert resp.status == 201 and device["status"] == "pending" and device["user"] == "alice"

        def login():
            ch = json.loads(_post(srv, f"/v1/devices/{device['id']}/session-challenge", {}).read())
            sig = sign_p1363(priv, f"{ch['challenge']}.login".encode("utf-8"))
            return json.loads(_post(srv, "/v1/sessions", {"device_id": device["id"], "challenge": ch["challenge"],
                                                          "signature": sig}).read())

        assert _http_error_status(login) is not None  # pending : aucune session

        approvals.confirm_device_cli(device["code"])
        session = login()
        assert session["user"] == "alice"
        assert _chat(srv, session["token"]).status == 200
        assert _http_error_status(lambda: _chat(srv, "jeton-inconnu")) == 401
    finally:
        srv.stop()


def test_web_enrollment_unknown_user_is_401_and_rate_limited(monkeypatch):
    import nokker.api.server as server_mod

    monkeypatch.setattr(server_mod, "_anon_enrolls", [])
    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    srv = start_full_server(kernel, approvals=approvals, api_keys={"key-alice": "alice"})
    try:
        _, spki = gen_keypair()
        assert _http_error_status(lambda: _post(srv, "/v1/devices", {"public_key_spki": spki,
                                                                     "user": "mallory"})) == 401
        for _ in range(server_mod.ANON_ENROLL_MAX - 1):  # le refus de mallory n'a pas consommé de place
            _post(srv, "/v1/devices", {"public_key_spki": spki, "user": "alice"})
        assert _post(srv, "/v1/devices", {"public_key_spki": spki, "user": "alice"}).status == 201
        assert _http_error_status(lambda: _post(srv, "/v1/devices", {"public_key_spki": spki,
                                                                     "user": "alice"})) == 401
        # une identité vérifiée n'est jamais bornée par ce débit
        assert _post(srv, "/v1/devices", {"public_key_spki": spki}, bearer="key-alice").status == 201
    finally:
        srv.stop()


def test_session_challenge_for_unknown_device_is_404():
    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    srv = start_full_server(kernel, approvals=approvals, api_keys={"key-alice": "alice"})
    try:
        status = _http_error_status(lambda: _post(srv, "/v1/devices/inexistant/session-challenge", {}))
        assert status == 404
    finally:
        srv.stop()


def test_events_endpoint_delivers_approval_requested():
    import time

    approvals_backend = InMemoryApprovals()
    bus = EventBus()
    approvals_backend.on_event = bus.publish
    kernel, _ = make_kernel_with_approvals(approvals_backend)
    srv = start_full_server(kernel, approvals=approvals_backend, events=bus, api_keys={"key-alice": "alice"})
    try:
        req = urllib.request.Request(srv.url("/v1/events"), headers={"Authorization": "Bearer key-alice"})
        resp = urllib.request.urlopen(req, timeout=10)

        from nokker.core.turn import TurnInput

        def fire():
            time.sleep(0.2)
            kernel.run_turn(TurnInput("fil-evt", "le disque est plein", sender="alice"))

        t = threading.Thread(target=fire)
        t.start()

        found = False
        for _ in range(200):
            line = resp.readline().decode("utf-8", errors="replace")
            if line.startswith("event: approval.requested"):
                found = True
                break
        t.join(timeout=5)
        assert found
    finally:
        srv.stop()


def test_events_endpoint_replays_missed_events_via_last_event_id():
    """Reprise SSE (trou de contrat relevé par `nokker-apps`) : un client qui se (re)connecte avec
    `Last-Event-ID` reçoit d'abord les événements manqués dans la fenêtre bornée, avant le direct."""
    approvals_backend = InMemoryApprovals()
    bus = EventBus()
    approvals_backend.on_event = bus.publish
    kernel, _ = make_kernel_with_approvals(approvals_backend)
    srv = start_full_server(kernel, approvals=approvals_backend, events=bus, api_keys={"key-alice": "alice"})
    try:
        from nokker.core.turn import TurnInput

        # Deux cartes posées AVANT toute connexion SSE — personne n'est encore abonné.
        kernel.run_turn(TurnInput("fil-a", "le disque est plein", sender="alice"))
        kernel.run_turn(TurnInput("fil-b", "le disque est plein", sender="alice"))
        first_id = bus._buffer[0].id  # id du tout premier événement publié (approval.requested #1)

        # Reconnexion avec Last-Event-ID = id du premier événement : seul le 2e doit être rejoué.
        req = urllib.request.Request(srv.url("/v1/events"),
                                     headers={"Authorization": "Bearer key-alice", "Last-Event-ID": str(first_id)})
        resp = urllib.request.urlopen(req, timeout=10)
        replayed_ids = []
        for _ in range(10):
            line = resp.readline().decode("utf-8", errors="replace").strip()
            if line.startswith("id:"):
                replayed_ids.append(int(line.split(":", 1)[1].strip()))
            if line.startswith("event:") and replayed_ids:
                break
        assert replayed_ids == [first_id + 1]
    finally:
        srv.stop()


def test_events_endpoint_without_last_event_id_does_not_replay():
    approvals_backend = InMemoryApprovals()
    bus = EventBus()
    approvals_backend.on_event = bus.publish
    kernel, _ = make_kernel_with_approvals(approvals_backend)
    srv = start_full_server(kernel, approvals=approvals_backend, events=bus, api_keys={"key-alice": "alice"})
    try:
        from nokker.core.turn import TurnInput

        kernel.run_turn(TurnInput("fil-c", "le disque est plein", sender="alice"))

        req = urllib.request.Request(srv.url("/v1/events"), headers={"Authorization": "Bearer key-alice"})
        resp = urllib.request.urlopen(req, timeout=1.5)
        got_old_event = False
        try:
            for _ in range(5):
                line = resp.readline().decode("utf-8", errors="replace")
                if line.startswith("event: approval.requested"):
                    got_old_event = True
                    break
        except TimeoutError:
            pass
        assert got_old_event is False
    finally:
        srv.stop()


def test_miniapp_files_are_never_cached_and_versioned(tmp_path):
    from nokker.api.server import MINIAPP_DIR, _miniapp_versioned

    html = _miniapp_versioned("approve.html", (MINIAPP_DIR / "approve.html").read_bytes(), MINIAPP_DIR).decode()
    assert 'href="style.css?v=' in html and 'src="approve.js?v=' in html
    js = _miniapp_versioned("approve.js", (MINIAPP_DIR / "approve.js").read_bytes(), MINIAPP_DIR).decode()
    assert '"./approve-core.js?v=' in js


def test_decision_during_a_running_turn_answers_approved_within_the_bound_and_executes_after(monkeypatch):
    """Revue du train du 2.11, défaut 7 : la route de la Mini App attendait la fin du tour en cours sur le fil, sans
    borne (risque de 524 Cloudflare à 100 s alors que la décision est enregistrée). Elle attend au plus
    `DECISION_WAIT_S` (45 s), puis répond 200 avec la carte telle qu'elle est (`approved`) ; l'exécution suit en fond.
    La Mini App affiche « Approuvé — Nina va rendre compte sur le fil d'origine » pour `approved`."""
    import threading
    import time

    from nokker.api import server as server_mod
    from tests.unit.approvals.conftest import telegram_init_data

    monkeypatch.setattr(server_mod, "DECISION_WAIT_S", 0.5)
    telegram_bot_token = "123456:FAKE-BOT-TOKEN"
    approvals = InMemoryApprovals(telegram_bot_token=telegram_bot_token)
    kernel, tools = make_kernel_with_approvals(approvals)
    srv = start_full_server(kernel, approvals=approvals, api_keys={"key-alice": "alice"},
                            telegram_bot_token=telegram_bot_token)
    held = threading.Event()
    release = threading.Event()
    try:
        priv, spki = gen_keypair()
        init_data = telegram_init_data(telegram_bot_token, 1000000001)
        device = json.loads(_post(srv, "/v1/devices", {"public_key_spki": spki, "name": "iPhone",
                                                       "init_data": init_data}, bearer="key-alice").read())
        approvals.confirm_device_cli(device["code"])
        from nokker.core.turn import TurnInput

        kernel.run_turn(TurnInput("fil-1", "le disque est plein sur hote21", sender="alice"))
        approval_id = json.loads(_get(srv, "/v1/approvals", bearer="key-alice").read())[0]["id"]
        thread = approvals.get(approval_id).thread_id

        def busy_turn():  # un autre tour tourne sur le même fil (modèle lent)
            with kernel.turn_locks.get(thread):
                held.set()
                release.wait(20)

        threading.Thread(target=busy_turn, daemon=True).start()
        assert held.wait(5)
        ch = json.loads(_post(srv, f"/v1/approvals/{approval_id}/challenge", {}, bearer="key-alice").read())
        sig = sign_p1363(priv, f"{ch['challenge']}.approve".encode("utf-8"))
        t0 = time.monotonic()
        decided = json.loads(_post(srv, f"/v1/approvals/{approval_id}/decision",
                                   {"decision": "approve", "device_id": device["id"], "challenge": ch["challenge"],
                                    "signature": sig, "init_data": init_data}, bearer="key-alice").read())
        assert time.monotonic() - t0 < 3  # borné, pas la durée du tour en cours
        assert decided["status"] == "approved" and tools.called == []  # décision enregistrée, exécution à venir
        release.set()
        end = time.monotonic() + 10
        while time.monotonic() < end and approvals.get(approval_id).status != "executed":
            time.sleep(0.02)
        assert approvals.get(approval_id).status == "executed" and tools.called == ["infra_lvextend"]
    finally:
        release.set()
        srv.stop()
