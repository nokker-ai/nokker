"""Clé de la passerelle illisible (`NOKKER_GATEWAY_SIGNING_KEY` mal collée) : le noyau démarre quand même, journalise
une ERROR claire SANS la clé, et reste « cloud_link inerte » comme sans clé ; `/healthz` dit `gateway: invalid_key`.

Revue du train du 2.11, §0 : « une clé illisible fait lever `Signer` au démarrage, le pod ne démarre plus ». Les clés
sont générées à la volée par le test (aucune clé dans le dépôt)."""
from __future__ import annotations

import json
import logging
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

from nokker.api.server import configure_cloud_link, make_handler
from nokker.cloud import http as cloud_http
from nokker.cloud.link import Signer, SigningKeyError
from nokker.config import NokkerConfig, TierConfig
from tests.unit.api.test_server import make_kernel

ENV = "NOKKER_GATEWAY_SIGNING_KEY"
MESSAGE = "clé de passerelle illisible : lien Lokkal éteint"


def _pem(key, fmt=serialization.PrivateFormat.PKCS8, enc=None) -> str:
    return key.private_bytes(serialization.Encoding.PEM, fmt, enc or serialization.NoEncryption()).decode()


GOOD = _pem(ec.generate_private_key(ec.SECP256R1()))
SEC1 = _pem(ec.generate_private_key(ec.SECP256R1()), serialization.PrivateFormat.TraditionalOpenSSL)
BODY = GOOD.splitlines()[2]  # une ligne du corps de la clé : ne doit apparaître dans aucun journal

BAD = {
    "tronquée": GOOD[: len(GOOD) // 2],
    "sans fin": GOOD.split("-----END")[0],
    "corps abîmé": GOOD.replace(BODY, BODY[:20] + "!!" + BODY[22:]),
    "guillemets": f'"{GOOD}"',
    "guillemets simples": f"'{GOOD}'",
    "retours échappés": GOOD.strip().replace("\n", "\\n"),
    "rsa": _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048)),
    "ed25519": _pem(ed25519.Ed25519PrivateKey.generate()),
    "p-384": _pem(ec.generate_private_key(ec.SECP384R1())),
    "clé publique": ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
    "chiffrée": _pem(ec.generate_private_key(ec.SECP256R1()),
                     enc=serialization.BestAvailableEncryption(b"mot-de-passe-de-test")),
    "n'importe quoi": "colle-ici-la-cle",
}


def _config() -> NokkerConfig:
    return NokkerConfig(tiers={"deep": TierConfig(base_url="http://127.0.0.1:9/v1", model="m")},
                        cloud_link={"base_url": "https://app.nokker.ai", "handoffs": False})


class _Tenancy:
    def account(self, account_id):
        return None


def _healthz() -> dict:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), {}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/healthz", timeout=5) as r:
            assert r.status == 200
            return json.loads(r.read())
    finally:
        httpd.shutdown()


@pytest.fixture(autouse=True)
def _reset():
    yield
    cloud_http.configure(None)


@pytest.mark.parametrize("label", list(BAD))
def test_unreadable_key_leaves_cloud_link_inert_with_a_clear_error_and_no_key_in_logs(label, monkeypatch, caplog):
    monkeypatch.setenv(ENV, BAD[label])
    caplog.set_level(logging.DEBUG)
    configure_cloud_link(_config(), _Tenancy(), None, None)  # ne lève pas : le pod démarre
    assert cloud_http._cloud is None  # aucune route /v1/link|gateway|inference|mirror|handoffs, aucun outil de plus
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and MESSAGE in errors[0].getMessage(), [r.getMessage() for r in caplog.records]
    fragments = [x for x in BAD[label].replace("\\n", "\n").splitlines() if len(x) >= 16 and "-----" not in x]
    for r in caplog.records:
        text = r.getMessage() + (logging.Formatter().formatException(r.exc_info) if r.exc_info else "")
        assert not any(f.strip("\"'") in text for f in fragments + [BODY]), (label, text)
    assert _healthz()["gateway"] == "invalid_key"


@pytest.mark.parametrize("pem", [GOOD, SEC1, GOOD.strip(), "\n" + GOOD + "\n\n"])
def test_a_good_p256_key_turns_the_link_on(pem, monkeypatch):
    monkeypatch.setenv(ENV, pem)
    configure_cloud_link(_config(), _Tenancy(), None, None)
    assert cloud_http._cloud is not None
    assert _healthz()["gateway"] == "on"


def test_no_key_and_no_cloud_link_are_reported_too(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    configure_cloud_link(_config(), _Tenancy(), None, None)
    assert cloud_http._cloud is None and _healthz()["gateway"] == "no_key"
    configure_cloud_link(NokkerConfig(tiers=_config().tiers), _Tenancy(), None, None)
    assert _healthz()["gateway"] == "off"


@pytest.mark.parametrize("label", ["rsa", "ed25519", "p-384", "clé publique", "tronquée", "guillemets"])
def test_signer_refuses_anything_but_an_es256_private_key(label):
    with pytest.raises(SigningKeyError) as e:
        Signer(private_pem=BAD[label].encode())
    assert BODY not in str(e.value) and e.value.__cause__ is None
    assert e.value.__context__ is None or e.value.__suppress_context__  # rien de `cryptography` ne remonte
