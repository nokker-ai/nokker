"""Détecteur « toc toc toc » v2 (docs/KNOCK.md §2) : mel-spectrogramme, séparation percussive par médianes,
seuil par bande relatif au bruit récent, petit réseau convolutif par attaque, puis le même rythme qu'en v1.

Chaîne, par trame de 5 ms (fenêtre de Hann de 25 ms, indépendante de la fréquence d'échantillonnage) :
1. **mel** : 32 bandes de 100 Hz à 7,5 kHz, en dB ;
2. **fond par bande** : médiane glissante des 400 ms passées de CHAQUE bande (recalculée toutes les 8 trames). C'est
   le « harmonique » d'une séparation harmonique/percussive par filtrage médian (HPSS, Fitzgerald 2010) en version
   causale : une voix, une note, la TV y entrent vite ; un coup n'y entre pas ;
3. **percussivité** : pour chaque trame, la hausse de chaque bande au-dessus de son fond, puis la MÉDIANE sur les
   bandes (le filtrage médian « vertical » de la HPSS) : un coup monte dans toutes les bandes à la fois, une syllabe
   ou une note dans quelques-unes. Seuil par bande relatif au bruit récent = pas de plancher global ;
4. **attaque candidate** : pic local de percussivité ≥ `odf_min_db` ;
5. **réseau** (`KnockCNN`, ~4 k paramètres) sur le patch de 100 ms autour de l'attaque (20 trames × 32 bandes,
   relatif au fond de chaque bande) : probabilité « coup ou clap » ;
6. **rythme** (`RhythmV2`) : le meilleur triplet de coups acceptés de la salve (intervalles permis et réguliers,
   niveaux voisins), qui ne se prolonge pas en série (pas de 4e coup semblable dans le même tempo).

Même algorithme en JS (`web/knock/knock-worklet.js`), parité vérifiée par `tests/unit/knock/test_js_parity.py`.
Aucun son n'est conservé : ~100 trames de 32 niveaux (≈ 13 ko) vivent en mémoire.
"""
from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from itertools import combinations
from pathlib import Path
from typing import Any

import numpy as np

from nokker.homebridge.knock.detector import Hit, KnockEvent

HOP_S = 0.005
WIN_S = 0.025
N_MELS = 32
FMIN, FMAX = 100.0, 7500.0
BG_FRAMES = 80        # 400 ms de fond par bande
BG_EVERY = 8          # fond recalculé toutes les 8 trames (40 ms)
PRE, POST = 4, 15     # patch : 4 trames avant l'attaque, 15 après (100 ms en tout)
PATCH = PRE + 1 + POST
WARMUP = 40          # 200 ms : le fond se forme avant toute attaque
SERIES_SIM = 0.95     # ressemblance spectrale d'un 4e coup pour qu'il fasse une série
MERGE_S = 0.12       # deux attaques acceptées à moins de 120 ms = un seul coup
CHUNK = 16           # trames traitées d'un coup (vectorisé), << taille de l'anneau
DB_EPS = 1e-10
CNN_PATH = Path(__file__).with_name("cnn.json")
TRIPLE_PATH = Path(__file__).with_name("triple.json")


@dataclass
class KnockSettingsV2:
    odf_min_db: float = 4.0          # percussivité minimale d'une attaque (médiane des hausses par bande)
    prob_min: float = 0.3            # probabilité minimale du réseau pour entrer dans un triplet
    refractory_s: float = 0.03
    ioi_min_s: float = 0.10
    ioi_max_s: float = 0.80
    regularity: float = 0.45
    triple_min: float = 0.99         # score minimal du meilleur triplet (juge des triplets ; sans juge : prob. moyenne)
    level_spread_db: float = 12.0
    guard_before_s: float = 0.8
    guard_after_s: float = 0.6
    hits: int = 3

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> KnockSettingsV2:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in names})


def lowpass(fc: float, sr: float) -> tuple[np.ndarray, np.ndarray]:
    """Biquad passe-bas RBJ (Q = 0,707), forme directe II transposée (= scipy.signal.lfilter, et le JS)."""
    w0 = 2 * math.pi * fc / sr
    alpha = math.sin(w0) / (2 * 0.7071067811865476)
    cw = math.cos(w0)
    b = [(1 - cw) / 2, 1 - cw, (1 - cw) / 2]
    a = [1 + alpha, -2 * cw, 1 - alpha]
    return np.array(b) / a[0], np.array(a) / a[0]


def frame_params(sr: float) -> tuple[int, int, int]:
    hop = max(1, math.floor(sr * HOP_S + 0.5))  # arrondi « au plus proche, .5 vers le haut » (= Math.round du JS)
    win = max(8, math.floor(sr * WIN_S + 0.5))
    nfft = 1 << (win - 1).bit_length()
    return hop, win, nfft


def hann(n: int) -> np.ndarray:
    i = np.arange(n)
    return 0.5 - 0.5 * np.cos(2 * math.pi * i / (n - 1))


def _hz_to_mel(f: float) -> float:
    return 2595.0 * math.log10(1.0 + f / 700.0)


def _mel_to_hz(m: float) -> float:
    return 700.0 * (10 ** (m / 2595.0) - 1.0)


def mel_filterbank(sr: int, nfft: int) -> np.ndarray:
    """Triangles HTK sur les fréquences des bins (k·sr/nfft). Même construction en JS."""
    fmax = min(FMAX, sr / 2 * 0.95)
    lo, hi = _hz_to_mel(FMIN), _hz_to_mel(fmax)
    pts = [_mel_to_hz(lo + (hi - lo) * i / (N_MELS + 1)) for i in range(N_MELS + 2)]
    nb = nfft // 2 + 1
    fb = np.zeros((N_MELS, nb))
    for m in range(N_MELS):
        a, c, b = pts[m], pts[m + 1], pts[m + 2]
        for k in range(nb):
            f = k * sr / nfft
            if a < f < b:
                fb[m, k] = (f - a) / (c - a) if f <= c else (b - f) / (b - c)
    return fb


# ------------------------------------------------------------------------------------------------- rythme ----
N_TRIPLE_FEATURES = 14


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = float(np.sqrt(np.dot(a, a))), float(np.sqrt(np.dot(b, b)))
    return float(np.dot(a, b)) / (na * nb) if na > 0 and nb > 0 else 0.0


def triple_features(tri: tuple, burst: list, window: list, ioi_mean: float, spread: float, ext: bool) -> list[float]:
    """Mesures d'un triplet candidat (docs/KNOCK.md §2.5), toutes relatives : probabilités du réseau, percussivité,
    écarts de niveau et de tempo, ressemblance spectrale des trois coups, et ce qui les entoure (autres attaques
    acceptées, densité d'attaques, la plus forte d'entre elles). `tri`/`burst` : `Hit` acceptés ; `window` : toutes
    les attaques (acceptées ou non) autour du triplet. Même calcul en JS."""
    pr = [h.features["prob"] for h in tri]
    lv = [h.features["level"] for h in tri]
    sigs = [h.features["sig"] for h in tri]
    sims = [_cos(sigs[0], sigs[1]), _cos(sigs[1], sigs[2]), _cos(sigs[0], sigs[2])]
    others = [h for h in burst if all(h is not t for t in tri)]
    t0, t2 = tri[0].t, tri[2].t
    span = (t2 - t0) + 1.6
    around = [w for w in window if all(abs(w[0] - t.t) > 1e-9 for t in tri)]
    top_other = max((w[2] for w in around), default=None)
    mean_lv = sum(lv) / 3
    return [
        min(pr), sum(pr) / 3,
        min(3.0, min(h.features["odf"] for h in tri) / 20.0),
        (max(lv) - min(lv)) / 10.0,
        spread,
        ioi_mean,
        min(sims), sum(sims) / 3,
        min(2.0, len(others) / 5.0),
        min(4.0, len(window) / span / 5.0),
        max((h.features["prob"] for h in others), default=0.0),
        max((_cos(h.features["sig"], sg) for h in others for sg in sigs), default=0.0),
        1.0 if ext else 0.0,
        3.0 if top_other is None else max(-3.0, min(3.0, (mean_lv - top_other) / 10.0)),
    ]


@dataclass
class TripleScorer:
    """Petit perceptron (14 → 16 → 1) qui juge un triplet : appris sur les triplets que le rythme trouve dans les
    fonds du pli dev (faux) et sur les vrais motifs posés (vrais). `triple.json`, quelques ko. Même calcul en JS."""

    mean: np.ndarray
    std: np.ndarray
    w1: np.ndarray
    b1: np.ndarray
    w2: np.ndarray
    b2: float

    @classmethod
    def load(cls, path: str | Path) -> TripleScorer:
        d = json.loads(Path(path).read_text(encoding="utf-8"))["weights"]
        return cls(**{k: (np.array(v, dtype=np.float64) if k != "b2" else float(v)) for k, v in d.items()})

    def score(self, f: list[float]) -> float:
        x = (np.array(f) - self.mean) / np.where(self.std > 0, self.std, 1.0)
        h = np.maximum(self.w1 @ x + self.b1, 0.0)
        z = float(self.w2 @ h + self.b2)
        return 1.0 / (1.0 + math.exp(-max(-40.0, min(40.0, z))))


class RhythmV2:
    """Rythme de la v2. Quand le calme revient après une salve d'attaques acceptées, énumère les triplets possibles
    (intervalles dans [`ioi_min_s`, `ioi_max_s`], réguliers à `regularity`, niveaux à `level_spread_db`), les juge
    (`TripleScorer` si présent, sinon la probabilité moyenne du réseau, sans quatrième coup qui prolonge le tempo) et
    déclenche si le meilleur atteint `triple_min`. Même algorithme en JS."""

    def __init__(self, settings: KnockSettingsV2, scorer: TripleScorer | None = None,
                 on_triple: Callable[[tuple, list[float]], None] | None = None):
        self.settings = settings
        self.scorer = scorer
        self.on_triple = on_triple  # banc / entraînement : chaque triplet jugé et ses mesures
        self._hits: list[Hit] = []
        self._next = 0  # premier coup accepté pas encore « fermé »
        self._all: list[tuple[float, float, float]] = []  # (t, prob, niveau) de TOUTES les attaques

    def add(self, hit: Hit) -> None:
        self._all.append((hit.t, hit.features["prob"], hit.features["level"]))
        if hit.accepted:
            self._hits.append(hit)

    def tick(self, now: float) -> KnockEvent | None:
        """Chaque coup accepté « se ferme » `guard_after_s` + 0,175 s après lui (rien ne peut plus le prolonger) : on
        juge alors les triplets qui finissent sur LUI, avec ce qui l'entoure. Pas besoin d'un silence global (une TV
        ne s'arrête jamais)."""
        s = self.settings
        horizon = now - (s.guard_before_s + s.hits * s.ioi_max_s + s.guard_after_s + 2.0)
        if self._all and self._all[0][0] < horizon:
            self._all = [w for w in self._all if w[0] >= horizon]
        if self._hits and self._hits[0].t < horizon:
            keep = [h for h in self._hits if h.t >= horizon]
            self._next = max(0, self._next - (len(self._hits) - len(keep)))
            self._hits = keep
        closed = now - (s.guard_after_s + 0.1 + POST * HOP_S)  # + le retard du jugement d'une attaque (75 ms)
        out = None
        while self._next < len(self._hits) and self._hits[self._next].t <= closed:
            k = self._next
            self._next += 1
            ev = self._judge_last(k, now)
            if ev is not None:
                out = ev
                # les coups du motif (et ceux d'avant) ne servent plus : pas de second déclenchement sur eux
                self._hits = self._hits[k + 1:]
                self._next = 0
        return out

    def _judge_last(self, k: int, now: float) -> KnockEvent | None:
        s = self.settings
        last = self._hits[k]
        start = last.t - (s.hits - 1) * s.ioi_max_s - s.guard_before_s
        burst = [h for h in self._hits if start <= h.t <= last.t + s.guard_after_s + 0.1]
        prior = [h for h in burst if h.t < last.t]
        best = None
        for a, b in combinations(prior, 2):
            tri = (a, b, last)
            ivs = (round(tri[1].t - tri[0].t, 4), round(tri[2].t - tri[1].t, 4))
            if min(ivs) < s.ioi_min_s or max(ivs) > s.ioi_max_s:
                continue
            spread = (max(ivs) - min(ivs)) / max(ivs)
            if spread > s.regularity:
                continue
            lv = [h.features["level"] for h in tri]
            if max(lv) - min(lv) > s.level_spread_db:
                continue
            ioi = (ivs[0] + ivs[1]) / 2
            mean_lv = sum(lv) / 3
            # un 4e coup qui prolonge le tempo, de même niveau ET de même son (pas, batterie, planche à découper) ;
            # une syllabe ou un tintement quelconque tombé là par hasard ne compte pas
            ext = any(all(h is not t for t in tri) and abs(h.features["level"] - mean_lv) <= s.level_spread_db
                      and (abs(h.t - (tri[0].t - ioi)) < 0.35 * ioi or abs(h.t - (tri[2].t + ioi)) < 0.35 * ioi)
                      and max(_cos(h.features["sig"], t.features["sig"]) for t in tri) >= SERIES_SIM
                      for h in burst)
            if ext:
                continue  # un quatrième coup semblable dans le même tempo : une série (pas, batterie), pas « toc toc toc »
            if self.scorer is None:
                score = sum(h.features["prob"] for h in tri) / 3
            else:
                window = [w for w in self._all if tri[0].t - 1.0 <= w[0] <= tri[2].t + 0.6]
                f = triple_features(tri, burst, window, ioi, spread, ext)
                score = self.scorer.score(f)
                if self.on_triple:
                    self.on_triple(tri, f)
            if best is None or score > best[0]:
                best = (score, tri, ivs)
        if best is None or best[0] < s.triple_min:
            return None
        score, tri, ivs = best
        return KnockEvent(t=tri[2].t, fired_at=now, intervals=ivs, confidence=round(score, 3), hits=tuple(tri))


# ------------------------------------------------------------------------------------------------- réseau ----
@dataclass
class KnockCNN:
    """conv3×3(1→c1) ReLU, max 2×2, conv3×3(c1→c2) ReLU, max 2×2, conv3×3(c2→c3) ReLU, moyenne globale, dense
    c3→1 (canaux lus dans les poids : 16, 32, 32 aujourd'hui). Convolutions « same » (bords à zéro). Entrée : patch
    (20 trames × 32 bandes)."""

    w1: np.ndarray
    b1: np.ndarray
    w2: np.ndarray
    b2: np.ndarray
    w3: np.ndarray
    b3: np.ndarray
    wd: np.ndarray
    bd: float

    @classmethod
    def load(cls, path: str | Path = CNN_PATH) -> KnockCNN:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**{k: (np.array(v, dtype=np.float64) if k != "bd" else float(v)) for k, v in d["weights"].items()})

    @staticmethod
    def _conv(x: np.ndarray, w: np.ndarray, b: np.ndarray) -> np.ndarray:
        cin, h, wd = x.shape
        xp = np.zeros((cin, h + 2, wd + 2))
        xp[:, 1:-1, 1:-1] = x
        cols = np.empty((cin, 3, 3, h, wd))
        for i in range(3):
            for j in range(3):
                cols[:, i, j] = xp[:, i:i + h, j:j + wd]
        out = np.tensordot(w, cols, axes=([1, 2, 3], [0, 1, 2])) + b[:, None, None]
        return np.maximum(out, 0.0)

    @staticmethod
    def _pool(x: np.ndarray) -> np.ndarray:
        c, h, w = x.shape
        h2, w2 = h // 2, w // 2
        return x[:, :h2 * 2, :w2 * 2].reshape(c, h2, 2, w2, 2).max(axis=(2, 4))

    def prob(self, patch: np.ndarray) -> float:
        x = patch[None].astype(np.float64)
        x = self._pool(self._conv(x, self.w1, self.b1))
        x = self._pool(self._conv(x, self.w2, self.b2))
        x = self._conv(x, self.w3, self.b3)
        z = float(self.wd @ x.mean(axis=(1, 2)) + self.bd)
        return 1.0 / (1.0 + math.exp(-max(-40.0, min(40.0, z))))


def make_patch(mel_db: np.ndarray, bg: np.ndarray) -> np.ndarray:
    """Patch relatif au fond de chaque bande, borné, à l'échelle du réseau."""
    return np.clip(mel_db - bg[None, :], -20.0, 50.0) / 20.0


# ---------------------------------------------------------------------------------------------- détecteur ----
class KnockDetectorV2:
    """Détecteur en flux : `process(bloc)` rend les `KnockEvent` reconnus dans ce bloc (souvent aucun).
    `on_candidate(hit, patch)` (banc, entraînement) reçoit chaque attaque candidate et son patch."""

    def __init__(self, sample_rate: int, settings: KnockSettingsV2 | None = None, cnn: KnockCNN | None = None,
                 on_hit: Callable[[Hit], None] | None = None,
                 on_candidate: Callable[[Hit, np.ndarray], None] | None = None, use_cnn: bool = True,
                 scorer: TripleScorer | None = None, use_scorer: bool = True,
                 on_triple: Callable[[tuple, list[float]], None] | None = None):
        self.in_sr = int(sample_rate)
        # 32 kHz et plus (navigateur : 44,1 / 48 kHz) : passe-bas (2 biquads) puis 1 échantillon sur `dec`, pour
        # calculer à ~16 kHz quel que soit le micro (le mel s'arrête à 7,5 kHz) ; même chaîne en JS.
        self.dec = max(1, math.floor(self.in_sr / 16000 + 0.5)) if self.in_sr >= 32000 else 1
        self.sr = self.in_sr / self.dec
        self._lp = lowpass(0.42 * self.sr, self.in_sr) if self.dec > 1 else None
        self.settings = settings or KnockSettingsV2()
        self.cnn = cnn if cnn is not None else (KnockCNN.load() if use_cnn and CNN_PATH.exists() else None)
        self.on_hit = on_hit
        self.on_candidate = on_candidate
        self.on_triple = on_triple
        self.scorer = scorer if scorer is not None else (
            TripleScorer.load(TRIPLE_PATH) if use_scorer and TRIPLE_PATH.exists() else None)
        self.hop, self.win, self.nfft = frame_params(self.sr)
        self.hop_s = self.hop / self.sr
        self.window = hann(self.win)
        self.fb = mel_filterbank(self.sr, self.nfft)
        centers = [_mel_to_hz(_hz_to_mel(FMIN) + (_hz_to_mel(min(FMAX, self.sr / 2 * 0.95)) - _hz_to_mel(FMIN))
                              * (m + 1) / (N_MELS + 1)) for m in range(N_MELS)]
        self._hf_bands = np.array([c >= 2000.0 for c in centers])  # bandes au-dessus de 2 kHz (clap ou coup)
        self.reset()

    def reset(self) -> None:
        self._z = [np.zeros(2), np.zeros(2)]
        self._phase = 0
        self._samples = np.zeros(self.win, dtype=np.float64)  # les `win` derniers échantillons (fenêtre glissante)
        self._fill = 0                                          # échantillons reçus depuis la dernière trame
        self._n = 0                                             # trames calculées
        keep = BG_FRAMES + PATCH + 2
        self._mel = np.zeros((keep, N_MELS))                    # anneau des niveaux mel (dB)
        self._odf = np.zeros(keep)
        self._bg_hist = np.zeros((keep, N_MELS))                # fond en vigueur à chaque trame
        self._bg = None
        self._last_cand = -10**9
        self._last_hit_t = -1e9
        self.rhythm = RhythmV2(self.settings, self.scorer, self.on_triple)

    @property
    def now(self) -> float:
        return self._n * self.hop_s

    # -- trames ------------------------------------------------------------------------------------------------
    def _frames(self, x: np.ndarray) -> np.ndarray:
        """Trames complètes de ce bloc : une à chaque `hop` échantillons reçus, sur les `win` derniers."""
        buf = np.concatenate([self._samples, x])
        first = self.hop - self._fill               # fin (dans x) de la première trame complète
        ends = np.arange(first, x.size + 1, self.hop) if x.size >= first else np.zeros(0, dtype=int)
        self._fill = (self._fill + x.size) % self.hop
        self._samples = buf[-self.win:]
        if not ends.size:
            return np.zeros((0, self.win))
        view = np.lib.stride_tricks.sliding_window_view(buf, self.win)
        return view[ends + self.win - self.win]  # trame k = buf[ends_k : ends_k + win] (buf commence win plus tôt)

    def process(self, block: np.ndarray) -> list[KnockEvent]:
        x = np.asarray(block, dtype=np.float64).reshape(-1)
        if self._lp is not None:
            from scipy.signal import lfilter

            b, a = self._lp
            x, self._z[0] = lfilter(b, a, x, zi=self._z[0])
            x, self._z[1] = lfilter(b, a, x, zi=self._z[1])
            start = (-self._phase) % self.dec
            self._phase = (self._phase + x.size) % self.dec
            x = x[start::self.dec]
        frames = self._frames(x)
        if not len(frames):
            return []
        spec = np.fft.rfft(frames * self.window, n=self.nfft, axis=1)
        power = (spec.real ** 2 + spec.imag ** 2) / self.win
        mel_db = 10.0 * np.log10(power @ self.fb.T + DB_EPS)
        events = []
        for i in range(0, len(mel_db), CHUNK):  # l'anneau garde ~100 trames : jamais plus de CHUNK d'avance
            events += self._frames_chunk(mel_db[i:i + CHUNK])
        return events

    def _frames_chunk(self, mel_db: np.ndarray) -> list[KnockEvent]:
        keep = self._mel.shape[0]
        bgs = np.empty_like(mel_db)
        for i, row in enumerate(mel_db):  # le fond ne dépend que des trames PASSÉES : séquentiel, 1 fois sur 4
            n = self._n + i
            if self._bg is None:
                self._bg = row.copy()
            elif n % BG_EVERY == 0 and n > 0:
                m = min(n, BG_FRAMES)
                self._bg = np.median(self._mel[(n - 1 - np.arange(m)) % keep], axis=0)
            self._mel[n % keep] = row
            self._bg_hist[n % keep] = self._bg
            bgs[i] = self._bg
        # percussivité : médiane sur les bandes des hausses au-dessus du fond (tout le bloc d'un coup)
        odf = np.median(np.maximum(mel_db - bgs, 0.0), axis=1)
        events = []
        for i in range(len(mel_db)):
            n = self._n
            self._odf[n % keep] = odf[i]
            self._n += 1
            ev = self._after_frame(n)
            if ev:
                events.append(ev)
        return events

    def _after_frame(self, n: int) -> KnockEvent | None:
        """Attaque candidate : pic local (±2 trames) de percussivité, jugée quand son patch est complet."""
        s = self.settings
        keep = self._mel.shape[0]
        c = n - POST
        if c >= WARMUP:
            o = self._odf
            oc = o[c % keep]
            if (oc >= s.odf_min_db and oc >= o[(c - 1) % keep] and oc >= o[(c - 2) % keep]
                    and oc > o[(c + 1) % keep] and oc > o[(c + 2) % keep]
                    and (c - self._last_cand) * self.hop_s >= s.refractory_s):
                self._last_cand = c
                self._judge(c, float(oc))
        return self.rhythm.tick(self._n * self.hop_s)

    def _judge(self, c: int, odf: float) -> None:
        keep = self._mel.shape[0]
        idx = [(c + d) % keep for d in range(-PRE, POST + 1)]
        bg = self._bg_hist[c % keep]
        patch = make_patch(self._mel[idx], bg)
        prob = self.cnn.prob(patch) if self.cnn is not None else 1.0
        peak = self._mel[[(c + d) % keep for d in range(3)]]
        level = float(10.0 * np.log10(np.sum(10.0 ** (peak.max(axis=0) / 10.0)) + DB_EPS))
        pk = 10.0 ** (peak.max(axis=0) / 10.0)
        hf = float(10.0 * np.log10((pk[self._hf_bands].sum() + DB_EPS) / (pk.sum() + DB_EPS)))
        feats = {"odf": odf, "snr": odf, "prob": prob, "level": level, "hf": hf,
                 "sig": np.clip(peak.max(axis=0) - bg, 0.0, 50.0)}  # signature spectrale du coup (hausse par bande)
        hit = Hit(t=c * self.hop_s, accepted=prob >= self.settings.prob_min, features=feats)
        if self.on_candidate:
            self.on_candidate(hit, patch)
        if self.on_hit:
            self.on_hit(hit)
        if hit.accepted and hit.t - self._last_hit_t < MERGE_S:
            # rebond du même coup (« flam », double impact, réverbération) : un seul coup, le plus fort des deux
            hs = self.rhythm._hits
            if hs and hs[-1].t == self._last_hit_t and level > hs[-1].features["level"]:
                hs[-1] = hit
                self._last_hit_t = hit.t
            return
        if hit.accepted:
            self._last_hit_t = hit.t
        self.rhythm.add(hit)
