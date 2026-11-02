"""Petit classifieur optionnel du heurtoir (docs/KNOCK.md §2.4) : régression logistique sur les mesures d'une attaque,
quelques centaines d'octets de poids (`classifier.json`), CPU. Il départage les coups et claps des autres attaques
impulsives (toux, vaisselle, batterie) APRÈS les règles ; drapeau `classifier_min_prob` (0 = non utilisé).

Entraîné par `bench/knock/bench.py --train-classifier` sur le pli d'entraînement (synthétique graines paires, ESC-50
plis 1-3), mesuré sur l'autre pli. Même calcul en JS (`web/knock/knock-worklet.js`, option `classifier`).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FEATURES = ("snr_c", "attack", "decay", "hf", "flux_n")
DEFAULT_PATH = Path(__file__).with_name("classifier.json")


def vector(f: dict[str, float]) -> list[float]:
    snr = max(f["snr"], 1e-3)
    return [min(snr, 40.0) / 40.0, f["attack"], f["decay"], f["hf"] / 10.0, min(f["flux"] / snr, 10.0) / 10.0]


@dataclass
class LogisticClassifier:
    mean: list[float]
    std: list[float]
    w: list[float]
    b: float

    def prob(self, feats: dict[str, float]) -> float:
        z = self.b
        for x, m, s, w in zip(vector(feats), self.mean, self.std, self.w):
            z += w * (x - m) / (s or 1.0)
        return 1.0 / (1.0 + math.exp(-max(-40.0, min(40.0, z))))

    def to_dict(self) -> dict[str, Any]:
        return {"features": list(FEATURES), "mean": self.mean, "std": self.std, "w": self.w, "b": self.b}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LogisticClassifier:
        if list(d.get("features") or []) != list(FEATURES):
            raise ValueError("classifieur : liste de mesures inattendue")
        return cls(mean=[float(x) for x in d["mean"]], std=[float(x) for x in d["std"]],
                   w=[float(x) for x in d["w"]], b=float(d["b"]))

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PATH) -> LogisticClassifier:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def train(rows: list[dict[str, float]], labels: list[bool], epochs: int = 400, lr: float = 0.5,
          l2: float = 1e-3) -> LogisticClassifier:
    """Descente de gradient (numpy), classes équilibrées par poids."""
    import numpy as np

    X = np.array([vector(r) for r in rows], dtype=np.float64)
    y = np.array(labels, dtype=np.float64)
    mean, std = X.mean(axis=0), X.std(axis=0) + 1e-6
    Z = (X - mean) / std
    pos = max(1.0, y.sum())
    neg = max(1.0, len(y) - y.sum())
    sw = np.where(y > 0, len(y) / (2 * pos), len(y) / (2 * neg))
    w = np.zeros(Z.shape[1])
    b = 0.0
    for _ in range(epochs):
        p = 1 / (1 + np.exp(-(Z @ w + b)))
        g = (p - y) * sw
        w -= lr * (Z.T @ g / len(y) + l2 * w)
        b -= lr * g.mean()
    return LogisticClassifier(mean=[round(float(v), 6) for v in mean], std=[round(float(v), 6) for v in std],
                              w=[round(float(v), 6) for v in w], b=round(float(b), 6))
