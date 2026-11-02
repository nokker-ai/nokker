"""Route `POST /v1/approvals/{id}/knock-arm` (docs/KNOCK.md §5.3), vraies requêtes HTTP en boucle locale : 404 quand
le heurtoir est éteint (drapeau éteint = rien de nouveau), armement par le décideur de la carte, refus d'une carte
forte, carte d'un autre locataire invisible."""
from __future__ import annotations

import json
import urllib.error

from nokker.approvals.memory import InMemoryApprovals
from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict
from nokker.knock.hub import KnockHub
from nokker.knock.policy import KnockConfig
from tests.unit.api.test_approvals_routes import _post, make_kernel_with_approvals
from tests.unit.api.test_server import _Server

ALICE = "alice"
TOKEN = "-".join(("tok", "alice", "test"))  # faux jeton, construit à l'exécution


def _card(b, strength="standard", rule="permissions:default", thread="knock-salon"):
    turn = Turn(thread_id=thread, index=0, input=InputCheck(text="x"), channel="api", sender=ALICE)
    v = Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=rule, reason="r", strength=strength)
    return b.request(turn, ToolCall(id="c1", name="send_mail", arguments={"to": "bob@example.com"}), v, None)


def _server(approvals, knock_hub):
    import threading
    from http.server import ThreadingHTTPServer

    from nokker.api.server import make_handler

    kernel, _ = make_kernel_with_approvals(approvals)
    handler = make_handler(kernel, {TOKEN: ALICE}, None, approvals=approvals, knock_hub=knock_hub)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return _Server(httpd=httpd, thread=None, port=httpd.server_address[1])


def _code(fn):
    try:
        r = fn()
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_route_is_404_when_knock_is_off():
    b = InMemoryApprovals(is_authorized=lambda u: u == ALICE)
    a = _card(b)
    srv = _server(b, None)
    try:
        status, _ = _code(lambda: _post(srv, f"/v1/approvals/{a.id}/knock-arm", {"room": "salon"}, bearer=TOKEN))
        assert status == 404
    finally:
        srv.httpd.shutdown()


def test_arm_standard_card_and_refuse_strong():
    b = InMemoryApprovals(is_authorized=lambda u: u == ALICE)
    hub = KnockHub(KnockConfig.from_raw({"enabled": True, "approve": {"enabled": True},
                                         "rooms": {"salon": {"thread_id": "knock-salon", "sender": ALICE}}}),
                   approvals=b)
    std, strong = _card(b), _card(b, strength="strong", rule="effect:destructive")
    srv = _server(b, hub)
    try:
        status, body = _code(lambda: _post(srv, f"/v1/approvals/{std.id}/knock-arm", {"room": "salon"}, bearer=TOKEN))
        assert status == 200 and body["window_s"] == 60.0 and hub.armed_in("salon") == [std.id]
        status, body = _code(lambda: _post(srv, f"/v1/approvals/{strong.id}/knock-arm", {"room": "salon"},
                                           bearer=TOKEN))
        assert status == 409 and body["error"]["code"] == "knock_refused"
        status, _ = _code(lambda: _post(srv, f"/v1/approvals/{std.id}/knock-arm", {"room": "salon"}))
        assert status == 401
        other = _card(b, thread="tn_bob:t9")
        status, _ = _code(lambda: _post(srv, f"/v1/approvals/{other.id}/knock-arm", {"room": "salon"}, bearer=TOKEN))
        assert status == 404
    finally:
        srv.httpd.shutdown()
