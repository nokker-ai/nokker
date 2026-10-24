"""`core/stream.py::ScrubbedDeltas` + branchement dans `Kernel.run_turn` — jamais un secret dans le flux."""
from __future__ import annotations

import pytest

from nokker.core.stream import DEFAULT_HOLDBACK, ScrubbedDeltas, holdback_for
from nokker.core.turn import Tier, TurnInput
from nokker.qa.invariants import DeterministicInvariants
from nokker.qa.secrets import KnownSecrets
from tests.unit.api.test_stream import StreamingShip, kernel_with

FAKE_SECRET = "Zq9-faux-" + "SECRET-de-test-7Hk2pWm"  # valeur inventée pour le test, jamais une vraie


class Sink:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def delta(self, text: str) -> None:
        self.events.append(("delta", text))

    def reset(self) -> None:
        self.events.append(("reset", ""))

    @property
    def text(self) -> str:
        return "".join(t for k, t in self.events if k == "delta")


def _invariants() -> DeterministicInvariants:
    known = KnownSecrets()
    known.add("TEST_FAUX_SECRET", FAKE_SECRET)
    return DeterministicInvariants(known=known)


def _feed_all(sd: ScrubbedDeltas, text: str, step: int) -> None:
    for i in range(0, len(text), step):
        sd.feed(text[i:i + step])


@pytest.mark.parametrize("step", [1, 3, 7, 16, 41])
def test_secret_split_between_deltas_never_leaves(step):
    inv = _invariants()
    sink = Sink()
    sd = ScrubbedDeltas(inv.scrub_output, sink, holdback_for(inv))
    sd.min_step = 1  # le pire cas : caviardage à chaque morceau
    text = ("Voici la configuration demandée pour le service, ligne par ligne, sans rien omettre. " * 2
            + f"La clé est {FAKE_SECRET} et elle reste privée. " + "Suite du texte, encore du texte. " * 6)
    _feed_all(sd, text, step)
    out = sink.text
    assert out, "rien n'a été streamé"
    assert FAKE_SECRET not in out
    # Pas même un morceau du secret (la coupe se fait sur un blanc : jamais un jeton à moitié).
    assert "[secret:TEST_FAUX_SECRET]" in out
    bare = out.replace("[secret:TEST_FAUX_SECRET]", "")
    for i in range(len(FAKE_SECRET) - 5):
        assert FAKE_SECRET[i:i + 6] not in bare
    assert inv.scrub_output(text).startswith(out)


def test_holdback_keeps_the_tail_and_short_text_is_not_emitted():
    sink = Sink()
    sd = ScrubbedDeltas(lambda t: t, sink, DEFAULT_HOLDBACK)
    _feed_all(sd, "Réponse courte de moins de quatre-vingts caractères.", 5)
    assert sink.events == []
    long = "mot " * 60
    sd2 = ScrubbedDeltas(lambda t: t, sink, DEFAULT_HOLDBACK)
    _feed_all(sd2, long, 4)
    assert len(long) - len(sink.text) >= DEFAULT_HOLDBACK
    assert long.startswith(sink.text)


def test_holdback_grows_with_the_longest_known_secret():
    known = KnownSecrets()
    known.add("LONG", "x" * 120)
    assert holdback_for(DeterministicInvariants(known=known)) == 248
    assert holdback_for(object()) == DEFAULT_HOLDBACK


def test_redaction_rewriting_emitted_text_stops_the_stream():
    calls = {"n": 0}

    def scrub(t: str) -> str:
        calls["n"] += 1
        return t if "BASCULE" not in t else "[réécrit] " + t

    sink = Sink()
    sd = ScrubbedDeltas(scrub, sink, 10)
    sd.min_step = 1
    _feed_all(sd, "du texte sans histoire qui part bien en direct puis ", 4)
    before = sink.text
    assert before
    _feed_all(sd, "BASCULE et encore beaucoup de texte derrière tout ça ", 4)
    assert sd.dead and sink.text == before


def test_unclosed_pem_block_is_held():
    sink = Sink()
    sd = ScrubbedDeltas(lambda t: t, sink, 5)
    sd.min_step = 1
    _feed_all(sd, "Avant le bloc. -----BEGIN " + "PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC " * 1, 3)
    assert "BEGIN" not in sink.text and "MIIE" not in sink.text


def test_discard_resets_only_if_something_was_emitted():
    sink = Sink()
    sd = ScrubbedDeltas(lambda t: t, sink, 5)
    sd.discard()
    assert sink.events == []
    sd = ScrubbedDeltas(lambda t: t, sink, 5)
    sd.min_step = 1
    _feed_all(sd, "un deux trois quatre cinq six sept ", 3)
    sd.discard()
    assert sink.events[-1] == ("reset", "")


def test_kernel_passes_on_delta_only_with_a_stream_and_a_capable_provider():
    ship = StreamingShip(script=[(["a "] * 10, []), (["b "] * 10, [])])
    k = kernel_with(ship)
    k.run_turn(TurnInput(thread_id="t1", user_text="x"))
    k.run_turn(TurnInput(thread_id="t1", user_text="y"), Sink())
    assert ship.seen_on_delta == [False, True]


def test_kernel_with_legacy_provider_and_stream_does_not_break():
    from tests.unit.api.test_server import ScriptedShip

    k = kernel_with(ScriptedShip("ok"))
    sink = Sink()
    turn = k.run_turn(TurnInput(thread_id="t2", user_text="x"), sink)
    assert turn.reply == "ok" and sink.events == []
    assert Tier.SHIP in k.providers


def test_provider_failing_mid_stream_resets_the_client_and_escalates():
    class Breaks:
        def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048, on_delta=None):
            on_delta("Début de réponse qui va être coupée net par l'amont, au milieu d'une phrase longue " * 2)
            raise RuntimeError("flux amont interrompu après le premier morceau")

    k = kernel_with(Breaks())
    sink = Sink()
    turn = k.run_turn(TurnInput(thread_id="t3", user_text="x"), sink)
    assert turn.escalated and turn.reply.startswith("escaladé")
    assert [kind for kind, _ in sink.events][-1] == "reset"


def test_stream_interrupted_resets_the_client_and_replays_without_streaming():
    """Vu en réel le 19.10 : l'amont coupe le flux après le 1er morceau → pas d'escalade, un rejeu sans stream."""
    from nokker.core.ports import StreamInterrupted
    from nokker.core.turn import Message, Usage
    from nokker.core.ports import Completion

    class Flaky:
        def __init__(self) -> None:
            self.calls: list[bool] = []

        def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048, on_delta=None):
            self.calls.append(on_delta is not None)
            if on_delta is not None:
                on_delta("Début de réponse qui va être coupée net par l'amont, au milieu d'une phrase longue " * 2)
                raise StreamInterrupted("flux amont interrompu après le premier morceau")
            return Completion(message=Message(role="assistant", content="Réponse complète."), usage=Usage(prompt=1, completion=1),
                              model="qwen/qwen3.8-27b", latency_s=0.01)

    ship = Flaky()
    k = kernel_with(ship)
    sink = Sink()
    turn = k.run_turn(TurnInput(thread_id="t4", user_text="x"), sink)
    assert ship.calls == [True, False]
    assert not turn.escalated and turn.reply == "Réponse complète."
    assert [kind for kind, _ in sink.events][-1] == "reset"


def _deep_policy():
    from nokker.core.ports import RouteDecision

    class DeepPolicy:
        def choose(self, state):
            return RouteDecision(tier=Tier.DEEP, reasoning=False, reason="test")

    return DeepPolicy()


def test_deep_unavailable_falls_back_to_ship_instead_of_escalating():
    """Vu en réel le 19.10 : Deep → HTTP 502 (page HTML) du Router → l'humain recevait la page. Repli Ship."""
    from tests.unit.api.test_server import ScriptedShip

    class Down:
        def complete(self, *a, **k):
            raise RuntimeError("HTTP 502 de https://router.sokkan.ch/v1: <!DOCTYPE html>\n<html>…")

    k = kernel_with(ScriptedShip("réponse de Ship"))
    k.providers[Tier.DEEP] = Down()
    k.policy = _deep_policy()
    turn = k.run_turn(TurnInput(thread_id="t5", user_text="x"))
    assert not turn.escalated and turn.reply == "réponse de Ship"
    assert [c.tier for c in turn.calls] == [Tier.SHIP]


def test_every_tier_down_escalates_without_html_in_the_reason():
    from nokker.core.loop import _short_error

    class Down:
        def complete(self, *a, **k):
            raise RuntimeError("HTTP 502 de https://router.sokkan.ch/v1: <!DOCTYPE html>\n<html class='x'>…")

    k = kernel_with(Down())
    k.providers[Tier.DEEP] = Down()
    k.policy = _deep_policy()
    turn = k.run_turn(TurnInput(thread_id="t6", user_text="x"))
    assert turn.escalated
    assert "<" not in turn.reply and "page d'erreur HTML" in turn.reply
    assert _short_error(RuntimeError("a  b\n c")) == "a b c"
