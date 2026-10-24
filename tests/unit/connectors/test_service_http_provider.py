from types import SimpleNamespace

import pytest

from nokker.connectors import TestResult, http as chttp
from nokker.connectors.crypto import SecretBox
from nokker.connectors.provider import ConnectorProvider
from nokker.connectors.service import Connectors
from nokker.connectors.store import MemoryConnectorStore
from nokker.tenancy.store import MemoryTenancy

SECRET = "app-pass-xyz"
MAIL = {"preset": "gmail", "address": "t@gmail.com"}
ICAL = {"mode": "ical", "label": "calendar.example.org"}
ICAL_URL = "https://calendar.example.org/private/abc123/basic.ics"


def make(ok=True, code=None):
    calls = []

    def tester(kind, settings, secret):
        calls.append((kind, secret))
        return TestResult(ok, "ok-msg" if ok else "refusé", None if ok else code)

    store = MemoryConnectorStore(SecretBox(SecretBox.generate_key()))
    return Connectors(store, tester), store, calls


def test_cards_start_absent_and_connect_tests_then_stores():
    svc, store, calls = make()
    assert [c["state"] for c in svc.cards("a1")] == ["absent", "absent"]
    card = svc.connect("a1", "mail", MAIL, f"  {SECRET} ")
    assert card["state"] == "ok" and card["message"] == "ok-msg" and calls == [("mail", SECRET)]
    assert SECRET not in repr(card) and card["settings"]["imap_host"] == "imap.gmail.com"
    assert store.get("a1", "mail").secret == SECRET


def test_failed_test_stores_nothing():
    svc, store, _ = make(ok=False, code="auth_failed")
    with pytest.raises(Exception) as e:
        svc.connect("a1", "mail", MAIL, SECRET)
    assert e.value.code == "auth_failed" and store.get("a1", "mail") is None


def test_ical_url_is_not_shown_on_the_card():
    svc, _, _ = make()
    card = svc.connect("a1", "calendar", ICAL, ICAL_URL)
    assert "abc123" not in repr(card) and card["settings"]["label"] == "calendar.example.org"


def test_retest_marks_ok_and_revoke_deletes():
    svc, store, _ = make()
    svc.connect("a1", "mail", MAIL, SECRET)
    assert svc.test("a1", "mail")["ok"] is True
    assert svc.revoke("a1", "mail") is True and svc.revoke("a1", "mail") is False
    with pytest.raises(Exception) as e:
        svc.test("a1", "mail")
    assert e.value.code == "not_configured"


def test_accounts_are_isolated():
    svc, store, _ = make()
    svc.connect("a1", "mail", MAIL, SECRET)
    assert store.get("a2", "mail") is None and [c["state"] for c in svc.cards("a2")] == ["absent", "absent"]


class H:
    def __init__(self, body=None):
        self.body, self.sent = body or {}, []

    def _json(self, status, payload):
        self.sent.append((status, payload))

    def _read_json_body(self):
        return self.body


@pytest.fixture
def http():
    svc, store, _ = make()
    chttp.configure(svc)
    yield svc
    chttp.configure(None)


ACC = SimpleNamespace(account_id="a1")


def test_http_routes(http):
    h = H()
    assert chttp.handle(h, "GET", "/v1/connectors", ACC) and h.sent[0][0] == 200 and len(h.sent[0][1]["connectors"]) == 2
    h = H({"settings": MAIL, "secret": SECRET})
    assert chttp.handle(h, "PUT", "/v1/connectors/mail", ACC)
    assert h.sent[0][0] == 200 and SECRET not in repr(h.sent)
    h = H()
    chttp.handle(h, "POST", "/v1/connectors/mail/test", ACC)
    assert h.sent[0] == (200, {"ok": True, "message": "ok-msg", "code": None})
    h = H()
    chttp.handle(h, "DELETE", "/v1/connectors/mail", ACC)
    assert h.sent[0] == (200, {"revoked": True})


def test_http_not_its_path_or_unconfigured(http):
    for method, path in (("GET", "/v1/other"), ("PATCH", "/v1/connectors"), ("PUT", "/v1/connectors/sms")):
        assert chttp.handle(H(), method, path, ACC) is False
    chttp.configure(None)
    assert chttp.handle(H(), "GET", "/v1/connectors", ACC) is False


def test_http_errors_map_to_status(http):
    h = H({"secret": SECRET})
    chttp.handle(h, "PUT", "/v1/connectors/mail", ACC)
    assert h.sent[0][0] == 400 and h.sent[0][1]["error"] == "bad_settings"
    h = H()
    chttp.handle(h, "POST", "/v1/connectors/calendar/test", ACC)
    assert h.sent[0][0] == 404


def test_http_auth_failure_is_422_and_rate_limited():
    svc, _, _ = make(ok=False, code="auth_failed")
    chttp.configure(svc)
    try:
        h = H({"settings": MAIL, "secret": SECRET})
        chttp.handle(h, "PUT", "/v1/connectors/mail", ACC)
        assert h.sent[0][0] == 422 and SECRET not in repr(h.sent)
        statuses = []
        for _ in range(10):
            h = H({"settings": MAIL, "secret": SECRET})
            chttp.handle(h, "PUT", "/v1/connectors/mail", ACC)
            statuses.append(h.sent[0][0])
        assert statuses[-1] == 429 and 422 in statuses
    finally:
        chttp.configure(None)


def test_provider_builds_sources_per_tenant_and_skips_broken():
    box = SecretBox(SecretBox.generate_key())
    store = MemoryConnectorStore(box)
    ten = MemoryTenancy()
    acc = ten.create_account("t@gmail.com")
    other = ten.create_account("o@gmail.com")
    store.put(acc.account_id, "mail", {**MAIL, "imap_host": "imap.gmail.com", "imap_port": 993, "smtp_host": "smtp.gmail.com",
                                       "smtp_port": 465, "username": "t@gmail.com"}, SECRET)
    store.put(acc.account_id, "calendar", {"mode": "bogus"}, "x")  # réglages périmés : sautés
    prov = ConnectorProvider(store, ten.account_for_tenant)
    names = {s.name for src in prov.sources_for(acc.tenant_id) for s in src.specs()}
    assert "mail_search" in names and not any(n.startswith("calendar") for n in names)
    assert prov.sources_for(other.tenant_id) == [] and prov.sources_for("inconnu") == []


def test_account_for_tenant_ignores_suspended():
    ten = MemoryTenancy()
    acc = ten.create_account("t@x.org")
    assert ten.account_for_tenant(acc.tenant_id).account_id == acc.account_id
    assert ten.account_for_tenant("nope") is None
