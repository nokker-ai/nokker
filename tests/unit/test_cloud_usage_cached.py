"""Appel relayé (palier Lokkal) : `usage.prompt_tokens_details.cached_tokens` de la réponse du Router arrive au ledger
(`cached_reported` vrai, bonne valeur) ; champ absent = « non mesuré » (faux), jamais 0 mesuré."""
from __future__ import annotations

import pytest

from nokker.cloud.http import ledger_usage_recorder
from nokker.tenancy.context import INTERNAL_TENANT


class _Ledger:
    def __init__(self) -> None:
        self.calls: list = []

    def record(self, thread_id, turn_index, call) -> None:
        self.calls.append(call)


def _record(usage: dict):
    ledger = _Ledger()
    ledger_usage_recorder(ledger, "m")(INTERNAL_TENANT, "machine-1", usage, 0.5, "m", 1)
    return ledger.calls[0].usage


def test_cached_tokens_reach_the_ledger():
    u = _record({"prompt_tokens": 4500, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 3200}})
    assert u.cached_reported is True and u.cached == 3200 and u.prompt == 4500


def test_zero_cached_is_measured_but_missing_field_is_not():
    assert _record({"prompt_tokens": 9, "prompt_tokens_details": {"cached_tokens": 0}}).cached_reported is True
    u = _record({"prompt_tokens": 9, "completion_tokens": 1})
    assert u.cached_reported is False and u.cached == 0


@pytest.mark.parametrize("details", [None, "x", []])
def test_malformed_details_are_not_measured(details):
    assert _record({"prompt_tokens": 9, "prompt_tokens_details": details}).cached_reported is False
