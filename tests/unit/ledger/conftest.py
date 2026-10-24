"""Fixtures communes aux tests du ledger (F6).

`pg_ledger` : backend Postgres jetable. DSN pris dans `NOKKER_TEST_PG_DSN` si posé, sinon tente le
Postgres local de dev (`localhost:15532`, conteneur `pgvector/pgvector:pg17` lancé pour ce chantier,
cf. rapport F6). Si rien n'est joignable, les tests qui en dépendent sont SKIP (pas d'échec) — le CI
sans Postgres garde les tests mémoire verts (deliverable 6 : « backends mémoire + Postgres jetable »,
jetable = peut ne pas exister partout).
"""
from __future__ import annotations

import os
import uuid

import pytest


DEFAULT_TEST_DSN = "postgresql://postgres:nokker@127.0.0.1:15532/nokker_f6"


def _reachable(dsn: str) -> bool:
    # importorskip ICI et pas au niveau du module : sinon un environnement sans psycopg saute TOUS les tests du
    # ledger (mémoire comprise), pas seulement ceux qui ont besoin de Postgres.
    psycopg = pytest.importorskip("psycopg", reason="psycopg[binary] non installé dans cet environnement")
    try:
        conn = psycopg.connect(dsn, connect_timeout=2)
        conn.close()
        return True
    except Exception:  # noqa: BLE001 — n'importe quelle panne de connexion -> skip, pas d'échec
        return False


@pytest.fixture
def pg_dsn() -> str:
    dsn = os.environ.get("NOKKER_TEST_PG_DSN", DEFAULT_TEST_DSN)
    if not _reachable(dsn):
        pytest.skip(f"Postgres de test non joignable ({dsn}) — lancer le conteneur jetable du rapport F6")
    return dsn


@pytest.fixture
def pg_ledger(pg_dsn):
    from nokker.ledger.costs import PostgresLedger
    from nokker.ledger.prices import load_from_yaml

    from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

    # Le ledger Postgres lit et écrit dans le locataire courant (RLS, docs/TENANCY.md §4).
    with tenant_scope(INTERNAL_TENANT):
        ledger = PostgresLedger(pg_dsn, prices=load_from_yaml())
        # Isolation : chaque test travaille sous un thread_id unique plutôt que de TRUNCATE une table
        # partagée entre tests parallèles (xdist) — moins rapide à nettoyer mais aucun risque de collision.
        ledger.test_prefix = f"test-{uuid.uuid4().hex[:8]}"
        yield ledger
        with ledger._connect() as conn:
            conn.execute("DELETE FROM ledger_entries WHERE thread_id LIKE %s", (f"{ledger.test_prefix}%",))
            conn.execute("DELETE FROM ledger_verdicts WHERE thread_id LIKE %s", (f"{ledger.test_prefix}%",))
