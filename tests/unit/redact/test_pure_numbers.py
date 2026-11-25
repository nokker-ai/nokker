"""Nombres décimaux et scientifiques : jamais caviardés (flottants d'un outil `add`) ; secrets et PII : toujours masqués.

Deux familles côte à côte. Règle (docs/INVARIANTS.md, « Nombres purs ») : seul un jeton ENTIÈREMENT décimal (un point entre
deux chiffres) ou scientifique (exposant) est relâché ; tout entier reste jugé comme avant ; cas ambigu : on masque."""
from __future__ import annotations

import re

import pytest

from nokker.core.loop import stall_signature
from nokker.core.turn import ToolCall, ToolResult
from nokker.payments.cardmask import mask_card_text, mask_tool_text
from nokker.qa.invariants import DeterministicInvariants
from nokker.qa.secrets import redact
from nokker.redact.numbers import number_spans
from nokker.traces.pii import scrub_pii

INTACT = [
    '{"result": 0.30000000000000004}',
    "1.2345678901234567",
    "0.4242424242424242",
    "3.141592653589793",
    "-2.718281828459045",
    "Total 1.4111111111111111 et 5.4242424242424243e-3",
    "[0.1234567890123456, 2.718281828459045]",
    "[0.30000000000000004, 0.6000000000000001, 4.000000000000001, 5.551115123125783e-17]",
    '{"values": [4.440892098500626e-16, -2.220446049250313e-16, 3.3000000000000003]}',
    '{"a": 0.30000000000000004, "b": [1.2345678901234567, -3.141592653589793e-05]}',
    "-3.141592653589793e-05",
    "1.2345678901234567e+300",
    "6.02214076e23",
    "1e-300",
    "-3.14e-05",
    "x = 6.02214076e23 mol-1",
    "12.5 kg",
    "9.80665 m/s²",
    "result: 2345678901234567.5",
    "0.12345678901234567890123456789012",
    "12345.6789012345678",
    "1e15",
    "12345",
    "total 42.90 CHF",
    "n = 123456789012",
    "1e300 et 2e5",
]

MASKED = [
    "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "ghp_" + "AbCdEfGhIjKlMnOpQrSt" + "UvWxYz0123456789",
    "AKIAIOSFODNN7EXAMPLE",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "5d41402abc4b2a76b9719d911017c592",
    "dGhpcyBpcyBhIHNlY3JldCBrZXkgZm9yIHRlc3Rpbmc9",
    "mot de passe : Xk9#mP2$vL8@nQ4!rT6&",
    "password: hunter2hunter2",
    "tok" + "en=" + "123456789012345678901234567890ab",
    "key sk-12345678901234567890123456",
    "12345678901234567890123456789012",
    "1234567890123456789012345678901234567890",
    "141963579e003574",
    "CH93 0076 2011 6238 5295 7",
    "CH9300762011623852957",
    "4242424242424242",
    "4242 4242 4242 4242",
    "4242-4242-4242-4242",
    "5555-5555-5555-4444",
    "378282246310005",
    "4000056655665556.",
    "4242424242424242.0",
    "-4111111111111111.25",
    "5.4242424242424242e-3",
    "-4242424242424242.0",
    "4242424242424242e0",
    "4242424242424242.00",
    "4.242424242424242e15",
    "-4.242424242424242E+15",
    "5555555555554444.0",
    "4000056655665556.5",
    "12345678901234567890123456789012.5",
]


JOURNAL_ONLY = ["756.1234.5678.97", "+41 79 123 45 67", "0041 79 123 45 67", "079 123 45 67"]


@pytest.mark.parametrize("text", JOURNAL_ONLY)
def test_pii_forms_masked_in_journal_layer(text):
    """AVS et téléphones : masqués par la couche JOURNAUX (`scrub_pii`) seulement ; leur vue par le modèle est un autre lot."""
    assert text not in scrub_pii(f"valeur {text} fin")


def _everywhere(text: str) -> str:
    """Le texte après chaque couche : repli de secrets, masque de cartes, invariants du noyau, journal (PII)."""
    wire = redact(text, wire=True).text
    inv = DeterministicInvariants().redact_result(ToolResult(call_id="c", name="t", content=text), None).content
    return "\x00".join([wire, mask_tool_text(wire), mask_card_text(text), inv, scrub_pii(text)])


@pytest.mark.parametrize("text", INTACT)
def test_intact(text):
    assert redact(text, wire=True).text == text
    assert mask_tool_text(text) == text
    assert mask_card_text(text) == text
    assert scrub_pii(text) == text
    res = ToolResult(call_id="c", name="add", content=text)
    assert DeterministicInvariants().redact_result(res, None).content == text


@pytest.mark.parametrize("text", MASKED)
def test_still_masked(text):
    """Chaque forme est masquée par AU MOINS une couche (le fil, les invariants du noyau ou le journal)."""
    layers = [redact(f"valeur {text} fin", wire=True).text,
              mask_tool_text(f"valeur {text} fin"),
              DeterministicInvariants().redact_result(
                  ToolResult(call_id="c", name="t", content=f"valeur {text} fin"), None).content,
              scrub_pii(f"valeur {text} fin")]
    probe = text.split(" : ")[-1].split(": ")[-1].rstrip(".")
    assert any(probe not in layer for layer in layers), layers


def test_one_point_only_number_spans():
    # Un jeton à plusieurs points (AVS, IP) ou collé à un préfixe n'est pas un décimal.
    assert number_spans("756.1234.5678.97") == []
    assert number_spans("192.168.1.10") == []
    assert number_spans("sk-123456789012345678901234") == []
    assert number_spans("tok" + "en=" + "123456789012345678901234567890ab") == []
    assert number_spans("a1.5") == []
    assert number_spans("12345") == []  # un entier nu n'est pas relâché
    assert [(1, 6)] == [(a, b) for a, b in number_spans(" 3.141") if (a, b) == (1, 6)]


def test_sentence_period_and_hex_lookalike():
    assert mask_card_text("carte : 4000056655665556.") == "carte : ••••••••••••••••."
    assert "141963579e003574" not in redact("la valeur est 141963579e003574 ok", wire=True).text


def test_loop_guard_signature_unaffected_by_numbers():
    call = ToolCall(id="c1", name="add", arguments={"a": 0.1, "b": 0.2})
    r1 = ToolResult(call_id="c1", name="add", content='{"result": 0.30000000000000004}')
    r2 = ToolResult(call_id="c2", name="add", content='{"result": 0.30000000000000004}')
    red = DeterministicInvariants().redact_result(r1, None)
    assert stall_signature(call, r1) == stall_signature(call, r2) == stall_signature(call, red)


# ── les chemins vers le modèle : redact(weak=True), redact(weak=False, wire=True), redact_result, redact_context ──────
def _paths(text: str) -> dict[str, str]:
    inv = DeterministicInvariants()
    msgs = inv.redact_context([{"role": "tool", "tool_call_id": "c", "content": text}])
    return {
        "weak": redact(text).text,
        "wire": redact(text, weak=False, wire=True).text,
        "result": inv.redact_result(ToolResult(call_id="c", name="t", content=text), None).content,
        "context": msgs[0]["content"],
    }


@pytest.mark.parametrize("digits", ["12345678901234567890123456789012", "1234567890123456789012345678901234567890"])
def test_long_digit_runs_masked_on_every_path(digits):
    text = f"valeur {digits} fin"
    for path, out in _paths(text).items():
        assert digits not in out, path
        # Étiquette `num` : une référence décimale n'est pas de l'hexadécimal (texte vu du modèle).
        assert "[secret:num-" in out and "[secret:hex-" not in out, path


@pytest.mark.parametrize("text", ["12345.6789012345678", "1e15", "0.30000000000000004", "3.141592653589793",
                                  "0.12345678901234567890123456789012", "id 123e4567-e89b-12d3-a456-426614174000"])
def test_numbers_and_uuid_intact_on_every_path(text):
    for path, out in _paths(f"valeur {text} fin").items():
        assert out == f"valeur {text} fin", path


def test_weak_false_paths_leave_hex_and_base64_as_before():
    """Constat (PAS une exigence) : sans le repli faible, un sha256 en hexadécimal, un base64 ou un jeton aléatoire sans
    mot-clé passent tels quels vers le modèle ; seul `redact(weak=True)` (texte humain, résultat SECRET) les masque."""
    for text in ["9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
                 "dGhpcyBpcyBhIHNlY3JldCBrZXkgZm9yIHRlc3Rpbmc9AAAA"]:
        paths = _paths(f"valeur {text} fin")
        assert text in paths["wire"] and text in paths["result"] and text in paths["context"]
        assert text not in paths["weak"]


def test_num_handle_shape_stable_and_idempotent():
    """32 chiffres purs : `[secret:num-<6 lettres>]`, même valeur = même poignée sur les 4 chemins, jamais re-caviardée."""
    digits = "12345678901234567890123456789012"
    outs = {p: o for p, o in _paths(f"v {digits} f").items()}
    assert len(set(outs.values())) == 1, outs
    assert re.fullmatch(r"v \[secret:num-[a-p]{6}\] f", outs["weak"]), outs
    for out in outs.values():
        assert redact(out).text == out and redact(out, weak=False, wire=True).text == out


def test_hex_with_letters_keeps_hex_label():
    text = "valeur 9f86d081884c7d659a2feaa0c55ad015 fin"  # 32 hexadécimaux avec lettres
    assert re.search(r"\[secret:hex-[a-p]{6}\]", redact(text).text)
    assert "[secret:num-" not in redact(text).text


def test_card_16_digits_keeps_card_mask_not_num():
    assert "••••" in mask_card_text("carte 4242424242424242 fin")
    assert "[secret:num-" not in redact("carte 4242424242424242 fin").text


def test_stall_signature_same_digits_same_signature():
    digits = "1234567890123456789012345678901234567890"
    call = ToolCall(id="c1", name="add", arguments={"x": 1})
    r1 = ToolResult(call_id="c1", name="add", content=f'{{"ref": "{digits}"}}')
    r2 = ToolResult(call_id="c2", name="add", content=f'{{"ref": "{digits}"}}')
    inv = DeterministicInvariants()
    assert stall_signature(call, inv.redact_result(r1, None)) == stall_signature(call, inv.redact_result(r2, None))
