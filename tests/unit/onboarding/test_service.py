import pytest

from nokker.approvals.memory import InMemoryApprovals
from nokker.onboarding import terms
from nokker.onboarding.consents import MemoryConsents
from nokker.onboarding.acceptances import MemoryAcceptances
from nokker.onboarding.front import FrontError, MemoryFront
from nokker.onboarding.service import Onboarding, OnboardingError
from nokker.tenancy.store import MemoryTenancy
from tests.unit.approvals.conftest import gen_keypair

FR = terms.load("fr")
EN = terms.load("en")


class Env:
    def __init__(self):
        self.front = MemoryFront({1: {"email": "Camille@Example.org", "prenom": "Camille", "lang": "fr"}})
        self.tenancy = MemoryTenancy()
        self.approvals = InMemoryApprovals()
        self.acc = MemoryAcceptances()
        self.consents = MemoryConsents()
        self.ob = Onboarding(self.front, self.tenancy, self.approvals, self.acc, self.consents)
        self.front.send(1, None, "alice")
        self.token = next(iter(self.front.tokens))
        self.spki = gen_keypair()[1]

    def accept(self, **kw):
        a = dict(token=self.token, terms_sha256=FR.sha256, public_key_spki=self.spki, device_name="Portable", ip="203.0.113.9")
        return self.ob.accept(**{**a, **kw})


@pytest.fixture
def env():
    return Env()


def test_accept_creates_account_acceptance_and_confirmed_device(env):
    out = env.accept()
    acc = env.tenancy.account_by_email("camille@example.org")
    assert out["account_id"] == acc.account_id and out["lang"] == "fr"
    [rec] = env.acc.for_account(acc.account_id)
    assert rec["terms_version_sha256"] == FR.sha256 and rec["ip"] == "203.0.113.9" and rec["lang"] == "fr"
    assert rec["invitation_id"] == "1"
    dev = env.approvals.get_device(out["device_id"])
    assert dev.status == "approved" and dev.user == acc.account_id and dev.approved_via == "invitation:1"
    assert env.tenancy.account_for_identity("device", out["device_id"]).account_id == acc.account_id


def test_english_terms_are_recorded_as_english(env):
    env.accept(terms_sha256=EN.sha256)
    [rec] = env.acc.rows
    assert rec["lang"] == "en"


def test_token_works_once(env):
    env.accept()
    with pytest.raises(OnboardingError) as e:
        env.accept(public_key_spki=gen_keypair()[1])
    assert e.value.code == "invalid_token"
    assert len(env.approvals.list_devices()) == 1


def test_stale_terms_do_not_burn_the_token_nor_create_anything(env):
    with pytest.raises(OnboardingError) as e:
        env.accept(terms_sha256="0" * 64)
    assert e.value.code == "terms_mismatch"
    assert env.front.check(env.token)["valid"]
    assert env.tenancy.account_by_email("camille@example.org") is None and not env.acc.rows


def test_bad_key_does_not_burn_the_token(env):
    with pytest.raises(OnboardingError) as e:
        env.accept(public_key_spki="pas-une-cle")
    assert e.value.code == "invalid_key" and env.front.check(env.token)["valid"]


def test_unknown_or_replaced_token_confirms_nothing(env):
    with pytest.raises(OnboardingError) as e:
        env.accept(token="inconnu")
    assert e.value.code == "invalid_token"
    old = env.token
    env.front.send(1, None, "alice")  # remplace
    with pytest.raises(OnboardingError):
        env.accept(token=old)
    assert env.tenancy.account_by_email("camille@example.org") is None and not env.approvals.list_devices()


def test_other_email_cannot_take_over_an_existing_account(env):
    env.accept()
    env.front.send(1, None, "alice")  # l'opérateur renvoie : le compte a déjà un appareil
    tok = [t for t, v in env.front.tokens.items() if not v["superseded"]][0]
    with pytest.raises(OnboardingError) as e:
        env.accept(token=tok)
    assert e.value.code == "account_exists"
    assert env.front.check(tok)["valid"]  # jeton rendu
    assert len(env.approvals.list_devices()) == 1


def test_failure_after_redeem_releases_and_retry_resumes_the_orphan_account(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("pg down")

    monkeypatch.setattr(env.acc, "record", boom)
    with pytest.raises(OnboardingError) as e:
        env.accept()
    assert e.value.code == "internal" and env.front.check(env.token)["valid"]
    assert not env.approvals.list_devices()
    monkeypatch.undo()
    out = env.accept()  # même lien : le compte orphelin est repris, pas de doublon
    assert env.approvals.get_device(out["device_id"]).status == "approved"
    assert len(env.acc.rows) == 1


def test_device_failure_removes_the_device(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(env.tenancy, "bind_identity", boom)
    with pytest.raises(OnboardingError):
        env.accept()
    assert env.approvals.list_devices() == [] and env.front.check(env.token)["valid"]


def test_front_down_means_503_and_nothing_created(env, monkeypatch):
    def down(token):
        raise FrontError("unavailable")

    monkeypatch.setattr(env.front, "redeem", down)
    with pytest.raises(OnboardingError) as e:
        env.accept()
    assert e.value.code == "front_unavailable" and e.value.status == 503
    assert env.tenancy.account_by_email("camille@example.org") is None


def test_invitation_peek_does_not_consume(env):
    assert env.ob.invitation(env.token) == {"valid": True, "lang": "fr", "prenom": "Camille"}
    assert env.ob.invitation("zz") == {"valid": False}
    assert env.front.check(env.token)["valid"]


def test_terms_versions_are_stable_and_distinct():
    assert FR.sha256 != EN.sha256 and terms.lang_of_version(FR.sha256) == "fr" and terms.lang_of_version("x") is None
    assert "en France" in FR.text and "in France" in EN.text and "28" in FR.text


def test_training_consent_is_never_precheched_and_refusal_is_logged(env):
    out = env.accept()
    assert env.consents.current(out["account_id"], "training") is False
    [row] = env.consents.history(out["account_id"])
    assert row["granted"] is False and row["source"] == "signup" and row["terms_sha256"] == FR.sha256


def test_training_consent_granted_at_signup_is_recorded_with_version_and_ip(env):
    out = env.accept(training_consent=True)
    assert env.consents.current(out["account_id"], "training") is True
    [row] = env.consents.history(out["account_id"])
    assert row["scope"] == "training" and row["ip"] == "203.0.113.9" and row["terms_sha256"] == FR.sha256


def test_stale_terms_record_no_consent(env):
    with pytest.raises(OnboardingError):
        env.accept(terms_sha256="0" * 64, training_consent=True)
    assert not env.consents.rows
