"""`/metrics` Prometheus — F6, PLAN-P1 §5.1 (« /metrics … — F2 la montera dans l'API »).

Une seule fonction pure : `render(ledger) -> str`, le texte d'exposition Prometheus. F2 n'a qu'à la
brancher dans `api/server.py` sur une route `GET /metrics` (content-type `text/plain; version=0.0.4`).
Aucune dépendance au client `prometheus_client` : le format texte est simple, et ça évite d'imposer une
dépendance de plus au noyau pour quatre familles de métriques.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from nokker.ledger.costs import LedgerBackend

LATENCY_BUCKETS_S: tuple[float, ...] = (0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 15.0, 30.0, 60.0)


def _escape(label: str) -> str:
    return label.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _histogram_lines(name: str, values: list[float], buckets: tuple[float, ...]) -> list[str]:
    lines = []
    total = len(values)
    for b in buckets:
        cum = sum(1 for v in values if v <= b)
        lines.append(f'{name}_bucket{{le="{b}"}} {cum}')
    lines.append(f'{name}_bucket{{le="+Inf"}} {total}')
    lines.append(f"{name}_sum {sum(values):.6f}")
    lines.append(f"{name}_count {total}")
    return lines


def render(ledger: LedgerBackend) -> str:
    """Texte d'exposition complet, sur la totalité du ledger (F2 filtre en amont si besoin — cette
    fonction ne prend pas de fenêtre de temps : c'est un instantané, pas une agrégation côté serveur)."""
    calls = ledger.fetch_calls()
    verdicts = ledger.fetch_verdicts()
    lines: list[str] = []

    lines.append("# HELP nokker_ledger_calls_total Appels LLM enregistrés par le ledger.")
    lines.append("# TYPE nokker_ledger_calls_total counter")
    by_tier_model: Counter[tuple[str, str]] = Counter((c.tier, c.model) for c in calls)
    for (tier, model), n in sorted(by_tier_model.items()):
        lines.append(f'nokker_ledger_calls_total{{tier="{_escape(tier)}",model="{_escape(model)}"}} {n}')

    lines.append("# HELP nokker_ledger_tokens_total Tokens par palier/modèle/catégorie (neuf, cache, sortie, raisonnement).")
    lines.append("# TYPE nokker_ledger_tokens_total counter")
    tok: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"new": 0, "cached": 0, "completion": 0, "reasoning": 0})
    for c in calls:
        key = (c.tier, c.model)
        tok[key]["new"] += c.new_tokens
        tok[key]["cached"] += c.cached_tokens
        tok[key]["completion"] += c.completion_tokens
        tok[key]["reasoning"] += c.reasoning_tokens
    for (tier, model), d in sorted(tok.items()):
        for kind, v in d.items():
            lines.append(f'nokker_ledger_tokens_total{{tier="{_escape(tier)}",model="{_escape(model)}",kind="{kind}"}} {v}')

    lines.append("# HELP nokker_ledger_chf_total CHF calculés par le ledger, par palier et modèle (T5).")
    lines.append("# TYPE nokker_ledger_chf_total counter")
    chf: dict[tuple[str, str], float] = defaultdict(float)
    for c in calls:
        chf[(c.tier, c.model)] += c.chf
    for (tier, model), v in sorted(chf.items()):
        lines.append(f'nokker_ledger_chf_total{{tier="{_escape(tier)}",model="{_escape(model)}"}} {v:.6f}')

    lines.append("# HELP nokker_ledger_call_latency_seconds Latence d'un appel LLM (du ledger).")
    lines.append("# TYPE nokker_ledger_call_latency_seconds histogram")
    lines.extend(_histogram_lines("nokker_ledger_call_latency_seconds", [c.latency_s for c in calls], LATENCY_BUCKETS_S))

    lines.append("# HELP nokker_ledger_verdicts_total Verdicts par porte et décision (pilier b).")
    lines.append("# TYPE nokker_ledger_verdicts_total counter")
    by_decision: Counter[tuple[str, str]] = Counter((v.gate, v.decision) for v in verdicts)
    for (gate, decision), n in sorted(by_decision.items()):
        lines.append(f'nokker_ledger_verdicts_total{{gate="{_escape(gate)}",decision="{_escape(decision)}"}} {n}')

    lines.append("# HELP nokker_ledger_verdict_latency_seconds Latence d'un verdict (juge, couche 2 surtout).")
    lines.append("# TYPE nokker_ledger_verdict_latency_seconds histogram")
    lines.extend(_histogram_lines("nokker_ledger_verdict_latency_seconds", [v.latency_s for v in verdicts],
                                   LATENCY_BUCKETS_S))

    return "\n".join(lines) + "\n"


__all__ = ["LATENCY_BUCKETS_S", "render"]
