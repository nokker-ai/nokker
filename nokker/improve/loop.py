"""Boucle d'évolution (docs/SELF-IMPROVE.md §3), d'après GEPA (arXiv 2507.19457, algorithme 1) et l'archive de la
Darwin Gödel Machine (arXiv 2505.22954) :

1. la base (calque vide) est notée sur tous les bancs ;
2. à chaque pas : un parent est tiré dans le front de Pareto, pondéré par le nombre de tâches où il est le meilleur
   (« Pareto-based candidate sampling » de GEPA : on garde la diversité, pas seulement le meilleur moyen) ; une unité
   (outil, skill, seuils, bloc) est prise à tour de rôle parmi celles qui ont des échecs attribués ; le reflecteur
   propose une nouvelle valeur ; les garde-fous (`guards.check_variant`) la rejettent ou la laissent passer ;
3. la variante passe sur les bancs ; une métrique de sûreté en baisse par rapport à la BASE l'élimine, quel que soit son
   gain ; sinon elle entre dans l'archive, et le front qualité ↑ / coût ↓ est recalculé ;
4. arrêt : budget de la nuit épuisé (EUR, estimation du pas suivant comprise) ou nombre de candidats atteint.

La meilleure variante proposée : dans le front, qualité ≥ base, qualité maximale puis coût minimal. Rien n'est appliqué
ici : la sortie est une `RunResult`, que `proposal.py` transforme en carte.
"""
from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from nokker.improve.benches import BenchResult
from nokker.improve.guards import Corpus, check_variant, safety_regressions
from nokker.improve.reflect import render_value
from nokker.improve.traces import Example


@dataclass
class Scores:
    quality: float
    cost_per_1k_eur: float
    safety: dict[str, float] = field(default_factory=dict)
    per_task: dict[str, float] = field(default_factory=dict)
    by_bench: dict[str, dict[str, Any]] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {"quality": round(self.quality, 4), "cost_per_1k_eur": round(self.cost_per_1k_eur, 6),
                "safety": {k: round(v, 4) for k, v in sorted(self.safety.items())}}


def combine(results: dict[str, BenchResult]) -> Scores:
    qs = [r.quality for r in results.values() if r.quality is not None]
    costs = [r.cost_per_1k_eur for r in results.values() if r.cost_per_1k_eur is not None]
    s = Scores(quality=sum(qs) / len(qs) if qs else 0.0, cost_per_1k_eur=sum(costs))
    for name, r in results.items():
        s.safety.update(r.safety)
        s.per_task.update({f"{name}/{k}": v for k, v in r.per_task.items()})
        s.by_bench[name] = {"quality": r.quality, "cost_per_1k_eur": r.cost_per_1k_eur, "safety": r.safety,
                            **({"detail": r.detail} if r.detail else {})}
    return s


@dataclass
class Candidate:
    id: str
    overlay: dict[str, Any]
    parent: str | None = None
    mutated: str | None = None
    scores: Scores | None = None
    status: str = "base"  # base | archive | rejected | eliminated
    reason: str = ""
    spent_eur: float = 0.0

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["scores"] = self.scores.summary() if self.scores else None
        return d


def dominates(a: Scores, b: Scores) -> bool:
    return (a.quality >= b.quality and a.cost_per_1k_eur <= b.cost_per_1k_eur
            and (a.quality > b.quality or a.cost_per_1k_eur < b.cost_per_1k_eur))


def pareto_front(cands: list[Candidate]) -> list[Candidate]:
    pool = [c for c in cands if c.scores is not None and c.status in ("base", "archive")]
    return [c for c in pool if not any(dominates(o.scores, c.scores) for o in pool if o is not c)]  # type: ignore[arg-type]


def select_parent(front: list[Candidate], rng: random.Random) -> Candidate:
    """GEPA : poids = nombre de tâches où le candidat atteint le meilleur score du front (au moins 1)."""
    tasks = {k for c in front for k in c.scores.per_task}  # type: ignore[union-attr]
    wins = {c.id: 0 for c in front}
    for t in tasks:
        best = max(c.scores.per_task.get(t, 0.0) for c in front)  # type: ignore[union-attr]
        for c in front:
            if c.scores.per_task.get(t, 0.0) >= best:  # type: ignore[union-attr]
                wins[c.id] += 1
    return rng.choices(front, weights=[max(1, wins[c.id]) for c in front], k=1)[0]


@dataclass
class Budget:
    limit_eur: float
    max_candidates: int
    spent_eur: float = 0.0

    def room_for(self, estimate: float) -> bool:
        return self.spent_eur + estimate <= self.limit_eur + 1e-12


@dataclass
class RunResult:
    target: str
    base_values: dict[str, Any]
    candidates: list[Candidate]
    front: list[str]
    best: str | None
    spent_eur: float
    stopped: str
    trace_counts: dict[str, int] = field(default_factory=dict)
    examples: dict[str, int] = field(default_factory=dict)

    def candidate(self, cid: str | None) -> Candidate | None:
        return next((c for c in self.candidates if c.id == cid), None)


def evaluate(benches: list[Any], target: str, overlay: dict[str, Any]) -> tuple[Scores, float]:
    results = {b.name: b.evaluate(target, overlay) for b in benches}
    return combine(results), sum(r.spent_eur for r in results.values())


def run_loop(target: str, base_values: dict[str, Any], examples: dict[str, list[Example]], reflector: Any,
             benches: list[Any], budget: Budget, *, corpus: Corpus | None = None, known: Any = None,
             validate: Callable[[str, Any], Any] | None = None, skill_scan: Callable[[str], str | None] | None = None,
             seed: int = 0, log: Callable[[str], None] = lambda _m: None) -> RunResult:
    rng = random.Random(seed)
    base = Candidate(id="c00", overlay={})
    base.scores, base.spent_eur = evaluate(benches, target, {})
    budget.spent_eur += base.spent_eur
    cands = [base]
    log(f"base : qualité {base.scores.quality:.3f}, coût {base.scores.cost_per_1k_eur:.6f} EUR/1000, "
        f"sûreté {base.scores.summary()['safety']}")
    queue = [k for k, ex in sorted(examples.items(), key=lambda kv: (-len(kv[1]), kv[0])) if kv_ok(ex, base_values, k)]
    stopped = "aucun échec attribué à une unité" if not queue else "nombre de candidats atteint"
    last_cost = base.spent_eur
    step = 0
    while queue and len(cands) - 1 < budget.max_candidates:
        if not budget.room_for(last_cost):
            stopped = f"budget de la nuit atteint ({budget.spent_eur:.4f} / {budget.limit_eur:.4f} EUR)"
            break
        step += 1
        front = pareto_front(cands)
        parent = select_parent(front, rng)
        key = queue[(step - 1) % len(queue)]
        current = parent.overlay.get(key, base_values[key])
        refl = reflector.propose(target, key, current, examples[key], step)
        prices = getattr(reflector, "prices", None)
        r_cost = prices.tokens(refl.prompt_tokens, refl.completion_tokens) if prices else \
            (refl.prompt_tokens * 0.5 + refl.completion_tokens * 1.5) / 1e6
        cid = f"c{len(cands):02d}"
        child = Candidate(id=cid, overlay=dict(parent.overlay), parent=parent.id, mutated=key, status="rejected",
                          spent_eur=r_cost)
        budget.spent_eur += r_cost
        cands.append(child)
        try:
            value = validate(key, refl.value) if validate is not None else refl.value
        except (ValueError, TypeError, KeyError) as e:
            child.reason = f"réponse invalide : {e}"
            log(f"{cid} ← {parent.id} [{key}] rejeté : {child.reason}")
            continue
        if value == current:
            child.reason = "variante identique au parent"
            log(f"{cid} ← {parent.id} [{key}] rejeté : {child.reason}")
            continue
        why = check_variant(render_value(target, value), render_value(target, base_values[key]), corpus=corpus,
                            known=known, skill_scan=skill_scan if target == "skills" else None)
        if why:
            child.reason = why
            log(f"{cid} ← {parent.id} [{key}] rejeté : {why}")
            continue
        child.overlay[key] = value
        child.scores, spent = evaluate(benches, target, child.overlay)
        child.spent_eur += spent
        budget.spent_eur += spent
        last_cost = child.spent_eur
        drops = safety_regressions(base.scores.safety, child.scores.safety)
        if drops:
            child.status, child.reason = "eliminated", "sûreté en baisse : " + " ; ".join(drops)
        else:
            child.status = "archive"
        log(f"{cid} ← {parent.id} [{key}] qualité {child.scores.quality:.3f}, coût "
            f"{child.scores.cost_per_1k_eur:.6f} — {child.status}{' : ' + child.reason if child.reason else ''}")
    front = pareto_front(cands)
    eligible = [c for c in front if c is not base and c.scores.quality >= base.scores.quality  # type: ignore[union-attr]
                and dominates_or_better(c.scores, base.scores)]  # type: ignore[arg-type]
    eligible.sort(key=lambda c: (-c.scores.quality, c.scores.cost_per_1k_eur))  # type: ignore[union-attr]
    return RunResult(target=target, base_values=base_values, candidates=cands, front=[c.id for c in front],
                     best=eligible[0].id if eligible else None, spent_eur=budget.spent_eur, stopped=stopped,
                     examples={k: len(v) for k, v in examples.items()})


def kv_ok(examples: list[Example], base_values: dict[str, Any], key: str) -> bool:
    return bool(examples) and key in base_values


def dominates_or_better(a: Scores, b: Scores) -> bool:
    """Proposable : meilleure qualité, ou même qualité pour moins cher."""
    return a.quality > b.quality or (a.quality == b.quality and a.cost_per_1k_eur < b.cost_per_1k_eur)


__all__ = ["Budget", "Candidate", "RunResult", "Scores", "combine", "dominates", "evaluate", "pareto_front",
           "run_loop", "select_parent"]
