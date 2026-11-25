"""Numéro AVS/AHV dans ce que voit le modèle (`redact_result`) : masqué / intact, côte à côte."""
from __future__ import annotations

import json

import pytest

from nokker.core.ports import ToolSpec
from nokker.core.turn import Effect, ToolCall, ToolResult
from nokker.core.loop import stall_signature
from nokker.qa.invariants import DeterministicInvariants
from nokker.qa.secrets import HANDLE_RE, KnownSecrets
from nokker.redact.avs import mask_avs
from nokker.redact.rules import avs_ean13

AVS = "756.1234.5678.97"  # exemple officiel : somme EAN-13 valide
BAD = "756.1234.5678.98"  # même motif, somme fausse
IBAN_CH = "CH93 0076 2011 6238 5295 7"


def _inv() -> DeterministicInvariants:
    return DeterministicInvariants(known=KnownSecrets())


def _out(content: str, effect: Effect = Effect.READ, data=None) -> ToolResult:
    spec = ToolSpec(name="t", description="d", parameters={}, effect=effect)
    return _inv().redact_result(ToolResult(call_id="c", name="t", content=content, data=data), spec)


def test_reference_checksums():
    assert avs_ean13(AVS) and not avs_ean13(BAD)
    assert not avs_ean13("1234567890123") and not avs_ean13("1700000000000")


@pytest.mark.parametrize("text", [
    AVS, "7561234567897", "756 1234 5678 97", "756-1234-5678-97", "756.1234 5678.97",
    "Son AVS est 756.1234.5678.97 depuis 2020",
    "(756.1234.5678.97)", "AVS: 756.1234.5678.97, merci", "numéro 756.1234.5678.97.", "ok 7561234567897;",
    "n° 7561234567897.",
    BAD,  # motif à points complet : masqué même si la somme est fausse (faute de frappe = donnée personnelle)
])
def test_masked(text):
    out = _out(text)
    assert not any(c in out.content for c in ("756.1234", "7561234567897", "756 1234", "756-1234", "5678.9"))
    assert HANDLE_RE.search(out.content) and out.secret_handles


def test_masked_in_json_and_table():
    js = json.dumps({"assure": {"avs": AVS, "nom": "X"}, "liste": [AVS, "7561234567897"]})
    out = _out(js)
    assert "756" not in out.content.replace("[secret:", "") and json.loads(out.content)["assure"]["nom"] == "X"
    table = f"| nom | avs |\n|---|---|\n| Dupont | {AVS} |\n| Martin | 756 1234 5678 97 |\n"
    out = _out(table)
    assert "1234" not in out.content and out.content.count("[secret:") == 2 and "| Dupont |" in out.content


def test_data_block_masked_even_if_text_clean():
    out = _out("ok", data={"avs": AVS, "n": 3, "l": [AVS]})
    assert out.data == {"avs": out.data["avs"], "n": 3, "l": [out.data["l"][0]]}
    assert HANDLE_RE.fullmatch(out.data["avs"]) and AVS not in json.dumps(out.data)


def test_same_marker_for_same_value_and_all_forms():
    a, b = mask_avs(AVS)[0], mask_avs("7561234567897")[0]
    assert a == b and HANDLE_RE.fullmatch(a)


def test_secret_effect_untouched_path_still_seals():
    out = _out(f"avs {AVS}", Effect.SECRET)
    assert AVS not in out.content


@pytest.mark.parametrize("text", [
    "1234567890123",  # 13 chiffres, somme invalide, pas de 756
    "1700000000000",  # horodatage en millisecondes
    "7561234567890",  # préfixe 756 mais somme fausse, sans points
    "756 1234 5678 98", "756-1234-5678-98",  # idem avec espaces / tirets
    "1.7561234567897", "7561234567897.5", "7561234567897,5", "7.561234567897e12", "x7561234567897",
    "756.1234.5678.97.5", "7561234567897123",  # collé à d'autres chiffres
    "3.141592653589793", "0.30000000000000004", "1e-7", "123456.789012345",
    "550e8400-e29b-41d4-a716-446655440000",
    "+41 79 123 45 67", "079 123 45 67", "+41791234567", "Appelle le 079 123 45 67 demain",
])
def test_intact(text):
    out = _out(text)
    assert out.content == text and not out.secret_handles


def test_iban_behaviour_unchanged():
    out = _out(f"IBAN {IBAN_CH} et CH9300762011623852957")
    assert "0076" not in out.content and "CH9300762011623852957" not in out.content
    assert "avs" not in out.content  # traité par sa propre règle (poignée iban-…)
    assert "[secret:iban-" in out.content


def test_stall_signature_stable_with_masked_avs_and_unchanged_otherwise():
    call = ToolCall(id="1", name="t", arguments={})
    r1, r2 = _out(f"a {AVS}"), _out("a 756 1234 5678 97")
    assert stall_signature(call, r1) == stall_signature(call, r2)
    plain = ToolResult(call_id="c", name="t", content="rien 12:00:01 ici")
    assert stall_signature(call, plain) == stall_signature(call, _out("rien 12:00:01 ici"))
