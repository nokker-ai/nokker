"""Appareils de l'app (076) sur un vrai Postgres sous RLS, connexion `nokker_app` : un locataire ne voit ni n'écrit les
appareils d'un autre ; 5 appareils au plus par membre ; aucune colonne de donnée lue. Ignoré sans `NOKKER_TEST_PG_DSN`."""
from __future__ import annotations

import psycopg
import pytest

from tests.integration.test_rls import A, B, pg, requires_pg  # noqa: F401  (pg : fixture)

pytestmark = pytest.mark.integration


@requires_pg
def test_pg_device_store_rls_and_limit(pg):  # noqa: F811
    from nokker.devices.store import DeviceError, PgDeviceStore, clean_caps
    from nokker.tenancy.context import tenant_scope

    st = PgDeviceStore(pg["app"])
    d = clean_caps("dev-alex-01", {"name": "iPhone [/x]", "platform": "ios", "caps": ["calendar.read"]})
    with tenant_scope(A):
        st.put("us_a", d)
        assert st.get("us_a", "dev-alex-01").name == "iPhone (/x)" and st.get("us_a", "dev-alex-01").caps == (
            "calendar.read",)
        st.put("us_a", clean_caps("dev-alex-01", {"name": "iPhone", "platform": "ios", "caps": []}))  # mise à jour
        assert st.get("us_a", "dev-alex-01").caps == ()
        for i in range(2, 6):
            st.put("us_a", clean_caps(f"dev-alex-0{i}", {"name": "A", "platform": "android", "caps": []}))
        with pytest.raises(DeviceError):
            st.put("us_a", clean_caps("dev-alex-09", {"name": "A", "platform": "ios", "caps": []}))
    with tenant_scope(B):
        assert st.list("us_a") == [] and st.get("us_a", "dev-alex-01") is None and not st.delete("us_a", "dev-alex-01")
    with tenant_scope(A):
        assert st.delete("us_a", "dev-alex-01") and len(st.list("us_a")) == 4
    with psycopg.connect(pg["su"]) as su:
        cols = {r[0] for r in su.execute("SELECT column_name FROM information_schema.columns "
                                         "WHERE table_name = 'member_devices'").fetchall()}
    assert cols == {"tenant_id", "account_id", "device_id", "name", "platform", "caps", "updated_at"}
