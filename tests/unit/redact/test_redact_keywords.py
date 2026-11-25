"""Valeur longue derrière un mot-clé de secret : masquée ; empreintes publiques et faux amis : intacts.

Règle (docs/INVARIANTS.md, « Valeur derrière un mot-clé de secret ») : sur TOUS les chemins, y compris ceux vers le
modèle (`weak=False`), une valeur de 24 caractères ou plus, d'un seul tenant, est masquée quand elle suit un mot-clé de
secret (suffixe de segment). Le mot-clé décide, pas la forme. Valeurs tirées d'une graine fixe, inventées."""
from __future__ import annotations

import random
import string
import uuid

import pytest

from nokker.core.loop import stall_signature
from nokker.core.turn import ToolCall, ToolResult
from nokker.qa.invariants import DeterministicInvariants
from nokker.qa.secrets import redact

_RNG = random.Random(20261118)


def _rand(alphabet: str, n: int) -> str:
    return "".join(_RNG.choice(alphabet) for _ in range(n))


_ALNUM = string.ascii_letters + string.digits
HEX32 = "a3f9" + _rand("0123456789abcdef", 28)
HEX40 = "b7e1" + _rand("0123456789abcdef", 36)
HEX64 = "c5d2" + _rand("0123456789abcdef", 60)
B64_40 = "Zk3" + _rand(_ALNUM, 36) + "+"
ALNUM24 = "Qx7" + _rand(_ALNUM, 21)
ALNUM32 = "Rm4" + _rand(_ALNUM, 29)
ALNUM40 = "Tn8" + _rand(_ALNUM, 37)
UUID = str(uuid.UUID(int=_RNG.getrandbits(128), version=4))
JWT_LIKE = "eyJ" + _rand(_ALNUM, 20) + "." + _rand(_ALNUM, 30) + "." + _rand(_ALNUM + "_-", 30)

# (texte, valeur qui doit disparaître)
MASKED = [
    (f"token: {B64_40}", B64_40),
    (f"token={HEX32}", HEX32),
    (f'{{"token": "{HEX64}"}}', HEX64),
    (f'{{"api_key": "{ALNUM40}"}}', ALNUM40),
    (f'{{"apiKey": "{ALNUM40}"}}', ALNUM40),
    (f"https://exemple.org/cb?token={HEX32}", HEX32),
    (f"https://exemple.org/cb?a=1&api_key={ALNUM32}", ALNUM32),
    (f"Authorization: Bearer {JWT_LIKE}", JWT_LIKE),
    (f"Authorization: Basic {B64_40}", B64_40),
    (f"client_secret = {HEX40}", HEX40),
    (f"PASSWORD={ALNUM24}", ALNUM24),
    (f"x-api-key: {HEX32}", HEX32),
    (f"access_token: {ALNUM32}", ALNUM32),
    (f"refreshToken: {ALNUM32}", ALNUM32),
    (f"ApiKey: {ALNUM32}", ALNUM32),
    (f"session_id: {UUID}", UUID),
    (f'"sessionId": "{UUID}"', UUID),
    (f"token: {UUID}", UUID),
    (f"cookie: sid={HEX32}", HEX32),
    (f"Cookie: {HEX40}", HEX40),
    (f"Set-Cookie: theme=dark; sid={ALNUM32}; Path=/; HttpOnly", ALNUM32),
    (f"secret {HEX40}", HEX40),
    (f"private_key: '{ALNUM32}'", ALNUM32),
    (f'access-key = "{ALNUM32}"', ALNUM32),
    (f"auth: {ALNUM32}", ALNUM32),
    (f"passwd: {ALNUM24}.", ALNUM24),
    (f'{{\\"token\\": \\"{HEX32}\\"}}', HEX32),
]

# texte intact sur tous les chemins
INTACT = [
    f"sha256: {HEX64}",
    f'{{"sha256": "{HEX64}"}}',
    f"commit {HEX40}",
    f'{{"commit": "{HEX40}"}}',
    f"id: {UUID}",
    f'{{"id": "{UUID}"}}',
    f"digest: sha256:{HEX64}",
    f"hash: {HEX32}",
    "max_tokens: 4096",
    "token_count: 12345",
    '{"tokenizer": "cl100k_base"}',
    f"author: {HEX32}",
    f"authority: {HEX32}",
    f"secretary: {HEX32}",
    "passwordless: true",
    "cookies_enabled: true",
    "auth: /etc/nokker/auth/config.yaml",
    "auth: https://exemple.org/chemin/long/vers/la/ressource",
    "auth: config_auth_provider_settings.yaml",
    "token: generate_secure_random_token_value",
    "token: getAuthenticationTokenForCurrentUser",
    "secret: xxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "password: Speicherplatzverwaltungsgesetz",
    "token = request.headers.get(name)",
    "pg_0123456789abcdef",
    "th_0123456789abcdef0123456789abcdef",
    "session_id: ep_0123456789abcdef01234567",
    "ep_89abcdef01234567 et rs_0123456789abcdef",
    '<untrusted-data id="8abf0123456789ab">x</untrusted-data>',
    f"token_count: 12345 sha256: {HEX64}",
]

# formes que les replis FAIBLES existants (`weak=True`, texte humain) masquent déjà sans cette règle (constaté sur la base)
WEAK_NOISY = {f'{{"commit": "{HEX40}"}}', "password: Speicherplatzverwaltungsgesetz",
              "th_0123456789abcdef0123456789abcdef", "session_id: ep_0123456789abcdef01234567"}


def _paths(text: str) -> dict[str, str]:
    inv = DeterministicInvariants()
    msgs = inv.redact_context([{"role": "tool", "tool_call_id": "c", "content": text}])
    return {
        "weak": redact(text).text,
        "wire": redact(text, weak=False, wire=True).text,
        "result": inv.redact_result(ToolResult(call_id="c", name="t", content=text), None).content,
        "context": msgs[0]["content"],
    }


@pytest.mark.parametrize(("text", "value"), MASKED)
def test_masked_on_every_path(text, value):
    for path, out in _paths(f"valeur {text} fin").items():
        assert value not in out, (path, out)
        assert "[secret:" in out, (path, out)


@pytest.mark.parametrize(("text", "value"), MASKED)
def test_masked_non_wire_weak_false(text, value):
    assert value not in redact(f"valeur {text} fin", weak=False).text


@pytest.mark.parametrize("text", INTACT)
def test_intact_on_model_paths(text):
    for path, out in _paths(f"valeur {text} fin").items():
        if path == "weak" and (any(h in text for h in (HEX32, HEX64)) or text in WEAK_NOISY):
            continue  # texte humain (`weak=True`) : ses replis existants (hexa, entropie, mot de passe), hors sujet ici
        assert out == f"valeur {text} fin", (path, out)


@pytest.mark.parametrize("text", INTACT)
def test_intact_on_wire_and_result(text):
    paths = _paths(f"valeur {text} fin")
    assert paths["wire"] == paths["result"] == paths["context"] == f"valeur {text} fin"


def test_keyword_and_separator_stay_readable():
    out = redact(f'{{"api_key": "{ALNUM40}", "n": 1}}', weak=False, wire=True).text
    assert out.startswith('{"api_key": "[secret:key-value-') and out.endswith('", "n": 1}')
    assert redact(f"Authorization: Basic {B64_40}", weak=False, wire=True).text.startswith("Authorization: Basic [secret:")
    assert redact(f"Cookie: theme=dark; sid={ALNUM32}; Path=/", weak=False, wire=True).text.startswith(
        "Cookie: theme=dark; sid=[secret:")


def test_idempotent():
    once = redact(f"token: {B64_40}", weak=False, wire=True).text
    assert redact(once, weak=False, wire=True).text == once


def test_same_value_same_handle():
    a = redact(f"token: {HEX32}", weak=False, wire=True).text
    b = redact(f'"api_key": "{HEX32}"', weak=False, wire=True).text
    assert a.split(": ")[1] == b.split('": "')[1].rstrip('"')


def test_handles_of_23_plus_chars_are_not_rearmed():
    text = "secret:aws-session-token-abcdef and token: [secret:aws-session-token-abcdef]"
    assert redact(text, weak=False, wire=True).text == text


def test_stall_signature_unchanged_except_masked_values():
    call = ToolCall(id="c1", name="t", arguments={"q": "x"})
    plain = ToolResult(call_id="c1", name="t", content=f"sha256: {HEX64} max_tokens: 4096")
    inv = DeterministicInvariants()
    assert stall_signature(call, inv.redact_result(plain, None)) == stall_signature(call, plain)
    r1 = ToolResult(call_id="c1", name="t", content=f"token: {HEX32}")
    r2 = ToolResult(call_id="c2", name="t", content=f"token: {HEX32}")
    assert stall_signature(call, inv.redact_result(r1, None)) == stall_signature(call, inv.redact_result(r2, None))
    assert stall_signature(call, inv.redact_result(r1, None)) != stall_signature(call, r1)


@pytest.mark.parametrize("pad", ["=", "=="])
@pytest.mark.parametrize("text", ["token: {b}", "api_key={b}"])
def test_base64_padding_inside_handle(text, pad):
    """Le `=`/`==` de remplissage base64 est dans la valeur masquée, pas laissé après la poignée."""
    body = "dGhpcyBpcyBhIHNlY3JldCBrZXkgZm9yIHRlc3Rpbg" if pad == "==" else "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo"
    for out in (redact(text.format(b=body + pad)).text, redact(text.format(b=body + pad), weak=False).text):
        assert body not in out and "=" not in out.replace("api_key=", "").replace("token=", ""), out
        assert out.rstrip('"').endswith("]"), out
        assert redact(out).text == out
