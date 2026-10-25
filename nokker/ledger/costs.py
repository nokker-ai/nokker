"""Implémentation du port `Ledger` (nokker.core.ports) — F6, PLAN-P1 §5.1.

Deux backends, même comportement (les tests tournent sur les deux) :
  - `InMemoryLedger` : pour les tests et un noyau sans Postgres.
  - `PostgresLedger` : psycopg 3, tables `ledger_entries`/`ledger_verdicts`
    (nokker/ledger/migrations/0001_ledger_entries.sql), INDÉPENDANTES du store de F2.

Coût par appel (priorité, PLAN-P1 §5.1 F6) :
  1. `call.chf` si déjà renseigné — c'est l'emplacement où `providers/sokkan.py` (F2) pose le `usage.cost`
     renvoyé par le Router quand il existe (confirmé dans le code du broker : `provider/app.py` calcule
     et renvoie `usage.cost` à chaque réponse, cf. docstring de `prices.py`). Ce module ne réclame jamais
     cette valeur lui-même : il n'a accès qu'à `LLMCall`, pas à la réponse brute du provider.
  2. sinon la grille (`prices.py`) : `(prompt_tokens - cached_tokens) * prix_entrée + cached_tokens *
     prix_cache + completion_tokens * prix_sortie`, convertie en CHF au taux de `PriceCatalog`.

`usage.cached_reported = False` (A5, « non mesuré ») ne change PAS ce calcul : `usage.cached` est alors
0 par construction (l'amont ne l'a jamais rempli), donc le résultat est le même que si on savait le
cache nul — rien n'est déduit en trop. Le drapeau sert seulement aux agrégats (`cache_rate`) : on n'y
affiche jamais un taux de cache mesuré sur des appels qui n'en ont rapporté aucun.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from nokker.core.turn import LLMCall, Verdict
from nokker.ledger.attribution import current_tag
from nokker.ledger.prices import PriceCatalog
from nokker.ledger.prices import load as load_prices

MIGRATION_SQL = (Path(__file__).parent / "migrations" / "0001_ledger_entries.sql").read_text(encoding="utf-8")
# Colonnes du projet d'équipe (attribution d'un appel, docs/LOKKAL.md §7.15) : migration du noyau, rejouée ici pour
# un ledger migré seul ; ses politiques d'équipe attendent la migration 023 (étape gardée).
PROJECT_SQL = (Path(__file__).parent.parent / "store" / "migrations" / "037_team_budget_brief.sql").read_text(
    encoding="utf-8")
# Colonnes du foyer (lot X23, quota commun) : même principe, migration 039 rejouée ici, étapes gardées.
HOUSEHOLD_SQL = (Path(__file__).parent.parent / "store" / "migrations" / "039_household.sql").read_text(
    encoding="utf-8")


# ─────────────────────────────── vues stockées ───────────────────────────────


@dataclass(frozen=True)
class CallRecord:
    """Un `LLMCall` tel qu'il sort du ledger (les deux backends renvoient ce type)."""

    thread_id: str
    turn_index: int
    tier: str
    model: str
    reasoning: bool
    prefix_sha256: str
    layer_tokens: dict[str, int]
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    cached_reported: bool
    latency_s: float
    route_reason: str
    chf: float
    cost_source: str  # router | catalog | unknown
    created_at: datetime
    reusable_tokens: int | None = None  # LLMCall.reusable_tokens (préfixe identique à l'appel précédent du fil)
    # Projet d'équipe de l'appel (`nokker/ledger/attribution.py`) : None hors projet.
    project_id: str | None = None
    project_tenant: str | None = None
    account: str | None = None
    # Foyer de l'appel (quota commun, lot X23) : None hors foyer.
    household_id: str | None = None
    household_tenant: str | None = None

    @property
    def new_tokens(self) -> int:
        return max(self.prompt_tokens - self.cached_tokens, 0)

    @property
    def transported_tokens(self) -> int:
        """A3 : tokens TRANSPORTÉS par appel = somme des couches si connue, sinon prompt total."""
        return sum(self.layer_tokens.values()) if self.layer_tokens else self.prompt_tokens


@dataclass(frozen=True)
class VerdictRecord:
    thread_id: str
    turn_index: int
    gate: str
    decision: str
    layer: int
    rule: str
    reason: str
    probability: float | None
    judge_model: str | None
    latency_s: float
    subject: str | None
    created_at: datetime


# ─────────────────────────────── agrégats ───────────────────────────────


@dataclass
class TierStats:
    n_calls: int = 0
    chf_total: float = 0.0
    tokens_transported_median: float | None = None
    cache_rate: float | None = None


@dataclass
class Aggregates:
    """Agrégats PLAN-P1 : A3 (tokens transportés), A5 (taux de cache), T5 (coût médian par tâche)."""

    n_calls: int = 0
    n_turns: int = 0
    tokens_transported_median: float | None = None
    cache_rate: float | None = None  # None = « non mesuré » (A5) : aucun appel n'a cached_reported=True
    cache_measured_calls: int = 0
    chf_total: float = 0.0
    chf_median_per_turn: float | None = None  # T5 : coût médian par tour/tâche
    latency_median_s: float | None = None
    by_tier: dict[str, TierStats] = field(default_factory=dict)
    verdicts_by_decision: dict[str, int] = field(default_factory=dict)


def cost_for_call(call: LLMCall, prices: PriceCatalog) -> tuple[float, str]:
    """(chf, source). Voir la priorité dans le docstring du module."""
    if call.chf is not None:
        return call.chf, "router"
    new_tokens = max(call.usage.prompt - call.usage.cached, 0)
    chf = prices.cost_chf(call.model, new_tokens, call.usage.cached, call.usage.completion)
    if chf is None:
        return 0.0, "unknown"
    return chf, "catalog"


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def build_aggregates(calls: list[CallRecord], verdicts: list[VerdictRecord]) -> Aggregates:
    agg = Aggregates()
    agg.n_calls = len(calls)
    agg.n_turns = len({(c.thread_id, c.turn_index) for c in calls})
    agg.chf_total = sum(c.chf for c in calls)
    agg.tokens_transported_median = _median([float(c.transported_tokens) for c in calls])
    agg.latency_median_s = _median([c.latency_s for c in calls])

    measured = [c for c in calls if c.cached_reported]
    agg.cache_measured_calls = len(measured)
    if measured:
        total_in = sum(c.prompt_tokens for c in measured)
        agg.cache_rate = (sum(c.cached_tokens for c in measured) / total_in) if total_in else 0.0
    else:
        agg.cache_rate = None  # jamais déduit à 0 % (A5)

    chf_per_turn: dict[tuple[str, int], float] = defaultdict(float)
    for c in calls:
        chf_per_turn[(c.thread_id, c.turn_index)] += c.chf
    agg.chf_median_per_turn = _median(list(chf_per_turn.values()))

    by_tier_calls: dict[str, list[CallRecord]] = defaultdict(list)
    for c in calls:
        by_tier_calls[c.tier].append(c)
    for tier, tcalls in by_tier_calls.items():
        tmeasured = [c for c in tcalls if c.cached_reported]
        tin = sum(c.prompt_tokens for c in tmeasured)
        agg.by_tier[tier] = TierStats(
            n_calls=len(tcalls),
            chf_total=sum(c.chf for c in tcalls),
            tokens_transported_median=_median([float(c.transported_tokens) for c in tcalls]),
            cache_rate=(sum(c.cached_tokens for c in tmeasured) / tin) if tmeasured and tin else None,
        )

    counts: dict[str, int] = defaultdict(int)
    for v in verdicts:
        counts[v.decision] += 1
    agg.verdicts_by_decision = dict(counts)
    return agg


class LedgerBackend(Protocol):
    """Sur-ensemble du port `Ledger` (record/record_verdict) utilisé par cli.py, metrics.py, reconcile.py."""

    def record(self, thread_id: str, turn_index: int, call: LLMCall) -> None: ...

    def record_verdict(self, thread_id: str, turn_index: int, verdict: Verdict) -> None: ...

    def fetch_calls(self, *, thread_id: str | None = None, turn_index: int | None = None,
                     day: date | None = None, tier: str | None = None,
                     include_subagents: bool = False) -> list[CallRecord]: ...
    # `include_subagents` (X2) : avec `thread_id`, ajoute les fils de sous-agents `<thread_id>/sa-…` (coût de session).

    def fetch_verdicts(self, *, thread_id: str | None = None, turn_index: int | None = None,
                        day: date | None = None) -> list[VerdictRecord]: ...

    def aggregates(self, *, thread_id: str | None = None, turn_index: int | None = None,
                    day: date | None = None, tier: str | None = None) -> Aggregates: ...


class _AggregatorMixin:
    """`aggregates()` partagé : construit sur `fetch_calls`/`fetch_verdicts` de chaque backend."""

    def aggregates(self, *, thread_id: str | None = None, turn_index: int | None = None,
                    day: date | None = None, tier: str | None = None) -> Aggregates:
        calls = self.fetch_calls(thread_id=thread_id, turn_index=turn_index, day=day, tier=tier)  # type: ignore[attr-defined]
        verdicts = self.fetch_verdicts(thread_id=thread_id, turn_index=turn_index, day=day)  # type: ignore[attr-defined]
        return build_aggregates(calls, verdicts)


# ─────────────────────────────── backend mémoire ───────────────────────────────


class InMemoryLedger(_AggregatorMixin):
    """Backend de test / noyau sans Postgres. Même comportement que `PostgresLedger`."""

    def __init__(self, prices: PriceCatalog | None = None, *, clock: Any = None) -> None:
        self.prices = prices if prices is not None else load_prices()
        self.clock = clock if clock is not None else (lambda: datetime.now(timezone.utc))
        self._calls: list[CallRecord] = []
        self._verdicts: list[VerdictRecord] = []

    def record(self, thread_id: str, turn_index: int, call: LLMCall) -> None:
        chf, source = cost_for_call(call, self.prices)
        tag = current_tag()
        self._calls.append(CallRecord(
            thread_id=thread_id, turn_index=turn_index, tier=call.tier.value, model=call.model,
            reasoning=call.reasoning, prefix_sha256=call.prefix_sha256, layer_tokens=dict(call.layer_tokens),
            prompt_tokens=call.usage.prompt, cached_tokens=call.usage.cached,
            completion_tokens=call.usage.completion, reasoning_tokens=call.usage.reasoning,
            cached_reported=call.usage.cached_reported, latency_s=call.latency_s,
            route_reason=call.route_reason, chf=chf, cost_source=source,
            created_at=self.clock(), reusable_tokens=call.reusable_tokens,
            project_id=tag.project_id if tag else None, project_tenant=tag.project_tenant if tag else None,
            account=tag.account if tag else None, household_id=tag.household_id if tag else None,
            household_tenant=tag.household_tenant if tag else None,
        ))

    def record_verdict(self, thread_id: str, turn_index: int, verdict: Verdict) -> None:
        self._verdicts.append(VerdictRecord(
            thread_id=thread_id, turn_index=turn_index, gate=verdict.gate.value, decision=verdict.decision.value,
            layer=verdict.layer, rule=verdict.rule, reason=verdict.reason, probability=verdict.probability,
            judge_model=verdict.judge_model, latency_s=verdict.latency_s, subject=verdict.subject,
            created_at=datetime.now(timezone.utc),
        ))

    def fetch_calls(self, *, thread_id: str | None = None, turn_index: int | None = None,
                     day: date | None = None, tier: str | None = None,
                     include_subagents: bool = False) -> list[CallRecord]:
        def same_thread(t: str) -> bool:
            return t == thread_id or (include_subagents and t.startswith(f"{thread_id}/sa-"))

        return [c for c in self._calls
                if (thread_id is None or same_thread(c.thread_id))
                and (turn_index is None or c.turn_index == turn_index)
                and (day is None or c.created_at.astimezone(timezone.utc).date() == day)
                and (tier is None or c.tier == tier)]

    def fetch_project_calls(self, project_id: str, project_tenant: str, *, since: datetime,
                            actor: str) -> list[CallRecord]:
        """Appels attribués au projet depuis `since`, tous comptes et locataires confondus (la vue du budget ; le
        service a vérifié que `actor` est membre, la RLS le refait en Postgres)."""
        return [c for c in self._calls if c.project_id == project_id and c.project_tenant == project_tenant
                and c.created_at >= since]

    def fetch_household_calls(self, household_id: str, household_tenant: str, *, since: datetime,
                              actor: str) -> list[CallRecord]:
        """Appels attribués au foyer depuis `since`, tous membres confondus (quota commun ; le service a vérifié que
        `actor` est membre, la RLS le refait en Postgres)."""
        return [c for c in self._calls if c.household_id == household_id and c.household_tenant == household_tenant
                and c.created_at >= since]

    def fetch_verdicts(self, *, thread_id: str | None = None, turn_index: int | None = None,
                        day: date | None = None) -> list[VerdictRecord]:
        return [v for v in self._verdicts
                if (thread_id is None or v.thread_id == thread_id)
                and (turn_index is None or v.turn_index == turn_index)
                and (day is None or v.created_at.astimezone(timezone.utc).date() == day)]


# ─────────────────────────────── backend Postgres ───────────────────────────────

_CALL_COLS = ("thread_id, turn_index, tier, model, reasoning, prefix_sha256, layer_tokens, prompt_tokens, "
              "cached_tokens, completion_tokens, reasoning_tokens, cached_reported, latency_s, route_reason, "
              "chf, cost_source, created_at, reusable_tokens, project_id, project_tenant, account, household_id, "
              "household_tenant")


def _call_record(r: Any) -> CallRecord:
    return CallRecord(
        thread_id=r[0], turn_index=r[1], tier=r[2], model=r[3], reasoning=r[4], prefix_sha256=r[5],
        layer_tokens=r[6] or {}, prompt_tokens=r[7], cached_tokens=r[8], completion_tokens=r[9],
        reasoning_tokens=r[10], cached_reported=r[11], latency_s=r[12], route_reason=r[13], chf=r[14],
        cost_source=r[15], created_at=r[16], reusable_tokens=r[17], project_id=r[18], project_tenant=r[19],
        account=r[20], household_id=r[21], household_tenant=r[22],
    )



class PostgresLedger(_AggregatorMixin):
    """psycopg 3. Tables propres (`ledger_entries`/`ledger_verdicts`), indépendantes du store de F2."""

    def __init__(self, dsn: str, prices: PriceCatalog | None = None, *, migrate: bool = True) -> None:
        from nokker.store import conn  # import local : psycopg n'est nécessaire qu'avec ce backend

        self._pg = conn
        self.dsn = dsn
        self.prices = prices if prices is not None else load_prices()
        if migrate:
            self.migrate()

    def _connect(self) -> Any:
        return self._pg.connect(self.dsn, autocommit=True)  # app.tenant_id = locataire courant (NoTenant hors scope)

    def migrate(self) -> None:
        # en propriétaire, puis la RLS (nokker/store/conn.py) ; colonnes du projet ensuite (037, gardée)
        self._pg.migrate(self.dsn, MIGRATION_SQL, PROJECT_SQL, HOUSEHOLD_SQL)

    def record(self, thread_id: str, turn_index: int, call: LLMCall) -> None:
        chf, source = cost_for_call(call, self.prices)
        tag = current_tag()
        with self._connect() as conn:
            if tag is not None:  # la politique `team_spend_tag` (037) vérifie que CE compte est membre du projet
                conn.execute("SELECT set_config('app.account_id', %s, false)", (tag.account,))
            conn.execute(
                "INSERT INTO ledger_entries (thread_id, turn_index, tier, model, reasoning, prefix_sha256, "
                "layer_tokens, prompt_tokens, cached_tokens, completion_tokens, reasoning_tokens, "
                "cached_reported, latency_s, route_reason, chf, cost_source, reusable_tokens, project_id, "
                "project_tenant, account, household_id, household_tenant) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (thread_id, turn_index, call.tier.value, call.model, call.reasoning, call.prefix_sha256,
                 json.dumps(call.layer_tokens), call.usage.prompt, call.usage.cached, call.usage.completion,
                 call.usage.reasoning, call.usage.cached_reported, call.latency_s, call.route_reason, chf, source,
                 call.reusable_tokens, tag.project_id if tag else None, tag.project_tenant if tag else None,
                 tag.account if tag else None, tag.household_id if tag else None,
                 tag.household_tenant if tag else None),
            )

    def record_verdict(self, thread_id: str, turn_index: int, verdict: Verdict) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO ledger_verdicts (thread_id, turn_index, gate, decision, layer, rule, reason, "
                "probability, judge_model, latency_s, subject) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (thread_id, turn_index, verdict.gate.value, verdict.decision.value, verdict.layer, verdict.rule,
                 verdict.reason, verdict.probability, verdict.judge_model, verdict.latency_s, verdict.subject),
            )

    def fetch_calls(self, *, thread_id: str | None = None, turn_index: int | None = None,
                     day: date | None = None, tier: str | None = None,
                     include_subagents: bool = False) -> list[CallRecord]:
        where, params = ["1=1"], []
        if thread_id is not None and include_subagents:
            # Préfixe comparé par `left()`, pas `LIKE` : un `_` d'identifiant (`th_…`) est un joker pour LIKE.
            child = f"{thread_id}/sa-"
            where.append("(thread_id = %s OR left(thread_id, %s) = %s)")
            params.extend([thread_id, len(child), child])
        elif thread_id is not None:
            where.append("thread_id = %s")
            params.append(thread_id)
        if turn_index is not None:
            where.append("turn_index = %s")
            params.append(turn_index)
        if day is not None:
            where.append("(created_at AT TIME ZONE 'UTC')::date = %s")
            params.append(day)
        if tier is not None:
            where.append("tier = %s")
            params.append(tier)
        # `app.account_id` n'est jamais posé ici : la politique `team_spend_read` (037) reste fermée, les lignes d'un
        # projet d'équipe venues d'autres locataires ne s'y mêlent pas (`fetch_project_calls` les lit, à part).
        sql = f"SELECT {_CALL_COLS} FROM ledger_entries WHERE " + " AND ".join(where)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [_call_record(r) for r in rows]

    def fetch_project_calls(self, project_id: str, project_tenant: str, *, since: datetime,
                            actor: str) -> list[CallRecord]:
        """Appels attribués au projet depuis `since`, tous locataires : `app.account_id` = `actor`, et la politique
        `team_spend_read` (037) n'ouvre ces lignes qu'à un membre du projet, rien d'autre d'un autre locataire."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT set_config('app.account_id', %s, false)", (actor,))
            cur.execute(f"SELECT {_CALL_COLS} FROM ledger_entries WHERE project_id = %s AND project_tenant = %s "
                        "AND created_at >= %s", (project_id, project_tenant, since))
            rows = cur.fetchall()
        return [_call_record(r) for r in rows]

    def fetch_household_calls(self, household_id: str, household_tenant: str, *, since: datetime,
                              actor: str) -> list[CallRecord]:
        """Appels attribués au foyer depuis `since`, tous locataires : `app.account_id` = `actor`, et la politique
        `household_spend_read` (039) n'ouvre ces lignes qu'à un membre du foyer, rien d'autre d'un autre locataire."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT set_config('app.account_id', %s, false)", (actor,))
            cur.execute(f"SELECT {_CALL_COLS} FROM ledger_entries WHERE household_id = %s AND household_tenant = %s "
                        "AND created_at >= %s", (household_id, household_tenant, since))
            rows = cur.fetchall()
        return [_call_record(r) for r in rows]

    def fetch_verdicts(self, *, thread_id: str | None = None, turn_index: int | None = None,
                        day: date | None = None) -> list[VerdictRecord]:
        where, params = ["1=1"], []
        if thread_id is not None:
            where.append("thread_id = %s")
            params.append(thread_id)
        if turn_index is not None:
            where.append("turn_index = %s")
            params.append(turn_index)
        if day is not None:
            where.append("(created_at AT TIME ZONE 'UTC')::date = %s")
            params.append(day)
        sql = ("SELECT thread_id, turn_index, gate, decision, layer, rule, reason, probability, judge_model, "
               "latency_s, subject, created_at FROM ledger_verdicts WHERE " + " AND ".join(where))
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [VerdictRecord(
            thread_id=r[0], turn_index=r[1], gate=r[2], decision=r[3], layer=r[4], rule=r[5], reason=r[6],
            probability=r[7], judge_model=r[8], latency_s=r[9], subject=r[10], created_at=r[11],
        ) for r in rows]


__all__ = [
    "Aggregates", "CallRecord", "InMemoryLedger", "LedgerBackend", "MIGRATION_SQL", "PostgresLedger", "TierStats",
    "VerdictRecord", "build_aggregates", "cost_for_call",
]
