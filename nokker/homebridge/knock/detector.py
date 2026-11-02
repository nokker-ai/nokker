"""Détecteur « toc toc toc » (docs/KNOCK.md §2) : attaques transitoires, rythme de trois coups, rejet du bruit.

Tout se fait sur des grandeurs RELATIVES (émergence au-dessus du plancher de bruit, part de cette émergence gagnée
avant le pic et perdue après, part des aigus) : le détecteur ne dépend ni du gain du micro, ni du timbre de la surface
(bois, verre, porte, bureau, clap).

Chaîne, par pas d'environ 5 ms (indépendant de la fréquence d'échantillonnage) :
1. trois bandes (biquads RBJ : passe-bande 200 Hz, passe-bande 1 kHz, passe-haut 2,5 kHz) + large bande → énergie
   du pas, en dB ;
2. **flux spectral** sur ces bandes : somme pondérée (aigus ×1,5) des hausses de niveau sur 10 ms, demi-onde ;
3. **plancher** du bruit ambiant suivi en continu (descend vite, monte lentement) ;
4. une attaque candidate (flux et émergence suffisants) est jugée 100 ms plus tard (`accepts`) : surgie du calme
   (`attack_min`), impulsive (`decay_min` : le niveau retombe vite), assez de médiums et d'aigus (`hf_min_db`),
   et, si on l'a branché, le petit classifieur (`classifier_min_prob`) ;
5. **rythme** (`RhythmTracker`) : exactement trois coups, intervalles dans [`ioi_min_s`, `ioi_max_s`], réguliers à
   `regularity` près, de niveaux voisins, précédés et suivis de calme, peu d'autres attaques autour (parole,
   musique, vaisselle).

Aucun son n'est conservé : seuls les niveaux des derniers pas (quelques centaines d'octets) vivent en mémoire.
Le même algorithme existe en JavaScript (`web/knock/knock-dsp.js`) ; `tests/unit/knock/test_js_parity.py` vérifie
que les deux trouvent les mêmes déclenchements.
"""
from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from itertools import pairwise
from typing import Any

import numpy as np
from scipy.signal import lfilter

HOP_S = 0.005
_EPS = 1e-12
PRE_HOPS = 12      # 60 ms avant l'attaque (assez court pour ne pas voir la queue du coup précédent)
EVAL_HOPS = 20     # jugement 100 ms après l'attaque
EMERGENCE_CAP_DB = 30.0  # `attack` et `decay` se mesurent en part d'une émergence plafonnée à 30 dB
FLOOR_MIN_DB = -85.0     # plancher minimal (bruit propre d'un micro réel) : pas de « silence numérique » infini


@dataclass
class KnockSettings:
    """Réglages du détecteur (docs/KNOCK.md §3). Défauts choisis sur le banc (bench/knock/README.md)."""

    flux_min_db: float = 20.0       # flux spectral pondéré minimal d'une attaque
    snr_min_db: float = 8.0         # émergence minimale du pic au-dessus du plancher de bruit
    attack_min: float = 0.6         # montée sur les 60 ms qui précèdent, en part de l'émergence (plafonnée à 30 dB)
    decay_min: float = 0.35         # chute au plus bas des 40-100 ms après le pic, même unité
    hf_min_db: float = -20.0        # part des médiums+aigus dans l'énergie du pic (rejette les pas sourds)
    ioi_min_s: float = 0.10         # intervalle minimal entre deux coups
    ioi_max_s: float = 0.80         # intervalle maximal
    regularity: float = 0.35        # |i1 - i2| / max(i1, i2) toléré
    level_spread_db: float = 9.0    # écart maximal de niveau entre les trois coups
    guard_before_s: float = 0.8     # calme exigé avant le premier coup
    guard_after_s: float = 0.6      # calme exigé après le troisième (= latence ajoutée)
    busy_max: int = 4               # autres attaques tolérées autour du motif
    hits: int = 3                   # nombre de coups (3 : décision produit)
    refractory_s: float = 0.06
    classifier_min_prob: float = 0.0  # 0 = classifieur non utilisé

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> KnockSettings:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in names})


@dataclass
class Hit:
    """Une attaque jugée : `accepted` si elle ressemble à un coup ou un clap."""

    t: float
    accepted: bool
    features: dict[str, float] = field(default_factory=dict)


@dataclass
class KnockEvent:
    """Trois coups reconnus. `t` = instant du dernier coup, `fired_at` = instant de la décision (s depuis le début)."""

    t: float
    fired_at: float
    intervals: tuple[float, ...]
    confidence: float
    hits: tuple[Hit, ...]


def _biquad(kind: str, f0: float, q: float, sr: float) -> tuple[np.ndarray, np.ndarray]:
    """Coefficients RBJ (Audio EQ Cookbook) — les mêmes formules qu'en JS, pour la parité."""
    f0 = min(f0, 0.45 * sr)
    w0 = 2 * math.pi * f0 / sr
    alpha = math.sin(w0) / (2 * q)
    cw = math.cos(w0)
    if kind == "bp":
        b = [alpha, 0.0, -alpha]
    elif kind == "hp":
        b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]
    else:
        raise ValueError(kind)
    a = [1 + alpha, -2 * cw, 1 - alpha]
    return np.array(b) / a[0], np.array(a) / a[0]


BANDS = (("bp", 200.0, 0.7, 0.5), ("bp", 1000.0, 0.7, 1.0), ("hp", 2500.0, 0.707, 1.5))


def _db(e: float) -> float:
    return 10.0 * math.log10(e + _EPS)


def accepts(s: KnockSettings, feats: dict[str, float], classifier: Any = None) -> bool:
    """L'attaque ressemble-t-elle à un coup ou à un clap ?"""
    ok = (feats["snr"] >= s.snr_min_db and feats["flux"] >= s.flux_min_db and feats["attack"] >= s.attack_min
          and feats["decay"] >= s.decay_min and feats["hf"] >= s.hf_min_db)
    if ok and classifier is not None and s.classifier_min_prob > 0:
        ok = classifier.prob(feats) >= s.classifier_min_prob
    return ok


class RhythmTracker:
    """Reconnaît le motif dans la suite des attaques jugées. `add(hit)` à chaque attaque, `tick(now)` à chaque pas."""

    def __init__(self, settings: KnockSettings):
        self.settings = settings
        self._hits: list[Hit] = []
        self._others: list[tuple[float, float]] = []  # attaques rejetées : (instant, niveau)

    def add(self, hit: Hit) -> None:
        (self._hits.append(hit) if hit.accepted else self._others.append((hit.t, hit.features["level"])))

    def tick(self, now: float) -> KnockEvent | None:
        s = self.settings
        horizon = now - (s.guard_before_s + s.hits * s.ioi_max_s + s.guard_after_s + 2.0)
        if self._hits and self._hits[0].t < horizon:
            self._hits = [h for h in self._hits if h.t >= horizon]
        if self._others and self._others[0][0] < horizon:
            self._others = [o for o in self._others if o[0] >= horizon]
        if len(self._hits) < s.hits:
            return None
        last = self._hits[-1].t
        if now - last < s.guard_after_s + EVAL_HOPS * HOP_S:
            return None  # attendre le calme après le dernier coup : un coup de plus change le motif
        # Le motif = les coups les plus forts de la salve (à `level_spread_db` du plus fort) ; les attaques acceptées
        # plus faibles (syllabe, tintement lointain) comptent comme bruit autour, pas comme un coup de plus.
        start = last - (s.hits - 1) * s.ioi_max_s - s.guard_before_s
        burst = [h for h in self._hits if h.t >= start]
        self._hits = []  # la salve est décidée maintenant, déclenchée ou non
        top = max(h.features["level"] for h in burst)
        group = [h for h in burst if h.features["level"] >= top - s.level_spread_db]
        quiet = [(h.t, h.features["level"]) for h in burst if h.features["level"] < top - s.level_spread_db]
        if len(group) != s.hits:
            return None  # deux coups, quatre coups ou plus : pas notre signal
        first = group[0].t
        ivs = tuple(round(b.t - a.t, 4) for a, b in pairwise(group))
        if any(i < s.ioi_min_s or i > s.ioi_max_s for i in ivs):
            return None
        spread = (max(ivs) - min(ivs)) / max(ivs) if max(ivs) > 0 else 0.0
        if spread > s.regularity:
            return None
        around = [o for o in self._others + quiet if first - s.guard_before_s <= o[0] <= last + s.guard_after_s]
        if len(around) > s.busy_max:
            return None
        # Une attaque aussi forte que les coups juste avant ou juste après (rejetée par les règles, ex. un pas plus
        # sourd) dit qu'ils font partie d'une série plus longue (marche, batterie) : pas notre signal.
        low = min(h.features["level"] for h in group) - 3.0
        if any(lvl >= low and (o < first or o > last) for o, lvl in around):
            return None
        busy = around
        snr = min(h.features["snr"] for h in group)
        conf = max(0.0, min(1.0, 0.5 * (1.0 - spread) + 0.5 * min(1.0, snr / 30.0) - 0.1 * len(busy)))
        return KnockEvent(t=last, fired_at=now, intervals=ivs, confidence=round(conf, 3), hits=tuple(group))


class KnockDetector:
    """Détecteur en flux : `process(bloc)` rend les `KnockEvent` reconnus dans ce bloc (souvent aucun)."""

    def __init__(self, sample_rate: int, settings: KnockSettings | None = None, classifier: Any = None,
                 on_hit: Callable[[Hit], None] | None = None):
        self.sr = int(sample_rate)
        self.settings = settings or KnockSettings()
        self.classifier = classifier
        self.on_hit = on_hit  # rappel optionnel (calibrage, banc)
        self.hop = max(1, round(self.sr * HOP_S))
        self.hop_s = self.hop / self.sr
        self._filters = [_biquad(k, f, q, self.sr) for k, f, q, _ in BANDS]
        self._weights = [w for *_, w in BANDS]
        self._zi = [np.zeros(2) for _ in BANDS]
        self._rest = np.zeros(0, dtype=np.float64)
        keep = PRE_HOPS + EVAL_HOPS + 4
        self._full: deque[float] = deque(maxlen=keep)         # énergie large bande par pas
        self._bands: deque[list[float]] = deque(maxlen=keep)  # énergie par bande
        self._n = 0
        self._floor: float | None = None
        self._pending: deque[tuple[int, float, float]] = deque()  # (pas, flux, plancher) en attente de jugement
        self._last_cand = -10**9
        self.rhythm = RhythmTracker(self.settings)

    def reset(self) -> None:
        """Oublie tout l'état (au mute, après un déclenchement)."""
        self.__init__(self.sr, self.settings, self.classifier, self.on_hit)

    @property
    def now(self) -> float:
        return self._n * self.hop_s

    def process(self, block: np.ndarray) -> list[KnockEvent]:
        x = np.asarray(block, dtype=np.float64).reshape(-1)
        if self._rest.size:
            x = np.concatenate([self._rest, x])
        n_hops = x.size // self.hop
        used = n_hops * self.hop
        self._rest = x[used:].copy()
        if not n_hops:
            return []
        x = x[:used]
        band_e = []
        for i, (b, a) in enumerate(self._filters):
            y, self._zi[i] = lfilter(b, a, x, zi=self._zi[i])
            band_e.append((y.reshape(n_hops, self.hop) ** 2).mean(axis=1).tolist())
        full_e = (x.reshape(n_hops, self.hop) ** 2).mean(axis=1).tolist()
        events: list[KnockEvent] = []
        for h in range(n_hops):
            ev = self._step(full_e[h], [band_e[0][h], band_e[1][h], band_e[2][h]])
            if ev:
                events.append(ev)
        return events

    def _step(self, e_full: float, e_bands: list[float]) -> KnockEvent | None:
        s = self.settings
        self._full.append(e_full)
        self._bands.append(e_bands)
        n = self._n
        self._n += 1
        lvl = _db(e_full)
        if self._floor is None:
            self._floor = lvl
        # plancher : suit vite vers le bas (0,25 s), lentement vers le haut (3 s)
        tau = 0.25 if lvl < self._floor else 3.0
        self._floor = max(FLOOR_MIN_DB, self._floor + (lvl - self._floor) * (self.hop_s / tau))
        if len(self._bands) >= 3:
            prev = self._bands[-3]
            flux = 0.0
            for w, eb, ep in zip(self._weights, e_bands, prev):
                d = _db(eb) - _db(ep)
                if d > 0:
                    flux += w * d
            if (flux >= s.flux_min_db and lvl - self._floor >= s.snr_min_db
                    and (n - self._last_cand) * self.hop_s >= s.refractory_s):
                self._pending.append((n, flux, self._floor))
                self._last_cand = n
        while self._pending and n - self._pending[0][0] >= EVAL_HOPS:
            self._judge(*self._pending.popleft(), n)
        return self.rhythm.tick(n * self.hop_s)

    def _judge(self, c: int, flux: float, floor: float, n: int) -> None:
        full = self._full
        i0 = len(full) - 1 - (n - c)
        if i0 < 2:
            return
        win = [full[i] for i in range(i0, min(i0 + 5, len(full)))]
        ipk = i0 + int(max(range(len(win)), key=win.__getitem__))
        peak = _db(full[ipk])
        tail = [full[i] for i in range(ipk + 8, min(ipk + 20, len(full)))] or [full[-1]]
        pre = [full[i] for i in range(max(0, i0 - PRE_HOPS), i0 - 2)] or [full[0]]
        bpk = self._bands[ipk]
        snr = peak - floor
        em = min(max(snr, 1e-3), EMERGENCE_CAP_DB)
        feats = {
            "flux": flux,
            "snr": snr,
            "attack": (peak - _db(sum(pre) / len(pre))) / em,
            "decay": (peak - _db(min(tail))) / em,
            "hf": _db((bpk[1] + bpk[2]) / 2) - _db(full[ipk]),
            "level": peak,
        }
        hit = Hit(t=c * self.hop_s, accepted=accepts(self.settings, feats, self.classifier), features=feats)
        if self.on_hit:
            self.on_hit(hit)
        self.rhythm.add(hit)
