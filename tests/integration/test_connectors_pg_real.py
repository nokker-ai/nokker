"""Vrai Postgres : PgConnectorStore (`NOKKER_TEST_PG_DSN`, voir test_pg_real.py)."""
from __future__ import annotations

import os

import pytest

from nokker.connectors.crypto import SecretBox
from nokker.connectors.errors import ConnectorError
from nokker.connectors.store import PgConnectorStore
from nokker.store.pg import apply_migrations

DSN = os.environ.get("NOKKER_TEST_PG_DSN")

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not DSN, reason="NOKKER_TEST_PG_DSN non posé — pas de Postgres jetable")]

MAIL = {"preset": "gmail", "address": "t@gmail.com", "imap_host": "imap.gmail.com"}



@pytest.fixture(autouse=True)
def _tenant():
    """Tables sous RLS (docs/TENANCY.md §4) : chaque test écrit dans un locataire de test."""
    from nokker.tenancy.context import tenant_scope

    with tenant_scope("tn_aaaaaaaaaaaa"):
        yield

def test_round_trip_secret_encrypted_at_rest_and_revoke():
    import psycopg

    apply_migrations(DSN)
    box = SecretBox(SecretBox.generate_key())
    st = PgConnectorStore(DSN, box)
    st.delete("us_ct_1", "mail")
    assert st.get("us_ct_1", "mail") is None and st.statuses("us_ct_1") == []
    st.put("us_ct_1", "mail", MAIL, "pw-clair-123")
    cred = st.get("us_ct_1", "mail")
    assert cred.secret == "pw-clair-123" and cred.settings == MAIL and cred.last_ok_at is None
    st.mark_ok("us_ct_1", "mail")
    assert st.statuses("us_ct_1")[0].last_ok_at is not None
    with psycopg.connect(DSN) as c:
        raw = c.execute("SELECT secret_enc, settings::text FROM connector_credentials WHERE account_id='us_ct_1'").fetchone()
    assert b"pw-clair-123" not in bytes(raw[0]) and "pw-clair-123" not in raw[1]
    st.put("us_ct_1", "mail", MAIL, "pw-nouveau")  # remplacement : le dernier essai réussi est remis à zéro
    assert st.get("us_ct_1", "mail").secret == "pw-nouveau" and st.statuses("us_ct_1")[0].last_ok_at is None
    assert PgConnectorStore(DSN, SecretBox(SecretBox.generate_key())).statuses("us_autre") == []
    assert st.delete("us_ct_1", "mail") is True and st.delete("us_ct_1", "mail") is False


def test_wrong_key_does_not_leak_and_bad_kind_refused():
    apply_migrations(DSN)
    st = PgConnectorStore(DSN, SecretBox(SecretBox.generate_key()))
    st.put("us_ct_2", "calendar", {"mode": "ical"}, "https://x.example.org/a.ics")
    other = PgConnectorStore(DSN, SecretBox(SecretBox.generate_key()))
    with pytest.raises(ConnectorError) as e:
        other.get("us_ct_2", "calendar")
    assert e.value.code == "key_missing"
    with pytest.raises(ConnectorError):
        st.put("us_ct_2", "sms", {}, "x")
    st.delete("us_ct_2", "calendar")
