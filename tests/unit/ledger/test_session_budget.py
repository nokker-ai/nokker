"""Plafond de dépense par session (docs/NOKKAN-HOST-API.md §2, drapeau `host_session_budget`).

- drapeau éteint : aucun plafond vérifié, `…/budget` en 404, `budget` ignoré à l'ouverture, réponses inchangées ;
- drapeau allumé : plafond en jetons et/ou en coût décompté sur le ledger, refus AVANT tout appel modèle, code stable
  et `retry: false` pour l'hôte ; routes par le VRAI serveur (`make_handler`).
"""
from __future__ import annotations

import json

import pytest

from nokker.config import NokkerConfig
from nokker.core.turn import LLMCall, Message, Tier, TurnInput, Usage
from nokker.ledger.costs import InMemoryLedger
from nokker.ledger.prices import PriceCatalog, PriceEntry
from nokker.ledger.session_budget import (
    EXHAUSTED,
    UNAVAILABLE,
    UNVERIFIABLE,
    BudgetError,
    SessionBudgets,
    parse_limits,
)
from tests.unit.api.host_api_support import Served, host_kernel

# Le modèle scripté consomme 100 jetons d'entrée par appel (`ScriptedProvider`). À 10 000 USD le million : 1 USD
# l'appel, soit 0,80 CHF au taux par défaut du serveur (1 USD = 0,80 CHF).
PRICED = PriceCatalog(entries={"scripted": PriceEntry(model="scripted", prompt_usd_per_m=10_000.0,
                                                      cached_usd_per_m=0.0, completion_usd_per_m=0.0, source="test")})


def _answers(n):
    return [Message("assistant", f"réponse {i}") for i in range(1, n + 1)]


def _served(tmp_path, n=6, *, prices=PRICED, on=True):
    k, ship = host_kernel(_answers(n), prices=prices)
    s = Served(tmp_path, k)
    if on:
        budgets = SessionBudgets(ledger=k.ledger)
        k.session_budget = budgets
        s.api.session_budget = budgets
    s.api.emit = s.bus.publish
    return s, k, ship


def _reply(out):
    return out["choices"][0]["message"]["content"]


# ─────────────── drapeau éteint = inchangé ───────────────


def test_flag_is_off_by_default_and_wires_nothing():
    from nokker.api.server import configure_host_api

    k, _ = host_kernel([])
    assert NokkerConfig().host_session_budget is False
    configure_host_api(NokkerConfig(), k, None)
    assert k.session_budget is None
    configure_host_api(NokkerConfig(host_session_budget=True), k, None)
    assert isinstance(k.session_budget, SessionBudgets) and k.session_budget.ledger is k.ledger


def test_flag_off_no_route_budget_field_ignored_and_turns_never_refused(tmp_path):
    s, k, ship = _served(tmp_path, on=False)
    try:
        status, opened = s.session(budget={"max_total_tokens": 1})
        assert status == 201 and set(opened) == {"thread_id", "cwd", "workspace"}  # champ ignoré, réponse d'avant
        thread = opened["thread_id"]
        for method in (None, "PUT"):
            status, err = s.req(f"/v1/local/sessions/{thread}/budget", method=method,
                                body={"max_total_tokens": 1} if method else None)
            assert status == 404 and err == {"error": {"message": "not found"}}  # la route n'existe pas
        for i in (1, 2, 3):
            status, out = s.chat(thread, f"tour {i}")
            assert status == 200 and _reply(out) == f"réponse {i}" and "nokker" not in out
        assert len(ship.seen) == 3
        assert all("error" not in d for _, d in s.events("turn.done"))
    finally:
        s.close()


# ─────────────── plafond en jetons ───────────────


def test_token_cap_refuses_without_any_model_call_and_host_sees_a_stable_code(tmp_path):
    s, k, ship = _served(tmp_path)
    try:
        thread = s.session()[1]["thread_id"]
        url = f"/v1/local/sessions/{thread}/budget"
        status, view = s.req(url)
        assert status == 200 and view["limits"] == {"max_cost_chf": None, "max_cost_usd": None,
                                                    "max_total_tokens": None} and view["exhausted"] is False
        status, view = s.req(url, method="PUT", body={"max_total_tokens": 150})
        assert status == 200 and view["limits"]["max_total_tokens"] == 150 and view["spent"]["total_tokens"] == 0
        assert ("session.budget", view) in s.events("session.budget")
        assert _reply(s.chat(thread, "un")[1]) == "réponse 1"  # 100 jetons : sous le plafond
        assert _reply(s.chat(thread, "deux")[1]) == "réponse 2"  # parti à 100 < 150 : un tour commencé finit
        status, out = s.chat(thread, "trois")
        assert status == 200 and len(ship.seen) == 2 and len(ship.script) == 4  # AUCUN appel modèle de plus
        assert _reply(out).startswith("Plafond de dépense de cette session atteint (200 jetons sur 150 jetons).")
        assert out["nokker"]["error"] == {"code": EXHAUSTED, "retry": False, "limit": "tokens",
                                          "message": "plafond de dépense de la session : aucun appel modèle"}
        assert out["usage"]["total_tokens"] == 0
        done = s.events("turn.done")[-1][1]
        assert done["error"]["code"] == EXHAUSTED and done["error"]["retry"] is False and done["error"]["limit"] == "tokens"
        refused = k.store.turns[-1]
        assert refused.calls == [] and [v.rule for v in refused.verdicts] == ["session_budget:tokens"]
        assert s.req(url)[1]["exhausted"] is True and s.req(url)[1]["reason"] == EXHAUSTED
        # l'hôte relève le plafond en cours de session : le tour suivant repart
        assert s.req(url, method="PUT", body={"max_total_tokens": 1000})[1]["exhausted"] is False
        assert _reply(s.chat(thread, "quatre")[1]) == "réponse 3" and len(ship.seen) == 3
        # tout à null : plus de plafond
        assert s.req(url, method="PUT", body={"max_total_tokens": None})[1]["limits"]["max_total_tokens"] is None
        assert k.session_budget.limits_of(thread) is None
    finally:
        s.close()


def test_streamed_refusal_carries_the_error_in_the_final_chunk(tmp_path):
    s, k, ship = _served(tmp_path)
    try:
        thread = s.session(budget={"max_total_tokens": 50})[1]["thread_id"]
        s.chat(thread, "un")
        status, chunks = s.chat(thread, "deux", stream_tokens=True)
        final = next(c for c in chunks if (c.get("nokker") or {}).get("final"))
        assert status == 200 and final["nokker"]["error"]["code"] == EXHAUSTED and final["nokker"]["error"]["retry"] is False
        assert len(ship.seen) == 1
    finally:
        s.close()


# ─────────────── plafond en coût ───────────────


def test_cost_cap_in_usd_set_at_session_creation(tmp_path):
    s, k, ship = _served(tmp_path)
    try:
        status, opened = s.session(budget={"max_cost_usd": 1.5})
        assert status == 201 and opened["budget"]["limits"] == {"max_cost_chf": None, "max_cost_usd": 1.5,
                                                               "max_total_tokens": None}
        thread = opened["thread_id"]
        s.chat(thread, "un")  # 1 USD
        s.chat(thread, "deux")  # 2 USD : parti sous le plafond
        out = s.chat(thread, "trois")[1]
        assert len(ship.seen) == 2 and out["nokker"]["error"]["code"] == EXHAUSTED and out["nokker"]["error"]["limit"] == "cost"
        assert "(2.00 USD sur 1.50 USD)" in _reply(out)
        view = s.req(f"/v1/local/sessions/{thread}/budget")[1]
        assert view["spent"]["cost_usd"] == 2.0 and view["spent"]["cost_chf"] == 1.6 and view["spent"]["calls"] == 2
        assert view["spent"]["turns"] == 2 and view["spent"]["unpriced_models"] == []
    finally:
        s.close()


def test_cost_cap_in_chf_uses_the_ledger_amount(tmp_path):
    s, k, ship = _served(tmp_path)
    try:
        thread = s.session(budget={"max_cost_chf": 0.5})[1]["thread_id"]
        s.chat(thread, "un")  # 0,80 CHF
        out = s.chat(thread, "deux")[1]
        assert len(ship.seen) == 1 and "(0.80 CHF sur 0.50 CHF)" in _reply(out)
    finally:
        s.close()


def test_cost_cap_on_an_unpriced_model_is_unverifiable_and_refused_but_tokens_still_work(tmp_path):
    s, k, ship = _served(tmp_path, prices=PriceCatalog(entries={}))
    try:
        thread = s.session(budget={"max_cost_usd": 100})[1]["thread_id"]
        assert _reply(s.chat(thread, "un")[1]) == "réponse 1"  # rien de dépensé encore : rien à vérifier
        out = s.chat(thread, "deux")[1]
        assert len(ship.seen) == 1 and out["nokker"]["error"] == {
            "code": UNVERIFIABLE, "retry": False, "message": "plafond de dépense de la session : aucun appel modèle"}
        assert _reply(out).startswith("Plafond de dépense de cette session invérifiable")
        view = s.req(f"/v1/local/sessions/{thread}/budget")[1]
        assert view["reason"] == UNVERIFIABLE and view["spent"]["unpriced_models"] == ["scripted"]
        assert view["spent"]["cost_usd"] is None and view["spent"]["cost_chf"] is None  # inconnu, jamais 0.0
        assert view["spent"]["total_tokens"] == 100
        # même fil, plafond en jetons : vérifiable sans prix
        s.req(f"/v1/local/sessions/{thread}/budget", method="PUT", body={"max_total_tokens": 10_000})
        assert _reply(s.chat(thread, "trois")[1]) == "réponse 2"
    finally:
        s.close()


# ─────────────── validation, sessions, sous-agents ───────────────


def test_route_validation_and_auth(tmp_path):
    s, k, _ = _served(tmp_path)
    try:
        thread = s.session()[1]["thread_id"]
        url = f"/v1/local/sessions/{thread}/budget"
        assert s.req(url, token=None)[0] == 401
        assert s.req("/v1/local/sessions/th_inconnu/budget")[0] == 404
        for bad in ({"max_cost_usd": 1, "max_cost_chf": 1}, {"max_total_tokens": -1}, {"max_total_tokens": 1.5},
                    {"max_cost_usd": "2"}, {"max_cost_usd": True}, {"max_budget_usd": 2}):
            status, err = s.req(url, method="PUT", body=bad)
            assert status == 400 and err["error"]["code"] == "bad_budget", bad
        assert k.session_budget.limits_of(thread) is None
        status, err = s.session(budget={"max_total_tokens": "beaucoup"})
        assert status == 400 and err["error"]["code"] == "bad_budget"
        assert len(s.api.code.sessions()) == 1  # un plafond refusé n'ouvre pas de session
    finally:
        s.close()


def test_refusal_texts_are_bilingual_like_the_project_budget_ones():
    """Même mécanisme de langue que `nokker/team/budget.py::REPLY` : un seul texte, français, ligne vide, anglais."""
    from nokker.ledger.session_budget import REPLY_EXHAUSTED, REPLY_UNVERIFIABLE
    from nokker.team.budget import REPLY as PROJECT

    for text in (*PROJECT.values(), REPLY_EXHAUSTED, REPLY_UNVERIFIABLE):
        fr, sep, en = text.partition("\n\n")
        assert sep and fr and en and "\n" not in fr and "\n" not in en
    ledger = InMemoryLedger(prices=PRICED)
    budgets = SessionBudgets(ledger=ledger)
    ledger.record("th_t", 0, _call())
    budgets.set("th_t", {"max_total_tokens": 50})
    assert budgets.check("th_t").reply == (
        "Plafond de dépense de cette session atteint (100 jetons sur 50 jetons). Rien n'a été envoyé au modèle ; la "
        "conversation est gardée.\n\n"
        "Spending cap of this session reached (100 tokens out of 50 tokens). Nothing was sent to the model; your "
        "conversation is kept.")
    budgets.set("th_t", {"max_cost_usd": 0.5})
    assert budgets.check("th_t").reply == (
        "Plafond de dépense de cette session atteint (1.00 USD sur 0.50 USD). Rien n'a été envoyé au modèle ; la "
        "conversation est gardée.\n\n"
        "Spending cap of this session reached (1.00 USD out of 0.50 USD). Nothing was sent to the model; your "
        "conversation is kept.")
    for limits, catalog in (({"max_cost_usd": 5}, PriceCatalog(entries={})),):
        unpriced = InMemoryLedger(prices=catalog)
        b = SessionBudgets(ledger=unpriced)
        unpriced.record("th_u", 0, _call())
        b.set("th_u", limits)
        assert b.check("th_u").reply == (
            "Plafond de dépense de cette session invérifiable pour l'instant. Par prudence, rien n'a été envoyé au "
            "modèle ; la conversation est gardée.\n\n"
            "The spending cap of this session cannot be checked right now. To be safe, nothing was sent to the model; "
            "your conversation is kept.")


def test_parse_limits():
    assert parse_limits({}) == {"max_cost": None, "currency": None, "max_total_tokens": None}
    assert parse_limits({"max_cost_usd": 2, "max_total_tokens": 10}) == {"max_cost": 2.0, "currency": "USD",
                                                                       "max_total_tokens": 10}
    assert parse_limits({"max_cost_chf": 0})["currency"] == "CHF"
    for bad in ([], "x", {"max_cost_usd": float("inf")}, {"max_cost_usd": float("nan")}):
        with pytest.raises(BudgetError):
            parse_limits(bad)


def _call(model="scripted", prompt=100):
    return LLMCall(tier=Tier.SHIP, model=model, reasoning=False, prefix_sha256="", layer_tokens={},
                   usage=Usage(prompt=prompt), latency_s=0.0, route_reason="t")


def test_subagent_calls_count_and_a_subagent_turn_is_refused_on_the_session_cap():
    ledger = InMemoryLedger(prices=PRICED)
    budgets = SessionBudgets(ledger=ledger)
    budgets.set("th_1", {"max_total_tokens": 250})
    ledger.record("th_1", 0, _call())
    ledger.record("th_1/sa-ab", 0, _call())
    assert budgets.check("th_1") is None and budgets.check("th_1/sa-ab") is None and budgets.check("th_2") is None
    ledger.record("th_1/sa-ab/sa-cd", 0, _call())
    for thread in ("th_1", "th_1/sa-ab"):
        got = budgets.check(thread)
        assert got is not None and got.failure["code"] == EXHAUSTED and got.verdict.rule == "session_budget:tokens"
    assert budgets.check("th_2") is None  # un autre fil n'est pas touché
    k, ship = host_kernel(_answers(1), prices=PRICED)
    k.ledger, k.session_budget = ledger, budgets
    turn = k.run_turn(TurnInput("th_1/sa-ab", "continue"))
    assert ship.seen == [] and turn.failure["retry"] is False


def test_limits_survive_a_restart_and_a_broken_file_fails_closed(tmp_path):
    ledger = InMemoryLedger(prices=PRICED)
    path = tmp_path / "state" / "session-budgets.json"
    SessionBudgets(ledger=ledger, path=path).set("th_1", {"max_cost_usd": 3, "max_total_tokens": 100})
    assert json.loads(path.read_text()) == {"th_1": {"currency": "USD", "max_cost": 3.0, "max_total_tokens": 100}}
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    again = SessionBudgets(ledger=ledger, path=path)
    assert again.limits_of("th_1") == {"currency": "USD", "max_cost": 3.0, "max_total_tokens": 100}
    ledger.record("th_1", 0, _call())
    assert again.check("th_1").failure["code"] == EXHAUSTED
    path.write_text("{pas du json")
    broken = SessionBudgets(ledger=ledger, path=path)
    got = broken.check("th_nouveau")
    assert got is not None and got.failure["code"] == UNAVAILABLE and got.failure["retry"] is True
    broken.set("th_nouveau", {"max_total_tokens": 1000})  # reposé : le fichier est de nouveau lisible
    assert broken.check("th_nouveau") is None


def test_unreadable_ledger_refuses_instead_of_spending_blind():
    class Down:
        prices = PRICED

        def fetch_calls(self, **kw):
            raise RuntimeError("base injoignable")

    budgets = SessionBudgets(ledger=Down())
    budgets._limits["th_1"] = {"max_cost": 1.0, "currency": "USD", "max_total_tokens": None}
    got = budgets.check("th_1")
    assert got.failure["code"] == UNAVAILABLE and got.failure["retry"] is True
    assert budgets.check("th_sans_plafond") is None  # sans plafond : le ledger n'est même pas lu
