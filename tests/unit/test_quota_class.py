"""Quota d'inférence atteint (403/429 dont le CORPS dit `quota_exceeded`) : classe `quota`, phrase de personne qui ne parle
ni de panne ni de réessai, `turn.done.error.code = quota_exceeded` + `retry_at`, et AUCUN repli (palier, escalade, rejeu
du fournisseur) : un quota atteint n'est pas une panne.

Avant : le 403 devenait la classe « denied » et la personne lisait « Le modèle de Nokker ne répond pas (accès refusé).
Réessaie dans un moment. »."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from nokker.core.loop import person_failure_class, quota_reached, quota_text, quota_when
from nokker.core.ports import RouteDecision
from nokker.core.quota_error import quota_body, quota_in_chain, quota_of, tag_error
from nokker.core.turn import Message, Tier, TurnInput
from nokker.providers.sokkan import _UpstreamStatus
from tests.unit.test_fixes_e2e_contracts import _hub, _turn
from tests.unit.test_loop_contract import kernel

URL = "https://nokker.example.test/v1"
RETRY = "2026-11-19T12:00:00Z"
BODY_RETRY = json.dumps({"error": {"code": "quota_exceeded", "message": "Limite du jour", "retry_at": RETRY}})
BODY_BARE = json.dumps({"error": {"code": "quota_exceeded", "message": "Limite du jour"}})


class _Failing:
    model = "m"
    vision = False
    vision_probe = None
    max_context_tokens = 0

    def __init__(self, exc):
        self.exc, self.calls = exc, 0

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048, **kw):
        self.calls += 1
        raise self.exc


# ───────────────────────── détection (défensive) ─────────────────────────


@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize("body", [BODY_RETRY, BODY_BARE])
def test_status_and_quota_body_give_the_quota_class(status, body):
    for exc in (RuntimeError(f"HTTP {status} de {URL}: {body}"), _UpstreamStatus(status, body)):
        assert person_failure_class(exc) == "quota"
        assert quota_reached(exc) is not None


def test_the_class_follows_the_cause_chain():
    try:
        raise RuntimeError("enrobage") from RuntimeError(f"HTTP 403 de {URL}: {BODY_RETRY}")
    except RuntimeError as e:
        assert person_failure_class(e) == "quota" and quota_in_chain(e) == {"retry_at": RETRY}


@pytest.mark.parametrize("exc,cls", [
    (RuntimeError(f"HTTP 403 de {URL}: forbidden"), "denied"),
    (RuntimeError(f'HTTP 403 de {URL}: {{"error":{{"code":"forbidden"}}}}'), "denied"),
    (RuntimeError(f'HTTP 403 de {URL}: {{"error":"quota_exceeded"}}'), "denied"),  # `error` en chaîne
    (RuntimeError(f'HTTP 403 de {URL}: {{"error":{{"message":"quota"}}}}'), "denied"),  # `code` absent
    (RuntimeError(f'HTTP 403 de {URL}: {{"error":{{"code":"quota_exceeded"'), "denied"),  # JSON coupé
    (RuntimeError(f"HTTP 403 de {URL}: <html>quota_exceeded</html>"), "denied"),
    (RuntimeError(f'HTTP 429 de {URL}: {{"error":{{"code":"rate"}}}}'), "limit"),
    (RuntimeError(f"HTTP 429 de {URL}: rate limit exceeded"), "limit"),
    (RuntimeError(f"HTTP 401 de {URL}: {BODY_RETRY}"), "key"),  # le statut compte : ni 401 ni 500 ne sont un quota
    (RuntimeError(f"HTTP 502 de {URL}: {BODY_RETRY}"), "service"),
    (RuntimeError(f"HTTP 403 de {URL}: [1, 2, 3]"), "denied"),
    (RuntimeError(f"HTTP 403 de {URL}: null"), "denied"),
])
def test_anything_else_keeps_the_status_classification(exc, cls):
    assert person_failure_class(exc) == cls and quota_reached(exc) is None


def test_the_body_reader_never_raises_and_reads_a_bounded_prefix():
    assert quota_body(None) is None and quota_body(b"x") is None and quota_body("") is None
    assert quota_body("x" * 10**6) is None
    # `quota_exceeded` loin au-delà du préfixe lu : ignoré (pas de lecture d'un corps énorme)
    assert quota_body(" " * 5000 + BODY_BARE) is None
    assert quota_of(_UpstreamStatus(403, BODY_BARE)) == {"retry_at": None}


@pytest.mark.parametrize("raw,want", [
    ("2026-11-19T12:00:00Z", "2026-11-19T12:00:00Z"),
    ("2026-11-19T13:00:00+01:00", "2026-11-19T12:00:00Z"),
    ("2026-11-19T12:00:00", "2026-11-19T12:00:00Z"),  # sans fuseau : lu comme UTC
    ("demain", None), ("", None), (12, None), (None, None), ("2026-13-45T99:00:00Z", None),
])
def test_retry_at_is_normalised_to_utc_or_dropped(raw, want):
    body = json.dumps({"error": {"code": "quota_exceeded", **({} if raw is None else {"retry_at": raw})}})
    assert quota_body(body) == {"retry_at": want}


# ───────────────────── attributs structurés (corps entier lu par le fournisseur) ─────────────────────


def _long(kind: str) -> str:
    if kind == "noise_before":  # 8 000 caractères de bruit AVANT le champ `error`
        return json.dumps({"noise": "x" * 8000, "error": {"code": "quota_exceeded", "retry_at": RETRY}})
    if kind == "huge_message":
        return json.dumps({"error": {"code": "quota_exceeded", "message": "m" * 20000, "retry_at": RETRY}})
    return json.dumps({"error": {"code": "quota_exceeded", "retry_at": RETRY}, "pad": "p" * 100_000})  # 100 Ko


def _http_error(status, body):
    return tag_error(RuntimeError(f"HTTP {status} de {URL}: {body[:500]}"), body)  # message tronqué à 500 comme le fournisseur


@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize("kind", ["noise_before", "huge_message"])
def test_long_body_is_detected_through_the_structured_attributes(status, kind):
    exc = _http_error(status, _long(kind))
    assert len(str(exc)) < 600 and exc.error_code == "quota_exceeded" and exc.retry_at == RETRY
    assert person_failure_class(exc) == "quota" and quota_in_chain(exc) == {"retry_at": RETRY}
    assert person_failure_class(_UpstreamStatus(status, _long(kind))) == "quota"


def test_body_over_64k_sets_no_attribute_and_falls_back_to_denied_without_raising():
    exc = _http_error(403, _long("100k"))
    assert not hasattr(exc, "error_code") and not hasattr(exc, "retry_at")
    assert person_failure_class(exc) == "denied" and quota_reached(exc) is None


def test_exception_without_attributes_still_uses_the_message_fallback():
    exc = RuntimeError(f"HTTP 403 de {URL}: {BODY_RETRY}")
    assert not hasattr(exc, "error_code") and quota_in_chain(exc) == {"retry_at": RETRY}


@pytest.mark.parametrize("body", ["", "pas du json", "[1]", '{"error": "quota_exceeded"}', '{"error": {"code": 7}}',
                                  '{"error": {"code": ""}}', '{"error": {"code": "a\\nb"}}',
                                  json.dumps({"error": {"code": "x" * 201}})])
def test_tag_error_ignores_anything_but_a_short_string_code(body):
    exc = tag_error(RuntimeError("x"), body)
    assert not hasattr(exc, "error_code")


def test_structured_code_other_than_quota_wins_over_the_message():
    exc = tag_error(RuntimeError(f"HTTP 403 de {URL}: {BODY_RETRY}"), '{"error": {"code": "forbidden"}}')
    assert exc.error_code == "forbidden" and person_failure_class(exc) == "denied"
    assert not hasattr(tag_error(RuntimeError("x"), BODY_RETRY.replace("quota_exceeded", "other")), "retry_at")


# ───────────────────────── textes de la personne ─────────────────────────

WITH = {
    "fr": "Quota d'utilisation atteint. Il se renouvelle le 19 novembre à 13:00 (heure suisse). Tu peux aussi passer à une formule supérieure depuis ton compte.",
    "en": "Usage quota reached. It renews on 19 November at 13:00 (Swiss time). You can also upgrade to a higher plan from your account.",
    "de": "Nutzungskontingent erreicht. Es erneuert sich am 19. November um 13:00 (Schweizer Zeit). Du kannst in deinem Konto auch zu einem höheren Tarif wechseln.",
    "it": "Quota di utilizzo raggiunta. Si rinnova il 19 novembre alle 13:00 (ora svizzera). Puoi anche passare a un piano superiore dal tuo account.",
}
WITHOUT = {
    "fr": "Quota d'utilisation atteint. Il se renouvelle bientôt. Tu peux aussi passer à une formule supérieure depuis ton compte.",
    "en": "Usage quota reached. It renews soon. You can also upgrade to a higher plan from your account.",
    "de": "Nutzungskontingent erreicht. Es erneuert sich bald. Du kannst in deinem Konto auch zu einem höheren Tarif wechseln.",
    "it": "Quota di utilizzo raggiunta. Si rinnova a breve. Puoi anche passare a un piano superiore dal tuo account.",
}
BANNED = {"fr": ("ne répond pas", "Réessaie", "inférence"), "en": ("not responding", "Try again", "Inference"),
          "de": ("antwortet nicht", "noch einmal", "Inferenz"), "it": ("non risponde", "Riprova", "inferenza")}


@pytest.mark.parametrize("lang", ["fr", "en", "de", "it"])
def test_quota_sentence_in_each_language_with_and_without_a_date(lang):
    assert quota_text(RETRY, lang, datetime(2026, 11, 18, tzinfo=UTC)) == WITH[lang]  # 12:00 UTC = 13:00 à Zurich en novembre
    assert quota_text(None, lang) == WITHOUT[lang] and quota_text("illisible", lang) == WITHOUT[lang]
    for text in (WITH[lang], WITHOUT[lang]):
        assert not any(w.lower() in text.lower() for w in BANNED[lang])
        assert "{" not in text and "http" not in text.lower()


def test_quota_when_is_swiss_local_time_in_summer_too():
    now = datetime(2026, 6, 1, tzinfo=UTC)
    assert quota_when("2026-07-01T12:00:00Z", "fr", now) == "le 1 juillet à 14:00 (heure suisse)"
    assert quota_when(None) is None and quota_when("x") is None


@pytest.mark.parametrize("lang,want", [
    ("fr", "le 3 janvier 2027 à 13:00 (heure suisse)"), ("en", "on 3 January 2027 at 13:00 (Swiss time)"),
    ("de", "am 3. Januar 2027 um 13:00 (Schweizer Zeit)"), ("it", "il 3 gennaio 2027 alle 13:00 (ora svizzera)"),
])
def test_quota_when_adds_the_year_when_the_deadline_is_in_another_year(lang, want):
    december = datetime(2026, 12, 30, 9, 0, tzinfo=UTC)
    assert quota_when("2027-01-03T12:00:00Z", lang, december) == want
    assert "2027" not in quota_when("2027-01-03T12:00:00Z", lang, datetime(2027, 1, 2, tzinfo=UTC))
    assert "2026" not in quota_when("2026-12-31T12:00:00Z", lang, december)  # même année : pas d'année
    assert "2027" in quota_text("2027-01-03T12:00:00Z", lang, december)


# ───────────────────────── le tour : ni repli, ni escalade, une seule fois ─────────────────────────


def _run(exc, tier=Tier.SHIP):
    k, _tools, ship, deep = kernel([Message("assistant", "ok")])
    bad = _Failing(exc)
    k.providers = {Tier.SHIP: ship, Tier.DEEP: bad if tier is Tier.DEEP else deep}
    if tier is Tier.SHIP:
        k.providers = {Tier.SHIP: bad, Tier.DEEP: deep}
    k._choose_route = lambda state: RouteDecision(tier=tier, reasoning=False, reason="test")
    turn = k.run_turn(TurnInput("t_quota", "salut"))
    return turn, bad, ship, deep


@pytest.mark.parametrize("status", [403, 429])
def test_quota_on_deep_does_not_fall_back_to_ship_nor_escalate(status):
    turn, bad, ship, _deep = _run(RuntimeError(f"HTTP {status} de {URL}: {BODY_RETRY}"), Tier.DEEP)
    assert bad.calls == 1 and len(ship.seen) == 0  # un seul appel, jamais de repli sur Ship
    assert turn.failure == {"code": "quota_exceeded", "message": quota_text(RETRY, "fr"), "person_message": quota_text(RETRY, "fr"),
                            "retry_at": RETRY}
    assert turn.reply == quota_text(RETRY, "fr") and turn.escalated is False
    rules = [v.rule for v in turn.verdicts]
    assert "router:quota" in rules and "router:fallback" not in rules and "router:unavailable" not in rules


def test_quota_on_ship_ends_the_turn_once_with_the_sentence_without_a_date():
    turn, bad, _ship, deep = _run(RuntimeError(f"HTTP 403 de {URL}: {BODY_BARE}"))
    assert bad.calls == 1 and len(deep.seen) == 0
    assert turn.failure == {"code": "quota_exceeded", "message": WITHOUT["fr"], "person_message": WITHOUT["fr"]}
    assert turn.reply == WITHOUT["fr"] and not turn.escalated
    assert "ne répond pas" not in turn.reply and "Réessaie" not in turn.reply


def test_the_control_a_real_outage_still_falls_back_and_escalates():
    turn, bad, ship, _ = _run(RuntimeError(f"HTTP 502 de {URL}: bad gateway"), Tier.DEEP)
    assert bad.calls == 1 and len(ship.seen) == 1  # repli de palier comme avant
    k, _t, _s, _d = kernel([])
    k.providers = {Tier.SHIP: _Failing(RuntimeError(f"HTTP 403 de {URL}: forbidden"))}
    t2 = k.run_turn(TurnInput("t_den", "salut"))
    assert t2.failure["code"] == "tier_unavailable" and "accès refusé" in t2.failure["person_message"]
    assert t2.escalated is True


def test_quota_phrase_follows_the_turn_language():
    k, _t, _s, _d = kernel([])
    k.providers = {Tier.SHIP: _Failing(RuntimeError(f"HTTP 403 de {URL}: {BODY_RETRY}"))}
    turn = k.run_turn(TurnInput("t_de", "hallo", lang="de"))
    assert turn.reply == quota_text(RETRY, "de")


def test_turn_done_carries_code_and_retry_at_only_when_known():
    hub, seen = _hub()
    msg = {"code": "quota_exceeded", "message": quota_text(RETRY, "fr"), "person_message": quota_text(RETRY, "fr")}
    hub.turn_done(_turn(failure={**msg, "retry_at": RETRY}))
    hub.turn_done(_turn(failure=dict(msg)))
    done = [d for n, d in seen if n == "turn.done"]
    assert done[0]["error"] == {"code": "quota_exceeded", "message": quota_text(RETRY, "fr"), "retry_at": RETRY}
    assert done[1]["error"] == {"code": "quota_exceeded", "message": quota_text(RETRY, "fr")}  # champ absent, jamais inventé


def test_host_extension_exposes_the_quota_error():
    from nokker.api.server import host_extension

    ext = host_extension(_turn(failure={"code": "quota_exceeded", "message": "m", "person_message": "m",
                                        "retry_at": RETRY}))
    assert ext["error"] == {"code": "quota_exceeded", "message": "m", "retry_at": RETRY}
    assert host_extension(_turn(failure={"code": "tier_unavailable", "message": "x"})) == {}
