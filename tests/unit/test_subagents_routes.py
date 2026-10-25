"""Sous-agents en arrière-plan côté routes locales : l'annulation du fil parent arrête ses enfants, `delegate_status`
appelable hors tour (`/agents` du client terminal) sans attente."""
from __future__ import annotations

import json
import threading

from nokker.tools.delegate import DelegateToolSource
from nokker.tools.registry import Registry
from tests.unit.test_local_api import _req
from tests.unit.test_local_tool_call import _server
from tests.unit.tools.test_delegate import FakeCode, Factory, ctx, turn


def _with_subagents(tmp_path):
    httpd, port, api, kernel, approvals, hub, l3, published, thread = _server(tmp_path)
    release, running = threading.Event(), threading.Event()

    def behave(k, inp):
        running.set()
        release.wait(5)
        return turn("fini")

    src = DelegateToolSource(Factory(behave=behave), FakeCode("/nonexistent"), background=True)
    kernel.tools = Registry(sources=[l3, src])
    src.bind(kernel.tools)
    api.subagents = src
    return httpd, port, api, src, thread, release, running


def test_annulation_du_fil_parent_arrete_ses_sous_agents(tmp_path):
    httpd, port, api, src, thread, release, running = _with_subagents(tmp_path)
    try:
        r = src.call("delegate", {"tasks": [{"goal": "g"}], "isolate": False, "background": True}, ctx(thread))
        assert not r.is_error
        running.wait(2)
        status, body = _req(port, f"/v1/threads/{thread}/cancel", token=api.token, body={})
        assert status == 202 and body["subagents_cancelled"] == 1
        res = json.loads(src.call("delegate_status", {"wait_s": 5}, ctx(thread)).content)["children"][0]
        assert res["exit_reason"] == "cancelled"
    finally:
        release.set()
        httpd.shutdown()


def test_delegate_status_hors_tour_sans_attente(tmp_path):
    httpd, port, api, src, thread, release, running = _with_subagents(tmp_path)
    try:
        src.call("delegate", {"tasks": [{"goal": "g"}], "isolate": False, "background": True}, ctx(thread))
        call = "/v1/local/tools/call"
        status, body = _req(port, call, token=api.token, body={"thread_id": thread, "name": "delegate_status"})
        assert status == 200 and not body["is_error"] and json.loads(body["content"])["running"] == 1
        assert _req(port, call, token=api.token, body={"thread_id": thread, "name": "delegate_status",
                                                       "arguments": {"wait_s": 3}})[0] == 400
        status, body = _req(port, call, token=api.token, body={"thread_id": thread, "name": "delegate_cancel"})
        assert status == 200 and not body["is_error"] and len(json.loads(body["content"])["cancelled"]) == 1
        api.subagents = None
        assert _req(port, call, token=api.token, body={"thread_id": thread, "name": "delegate_status"})[0] == 403
    finally:
        release.set()
        httpd.shutdown()


def test_carte_d_un_sous_agent_disparu_decidee_par_la_route_locale_est_refusee(tmp_path):
    """Câblage du vrai `make_handler` : l'exécuteur ne prend jamais le noyau du parent pour un fil `<fil>/sa-<id>`."""
    from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict
    from tests.unit.test_thread_serialization import _wait

    httpd, port, api, kernel, approvals, hub, l3, published, thread = _server(tmp_path)
    try:
        api.subagents = DelegateToolSource(Factory(), None, background=True)  # ne connaît aucun enfant (redémarrage)
        runs = []
        kernel.run_turn = lambda *a, **k: runs.append(a)
        child = Turn(thread_id=f"{thread}/sa-deadbeef", index=0, input=InputCheck(text=""), channel="subagent")
        card = approvals.request(child, ToolCall("c1", "checkpoint_restore", {"hash": "abc"}),
                                 Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="effect:destructive"),
                                 None)
        status, _ = _req(port, f"/v1/local/approvals/{card.id}/decide", token=api.token,
                         body={"decision": "approve", "scope": "once"})
        assert status in (200, 202)
        assert _wait(lambda: approvals.get(card.id).status == "failed")
        assert "sous-agent" in approvals.get(card.id).error and l3.calls == [] and runs == []
    finally:
        httpd.shutdown()


def test_annulation_du_fil_d_un_sous_agent_seul(tmp_path):
    """`/agents cancel <id>` du client terminal : le fil `<parent>/sa-<id>`, « / » en clair ou encodé."""
    httpd, port, api, src, thread, release, running = _with_subagents(tmp_path)
    try:
        r = src.call("delegate", {"tasks": [{"goal": "g"}], "isolate": False, "background": True}, ctx(thread))
        child = json.loads(r.content)["children"][0]["thread_id"]
        running.wait(2)
        encoded = child.replace("/", "%2F")
        status, body = _req(port, f"/v1/threads/{encoded}/cancel", token=api.token, body={})
        assert status == 202 and body["cancelled"] == child and body["subagents_cancelled"] == 1
        assert _req(port, f"/v1/threads/{child}/cancel", token=api.token, body={})[0] == 202
        assert _req(port, f"/v1/threads/{thread}/x/cancel", token=api.token, body={})[0] != 202
    finally:
        release.set()
        httpd.shutdown()
