"""Vérification stricte d'un ID token OIDC (nokker/sso/idtoken.py) : signature RS256/ES256 seulement, `alg: none` et
HS256 refusés, iss, aud, azp, exp/iat/nbf avec une petite tolérance, nonce. Clés générées dans le test."""
from __future__ import annotations

import time

import pytest

from nokker.sso.idtoken import TokenInvalid, verify_id_token
from tests.unit.sso import fake_idp as f

NOW = 1_800_000_000


def jwks(*extra):
    keys = [f.rsa_jwk(f.keys()["rsa"], "r1"), f.ec_jwk(f.keys()["ec"], "e1"), *extra]
    return lambda kid: keys


def verify(token, nonce="n-1", keys=None, **kw):
    return verify_id_token(token, keys=keys or jwks(), issuer=f.ISSUER, client_id=f.CLIENT_ID, nonce=nonce,
                           now=kw.pop("now", NOW), **kw)


def code(token, **kw):
    with pytest.raises(TokenInvalid) as e:
        verify(token, **kw)
    return e.value.code


def test_rs256_and_es256_pass():
    assert verify(f.sign(f.claims(now=NOW)))["sub"] == "u-1001"
    assert verify(f.sign(f.claims(now=NOW), alg="ES256", kid="e1"))["email"] == "alice@example.com"


def test_alg_none_refused_even_without_signature():
    assert code(f.sign(f.claims(now=NOW), alg="none", kid=None)) == "bad_alg"


def test_hs256_refused_no_key_confusion():
    # Un HS256 signé avec la clé publique (confusion d'algorithme) ne passe jamais : seuls RS256 et ES256.
    assert code(f.sign(f.claims(now=NOW), alg="HS256")) == "bad_alg"


def test_alg_must_match_key_type():
    # En-tête ES256 qui désigne la clé RSA : refusé, pas de bascule silencieuse.
    tok = f.sign(f.claims(now=NOW), alg="ES256", kid="r1")
    assert code(tok) == "bad_signature"


def test_wrong_signature_refused():
    other = f.keys()["rsa2"]
    assert code(f.sign(f.claims(now=NOW), key=other)) == "bad_signature"
    tok = f.sign(f.claims(now=NOW))
    head, _body, sig = tok.split(".")
    tampered = f.sign(f.claims(now=NOW, sub="u-evil")).split(".")[1]
    assert code(f"{head}.{tampered}.{sig}") == "bad_signature"


def test_unknown_kid_refused():
    assert code(f.sign(f.claims(now=NOW), kid="nope")) == "unknown_key"


def test_crit_header_refused():
    assert code(f.sign(f.claims(now=NOW), header={"crit": ["exp"]})) == "bad_header"


def test_embedded_jwk_header_is_never_used():
    # Une clé glissée dans l'en-tête (jwk) n'est pas une clé de confiance : on vérifie contre le JWKS seulement.
    attacker = f.keys()["rsa2"]
    tok = f.sign(f.claims(now=NOW), key=attacker, kid="r1", header={"jwk": f.rsa_jwk(attacker, "r1")})
    assert code(tok) == "bad_signature"


def test_wrong_issuer_refused():
    assert code(f.sign(f.claims(now=NOW, iss="https://evil.example.com"))) == "bad_iss"
    assert code(f.sign(f.claims(now=NOW, iss=f.ISSUER + "/"))) == "bad_iss"  # comparaison exacte


def test_wrong_audience_refused():
    assert code(f.sign(f.claims(now=NOW, aud="someone-else"))) == "bad_aud"
    assert code(f.sign(f.claims(now=NOW, aud=["a", "b"]))) == "bad_aud"


def test_multiple_audiences_require_azp():
    assert code(f.sign(f.claims(now=NOW, aud=[f.CLIENT_ID, "other"]))) == "bad_azp"
    assert code(f.sign(f.claims(now=NOW, aud=[f.CLIENT_ID, "other"], azp="other"))) == "bad_azp"
    assert verify(f.sign(f.claims(now=NOW, aud=[f.CLIENT_ID, "other"], azp=f.CLIENT_ID)))
    assert code(f.sign(f.claims(now=NOW, azp="other"))) == "bad_azp"  # azp présent : toujours nous


def test_expiry_with_small_leeway():
    assert verify(f.sign(f.claims(now=NOW - 330)))  # exp = NOW - 30 : dans la tolérance de 60 s
    assert code(f.sign(f.claims(now=NOW - 400))) == "expired"  # exp = NOW - 100
    assert code(f.sign(f.claims(now=NOW, exp=...))) == "expired"  # exp absent


def test_iat_in_future_or_too_old_refused():
    assert code(f.sign(f.claims(now=NOW + 120, exp=NOW + 400))) == "bad_iat"
    assert code(f.sign(f.claims(now=NOW - 3600, exp=NOW + 300))) == "bad_iat"
    assert code(f.sign(f.claims(now=NOW, iat=...))) == "bad_iat"


def test_nbf_respected():
    assert code(f.sign(f.claims(now=NOW, nbf=NOW + 120))) == "not_yet_valid"
    assert verify(f.sign(f.claims(now=NOW, nbf=NOW + 30)))


def test_nonce_must_match():
    assert code(f.sign(f.claims("other-nonce", now=NOW))) == "bad_nonce"
    assert code(f.sign(f.claims(now=NOW, nonce=...))) == "bad_nonce"


def test_sub_required():
    assert code(f.sign(f.claims(now=NOW, sub=""))) == "bad_sub"


def test_garbage_refused():
    for tok in ("", "a.b", "a.b.c.d", "!!!.???.***", "e30.e30.e30"):
        assert code(tok) in {"malformed", "bad_alg"}


def test_short_rsa_key_refused():
    from cryptography.hazmat.primitives.asymmetric import rsa

    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    tok = f.sign(f.claims(now=NOW), key=weak, kid="w1")
    assert code(tok, keys=jwks(f.rsa_jwk(weak, "w1"))) == "weak_key"


def test_real_clock_default_works():
    assert verify(f.sign(f.claims()), now=time.time())
