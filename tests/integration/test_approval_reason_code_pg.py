"""`reason_code` de la carte (migration 087) sur un vrai Postgres, store connecté en `nokker_app` (sous RLS) : migration
rejouable, code relu tel quel, carte d'avant sans code → None, `arguments` intact. Ignoré sans `NOKKER_TEST_PG_DSN`."""
from __future__ import annotations

import pytest

from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict
from tests.integration.test_rls import A, pg, pod_env, requires_pg  # noqa: F401  (fixtures)

pytestmark = pytest.mark.integration


@requires_pg
def test_reason_code_column_roundtrip_legacy_null_and_arguments_intact(pg, pod_env, monkeypatch):  # noqa: F811
    import psycopg

    from nokker.approvals.pg import REASON_CODE_SQL, PgApprovals
    from nokker.store.conn import migrate
    from nokker.tenancy.context import scoped_thread_id, tenant_scope

    monkeypatch.setenv("NOKKER_PG_OWNER_DSN", pg["owner"])
    # idempotence : la migration repasse deux fois de plus sans erreur
    assert migrate(pg["owner"], REASON_CODE_SQL, rls=False, strict=True)
    assert migrate(pg["owner"], REASON_CODE_SQL, rls=False, strict=True)
    with psycopg.connect(pg["su"]) as conn:
        col = conn.execute("SELECT data_type, is_nullable FROM information_schema.columns "
                           "WHERE table_name = 'approvals' AND column_name = 'reason_code'").fetchone()
    assert col == ("text", "YES")

    approvals = PgApprovals(dsn=pg["app"])
    args = {"command": "make", "path": "a"}
    try:
        with tenant_scope(A):
            turn = Turn(thread_id=scoped_thread_id(A, "rc"), index=0, input=InputCheck(text="x"))
            with_code = approvals.request(
                turn, ToolCall(id="c1", name="patch", arguments=args),
                Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="permissions:accept_edits",
                        reason="mode accept_edits : écriture demandée", strength="standard",
                        code="accept_edits_write"), None, notify=False)
            no_code = approvals.request(
                turn, ToolCall(id="c2", name="patch", arguments=args),
                Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="effect:destructive",
                        strength="standard"), None, notify=False)
            got = approvals.get(with_code.id)
            assert got.reason_code == "accept_edits_write" and got.arguments == args
            assert approvals.get(no_code.id).reason_code is None
            # une décision (UPSERT) ne perd pas le code
            approvals.decide_auto(with_code.id, "approve", "ok", model="m", effect="write")
            assert approvals.get(with_code.id).reason_code == "accept_edits_write"
        with psycopg.connect(pg["su"]) as conn:
            raw = conn.execute("SELECT arguments, reason_code FROM approvals WHERE id = %s",
                               (with_code.id,)).fetchone()
            # carte d'avant 087 : ligne sans la valeur de la colonne
            conn.execute("UPDATE approvals SET reason_code = NULL WHERE id = %s", (no_code.id,))
            conn.commit()
        assert raw == (args, "accept_edits_write")  # rien de glissé dans `arguments`
        with tenant_scope(A):
            legacy = approvals.get(no_code.id)
            assert legacy.reason_code is None and legacy.arguments == args
    finally:
        approvals.close()
