"""n°27 (b) : une carte REFUSÉE par l'hôte (`POST /v1/local/approvals/{id}/decide`, `decision: "reject"`) reprend le tour
comme une carte approuvée : le modèle reçoit le refus (bloc `[Approbation]` de la suite, même chemin que l'approbation) et
CONTINUE autrement ; le `tool.result` de l'appel d'origine part avec `status: "refused"` et `reason: "declined"`. Une seule
reprise par décision ; double décision et carte expirée : 409, aucune reprise. Vraie boucle, vraies requêtes HTTP,
faux fournisseur."""
from __future__ import annotations

import threading

from nokker.core.ports import Completion, ToolSpec
from nokker.core.turn import Effect, Message, ToolCall, Usage
from tests.unit.test_thread_serialization import (
    APPROBATION,
    _chat,
    _pending_card,
    _req,
    _server,
    _wait,
)

OTHER = "infra_other"


class RefusalAwareModel:
    """Tour humain : appelle `infra_lvextend` (carte), puis conclut. Suite (bloc [Approbation]) : si le bloc dit « rejected »,
    répond autrement (une autre voie) ; sinon « fait »."""

    def __init__(self, two_cards: bool = False):
        self.two_cards = two_cards
        self.suites: list[str] = []
        self.calls = 0
        self.lock = threading.Lock()

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        current = messages[1]["content"]
        with self.lock:
            self.calls += 1
        if current.startswith(APPROBATION):
            with self.lock:
                self.suites.append(current)
            text = "Je ne le fais pas ; autre voie proposée." if "Décision : rejected" in current else "fait"
            return Completion(message=Message(role="assistant", content=text), usage=Usage(prompt=5), model="t",
                              latency_s=0.0)
        if not any(m.get("role") == "tool" for m in messages):
            tcs = [ToolCall(id="call-lv", name="infra_lvextend", arguments={"path": "/srv/share", "size": "50G"})]
            if self.two_cards:
                tcs.append(ToolCall(id="call-ot", name=OTHER, arguments={"x": "1"}))
            return Completion(message=Message(role="assistant", tool_calls=tcs), usage=Usage(prompt=5), model="t",
                              latency_s=0.0)
        return Completion(message=Message(role="assistant", content="j'attends ta décision"), usage=Usage(prompt=5),
                          model="t", latency_s=0.0)


def _world(tmp_path, two_cards=False):
    httpd, port, api, kernel, approvals, tools, _, events = _server(tmp_path)
    tools.specs[OTHER] = ToolSpec(OTHER, "autre", {}, Effect.DESTRUCTIVE)
    m = RefusalAwareModel(two_cards)
    kernel.providers = {t: m for t in kernel.providers}
    return httpd, port, api, kernel, approvals, tools, m, events


def _decide(port, api, card, decision):
    return _req(port, f"/v1/local/approvals/{card.id}/decide", token=api.token, body={"decision": decision, "scope": "once"})


def _results(events, call_id):
    return [d for n, d in events if n == "tool.result" and d.get("call_id") == call_id and d.get("approval_id")]


def test_approved_card_resumes_and_runs_the_tool(tmp_path):
    httpd, port, api, kernel, approvals, tools, model, events = _world(tmp_path)
    try:
        _chat(port, api.token, "th_ok", "agrandis le volume", [])
        card = _pending_card(approvals, "th_ok")
        status, body = _decide(port, api, card, "approve")
        assert status == 202 and body["continuation"] in ("running", "queued")
        assert _wait(lambda: len(kernel.store.history("th_ok")) == 2)
        assert tools.called == ["infra_lvextend"] and "Décision : executed" in model.suites[0]
        assert _wait(lambda: len(_results(events, "call-lv")) == 1)
        assert "status" not in _results(events, "call-lv")[0]
    finally:
        httpd.shutdown()


def test_rejected_card_resumes_the_turn_and_the_model_answers_differently(tmp_path):
    httpd, port, api, kernel, approvals, tools, model, events = _world(tmp_path)
    try:
        _chat(port, api.token, "th_ko", "agrandis le volume", [])
        card = _pending_card(approvals, "th_ko")
        status, body = _decide(port, api, card, "reject")
        assert status == 202 and body["status"] == "rejected" and body["continuation"] in ("running", "queued"), body
        assert _wait(lambda: len(kernel.store.history("th_ko")) == 2), "le tour n'a pas repris après le refus"
        suite = kernel.store.history("th_ko")[1]
        assert suite.input.text.startswith(APPROBATION) and "Décision : rejected" in suite.input.text
        assert "refusé par" in suite.input.text
        assert len(model.suites) == 1                      # le fournisseur est rappelé UNE fois avec le refus
        assert "autre voie" in (suite.reply or "")         # réponse finale différente
        assert tools.called == []                          # l'outil n'a jamais tourné
        assert approvals.get(card.id).status == "rejected"
        assert _wait(lambda: len(_results(events, "call-lv")) == 1)
        ev = _results(events, "call-lv")[0]
        assert ev["status"] == "refused" and ev["reason"] == "declined" and ev["approval_id"] == card.id
    finally:
        httpd.shutdown()


def test_double_decision_and_late_refusal_do_not_resume_twice(tmp_path):
    httpd, port, api, kernel, approvals, tools, model, events = _world(tmp_path)
    try:
        _chat(port, api.token, "th_dbl", "agrandis le volume", [])
        card = _pending_card(approvals, "th_dbl")
        assert _decide(port, api, card, "reject")[0] == 202
        assert _wait(lambda: len(kernel.store.history("th_dbl")) == 2)
        for decision in ("reject", "approve"):
            status, _ = _decide(port, api, card, decision)
            assert status == 409
        threading.Event().wait(0.3)
        assert len(kernel.store.history("th_dbl")) == 2 and len(model.suites) == 1 and tools.called == []
        assert len(_results(events, "call-lv")) == 1
    finally:
        httpd.shutdown()


def test_refusal_after_expiry_is_refused_and_does_not_resume(tmp_path):
    httpd, port, api, kernel, approvals, tools, model, _ = _world(tmp_path)
    try:
        _chat(port, api.token, "th_exp", "agrandis le volume", [])
        card = _pending_card(approvals, "th_exp")
        approvals.clock = lambda: card.expires_at + 1          # la carte est périmée au moment du refus
        status, _ = _decide(port, api, card, "reject")
        assert status == 409
        threading.Event().wait(0.3)
        assert len(kernel.store.history("th_exp")) == 1 and model.suites == [] and tools.called == []
    finally:
        httpd.shutdown()


def test_two_cards_in_one_turn_one_approved_one_refused(tmp_path):
    httpd, port, api, kernel, approvals, tools, model, events = _world(tmp_path, two_cards=True)
    try:
        _chat(port, api.token, "th_two", "fais les deux", [])
        assert _wait(lambda: len([a for a in approvals.pending() if a.thread_id == "th_two"]) == 2)
        cards = {a.tool_name: a for a in approvals.pending() if a.thread_id == "th_two"}
        assert _decide(port, api, cards["infra_lvextend"], "approve")[0] == 202
        assert _decide(port, api, cards[OTHER], "reject")[0] == 202
        assert _wait(lambda: len(kernel.store.history("th_two")) == 3)
        assert tools.called == ["infra_lvextend"]                         # seule l'approuvée s'exécute
        assert sorted(("Décision : executed" in s, "Décision : rejected" in s) for s in model.suites) == [
            (False, True), (True, False)]
        assert len(_results(events, "call-lv")) == 1 and len(_results(events, "call-ot")) == 1
        assert _results(events, "call-ot")[0]["status"] == "refused"
    finally:
        httpd.shutdown()
