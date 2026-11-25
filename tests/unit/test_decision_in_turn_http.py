"""Contrat hôte de « carte tranchée pendant le tour » (`local.approval_wait_s`, docs/HOST-EMBED.md) : vraies requêtes HTTP,
vraie boucle, vrais événements du hub (`tool.call`, `tool.result`, `turn.done`) et des approbations (`approval.requested`,
`approval.decided`), faux modèle. Chaque ligne du contrat de la doc est une assertion d'ici."""
from __future__ import annotations

import json
import threading
import time
import urllib.request

from nokker.core.ports import Completion
from nokker.core.turn import Message, ToolCall, Usage
from tests.unit.test_thread_serialization import (
    HistoryContext,  # noqa: F401 — réutilisé par `_server`
    _chat,
    _pending_card,
    _req,
    _server,
    _wait,
)

DRAFT = "Pour l'instant la commande attend ton approbation, je patiente. " * 4


class Model:
    """Appel 1 : `infra_lvextend` (carte). Ensuite d'après ce que le tour lui montre : résultat « en attente » → brouillon « en
    attente » (streamé s'il y a un puits) ; résultat réel → « Fait : … » ; refus → « sans cette action » ; bloc [Approbation]
    ajouté par le filet → « Fait (filet) »."""

    def __init__(self, hook=None):
        self.hook, self.n, self.release, self.card_posed = hook, 0, threading.Event(), threading.Event()
        self.release.set()

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048, on_delta=None):
        self.n += 1
        tool_msgs = [m["content"] for m in messages if m["role"] == "tool"]
        if not tool_msgs and not any("[Approbation]" in str(m["content"]) for m in messages[2:]):
            tc = ToolCall(id="call-lv", name="infra_lvextend", arguments={"path": "/srv/share", "size": "50G"})
            return Completion(message=Message(role="assistant", tool_calls=[tc]), usage=Usage(prompt=5), model="t",
                              latency_s=0.0)
        self.card_posed.set()
        self.release.wait(10)
        if any("[Approbation]" in str(m["content"]) for m in messages[2:]):
            reply = "Fait (filet)."
        elif tool_msgs[-1].startswith("EN ATTENTE"):
            reply = DRAFT
        elif tool_msgs[-1].startswith("Refusé par la personne"):
            reply = "D'accord, sans cette action."
        else:
            reply = "Fait : " + tool_msgs[-1]
        if on_delta is not None:
            for i in range(0, len(reply), 40):
                on_delta(reply[i:i + 40])
        if reply == DRAFT and self.hook is not None:
            self.hook()  # la carte est tranchée ENTRE le brouillon « en attente » et sa publication
        return Completion(message=Message(role="assistant", content=reply), usage=Usage(prompt=5), model="t", latency_s=0.0)


def _wired(tmp_path, wait_s, hook=None):
    httpd, port, api, kernel, approvals, tools, model, events = _server(tmp_path)
    m = Model(hook)
    kernel.providers = {next(iter(kernel.providers)): m}
    kernel.approval_wait_s = wait_s
    approvals.on_event = lambda kind, data: events.append((kind, data))
    return httpd, port, api, kernel, approvals, tools, m, events


def _decide(port, api, card, decision):
    return _req(port, f"/v1/local/approvals/{card.id}/decide", token=api.token, timeout=5,
                body={"decision": decision, "scope": "once"})


def _kinds(events, thread):
    return [k for k, d in events if d.get("thread_id", thread) == thread or "approval" in k]


def test_approved_in_the_window_the_decide_route_says_inline_and_the_same_turn_reports_the_result(tmp_path):
    httpd, port, api, kernel, approvals, tools, model, events = _wired(tmp_path, 5.0)
    thread = "th_inline"
    try:
        out: list = []
        t = threading.Thread(target=_chat, args=(port, api.token, thread, "agrandis le volume", out), daemon=True)
        t.start()
        card = _pending_card(approvals, thread)
        status, body = _decide(port, api, card, "approve")
        assert status == 202 and body["continuation"] == "inline" and body["approval_id"] == card.id
        t.join(10)
        content = out[0][1]["choices"][0]["message"]["content"]
        assert out[0][0] == 200 and content.startswith("Fait : ") and "attend" not in content and "Volume agrandi" in content
        assert tools.called == ["infra_lvextend"] and approvals.get(card.id).status == "executed"
        # séquence d'événements : appel, carte, décision, résultat de l'appel d'origine, fin du tour ; rien d'autre sur la carte
        kinds = [k for k, d in events if k in ("tool.call", "approval.requested", "approval.decided", "tool.result",
                                               "turn.done")]
        assert kinds == ["tool.call", "approval.requested", "approval.decided", "tool.result", "turn.done"], kinds
        result = [d for k, d in events if k == "tool.result"]
        assert len(result) == 1 and result[0]["call_id"] == "call-lv" and result[0]["ok"] is True
        assert "approval_id" not in result[0] and "status" not in result[0]  # un résultat ordinaire de la boucle
        assert not any(k == "turn.started" and d.get("turn_id") == f"{thread}:1" for k, d in events)  # pas de tour de suite
        assert [x.index for x in kernel.store.history(thread)] == [0]
    finally:
        httpd.shutdown()


def test_declined_in_the_window_is_a_refused_tool_result_and_the_turn_continues(tmp_path):
    httpd, port, api, kernel, approvals, tools, model, events = _wired(tmp_path, 5.0)
    thread = "th_declined"
    try:
        out: list = []
        t = threading.Thread(target=_chat, args=(port, api.token, thread, "agrandis le volume", out), daemon=True)
        t.start()
        card = _pending_card(approvals, thread)
        status, body = _decide(port, api, card, "reject")
        assert status == 202 and body["continuation"] == "inline"
        t.join(10)
        assert out[0][1]["choices"][0]["message"]["content"] == "D'accord, sans cette action." and tools.called == []
        [result] = [d for k, d in events if k == "tool.result"]
        assert result["call_id"] == "call-lv" and result["status"] == "refused" and result["reason"] == "declined"
        assert [x.index for x in kernel.store.history(thread)] == [0]  # pas de tour de suite
    finally:
        httpd.shutdown()


def test_nothing_visible_during_the_wait_then_decided_after_the_window_takes_the_usual_path(tmp_path):
    """Délai dépassé : le tour finit « en attente » ; la décision tardive passe par la suite habituelle (`running`)."""
    httpd, port, api, kernel, approvals, tools, model, events = _wired(tmp_path, 0.3)
    thread = "th_late"
    try:
        out: list = []
        _chat(port, api.token, thread, "agrandis le volume", out)
        content = out[0][1]["choices"][0]["message"]["content"]
        assert out[0][0] == 200 and "attend ton approbation" in content and tools.called == []
        card = _pending_card(approvals, thread)
        status, body = _decide(port, api, card, "approve")
        assert status == 202 and body["continuation"] == "running"
        assert _wait(lambda: approvals.get(card.id).status == "executed" and len(kernel.store.history(thread)) == 2)
        late = [d for k, d in events if k == "tool.result" and d.get("approval_id") == card.id]
        assert len(late) == 1 and late[0]["call_id"] == "call-lv"  # le résultat de l'appel d'origine, par l'exécuteur
        assert tools.called == ["infra_lvextend"]
    finally:
        httpd.shutdown()


def test_decided_after_the_window_while_the_turn_still_runs_is_queued(tmp_path):
    httpd, port, api, kernel, approvals, tools, model, events = _wired(tmp_path, 0.2)
    model.release.clear()
    thread = "th_queued2"
    try:
        out: list = []
        t = threading.Thread(target=_chat, args=(port, api.token, thread, "agrandis le volume", out), daemon=True)
        t.start()
        assert model.card_posed.wait(10)
        time.sleep(0.4)  # la fenêtre est passée, le tour tourne encore (modèle lent)
        card = _pending_card(approvals, thread)
        status, body = _decide(port, api, card, "approve")
        assert status == 202 and body["continuation"] == "queued" and tools.called == []
        model.release.set()
        t.join(10)
        assert _wait(lambda: approvals.get(card.id).status == "executed")
    finally:
        model.release.set()
        httpd.shutdown()


def test_setting_off_the_route_keeps_running_or_queued_never_inline(tmp_path):
    httpd, port, api, kernel, approvals, tools, model, events = _wired(tmp_path, 0.0)
    thread = "th_off"
    try:
        out: list = []
        _chat(port, api.token, thread, "agrandis le volume", out)
        card = _pending_card(approvals, thread)
        status, body = _decide(port, api, card, "approve")
        assert status == 202 and body["continuation"] == "running"
    finally:
        httpd.shutdown()


# ── X-Nokker-Stream-Tokens ──


def _stream(port, token, thread, text):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions", method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}", "X-Session-Id": thread,
                 "X-Nokker-Stream-Tokens": "1"},
        data=json.dumps({"stream": True, "messages": [{"role": "user", "content": text}]}).encode())
    seq: list[tuple[str, str]] = []
    with urllib.request.urlopen(req, timeout=30) as r:
        for raw in r:
            line = raw.decode().strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            obj = json.loads(line[6:])
            ext = obj.get("nokker") or {}
            delta = (obj["choices"][0].get("delta") or {}) if obj.get("choices") else {}
            if ext.get("final"):
                seq.append(("final", obj["choices"][0]["delta"].get("content", "")))
            elif ext.get("reset"):
                seq.append(("reset", ""))
            elif delta.get("content"):
                seq.append(("delta", delta["content"]))
    return seq


def test_stream_tokens_in_window_decision_never_streams_a_pending_draft(tmp_path):
    httpd, port, api, kernel, approvals, tools, model, events = _wired(tmp_path, 5.0)
    thread = "th_stream_inline"
    try:
        box: list = []
        t = threading.Thread(target=lambda: box.append(_stream(port, api.token, thread, "agrandis le volume")), daemon=True)
        t.start()
        card = _pending_card(approvals, thread)
        _decide(port, api, card, "approve")
        t.join(15)
        seq = box[0]
        assert "".join(x for k, x in seq if k == "delta").find("attend ton approbation") == -1
        assert seq[-1][0] == "final" and seq[-1][1].startswith("Fait : ")  # le texte du tour qui fait foi
    finally:
        httpd.shutdown()


def test_stream_tokens_late_card_streams_the_draft_then_a_reset_then_the_real_answer(tmp_path):
    """Filet : la carte est tranchée entre le brouillon « en attente » (déjà streamé) et sa publication : `reset` (le client
    vide le brouillon), puis le texte de la reprise, puis le chunk final qui fait foi. Le brouillon n'est jamais la réponse."""
    holder: dict = {}

    def decide_now():
        card = holder["approvals"].pending()[0]
        holder["approvals"].decide_local(card.id, "approve", "owner")

    httpd, port, api, kernel, approvals, tools, model, events = _wired(tmp_path, 0.2, hook=decide_now)
    holder["approvals"] = approvals
    thread = "th_stream_late"
    try:
        seq = _stream(port, api.token, thread, "agrandis le volume")
        kinds = [k for k, _ in seq]
        assert "reset" in kinds and kinds[-1] == "final"
        first_reset = kinds.index("reset")
        drafted = "".join(x for k, x in seq[:first_reset] if k == "delta")
        assert drafted and drafted in DRAFT and "attend" in drafted  # le brouillon est parti AVANT le reset
        after = "".join(x for k, x in seq[first_reset:] if k == "delta")
        assert "attend ton approbation" not in after  # rien du brouillon après le reset
        assert seq[-1][1] == "Fait (filet)."
        assert tools.called == ["infra_lvextend"]  # une seule exécution, par le filet
        assert [x.index for x in kernel.store.history(thread)] == [0]
    finally:
        httpd.shutdown()
