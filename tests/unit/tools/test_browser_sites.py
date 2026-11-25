"""Navigateur générique de Lokkal (nokker/tools/browser_sites.py) : browser_* du locataire interne sur l'enfant dédié
de sites-chrome (`/s/st_navigateur/mcp`), jamais pour un testeur, sans POST /sessions ; garde des références et clôture
du pool conservées ; drapeau éteint ou sites-chrome absent = aucun outil."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from nokker.core.ports import ToolContext
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from nokker.tools.browser_sites import SitesBrowserSource, build_sites_browser
from nokker.tools.result_fence import fence_browser_result


class FakeMcp:
    calls: list = []

    def __init__(self, url, headers):
        self.url, self.token = url, headers["Authorization"]

    def call(self, name, args):
        FakeMcp.calls.append((self.url, self.token, name, args))
        if name == "browser_snapshot":
            return "Page URL: http://sejours.test:8080/\n- button \"Chercher\" [ref=e3]", False, None
        return f"ok {name}", False, None


def _no_http(req: httpx.Request) -> httpx.Response:  # aucune route de session : l'enfant naît au premier appel MCP
    raise AssertionError(f"appel HTTP inattendu : {req.method} {req.url}")


def _src(child="st_navigateur"):
    FakeMcp.calls = []
    return SitesBrowserSource(base_url="http://sites-chrome:8119/", api_token=lambda: "sc-token", child_id=child,
                              client=httpx.Client(transport=httpx.MockTransport(_no_http)), mcp_factory=FakeMcp)


def ctx(thread="th1"):
    return ToolContext(thread_id=thread, turn_index=0, channel="api")


def test_internal_owner_gets_the_dedicated_child():
    src = _src()
    with tenant_scope(INTERNAL_TENANT):
        r = src.call("browser_navigate", {"url": "http://sejours.test:8080/", "lane": "x"}, ctx())
    assert not r.is_error
    assert r.content == fence_browser_result("browser_navigate", {}, "ok browser_navigate")  # contenu de tiers clôturé
    url, tok, name, args = FakeMcp.calls[0]
    assert url == "http://sites-chrome:8119/s/st_navigateur/mcp" and tok == "Bearer sc-token"
    assert args == {"url": "http://sejours.test:8080/"}  # `lane` retiré


def test_tester_is_refused():
    src = _src()
    with tenant_scope("tn_abc123"):
        r = src.call("browser_snapshot", {}, ctx())
    assert r.is_error and "propriétaire de cette machine" in r.content and not FakeMcp.calls


def test_ref_guard_kept():
    src = _src()
    with tenant_scope(INTERNAL_TENANT):
        r = src.call("browser_click", {"ref": "e9"}, ctx())
        assert r.is_error and "Refusé : " in r.content
        src.call("browser_snapshot", {}, ctx())
        assert not src.call("browser_click", {"ref": "e3"}, ctx()).is_error


def test_turn_end_closes_tabs_only_after_use():
    src = _src()
    with tenant_scope(INTERNAL_TENANT):
        src.turn_done("th1")
        assert not FakeMcp.calls
        src.call("browser_snapshot", {}, ctx())
        src.turn_done("th1")
    assert FakeMcp.calls[-1][2] == "browser_close"


def test_specs_are_the_pool_schemas_under_their_own_source():
    names = {s.name: s.source for s in _src().specs()}
    assert "browser_navigate" in names and set(names.values()) == {"lokkal:sites-browser"}


@pytest.mark.parametrize("bad", ["st_short", "st_NAVIGATEUR", "navigateur", "st_navigateur1", "../st_navigateur"])
def test_child_id_shape(bad):
    with pytest.raises(ValueError):
        _src(bad)


def test_build_flag_and_env(monkeypatch):
    on = SimpleNamespace(sites_browser_generic=True, sites_browser_generic_id="st_sejours001")
    monkeypatch.delenv("NOKKER_SITES_BROWSER_URL", raising=False)
    assert build_sites_browser(on) is None  # sites-chrome absent
    monkeypatch.setenv("NOKKER_SITES_BROWSER_URL", "http://sites-chrome:8119")
    assert build_sites_browser(SimpleNamespace(sites_browser_generic=False)) is None  # drapeau éteint
    src = build_sites_browser(on)
    assert src is not None and src.child_id == "st_sejours001" and src.ops is not None
