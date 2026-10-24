"""Client du pool de navigateurs (nokker/tools/browser_pool.py) contre un faux gestionnaire (contrat
docs/BROWSER-POOL.md) : une session par locataire réutilisée, jamais partagée, expirée → rouverte une fois, 429 et 503
dits en une phrase, rien pour le locataire interne, browser_close en fin de tour seulement si le tour a navigué."""
from __future__ import annotations

import httpx

from nokker.core.ports import ToolContext
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from nokker.tools.browser_pool import BrowserPoolSource


class Pool:
    def __init__(self):
        self.posts, self.busy, self.n = [], False, 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Bearer pool-key"
        tenant = __import__("json").loads(req.content)["tenant_id"]
        self.posts.append(tenant)
        if self.busy:
            return httpx.Response(429, headers={"Retry-After": "30"}, json={"error": "busy", "retry_after_s": 30})
        self.n += 1
        return httpx.Response(201, json={"session_id": f"s{self.n}", "mcp_url": f"http://pool/s/s{self.n}/mcp",
                                         "token": f"tok-{tenant}-{self.n}", "expires_at": "2099-01-01T00:00:00Z"})


class FakeMcp:
    calls: list = []
    expire_next: set = set()

    def __init__(self, url, headers):
        self.url, self.token = url, headers["Authorization"]

    def call(self, name, args):
        FakeMcp.calls.append((self.url, self.token, name, args))
        if self.url in FakeMcp.expire_next:
            FakeMcp.expire_next.discard(self.url)
            return "expirée", True, -32001
        return f"ok {name}", False, None


def _src(pool):
    FakeMcp.calls = []
    return BrowserPoolSource(base_url="http://pool", api_token=lambda: "pool-key",
                             client=httpx.Client(transport=httpx.MockTransport(pool)), mcp_factory=FakeMcp)


def ctx(thread="th1"):
    return ToolContext(thread_id=thread, turn_index=0, channel="api")


def test_schemas_without_lane_profile_or_upload():
    names = {s.name for s in _src(Pool()).specs()}
    assert "browser_navigate" in names and not names & {"browser_lane", "browser_profile", "browser_file_upload"}


def test_one_session_per_tenant_reused_and_isolated():
    pool = Pool()
    src = _src(pool)
    with tenant_scope("tn_a"):
        assert src.call("browser_navigate", {"url": "https://ex.test", "lane": "x"}, ctx()).content == "ok browser_navigate"
        src.call("browser_snapshot", {}, ctx())
    with tenant_scope("tn_b"):
        src.call("browser_snapshot", {}, ctx())
    assert pool.posts == ["tn_a", "tn_b"]  # A réutilise sa session ; B a la sienne
    tokens = {c[1] for c in FakeMcp.calls}
    assert tokens == {"Bearer tok-tn_a-1", "Bearer tok-tn_b-2"}
    assert all("lane" not in c[3] for c in FakeMcp.calls)  # sans effet dans le pool : jamais transmis


def test_expired_session_is_reopened_once():
    pool = Pool()
    src = _src(pool)
    with tenant_scope("tn_a"):
        src.call("browser_snapshot", {}, ctx())
        FakeMcp.expire_next = {"http://pool/s/s1/mcp"}
        r = src.call("browser_snapshot", {}, ctx())
    assert not r.is_error and pool.posts == ["tn_a", "tn_a"]


def test_busy_pool_and_internal_tenant():
    pool = Pool()
    pool.busy = True
    src = _src(pool)
    with tenant_scope("tn_a"):
        r = src.call("browser_navigate", {"url": "https://ex.test"}, ctx())
    assert r.is_error and "occupé" in r.content and "30 s" in r.content
    with tenant_scope(INTERNAL_TENANT):
        assert src.call("browser_navigate", {}, ctx()).is_error  # l'interne a brosim, pas le pool


def test_browser_close_at_turn_end_only_if_the_turn_browsed():
    pool = Pool()
    src = _src(pool)
    with tenant_scope("tn_a"):
        src.turn_done("th1")
        assert FakeMcp.calls == []
        src.call("browser_navigate", {"url": "https://ex.test"}, ctx("th1"))
        src.turn_done("th1")
    assert [c[2] for c in FakeMcp.calls] == ["browser_navigate", "browser_close"]
