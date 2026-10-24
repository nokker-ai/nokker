import pytest

from nokker.approvals.memory import InMemoryApprovals
from nokker.onboarding import http, terms
from nokker.onboarding.acceptances import MemoryAcceptances
from nokker.onboarding.consents import MemoryConsents
from nokker.onboarding.front import MemoryFront
from nokker.onboarding.service import Onboarding
from nokker.tenancy.store import MemoryTenancy
from tests.unit.approvals.conftest import gen_keypair


class Handler:
    def __init__(self, body=None, ip="198.51.100.4", headers=None):
        self.headers = headers or {}
        self.client_address = (ip, 1)
        self._body = body or {}
        self.out = None

    def _json(self, status, obj):
        self.out = (status, obj)

    def _read_json_body(self):
        return self._body


@pytest.fixture
def stack():
    front = MemoryFront({1: {"email": "c@example.org", "prenom": "Camille", "lang": "fr"}})
    front.send(1, None, "alice")
    http.configure(Onboarding(front, MemoryTenancy(), InMemoryApprovals(), MemoryAcceptances()))
    yield front, next(iter(front.tokens))
    http.configure(None)


def call(method, path, **kw):
    h = Handler(**kw)
    return http.handle(h, method, path), h.out


def test_not_configured_or_foreign_path_is_not_handled(stack):
    assert call("GET", "/v1/threads") == (False, None)
    http.configure(None)
    assert call("GET", "/v1/onboarding/terms") == (False, None)


def test_invitation_then_terms_then_accept(stack):
    _, token = stack
    assert call("GET", f"/v1/onboarding/invitation?t={token}") == (True, (200, {"valid": True, "lang": "fr", "prenom": "Camille"}))
    _, (st, t) = call("GET", "/v1/onboarding/terms?lang=en")
    assert st == 200 and t["sha256"] == terms.load("en").sha256 and t["text"].startswith("# Nokker")
    body = {"token": token, "terms_sha256": t["sha256"], "public_key_spki": gen_keypair()[1], "name": "Mac"}
    handled, (st, out) = call("POST", "/v1/onboarding/accept", body=body)
    assert handled and st == 201 and out["account_id"].startswith("us_") and out["lang"] == "fr"
    _, (st, out) = call("POST", "/v1/onboarding/accept", body=body)
    assert st == 410 and out == {"error": "invalid_token"}


def test_stale_terms_is_409(stack):
    _, token = stack
    _, (st, out) = call("POST", "/v1/onboarding/accept",
                        body={"token": token, "terms_sha256": "0" * 64, "public_key_spki": gen_keypair()[1]})
    assert (st, out) == (409, {"error": "terms_mismatch"})


def test_unknown_route_under_prefix_falls_through(stack):
    assert call("GET", "/v1/onboarding/nope") == (False, None)
    assert call("DELETE", "/v1/onboarding/accept") == (False, None)


def test_ip_comes_from_the_edge_header_when_present(stack):
    h = Handler(headers={"CF-Connecting-IP": "203.0.113.7"})
    assert http.client_ip(h) == "203.0.113.7" and http.client_ip(Handler()) == "198.51.100.4"


def test_rate_limit_per_ip(stack):
    for _ in range(http.RATE_MAX):
        assert call("GET", "/v1/onboarding/terms")[1][0] == 200
    assert call("GET", "/v1/onboarding/terms")[1] == (429, {"error": "rate"})
    assert call("GET", "/v1/onboarding/terms", ip="198.51.100.5")[1][0] == 200


def test_training_consent_only_true_counts(stack):
    front, token = stack
    ob = http._onboarding
    ob.consents = MemoryConsents()
    body = {"token": token, "terms_sha256": terms.load("fr").sha256, "public_key_spki": gen_keypair()[1],
            "training_consent": "true"}  # une chaîne n'est pas un accord
    _, (st, out) = call("POST", "/v1/onboarding/accept", body=body)
    assert st == 201 and ob.consents.current(out["account_id"], "training") is False


def test_account_shows_terms_and_consent_and_consent_can_be_withdrawn():
    front = MemoryFront({1: {"email": "c@example.org", "prenom": "Camille", "lang": "fr"}})
    front.send(1, None, "alice")
    tenancy, consents = MemoryTenancy(), MemoryConsents()
    http.configure(Onboarding(front, tenancy, InMemoryApprovals(), MemoryAcceptances(), consents=consents))
    try:
        sha = terms.load("fr").sha256
        body = {"token": next(iter(front.tokens)), "terms_sha256": sha, "public_key_spki": gen_keypair()[1],
                "name": "Mac", "training_consent": True}
        _, (st, out) = call("POST", "/v1/onboarding/accept", body=body)
        account = tenancy.account(out["account_id"])

        def acc(method, path, **kw):
            h = Handler(**kw)
            return http.account_route(h, method, path, account), h.out

        handled, (st, me) = acc("GET", "/v1/account")
        assert handled and st == 200 and me["email"] == "c@example.org" and me["status"] == "active"
        assert me["terms"]["version"] == sha and me["consents"] == {"training": True}
        assert acc("PUT", "/v1/account/consent", body={"scope": "training", "granted": False})[1] == \
            (200, {"scope": "training", "granted": False})
        acc("PUT", "/v1/account/consent", body={"scope": "training", "granted": False})  # idempotent : pas de ligne
        assert [(r["granted"], r["source"]) for r in consents.history(account.account_id)] == \
            [(True, "signup"), (False, "account")]
        assert acc("GET", "/v1/account")[1][1]["consents"] == {"training": False}
        for bad in ({"scope": "marketing", "granted": True}, {"scope": "training", "granted": "yes"}):
            assert acc("PUT", "/v1/account/consent", body=bad)[1] == (400, {"error": "invalid"})
        assert acc("GET", "/v1/threads") == (False, None)
    finally:
        http.configure(None)
