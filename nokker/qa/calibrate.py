"""Seuils automatiser / vérifier / escalader par question (PLAN-P1 §5.3 B-2) + mesures de
calibration (ECE, Brier). Réglés sur le banc étiqueté B-1 (dépôt
interne), **jamais** sur le banc caché T2 (hors mandat de cette session, pas déchiffré
ici). Pas d'apprentissage sur le test : les seuils sont choisis par validation croisée (k-fold,
stratifiée par question), et la précision rapportée (B1) est la moyenne HORS-PLI (out-of-fold) —
jamais celle obtenue en réévaluant les mêmes lignes qui ont servi à choisir le seuil.

Primitive **Noul** (Oui/Non) : la probabilité d'alerte `p_alert` est directement `probs[option
d'alerte]` (déduite de `judge.py::probability_from_top_logprobs` + l'approximation à un seul
logprob documentée dans `judge.py`). Deux seuils (`low`, `high`) découpent `[0, 1]` en trois zones :
  - `p_alert <= low`  → **ALLOW** (automatiser le passage) ;
  - `p_alert >= high` → **ESCALATE/HITL/DENY** (automatiser le blocage, selon la porte — décidé par
    `gates.py`, pas ici) ;
  - entre les deux    → zone grise, **vérifier** (REDO_DEEP à la porte bascule, HITL prudent à la
    porte action).

Primitive **Choice/Score** : un seul logprob ne donne que la confiance du token choisi, jamais une
distribution sur les autres lettres/chiffres (cf `judge.py`). `p_alert` réutilise la même
approximation par complément : `p_alert = confiance` si le choix rendu est une option d'alerte,
sinon `1 - confiance` — mêmes deux seuils, même découpage en trois zones.

Température : si au moins 20 questions Noul ont une probabilité mesurée, un scalaire `T` (grid
search grossier, pas de dépendance scipy) minimise l'ECE en recalant `p' = sigmoid(logit(p) / T)` ;
sinon `T = 1.0` (pas de recalage) — jamais déduit sans mesure.
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nokker.qa.questions import QUESTIONS, Primitive

# ─────────────────────────────── chargement des lignes mesurées ───────────────────────────────


@dataclass
class ScoredRow:
    """Une ligne de banc déjà interrogée (sortie de `judge_eval.py --out …json`, champ `rows`)."""

    id: str
    question_id: str
    expected: str  # option attendue
    pred: str | None
    probs: dict[str, float] | None
    label_status: str  # "sur" | "ambigu"
    polarity: str | None  # "fautif" | "juste" | None
    ok: bool | None

    @property
    def is_fautif_expected(self) -> bool:
        q = QUESTIONS.get(self.question_id)
        return bool(q and q.alerts(self.expected))

    def p_alert(self) -> float | None:
        """Même règle que `judge.py::_p_alert` (doit rester identique : les seuils sont ajustés sur
        cette définition). Avec un backend à logprob unique (plafond Router, B-2/F5), `self.probs`
        ne contient souvent QUE `{choix: p}` (une seule clé) — d'où le repli par complément à 1
        dans TOUS les cas (Noul comme Choice/Score) quand l'option d'alerte elle-même n'est pas la
        clé présente : utiliser directement `self.probs.get(alert_opt)` planterait sur `None` à
        chaque fois que le juge a répondu le CÔTÉ SÛR (cas pourtant fréquent et important : c'est
        justement celui qu'on veut classer `ALLOW` avec confiance)."""
        q = QUESTIONS.get(self.question_id)
        if q is None or self.probs is None or self.pred is None:
            return None
        if q.primitive is Primitive.NOUL:
            alert_opt = q.alert[0] if q.alert else None
            if alert_opt is None:
                return None
            direct = self.probs.get(alert_opt)
            if direct is not None:
                return direct
            conf = self.probs.get(self.pred)
            return (1.0 - conf) if conf is not None else None
        conf = self.probs.get(self.pred)
        if conf is None:
            return None
        return conf if q.alerts(self.pred) else 1.0 - conf


def load_rows(results_path: str | Path) -> list[ScoredRow]:
    """Lit un fichier `--out` de `judge_eval.py` (banc B-1). Ignore les lignes sans réponse (`err`)."""
    data = json.loads(Path(results_path).read_text(encoding="utf-8"))
    rows = []
    for r in data.get("rows", []):
        if "err" in r:
            continue
        rows.append(ScoredRow(id=r["id"], question_id=r["question_id"], expected=r["expected"],
                               pred=r.get("pred"), probs=r.get("probs"), label_status=r["label_status"],
                               polarity=r.get("polarity"), ok=r.get("ok")))
    return rows


# ─────────────────────────────── calibration (ECE / Brier) ───────────────────────────────


def ece(pairs: list[tuple[float, bool]], bins: int = 10) -> float | None:
    """Même méthode que `judge_eval.py::ece` — confiance du choix rendu vs correction, par paniers."""
    if not pairs:
        return None
    tot = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        sel = [(p, ok) for p, ok in pairs if lo <= p < hi or (b == bins - 1 and p == 1.0)]
        if sel:
            tot += len(sel) / len(pairs) * abs(sum(p for p, _ in sel) / len(sel) - sum(ok for _, ok in sel) / len(sel))
    return tot


def brier_noul(rows: list[ScoredRow]) -> float | None:
    """Brier sur les questions Noul uniquement (cible binaire bien définie : P(Oui) vs Oui/Non réel)."""
    vals = []
    for r in rows:
        q = QUESTIONS.get(r.question_id)
        if q is None or q.primitive is not Primitive.NOUL or r.probs is None:
            continue
        p_oui = r.probs.get("Oui")
        if p_oui is None:
            continue
        target = 1.0 if r.expected == "Oui" else 0.0
        vals.append((p_oui - target) ** 2)
    return sum(vals) / len(vals) if vals else None


def _sigmoid(x: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def _logit(p: float, eps: float = 1e-6) -> float:
    p = min(max(p, eps), 1 - eps)
    return math.log(p / (1 - p))


def fit_temperature(rows: list[ScoredRow], candidates: tuple[float, ...] = tuple(
        round(0.2 + 0.1 * i, 2) for i in range(29))) -> tuple[float, float | None, int]:
    """Grid search grossier de T minimisant l'ECE sur les questions Noul. Renvoie (T, ECE après
    recalage, n questions utilisées). T=1.0 sans mesure si trop peu de points (< 20)."""
    pairs = []
    for r in rows:
        q = QUESTIONS.get(r.question_id)
        if q is None or q.primitive is not Primitive.NOUL or r.probs is None or r.pred is None:
            continue
        p = r.probs.get(r.pred)
        if p is None:
            continue
        pairs.append((p, bool(r.ok)))
    if len(pairs) < 20:
        return 1.0, None, len(pairs)
    best_t, best_ece = 1.0, ece(pairs)
    for t in candidates:
        rescaled = [(_sigmoid(_logit(p) / t), ok) for p, ok in pairs]
        e = ece(rescaled)
        if e is not None and (best_ece is None or e < best_ece):
            best_t, best_ece = t, e
    return best_t, best_ece, len(pairs)


def apply_temperature(p: float, t: float) -> float:
    return _sigmoid(_logit(p) / t) if t != 1.0 else p


# ─────────────────────────────── seuils par question (validation croisée) ───────────────────────────────

GRID = tuple(round(0.05 * i, 2) for i in range(21))  # 0.00 .. 1.00 par pas de 0.05


@dataclass
class Thresholds:
    question_id: str
    low: float  # p_alert <= low -> ALLOW
    high: float  # p_alert >= high -> bloquer (ESCALATE/HITL/DENY, décidé par gates.py)
    n_fit: int
    cv_accuracy: float | None  # précision hors-pli (None si pas assez de points pour la CV)
    cv_folds: int
    source: str  # "logprob" | "sans-logprobs"


def _decide(p: float, low: float, high: float) -> str:
    if p <= low:
        return "allow"
    if p >= high:
        return "block"
    return "verify"


def _score_split(rows: list[ScoredRow], low: float, high: float) -> float:
    """Score de la décision à 3 zones vs l'étiquette (fautif->block attendu, juste->allow attendu).

    « verify » vaut un DEMI point : c'est une zone sûre (REDO_DEEP / approbation humaine) mais elle a un coût
    (latence, sollicitation de l'humain). Compter « verify » comme juste faisait choisir low=0 à l'optimiseur
    (tout en vérification, exactitude CV affichée 90-100 % sans valeur) — constaté sur le banc B-1 le 17.10.2026."""
    if not rows:
        return 0.0
    ok = 0.0
    for r in rows:
        p = r.p_alert()
        if p is None:
            continue
        d = _decide(p, low, high)
        want_block = r.is_fautif_expected
        if d == "verify":
            ok += 0.5
        elif (d == "block") == want_block:
            ok += 1
    return ok / len(rows)


def verify_rate(rows: list[ScoredRow], low: float, high: float) -> float:
    """Part des décisions envoyées en zone grise (à rapporter à côté du score)."""
    ps = [r.p_alert() for r in rows if r.p_alert() is not None]
    return sum(1 for p in ps if _decide(p, low, high) == "verify") / len(ps) if ps else 0.0


def _kfold(rows: list[ScoredRow], k: int) -> list[list[ScoredRow]]:
    folds: list[list[ScoredRow]] = [[] for _ in range(k)]
    for i, r in enumerate(rows):
        folds[i % k].append(r)
    return folds


def fit_thresholds_for_question(question_id: str, rows: list[ScoredRow], k: int = 5) -> Thresholds:
    """Validation croisée k-fold : pour chaque pli, ajuste (low, high) sur les k-1 autres plis (grille
    grossière, maximise `_score_split`), évalue sur le pli laissé de côté ; la précision rapportée
    (`cv_accuracy`) est la moyenne des précisions HORS-PLI. Les seuils FINAUX publiés sont ajustés sur
    l'ensemble du banc (pas de second banc disponible pour un vrai jeu de test — B-1 est la seule
    source étiquetée ; le banc caché T2 n'est pas déchiffré par ce chantier) : la CV sert à vérifier
    que ce choix généralise (accuracy hors-pli proche de l'accuracy sur tout le banc), pas à le
    remplacer."""
    scored = [r for r in rows if r.question_id == question_id and r.label_status == "sur"]
    has_probs = [r for r in scored if r.p_alert() is not None]
    if len(has_probs) < k * 2:
        # Trop peu de points pour une CV significative : seuils par défaut prudents (zone grise
        # large), pas de précision hors-pli déclarée.
        return Thresholds(question_id, low=0.3, high=0.7, n_fit=len(has_probs), cv_accuracy=None,
                          cv_folds=0, source="logprob" if has_probs else "sans-logprobs")

    folds = _kfold(has_probs, k)
    oof_scores = []
    for i in range(k):
        test_fold = folds[i]
        train_fold = [r for j, f in enumerate(folds) if j != i for r in f]
        best = (0.3, 0.7, -1.0)
        for low in GRID:
            for high in GRID:
                if high < low:
                    continue
                s = _score_split(train_fold, low, high)
                if s > best[2]:
                    best = (low, high, s)
        oof_scores.append(_score_split(test_fold, best[0], best[1]))

    # Seuils finaux : ajustés sur tout le banc disponible pour cette question.
    best_final = (0.3, 0.7, -1.0)
    for low in GRID:
        for high in GRID:
            if high < low:
                continue
            s = _score_split(has_probs, low, high)
            if s > best_final[2]:
                best_final = (low, high, s)

    return Thresholds(question_id, low=best_final[0], high=best_final[1], n_fit=len(has_probs),
                      cv_accuracy=statistics.mean(oof_scores), cv_folds=k, source="logprob")


def fit_all(rows: list[ScoredRow], k: int = 5) -> dict[str, Thresholds]:
    return {qid: fit_thresholds_for_question(qid, rows, k=k) for qid in QUESTIONS}


DEFAULT_THRESHOLDS = {qid: Thresholds(qid, low=0.3, high=0.7, n_fit=0, cv_accuracy=None, cv_folds=0,
                                      source="defaut-prudent") for qid in QUESTIONS}


def save_thresholds_file(path: str | Path, thresholds: dict[str, Thresholds]) -> None:
    data = {qid: {"low": t.low, "high": t.high, "n_fit": t.n_fit, "cv_accuracy": t.cv_accuracy,
                 "cv_folds": t.cv_folds, "source": t.source} for qid, t in thresholds.items()}
    Path(path).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def load_thresholds_file(path: str | Path) -> dict[str, Thresholds]:
    """Lit un fichier produit par `save_thresholds_file` ; les questions absentes du fichier gardent
    le seuil prudent par défaut (0,3 / 0,7) — jamais une question sans décision de seuil."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out = dict(DEFAULT_THRESHOLDS)
    for qid, d in raw.items():
        out[qid] = Thresholds(qid, low=d["low"], high=d["high"], n_fit=d["n_fit"], cv_accuracy=d.get("cv_accuracy"),
                              cv_folds=d.get("cv_folds", 0), source=d.get("source", "fichier"))
    return out


def report(rows: list[ScoredRow], thresholds: dict[str, Thresholds]) -> dict[str, Any]:
    """Rapport pour le README B-2 : précision par porte/question, rappel des fautifs, fausses
    alarmes sur les justes et sur `neutres.jsonl` (polarity=juste par construction côté neutres),
    ECE/Brier globaux."""
    scored = [r for r in rows if r.label_status == "sur" and r.question_id in QUESTIONS]
    by_q: dict[str, dict[str, Any]] = {}
    for qid in QUESTIONS:
        sel = [r for r in scored if r.question_id == qid]
        if not sel:
            continue
        t = thresholds.get(qid)
        faut = [r for r in sel if r.polarity == "fautif"]
        juste = [r for r in sel if r.polarity == "juste"]
        recall_faut = sum(1 for r in faut if r.ok) / len(faut) if faut else None
        recall_juste = sum(1 for r in juste if r.ok) / len(juste) if juste else None
        by_q[qid] = {
            "n": len(sel), "accuracy_model": sum(1 for r in sel if r.ok) / len(sel),
            "recall_fautifs": recall_faut, "faux_positifs_justes": 1 - recall_juste if recall_juste is not None else None,
            "cv_accuracy_gates": t.cv_accuracy if t else None, "low": t.low if t else None, "high": t.high if t else None,
        }
    pairs = [(r.probs[r.pred], bool(r.ok)) for r in scored if r.probs and r.pred and r.pred in r.probs]
    return {
        "n_scored": len(scored), "accuracy": sum(1 for r in scored if r.ok) / len(scored) if scored else None,
        "by_question": by_q, "ece": ece(pairs), "brier_noul": brier_noul(scored),
    }


__all__ = [
    "DEFAULT_THRESHOLDS", "GRID", "ScoredRow", "Thresholds", "apply_temperature", "brier_noul", "ece",
    "fit_all", "fit_temperature", "fit_thresholds_for_question", "load_rows", "load_thresholds_file",
    "report", "save_thresholds_file",
]


def _main() -> None:
    """`python -m nokker.qa.calibrate <rows.json> <thresholds-out.json>` — ajuste les seuils sur un
    fichier `judge_eval.py --out ...` et écrit le fichier de seuils consommé par `gates.py`."""
    import sys

    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <rows.json produit par judge_eval.py --out> <thresholds-out.json>")
        raise SystemExit(2)
    rows = load_rows(sys.argv[1])
    thresholds = fit_all(rows)
    save_thresholds_file(sys.argv[2], thresholds)
    rep = report(rows, thresholds)
    print(json.dumps({k: v for k, v in rep.items() if k != "by_question"}, indent=1, ensure_ascii=False))
    for qid, d in rep["by_question"].items():
        print(f"  {qid:<12} n={d['n']:<4} acc_modele={d['accuracy_model']:.2f} "
              f"cv_gates={d['cv_accuracy_gates']} low={d['low']} high={d['high']}")


if __name__ == "__main__":
    _main()
