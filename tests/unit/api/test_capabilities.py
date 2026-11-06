"""`GET /v1/capabilities` (05.11.2026, docs/CLIENT-API.md §Capacités) : drapeaux des fonctions actives pour
l'appelant, en vraie requête HTTP. Authentifiée, sans secret ni donnée d'un autre locataire ; la lecture du foyer se
fait dans `tenant_scope` du locataire de l'appelant."""
from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

import pytest

from nokker.api import capabilities as caps
from nokker.api.events import EventBus
from nokker.api.server import make_handler
from nokker.config import NokkerConfig
from nokker.house import http as house_http
from nokker.household import http as household_http
from nokker.tenancy.context import current_tenant_or_none
from nokker.tenancy.store import MemoryTenancy
from tests.unit.api.test_server import _Server, make_kernel
from tests.unit.guide.test_guide_routes import call

FLAGS = {"account", "agents", "approvals", "billing", "cloud_byok", "code", "connectors_multi", "connectors_oauth", "consolidation", "family_calendar", "glance",
         "guide", "house", "household", "household_paper", "learning", "local", "local_cost", "long_memory", "operate",
         "packs", "pay", "pay_agent", "plan", "presence", "share", "show_me", "speech", "sso", "steer", "team",
         "threads", "turn_cost", "who_does_what"}


class FakeHousehold:
    """Foyer : `membership` lu dans le locataire courant, comme le vrai (`Household.membership`)."""

    def __init__(self, members: dict[str, str]):
        self.members = members  # compte → locataire
        self.seen: list[tuple[str, str | None]] = []

    def membership(self, account_id):
        tenant = current_tenant_or_none()
        self.seen.append((account_id, tenant))
        return object() if self.members.get(account_id) == tenant else None


@pytest.fixture
def rig(monkeypatch):
    # Services posés par d'autres tests (globaux de module) : éteints ici, remis en place par monkeypatch.
    import importlib

    for name in ("nokker.guide.http", "nokker.household.famcal.http", "nokker.household.paper.http",
                 "nokker.learning.http", "nokker.memory.long_http", "nokker.memory.consolidation.http",
                 "nokker.presence.http", "nokker.sso.http", "nokker.team.http", "nokker.webpacks.http"):
        monkeypatch.setattr(importlib.import_module(name), "enabled", lambda: False)
    for name in ("nokker.billing.http", "nokker.payments.http", "nokker.voice.web", "nokker.incidents.http",
                 "nokker.plan.http", "nokker.code_sessions.http", "nokker.ambient.http"):
        monkeypatch.setattr(importlib.import_module(name), "service", lambda: None)
    caps.configure(None)
    household_http.configure(None)
    house_http.configure(None)
    tenancy = MemoryTenancy()
    alice, bob = tenancy.create_account("alice@example.org"), tenancy.create_account("bob@example.org")
    keys = {"key-a": alice.account_id, "key-b": bob.account_id, "key-i": "owner1"}
    kernel = make_kernel()
    handler = make_handler(kernel, keys, None, approvals=None, events=EventBus(), hub=None,
                           known_users=frozenset({"owner1"}), tenancy=tenancy)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    srv = _Server(httpd=httpd, thread=th, port=httpd.server_address[1])
    yield srv, alice, bob
    srv.stop()
    caps.configure(None)
    household_http.configure(None)
    house_http.configure(None)


def test_requires_an_identity(rig):
    srv, _, _ = rig
    assert call(srv, "GET", "/v1/capabilities", key=None)[0] == 401
    assert call(srv, "GET", "/v1/capabilities", key="mauvaise")[0] == 401


def test_default_shape_everything_off(rig):
    srv, _, _ = rig
    st, body = call(srv, "GET", "/v1/capabilities", key="key-a")
    assert st == 200 and body["version"] == 1 and body["mode"] == "cloud"
    assert set(body) == {"version", "mode", "features"} and set(body["features"]) == FLAGS
    f = body["features"]
    assert all(isinstance(v, bool) for v in f.values())
    assert f["account"] is True  # compte de nokker.ai
    assert not any(v for k, v in f.items() if k != "account")
    st, internal = call(srv, "GET", "/v1/capabilities", key="key-i")
    assert st == 200 and internal["features"]["account"] is False


def test_flags_follow_config_and_caller(rig):
    srv, alice, bob = rig
    cfg = NokkerConfig(show_me=True)
    cfg.cloud_byok = {"enabled": True}  # type: ignore[attr-defined]  # bloc de p2/cloud-byok, pas encore ici
    caps.configure(cfg)
    hh = FakeHousehold({alice.account_id: alice.tenant_id})
    household_http.configure(hh)
    house_http.configure(object())
    a = call(srv, "GET", "/v1/capabilities", key="key-a")[1]["features"]
    b = call(srv, "GET", "/v1/capabilities", key="key-b")[1]["features"]
    i = call(srv, "GET", "/v1/capabilities", key="key-i")[1]["features"]
    assert a["show_me"] and a["cloud_byok"] and a["household"] and a["house"]
    assert b["household"] and not b["house"]  # foyer allumé, mais Bob n'est membre d'aucun
    assert not i["cloud_byok"] and not i["house"]  # BYOK cloud et salle Maison : comptes de nokker.ai seulement
    # La lecture du foyer s'est faite DANS le locataire de l'appelant (bug NoTenant), jamais dans un autre.
    assert (alice.account_id, alice.tenant_id) in hh.seen and (bob.account_id, bob.tenant_id) in hh.seen
    assert all(t is not None for _, t in hh.seen)


def test_house_off_when_household_unreadable(rig):
    srv, _, _ = rig

    class Broken:
        def membership(self, account_id):
            raise RuntimeError("base en panne")

    household_http.configure(Broken())
    house_http.configure(object())
    st, body = call(srv, "GET", "/v1/capabilities", key="key-a")
    assert st == 200 and body["features"]["household"] and not body["features"]["house"]


def test_no_secret_in_body(rig):
    srv, alice, _ = rig
    caps.configure(NokkerConfig(show_me=True))
    st, body = call(srv, "GET", "/v1/capabilities", key="key-a")
    raw = repr(body)
    assert st == 200 and alice.account_id not in raw and alice.tenant_id not in raw and "key-" not in raw


def test_lokkal_owner_sees_local_and_cost(tmp_path, rig):
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.ledger.costs import InMemoryLedger
    from nokker.local.api import LOCAL_OWNER
    from tests.unit.api.test_approvals_routes import make_kernel_with_approvals
    from tests.unit.test_local_api import _api, _req

    kernel, _ = make_kernel_with_approvals(InMemoryApprovals())
    api = _api(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: LOCAL_OWNER}, InMemoryLedger(None),
                                                               local_api=api))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert _req(port, "/v1/capabilities")[0] == 401
        status, body = _req(port, "/v1/capabilities", token=api.token)
        assert status == 200 and body["mode"] == "lokkal"
        assert body["features"]["local"] and body["features"]["local_cost"]
        assert not body["features"]["account"] and not body["features"]["cloud_byok"]
        assert not body["features"]["glance"]
        import nokker.ambient.http as amb

        orig = amb.service
        amb.service = lambda: _Glance()
        try:
            status, body = _req(port, "/v1/capabilities", token=api.token)
            assert body["features"]["glance"]  # Lokkal : le propriétaire voit ses lunettes
        finally:
            amb.service = orig
        assert not body["features"]["pay"]  # sans service de paiement
        import nokker.payments.http as pay_http

        orig_pay = pay_http.service
        pay_http.service = lambda: object()
        try:
            status, body = _req(port, "/v1/capabilities", token=api.token)
            assert body["features"]["pay"]  # own_card : /v1/payments/* au propriétaire sur la machine
        finally:
            pay_http.service = orig_pay
    finally:
        httpd.shutdown()


class _Glance:
    glance = object()


def _on(monkeypatch, *names):
    import importlib

    for name in names:
        mod = importlib.import_module(name)
        if name == "nokker.ambient.http":
            monkeypatch.setattr(mod, "service", lambda: _Glance())
        elif hasattr(mod, "enabled") and name == "nokker.presence.http":
            monkeypatch.setattr(mod, "enabled", lambda: True)
        else:
            monkeypatch.setattr(mod, "service", lambda: object())


def test_operate_plan_code_glance_presence_follow_their_routes(rig, monkeypatch):
    srv, _, _ = rig
    off = call(srv, "GET", "/v1/capabilities", key="key-a")[1]["features"]
    assert not any(off[k] for k in ("operate", "plan", "code", "glance", "presence", "who_does_what"))
    _on(monkeypatch, "nokker.incidents.http", "nokker.plan.http", "nokker.code_sessions.http", "nokker.ambient.http",
        "nokker.presence.http")
    a = call(srv, "GET", "/v1/capabilities", key="key-a")[1]["features"]
    i = call(srv, "GET", "/v1/capabilities", key="key-i")[1]["features"]
    # Compte de nokker.ai : les cinq routes lui répondent.
    assert all(a[k] for k in ("operate", "plan", "code", "glance", "presence", "who_does_what"))
    # Identité interne : incidents et plan (toute identité vérifiée) ; code, glance (cloud) et presence exigent un
    # compte `us_…` (403 account_required) : faux.
    assert i["operate"] and i["plan"]
    assert not any(i[k] for k in ("code", "glance", "presence", "who_does_what"))


def test_account_only_routes_are_false_for_internal(rig, monkeypatch):
    srv, _, _ = rig
    import importlib

    for name in ("nokker.team.http", "nokker.webpacks.http", "nokker.learning.http", "nokker.sso.http"):
        monkeypatch.setattr(importlib.import_module(name), "enabled", lambda: True)
    monkeypatch.setattr(importlib.import_module("nokker.payments.http"), "service", lambda: object())
    a = call(srv, "GET", "/v1/capabilities", key="key-a")[1]["features"]
    i = call(srv, "GET", "/v1/capabilities", key="key-i")[1]["features"]
    assert all(a[k] for k in ("team", "packs", "learning", "sso", "pay"))
    assert not any(i[k] for k in ("team", "packs", "learning", "sso", "pay"))
