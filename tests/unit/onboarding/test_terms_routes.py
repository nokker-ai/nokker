"""Réacceptation des conditions sur le vrai `make_handler` : `GET /v1/terms` public, `GET /v1/account` (terms_current,
terms_outdated) et `POST /v1/account/terms` par la session du compte, dans son locataire."""
from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from nokker.approvals.memory import InMemoryApprovals
from nokker.onboarding import http as onboarding_http
from nokker.onboarding import terms
from nokker.onboarding.acceptances import MemoryAcceptances
from nokker.onboarding.front import MemoryFront
from nokker.onboarding.service import Onboarding
from nokker.tenancy.context import tenant_scope
from nokker.tenancy.store import MemoryTenancy


@pytest.fixture
def world():
    from nokker.api.server import make_handler
    from tests.unit.api.test_server import make_kernel
    from tests.unit.team.test_http import _session

    tenancy, approvals, acceptances = MemoryTenancy(), InMemoryApprovals(), MemoryAcceptances()
    onboarding_http.configure(Onboarding(MemoryFront({}), tenancy, approvals, acceptances))
    acc = tenancy.create_account("camille@example.org")
    with tenant_scope(acc.tenant_id):
        acceptances.record(acc.account_id, "inv-1", "f" * 64, "fr", "198.51.100.1")  # une version d'avant
    handler = make_handler(make_kernel(), {}, approvals=approvals, tenancy=tenancy, open_api=False,
                           internal_tenant=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    token = _session(approvals, acc.account_id)
    port = httpd.server_address[1]

    def call(method, path, body=None, auth=True):
        h = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if auth else {})}
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=h,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    yield call, acceptances, acc
    httpd.shutdown()
    onboarding_http.configure(None)


def test_reaccept_through_the_real_server(world):
    call, acceptances, acc = world
    st, t = call("GET", "/v1/terms?lang=fr", auth=False)
    assert st == 200 and t["version"] == hashlib.sha256(t["text"].encode()).hexdigest() == terms.load("fr").sha256
    st, me = call("GET", "/v1/account")
    assert st == 200 and me["terms_outdated"] is True and me["terms_current"]["version"] == t["version"]
    assert call("POST", "/v1/account/terms", {"version": "f" * 64}) == (409, {"error": "stale_version"})
    assert call("POST", "/v1/account/terms", {"version": t["version"]}, auth=False)[0] == 401
    st, out = call("POST", "/v1/account/terms", {"version": t["version"]})
    assert st == 200 and out["terms_outdated"] is False
    assert call("GET", "/v1/account")[1]["terms_outdated"] is False
    with tenant_scope(acc.tenant_id):
        assert [r["invitation_id"] for r in acceptances.for_account(acc.account_id)] == ["inv-1", None]
