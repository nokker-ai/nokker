"""ACCORD — table `accord_devices` (092) sur un vrai Postgres : le store PG appaire, relit, révoque ; la RLS isole les
locataires (un locataire ne voit, ne révoque ni n'écrase la clé d'un autre) ; une clé active est unique par locataire ;
`nokker erase` les couvre (test_erase.py, toutes les tables rattachées)."""
from __future__ import annotations

import pytest

from nokker.accord.store import Device, PgAccordStore
from nokker.tenancy.context import tenant_scope
from tests.integration.test_rls import pg, requires_pg  # noqa: F401 — fixture partagée (base migrée, rôles)

pytestmark = pytest.mark.integration

A, B = "tn_accord_a", "tn_accord_b"
PUB = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def _dev(i: str, pub: str = PUB) -> Device:
    return Device(device_id="acc_" + i * 12, pubkey=pub, key8="0123abcd", host_fp="a" * 64, paired_at=1.0)


@requires_pg
def test_pg_store_pair_read_revoke_and_isolation(pg):  # noqa: F811
    st = PgAccordStore(pg["app"])
    with tenant_scope(A):
        st.add(_dev("1"))
        assert st.get("acc_" + "1" * 12).key8 == "0123abcd"
        assert st.by_pubkey(PUB).active
    with tenant_scope(B):
        assert st.get("acc_" + "1" * 12) is None and st.by_pubkey(PUB) is None and st.list() == []
        assert st.revoke("acc_" + "1" * 12, 2.0) is False  # pas la sienne
        st.add(_dev("2"))  # même clé publique dans un AUTRE locataire : permis (un appareil par foyer)
    with tenant_scope(A):
        assert [d.device_id for d in st.list()] == ["acc_" + "1" * 12]
        import psycopg

        with pytest.raises(psycopg.errors.UniqueViolation):
            st.add(_dev("3"))  # même clé active dans le même locataire : refusée (index unique partiel)
        assert st.revoke("acc_" + "1" * 12, 3.0) is True
        assert st.by_pubkey(PUB).revoked_at == 3.0
        st.add(_dev("4"))  # après révocation, la même clé peut être ré-appairée (nouvel identifiant)
        assert st.by_pubkey(PUB).device_id == "acc_" + "4" * 12

@requires_pg
def test_pg_store_name_and_last_seen_094(pg):  # noqa: F811
    st = PgAccordStore(pg["app"])
    with tenant_scope(A):
        st.add(_dev("5", pub="B" * 43))
        d = st.get("acc_" + "5" * 12)
        assert d.name is None and d.last_seen_at is None and d.to_json()["name"] == "ACCORD 0123abcd"
        assert st.rename("acc_" + "5" * 12, "Bureau") is True
        st.touch("acc_" + "5" * 12, 7.0)
        d = st.get("acc_" + "5" * 12)
        assert (d.name, d.last_seen_at) == ("Bureau", 7.0)
        import psycopg

        with pytest.raises(psycopg.errors.CheckViolation):
            st.rename("acc_" + "5" * 12, "x" * 41)  # la base refuse aussi (CHECK de 094)
    with tenant_scope(B):
        assert st.rename("acc_" + "5" * 12, "Pirate") is False  # RLS : pas le sien
        st.touch("acc_" + "5" * 12, 9.0)
    with tenant_scope(A):
        assert st.get("acc_" + "5" * 12).last_seen_at == 7.0 and st.get("acc_" + "5" * 12).name == "Bureau"
        assert st.revoke("acc_" + "5" * 12, 8.0) is True
        assert st.rename("acc_" + "5" * 12, "Autre") is False  # révoqué


# Effacement : tests/integration/test_erase.py sème une ligne par locataire dans CHAQUE table rattachée (accord_devices
# compris) et vérifie que `nokker erase` emporte celles du compte effacé et elles seules.
