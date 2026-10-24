"""Rapprochement facture (T5) : le total du ledger d'une journée == la facture Router de la même clé, à
± 2 %. F6, PLAN-P1 §5.1.

## Route d'usage par clé sur le Router : NON — vérifié par lecture de code le 17.10.2026

`broker/app.py` et `store.py` du Router (lecture seule, aucun appel, aucune écriture) :

  - `GET /v1/models` (app.py:423-440) expose le CATALOGUE de prix public, pas une consommation.
  - La consommation existe bien côté serveur — `store.usage_summary()` et `store.usage_daily()`
    (store.py:726-744, table SQLite `requests`, colonne `cost_micro` posée à chaque requête facturée
    dans `bill()`, app.py:1426-1442) — mais ces deux fonctions ne sont appelées QUE par des pages HTML
    protégées par cookie de session (`GET /account/usage` → `usage_html`, app.py:689 ; `GET
    /account/billing` → `billing_ctx`, app.py:696,712). Aucune n'est exposée en JSON à une clé API.
  - Donc : **pas de route d'usage par clé sur le Router**, au sens d'un appel qu'un script pourrait
    faire avec juste `Authorization: Bearer <clé>`. Il faut soit une session web (cookie de compte),
    soit un accès direct à la base du broker.

## Deux modes de rapprochement, les deux prévus

1. `mode="router"` (défaut, compte normal) : compare le ledger à la vraie facture Router. Faute de
   route API, ce module accepte SOIT `--router-total-usd` (un total obtenu à la main — export de
   `/account/billing`, ou lu par une session qui a le cookie), SOIT `--broker-sqlite` + `--account-id`
   (accès direct, lecture seule, au fichier SQLite du broker — même requête que `usage_daily()`, à
   lancer depuis un hôte qui voit ce fichier, ex. `tokenalp1`).
2. `mode="catalog"` (compte interne à prix nul, décision Nick 17.10 — Router `356d4d3`) : dès que la
   clé de banc devient un compte interne, son `cost_micro` restera à 0 EN CONTINU côté broker (prix
   nul, cf. note `nokker-p0` « Crédit interne … BLOQUÉ, pas fait » : conçu, pas encore posé, mais c'est
   la cible). Il n'y a alors plus de « vraie facture » à comparer : ce mode reprix chaque appel du
   ledger au tarif CATALOGUE public (`prices.py`) et compare ce total à lui-même — un contrôle de
   cohérence interne du ledger, pas un rapprochement externe. Les deux modes partagent le même seuil
   ±2 % et la même structure `Reconciliation`.
"""
from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from datetime import date

from nokker.ledger.costs import LedgerBackend
from nokker.ledger.prices import DEFAULT_CHF_USD_RATE, PriceCatalog
from nokker.ledger.prices import load as load_prices
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

WITHIN_PCT = 2.0


@dataclass(frozen=True)
class Reconciliation:
    day: date
    mode: str  # router | catalog
    ledger_chf: float
    reference_chf: float
    diff_chf: float
    diff_pct: float | None  # None seulement si reference_chf == 0 (division impossible)
    within_tolerance: bool


def broker_usage_usd_from_sqlite(db_path: str, account_id: str, day: date) -> float:
    """Même requête que `store.Store.usage_daily()` (store.py:734-739), en lecture seule, sans passer
    par le serveur. `status=200` : seules les requêtes réellement facturées (comme `usage_daily`)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT SUM(cost_micro) AS cost_micro FROM requests "
            "WHERE account_id = ? AND date(ts, 'unixepoch') = ? AND status = 200",
            (account_id, day.isoformat()),
        ).fetchone()
    finally:
        conn.close()
    micro = row["cost_micro"] if row and row["cost_micro"] is not None else 0
    return micro / 1_000_000


def ledger_total_chf(ledger: LedgerBackend, day: date, *, thread_id: str | None = None) -> float:
    return ledger.aggregates(day=day, thread_id=thread_id).chf_total


def ledger_catalog_total_chf(ledger: LedgerBackend, day: date, prices: PriceCatalog, *,
                              thread_id: str | None = None) -> float:
    """Reprix chaque appel du ledger au tarif catalogue — ignore le `chf` déjà stocké (mode interne)."""
    total_usd = 0.0
    for c in ledger.fetch_calls(day=day, thread_id=thread_id):
        usd = prices.cost_usd(c.model, c.new_tokens, c.cached_tokens, c.completion_tokens)
        if usd is not None:
            total_usd += usd
    return prices.usd_to_chf(total_usd)


def reconcile(ledger: LedgerBackend, day: date, *, mode: str = "router", thread_id: str | None = None,
              broker_sqlite_path: str | None = None, account_id: str | None = None,
              router_total_usd: float | None = None, prices: PriceCatalog | None = None,
              chf_usd_rate: float = DEFAULT_CHF_USD_RATE) -> Reconciliation:
    if mode not in ("router", "catalog"):
        raise ValueError(f"mode inconnu: {mode!r} (router|catalog)")

    ledger_chf = ledger_total_chf(ledger, day, thread_id=thread_id)

    if mode == "catalog":
        reference_chf = ledger_catalog_total_chf(ledger, day, prices or load_prices(), thread_id=thread_id)
    else:
        if router_total_usd is not None:
            reference_usd = router_total_usd
        elif broker_sqlite_path and account_id:
            reference_usd = broker_usage_usd_from_sqlite(broker_sqlite_path, account_id, day)
        else:
            raise ValueError(
                "mode=router nécessite --router-total-usd (export manuel) OU "
                "--broker-sqlite + --account-id (accès direct à la base, lecture seule)."
            )
        reference_chf = reference_usd / chf_usd_rate

    diff = ledger_chf - reference_chf
    pct = (diff / reference_chf * 100) if reference_chf else None
    within = (abs(pct) <= WITHIN_PCT) if pct is not None else (abs(diff) < 1e-9)
    return Reconciliation(day=day, mode=mode, ledger_chf=ledger_chf, reference_chf=reference_chf, diff_chf=diff,
                           diff_pct=pct, within_tolerance=within)


def format_reconciliation(r: Reconciliation) -> str:
    pct = f"{r.diff_pct:+.2f}%" if r.diff_pct is not None else "—"
    verdict = "OK (≤ 2 %)" if r.within_tolerance else "ÉCART (> 2 %)"
    return (f"{r.day.isoformat()} [{r.mode}] ledger={r.ledger_chf:.5f} CHF  référence={r.reference_chf:.5f} CHF  "
            f"diff={r.diff_chf:+.5f} CHF ({pct})  -> {verdict}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m nokker.ledger.reconcile", description=__doc__.split("\n\n")[0])
    parser.add_argument("--dsn", required=True, help="DSN Postgres du ledger.")
    parser.add_argument("--day", required=True, help="YYYY-MM-DD.")
    parser.add_argument("--thread", dest="thread_id", default=None)
    parser.add_argument("--mode", choices=["router", "catalog"], default="router")
    parser.add_argument("--router-total-usd", type=float, default=None)
    parser.add_argument("--broker-sqlite", dest="broker_sqlite_path", default=None)
    parser.add_argument("--account-id", default=None)
    parser.add_argument("--tenant", default=INTERNAL_TENANT, help="locataire lu (RLS, docs/TENANCY.md §4)")
    args = parser.parse_args(argv)

    from nokker.ledger.costs import PostgresLedger

    with tenant_scope(args.tenant):
        ledger = PostgresLedger(args.dsn)
        r = reconcile(ledger, date.fromisoformat(args.day), mode=args.mode, thread_id=args.thread_id,
                      broker_sqlite_path=args.broker_sqlite_path, account_id=args.account_id,
                      router_total_usd=args.router_total_usd)
    print(format_reconciliation(r))
    return 0 if r.within_tolerance else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "Reconciliation", "WITHIN_PCT", "broker_usage_usd_from_sqlite", "format_reconciliation", "ledger_catalog_total_chf",
    "ledger_total_chf", "reconcile",
]
