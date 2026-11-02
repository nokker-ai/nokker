"""Vue glance (lunettes) : appairage par code court ou QR, jeton en empreinte seule, révocation (titulaire et adulte du
foyer), et PORTÉE tenue par le vrai serveur du noyau : le jeton glance n'ouvre que GET /v1/glance ; toute écriture,
décision de carte ou autre route → 403."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from nokker.ambient import http as ambient_http
from nokker.ambient.displays import DisplayError, Displays, Membership, MemoryDisplayStore, sha
from nokker.ambient.glance import ALPHABET, Glance
from nokker.config import NokkerConfig

ALICE = "us_aaaaaaaaaaa1"
HOUSE = Membership(account_id=ALICE, household_id="tp_house_a", household_tenant="tn_aaaaaaaaaaaa", profile="adult")
ADULT_B = Membership(account_id="us_aaaaaaaaaaa2", household_id="tp_house_a", household_tenant="tn_aaaaaaaaaaaa",
                     profile="adult")
TEEN = Membership(account_id="us_aaaaaaaaaaa3", household_id="tp_house_a", household_tenant="tn_aaaaaaaaaaaa",
                  profile="teen")


@pytest.fixture(autouse=True)
def _reset():
    yield
    ambient_http.configure(None)


def test_offer_claim_once_and_hash_only():
    g = Glance(store=MemoryDisplayStore())
    offer = g.offer(ALICE, "tn_aaaaaaaaaaaa")
    code = offer["code"].replace("-", "")
    assert len(code) == 8 and set(code) <= set(ALPHABET) and offer["qr"] == "NOKKER-GLANCE:" + code
    assert offer["expires_in"] == 300
    out = g.claim(offer["qr"])  # le QR scanné vaut le code saisi
    token = out["token"]
    assert token.startswith("nkg_") and out["scope"] == "glance"
    row = g.store.rows[out["device"]["id"]]
    assert row.token_sha256 == sha(token) and token not in repr(g.store.rows) and code not in repr(g.store.rows)
    with pytest.raises(DisplayError) as e:
        g.claim(offer["code"])  # une seule fois
    assert e.value.status == 404
    assert g.resolve(token).account_id == ALICE


def test_code_expires_and_attempts_are_bounded():
    clock = [1000.0]
    g = Glance(store=MemoryDisplayStore(), clock=lambda: clock[0])
    offer = g.offer(ALICE, "tn_aaaaaaaaaaaa")
    clock[0] += 301
    with pytest.raises(DisplayError):
        g.claim(offer["code"])
    for _ in range(19):
        with pytest.raises(DisplayError):
            g.claim("ZZZZ-ZZZZ")
    with pytest.raises(DisplayError) as e:
        g.claim("ZZZZ-ZZZZ")
    assert e.value.status == 429 or e.value.status == 404
    with pytest.raises(DisplayError) as e:
        g.claim("ZZZZ-ZZZZ")
    assert e.value.status == 429


def test_glance_and_eink_tokens_do_not_cross():
    store = MemoryDisplayStore()
    g, d = Glance(store=store), Displays(store=store)
    gtok = g.claim(g.offer(ALICE, "tn_aaaaaaaaaaaa")["code"])["token"]
    etok = d.register(HOUSE, {"model": "trmnl"})["token"]
    assert d.resolve(gtok) is None and g.resolve(etok) is None
    assert [x.kind for x in store.active()] == ["eink"]  # rien de glance n'est publié vers la passerelle


def test_revocation_by_owner_and_by_household_adult_only():
    store = MemoryDisplayStore()
    g, d = Glance(store=store), Displays(store=store)
    t1 = g.claim(g.offer(ALICE, HOUSE.household_tenant, HOUSE.household_id)["code"])
    t2 = g.claim(g.offer(ALICE, HOUSE.household_tenant, HOUSE.household_id)["code"])
    with pytest.raises(DisplayError):
        g.revoke("us_bbbbbbbbbbb1", t1["device"]["id"])  # pas le titulaire
    g.revoke(ALICE, t1["device"]["id"])
    assert g.resolve(t1["token"]) is None
    with pytest.raises(DisplayError):
        d.revoke(TEEN, t2["device"]["id"])  # un ado du foyer : non
    d.revoke(ADULT_B, t2["device"]["id"])  # un adulte du foyer : oui
    assert g.resolve(t2["token"]) is None
    with pytest.raises(DisplayError):
        g.claim(g.offer(ALICE, "tn_aaaaaaaaaaaa")["code"] + "X")


def test_at_most_five_per_account():
    g = Glance(store=MemoryDisplayStore())
    for _ in range(5):
        g.offer(ALICE, "tn_aaaaaaaaaaaa")
    with pytest.raises(DisplayError) as e:
        g.offer(ALICE, "tn_aaaaaaaaaaaa")
    assert e.value.status == 409


# ─────────────────────── vrai serveur du noyau : portée ───────────────────────

def _call(port, path, *, token=None, body=None, method=None):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=h,
                                 method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"null")
        except ValueError:
            return e.code, raw


@pytest.fixture()
def server(tmp_path, monkeypatch):
    from nokker.api.server import configure_ambient, make_handler
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.local.api import LOCAL_OWNER
    from tests.unit.api.test_approvals_routes import make_kernel_with_approvals
    from tests.unit.test_local_api import _api

    monkeypatch.delenv("NOKKER_HOME_BRIDGE_URL", raising=False)
    monkeypatch.delenv("NOKKER_PG_DSN", raising=False)
    configure_ambient(NokkerConfig(ambient={"glance": {"enabled": True}, "eink": {"enabled": True}}),
                      SimpleNamespace(invariants=SimpleNamespace(scrub_output=lambda s: s)))
    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    api = _api(tmp_path)
    api.state_dir = tmp_path / "state"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: LOCAL_OWNER}, approvals=approvals,
                                                               local_api=api))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield SimpleNamespace(port=port, api=api, approvals=approvals, owner=LOCAL_OWNER)
    httpd.shutdown()


def test_server_pairing_read_and_scope(server):
    port, owner_tok = server.port, server.api.token
    assert _call(port, "/v1/glance/pair", body={})[0] == 401  # l'offre exige le titulaire connecté
    st, offer = _call(port, "/v1/glance/pair", token=owner_tok, body={"name": "Lunettes"})
    assert st == 201
    st, claimed = _call(port, "/v1/glance/claim", body={"code": offer["code"]})
    assert st == 201
    token = claimed["token"]
    # la lecture permise
    st, view = _call(port, "/v1/glance", token=token)
    assert st == 200 and set(view) >= {"brief", "brief_last_run", "approvals", "incidents", "unavailable", "at"}
    assert isinstance(view["approvals"], list) and isinstance(view["incidents"], list)
    # tout le reste : 403, quelle que soit la méthode (écritures, approbations, autres lectures)
    refused = [("GET", "/v1/approvals", None), ("POST", "/v1/approvals/ap_x/decision", {"decision": "approve"}),
               ("POST", "/v1/approvals/ap_x/challenge", {}), ("POST", "/v1/plan/brief", {}),
               ("DELETE", "/v1/plan/brief", None), ("GET", "/v1/plan/brief", None),
               ("POST", "/v1/incidents/in_x/ack", {}), ("GET", "/v1/incidents", None),
               ("POST", "/v1/chat/completions", {"messages": []}), ("GET", "/v1/threads", None),
               ("POST", "/v1/glance/pair", {}), ("GET", "/v1/glance/devices", None),
               ("DELETE", f"/v1/glance/devices/{claimed['device']['id']}", None),
               ("PUT", "/v1/ambient/board", {"courses": ["x"]}), ("POST", "/v1/ambient/displays", {"model": "trmnl"}),
               ("GET", "/v1/local/approvals", None), ("PATCH", "/v1/agents/ag_x", {}), ("POST", "/v1/pause", {}),
               ("POST", "/v1/glance", {}), ("GET", "/v1/s/abc", None)]
    for method, path, body in refused:
        st, out = _call(port, path, token=token, body=body, method=method)
        assert st == 403, (method, path, st)
        assert out["error"]["code"] == "glance_scope"
    # révocation par le titulaire : le jeton tombe
    assert _call(port, f"/v1/glance/devices/{claimed['device']['id']}", token=owner_tok, method="DELETE")[0] == 200
    assert _call(port, "/v1/glance", token=token)[0] == 401
    assert _call(port, "/v1/glance")[0] == 401  # sans jeton glance


def test_view_lists_pending_cards_of_the_owner(server):
    from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict
    from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

    st, offer = _call(server.port, "/v1/glance/pair", token=server.api.token, body={})
    token = _call(server.port, "/v1/glance/claim", body={"code": offer["code"]})[1]["token"]
    assert _call(server.port, "/v1/glance", token=token)[1]["approvals"] == []
    turn = Turn(thread_id="th_glance", index=0, input=InputCheck(text="x"), channel="api", sender=server.owner)
    with tenant_scope(INTERNAL_TENANT):
        card = server.approvals.request(turn, ToolCall(id="c1", name="terminal", arguments={"command": "ls"}),
                                        Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1,
                                                rule="permissions:default", strength="standard"), None)
    view = _call(server.port, "/v1/glance", token=token)[1]
    assert [a["id"] for a in view["approvals"]] == [card.id]
    # lecture seule : la carte reste en attente, la décision par le jeton glance est refusée
    st, _ = _call(server.port, f"/v1/approvals/{card.id}/decision", token=token, body={"decision": "approve"})
    assert st == 403 and server.approvals.get(card.id).status == "pending"


def test_glance_routes_absent_when_flag_off(tmp_path):
    from nokker.api.server import configure_ambient

    configure_ambient(NokkerConfig(), SimpleNamespace(invariants=SimpleNamespace(scrub_output=lambda s: s)))
    assert not ambient_http.is_glance_path("/v1/glance")


def test_cloud_beta_accounts_pair_read_scope_and_isolation(monkeypatch):
    """Bêta cloud (aucune machine Lokkal, comptes testeurs `us_…`, sessions d'appareil) : chacun appaire SES
    lunettes ; la vue d'ana ne voit pas les cartes de cam ; cam ne révoque pas les lunettes d'ana."""
    from nokker.api.server import configure_ambient, make_handler
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict
    from nokker.tenancy.context import scoped_thread_id, tenant_scope
    from nokker.tenancy.store import MemoryTenancy
    from tests.unit.api.test_approvals_routes import make_kernel_with_approvals
    from tests.unit.team.test_http import _session

    monkeypatch.delenv("NOKKER_PG_DSN", raising=False)
    configure_ambient(NokkerConfig(ambient={"glance": {"enabled": True}}),
                      SimpleNamespace(invariants=SimpleNamespace(scrub_output=lambda s: s)))
    tenancy = MemoryTenancy()
    ana, cam = tenancy.create_account("ana@example.org"), tenancy.create_account("cam@example.org")
    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {}, approvals=approvals, tenancy=tenancy,
                                                               open_api=False, internal_tenant=False))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        sa, sc = _session(approvals, ana.account_id), _session(approvals, cam.account_id)
        for acc in (ana, cam):  # une carte en attente chacun, dans son locataire
            with tenant_scope(acc.tenant_id):
                approvals.request(Turn(thread_id=scoped_thread_id(acc.tenant_id, f"th_{acc.account_id}"), index=0, input=InputCheck(text="x"),
                                       channel="api", sender=acc.account_id),
                                  ToolCall(id="c1", name="terminal", arguments={"command": "ls"}),
                                  Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1,
                                          rule="permissions:default", strength="standard"), None)
        st, offer = _call(port, "/v1/glance/pair", token=sa, body={"name": "Lunettes d'ana"})
        assert st == 201
        st, claimed = _call(port, "/v1/glance/claim", body={"code": offer["code"].lower()})
        assert st == 201
        st, view = _call(port, "/v1/glance", token=claimed["token"])
        assert st == 200 and len(view["approvals"]) == 1
        assert view["approvals"][0]["thread_id"].endswith(f"th_{ana.account_id}")
        assert "brief" in view["unavailable"] and "incidents" in view["unavailable"]  # drapeaux éteints : null / []
        assert _call(port, "/v1/approvals", token=claimed["token"])[0] == 403
        did = claimed["device"]["id"]
        assert _call(port, f"/v1/glance/devices/{did}", token=sc, method="DELETE")[0] == 404  # pas à cam
        assert _call(port, "/v1/glance/devices", token=sc)[1] == {"devices": []}
        assert [d["id"] for d in _call(port, "/v1/glance/devices", token=sa)[1]["devices"]] == [did]
        assert _call(port, f"/v1/glance/devices/{did}", token=sa, method="DELETE")[0] == 200
        assert _call(port, "/v1/glance", token=claimed["token"])[0] == 401
    finally:
        httpd.shutdown()


def _raw(port, path, token):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.loads(e.read() or b"null")


def test_read_rate_limit_two_per_minute_per_token(server):
    g = ambient_http.service().glance
    now = [1_000_000.0]
    g.clock = lambda: now[0]
    toks = []
    for _ in range(2):
        st, offer = _call(server.port, "/v1/glance/pair", token=server.api.token, body={})
        toks.append(_call(server.port, "/v1/glance/claim", body={"code": offer["code"]})[1]["token"])
    t1, t2 = toks
    assert _raw(server.port, "/v1/glance", t1)[0] == 200
    now[0] += 10
    assert _raw(server.port, "/v1/glance", t1)[0] == 200
    now[0] += 5
    st, headers, body = _raw(server.port, "/v1/glance", t1)
    assert st == 429 and body["error"]["code"] == "glance_rate" and headers["Retry-After"] == "45"
    assert _raw(server.port, "/v1/glance", t2)[0] == 200  # par jeton : l'autre lit encore
    now[0] += 45  # la première lecture sort de la fenêtre
    assert _raw(server.port, "/v1/glance", t1)[0] == 200
    assert _raw(server.port, "/v1/glance", t1)[0] == 429


def test_tokens_never_in_logs(server, caplog, capsys):
    import logging

    caplog.set_level(logging.DEBUG)
    st, offer = _call(server.port, "/v1/glance/pair", token=server.api.token, body={})
    code = offer["code"]
    claimed = _call(server.port, "/v1/glance/claim", body={"code": code})[1]
    token = claimed["token"]
    for _ in range(3):  # lecture, puis 429 (limite)
        _raw(server.port, "/v1/glance", token)
    _call(server.port, "/v1/approvals", token=token)  # 403 de portée
    _call(server.port, "/v1/glance/claim", body={"code": code})  # code déjà pris : 404
    st, out = _call(server.port, "/v1/ambient/displays", token=server.api.token, body={"model": "trmnl"})
    etok = out["token"]
    urllib.request.urlopen(f"http://127.0.0.1:{server.port}/v1/eink/{etok}.png", timeout=10).read()  # jeton dans l'URL
    _call(server.port, f"/v1/glance/devices/{claimed['device']['id']}", token=server.api.token, method="DELETE")
    _raw(server.port, "/v1/glance", token)  # 401 après révocation
    logs = "\n".join(r.getMessage() for r in caplog.records) + capsys.readouterr().err
    assert "/v1/eink/nkd_***.png" in logs  # le journal d'accès a bien vu la requête, masquée
    for secret in (token, token[4:], etok, etok[4:], code, code.replace("-", "")):
        assert secret not in logs
