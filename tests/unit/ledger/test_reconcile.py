from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from nokker.ledger.costs import InMemoryLedger
from nokker.ledger.prices import load_from_yaml
from nokker.ledger.reconcile import (
    broker_usage_usd_from_sqlite, format_reconciliation, ledger_catalog_total_chf, ledger_total_chf, reconcile,
)
from tests.unit.ledger.test_costs import make_call

TODAY = dt.datetime.now(dt.timezone.utc).date()


def _ledger_with_known_cost(chf: float) -> InMemoryLedger:
    ledger = InMemoryLedger(prices=load_from_yaml())
    ledger.record("f", 0, make_call(chf=chf))
    return ledger


# ───────────────────────── mode router, total fourni à la main ─────────────────────────


def test_reconcile_router_mode_within_tolerance():
    ledger = _ledger_with_known_cost(0.01)
    r = reconcile(ledger, TODAY, mode="router", router_total_usd=0.01 * 1.25)  # même total, converti en USD
    assert r.within_tolerance
    assert r.diff_pct == pytest.approx(0.0, abs=1e-6)


def test_reconcile_router_mode_outside_tolerance():
    ledger = _ledger_with_known_cost(0.01)
    r = reconcile(ledger, TODAY, mode="router", router_total_usd=100.0)  # facture très différente
    assert not r.within_tolerance
    assert abs(r.diff_pct) > 2.0


def test_reconcile_router_mode_requires_a_reference():
    ledger = _ledger_with_known_cost(0.01)
    with pytest.raises(ValueError):
        reconcile(ledger, TODAY, mode="router")


def test_reconcile_unknown_mode_raises():
    ledger = _ledger_with_known_cost(0.01)
    with pytest.raises(ValueError):
        reconcile(ledger, TODAY, mode="n'importe-quoi", router_total_usd=1.0)


# ───────────────────────── mode catalog (compte interne à prix nul, décision Alice 17.10) ─────────────────────────


def test_reconcile_catalog_mode_reprices_from_scratch_ignoring_stored_chf():
    """Le `chf` stocké peut être 0 (compte interne) : le mode catalogue reprix l'appel lui-même."""
    ledger = InMemoryLedger(prices=load_from_yaml())
    ledger.record("f", 0, make_call(chf=0.0, model="qwen/qwen3.8-27b", prompt=1_000_000, cached=0, completion=0))
    r = reconcile(ledger, TODAY, mode="catalog")
    assert r.ledger_chf == pytest.approx(0.0)  # ce qui est stocké
    assert r.reference_chf == pytest.approx(0.20 / 1.25)  # ce que le catalogue aurait facturé
    assert not r.within_tolerance  # 0 vs un total non nul -> écart


def test_ledger_total_chf_and_catalog_total_chf_helpers():
    ledger = _ledger_with_known_cost(0.03)
    assert ledger_total_chf(ledger, TODAY) == pytest.approx(0.03)
    cat_total = ledger_catalog_total_chf(ledger, TODAY, load_from_yaml())
    assert cat_total >= 0.0  # recalcul indépendant, ne doit juste pas planter / être négatif


def test_format_reconciliation_contains_verdict():
    ledger = _ledger_with_known_cost(0.01)
    r = reconcile(ledger, TODAY, mode="router", router_total_usd=0.01 * 1.25)
    text = format_reconciliation(r)
    assert "OK" in text
    assert r.mode in text


# ───────────────────────── broker_usage_usd_from_sqlite : même requête que store.usage_daily ─────────────────────────


def _make_fake_broker_db(path: str, rows: list[dict]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE requests (account_id TEXT, ts REAL, status INTEGER, cost_micro INTEGER, model TEXT)"
    )
    conn.executemany(
        "INSERT INTO requests (account_id, ts, status, cost_micro, model) VALUES (?,?,?,?,?)",
        [(r["account_id"], r["ts"], r["status"], r["cost_micro"], r.get("model", "m")) for r in rows],
    )
    conn.commit()
    conn.close()


def test_broker_usage_usd_from_sqlite_sums_only_billed_requests(tmp_path):
    db = str(tmp_path / "broker.db")
    day = dt.date(2026, 10, 17)
    ts_in_day = dt.datetime(2026, 10, 17, 12, 0, tzinfo=dt.timezone.utc).timestamp()
    ts_other_day = dt.datetime(2026, 10, 16, 12, 0, tzinfo=dt.timezone.utc).timestamp()
    _make_fake_broker_db(db, [
        {"account_id": "acct_x", "ts": ts_in_day, "status": 200, "cost_micro": 10_000},
        {"account_id": "acct_x", "ts": ts_in_day, "status": 200, "cost_micro": 5_000},
        {"account_id": "acct_x", "ts": ts_in_day, "status": 429, "cost_micro": 0},  # pas facturé, ignoré
        {"account_id": "acct_other", "ts": ts_in_day, "status": 200, "cost_micro": 999_999},  # autre compte
        {"account_id": "acct_x", "ts": ts_other_day, "status": 200, "cost_micro": 777},  # mauvais jour
    ])
    usd = broker_usage_usd_from_sqlite(db, "acct_x", day)
    assert usd == pytest.approx(15_000 / 1_000_000)


def test_reconcile_router_mode_with_broker_sqlite(tmp_path):
    db = str(tmp_path / "broker.db")
    ts_in_day = dt.datetime.combine(TODAY, dt.time(12, 0), tzinfo=dt.timezone.utc).timestamp()
    # ledger = 0.01 CHF = 0.0125 USD -> 12_500 micro-dollars facturés côté broker pour correspondre.
    _make_fake_broker_db(db, [{"account_id": "acct_x", "ts": ts_in_day, "status": 200, "cost_micro": 12_500}])
    ledger = _ledger_with_known_cost(0.01)
    r = reconcile(ledger, TODAY, mode="router", broker_sqlite_path=db, account_id="acct_x")
    assert r.within_tolerance
