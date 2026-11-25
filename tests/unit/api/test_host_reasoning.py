"""Flux de réflexion vers le client (docs/NOKKAN-HOST-API.md §3, drapeau `host_reasoning_stream`).

- drapeau éteint : la réflexion de l'amont est ignorée comme avant (aucun champ, aucun événement, aucun rappel) ;
- drapeau allumé : elle sort dans un champ SÉPARÉ (`nokker.reasoning`, `turn.reasoning`), caviardée, jamais dans le
  texte de la réponse, jamais dans ce qui retourne au modèle ni dans ce qui est gardé du tour.
Par le VRAI serveur (`make_handler`) pour le flux et la réponse.
"""
from __future__ import annotations

import json

from nokker.config import NokkerConfig
from nokker.core.ports import Completion
from nokker.core.turn import Message, ToolCall, TurnInput, Usage
from tests.unit.api.host_api_support import Served, host_kernel

# Assez longue pour dépasser la retenue du caviardage (80 caractères) et partir en plusieurs morceaux en direct.
THOUGHT = ("La personne demande le total. " * 6 + "Je vérifie 17 fois 23 : 340 plus 51 font 391. "
           "Son mot de passe hunter2 ne doit pas sortir. " + "Je réponds par le nombre seul. " * 4).strip()
CLEAN = THOUGHT.replace("hunter2", "[secret:pw]")


class Thinker:
    """Amont scripté qui renvoie de la réflexion à côté de la réponse, en direct si on le lui demande."""

    model = "scripted"

    def __init__(self, script):
        self.script, self.seen, self.asked_live = list(script), [], []

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048, on_delta=None,
                 on_reasoning_delta=None):
        self.seen.append(messages)
        self.asked_live.append(on_reasoning_delta is not None)
        think, msg = self.script.pop(0)
        if on_reasoning_delta is not None:
            for i in range(0, len(think), 40):
                on_reasoning_delta(think[i:i + 40])
        if on_delta is not None and msg.content:
            on_delta(msg.content)
        return Completion(message=msg, usage=Usage(prompt=100), model=self.model, latency_s=0.01, reasoning=think)


def _kernel(script, *, on):
    from nokker.core.turn import Tier

    k, _ = host_kernel([])
    thinker = Thinker(script)
    k.providers[Tier.SHIP] = thinker
    k.reasoning_stream = on
    return k, thinker


def _text_sent_to_model(thinker) -> str:
    return json.dumps(thinker.seen, ensure_ascii=False)


# ─────────────── drapeau éteint = inchangé ───────────────


def test_flag_is_off_by_default_and_set_by_config_only():
    from nokker.api.server import configure_host_api

    k, _ = host_kernel([])
    assert NokkerConfig().host_reasoning_stream is False and k.reasoning_stream is False
    configure_host_api(NokkerConfig(), k, None)
    assert k.reasoning_stream is False
    configure_host_api(NokkerConfig(host_reasoning_stream=True), k, None)
    assert k.reasoning_stream is True


def test_flag_off_reasoning_is_dropped_everywhere(tmp_path):
    k, thinker = _kernel([(THOUGHT, Message("assistant", "391")), (THOUGHT, Message("assistant", "391"))], on=False)
    s = Served(tmp_path, k)
    try:
        status, chunks = s.chat("th_off", "combien ?", stream_tokens=True)
        assert status == 200 and thinker.asked_live == [False]  # l'amont n'est même pas écouté sur ce canal
        assert all(not any(key.startswith("reasoning") for key in (c.get("nokker") or {})) for c in chunks)
        final = next(c for c in chunks if (c.get("nokker") or {}).get("final"))
        assert final["nokker"] == {"reset": True, "final": True}  # le chunk final d'avant, à la clé près
        assert final["choices"][0]["delta"]["content"] == "391"
        status, out = s.chat("th_off", "encore ?")
        assert "nokker" not in out and out["choices"][0]["message"]["content"] == "391"
        assert s.events("turn.reasoning") == []
        assert all(t.reasoning == [] for t in k.store.turns)
        assert "La personne demande" not in json.dumps([e.data for e in s.bus.replay_since(0)], ensure_ascii=False)
    finally:
        s.close()


# ─────────────── drapeau allumé ───────────────


def test_streamed_reasoning_is_a_separate_field_never_mixed_with_the_answer(tmp_path):
    k, thinker = _kernel([(THOUGHT, Message("assistant", "391"))], on=True)
    s = Served(tmp_path, k)
    try:
        status, chunks = s.chat("th_on", "combien ?", stream_tokens=True)
        assert status == 200 and thinker.asked_live == [True]
        live = [c for c in chunks if "reasoning_delta" in (c.get("nokker") or {})]
        finals = [c for c in chunks if "reasoning" in (c.get("nokker") or {})]
        assert live and len(finals) == 1
        assert all(c["choices"][0]["delta"] == {} for c in live + finals)  # jamais dans `delta.content`
        # fin de l'appel : le texte complet, qui remplace les morceaux
        assert finals[0]["nokker"] == {"reasoning": {"call_index": 0, "text": CLEAN}}
        assert all(c["nokker"]["call_index"] == 0 for c in live)
        assert CLEAN.startswith("".join(c["nokker"]["reasoning_delta"] for c in live))
        assert chunks.index(finals[0]) > chunks.index(live[-1])
        answer = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
        assert "La personne" not in answer and "391" in answer
        assert "hunter2" not in json.dumps(chunks, ensure_ascii=False)  # caviardée, en direct comme à la fin
        final = next(c for c in chunks if (c.get("nokker") or {}).get("final"))
        assert final["choices"][0]["delta"]["content"] == "391" and final["nokker"] == {"reset": True, "final": True}
        events = s.events("turn.reasoning")
        assert events == [("turn.reasoning", {"thread_id": "th_on", "turn_id": "th_on:0", "call_index": 0,
                                              "text": CLEAN})]
        delta = s.events("turn.delta")[-1][1]
        assert delta["text"] == "391"  # l'événement de la réponse ne porte pas de réflexion
    finally:
        s.close()


def test_non_streamed_turn_returns_reasoning_beside_the_message(tmp_path):
    k, thinker = _kernel([(THOUGHT, Message("assistant", "391"))], on=True)
    s = Served(tmp_path, k)
    try:
        status, out = s.chat("th_ns", "combien ?")
        assert status == 200 and thinker.asked_live == [False]
        assert out["choices"][0]["message"] == {"role": "assistant", "content": "391"}
        assert out["nokker"] == {"reasoning": [{"call_index": 0, "text": CLEAN}]}
    finally:
        s.close()


def test_reasoning_never_returns_to_the_model_nor_stays_in_the_turn():
    tool = Message("assistant", tool_calls=[ToolCall(id="c1", name="tool_find", arguments={"query": "agenda"})])
    k, thinker = _kernel([("Première réflexion : chercher un outil.", tool),
                          ("Deuxième réflexion : conclure.", Message("assistant", "Voilà.")),
                          ("Troisième réflexion : tour suivant.", Message("assistant", "Encore."))], on=True)
    seen = []

    class Obs:
        def __getattr__(self, name):
            return lambda *a, **kw: None

        def turn_reasoning(self, turn, data):
            seen.append((turn.index, data["call_index"], data["text"]))

    k.observer = Obs()
    first = k.run_turn(TurnInput("th_m", "trouve l'agenda"))
    second = k.run_turn(TurnInput("th_m", "et ensuite ?"))
    assert first.reply == "Voilà." and second.reply == "Encore."
    assert first.reasoning == [{"call_index": 0, "text": "Première réflexion : chercher un outil."},
                               {"call_index": 1, "text": "Deuxième réflexion : conclure."}]
    assert seen == [(0, 0, "Première réflexion : chercher un outil."), (0, 1, "Deuxième réflexion : conclure."),
                    (1, 0, "Troisième réflexion : tour suivant.")]
    assert len(thinker.seen) == 3 and "réflexion" not in _text_sent_to_model(thinker)  # ni dans le tour, ni au suivant
    for turn in k.store.turns:
        assert "réflexion" not in json.dumps([m.to_openai() for m in turn.messages], ensure_ascii=False)
        assert "réflexion" not in turn.reply


def test_upstream_without_reasoning_exposes_nothing(tmp_path):
    k, thinker = _kernel([("", Message("assistant", "391")), ("   ", Message("assistant", "391"))], on=True)
    s = Served(tmp_path, k)
    try:
        assert "nokker" not in s.chat("th_e", "combien ?")[1]
        chunks = s.chat("th_e", "encore ?", stream_tokens=True)[1]
        assert all(not any(key.startswith("reasoning") for key in (c.get("nokker") or {})) for c in chunks)
        assert s.events("turn.reasoning") == []
    finally:
        s.close()


def test_provider_that_cannot_stream_reasoning_still_gives_it_at_the_end_of_the_call(tmp_path):
    class Plain:
        model = "scripted"

        def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048, on_delta=None):
            return Completion(message=Message("assistant", "391"), usage=Usage(prompt=10), model=self.model,
                              latency_s=0.01, reasoning="Calcul fait.")

    from nokker.core.turn import Tier

    k, _ = host_kernel([])
    k.providers[Tier.SHIP], k.reasoning_stream = Plain(), True
    s = Served(tmp_path, k)
    try:
        chunks = s.chat("th_p", "combien ?", stream_tokens=True)[1]
        think = [c["nokker"] for c in chunks if any(key.startswith("reasoning") for key in (c.get("nokker") or {}))]
        assert think == [{"reasoning": {"call_index": 0, "text": "Calcul fait."}}]
    finally:
        s.close()


def test_a_broken_sink_never_breaks_the_turn():
    k, thinker = _kernel([(THOUGHT, Message("assistant", "391"))], on=True)

    class Sink:
        def delta(self, text):
            pass

        def reset(self):
            pass

        def reasoning(self, text, final=False, call_index=0):
            if final:
                raise RuntimeError("client parti")

    turn = k.run_turn(TurnInput("th_b", "combien ?"), Sink())
    assert turn.reply == "391" and turn.error is None
