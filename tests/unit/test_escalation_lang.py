"""Langue du texte d'escalade vu de la personne (C7f) : la langue de la requête (`Accept-Language`, contextvar de
`rule_labels`) doit arriver jusqu'à `person_why` dans les vrais chemins : tour dans la requête, tour en flux (fil de
travail), suite d'une carte décidée (fil d'exécution) et tour de fond d'une bulle. Vrai serveur, vraies requêtes."""
from __future__ import annotations

import threading

from nokker.approvals.rule_labels import rule_label, set_request_lang
from nokker.core.turn import Decision, Gate, Verdict
from tests.unit.test_fixes_e2e_contracts import _req as _plain_req  # noqa: F401  (même contrat de requête)
from nokker.core import model_texts as mt
from tests.unit.test_thread_serialization import _chat, _pending_card, _req, _server, _wait

RULE = "effect:send_external"
DE = rule_label(RULE, "de")
FR = rule_label(RULE, "fr")


class _EscOnSwitch:
    """Porte de bascule qui escalade (règle avec libellé) ; `only_suite` : seulement le tour de suite d'une carte."""

    def __init__(self, inner, only_suite=False):
        self.inner, self.only_suite = inner, only_suite

    def action(self, call, spec, turn):
        return self.inner.action(call, spec, turn)

    def switch(self, reply, turn, tier):
        if self.only_suite and not turn.input.text.startswith(tuple(mt.APPROVAL_OPEN.values())):
            return self.inner.switch(reply, turn, tier)
        return [Verdict(gate=Gate.SWITCH, decision=Decision.ESCALATE, layer=1, rule=RULE, reason="motif utile")]


def test_labels_differ_by_language():
    assert DE != FR and "Versand" in DE


def test_request_thread_turn_escalation_is_in_the_request_language(tmp_path):
    httpd, port, api, kernel, approvals, tools, model, _ = _server(tmp_path)
    model.release.set()
    kernel.gates = _EscOnSwitch(kernel.gates)
    try:
        out: list = []
        _chat(port, api.token, "th_lang1", "bonjour", out)
        # _chat n'envoie pas d'Accept-Language : français par défaut
        assert out[0][0] == 200 and FR in str(out[0][1]) and DE not in str(out[0][1])
        out = []
        out.append(_req(port, "/v1/chat/completions", token=api.token, timeout=20,
                        headers={"X-Session-Id": "th_lang2", "Accept-Language": "de-CH,de;q=0.9"},
                        body={"messages": [{"role": "user", "content": "bonjour"}]}))
        assert out[0][0] == 200 and DE in str(out[0][1]) and FR not in str(out[0][1]), out[0][1]
    finally:
        httpd.shutdown()


def test_streamed_turn_in_worker_thread_keeps_the_request_language(tmp_path):
    import urllib.request
    httpd, port, api, kernel, approvals, tools, model, _ = _server(tmp_path)
    model.release.set()
    kernel.gates = _EscOnSwitch(kernel.gates)
    try:
        import json
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/chat/completions", method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api.token}",
                     "X-Session-Id": "th_lang3", "Accept-Language": "de"},
            data=json.dumps({"stream": True, "messages": [{"role": "user", "content": "bonjour"}]}).encode())
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode()
        assert DE in body and FR not in body, body
    finally:
        httpd.shutdown()


def test_card_suite_turn_keeps_the_language_of_the_deciding_request(tmp_path):
    httpd, port, api, kernel, approvals, tools, model, _ = _server(tmp_path)
    model.release.set()
    thread = "th_lang4"
    kernel.gates = _EscOnSwitch(kernel.gates, only_suite=True)
    try:
        out: list = []
        _chat(port, api.token, thread, "agrandis le volume", out)
        card = _pending_card(approvals, thread)
        status, _b = _req(port, f"/v1/local/approvals/{card.id}/decide", token=api.token,
                          headers={"Accept-Language": "de"}, body={"decision": "approve", "scope": "once"})
        assert status in (200, 202)
        assert _wait(lambda: len(kernel.store.history(thread)) == 2)
        suite = kernel.store.history(thread)[1]
        text = " ".join(m.content or "" for m in suite.messages) + (suite.reply or "")
        assert DE in text and FR not in text, text
    finally:
        httpd.shutdown()


def test_bubble_background_turn_keeps_the_context_language(tmp_path):
    """`BubbleHub._run_background_turn` lance son tour par `spawn` (copie du contexte) : la langue de la requête qui a
    déclenché la tâche de fond est celle du texte d'escalade."""
    from nokker.bubbles import hub as hub_mod
    seen: list[str] = []
    done = threading.Event()

    def work():
        seen.append(rule_label(RULE))
        done.set()

    set_request_lang("de", None)
    try:
        hub_mod._spawn_thread(work)
        assert done.wait(5) and seen == [DE]
    finally:
        set_request_lang(None, None)
