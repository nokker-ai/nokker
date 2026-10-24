"""Vrai Postgres (docker jetable, `pgvector/pgvector:pg17` — PLAN F2) : migrations, append-only, store.

Marqué `integration` (réseau/service, pas de fakes) — ignoré par défaut : `NOKKER_TEST_PG_DSN` doit être
posé (voir `docs/REPO.md`/rapport F2 pour la commande qui lance le conteneur jetable)."""
from __future__ import annotations

import os

import psycopg
import pytest

from nokker.core.turn import InputCheck, Turn
from nokker.store.pg import PgStore, apply_migrations, turn_to_dict

DSN = os.environ.get("NOKKER_TEST_PG_DSN")

pytestmark = pytest.mark.integration
requires_pg = pytest.mark.skipif(not DSN, reason="NOKKER_TEST_PG_DSN non posé — pas de Postgres jetable")


@pytest.fixture(autouse=True)
def _tenant():
    """Les stores Postgres lisent et écrivent dans le locataire courant (RLS, docs/TENANCY.md §4)."""
    from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

    with tenant_scope(INTERNAL_TENANT):
        yield


@requires_pg
def test_migrations_are_idempotent():
    apply_migrations(DSN)
    apply_migrations(DSN)  # rejouer ne doit pas échouer (CREATE ... IF NOT EXISTS / OR REPLACE)


@requires_pg
def test_append_and_history_round_trip():
    apply_migrations(DSN)
    store = PgStore(DSN)
    t0 = Turn(thread_id="it-1", index=store.next_index("it-1"), input=InputCheck(text="bonjour"),
             channel="telegram", sender="alice")
    t0.reply = "bonjour Alice"
    store.append(t0)
    t1 = Turn(thread_id="it-1", index=store.next_index("it-1"), input=InputCheck(text="et ensuite ?"))
    store.append(t1)
    history = store.history("it-1")
    assert [t.index for t in history] == [0, 1]
    assert history[0].sender == "alice" and history[0].channel == "telegram"
    assert history[0].reply == "bonjour Alice"
    store.close()


@requires_pg
def test_turns_table_is_append_only_update_rejected():
    apply_migrations(DSN)
    store = PgStore(DSN)
    turn = Turn(thread_id="it-2", index=store.next_index("it-2"), input=InputCheck(text="x"))
    store.append(turn)
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute("UPDATE turns SET turn = turn WHERE thread_id = %s AND turn_index = 0", ("it-2",))
    store.close()


@requires_pg
def test_turns_table_is_append_only_delete_rejected():
    apply_migrations(DSN)
    store = PgStore(DSN)
    turn = Turn(thread_id="it-3", index=store.next_index("it-3"), input=InputCheck(text="x"))
    store.append(turn)
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute("DELETE FROM turns WHERE thread_id = %s", ("it-3",))
    store.close()


@requires_pg
def test_verdicts_and_llm_calls_tables_populated():
    from nokker.core.turn import Decision, Gate, LLMCall, Tier, Usage, Verdict

    apply_migrations(DSN)
    store = PgStore(DSN)
    turn = Turn(thread_id="it-4", index=store.next_index("it-4"), input=InputCheck(text="x"))
    turn.calls = [LLMCall(tier=Tier.SHIP, model="m", reasoning=False, prefix_sha256="h", layer_tokens={},
                          usage=Usage(prompt=1, completion=1), latency_s=0.1, route_reason="r")]
    turn.verdicts = [Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="r")]
    store.append(turn)
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM llm_calls WHERE thread_id = %s", ("it-4",))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM verdicts WHERE thread_id = %s", ("it-4",))
        assert cur.fetchone()[0] == 1
    store.close()


@requires_pg
def test_next_index_increments_per_thread():
    apply_migrations(DSN)
    store = PgStore(DSN)
    assert store.next_index("it-fresh") == 0
    store.append(Turn(thread_id="it-fresh", index=0, input=InputCheck(text="x")))
    assert store.next_index("it-fresh") == 1
    store.close()


@requires_pg
def test_turn_to_dict_is_jsonb_serializable():
    apply_migrations(DSN)
    turn = Turn(thread_id="it-5", index=0, input=InputCheck(text="x"))
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT %s::jsonb", (psycopg.types.json.Jsonb(turn_to_dict(turn)),))
        assert cur.fetchone()[0]["thread_id"] == "it-5"
