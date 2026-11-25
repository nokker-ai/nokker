"""Repli de palier Deep -> Ship : événement hôte `route.fallback`, `turn.done.fallback`, `turn.done.notice` (ligne pour la
personne, jamais dans la réponse ni l'historique). Aucun réseau."""
from __future__ import annotations

import pytest

from nokker.byok.context import ByokRoute
from nokker.core import person_texts as ptx
from nokker.core.ports import RouteDecision
from nokker.core.turn import Message, Tier, TurnInput
from tests.unit.test_fixes_e2e_contracts import _hub
from tests.unit.test_loop_contract import kernel

LANGS = ("fr", "en", "de", "it")
FR = "Réponse en mode allégé : le mode approfondi est indisponible pour le moment."
TEXTS = {
    "fr": FR,
    "en": "Answered in lightweight mode: in-depth mode is unavailable right now.",
    "de": "Antwort im Kompaktmodus: Der ausführliche Modus ist gerade nicht verfügbar.",
    "it": "Risposta in modalità ridotta: la modalità approfondita non è disponibile al momento.",
}


class _Down:
    model = "m"
    vision = False
    vision_probe = None
    max_context_tokens = 0

    def __init__(self, msg="HTTP 502 de https://amont.example.test/v1: bad gateway"):
        self.msg, self.calls = msg, 0

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048, **kw):
        self.calls += 1
        raise RuntimeError(self.msg)


def _world(tier=Tier.DEEP, deep=None, ship_down=None, script=None, observe=True):
    k, _t, ship, d = kernel(script if script is not None else [Message("assistant", "voilà")])
    deep = deep if deep is not None else _Down()
    k.providers = {Tier.SHIP: ship_down or ship, Tier.DEEP: deep if tier is Tier.DEEP else d}
    k._choose_route = lambda state: RouteDecision(tier=tier, reasoning=False, reason="test")
    hub, seen = _hub()
    if observe:
        k.observer = hub
    return k, hub, seen, deep, ship


def _events(seen, name):
    return [d for n, d in seen if n == name]


def _done(seen):
    return _events(seen, "turn.done")[-1]


def test_deep_fails_ship_answers_event_once_and_done_fields():
    k, _h, seen, deep, ship = _world()
    turn = k.run_turn(TurnInput("t_rf", "salut"))
    assert deep.calls == 1 and len(ship.seen) == 1 and turn.reply == "voilà"
    ev = _events(seen, "route.fallback")
    assert len(ev) == 1 and ev[0]["from"] == "deep" and ev[0]["to"] == "ship"
    assert ev[0]["reason"] == "service unavailable (HTTP 502)"  # classe fermée + statut : ni adresse, ni corps de l'amont
    done = _done(seen)
    assert done["fallback"] == {"from": "deep", "to": "ship"} and done["notice"] == FR
    # le verdict du ledger est inchangé
    v = [x for x in turn.verdicts if x.rule == "router:fallback"]
    assert len(v) == 1 and v[0].reason.startswith("palier deep indisponible (") and v[0].reason.endswith(") → ship")


@pytest.mark.parametrize("lang", LANGS)
def test_notice_in_each_language_and_never_in_reply_or_history(lang):
    k, _h, seen, *_ = _world()
    turn = k.run_turn(TurnInput("t_rf_" + lang, "salut", lang=lang))
    notice = _done(seen)["notice"]
    assert notice == TEXTS[lang] == ptx.pick(ptx.DEEP_FALLBACK_NOTICE, lang)
    assert turn.reply == "voilà"
    assert all(notice not in (m.content or "") for m in turn.messages)


def test_the_notice_text_is_never_sent_to_the_model():
    k, _h, _seen, _deep, ship = _world()
    k.run_turn(TurnInput("t_rf_m", "salut"))
    assert all(TEXTS[lang] not in str(ship.seen) for lang in LANGS)


def test_ship_turn_emits_nothing():
    k, _h, seen, deep, _ship = _world(tier=Tier.SHIP)
    k.run_turn(TurnInput("t_s", "salut"))
    assert deep.calls == 0 and not _events(seen, "route.fallback")
    assert "fallback" not in _done(seen) and "notice" not in _done(seen)


def test_deep_works_no_fallback_fields():
    k, _t, _ship, _deep = kernel([], deep_script=[Message("assistant", "profond")])
    hub, seen = _hub()
    k.observer = hub
    k._choose_route = lambda state: RouteDecision(tier=Tier.DEEP, reasoning=False, reason="test")
    turn = k.run_turn(TurnInput("t_ok", "salut"))
    assert turn.reply == "profond" and turn.fallback is None
    assert "fallback" not in _done(seen) and "notice" not in _done(seen) and not _events(seen, "route.fallback")


def test_byok_turn_emits_nothing():
    class Svc:
        def route_for_turn(self):
            return ByokRoute(name="Mon modèle", provider=_Down("HTTP 401 de https://x.example.test/v1: no"))

    k, _h, seen, *_ = _world(observe=True)
    k.byok = Svc()
    turn = k.run_turn(TurnInput("t_b", "salut"))
    assert turn.failure["code"] == "byok_unavailable"
    assert not _events(seen, "route.fallback")
    assert "fallback" not in _done(seen) and "notice" not in _done(seen)


@pytest.mark.parametrize("status", [403, 429])
def test_quota_emits_nothing(status):
    body = '{"error": {"code": "quota_exceeded", "message": "x"}}'
    k, _h, seen, deep, ship = _world(deep=_Down(f"HTTP {status} de https://amont.example.test/v1: {body}"))
    turn = k.run_turn(TurnInput("t_q", "salut"))
    assert deep.calls == 1 and len(ship.seen) == 0 and turn.failure["code"] == "quota_exceeded"
    assert not _events(seen, "route.fallback")
    assert "fallback" not in _done(seen) and "notice" not in _done(seen)


def test_both_tiers_down_no_notice_tier_unavailable_as_before():
    k, _h, seen, deep, _ship = _world(ship_down=_Down())
    turn = k.run_turn(TurnInput("t_both", "salut"))
    assert turn.failure["code"] == "tier_unavailable" and turn.escalated
    assert [v.rule for v in turn.verdicts].count("router:fallback") == 1
    done = _done(seen)
    assert "notice" not in done and "fallback" not in done
    assert all(FR not in (m.content or "") for m in turn.messages)


def test_observer_that_raises_does_not_stop_the_turn():
    class Boom:
        def __getattr__(self, name):
            def f(*a, **kw):
                raise RuntimeError("observateur en panne")
            return f

    k, *_ = _world(observe=False)
    k.observer = Boom()
    turn = k.run_turn(TurnInput("t_boom", "salut"))
    assert turn.reply == "voilà" and turn.fallback == {"from": "deep", "to": "ship"}


def test_two_fallbacks_in_one_turn_one_event_one_notice():
    script = [Message("assistant", tool_calls=[__import__("tests.unit.test_loop_contract", fromlist=["tc"]).tc("calendar_list", day="1")]),
              Message("assistant", "fini")]
    k, _h, seen, deep, ship = _world(script=script)
    turn = k.run_turn(TurnInput("t_two", "mon agenda ?"))
    assert deep.calls >= 2 and len(ship.seen) == 2 and turn.reply == "fini"
    assert len(_events(seen, "route.fallback")) == 1
    assert _done(seen)["notice"] == FR
