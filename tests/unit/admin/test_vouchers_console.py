"""Console : onglet Codes (vouchers d'essai) — rôles, en-tête console, journal, aucune adresse."""
import json

import pytest

from nokker.admin.server import serve_admin
from nokker.admin.store import MemoryAdminStore
from nokker.billing.voucher import MemoryVouchers, Vouchers
from tests.unit.admin.test_admin import get, post, verifier

NOW = 2_000_000_000


@pytest.fixture()
def vconsole():
    store = MemoryAdminStore()
    store.seed_operators({"alice@example.ch": "owner", "bruno@example.ch": "ops", "lire@example.ch": "lecteur"},
                         "system:config")
    v = Vouchers(MemoryVouchers(), "sel-de-test-0123456789", clock=lambda: NOW)
    httpd = serve_admin(store, verifier(), "127.0.0.1", 0, vouchers=v)
    yield store, v, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def actions(store):
    return [a["action"] for a in store.audit(limit=50)]


def test_ops_creates_shared_and_single_codes_with_audit(vconsole):
    store, v, base = vconsole
    st, body = post(base, "/admin/v1/vouchers", {"campaign": "radio", "kind": "shared", "code": "radio26",
                                                 "expires": "2034-06-30", "max_uses": 100}, "bruno@example.ch")
    assert (st, body) == (201, {"created": 1, "code": "RADIO26"})
    st, body = post(base, "/admin/v1/vouchers", {"campaign": "flyers", "kind": "single", "count": 30,
                                                 "expires": "2034-06-30"}, "bruno@example.ch")
    assert (st, body) == (201, {"created": 30})  # un lot unique ne revient pas dans la réponse
    assert actions(store).count("voucher.create") == 2


def test_create_refusals(vconsole):
    store, _, base = vconsole
    good = {"campaign": "radio", "kind": "shared", "code": "R1X", "expires": "2034-06-30"}
    assert post(base, "/admin/v1/vouchers", good, "lire@example.ch")[0] == 403                    # lecteur
    assert post(base, "/admin/v1/vouchers", good, "bruno@example.ch", console_header=False)[0] == 403  # sans en-tête
    assert post(base, "/admin/v1/vouchers", good)[0] == 401                                       # sans jeton
    assert post(base, "/admin/v1/vouchers", {**good, "kind": "x"}, "bruno@example.ch") == (400, {"error": "bad_kind"})
    assert post(base, "/admin/v1/vouchers", {**good, "count": "abc", "kind": "single"},
                "bruno@example.ch") == (400, {"error": "bad_request"})
    assert "voucher.create" not in actions(store)


def test_report_is_anonymous_and_export_is_owner_only_and_audited(vconsole):
    store, v, base = vconsole
    codes = v.create(campaign="flyers", kind="single", count=3, expires="2034-06-30", actor="o")
    v.create(campaign="radio", kind="shared", code="RADIO26", expires="2034-06-30", actor="o")
    st, raw = get(base, "/admin/v1/vouchers", "lire@example.ch")
    rep = json.loads(raw)
    assert st == 200 and {c["campaign"] for c in rep["campaigns"]} == {"flyers", "radio"}
    assert [c["code"] for c in rep["codes"]] == ["RADIO26"]  # les codes uniques ne sortent que par l'export
    assert not any(c in raw.decode() for c in codes)
    assert get(base, "/admin/v1/vouchers/flyers/codes", "bruno@example.ch")[0] == 403
    st, raw = get(base, "/admin/v1/vouchers/flyers/codes", "alice@example.ch")
    assert st == 200 and json.loads(raw)["codes"] == sorted(codes)
    assert "voucher.export" in actions(store)


def test_disable(vconsole):
    store, v, base = vconsole
    v.create(campaign="radio", kind="shared", code="RADIO26", expires="2034-06-30", actor="o")
    assert post(base, "/admin/v1/vouchers/RADIO26/disable", {}, "bruno@example.ch") == (200, {"disabled": True})
    assert v.store.get("RADIO26")["disabled_at"] is not None
    assert post(base, "/admin/v1/vouchers/NOPE1/disable", {}, "bruno@example.ch")[0] == 404
    assert "voucher.disable" in actions(store)


def test_without_service_the_routes_say_so():
    store = MemoryAdminStore()
    store.seed_operators({"alice@example.ch": "owner"}, "system:config")
    httpd = serve_admin(store, verifier(), "127.0.0.1", 0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert get(base, "/admin/v1/vouchers", "alice@example.ch")[0] == 503
        assert post(base, "/admin/v1/vouchers", {}, "alice@example.ch")[0] == 503
    finally:
        httpd.shutdown()
