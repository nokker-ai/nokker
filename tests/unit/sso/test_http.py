"""SSO OIDC sur le vrai `make_handler` : routes du propriétaire, parcours public start → callback → session, SSO
obligatoire vu par HTTP, secret jamais dans une réponse, et drapeau éteint qui ne change rien."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from nokker.api.server import make_handler
from nokker.approvals.memory import InMemoryApprovals
from nokker.sso import http as sso_http
from nokker.sso.idp import Idp
from nokker.sso.secrets import MemorySsoSecrets
from nokker.sso.service import Sso
from nokker.sso.store import MemorySsoStore
from nokker.team import http as team_http
from nokker.team.service import Team
from nokker.team.store import MemoryTeamStore
from nokker.tenancy.resolve import account_tenant_fn
from nokker.tenancy.store import MemoryTenancy
from tests.unit.api.test_server import make_kernel
from tests.unit.approvals.conftest import gen_keypair, sign_p1363
from tests.unit.sso import fake_idp as f

REDIRECT = "https://app.example.com/sso/callback"


def _session_for(approvals, device_id, priv):
    ch = approvals.login_challenge(device_id)
    return approvals.create_session(device_id, ch.canonical(), sign_p1363(priv, f"{ch.canonical()}.login".encode())).token


def _start(sso_on: bool):
    tenancy = MemoryTenancy()
    owner = tenancy.create_account("owner@example.com")
    approvals = InMemoryApprovals(account_tenant=account_tenant_fn(tenancy))
    team = Team(store=MemoryTeamStore(), tenancy=tenancy)
    team_http.configure(team)
    idp = f.FakeIdp()
    dns: dict[str, list[str]] = {}
    sso = None
    if sso_on:
        sso = Sso(store=MemorySsoStore(), team=team, tenancy=tenancy, approvals=approvals, secrets=MemorySsoSecrets(),
                  idp=Idp(http=idp), redirect_uri=REDIRECT, resolve_txt=lambda n: dns.get(n, []))
        team.sso_gate = sso.gate_reason
    sso_http.configure(sso)
    handler = make_handler(make_kernel(), {}, approvals=approvals, tenancy=tenancy, open_api=False,
                           internal_tenant=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    priv, spki = gen_keypair()
    d = approvals.enroll_device(user=owner.account_id, name="poste", public_key_spki=spki)
    approvals.confirm_device_cli(d.code)
    tokens = {"owner": _session_for(approvals, d.id, priv)}
    port = httpd.server_address[1]
    seen: list[str] = []

    def call(method, path, body=None, who=None, token=None):
        h = {"Content-Type": "application/json"}
        if who or token:
            h["Authorization"] = f"Bearer {token or tokens[who]}"
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=h,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                seen.append(raw.decode())
                return r.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read()
            seen.append(raw.decode())
            return e.code, json.loads(raw) if raw else None

    ctx = {"call": call, "tokens": tokens, "idp": idp, "dns": dns, "approvals": approvals, "tenancy": tenancy,
           "owner": owner, "seen": seen}
    return httpd, ctx


@pytest.fixture
def on():
    httpd, ctx = _start(True)
    yield ctx
    httpd.shutdown()
    sso_http.configure(None)
    team_http.configure(None)


@pytest.fixture
def off():
    httpd, ctx = _start(False)
    yield ctx
    httpd.shutdown()
    team_http.configure(None)


def _setup(ctx, required=False):
    call = ctx["call"]
    st, p = call("POST", "/v1/projects", {"name": "Plateforme"}, who="owner")
    assert st == 201
    st, c = call("POST", "/v1/sso/connections", {"name": "Acme", "issuer": f.ISSUER, "client_id": f.CLIENT_ID,
                                                 "client_secret_name": "acme-oidc"}, who="owner")
    assert st == 201 and c["client_secret"] == {"name": "acme-oidc", "set": False} and c["redirect_uri"] == REDIRECT
    st, out = call("PUT", f"/v1/sso/connections/{c['id']}/client-secret", {"value": f.CLIENT_SECRET}, who="owner")
    assert st == 200 and out == {"client_secret": {"name": "acme-oidc", "set": True}}
    st, d = call("POST", f"/v1/sso/connections/{c['id']}/domains", {"domain": "example.com"}, who="owner")
    assert st == 201 and d["verified"] is False
    st, e = call("POST", f"/v1/sso/connections/{c['id']}/domains/example.com/verify", {}, who="owner")
    assert st == 409 and e["error"]["code"] == "not_verified"
    ctx["dns"][d["txt"]["name"]] = [d["txt"]["value"]]
    st, d = call("POST", f"/v1/sso/connections/{c['id']}/domains/example.com/verify", {}, who="owner")
    assert st == 200 and d["verified"] is True
    st, b = call("PUT", f"/v1/projects/{p['id']}/sso", {"connection": c["id"], "required": required,
                                                         "mappings": [{"group": "eng", "role": "member"}]}, who="owner")
    assert st == 200 and b["required"] is required
    return p["id"], c["id"]


def _browser_login(ctx, cid, groups=("eng",)):
    call, idp = ctx["call"], ctx["idp"]
    priv, spki = gen_keypair()
    st, out = call("POST", "/v1/auth/oidc/start", {"connection": cid, "public_key_spki": spki, "name": "portable"})
    assert st == 200, out
    grant = idp.authorize(out["authorize_url"])
    idp.next_claims = {"groups": list(groups)}
    sig = sign_p1363(priv, f"{grant['state']}.oidc".encode())
    st, res = call("POST", "/v1/auth/oidc/callback", {"state": grant["state"], "code": grant["code"], "signature": sig})
    assert st == 200, res
    token = _session_for(ctx["approvals"], res["device_id"], priv)
    return res, token, (grant, sig)


def test_full_flow_over_http(on):
    pid, cid = _setup(on)
    res, token, (grant, sig) = _browser_login(on, cid)
    assert res["projects"] == [{"id": pid, "role": "member"}]
    st, p = on["call"]("GET", f"/v1/projects/{pid}", token=token)
    assert st == 200 and p["role"] == "member"
    # rejeu du même retour : refusé
    st, e = on["call"]("POST", "/v1/auth/oidc/callback", {"state": grant["state"], "code": grant["code"],
                                                          "signature": sig})
    assert st == 401 and e["error"]["code"] == "invalid_state"
    st, audit = on["call"]("GET", f"/v1/sso/connections/{cid}/audit", who="owner")
    assert st == 200 and {"login_ok", "member_added", "binding_set"} <= {x["event"] for x in audit["events"]}


def test_required_sso_over_http(on):
    pid, cid = _setup(on, required=True)
    res, sso_token, _ = _browser_login(on, cid)
    # le même compte, par un appareil de code e-mail : refusé sur les ressources du projet
    priv, spki = gen_keypair()
    approvals = on["approvals"]
    d = approvals.enroll_device(user=res["user"], name="autre", public_key_spki=spki)
    approvals.confirm_device_by_email_code(d.id)
    code_token = _session_for(approvals, d.id, priv)
    st, e = on["call"]("GET", f"/v1/projects/{pid}/rules", token=code_token)
    assert st == 403 and e["error"]["code"] == "sso_required" and e["error"]["connection"] == cid
    st, lst = on["call"]("GET", "/v1/projects", token=code_token)
    assert st == 200 and lst["projects"][0]["sso"] == {"required": True, "connection": cid}
    assert "rules" not in lst["projects"][0]
    assert on["call"]("GET", f"/v1/projects/{pid}/rules", token=sso_token)[0] == 200
    assert on["call"]("GET", f"/v1/projects/{pid}", who="owner")[0] == 200  # le propriétaire garde la main


def test_owner_routes_hidden_from_others_and_owner_never_mappable(on):
    pid, cid = _setup(on)
    _, token, _ = _browser_login(on, cid)
    for method, path, body in (("GET", f"/v1/sso/connections/{cid}", None),
                               ("PUT", f"/v1/sso/connections/{cid}/client-secret", {"value": "x"}),
                               ("GET", f"/v1/sso/connections/{cid}/audit", None),
                               ("DELETE", f"/v1/sso/connections/{cid}", None)):
        st, e = on["call"](method, path, body, token=token)
        assert st == 404 and e["error"]["code"] == "not_found", (method, path)
    st, e = on["call"]("GET", f"/v1/projects/{pid}/sso", token=token)
    assert st == 403 and e["error"]["code"] == "forbidden"
    st, e = on["call"]("PUT", f"/v1/projects/{pid}/sso", {"connection": cid, "mappings": [
        {"group": "admins", "role": "owner"}]}, who="owner")
    assert st == 400 and e["error"]["code"] == "owner_not_mappable"


def test_secret_never_in_any_http_response(on):
    pid, cid = _setup(on)
    _browser_login(on, cid)
    _browser_login(on, cid, groups=())
    for path in ("/v1/sso/connections", f"/v1/sso/connections/{cid}", f"/v1/sso/connections/{cid}/audit",
                 f"/v1/projects/{pid}/sso"):
        assert on["call"]("GET", path, who="owner")[0] == 200
    st, e = on["call"]("PATCH", f"/v1/sso/connections/{cid}", {"client_secret": f.CLIENT_SECRET}, who="owner")
    assert st == 400 and e["error"]["code"] == "secret_value_refused"
    blob = "\n".join(on["seen"])
    assert f.CLIENT_SECRET not in blob and "at-xyz" not in blob


def test_public_routes_need_no_session_but_owner_routes_do(on):
    st, _ = on["call"]("GET", "/v1/sso/connections")
    assert st == 401
    _, spki = gen_keypair()
    st, e = on["call"]("POST", "/v1/auth/oidc/start", {"connection": "sc_0000000000000000", "public_key_spki": spki})
    assert st == 404 and e["error"]["code"] == "unknown_connection"


def test_flag_off_changes_nothing(off):
    call = off["call"]
    st, p = call("POST", "/v1/projects", {"name": "Plateforme"}, who="owner")
    assert st == 201
    for method, path, body in (("GET", "/v1/sso/connections", None), ("POST", "/v1/sso/connections", {}),
                               ("GET", f"/v1/projects/{p['id']}/sso", None),
                               ("PUT", f"/v1/projects/{p['id']}/sso", {}),
                               ("POST", "/v1/auth/oidc/start", {}), ("POST", "/v1/auth/oidc/callback", {})):
        st, _ = call(method, path, body, who="owner")
        assert st == 404, (method, path)
    st, got = call("GET", f"/v1/projects/{p['id']}", who="owner")
    assert st == 200 and "sso" not in got
