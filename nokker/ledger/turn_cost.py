"""Coût par tour et totaux (X2, docs/LOKKAL.md §7.8) : une seule mise en forme pour l'événement `turn.cost`, le
compte rendu de `nokker code --json` et la route `GET /v1/local/cost`.

Règles (les mêmes partout) :
  - jetons par modèle : entrée (`input_tokens`, cache compris), dont en cache (`cached_tokens`), sortie
    (`output_tokens`, raisonnement compris), dont raisonnement (`reasoning_tokens`) ; `cached_tokens` et
    `reasoning_tokens` valent `None` quand AUCUN appel du modèle ne les a déclarés (absent n'est pas zéro, A5) ;
  - coût dans la devise de l'entrée du catalogue (`nokker/ledger/prices.py`, `PriceEntry.currency`) : jamais de taux de
    change inventé, donc un total est un dictionnaire `{devise: montant}` ; un `usage.cost` rendu par le Router
    (`LLMCall.chf`) est pris tel quel, en CHF ;
  - modèle absent du catalogue : jetons seulement (`cost: None`, nom dans `unpriced_models`), jamais un prix inventé.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from nokker.core.turn import LLMCall
from nokker.ledger.prices import PriceCatalog

AMOUNT_DIGITS = 6
SUBAGENT_MARK = "/sa-"


@dataclass(frozen=True)
class UsageRow:
    """Un appel modèle, ramené à ce que le coût lit (depuis un `LLMCall` du tour ou un `CallRecord` du ledger)."""

    model: str
    tier: str
    prompt: int
    cached: int
    cached_reported: bool
    completion: int
    reasoning: int
    reasoning_reported: bool
    router_chf: float | None = None  # `usage.cost` du Router, en CHF ; None = coût calculé par le catalogue


def row_from_call(call: LLMCall) -> UsageRow:
    u = call.usage
    return UsageRow(model=call.model, tier=call.tier.value, prompt=u.prompt, cached=u.cached,
                    cached_reported=u.cached_reported, completion=u.completion, reasoning=u.reasoning,
                    reasoning_reported=getattr(u, "reasoning_reported", False) or u.reasoning > 0,
                    router_chf=call.chf)


def row_from_record(rec: Any) -> UsageRow:
    """`CallRecord` du ledger. Le ledger ne garde pas « raisonnement déclaré » : un compte non nul vaut déclaration,
    un zéro reste inconnu (on n'affiche pas « 0 raisonnement » sur la foi d'une colonne vide)."""
    return UsageRow(model=rec.model, tier=rec.tier, prompt=rec.prompt_tokens, cached=rec.cached_tokens,
                    cached_reported=rec.cached_reported, completion=rec.completion_tokens,
                    reasoning=rec.reasoning_tokens, reasoning_reported=rec.reasoning_tokens > 0,
                    router_chf=rec.chf if rec.cost_source == "router" else None)


def _row_cost(row: UsageRow, prices: PriceCatalog | None) -> tuple[float, str, str] | None:
    """(montant, devise, source) d'un appel, ou None si son modèle n'a pas de prix."""
    if row.router_chf is not None:
        return row.router_chf, "CHF", "router"
    if prices is None:
        return None
    got = prices.cost(row.model, max(row.prompt - row.cached, 0), row.cached, row.completion)
    return None if got is None else (got[0], got[1], "catalog")


def _round(costs: dict[str, float]) -> dict[str, float]:
    return {cur: round(v, AMOUNT_DIGITS) for cur, v in sorted(costs.items())}


def summarize(rows: Iterable[UsageRow], prices: PriceCatalog | None) -> dict[str, Any]:
    """Jetons et coût, par modèle et au total (forme du contrat, docs/LOKKAL.md §7.8)."""
    by_model: dict[str, dict[str, Any]] = {}
    total_cost: dict[str, float] = defaultdict(float)
    n = 0
    for row in rows:
        n += 1
        m = by_model.get(row.model)
        if m is None:
            m = by_model[row.model] = {"model": row.model, "tiers": [], "calls": 0, "input_tokens": 0,
                                       "cached_tokens": None, "output_tokens": 0, "reasoning_tokens": None,
                                       "total_tokens": 0, "_cost": defaultdict(float), "_priced": 0,
                                       "_sources": set()}
        if row.tier not in m["tiers"]:
            m["tiers"].append(row.tier)
        m["calls"] += 1
        m["input_tokens"] += row.prompt
        m["output_tokens"] += row.completion
        m["total_tokens"] += row.prompt + row.completion
        if row.cached_reported:
            m["cached_tokens"] = (m["cached_tokens"] or 0) + row.cached
        if row.reasoning_reported:
            m["reasoning_tokens"] = (m["reasoning_tokens"] or 0) + row.reasoning
        got = _row_cost(row, prices)
        if got is not None:
            amount, currency, source = got
            m["_cost"][currency] += amount
            m["_priced"] += 1
            m["_sources"].add(source)
            total_cost[currency] += amount
    models: list[dict[str, Any]] = []
    unpriced: list[str] = []
    for name in sorted(by_model):
        m = by_model[name]
        cost, priced, sources = m.pop("_cost"), m.pop("_priced"), m.pop("_sources")
        m["cost"] = _round(cost) if priced else None
        # « partial » : certains appels du modèle sans prix (un `usage.cost` du Router sur une partie seulement).
        m["price_source"] = (None if not priced else
                             ("+".join(sorted(sources)) + ("+partial" if priced < m["calls"] else "")))
        if not priced:
            unpriced.append(name)
        models.append(m)

    def _sum(key: str) -> int | None:
        vals = [m[key] for m in models if m[key] is not None]
        return sum(vals) if vals else None

    return {
        "calls": n,
        "input_tokens": sum(m["input_tokens"] for m in models),
        "cached_tokens": _sum("cached_tokens"),
        "output_tokens": sum(m["output_tokens"] for m in models),
        "reasoning_tokens": _sum("reasoning_tokens"),
        "total_tokens": sum(m["total_tokens"] for m in models),
        "cost": _round(total_cost),
        "unpriced_models": unpriced,
        "models": models,
    }


def turn_cost(calls: Iterable[LLMCall], prices: PriceCatalog | None, *,
              subagents: Iterable[UsageRow] = ()) -> dict[str, Any]:
    """Corps de `turn.cost` (sans `thread_id`/`turn_id`) : les appels du tour, plus ceux de ses sous-agents."""
    own = [row_from_call(c) for c in calls]
    children = list(subagents)
    out = summarize(own + children, prices)
    out["subagent_calls"] = len(children)
    return out


def is_subagent_thread(thread_id: str) -> bool:
    return SUBAGENT_MARK in thread_id


def root_thread(thread_id: str) -> str:
    """Fil de la session d'un fil de sous-agent `<fil>/sa-<id>[/sa-<id>…]` (lui-même sinon)."""
    return thread_id.split(SUBAGENT_MARK, 1)[0]


def ledger_totals(ledger: Any, prices: PriceCatalog | None, *, thread_id: str | None = None,
                  day: date | None = None, since: datetime | None = None) -> dict[str, Any]:
    """Totaux du ledger : une session (`thread_id`, ses sous-agents compris) et/ou un jour UTC (`day`). `turns` ne
    compte que les tours des fils de session (un tour de sous-agent fait partie du tour qui l'a lancé)."""
    if thread_id is not None:
        records = ledger.fetch_calls(thread_id=thread_id, day=day, include_subagents=True)
    else:
        records = ledger.fetch_calls(day=day)
    if since is not None:
        records = [r for r in records if r.created_at.astimezone(timezone.utc) >= since]
    out = summarize((row_from_record(r) for r in records), prices)
    out["turns"] = len({(r.thread_id, r.turn_index) for r in records if not is_subagent_thread(r.thread_id)})
    return out


__all__ = ["UsageRow", "is_subagent_thread", "ledger_totals", "root_thread", "row_from_call", "row_from_record",
           "summarize", "turn_cost"]
