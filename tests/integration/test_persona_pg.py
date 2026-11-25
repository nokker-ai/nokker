"""« Ton Nokker » (083, V6) sur un vrai Postgres sous RLS, connexion `nokker_app` : un choix par compte, invisible
d'un autre locataire, réécrit en place. Ignoré sans `NOKKER_TEST_PG_DSN`."""
from __future__ import annotations

import pytest

from tests.integration.test_rls import A, B, pg, requires_pg  # noqa: F401  (pg : fixture)

pytestmark = pytest.mark.integration


@requires_pg
def test_pg_persona_is_per_account_and_tenant(pg):  # noqa: F811
    from nokker.persona import PersonaService, PgPersonaStore, catalog_from_config
    from nokker.tenancy.context import tenant_scope

    svc = PersonaService(catalog=catalog_from_config({"enabled": True}, {}), store=PgPersonaStore(pg["app"]))
    with tenant_scope(A):
        assert svc.get("us_pga")["defaulted"] is True
        svc.put("us_pga", A, {"name": "Ada", "avatar": {"id": "nina", "version": 1}})
        svc.put("us_pga", A, {"name": "Ada Lou"})
        got = svc.get("us_pga")
        assert got["name"] == "Ada Lou" and got["avatar"] == {"id": "nina", "version": 1}
    with tenant_scope(B):
        assert svc.get("us_pga")["defaulted"] is True  # RLS : rien du locataire A
        assert svc.export("us_pga") is None
