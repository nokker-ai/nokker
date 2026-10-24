"""Vrai Postgres : migration 005 et PgAcceptances (`NOKKER_TEST_PG_DSN`, voir test_pg_real.py)."""
from __future__ import annotations

import os

import pytest

from nokker.onboarding.acceptances import PgAcceptances
from nokker.store.pg import apply_migrations

DSN = os.environ.get("NOKKER_TEST_PG_DSN")

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not DSN, reason="NOKKER_TEST_PG_DSN non posé — pas de Postgres jetable")]



@pytest.fixture(autouse=True)
def _tenant():
    """Tables sous RLS (docs/TENANCY.md §4) : chaque test écrit dans un locataire de test."""
    from nokker.tenancy.context import tenant_scope

    with tenant_scope("tn_aaaaaaaaaaaa"):
        yield

def test_acceptance_round_trip_and_migration_idempotent():
    apply_migrations(DSN)
    apply_migrations(DSN)
    acc = PgAcceptances(DSN)
    acc.record("us_it_1", "42", "ab" * 32, "fr", "203.0.113.9")
    acc.record("us_it_1", "42", "cd" * 32, "en", None)
    rows = acc.for_account("us_it_1")
    assert [r["terms_version_sha256"][:2] for r in rows] == ["ab", "cd"]
    assert rows[0]["ip"] == "203.0.113.9" and rows[0]["invitation_id"] == "42" and rows[0]["accepted_at"]
    assert acc.for_account("us_autre") == []


def test_email_codes_round_trip_attempts_and_burn():
    """Migration 009 et PgCodes : un code par compte (remplacé), essais comptés atomiquement, brûlé à l'usage."""
    from datetime import datetime, timedelta, timezone

    from nokker.onboarding.emailcode import PgCodes
    from nokker.tenancy.store import PgTenancy

    apply_migrations(DSN)
    apply_migrations(DSN)
    acc = PgTenancy(DSN).create_account(f"code-{os.getpid()}@example.org")
    codes, exp = PgCodes(DSN), datetime.now(timezone.utc) + timedelta(minutes=10)
    assert codes.attempt(acc.account_id) is None
    codes.put(acc.account_id, "a" * 64, exp)
    codes.attempt(acc.account_id)
    codes.put(acc.account_id, "b" * 64, exp)  # un nouveau code remet les essais à zéro
    sha, _, attempts = codes.attempt(acc.account_id)
    assert (sha, attempts) == ("b" * 64, 1)
    assert codes.attempt(acc.account_id)[2] == 2
    codes.burn(acc.account_id)
    assert codes.attempt(acc.account_id) is None


def test_consents_round_trip_append_only_current_is_last():
    """Migration 010 et PgConsents : l'état courant est la dernière ligne, jamais pré-coché, historique conservé."""
    from nokker.onboarding.consents import PgConsents

    apply_migrations(DSN)
    apply_migrations(DSN)
    c = PgConsents(DSN)
    assert c.current("us_c_1", "training") is False
    c.record("us_c_1", "training", True, "signup", "ab" * 32, "203.0.113.9")
    assert c.current("us_c_1", "training") is True
    c.record("us_c_1", "training", False, "account", None, None)
    assert c.current("us_c_1", "training") is False
    assert [(r["granted"], r["source"]) for r in c.history("us_c_1")] == [(True, "signup"), (False, "account")]
    assert c.history("us_autre") == []
