"""`PATCH /v1/account {lang}` : langue du compte réglée par la personne (migration 089 : NULL = pas posée). Vrai serveur,
vrai noyau, faux modèle qui garde ce que le tour a reçu : la langue est relue à chaque tour, sans cache."""
# ruff: noqa: F811
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from nokker.core import model_texts as mt
from nokker.api.server import account_lang_of, make_handler
from tests.unit.tenancy.test_isolation_suite import beta  # noqa: F401  (fixture)

# Marque de la langue du tour dans le contexte : le libellé du bloc date de l'en-tête (`context.date_label`), rendu dans la
# langue RÉSOLUE du tour (le moteur V1 de la bêta n'écrit pas la ligne `context.reply_language`, seul le V0 le fait).
EN_LINE, FR_LINE, DE_LINE = (mt.pick(mt.DATE_LABEL, c) for c in ("en", "fr", "de"))
FR_TEXT = "Bonjour, peux-tu me résumer ma journée de demain et préparer la liste des courses pour la semaine ?"


def _cur(seen):
    """Dernier message de la personne, avec SON en-tête (les tours d'avant gardent le leur dans l'historique)."""
    return seen[-1].text.rsplit("<user>", 1)[-1]


@pytest.fixture(autouse=True)
def _no_env_lang(monkeypatch):
    monkeypatch.delenv("NOKKER_LANG", raising=False)




def test_patch_lang_returns_the_account_in_get_shape(beta):
    call = beta.call
    assert call("PATCH", "/v1/account", {"lang": "en"})[0] == 401
    st, body = call("PATCH", "/v1/account", {"lang": "en"}, who="A")
    assert st == 200
    assert body == call("GET", "/v1/account", who="A")[1]  # même forme, même contenu
    assert body["lang"] == "en" and body["email"] == "a@example.org"
    assert beta.tenancy.account(beta.a.account_id).lang == "en"
    assert beta.tenancy.account(beta.b.account_id).lang is None  # le compte d'un autre n'est jamais touché
    assert call("GET", "/v1/account", who="B")[1]["email"] == "b@example.org"
    for code in ("fr", "de", "it"):
        assert call("PATCH", "/v1/account", {"lang": code}, who="A")[1]["lang"] == code
        assert beta.tenancy.account(beta.a.account_id).lang == code


@pytest.mark.parametrize("bad", ["xx", "", "EN", " en", "english", "fr-CH", 3, True, ["en"], {"a": 1}])
def test_patch_lang_refuses_what_is_not_a_listed_language(beta, bad):
    st, body = beta.call("PATCH", "/v1/account", {"lang": bad}, who="A")
    assert st == 400 and body["error"]["code"] == "bad_lang" and body["error"]["valid"] == ["fr", "en", "de", "it"]
    assert beta.tenancy.account(beta.a.account_id).lang is None


def test_patch_lang_bad_bodies_are_400_invalid_and_write_nothing(beta):
    for bad in ({}, {"lang": "en", "email": "x@example.org"}, {"email": "x@example.org"}, [], "en"):
        st, body = beta.call("PATCH", "/v1/account", bad, who="A")
        assert st == 400 and body["error"] == "invalid", (bad, st, body)
    req = urllib.request.Request(f"http://127.0.0.1:{beta.port}/v1/account", method="PATCH", data=b"pas du json",
                                 headers={"Authorization": f"Bearer {beta.tokens['A']}", "Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 400
    assert beta.tenancy.account(beta.a.account_id).lang is None


def test_patch_lang_same_value_is_a_200_without_a_write(beta, monkeypatch):
    beta.call("PATCH", "/v1/account", {"lang": "de"}, who="A")
    writes = []
    orig = beta.tenancy.set_lang
    monkeypatch.setattr(beta.tenancy, "set_lang", lambda *a: writes.append(a) or orig(*a))
    assert beta.call("PATCH", "/v1/account", {"lang": "de"}, who="A")[1]["lang"] == "de"
    beta.call("PATCH", "/v1/account", {"lang": None}, who="A")
    beta.call("PATCH", "/v1/account", {"lang": None}, who="A")
    assert writes == [(beta.a.account_id, None)]  # la remise à zéro n'écrit qu'une fois


def test_patch_lang_null_resets_to_no_account_language(beta):
    beta.call("PATCH", "/v1/account", {"lang": "en"}, who="A")
    st, body = beta.call("PATCH", "/v1/account", {"lang": None}, who="A")
    assert st == 200 and body["lang"] == "fr"  # GET montre « fr » tant que rien n'est posé (`lang or "fr"`)
    assert beta.tenancy.account(beta.a.account_id).lang is None
    assert account_lang_of(beta.tenancy)(beta.a.account_id) is None


def test_first_name_patch_is_unchanged(beta):
    assert beta.call("PATCH", "/v1/account", {"first_name": "Alice"}, who="A") == (200, {"first_name": "Alice", "name": "Alice"})


def test_not_a_cloud_account_is_refused_like_get(beta):
    """Sans compte `us_…` (identité interne, ou Lokkal sans magasin de comptes) : même refus que GET /v1/account."""
    st_get, _ = beta.call("GET", "/v1/account")
    st_patch, _ = beta.call("PATCH", "/v1/account", {"lang": "en"})
    assert st_get == st_patch == 401


def test_lokkal_without_tenancy_has_no_account_route(beta):
    handler = make_handler(beta.kernel, {}, None, approvals=beta.approvals, tenancy=None, open_api=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        out = {}
        for method, body in (("GET", None), ("PATCH", json.dumps({"lang": "en"}).encode())):
            req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}/v1/account", method=method, data=body,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    out[method] = r.status
            except urllib.error.HTTPError as e:
                out[method] = e.code
        assert out["PATCH"] == out["GET"] and out["PATCH"] >= 400, out  # même convention que GET
    finally:
        httpd.shutdown()


def test_next_turn_follows_the_account_language_without_stale_cache(beta):
    beta.kernel.account_lang = account_lang_of(beta.tenancy)  # câblé ainsi par `serve` (server.py)
    beta.kernel.limits = None  # le plafond de tours du jour (3 dans la fixture) couperait le test
    # compte en anglais, message ambigu : le tour est en anglais
    assert beta.call("PATCH", "/v1/account", {"lang": "en"}, who="A")[0] == 200
    for n, text in enumerate(("ok", "12:30", "Zoé Dupont"), 1):
        st, _body, seen = beta.chat(text, "A", session=f"amb{n}")
        assert st == 200 and EN_LINE in _cur(seen), (text, _cur(seen)[-300:])
    # PATCH fr : le tour SUIVANT, dans le même fil, est en français
    st, _b, seen = beta.chat("08:15", "A", session="same")
    assert EN_LINE in _cur(seen)
    assert beta.call("PATCH", "/v1/account", {"lang": "fr"}, who="A")[0] == 200
    st, _b, seen = beta.chat("09:45", "A", session="same")
    assert FR_LINE in _cur(seen) and EN_LINE not in _cur(seen)
    # un choix explicite l'emporte même sur un message net dans une autre langue
    assert beta.call("PATCH", "/v1/account", {"lang": "de"}, who="A")[0] == 200
    st, _b, seen = beta.chat(FR_TEXT, "A", session="same")
    assert DE_LINE in _cur(seen)
    # null : la détection du message prime (français net), et un message ambigu ne reçoit plus la ligne du compte
    assert beta.call("PATCH", "/v1/account", {"lang": None}, who="A")[0] == 200
    st, _b, seen = beta.chat(FR_TEXT, "A", session="same")
    assert FR_LINE in _cur(seen) and DE_LINE not in _cur(seen)
    # B n'a rien vu de tout cela
    st, _b, seen = beta.chat("ok", "B", session="b1")
    assert EN_LINE not in _cur(seen) and DE_LINE not in _cur(seen)


def test_lang_is_default_follows_the_column(beta):
    """`lang_is_default` : vrai tant qu'aucune langue n'est posée (colonne NULL) ; `lang` reste la langue RÉSOLUE (« fr » faute de
    mieux), jamais null."""
    call = beta.call
    st, body = call("GET", "/v1/account", who="A")
    assert st == 200 and body["lang"] == "fr" and body["lang_is_default"] is True
    st, body = call("PATCH", "/v1/account", {"lang": "en"}, who="A")
    assert (st, body["lang"], body["lang_is_default"]) == (200, "en", False)
    assert call("GET", "/v1/account", who="A")[1]["lang_is_default"] is False
    assert call("GET", "/v1/account", who="B")[1]["lang_is_default"] is True  # B intact
    st, body = call("PATCH", "/v1/account", {"lang": "fr"}, who="A")  # fr posé : un choix, pas le défaut
    assert (st, body["lang"], body["lang_is_default"]) == (200, "fr", False)
    st, body = call("PATCH", "/v1/account", {"lang": None}, who="A")
    assert (st, body["lang"], body["lang_is_default"]) == (200, "fr", True)
    assert call("GET", "/v1/account", who="A")[1]["lang_is_default"] is True
