"""Côté nokker.ai du lien Lokkal (nokker/cloud/link.py) : flux d'appareil, jeton rendu une fois, empreintes seulement,
noms de passerelle uniques, révocation, jetons ES256 vérifiés contre le JWKS (signature, expiration, machine)."""
from __future__ import annotations

import pytest

from nokker.cloud.link import LinkService, Signer, TokenError, slug, verify_token


def test_device_flow_gives_the_token_once_and_stores_only_hashes():
    clock = [1000.0]
    svc = LinkService(clock=lambda: clock[0])
    d = svc.device("MacBook de Pierre")
    assert len(d["user_code"]) == 9 and d["verification_uri"].endswith("/link")
    assert svc.token(d["device_code"]) == {"state": "pending"}
    out = svc.approve(d["user_code"].lower(), account="us_pierre", tenant="tn_pierre")
    assert out["host"] == "macbook-de-pierre.nokker.app"
    t = svc.token(d["device_code"])
    assert t["state"] == "approved" and t["machine_token"] == t["api_key"] and t["inference_base"].endswith("/v1/inference")
    assert svc.token(d["device_code"]) == {"state": "expired"}  # jeton rendu une seule fois
    m = svc.machine_for_token(t["machine_token"])
    assert m.tenant == "tn_pierre" and t["machine_token"] not in repr(svc.store.machines)  # empreinte seulement
    assert svc.revoke(t["machine_token"]) and svc.machine_for_token(t["machine_token"]) is None


def test_codes_expire_and_gateway_names_are_unique():
    clock = [0.0]
    svc = LinkService(clock=lambda: clock[0], ttl_s=60)
    a, b = svc.device("atelier"), svc.device("atelier")
    assert svc.approve(a["user_code"], "us_1", "tn_1")["host"] == "atelier.nokker.app"
    assert svc.approve(b["user_code"], "us_2", "tn_2")["host"] == "atelier-2.nokker.app"
    c = svc.device("x")
    clock[0] = 61
    with pytest.raises(LookupError):
        svc.approve(c["user_code"], "us_1", "tn_1")
    assert svc.token(c["device_code"]) == {"state": "expired"}
    assert slug("Éric's  Laptop!!") == "eric-s-laptop" and slug("") == "machine"


def test_es256_tokens_verify_against_the_jwks():
    signer = Signer.generate()
    jwks = signer.jwks()
    acc = signer.access_token("us_p", "tn_p", now=1000)
    assert verify_token(acc, jwks, now=1100)["tenant"] == "tn_p"
    with pytest.raises(TokenError, match="expiré"):
        verify_token(acc, jwks, now=1000 + 901)
    dele = signer.delegation_token("us_p", "tn_p", machine="mc_1", web_session="ws_1", now=1000)
    assert verify_token(dele, jwks, machine="mc_1", now=1010)["scope"] == "lokkal:session"
    with pytest.raises(TokenError, match="autre machine"):
        verify_token(dele, jwks, machine="mc_2", now=1010)
    forged = acc[:-4] + ("AAAA" if not acc.endswith("AAAA") else "BBBB")
    with pytest.raises(TokenError):
        verify_token(forged, jwks, now=1100)
    with pytest.raises(TokenError, match="clé inconnue"):
        verify_token(acc, Signer.generate(kid="autre").jwks(), now=1100)
