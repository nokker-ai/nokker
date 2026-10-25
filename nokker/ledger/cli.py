"""`nokker costs` — F6, PLAN-P1 §5.1 (« fonction CLI exposée … F2 la branchera dans nokker/cli.py »).

Deux niveaux :
  - `add_costs_subparser`/`costs_command` : ce que `nokker/cli.py` (F2) branche sur son `argparse`
    existant — aucun `argparse.ArgumentParser()` créé ici qui pourrait entrer en conflit avec le sien.
  - `main()` : un point d'entrée autonome (`python -m nokker.ledger.cli`) pour le dev/les tests, avant
    que F2 n'existe. Il construit SON PROPRE parser, séparé de celui de `add_costs_subparser`.
"""
from __future__ import annotations

import argparse
from datetime import date

from nokker.ledger.costs import Aggregates, LedgerBackend, TierStats
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope


def add_costs_subparser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Ajoute `costs` à un `argparse` existant. `nokker/cli.py` fournit `ledger` à `costs_command`
    lui-même (construit depuis sa propre config DSN) ; cette fonction ne construit aucun ledger."""
    p = subparsers.add_parser("costs", help="Coûts et agrégats du ledger (T5, A3, A5).")
    p.add_argument("--day", help="YYYY-MM-DD (défaut : tout le ledger, pas seulement aujourd'hui).")
    p.add_argument("--thread", dest="thread_id", help="Limiter à un fil (thread_id).")
    p.add_argument("--tier", choices=["local", "small", "ship", "deep"], help="Limiter à un palier.")
    p.set_defaults(_nokker_ledger_cmd=costs_command)
    return p


def costs_command(ledger: LedgerBackend, args: argparse.Namespace) -> int:
    day = date.fromisoformat(args.day) if getattr(args, "day", None) else None
    agg = ledger.aggregates(day=day, thread_id=getattr(args, "thread_id", None), tier=getattr(args, "tier", None))
    print(format_aggregates(agg))
    return 0


def format_aggregates(agg: Aggregates) -> str:
    cache = "non mesuré" if agg.cache_rate is None else f"{agg.cache_rate:.1%} ({agg.cache_measured_calls} appels mesurés)"
    chf_turn = f"{agg.chf_median_per_turn:.5f}" if agg.chf_median_per_turn is not None else "—"
    tokens = f"{agg.tokens_transported_median:.0f}" if agg.tokens_transported_median is not None else "—"
    latency = f"{agg.latency_median_s:.2f}" if agg.latency_median_s is not None else "—"
    lines = [
        f"appels: {agg.n_calls}  tours: {agg.n_turns}",
        f"tokens transportés (médiane/appel, A3): {tokens}",
        f"taux de cache (A5): {cache}",
        f"CHF total: {agg.chf_total:.5f}",
        f"CHF médian par tour (T5): {chf_turn}",
        f"latence médiane par appel: {latency} s",
    ]
    if agg.by_tier:
        lines.append("par palier:")
        for tier, stats in sorted(agg.by_tier.items()):
            lines.append(f"  {tier}: {_format_tier(stats)}")
    if agg.verdicts_by_decision:
        lines.append("verdicts par décision:")
        for decision, n in sorted(agg.verdicts_by_decision.items()):
            lines.append(f"  {decision}: {n}")
    return "\n".join(lines)


def _format_tier(stats: TierStats) -> str:
    cache = "non mesuré" if stats.cache_rate is None else f"{stats.cache_rate:.1%}"
    tokens = f"{stats.tokens_transported_median:.0f}" if stats.tokens_transported_median is not None else "—"
    return f"{stats.n_calls} appels, {stats.chf_total:.5f} CHF, tokens médian {tokens}, cache {cache}"


def _build_ledger(dsn: str | None) -> LedgerBackend:
    if dsn:
        from nokker.ledger.costs import PostgresLedger

        return PostgresLedger(dsn)
    from nokker.ledger.costs import InMemoryLedger

    return InMemoryLedger()


def main(argv: list[str] | None = None) -> int:
    """Autonome (dev/tests), pas ce que F2 appelle : F2 utilise `add_costs_subparser`/`costs_command`
    sur son propre parser et son propre ledger déjà construit."""
    parser = argparse.ArgumentParser(prog="python -m nokker.ledger.cli")
    parser.add_argument("--dsn", help="DSN Postgres ; sans lui, ledger en mémoire (vide, pour lire prices.yaml etc).")
    parser.add_argument("--tenant", default=INTERNAL_TENANT, help="locataire lu (RLS)")
    sub = parser.add_subparsers(dest="command", required=True)
    add_costs_subparser(sub)
    args = parser.parse_args(argv)
    with tenant_scope(args.tenant):
        ledger = _build_ledger(args.dsn)
        return args._nokker_ledger_cmd(ledger, args)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["add_costs_subparser", "costs_command", "format_aggregates", "main"]
