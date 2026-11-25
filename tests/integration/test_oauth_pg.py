"""Connecteurs OAuth (069) sur un vrai Postgres, connexion `nokker_app`. Ignoré sans `NOKKER_TEST_PG_DSN`.
Jetons chiffrés en base, compte relié invisible d'un autre locataire, flux pris UNE fois par l'empreinte de son
`state` (sans locataire), nouvelles natures de connecteurs acceptées."""
from __future__ import annotations

import pytest

from tests.integration.test_rls import A, B, pg, requires_pg  # noqa: F401 — fixtures (base neuve, rôles, migrations)

pytestmark = pytest.mark.integration


@requires_pg
def test_pg_oauth_store_tokens_sealed_tenant_isolated_and_flow_single_use(pg):  # noqa: F811
    import psycopg
    from cryptography.fernet import Fernet

    from nokker.connectors.crypto import SecretBox
    from nokker.connectors.oauth import Flow, OAuthAccount, PgOAuthStore
    from nokker.connectors.store import PgConnectorStore
    from nokker.tenancy.context import tenant_scope

    box = SecretBox(Fernet.generate_key())
    st = PgOAuthStore(pg["app"], box)
    with tenant_scope(A):
        acc = st.save(OAuthAccount(id="oa_pgtest0001", account_id="us_pga", provider="google", subject="s-1",
                                   email="a@example.org", scopes=("openid",), services=("calendar",)),
                      {"refresh_token": "rt-SECRET", "access": {}})
        assert st.get(acc.id)[1]["refresh_token"] == "rt-SECRET" and st.find("us_pga", "google", "main", "s-1") is not None
        st.put_flow(Flow(state_sha256="f" * 64, tenant_id=A, account_id="us_pga", provider="google",
                         services=("calendar",), code_verifier="v", created_at=1.0, expires_at=9e9))
        st.set_state(acc.id, "reconnect")
        assert st.get(acc.id)[0].state == "reconnect"
        # nouvelles natures (069) : un connecteur « contacts » s'enregistre
        PgConnectorStore(pg["app"], box).add("us_pga", "contacts", "", {"mode": "carddav"}, "pw")
    with tenant_scope(B):
        assert st.get(acc.id) is None and st.of("us_pga") == []  # autre locataire : rien
    with psycopg.connect(pg["su"]) as su:
        raw = su.execute("SELECT token_enc FROM oauth_accounts WHERE id = %s", (acc.id,)).fetchone()[0]
    assert b"rt-SECRET" not in bytes(raw)  # chiffré en base
    flow = st.take_flow("f" * 64)  # callback : sans locataire, par la clé
    assert flow is not None and flow.tenant_id == A and flow.services == ("calendar",)
    assert st.take_flow("f" * 64) is None  # usage unique
    with tenant_scope(A):
        assert st.delete(acc.id) is True and st.get(acc.id) is None
