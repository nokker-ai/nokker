"""G2 : un résultat d'outil NON étiqueté SECRET (et le filet du fil) caviarde les secrets à MOT-CLÉ (« mot de passe : X »,
`password=X`, `passwd`, `PIN : 483920`). Mesuré sur un corpus synthétique de retours d'outils (`tool_return_corpus.py`) :
0 secret à mot-clé qui passe, et moins de 2 % de sorties légitimes touchées. L'entropie et l'hexadécimal restent ÉTEINTS sur
ces résultats (mesuré : 9,8 % et 19,7 % de faux positifs, 26 % ensemble) : un secret sans mot-clé reste hors de portée."""
from __future__ import annotations

from nokker.core.ports import ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.qa.invariants import DeterministicInvariants
from nokker.qa.secrets import redact
from tests.unit.qa.tool_return_corpus import LEGIT, NO_KEYWORD, SECRETS

READ = ToolSpec(name="read_file", description="d", parameters={}, effect=Effect.READ)


def _result(text: str) -> ToolResult:
    return ToolResult(call_id="c1", name="read_file", content=text)


def test_keyword_secrets_in_a_tool_result_are_masked():
    inv = DeterministicInvariants()
    keyword = [s for s in SECRETS if s not in NO_KEYWORD]
    assert len(keyword) >= 60
    passed = [t for t, v in keyword if v in inv.redact_result(_result(t), READ).content]
    assert passed == []  # aucun secret à mot-clé ne passe
    # sans spec (outil inconnu) : même traitement
    assert all(v not in inv.redact_result(_result(t), None).content for t, v in keyword)


def test_keyword_secrets_in_the_wire_are_masked():
    inv = DeterministicInvariants()
    msgs = [{"role": "tool", "content": t} for t, v in SECRETS if (t, v) not in NO_KEYWORD]
    out = inv.redact_context(msgs)
    values = [v for t, v in SECRETS if (t, v) not in NO_KEYWORD]
    assert not [1 for m, v in zip(out, values, strict=True) if v in m["content"]]


def test_the_secret_tool_data_block_is_masked_too():
    inv = DeterministicInvariants()
    r = ToolResult(call_id="c", name="read_file", content='{"password": "Xk9!qpzT42Lm"}',
                   data={"user": "admin", "password": "Xk9!qpzT42Lm"})
    out = inv.redact_result(r, READ)
    assert "Xk9!qpzT42Lm" not in out.content and "Xk9!qpzT42Lm" not in str(out.data)


def test_false_positives_on_legit_tool_output_under_two_percent():
    inv = DeterministicInvariants()
    touched = [t for t in LEGIT if inv.redact_result(_result(t), READ).content != t]
    assert len(LEGIT) >= 300
    assert len(touched) / len(LEGIT) < 0.02, touched[:5]
    wire = inv.redact_context([{"role": "tool", "content": t} for t in LEGIT])
    assert sum(1 for m, t in zip(wire, LEGIT, strict=True) if m["content"] != t) / len(LEGIT) < 0.02


def test_code_and_config_with_password_field_names_are_left_alone():
    for text in ("password = os.environ['DB_PASSWORD']", "password: ${DB_PASSWORD}", "password: string (min 8)",
                 "def f(user, password):\n    return login(user=user, password=password)", "self.password = hash_password(raw)",
                 "Mot de passe oublié ? Cliquez ici.", "pin: false\npin_memory: true", "pw: /home/user/project",
                 "{\"password\": null}", "{\"has_password\": true}"):
        assert redact(text, None, weak=False, tool_keywords=True).text == text, text


def test_entropy_and_hex_stay_off_for_tool_results():
    """Choix mesuré (voir l'en-tête) : un hash git ou un sha256 dans une sortie d'outil n'est pas touché."""
    text = "3f9a1c7de24b86a05c1d7e9f2b4c6d8e0a1b3c5d  dist/app.tar.gz\n9fB2kQ7xLm3ZpR8tVw1YcN5d"
    assert redact(text, None, weak=False, tool_keywords=True).text == text


def test_pin_needs_the_keyword_and_four_digits():
    assert "483920" not in redact("PIN : 483920", None, weak=False, tool_keywords=True).text
    assert "pin" in redact("code pin = 1234", None, weak=False, tool_keywords=True).text.lower()
    assert "1234" not in redact("code pin = 1234", None, weak=False, tool_keywords=True).text
    for text in ("pin: 12", "the pin 483920 is not labelled", "pinned: 483920"):
        assert redact(text, None, weak=False, tool_keywords=True).text == text, text
