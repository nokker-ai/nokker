"""Contrat HTTP du BYOK cloud sur le vrai `make_handler` : GET/PUT /v1/account/models, POST …/test, 403 plan_required
sans formule, validations du PUT (calquées sur le PUT local), garde SSRF, isolation entre locataires, et la clé jamais
dans une réponse."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from nokker.api.server import make_handler
from nokker.approvals.memory import InMemoryApprovals
from nokker.byok import http as byok_http
from nokker.byok.service import secret_name
from nokker.tenancy.context import tenant_scope
from nokker.tenancy.resolve import account_tenant_fn
from nokker.tenancy.store import MemoryTenancy
from tests.unit.api.test_server import make_kernel
from tests.unit.approvals.conftest import gen_keypair, sign_p1363
from tests.unit.byok.conftest import FakeEndpoint, make_service

KEY_A = "sk-tenant-A-0123456789abcdef"
KEY_B = "sk-tenant-B-fedcba9876543210"
EP = "https://api.example.com/v1"


def _session(approvals, user):
    priv, spki = gen_keypair()
    d = approvals.enroll_device(user=user, name="poste", public_key_spki=spki)
    approvals.confirm_device_cli(d.code)
    ch = approvals.login_challenge(d.id)
    return approvals.create_session(d.id, ch.canonical(), sign_p1363(priv, f"{ch.canonical()}.login".encode())).token


@pytest.fixture
def rig():
    tenancy = MemoryTenancy()
    a, b, poor = (tenancy.create_account(f"{n}@example.com") for n in ("a", "b", "poor"))
    approvals = InMemoryApprovals(account_tenant=account_tenant_fn(tenancy))
    svc, ep = make_service({a.tenant_id, b.tenant_id}, FakeEndpoint(keys={"api.example.com": KEY_A,
                                                                         "other.example.com": KEY_B}))
    byok_http.configure(svc)
    handler = make_handler(make_kernel(), {}, approvals=approvals, tenancy=tenancy, open_api=False,
                           internal_tenant=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    tokens = {"a": _session(approvals, a.account_id), "b": _session(approvals, b.account_id),
              "poor": _session(approvals, poor.account_id)}
    seen: list[str] = []
    port = httpd.server_address[1]

    def call(method, path, body=None, who="a"):
        h = {"Content-Type": "application/json", "Authorization": f"Bearer {tokens[who]}"}
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=h,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read().decode()
                seen.append(raw)
                return r.status, json.loads(raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            seen.append(raw)
            return e.code, json.loads(raw) if raw else None

    yield {"call": call, "svc": svc, "ep": ep, "a": a, "b": b, "poor": poor, "seen": seen}
    httpd.shutdown()
    byok_http.configure(None)


def test_get_put_contract_and_key_never_returned(rig):
    call = rig["call"]
    assert call("GET", "/v1/account/models") == (200, {"default": "router", "effective": "router", "byok": [],
                                                       "entitled": True, "router_included": True})
    st, body = call("PUT", "/v1/account/models", {"default": "byok:perso", "byok": [
        {"name": "perso", "base_url": EP + "/", "model": "m-large", "api_key": KEY_A}]})
    assert st == 200, body
    assert body == {"default": "byok:perso", "effective": "byok:perso", "entitled": True, "router_included": True,
                    "byok": [
        {"name": "perso", "base_url": EP, "model": "m-large", "has_key": True, "ok": True,
         "models": ["m-large", "m-small"]}]}
    # PUT sans api_key : la clé existante est gardée (même hôte), et l'entrée n'est pas retestée
    n = len(rig["ep"].seen)
    st, body = call("PUT", "/v1/account/models", {"byok": [{"name": "perso", "base_url": EP, "model": "m-small"}]})
    assert st == 200 and body["byok"][0]["has_key"] is True and body["byok"][0]["model"] == "m-small"
    assert len(rig["ep"].seen) == n
    with tenant_scope(rig["a"].tenant_id):
        assert rig["svc"].secrets.get(rig["a"].tenant_id, "perso") == KEY_A
    st, body = call("POST", "/v1/account/models/test", {"base_url": EP, "model": "m-large"})  # clé enregistrée
    assert st == 200 and body["ok"] is True and body["model_listed"] is True and "latency_ms" in body
    assert all(KEY_A not in raw for raw in rig["seen"])  # jamais la clé dans une réponse


def test_put_validations_mirror_the_local_put(rig):
    call = rig["call"]
    base = {"name": "perso", "base_url": EP, "model": "m-large", "api_key": KEY_A}
    for body, code in (
        ({"default": "byok:absent", "byok": [base]}, "bad_default"),
        ({"default": "byok:perso", "byok": [{**base, "model": None}]}, "bad_default"),
        ({"default": "local:http://x|m"}, "bad_default"),
        ({"byok": [{**base, "name": "a b"}]}, "bad_name"),
        ({"byok": [base, base]}, "bad_name"),
        ({"byok": [{**base, "base_url": "http://api.example.com/v1"}]}, "bad_base_url"),
        ({"byok": [{**base, "api_key": "sk bad\nkey"}]}, "bad_api_key"),
    ):
        st, out = call("PUT", "/v1/account/models", body)
        assert (st, out["error"]["code"]) == (400, code), body
    assert call("GET", "/v1/account/models")[1]["byok"] == []  # un refus n'écrit rien
    with tenant_scope(rig["a"].tenant_id):
        assert rig["svc"].secrets.get(rig["a"].tenant_id, "perso") is None
    # retirer l'entrée par défaut : retour au Router, et la clé part du coffre
    assert call("PUT", "/v1/account/models", {"default": "byok:perso", "byok": [base]})[0] == 200
    st, out = call("PUT", "/v1/account/models", {"byok": []})
    assert st == 200 and out["default"] == "router" and out["byok"] == []
    with tenant_scope(rig["a"].tenant_id):
        assert rig["svc"].secrets.get(rig["a"].tenant_id, "perso") is None


def test_changing_host_without_key_drops_the_stored_key(rig):
    call = rig["call"]
    call("PUT", "/v1/account/models", {"byok": [{"name": "perso", "base_url": EP, "model": "m", "api_key": KEY_A}]})
    st, out = call("PUT", "/v1/account/models", {"byok": [{"name": "perso", "base_url": "https://other.example.com/v1",
                                                            "model": "m"}]})
    assert st == 200 and out["byok"][0]["has_key"] is False
    # la clé de A n'est jamais partie vers l'autre hôte
    assert not [s for s in rig["ep"].seen if s["host"] == "other.example.com" and KEY_A in s["auth"]]


@pytest.mark.parametrize("host", ["intranet.example.com", "metadata.example.com", "169.254.169.254", "loop.example.com",
                                  "ula.example.com", "tailnet.example.com", "mixed.example.com"])
def test_ssrf_guard_on_put_and_test(rig, host):
    call = rig["call"]
    st, out = call("PUT", "/v1/account/models", {"byok": [{"name": "x", "base_url": f"https://{host}/v1", "model": "m"}]})
    assert (st, out["error"]["code"]) == (400, "blocked_address")
    st, out = call("POST", "/v1/account/models/test", {"base_url": f"https://{host}/v1"})
    assert (st, out["error"]["code"]) == (400, "blocked_address")
    assert rig["ep"].seen == []  # aucun octet n'est parti


def test_plan_required_without_entitlement_but_get_works(rig):
    call = rig["call"]
    st, out = call("GET", "/v1/account/models", who="poor")
    assert st == 200 and out["entitled"] is False and out["effective"] == "router"
    for method, path, body in (("PUT", "/v1/account/models", {"default": "router"}),
                               ("POST", "/v1/account/models/test", {"base_url": EP})):
        st, out = call(method, path, body, who="poor")
        assert (st, out["error"]["code"]) == (403, "plan_required")
    assert rig["ep"].seen == []


def test_tenants_are_isolated(rig):
    call = rig["call"]
    call("PUT", "/v1/account/models", {"default": "byok:perso", "byok": [
        {"name": "perso", "base_url": EP, "model": "m-large", "api_key": KEY_A}]}, who="a")
    st, out = call("GET", "/v1/account/models", who="b")
    assert st == 200 and out["byok"] == [] and out["default"] == "router"  # B ne voit rien de A
    # B nomme son entrée pareil : clé et ligne à part
    call("PUT", "/v1/account/models", {"byok": [{"name": "perso", "base_url": "https://other.example.com/v1",
                                                  "model": "m", "api_key": KEY_B}]}, who="b")
    a, b = rig["a"].tenant_id, rig["b"].tenant_id
    assert secret_name(a, "perso") != secret_name(b, "perso")
    with tenant_scope(a):
        assert rig["svc"].secrets.get(a, "perso") == KEY_A and rig["svc"].store.get().entry("perso")["base_url"] == EP
    with tenant_scope(b):
        assert rig["svc"].secrets.get(b, "perso") == KEY_B
    # B re-teste l'adresse de A sans clé : la clé de A n'est PAS utilisée (aucune entrée de B à cette adresse)
    st, out = call("POST", "/v1/account/models/test", {"base_url": EP}, who="b")
    assert st == 200 and out["ok"] is False and out["error"] == "HTTP 401"
    assert all(KEY_A not in raw and KEY_B not in raw for raw in rig["seen"])


def test_routes_are_absent_when_switched_off(rig):
    byok_http.configure(None)
    assert rig["call"]("GET", "/v1/account/models")[0] == 404
