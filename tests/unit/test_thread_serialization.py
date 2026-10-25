"""File par fil (docs/LOKKAL.md §7.4.9) : les tours d'un même fil ne tournent jamais en parallèle, la décision d'une
carte répond tout de suite et son exécution suit en fond, l'exécuteur publie `tool.result` pour l'appel en attente.

Reproduit les trois défauts vus par l'essai du client des éditeurs (X12) contre un vrai noyau local : faux modèle lent,
vraies requêtes HTTP, vraie boucle."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from nokker.api.events import EventBus
from nokker.api.server import build_hub, make_handler
from nokker.approvals.memory import InMemoryApprovals
from nokker.config import NokkerConfig
from nokker.core.ports import Assembled, Completion, RouteDecision
from nokker.core.turn import Message, Tier, ToolCall, TurnInput, Usage
from nokker.local.api import LOCAL_OWNER
from tests.unit.api.test_approvals_routes import make_kernel_with_approvals
from tests.unit.test_local_api import _api

APPROBATION = "[Approbation]"


class HistoryContext:
    """Rejoue l'historique du fil (textes des tours précédents) dans un message système, puis le tour courant : le faux
    modèle voit ainsi ce que verrait le vrai (sans l'historique, une suite « n'a pas le contexte »)."""

    def assemble(self, turn, history, recall, loaded, tier):
        past = [f"{t.index}:{t.input.text}" for t in history]
        msgs = [{"role": "system", "content": "HIST|" + "|".join(past)},
                {"role": "user", "content": turn.input.text}]
        msgs += [m.to_openai() for m in turn.messages]
        return Assembled(messages=msgs, tools=[], prefix_sha256="fixed")

    def admit_result(self, r):
        return r


class SlowModel:
    """Premier appel d'un tour humain : `infra_lvextend` (carte). Appel suivant du même tour : signale que la carte est
    posée, puis attend `release` (le tour est lent). Tour de suite (bloc [Approbation]) : note l'historique vu, attend
    `release_suite` s'il est posé, et répond."""

    def __init__(self):
        self.card_posed = threading.Event()
        self.release = threading.Event()
        self.release_suite: threading.Event | None = None
        self.suite_history: list[str] = []
        self.log: list[str] = []
        self._lock = threading.Lock()

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        hist = messages[0]["content"]
        current = messages[1]["content"]
        if current.startswith(APPROBATION):
            with self._lock:
                self.suite_history.append(hist)
                self.log.append("suite:start")
            if self.release_suite is not None:
                self.release_suite.wait(10)
            with self._lock:
                self.log.append("suite:end")
            return Completion(message=Message(role="assistant", content="suite faite"), usage=Usage(prompt=5),
                              model="t", latency_s=0.0)
        if not any(m.get("role") == "tool" for m in messages):
            tc = ToolCall(id="call-lv", name="infra_lvextend", arguments={"path": "/srv/share", "size": "50G"})
            return Completion(message=Message(role="assistant", tool_calls=[tc]), usage=Usage(prompt=5), model="t",
                              latency_s=0.0)
        self.card_posed.set()
        self.release.wait(10)
        with self._lock:
            self.log.append("first:end")
        return Completion(message=Message(role="assistant", content="j'attends ta décision"), usage=Usage(prompt=5),
                          model="t", latency_s=0.0)


def _req(port, path, *, token, body=None, headers=None, timeout=5.0):
    h = {"Content-Type": "application/json", "Authorization": f"Bearer {token}", **(headers or {})}
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _server(tmp_path):
    approvals = InMemoryApprovals()
    kernel, tools = make_kernel_with_approvals(approvals)
    model = SlowModel()
    kernel.context = HistoryContext()
    kernel.providers = {Tier.SHIP: model}

    class Pol:
        def choose(self, state):
            return RouteDecision(tier=Tier.SHIP, reasoning=False, reason="t")

    kernel.policy = Pol()
    hub = build_hub(NokkerConfig(web_search_enabled=False, approvals_enabled=False), EventBus())
    events: list[tuple[str, dict]] = []
    hub.publish = lambda kind, data: events.append((kind, data))
    hub.kernel = kernel
    kernel.observer = hub
    api = _api(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: LOCAL_OWNER}, approvals=approvals,
                                                               local_api=api, hub=hub))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port, api, kernel, approvals, tools, model, events


def _chat(port, token, thread, text, out):
    out.append(_req(port, "/v1/chat/completions", token=token, headers={"X-Session-Id": thread}, timeout=20,
                    body={"messages": [{"role": "user", "content": text}]}))


def _wait(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def _pending_card(approvals, thread):
    assert _wait(lambda: any(a.thread_id == thread for a in approvals.pending()))
    return next(a for a in approvals.pending() if a.thread_id == thread)


def test_decision_during_the_turn_waits_for_it_then_sees_its_context(tmp_path):
    """Défaut 1 : la décision arrive PENDANT le tour qui a posé la carte. Le tour de suite attend la fin de ce tour, voit
    son contexte, prend l'index suivant ; rien n'est perdu."""
    httpd, port, api, kernel, approvals, tools, model, _ = _server(tmp_path)
    thread = "th_serial"
    try:
        first: list = []
        t = threading.Thread(target=_chat, args=(port, api.token, thread, "agrandis le volume", first), daemon=True)
        t.start()
        assert model.card_posed.wait(10)  # la carte est posée, le tour tourne encore (modèle lent)
        card = _pending_card(approvals, thread)
        status, body = _req(port, f"/v1/local/approvals/{card.id}/decide", token=api.token,
                            body={"decision": "approve", "scope": "once"})
        assert status in (200, 202), body
        assert model.suite_history == []  # la suite n'a pas démarré pendant le tour
        model.release.set()
        t.join(10)
        assert first and first[0][0] == 200
        assert _wait(lambda: len(kernel.store.history(thread)) == 2)
        assert _wait(lambda: approvals.get(card.id).status == "executed")
        turns = kernel.store.history(thread)
        assert [x.index for x in turns] == [0, 1]  # pas d'index en double
        assert turns[0].input.text == "agrandis le volume" and turns[1].input.text.startswith(APPROBATION)
        assert model.log == ["first:end", "suite:start", "suite:end"]
        assert model.suite_history == ["HIST|0:agrandis le volume"]  # la suite a le contexte du tour qui l'a posée
        assert tools.called == ["infra_lvextend"]
    finally:
        model.release.set()
        httpd.shutdown()


def test_executor_publishes_tool_result_for_the_waiting_call(tmp_path):
    """Défaut 2 : l'outil approuvé publie `tool.result` (même `call_id` que l'appel en attente, `approval_id`, sortie
    caviardée), à l'audience du fil, avant le tour de suite."""
    httpd, port, api, kernel, approvals, tools, model, events = _server(tmp_path)
    thread = "th_result"
    model.release.set()
    try:
        first: list = []
        _chat(port, api.token, thread, "agrandis le volume", first)
        assert first[0][0] == 200
        card = _pending_card(approvals, thread)
        status, _ = _req(port, f"/v1/local/approvals/{card.id}/decide", token=api.token,
                         body={"decision": "approve", "scope": "once"})
        assert status in (200, 202)
        assert _wait(lambda: len(kernel.store.history(thread)) == 2)
        results = [d for k, d in events if k == "tool.result" and d.get("approval_id") == card.id]
        assert len(results) == 1, [k for k, _ in events]
        r = results[0]
        assert r["call_id"] == "call-lv" and r["name"] == "infra_lvextend" and r["is_error"] is False
        assert r["thread_id"] == thread and "Volume agrandi" in r["preview"]
        assert r["_audience"] == [LOCAL_OWNER]
        kinds = [(k, d.get("turn_id")) for k, d in events]
        assert kinds.index(("tool.result", r.get("turn_id"))) < kinds.index(("turn.started", f"{thread}:1"))
    finally:
        httpd.shutdown()


def test_decide_answers_before_the_suite_turn_ends(tmp_path):
    """Défaut 3 : `decide` ne tient pas la requête pendant le tour de suite (un client coupe au-delà de 5 min)."""
    httpd, port, api, kernel, approvals, tools, model, _ = _server(tmp_path)
    thread = "th_fast"
    model.release.set()
    model.release_suite = threading.Event()
    try:
        first: list = []
        _chat(port, api.token, thread, "agrandis le volume", first)
        card = _pending_card(approvals, thread)
        t0 = time.monotonic()
        status, body = _req(port, f"/v1/local/approvals/{card.id}/decide", token=api.token, timeout=3,
                            body={"decision": "approve", "scope": "once"})
        assert time.monotonic() - t0 < 2.5
        assert status == 202 and body["continuation"] == "running" and body["approval_id"] == card.id
        assert body["scope"] == "once" and body["status"] in ("approved", "executed")
        assert _wait(lambda: "suite:start" in model.log)
        assert "suite:end" not in model.log  # la suite tourne encore, la réponse est déjà partie
        model.release_suite.set()
        assert _wait(lambda: len(kernel.store.history(thread)) == 2)
    finally:
        if model.release_suite is not None:
            model.release_suite.set()
        httpd.shutdown()


def test_decision_while_a_turn_runs_is_queued_and_reject_has_no_continuation(tmp_path):
    httpd, port, api, kernel, approvals, tools, model, _ = _server(tmp_path)
    thread = "th_queued"
    try:
        first: list = []
        t = threading.Thread(target=_chat, args=(port, api.token, thread, "agrandis le volume", first), daemon=True)
        t.start()
        assert model.card_posed.wait(10)
        card = _pending_card(approvals, thread)
        status, body = _req(port, f"/v1/local/approvals/{card.id}/decide", token=api.token, timeout=3,
                            body={"decision": "approve", "scope": "once"})
        assert status == 202 and body["continuation"] == "queued" and body["status"] == "approved"
        assert tools.called == []  # l'outil attend lui aussi la fin du tour
        model.release.set()
        t.join(10)
        assert _wait(lambda: len(kernel.store.history(thread)) == 2)
        # Un refus : rien ne tourne après (comme avant), réponse 200.
        model.card_posed.clear()
        second: list = []
        _chat(port, api.token, "th_reject", "agrandis le volume", second)
        card2 = _pending_card(approvals, "th_reject")
        status, body = _req(port, f"/v1/local/approvals/{card2.id}/decide", token=api.token,
                            body={"decision": "reject", "scope": "once"})
        assert status == 200 and body["continuation"] == "none" and body["status"] == "rejected"
    finally:
        model.release.set()
        httpd.shutdown()


def test_user_message_during_a_turn_is_queued_with_its_own_index(tmp_path):
    """Un message utilisateur arrivé pendant un tour attend son tour (file FIFO), ne prend pas le même index."""
    httpd, port, api, kernel, approvals, tools, model, _ = _server(tmp_path)
    thread = "th_msgs"
    try:
        first: list = []
        t = threading.Thread(target=_chat, args=(port, api.token, thread, "agrandis le volume", first), daemon=True)
        t.start()
        assert model.card_posed.wait(10)
        second: list = []
        t2 = threading.Thread(target=_chat, args=(port, api.token, thread, "[Approbation] et ensuite ?", second),
                              daemon=True)
        t2.start()
        time.sleep(0.3)
        assert model.suite_history == []  # le second message n'a pas démarré
        model.release.set()
        t.join(10)
        t2.join(10)
        assert first[0][0] == 200 and second[0][0] == 200
        assert [x.index for x in kernel.store.history(thread)] == [0, 1]
        assert model.suite_history == ["HIST|0:agrandis le volume"]
    finally:
        model.release.set()
        httpd.shutdown()


def test_kernel_serializes_turns_of_a_thread_for_every_caller(tmp_path):
    """La file est dans le noyau : un appelant qui ne passe pas par le hub (CLI, sous-agents) est sérialisé aussi."""
    httpd, port, api, kernel, approvals, tools, model, _ = _server(tmp_path)
    try:
        threads = [threading.Thread(target=kernel.run_turn,
                                    args=(TurnInput(thread_id="th_k", user_text="agrandis le volume",
                                                    sender=LOCAL_OWNER),), daemon=True) for _ in range(3)]
        for x in threads:
            x.start()
        assert model.card_posed.wait(10)
        model.release.set()
        for x in threads:
            x.join(10)
        assert [x.index for x in kernel.store.history("th_k")] == [0, 1, 2]
    finally:
        model.release.set()
        httpd.shutdown()


def test_queued_suite_is_dropped_when_the_thread_is_cancelled(tmp_path):
    """Annulation (§7.4.4) pendant qu'une suite attend dans la file : l'outil n'est pas lancé, la carte finit `failed`
    et son `tool.result` (erreur) ferme l'appel en attente."""
    httpd, port, api, kernel, approvals, tools, model, events = _server(tmp_path)
    thread = "th_cancel"
    try:
        first: list = []
        t = threading.Thread(target=_chat, args=(port, api.token, thread, "agrandis le volume", first), daemon=True)
        t.start()
        assert model.card_posed.wait(10)
        card = _pending_card(approvals, thread)
        status, body = _req(port, f"/v1/local/approvals/{card.id}/decide", token=api.token,
                            body={"decision": "approve", "scope": "once"})
        assert status == 202 and body["continuation"] == "queued"
        assert _req(port, f"/v1/threads/{thread}/cancel", token=api.token, body={})[0] == 202
        model.release.set()
        t.join(10)
        assert _wait(lambda: approvals.get(card.id).status == "failed")
        assert tools.called == [] and model.suite_history == []
        assert _wait(lambda: any(k == "tool.result" and d.get("approval_id") == card.id and d["is_error"]
                                 for k, d in events))
    finally:
        model.release.set()
        httpd.shutdown()


def test_turn_lock_is_fair_reentrant_and_keeps_reserved_places():
    from nokker.core.turn_lock import TurnLock

    lock = TurnLock()
    order: list[str] = []
    lock.acquire()
    assert lock.acquire(blocking=False)  # réentrant
    lock.release()
    first = lock.reserve()  # une suite réservée AVANT les autres garde son rang
    assert not first.immediate and lock.busy()

    def worker(name, ticket=None):
        lock.acquire(ticket=ticket)
        order.append(name)
        lock.release()

    ws = [threading.Thread(target=worker, args=("suite", first), daemon=True)]
    for name in ("b", "c", "d"):
        ws.append(threading.Thread(target=worker, args=(name,), daemon=True))
        ws[-1].start()
        _wait(lambda n=len(ws): lock.waiting() == n)
    ws[0].start()
    other: list[bool] = []
    probe = threading.Thread(target=lambda: other.append(lock.acquire(blocking=False)), daemon=True)
    probe.start()
    probe.join(5)
    assert other == [False]  # tenu (par ce fil d'exécution) : un autre n'entre pas sans attendre
    lock.release()
    for w in ws:
        w.join(5)
    assert order == ["suite", "b", "c", "d"]  # premier arrivé, premier servi
    assert not lock.busy()
    blocker = lock.reserve()  # place réservée sans travail : le verrou n'est pas pris sans attendre (pas de saut)
    assert blocker.immediate and not lock.acquire(blocking=False)
    assert not lock.acquire(timeout=0.05)  # délai : la place est rendue
    lock.cancel(blocker)
    assert lock.acquire(blocking=False)
    lock.release()
    assert not lock.busy()
