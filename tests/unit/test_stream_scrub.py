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


# ── retenue par début de secret (`secret_tail`, 05.11) ──
import base64 as _b64  # noqa: E402
import random  # noqa: E402

from nokker.core.stream import secret_tail  # noqa: E402

SSH_LIKE = ("-----BEGIN OPENSSH " + "PRIVATE KEY-----\n" + "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB" * 7
            + "\n-----END OPENSSH " + "PRIVATE KEY-----")  # forme d'une clé (≈ 430 car.), pas une vraie clé
OTHER = "Zq9-faux-" + "SECRET-de-test-8Lm3xQv"          # partage son début (« Zq9-faux-SECRET-de-test- ») avec FAKE


def _known(*pairs):
    k = KnownSecrets()
    for n, v in pairs:
        k.add(n, v)
    return k


class CheckingSink(Sink):
    """Vérifie à CHAQUE delta que le cumul émis est un début du texte final caviardé : jamais un caractère de secret."""

    def __init__(self, final: str) -> None:
        super().__init__()
        self.final = final

    def delta(self, text: str) -> None:
        super().delta(text)
        assert self.final.startswith(self.text), (self.text[-80:], self.final[:len(self.text)][-80:])


def _run(text, known, pieces, min_step=1):
    inv = DeterministicInvariants(known=known)
    sink = CheckingSink(inv.scrub_output(text))
    sd = ScrubbedDeltas(inv.scrub_output, sink, DEFAULT_HOLDBACK, known=known)
    sd.min_step = min_step
    for p in pieces:
        sd.feed(p)
    return sink, inv


def _assert_no_secret_bits(out, *secrets):
    bare = out
    for n in ("TEST_FAUX_SECRET", "AUTRE", "CLE_SSH"):
        bare = bare.replace(f"[secret:{n}]", "")
    flat = bare.replace(" ", "").replace("\t", "")
    for s in secrets:
        s2 = s.replace(" ", "").replace("\t", "")
        for i in range(len(s2) - 5):
            assert s2[i:i + 6] not in flat, s2[i:i + 6]


def test_secret_tail_measures_the_start_of_a_known_value_only():
    k = _known(("TEST_FAUX_SECRET", FAKE_SECRET))
    assert secret_tail("du texte ordinaire ", k) == 0
    assert secret_tail("la clé est " + FAKE_SECRET[:5], k) == 5
    assert secret_tail("la clé est " + " ".join(FAKE_SECRET[:7]), k) == len(" ".join(FAKE_SECRET[:7]))
    b64 = _b64.b64encode(FAKE_SECRET.encode()).decode()
    assert secret_tail("encodé : " + b64[:9], k) == 9
    assert secret_tail("x", None) == 0 and secret_tail("", k) == 0


INTRO = "Voici la configuration demandée pour le service, ligne par ligne, sans rien omettre du tout. "
OUTRO = " et elle reste privée. Suite du texte, encore du texte, pour finir proprement la réponse. "


def test_secret_cut_at_every_character_in_two_pieces():
    k = _known(("TEST_FAUX_SECRET", FAKE_SECRET))
    text = INTRO + "La clé est " + FAKE_SECRET + OUTRO
    for i in range(1, len(text)):
        sink, _ = _run(text, k, [text[:i], text[i:]])
        _assert_no_secret_bits(sink.text, FAKE_SECRET)


def test_secret_cut_at_every_pair_of_characters_around_it_in_three_pieces():
    k = _known(("TEST_FAUX_SECRET", FAKE_SECRET))
    text = INTRO + "La clé est " + FAKE_SECRET + OUTRO
    a = text.index(FAKE_SECRET) - 4
    b = a + len(FAKE_SECRET) + 8
    for i in range(a, b):
        for j in range(i + 1, b + 1):
            sink, _ = _run(text, k, [text[:i], text[i:j], text[j:]])
            _assert_no_secret_bits(sink.text, FAKE_SECRET)


def test_two_secrets_sharing_a_start():
    k = _known(("TEST_FAUX_SECRET", FAKE_SECRET), ("AUTRE", OTHER))
    assert FAKE_SECRET[:24] == OTHER[:24]
    text = INTRO + "Le second : " + OTHER + OUTRO
    for step in (1, 2, 5, 11, 23):
        sink, _ = _run(text, k, [text[i:i + step] for i in range(0, len(text), step)])
        _assert_no_secret_bits(sink.text, FAKE_SECRET, OTHER)
        assert "[secret:AUTRE]" in sink.text


def test_secret_starting_in_one_delta_right_after_another_secret():
    k = _known(("TEST_FAUX_SECRET", FAKE_SECRET), ("AUTRE", OTHER))
    for sep in ("", " ", "\t"):
        text = INTRO + "Les deux : " + FAKE_SECRET + sep + OTHER + OUTRO
        start2 = text.index(OTHER)
        for cut in range(start2 - 3, start2 + len(OTHER)):
            sink, _ = _run(text, k, [text[:cut], text[cut:cut + 7], text[cut + 7:]])
            _assert_no_secret_bits(sink.text, FAKE_SECRET, OTHER)


def test_spaced_variant_with_any_number_of_blanks():
    k = _known(("TEST_FAUX_SECRET", FAKE_SECRET))
    rnd = random.Random(7)
    for _ in range(20):
        spaced = "".join(ch + rnd.choice(["", " ", "  ", "\t", " \t "]) for ch in FAKE_SECRET).rstrip()
        text = INTRO + "Épelée : " + spaced + OUTRO
        for step in (1, 3, 8):
            sink, _ = _run(text, k, [text[i:i + step] for i in range(0, len(text), step)])
            _assert_no_secret_bits(sink.text, FAKE_SECRET)


def test_open_pem_block_is_still_held_with_known_secrets():
    k = _known(("CLE_SSH", SSH_LIKE))
    text = INTRO + "Le fichier commence par -----BEGIN OPENSSH PRIVATE KEY-----\nAAAAB3NzaC1yc2EAAAADAQABAAAB et continue "
    inv = DeterministicInvariants(known=k)
    sink = Sink()
    sd = ScrubbedDeltas(inv.scrub_output, sink, DEFAULT_HOLDBACK, known=k)
    sd.min_step = 1
    _feed_all(sd, text, 3)
    assert "BEGIN" not in sink.text and "AAAAB3" not in sink.text


def test_short_answers_stream_with_a_400_char_ssh_key_in_the_vault():
    """Avant : retenue 2 × 430 + 8 ≈ 870 caractères, une réponse de 200 caractères ne sortait qu'à la fin."""
    k = _known(("CLE_SSH", SSH_LIKE), ("TEST_FAUX_SECRET", FAKE_SECRET))
    assert holdback_for(DeterministicInvariants(known=k)) > 800
    reply = ("Il est vingt et une heures dix, jeudi, heure de Zurich. Le dossier de travail est vide pour le moment, "
             "dis-moi ce que tu veux faire et je m'en occupe tout de suite. ")
    assert len(reply) < 250
    sink, _ = _run(reply, k, [reply[i:i + 4] for i in range(0, len(reply), 4)], min_step=24)
    # tout sauf la retenue de base, au pas de caviardage (24) et au mot entier près
    assert sink.text and len(sink.text) >= len(reply) - DEFAULT_HOLDBACK - 24 - 20
    assert reply.startswith(sink.text)


def test_ssh_key_itself_never_leaves_even_split_finely():
    k = _known(("CLE_SSH", SSH_LIKE))
    text = INTRO + "La clé : " + SSH_LIKE + OUTRO
    for step in (1, 7, 64):
        sink, _ = _run(text, k, [text[i:i + step] for i in range(0, len(text), step)])
        assert "b3BlbnNzaC1r" not in sink.text and "BEGIN" not in sink.text


WORDS = ("le chat dort sur la table pendant que la pluie tombe doucement dehors et nous parlons du projet de la "
         "semaine avec calme puis nous rangeons les livres dans le salon avant le repas du soir").split()


def _secret(rnd):
    alpha = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz-_"
    return "Zq" + "".join(rnd.choice(alpha) for _ in range(rnd.randint(12, 40)))


@pytest.mark.parametrize("seed", range(300))
def test_property_no_secret_character_is_ever_emitted_before_redaction(seed):
    """Découpages aléatoires : à CHAQUE delta, le cumul émis est un début du texte final caviardé (CheckingSink), donc
    aucun début de secret (exact, espacé, base64) ne part avant que le scrubber l'ait remplacé."""
    rnd = random.Random(seed)
    secrets = [_secret(rnd) for _ in range(rnd.randint(1, 3))]
    if rnd.random() < 0.3:  # deux secrets au même début
        secrets.append(secrets[0][:rnd.randint(4, len(secrets[0]) - 2)] + _secret(rnd)[2:])
    if rnd.random() < 0.2:
        secrets.append(SSH_LIKE)
    k = _known(*[(f"S{i}", s) for i, s in enumerate(secrets)])
    parts = []
    for _ in range(rnd.randint(3, 12)):
        parts.append(" ".join(rnd.choice(WORDS) for _ in range(rnd.randint(1, 12))))
        if rnd.random() < 0.6:
            s = rnd.choice(secrets)
            form = rnd.choice(["exact", "exact", "spaced", "b64", "b64url"])
            if form == "spaced":
                s = "".join(ch + rnd.choice(["", " ", "\t", "  "]) for ch in s).rstrip()
            elif form == "b64":
                s = _b64.b64encode(s.encode()).decode().rstrip("=" if rnd.random() < 0.5 else "")
            elif form == "b64url":
                s = _b64.urlsafe_b64encode(s.encode()).decode()
            parts.append(s)
    text = " ".join(parts) + " fin."
    cuts = sorted(rnd.sample(range(1, len(text)), min(len(text) - 1, rnd.randint(1, 40))))
    pieces = [text[a:b] for a, b in zip([0, *cuts], [*cuts, len(text)])]
    _run(text, k, pieces, min_step=rnd.choice([1, 1, 5, 24]))


def test_variant_tables_forget_values_that_left_the_vault():
    from nokker.core import stream as st

    k = _known(("TEST_FAUX_SECRET", FAKE_SECRET), ("AUTRE", OTHER))
    secret_tail("texte " + FAKE_SECRET[:5], k)
    assert FAKE_SECRET in st._VARIANTS and OTHER in st._VARIANTS
    rotated = _known(("TEST_FAUX_SECRET", FAKE_SECRET + "-v2"))
    secret_tail("texte", rotated)
    assert FAKE_SECRET not in st._VARIANTS and OTHER not in st._VARIANTS
    assert set(st._VARIANTS) == {FAKE_SECRET + "-v2"}
