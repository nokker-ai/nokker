"""Client du fournisseur (nokker/sso/idp.py) : découverte en cache et vérifiée, JWKS en cache avec rotation, échange
du code (PKCE), garde réseau (https seulement, pas d'adresse privée). Faux fournisseur en mémoire, aucun réseau."""
from __future__ import annotations

import json
import socket

import pytest

from nokker.sso.idp import Idp, IdpError, SafeHttp
from tests.unit.sso import fake_idp as f


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def test_discovery_is_cached_and_issuer_checked():
    idp, clock = f.FakeIdp(), Clock()
    client = Idp(http=idp, clock=clock)
    d = client.discovery(f.ISSUER)
    assert d["token_endpoint"] == f.ISSUER + "/token"
    client.discovery(f.ISSUER)
    assert idp.gets.count(f.ISSUER + "/.well-known/openid-configuration") == 1
    clock.t += 3601
    client.discovery(f.ISSUER)
    assert idp.gets.count(f.ISSUER + "/.well-known/openid-configuration") == 2


def test_discovery_with_another_issuer_refused():
    idp = f.FakeIdp()
    idp.discovery_over = {"issuer": "https://evil.example.com"}
    with pytest.raises(IdpError) as e:
        Idp(http=idp).discovery(f.ISSUER)
    assert e.value.code == "bad_discovery"


def test_discovery_endpoints_must_be_https():
    idp = f.FakeIdp()
    idp.discovery_over = {"token_endpoint": "http://idp.example.com/token"}
    with pytest.raises(IdpError) as e:
        Idp(http=idp).discovery(f.ISSUER)
    assert e.value.code == "bad_discovery"


def test_jwks_cached_then_reloaded_once_on_unknown_kid():
    idp, clock = f.FakeIdp(), Clock()
    client = Idp(http=idp, clock=clock)
    d = client.discovery(f.ISSUER)
    assert [k["kid"] for k in client.keys(d, "r1")] == ["r1", "e1"]
    client.keys(d, "e1")
    assert idp.gets.count(f.ISSUER + "/jwks") == 1
    # Rotation : le fournisseur publie une nouvelle clé ; un kid inconnu recharge le JWKS.
    idp.jwks_keys = [f.rsa_jwk(f.keys()["rsa2"], "r2")]
    clock.t += 60
    assert [k["kid"] for k in client.keys(d, "r2")] == ["r2"]
    assert idp.gets.count(f.ISSUER + "/jwks") == 2
    # Un kid inconnu en rafale ne martèle pas le fournisseur : au plus un rechargement toutes les 30 s.
    client.keys(d, "zz")
    client.keys(d, "zz")
    assert idp.gets.count(f.ISSUER + "/jwks") == 2


def test_exchange_sends_pkce_and_basic_auth_and_returns_id_token():
    idp = f.FakeIdp()
    client = Idp(http=idp)
    d = client.discovery(f.ISSUER)
    import base64
    import hashlib

    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    idp.codes["c1"] = {"nonce": "n-1", "challenge": challenge, "redirect_uri": "https://app.example.com/sso/callback"}
    out = client.exchange(d, code="c1", verifier=verifier, redirect_uri="https://app.example.com/sso/callback",
                          client_id=f.CLIENT_ID, client_secret=f.CLIENT_SECRET, token_auth="client_secret_basic")
    assert out["id_token"].count(".") == 2
    post = idp.token_posts[-1]
    assert post["data"]["code_verifier"] == verifier and "client_secret" not in post["data"]


def test_exchange_error_does_not_leak_secret():
    idp = f.FakeIdp()
    client = Idp(http=idp)
    d = client.discovery(f.ISSUER)
    with pytest.raises(IdpError) as e:
        client.exchange(d, code="unknown", verifier="v" * 64, redirect_uri="https://app.example.com/cb",
                        client_id=f.CLIENT_ID, client_secret=f.CLIENT_SECRET, token_auth="client_secret_post")
    assert e.value.code == "token_endpoint" and f.CLIENT_SECRET not in str(e.value)


def _resolver(ip):
    def resolve(host, port, proto=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    return resolve


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "100.100.1.1", "192.168.1.2"])
def test_safe_http_refuses_private_addresses(ip):
    with pytest.raises(IdpError) as e:
        SafeHttp(resolver=_resolver(ip)).get("https://idp.example.com/.well-known/openid-configuration")
    assert e.value.code == "unreachable"


def test_safe_http_refuses_plain_http():
    with pytest.raises(IdpError) as e:
        SafeHttp(resolver=_resolver("93.184.216.34")).get("http://idp.example.com/x")
    assert e.value.code == "unreachable"


def test_bad_json_from_idp_is_an_idp_error():
    class Broken(f.FakeIdp):
        def get(self, url):
            return 200, b"not json"

    with pytest.raises(IdpError):
        Idp(http=Broken()).discovery(f.ISSUER)
    assert json.dumps({"ok": 1})
