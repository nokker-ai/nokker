"""Libellé d'une panne d'amont pour la personne : classes fermées, libellés figés, jamais le texte de l'exception."""
from __future__ import annotations

import asyncio
import hashlib
import socket
import ssl

import httpcore
import httpx
import pytest

from nokker.byok.context import ByokRoute
from nokker.core.loop import (BYOK_ACTIONS, BYOK_DOWN_REPLY, byok_down_reply, person_failure_class, THREAD_MODEL_DOWN_REPLY, _FAILURE_LABELS, _short_error,
                              person_failure_label)
from nokker.core.turn import Tier, TurnInput
from nokker.providers.sokkan import _UpstreamStatus
from nokker.tenancy.context import tenant_scope
from tests.unit.test_local_thread_model import Prov, T, _world

URL = "https://api.exemple.test/v1"


def _wrapped(cause: BaseException, msg="échec réseau après 3 tentative(s) : ") -> RuntimeError:
    try:
        raise RuntimeError(msg + str(cause)) from cause
    except RuntimeError as e:
        return e


class _Status(Exception):
    def __init__(self, code):
        super().__init__("boom")
        self.status_code = code


class _Resp(Exception):
    def __init__(self, code):
        super().__init__("boom")
        self.response = type("R", (), {"status_code": code, "text": ""})()


CASES = [
    ("timeout", httpx.ReadTimeout("timed out")),
    ("timeout", httpx.ConnectTimeout("timed out")),
    ("timeout", TimeoutError("timed out")),
    ("timeout", RuntimeError(f"HTTP 408 de {URL}: request timeout")),
    ("timeout", _Status(408)),
    ("timeout", asyncio.TimeoutError()),
    ("timeout", _wrapped(httpx.ReadTimeout("timed out"))),
    ("key", RuntimeError(f"HTTP 401 de {URL}: {{\"error\":\"invalid api key\"}}")),
    ("key", _Status(401)),
    ("denied", RuntimeError(f"HTTP 403 de {URL}: forbidden")),
    ("denied", _Resp(403)),
    ("service", _UpstreamStatus(503, "<html>Service Unavailable</html>")),
    ("service", RuntimeError(f"HTTP 502 de {URL}: bad gateway")),
    ("service", RuntimeError(f"HTTP 504 de {URL} après 2 tentative(s) (budget total 180s épuisé)")),
    ("service", RuntimeError(f"HTTP 500 de {URL}: Internal Server Error")),
    ("limit", RuntimeError(f"HTTP 429 de {URL}: rate limit exceeded")),
    ("limit", _UpstreamStatus(429, "slow down")),
    ("quota", RuntimeError(f'HTTP 403 de {URL}: {{"error":{{"code":"quota_exceeded","message":"Limite du jour"}}}}')),
    ("quota", _UpstreamStatus(429, '{"error": {"code": "quota_exceeded", "retry_at": "2026-11-19T12:00:00Z"}}')),
    ("model", RuntimeError(f"HTTP 404 de {URL}: The model `gpt-x` does not exist")),
    ("model", _UpstreamStatus(404, '{"error":{"message":"model not found"}}')),
    ("unreachable", RuntimeError(f"HTTP 404 de {URL}: Not Found")),
    ("unreachable", RuntimeError("HTTP 404 de https://api.exemple.test/model-api/v1: Not Found")),
    ("unreachable", _UpstreamStatus(404, "page not found")),
    ("unreachable", httpx.ConnectError("[Errno 111] Connection refused")),
    ("unreachable", ConnectionRefusedError(111, "Connection refused")),
    ("unreachable", socket.gaierror(-2, "Name or service not known")),
    ("unreachable", ssl.SSLError("CERTIFICATE_VERIFY_FAILED")),
    ("unreachable", httpcore.ConnectError("connexion refusée : 10.0.0.1:443 n'est pas le point d'accès enregistré")),
    ("unreachable", OSError("Network is unreachable")),
    ("unreachable", _wrapped(httpx.ConnectError("[Errno 111] Connection refused"))),
    ("unreachable", RuntimeError("échec réseau après 3 tentatives")),
    ("other", RuntimeError(f"HTTP 400 de {URL}: bad request")),
    ("other", ValueError("quelque chose de bizarre")),
    ("other", RuntimeError("appel à m coupé (provider fermé)")),
    ("other", RuntimeError("code 503 dans le texte")),  # motif ancré : un nombre quelconque ne compte pas
    ("other", None),
]


@pytest.mark.parametrize("cls,exc", CASES, ids=[f"{c}-{type(e).__name__}-{i}" for i, (c, e) in enumerate(CASES)])
def test_class_of_each_failure(cls, exc):
    assert person_failure_label(exc) == _FAILURE_LABELS[cls]


def test_every_class_is_covered():
    assert {c for c, _ in CASES} == set(_FAILURE_LABELS)


@pytest.mark.parametrize("exc", [e for _, e in CASES if e is not None])
def test_the_reply_never_carries_the_exception_text(exc):
    why, why_en = person_failure_label(exc)
    for reply in (byok_down_reply("Mon modèle", exc),
                  THREAD_MODEL_DOWN_REPLY.format(name="Mon modèle", why=why, why_en=why_en)):
        for frag in ("HTTP", "Errno", "https://", "Error", "Exception", "Timeout", "httpx", "Connection refused",
                     str(exc)):
            if not frag or frag in {x for pair in _FAILURE_LABELS.values() for x in pair}:
                continue  # texte d'exception qui coïncide avec un libellé figé (« timed out »)
            assert frag not in reply, (frag, reply)


FROZEN_ACTIONS = {
    "key": ("4ce375851463286c", "054a186ba3cf83a1"), "denied": ("4ce375851463286c", "054a186ba3cf83a1"),
    "unreachable": ("4ce375851463286c", "054a186ba3cf83a1"), "other": ("4ce375851463286c", "054a186ba3cf83a1"),
    "timeout": ("f822e6e97779fbfe", "9ed952c0d617c1fc"), "service": ("f822e6e97779fbfe", "9ed952c0d617c1fc"),
    "limit": ("4cd4cd4bd6280e9d", "53f7e8cf0eb00852"), "quota": ("4cd4cd4bd6280e9d", "53f7e8cf0eb00852"), "model": ("b9165cac0a04b9cf", "c4d1fd7f41da8e80"),
}
# Réponse complète rendue (nom « Mon modèle »), FR + EN, par classe.
FROZEN_BYOK = {
    "timeout": "efd0777a7fd95903", "key": "2bb5b4815e0f1dec", "denied": "7d6101806037a2c4",
    "service": "091f742d92f0edf4", "limit": "0308b09889482a61", "model": "498acacf21dd947a",
    "unreachable": "eacd5aedf683effc", "other": "c04cd71c176aa377", "quota": "5dc636aff6691de5",
}
_EXC_OF = {c: e for c, e in reversed(CASES)}


def _byok_for(cls):
    return byok_down_reply("Mon modèle", _EXC_OF[cls])


def test_frozen_labels_and_templates():
    h = lambda t: hashlib.sha256(t.encode()).hexdigest()[:16]  # noqa: E731
    assert {k: (h(a), h(b)) for k, (a, b) in _FAILURE_LABELS.items()} == {
        "timeout": ("8c69efbb42a7c44b", "3dcd80f1b15f796e"),
        "key": ("021506c4273df653", "24405ee8d426be2d"),
        "denied": ("dc0d8c3000e6aa53", "a3b11db62f9b9a83"),
        "service": ("7190052e16bff424", "a44fe80197852e95"),
        "limit": ("4cf911db4399bce3", "85bbf21ee7bdda40"),
        "model": ("668c739a8bc3f9d7", "8c2854b8f6ffcaf0"),
        "unreachable": ("a05054e0e76ca4e8", "88129d98c2c86a25"),
        "other": ("5ec8eb2c6be184eb", "5e3176197f53ec3e"),
        "quota": ("2f7b5ada313200cd", "589f008c0045e53d"),
    }
    assert h(THREAD_MODEL_DOWN_REPLY) == "56e44987511eacaa" and h(BYOK_DOWN_REPLY) == "710396ac2e4216ce"
    assert {k: (h(a), h(b)) for k, (a, b) in BYOK_ACTIONS.items()} == FROZEN_ACTIONS
    assert {k: h(_byok_for(k)) for k in _FAILURE_LABELS} == FROZEN_BYOK


def test_byok_reply_is_the_full_sentence_for_each_class():
    A = ("Vérifie le point d'accès et la clé dans Mon compte → Modèles, ou repasse sur le modèle de Nokker.",
         "Check the endpoint and key in My account → Models, or switch back to Nokker's model.")
    R = ("Réessaie dans un moment, ou repasse sur le modèle de Nokker.",
         "Try again in a moment, or switch back to Nokker's model.")
    want = {
        "key": A, "denied": A, "unreachable": A, "other": A, "timeout": R, "service": R,
        "limit": ("Réessaie plus tard, vérifie ta limite d'utilisation chez ton fournisseur, ou repasse sur le modèle de Nokker.",
                  "Try again later, check your usage limit with your provider, or switch back to Nokker's model."),
        "model": ("Vérifie le nom du modèle dans Mon compte → Modèles, ou repasse sur le modèle de Nokker.",
                  "Check the model name in My account → Models, or switch back to Nokker's model."),
    }
    want["quota"] = want["limit"]
    assert set(want) == set(_FAILURE_LABELS)
    for cls, (fr, en) in want.items():
        why, why_en = _FAILURE_LABELS[cls]
        if cls == "quota":  # quota du fournisseur de la personne : motif BYOK propre
            why, why_en = "quota atteint chez ton fournisseur", "quota reached at your provider"
        assert _byok_for(cls) == (f"Ton modèle « Mon modèle » ne répond pas ({why}). {fr}\n\n"
                                  f"Your model \"Mon modèle\" is not responding ({why_en}). {en}")
    assert _byok_for("other") == BYOK_DOWN_REPLY.format(name="Mon modèle", why="erreur inattendue",
                                                         why_en="unexpected error")


def test_http_408_is_a_timeout():
    assert person_failure_class(_Status(408)) == "timeout"
    assert person_failure_class(RuntimeError(f"HTTP 408 de {URL}: x")) == "timeout"


def test_short_error_is_unchanged_for_the_logs():
    assert _short_error(RuntimeError("HTTP 401 de https://x.test: no")) == "HTTP 401 de https://x.test: no"
    assert _short_error(None) == "erreur inconnue"


class _Down(Prov):
    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        raise RuntimeError(f"HTTP 401 de {URL}: invalid api key")


def _run(kernel):
    with tenant_scope(T):
        return kernel.run_turn(TurnInput(thread_id="th_x", user_text="salut", channel="api", sender="us_a"))


def test_thread_model_turn_renders_the_label_but_keeps_the_raw_failure_message():
    kernel, _ = _world(ByokRoute(name="Mon modèle", provider=_Down("x"), from_thread=True))
    t = _run(kernel)
    assert t.messages[-1].content == THREAD_MODEL_DOWN_REPLY.format(name="Mon modèle", why="clé refusée",
                                                                    why_en="key rejected")
    assert "HTTP" not in t.messages[-1].content
    assert t.failure["message"] == f"modèle « Mon modèle » injoignable : HTTP 401 de {URL}: invalid api key"


def test_cloud_byok_turn_renders_the_label_but_keeps_the_raw_failure_message():
    class Svc:
        def route_for_turn(self):
            return ByokRoute(name="Mon modèle", provider=_Down("x"))

    kernel, _ = _world(None)
    kernel.byok = Svc()
    t = _run(kernel)
    assert t.messages[-1].content == BYOK_DOWN_REPLY.format(name="Mon modèle", why="clé refusée", why_en="key rejected")
    assert t.failure["message"].endswith(f"HTTP 401 de {URL}: invalid api key")


# ---- C7d : ce que la personne lit d'une panne de palier hors BYOK (escalade, erreur de tour, maison) ----
import re  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from nokker.core.loop import (PERSON_ERROR_DOWN, PERSON_ERROR_NEUTRAL, PERSON_REASON_DOWN,  # noqa: E402
                              failure_person_text)
from nokker.house.room import HouseRoom  # noqa: E402
from nokker.qa.escalate import EscalatorV0, LocalEscalator  # noqa: E402

RAW = "https://router.example.test/v1/chat"
REALISTIC = [
    ("service", RuntimeError(f"HTTP 502 de {RAW}: bad gateway ship deep self")),
    ("unreachable", OSError(111, f"[Errno 111] Connection refused {RAW}")),
    ("timeout", httpx.ReadTimeout(f"timed out ship {RAW}")),
    ("key", RuntimeError(f"HTTP 401 de {RAW}: invalid key")),
]
_WORDS = re.compile(r"https?://|\bship\b|\bdeep\b|\bself\b|HTTP \d|Errno|Traceback|Exception|Error\b", re.I)


class _Raising(Prov):
    def __init__(self, exc):
        super().__init__("router")
        self.exc = exc

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        raise self.exc


def _down_turn(exc, escalator=None):
    kernel, _ = _world(None)
    p = _Raising(exc)
    kernel.providers = {Tier.SHIP: p, Tier.DEEP: p, Tier.SMALL: p}
    if escalator is not None:
        kernel.escalator = escalator
    return _run(kernel)


def _person_views(t):
    room = HouseRoom.__new__(HouseRoom)
    room.kernel = SimpleNamespace()
    return [t.messages[-1].content, failure_person_text(t.failure), room._outcome(t)[0]]


@pytest.mark.parametrize("exc", [e for c, e in CASES if isinstance(e, BaseException) and c != "quota"]
                         + [e for _, e in REALISTIC])  # le quota n'est pas une panne de palier : test_quota_class.py
@pytest.mark.parametrize("esc", [None, LocalEscalator()])
def test_tier_down_person_sees_only_the_label(exc, esc):
    t = _down_turn(exc, esc)
    why = person_failure_label(exc)[0]
    views = _person_views(t)
    assert t.failure["code"] == "tier_unavailable"
    assert PERSON_ERROR_DOWN.format(why=why) == views[1]
    assert PERSON_REASON_DOWN.format(why=why) in views[2]  # la maison rend la réponse d'escalade
    assert PERSON_REASON_DOWN.format(why=why) in views[0]
    for v in views:
        assert not _WORDS.search(v), v
        assert not str(exc) or str(exc) not in v
    # la cause brute reste côté interne
    assert _short_error(exc) in t.failure["message"] and t.failure["message"].startswith("palier ")


def test_escalator_phrases_unchanged():
    r = PERSON_REASON_DOWN.format(why="service indisponible")
    assert EscalatorV0().escalate(SimpleNamespace(thread_id="t", index=1), r) == \
        "Je transmets à l'équipe : le modèle de Nokker ne répond pas (service indisponible)."
    assert LocalEscalator().escalate(SimpleNamespace(thread_id="t", index=1), r) == \
        "J'ai besoin de toi pour continuer : le modèle de Nokker ne répond pas (service indisponible)."


def test_byok_failure_person_message_is_label_only():
    kernel, _ = _world(ByokRoute(name="Mon modèle", provider=_Down("x"), from_thread=True))
    t = _run(kernel)
    assert failure_person_text(t.failure) == "Le modèle de Nokker ne répond pas (clé refusée)."
    assert "HTTP" in t.failure["message"]


@pytest.mark.parametrize("failure", [
    {"code": "internal_error", "message": f"KeyError: 'x' dans {RAW}"},
    {"code": "max_calls", "message": "limite d'appels ship"},
    {"code": "cancelled", "message": "annulé"},
    {"code": "tool_error", "message": "HTTP 500 outil"},
])
def test_non_model_failure_is_neutral(failure):
    assert failure_person_text(failure) == PERSON_ERROR_NEUTRAL == "Une erreur est survenue."


def test_new_constants_frozen():
    h = lambda t: hashlib.sha256(t.encode()).hexdigest()[:16]  # noqa: E731
    assert (h(PERSON_REASON_DOWN), h(PERSON_ERROR_DOWN), h(PERSON_ERROR_NEUTRAL)) == ("795e6d580c441c16", "26c7afc9e2fec288", "e7d85b151f651bc8")


def test_house_renders_person_text_when_no_reply():
    room = HouseRoom.__new__(HouseRoom)
    room.kernel = SimpleNamespace()
    t = SimpleNamespace(reply="", approval_ids=[], failure={"code": "tier_unavailable", "message": f"palier ship : {RAW}",
                                                          "person_message": "Le modèle de Nokker ne répond pas (x)."})
    assert room._outcome(t)[0] == "Le modèle de Nokker ne répond pas (x)."
    t.failure = {"code": "internal_error", "message": f"boom {RAW}"}
    assert room._outcome(t)[0] == PERSON_ERROR_NEUTRAL


BYOK_QUOTA = {
    "fr": "Ton modèle « Mon modèle » ne répond pas (quota atteint chez ton fournisseur). Réessaie plus tard, vérifie ta limite d'utilisation chez ton fournisseur, ou repasse sur le modèle de Nokker.\n\nYour model \"Mon modèle\" is not responding (quota reached at your provider). Try again later, check your usage limit with your provider, or switch back to Nokker's model.",
    "en": "Your model \"Mon modèle\" is not responding (quota reached at your provider). Try again later, check your usage limit with your provider, or switch back to Nokker's model.",
    "de": "Dein Modell «Mon modèle» antwortet nicht (Kontingent bei deinem Anbieter erreicht). Versuche es später noch einmal, prüfe das Nutzungslimit bei deinem Anbieter oder wechsle zurück zum Modell von Nokker.",
    "it": "Il tuo modello «Mon modèle» non risponde (quota raggiunta presso il tuo fornitore). Riprova più tardi, controlla il limite di utilizzo presso il tuo fornitore, oppure torna al modello di Nokker.",
}


@pytest.mark.parametrize("lang", ["fr", "en", "de", "it"])
def test_byok_quota_sentence_names_the_providers_quota_not_nokkers(lang):
    exc = _EXC_OF["quota"]
    reply = byok_down_reply("Mon modèle", exc, lang)
    assert reply == BYOK_QUOTA[lang]
    assert "inférence" not in reply and "d'utilisation atteint" not in reply
    for word in ("Nutzungskontingent", "Usage quota", "Quota di utilizzo", "quota di utilizzo"):
        assert word not in reply
