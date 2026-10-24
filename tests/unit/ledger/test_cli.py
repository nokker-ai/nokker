from __future__ import annotations

import argparse

from nokker.core.turn import Tier
from nokker.ledger.cli import add_costs_subparser, format_aggregates, main
from nokker.ledger.costs import InMemoryLedger
from nokker.ledger.prices import load_from_yaml
from tests.unit.ledger.test_costs import make_call


def test_add_costs_subparser_integrates_into_a_host_parser():
    """Simule ce que F2 fait dans nokker/cli.py : un parser hôte, sa propre config DSN, PUIS costs."""
    host = argparse.ArgumentParser(prog="nokker")
    sub = host.add_subparsers(dest="command", required=True)
    add_costs_subparser(sub)
    args = host.parse_args(["costs", "--day", "2026-10-17", "--tier", "ship"])
    assert args.command == "costs"
    assert args.day == "2026-10-17"
    assert args.tier == "ship"
    assert callable(args._nokker_ledger_cmd)


def test_costs_command_prints_aggregates(capsys):
    ledger = InMemoryLedger(prices=load_from_yaml())
    ledger.record("f", 0, make_call(tier=Tier.SHIP, chf=0.01))
    host = argparse.ArgumentParser()
    sub = host.add_subparsers(dest="command", required=True)
    add_costs_subparser(sub)
    args = host.parse_args(["costs"])
    rc = args._nokker_ledger_cmd(ledger, args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "appels: 1" in out
    assert "CHF total: 0.01000" in out


def test_format_aggregates_shows_non_mesure_when_cache_unknown():
    ledger = InMemoryLedger(prices=load_from_yaml())
    ledger.record("f", 0, make_call(cached_reported=False))
    agg = ledger.aggregates()
    text = format_aggregates(agg)
    assert "non mesuré" in text


def test_main_entrypoint_in_memory(capsys):
    rc = main(["costs"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "appels: 0" in out
