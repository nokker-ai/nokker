"""Contrôle de démarrage de la mémoire longue directe (nokker/memory/corthexis.py) : sur une installation neuve, l'embed
télécharge encore son modèle ; avertissement et nouvel essai, ERREUR seulement si la mémoire reste injoignable."""
from __future__ import annotations

import logging
import threading

from nokker.memory import corthexis as cx


def _run(monkeypatch, caplog, answers):
    mem = object.__new__(cx.DirectCorthexisMemory)
    seq = iter(answers)
    mem.status = lambda max_age_s=0.0: next(seq)
    monkeypatch.setattr(cx, "STARTUP_RETRY_EVERY_S", 0)
    started = []
    real = threading.Thread

    def thread(*a, **k):
        t = real(*a, **k)
        started.append(t)
        return t

    monkeypatch.setattr(cx.threading, "Thread", thread)
    with caplog.at_level(logging.INFO, logger=cx.logger.name):
        mem.warm_up()
        started[0].join(5)
    return [(r.levelno, r.getMessage()) for r in caplog.records if r.name == cx.logger.name]


def test_embed_not_ready_yet_warns_then_ready(monkeypatch, caplog):
    monkeypatch.setattr(cx, "STARTUP_RETRY_S", 60)
    down = {"ok": False, "error": "embed injoignable"}
    logs = _run(monkeypatch, caplog, [down, down, {"ok": True, "store": {"notes": 3}, "embed": {"ms": 9}}])
    assert [lvl for lvl, _ in logs] == [logging.WARNING, logging.WARNING, logging.INFO]
    assert "nouvel essai" in logs[0][1] and "prête" in logs[-1][1]


def test_still_down_after_the_window_is_an_error(monkeypatch, caplog):
    monkeypatch.setattr(cx, "STARTUP_RETRY_S", 0)
    logs = _run(monkeypatch, caplog, [{"ok": False, "error": "base injoignable"}])
    assert [lvl for lvl, _ in logs] == [logging.ERROR] and "INDISPONIBLE" in logs[0][1]
