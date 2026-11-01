"""Traces sur Postgres jetable (migration 045, `PgTraceSink`, `PgTraceSource`). Lancé seulement si
`NOKKER_TEST_PG_DSN` désigne une base joignable (jamais une base de service)."""
from __future__ import annotations

import json
import os
import uuid

import pytest

from nokker.core.turn import Message
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from nokker.traces.export import PgTraceSource, export
from nokker.traces.recorder import PgTraceSink, TraceRecorder
from tests.unit.traces.test_traces import THREE_CALLS, opts, read_lines, run


@pytest.fixture(scope="module")
def dsn():
    value = os.environ.get("NOKKER_TEST_PG_DSN", "")
    if not value:
        pytest.skip("NOKKER_TEST_PG_DSN absent")
    psycopg = pytest.importorskip("psycopg")
    try:
        psycopg.connect(value, connect_timeout=2).close()
    except Exception:  # noqa: BLE001
        pytest.skip("base de test injoignable")
    from nokker.store.pg import apply_migrations

    apply_migrations(value, strict=True)
    apply_migrations(value, strict=True)  # rejouable
    return value


def test_pg_record_export_and_purge(dsn, tmp_path):
    thread = f"traces-{uuid.uuid4().hex[:8]}"
    rec = TraceRecorder(sink=PgTraceSink(dsn), kernel_sha="abc1234", target="canary")
    run(list(THREE_CALLS), thread=thread, rec=rec)
    src = PgTraceSource(dsn)
    rows = [r for r in src.recorded(INTERNAL_TENANT, opts().since, opts().until) if r["thread_id"] == thread]
    assert [r["call_index"] for r in rows] == [0, 1, 2]
    assert rows[0]["prompt"][0] == {"role": "system", "content": "Tu es Atlas."}
    lines = [ln for ln in read_lines(export(src, tmp_path, opts(reconstruct=False)).path) if ln["meta"]["thread"] == thread]
    assert [ln["meta"]["call"] for ln in lines] == [0, 1, 2] and lines[0]["meta"]["kernel_sha"] == "abc1234"
    # Rétention : une ligne vieillie de 40 jours part, les autres restent.
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("UPDATE trace_calls SET recorded_at = now() - interval '40 days' "
                     "WHERE thread_id = %s AND call_index = 0", (thread,))
    with tenant_scope(INTERNAL_TENANT):
        assert PgTraceSink(dsn).purge(30) >= 1
    left = [r["call_index"] for r in src.recorded(INTERNAL_TENANT, opts().since, opts().until) if r["thread_id"] == thread]
    assert left == [1, 2]
    assert json.dumps(rows[0]["meta"])  # JSONB relu en dict


def test_pg_source_reconstructs_from_turns_and_reads_consent(dsn, tmp_path):
    from nokker.onboarding.consents import PgConsents
    from nokker.store.pg import PgStore

    thread = f"traces-{uuid.uuid4().hex[:8]}"
    with tenant_scope(INTERNAL_TENANT):
        store = PgStore(dsn)
    run([Message("assistant", "bonjour")], text="salut", thread=thread, rec=TraceRecorder(sink=PgTraceSink(dsn)),
        store=store)
    src = PgTraceSource(dsn)
    assert src.turn(INTERNAL_TENANT, thread, 0)["input"]["text"] == "salut"
    with tenant_scope(INTERNAL_TENANT):
        PgConsents(dsn).record("us_cccccccccccc", "training", True, "account", None, None)
    assert src.training_consent(INTERNAL_TENANT, "us_cccccccccccc") is True
    assert src.training_consent(INTERNAL_TENANT, "us_dddddddddddd") is False
