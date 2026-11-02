"""Déclencheur nocturne de la consolidation (docs/CONSOLIDATION.md §5), à l'heure LOCALE de chaque compte.

Le noyau a déjà un cron (`nokker/agents/cron.py`, déclencheur `schedule` des agents, fuseau et changements d'heure
compris) : on le réutilise tel quel. On ne passe PAS par l'ordonnanceur Crew lui-même : il fait tourner des agents (un
modèle, des outils, des cartes) et il est éteint chez nokker.ai (`NOKKER_CREW_ENGINE`) ; la consolidation n'est pas un
agent, c'est un geste du noyau, sans outil.

Un fil de fond relit toutes les `tick_s` secondes les comptes connus (un locataire à la fois, dans son
`tenant_scope`) ; chaque compte a son cron et son fuseau (`/v1/memory/consolidation/settings`, défaut 03:30
Europe/Zurich). Échéance passée → une consolidation `schedule` ; la nuit est unique en base (index unique), donc deux
pods ou un redémarrage ne la font jamais deux fois. Au démarrage, l'échéance des dernières 24 h qui n'a pas tourné
est rattrapée."""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from nokker.agents.cron import next_run, zone
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

log = logging.getLogger("nokker.memory.consolidation.scheduler")
DAY = 86_400.0


@dataclass
class ConsolidationScheduler:
    service: Any  # Consolidator
    accounts: Callable[[], list[tuple[str, str]]]  # → [(locataire, compte)]
    clock: Callable[[], float] = time.time
    _due: dict[tuple[str, str], tuple[tuple[str, str], float | None]] = field(default_factory=dict)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)

    def _next(self, cron: str, tz: str, after: float) -> float | None:
        nxt = next_run(cron, tz, datetime.fromtimestamp(after, UTC))
        return nxt.timestamp() if nxt is not None else None

    def tick(self, now: float | None = None) -> list[dict[str, Any]]:
        now = float(self.clock()) if now is None else now
        done = []
        try:
            pairs = self.accounts()
        except Exception:
            log.exception("consolidation : comptes illisibles")
            return done
        for tid, acc in pairs:
            if tid == INTERNAL_TENANT or not acc:
                continue
            try:
                with tenant_scope(tid):
                    out = self._tick_account(tid, acc, now)
                if out is not None and "run" in out:  # « déjà faite », « occupé » : rien de nouveau
                    done.append(out)
            except Exception:
                log.exception("consolidation : échéance d'un compte en échec")
        return done

    def _tick_account(self, tid: str, acc: str, now: float) -> dict[str, Any] | None:
        actor = self.service.actor(acc)
        sched = self.service.schedule_of(actor)
        if not sched["enabled"]:
            self._due.pop((tid, acc), None)
            return None
        cfg = (sched["cron"], sched["tz"])
        key = (tid, acc)
        cur = self._due.get(key)
        if cur is None or cur[0] != cfg:
            prev = self._next(*cfg, now - DAY)  # rattrapage : l'échéance des dernières 24 h
            due = prev if prev is not None and prev <= now else self._next(*cfg, now)
            self._due[key] = (cfg, due)
            cur = self._due[key]
        due = cur[1]
        if due is None or now < due:
            return None
        night = datetime.fromtimestamp(due, zone(cfg[1])).strftime("%Y-%m-%d")
        out = self.service.run(acc, trigger="schedule", tz=cfg[1], night=night)
        if out.get("status") != "busy":  # occupé : on réessaie au tick suivant
            self._due[key] = (cfg, self._next(*cfg, now))
        return out

    def start(self, tick_s: float) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while True:
                time.sleep(tick_s)
                try:
                    self.tick()
                except Exception:
                    log.exception("consolidation : tick en échec")

        self._thread = threading.Thread(target=loop, daemon=True, name="nokker-memory-consolidation")
        self._thread.start()


def tenant_accounts(dsn: str, tenancy: Any) -> Callable[[], list[tuple[str, str]]]:
    """Comptes testeurs : un compte par locataire en bêta (`Tenancy.account_for_tenant`), l'interne exclu."""
    from nokker.store.conn import known_tenants

    def accounts() -> list[tuple[str, str]]:
        out = []
        for tid in known_tenants(dsn):
            if tid == INTERNAL_TENANT:
                continue
            a = tenancy.account_for_tenant(tid) if tenancy is not None else None
            if a is not None:
                out.append((tid, a.account_id))
        return out

    return accounts


__all__ = ["ConsolidationScheduler", "tenant_accounts"]
