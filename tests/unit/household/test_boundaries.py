"""Profils mineurs aux frontières du noyau : consentement d'entraînement refusé (API ; la base dans
tests/integration/test_household_rls.py), pas de connexion SSO, rien dans le miroir."""
from __future__ import annotations

import pytest

from nokker.onboarding import http as onboarding_http
from nokker.onboarding.consents import MemoryConsents
from nokker.tenancy.store import MemoryTenancy


class FakeHandler:
    def __init__(self, body):
        self.body, self.out = body, None

    def _read_json_body(self):
        return self.body

    def _json(self, status, payload):
        self.out = (status, payload)

    headers: dict = {}  # noqa: RUF012 — faux gestionnaire de test
    client_address = ("198.51.100.9", 0)


class FakeOnboarding:
    def __init__(self):
        self.consents = MemoryConsents()

        class Acc:
            @staticmethod
            def for_account(_):
                return []

        self.acceptances = Acc()


@pytest.fixture
def ob():
    acc = MemoryTenancy().create_account("ado@example.com")
    fake = FakeOnboarding()
    onboarding_http.configure(fake)  # type: ignore[arg-type]
    onboarding_http.configure_minors(lambda a: a == acc.account_id)
    yield acc, fake
    onboarding_http.configure_minors(None)
    onboarding_http.configure(None)


def test_minor_cannot_grant_the_training_consent_through_the_api(ob):
    acc, fake = ob
    h = FakeHandler({"scope": "training", "granted": True})
    assert onboarding_http.account_route(h, "PUT", "/v1/account/consent", acc)
    assert h.out == (403, {"error": "minor_training_refused"})
    assert fake.consents.current(acc.account_id, "training") is False
    h = FakeHandler({"scope": "training", "granted": False})  # retirer reste toujours possible
    onboarding_http.account_route(h, "PUT", "/v1/account/consent", acc)
    assert h.out[0] == 200
    h = FakeHandler({})
    onboarding_http.account_route(h, "GET", "/v1/account", acc)
    assert h.out[1]["consents"] == {"training": False, "training_locked": "minor"}


def test_no_sso_login_for_a_minor():
    from tests.unit.sso.conftest import make_rig

    rig = make_rig()
    rig.login(email="ado@example.com", sub="u-ado")
    minors = {rig.tenancy.account_by_email("ado@example.com").account_id}
    rig.sso.is_minor = lambda a: a in minors
    with pytest.raises(Exception) as e:  # par l'identité déjà liée
        rig.login(email="ado@example.com", sub="u-ado")
    assert (getattr(e.value, "status", None), getattr(e.value, "code", None)) == (403, "minor_account")
    other = rig.tenancy.create_account("enfant@example.com")
    minors.add(other.account_id)
    with pytest.raises(Exception) as e:  # par l'adresse d'un compte existant
        rig.login(email="enfant@example.com", sub="u-enfant")
    assert getattr(e.value, "code", None) == "minor_account"


def test_mirror_is_closed_to_minor_accounts():
    from nokker.household.boundaries import no_minor_mirror

    allowed = no_minor_mirror(lambda acc: True, lambda acc: acc == "us_ado000000001")
    assert allowed("us_adulte0000001") is True and allowed("us_ado000000001") is False
    assert no_minor_mirror(None, lambda acc: False) is None  # miroir éteint : il le reste


def test_public_pages_are_closed_to_minor_accounts():
    from nokker.household.boundaries import no_minor_public_pages

    allowed = no_minor_public_pages(lambda acc: True, lambda acc: acc == "us_ado000000001")
    assert allowed("us_adulte0000001") is True and allowed("us_ado000000001") is False
    assert no_minor_public_pages(None, lambda acc: True) is None
