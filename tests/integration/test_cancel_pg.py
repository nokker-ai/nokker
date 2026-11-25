"""`POST /v1/threads/{id}/cancel` avec les cartes en Postgres (store connecté en `nokker_app`, sous RLS) : la route lisait
et marquait les cartes sans locataire (NoTenant → 500). Ignoré sans `NOKKER_TEST_PG_DSN`."""
from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

import pytest

from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict
from tests.integration.test_rls import A, B, pg, pod_env, requires_pg  # noqa: F401  (fixtures)

pytestmark = pytest.mark.integration


def _card(approvals, thread_id, call_id):
    turn = Turn(thread_id=thread_id, index=0, input=InputCheck(text="x"))
    return approvals.request(turn, ToolCall(id=call_id, name="patch", arguments={"path": "a"}),
                             Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="permissions:default",
                                     strength="standard"), None, notify=False)


@requires_pg
@pytest.mark.parametrize("tenant", ["internal", A])
def test_cancel_route_retires_the_cards_of_the_thread_under_rls(pg, pod_env, monkeypatch, tmp_path, tenant):  # noqa: F811
    from nokker.api.server import make_handler
    from nokker.approvals.pg import PgApprovals
    from nokker.local.api import LOCAL_OWNER
    from nokker.tenancy.context import INTERNAL_TENANT, scoped_thread_id, tenant_scope
    from tests.unit.api.test_server import make_kernel
    from tests.unit.test_local_api import _api, _req

    monkeypatch.setenv("NOKKER_PG_OWNER_DSN", pg["owner"])
    tid = INTERNAL_TENANT if tenant == "internal" else tenant
    thread = scoped_thread_id(tid, f"th_cancel_{tenant[-4:]}")
    other = scoped_thread_id(tid, f"th_other_{tenant[-4:]}")
    approvals = PgApprovals(dsn=pg["app"])
    api = _api(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), {api.token: LOCAL_OWNER},
                                                               local_api=api, approvals=approvals))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    srv = threading.Thread(target=httpd.serve_forever, daemon=True)
    srv.start()
    try:
        with tenant_scope(tid):
            mine, sub, foreign = _card(approvals, thread, "c1"), _card(approvals, thread + "/sa-1", "c2"), \
                _card(approvals, other, "c3")
        status, body = _req(port, f"/v1/threads/{thread.replace('/', '%2F')}/cancel", token=api.token, body={})
        assert status == 202, body  # 500 (NoTenant) avant la correction
        assert body["approvals_cancelled"] == 2 and body["running"] is False
        with tenant_scope(tid):
            assert approvals.get(mine.id).status == "failed" and approvals.get(sub.id).status == "failed"
            assert approvals.get(foreign.id).status == "pending"  # un autre fil du même locataire : intact
    finally:
        httpd.shutdown()
        httpd.server_close()
        srv.join(timeout=5)
        approvals.close()
