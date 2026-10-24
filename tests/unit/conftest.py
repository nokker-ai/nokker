"""Les tests unitaires jouent l'instance interne : ses opérateurs (« alice », « bruno » et leurs adresses) peuvent
donner un GO, comme le canari qui les pose dans sa config (`extra_go_senders`). Le noyau, lui, n'en a aucun par
défaut (`GoAuthorizedSenders().senders` vide) : un test qui vérifie ce défaut prend le marqueur `no_operators`."""
from __future__ import annotations

import pytest

from nokker.qa.invariants import GoAuthorizedSenders

TEST_OPERATORS = frozenset({"alice@example.ch", "alice", "alice", "bruno@example.ch", "bruno"})


def pytest_configure(config):
    config.addinivalue_line("markers", "no_operators: le défaut réel du noyau, sans opérateurs internes")


@pytest.fixture(autouse=True)
def _internal_operators(request, monkeypatch):
    if request.node.get_closest_marker("no_operators") is None:
        monkeypatch.setattr(GoAuthorizedSenders.__init__, "__defaults__", (TEST_OPERATORS,))
    yield
