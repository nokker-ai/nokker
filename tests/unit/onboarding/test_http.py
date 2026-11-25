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


def _accepted_account(lang="fr"):
    front = MemoryFront({1: {"email": "c@example.org", "prenom": "Camille", "lang": lang}})
    front.send(1, None, "alice")
    tenancy, acceptances = MemoryTenancy(), MemoryAcceptances()
    http.configure(Onboarding(front, tenancy, InMemoryApprovals(), acceptances))
    body = {"token": next(iter(front.tokens)), "terms_sha256": terms.load(lang).sha256,
            "public_key_spki": gen_keypair()[1], "name": "Mac"}
    _, (st, out) = call("POST", "/v1/onboarding/accept", body=body)
    assert st == 201
    return tenancy.account(out["account_id"]), acceptances


def _acc(account, method, path, **kw):
    h = Handler(**kw)
    return http.account_route(h, method, path, account), h.out


def _changed_terms(monkeypatch):
    """Conditions modifiées : le texte courant (fr ET en) n'est plus celui qui a été accepté."""
    real = terms.load

    def load(lang):
        t = real(lang)
        new = t.text + "\n- Lecture à voix haute : nouveau paragraphe.\n"
        import hashlib

        return terms.Terms(lang=t.lang, text=new, sha256=hashlib.sha256(new.encode()).hexdigest())

    monkeypatch.setattr(terms, "load", load)
    return load


def test_account_says_whether_terms_are_current():
    account, _ = _accepted_account()
    try:
        _, (st, me) = _acc(account, "GET", "/v1/account")
        assert st == 200 and me["terms_outdated"] is False
        assert me["terms_current"] == {"version": terms.load("fr").sha256, "lang": "fr"}
    finally:
        http.configure(None)


def test_changed_terms_are_outdated_then_reaccepted(monkeypatch):
    account, acceptances = _accepted_account()
    try:
        old = terms.load("fr").sha256
        new = _changed_terms(monkeypatch)("fr").sha256
        _, (st, me) = _acc(account, "GET", "/v1/account")
        assert me["terms_outdated"] is True and me["terms_current"] == {"version": new, "lang": "fr"}
        assert me["terms"]["version"] == old  # la dernière acceptation reste lisible
        # écran périmé (l'ancienne version, ou une version inconnue) : 409, rien d'enregistré
        for stale in (old, "0" * 64, ""):
            assert _acc(account, "POST", "/v1/account/terms", body={"version": stale})[1] == \
                (409, {"error": "stale_version"})
        assert len(acceptances.for_account(account.account_id)) == 1
        handled, (st, out) = _acc(account, "POST", "/v1/account/terms", body={"version": new}, ip="198.51.100.9")
        assert handled and st == 200 and out["terms"]["version"] == new and out["terms_outdated"] is False
        rows = acceptances.for_account(account.account_id)
        assert len(rows) == 2 and rows[-1]["invitation_id"] is None and rows[-1]["ip"] == "198.51.100.9"
        assert rows[-1]["terms_version_sha256"] == new and rows[-1]["lang"] == "fr"
        assert _acc(account, "GET", "/v1/account")[1][1]["terms_outdated"] is False
    finally:
        http.configure(None)


def test_accepting_the_current_text_of_the_other_language_is_fine(monkeypatch):
    account, _ = _accepted_account("fr")
    try:
        en = _changed_terms(monkeypatch)("en").sha256
        _, (st, out) = _acc(account, "POST", "/v1/account/terms", body={"version": en})
        assert st == 200 and out["terms"]["lang"] == "en" and out["terms_outdated"] is False
        assert out["terms_current"]["lang"] == "en"
    finally:
        http.configure(None)


def test_reaccept_without_onboarding_is_503():
    account, _ = _accepted_account()
    http.configure(None)
    assert _acc(account, "POST", "/v1/account/terms", body={"version": "x"})[1] == (503, {"error": "terms_disabled"})


def test_public_current_terms_text():
    h = Handler()
    assert http.terms_route(h, "GET", "/v1/terms?lang=en")
    st, out = h.out
    assert st == 200 and out == {"version": terms.load("en").sha256, "lang": "en", "text": terms.load("en").text}
    h = Handler()
    assert http.terms_route(h, "GET", "/v1/terms?lang=xx") and h.out[1]["lang"] == "fr"
    assert http.terms_route(Handler(), "POST", "/v1/terms") is False
    assert http.terms_route(Handler(), "GET", "/v1/termsx") is False


# Conditions d'avant le §5 « Lecture à voix haute » (ElevenLabs, 03.11.2026) : sha des textes acceptés jusque-là.
TERMS_BEFORE_SPEECH = {"fr": "185d09cd3a46dc2f4698660a3f16aa712eb03d610f3281127877a6ddd14f46f9",
                       "en": "487fa447c09fced114931f4fc76198f57a6579d14247c0ce289d7dc24b9b2b6c"}


def test_speech_paragraph_changes_the_terms_and_old_acceptances_are_outdated():
    for lang, old in TERMS_BEFORE_SPEECH.items():
        t = terms.load(lang)
        assert t.sha256 != old and "ElevenLabs" in t.text
        assert terms.lang_of_version(old) is None  # l'ancienne version n'est plus acceptable
    front = MemoryFront({})
    tenancy, acceptances = MemoryTenancy(), MemoryAcceptances()
    http.configure(Onboarding(front, tenancy, InMemoryApprovals(), acceptances))
    try:
        acc = tenancy.create_account("ancien@example.org")
        acceptances.record(acc.account_id, "inv-0", TERMS_BEFORE_SPEECH["fr"], "fr", "198.51.100.2")
        _, (st, me) = _acc(acc, "GET", "/v1/account")
        assert st == 200 and me["terms_outdated"] is True
        assert me["terms_current"] == {"version": terms.load("fr").sha256, "lang": "fr"}
    finally:
        http.configure(None)
