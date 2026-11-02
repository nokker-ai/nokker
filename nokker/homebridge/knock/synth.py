"""Sons synthétiques pour le banc et les tests (docs/KNOCK.md §6) : coups sur plusieurs surfaces, claps, et bruits
ambiants difficiles (parole, musique avec batterie, TV, cuisine, pas, ventilation). Déterministe par graine.

Modèle d'un coup : quelques modes amortis (fréquences et amortissements propres à la surface) + un clic de contact
bref, puis une réverbération de pièce (bruit à décroissance exponentielle). Un clap : salve de bruit filtrée de
quelques ms. Ce ne sont pas des enregistrements : le banc mesure aussi sur des sons réels (ESC-50, bench/knock).
"""
from __future__ import annotations

import numpy as np
from scipy.signal import butter, fftconvolve, lfilter

SURFACES = {
    # (fréquences des modes Hz, amortissements s, gain relatif)
    "bois": ([(180, 420), (700, 1300), (2000, 3600)], (0.012, 0.035), (1.0, 0.6, 0.35)),
    "porte": ([(90, 200), (300, 650), (1100, 1800)], (0.025, 0.06), (1.0, 0.5, 0.25)),
    "verre": ([(1800, 3200), (3800, 5200), (6000, 7400)], (0.03, 0.09), (0.8, 1.0, 0.5)),
    "bureau": ([(250, 600), (1300, 2200), (3000, 4500)], (0.01, 0.03), (1.0, 0.7, 0.4)),
}


def _rng(seed):
    return seed if isinstance(seed, np.random.Generator) else np.random.default_rng(seed)


def _band(x, lo, hi, sr, order=2):
    hi = min(hi, 0.45 * sr)
    b, a = butter(order, [lo / (sr / 2), hi / (sr / 2)], btype="band")
    return lfilter(b, a, x)


def reverb(x, sr, rng, rt60=None, wet=None):
    rng = _rng(rng)
    rt60 = rt60 if rt60 is not None else rng.uniform(0.15, 0.6)
    wet = wet if wet is not None else rng.uniform(0.05, 0.25)
    n = int(sr * rt60)
    t = np.arange(n) / sr
    ir = rng.standard_normal(n) * np.exp(-6.9 * t / rt60)
    ir /= np.sqrt((ir ** 2).sum()) + 1e-12
    y = fftconvolve(x, ir)[: x.size]
    return x + wet * y / (np.abs(y).max() + 1e-12) * np.abs(x).max()


def knock(sr, surface="bois", rng=None, dur=0.35):
    rng = _rng(rng)
    modes, (dmin, dmax), gains = SURFACES[surface]
    n = int(sr * dur)
    t = np.arange(n) / sr
    y = np.zeros(n)
    for (lo, hi), g in zip(modes, gains):
        f = rng.uniform(lo, hi)
        d = rng.uniform(dmin, dmax)
        y += g * np.sin(2 * np.pi * f * t + rng.uniform(0, 6.28)) * np.exp(-t / d)
    click_n = max(2, int(sr * rng.uniform(0.0008, 0.003)))
    click = rng.standard_normal(click_n) * np.exp(-np.arange(click_n) / (click_n / 3))
    y[:click_n] += 0.5 * click
    y *= np.minimum(1.0, t / 0.0005)  # attaque ~0,5 ms
    return y / (np.abs(y).max() + 1e-12)


def clap(sr, rng=None, dur=0.25):
    rng = _rng(rng)
    n = int(sr * dur)
    t = np.arange(n) / sr
    burst = rng.standard_normal(n) * np.exp(-t / rng.uniform(0.006, 0.014))
    y = _band(burst, 700, rng.uniform(3500, 6000), sr)
    return y / (np.abs(y).max() + 1e-12)


def pattern(sr, kind="bois", count=3, ioi=None, jitter=0.12, rng=None, room=True):
    """`count` coups (ou claps) espacés de `ioi` ± `jitter`. Rend (signal, instants des coups en s)."""
    rng = _rng(rng)
    ioi = ioi if ioi is not None else rng.uniform(0.18, 0.55)
    times, t = [], 0.0
    for i in range(count):
        times.append(t)
        t += ioi * (1 + rng.uniform(-jitter, jitter))
    total = int(sr * (times[-1] + 0.5))
    y = np.zeros(total)
    for tt in times:
        s = clap(sr, rng) if kind == "clap" else knock(sr, kind, rng)
        s *= rng.uniform(0.75, 1.0)
        i = int(tt * sr)
        m = min(s.size, total - i)
        y[i:i + m] += s[:m]
    if room:
        y = reverb(y, sr, rng)
    return y / (np.abs(y).max() + 1e-12), times


def _env_notes(n, sr, rng, rate, attack, decay):
    env = np.zeros(n)
    t = 0.0
    while t < n / sr:
        i = int(t * sr)
        L = int(sr * rng.uniform(0.08, 0.3))
        k = np.arange(min(L, n - i))
        env[i:i + k.size] += np.minimum(1, k / (attack * sr)) * np.exp(-k / (decay * sr))
        t += rng.exponential(1 / rate)
    return env


def speech(sr, seconds, rng=None):
    """Parole synthétique : voisement à f0 variable, syllabes à 3-6 Hz, plosives (salves brèves de bruit)."""
    rng = _rng(rng)
    n = int(sr * seconds)
    t = np.arange(n) / sr
    f0 = rng.uniform(100, 220) * (1 + 0.1 * np.sin(2 * np.pi * rng.uniform(0.2, 1) * t))
    ph = 2 * np.pi * np.cumsum(f0) / sr
    src = sum(np.sin(k * ph) / k for k in range(1, 25))
    voiced = _band(src, 250, 900, sr) + 0.6 * _band(src, 1000, 2600, sr) + 0.2 * _band(src, 2600, 3800, sr)
    env = _env_notes(n, sr, rng, rate=rng.uniform(3, 6), attack=0.02, decay=0.08)
    y = voiced * env
    # plosives / fricatives
    for _ in range(int(seconds * rng.uniform(0.8, 1.6))):
        i = rng.integers(0, max(1, n - sr // 10))
        L = int(sr * rng.uniform(0.008, 0.04))
        y[i:i + L] += _band(rng.standard_normal(L), 1500, 7000, sr) * rng.uniform(0.3, 1.0) * np.abs(y).max()
    # pauses entre phrases
    gate = np.ones(n)
    t0 = 0.0
    while t0 < seconds:
        t0 += rng.uniform(1.5, 5)
        gate[int(t0 * sr): int((t0 + rng.uniform(0.2, 0.9)) * sr)] = 0.05
    return y * gate


def music(sr, seconds, rng=None, drums=True):
    """Musique : accords à enveloppe de notes, et une batterie (grosse caisse, caisse claire, charleston) régulière."""
    rng = _rng(rng)
    n = int(sr * seconds)
    t = np.arange(n) / sr
    y = np.zeros(n)
    bpm = rng.uniform(80, 135)
    beat = 60 / bpm
    for _ in range(3):
        f = 110 * 2 ** (rng.integers(0, 24) / 12)
        env = _env_notes(n, sr, rng, rate=1 / beat, attack=0.01, decay=0.4)
        y += 0.3 * sum(np.sin(2 * np.pi * k * f * t) / k for k in range(1, 6)) * env
    if drums:
        hits = np.zeros(n)
        k = 0
        while k * beat / 2 < seconds:
            i = int(k * beat / 2 * sr)
            d = knock(sr, "porte", rng) * 0.9 if k % 4 == 0 else (clap(sr, rng) * 0.7 if k % 4 == 2 else
                                                              _band(rng.standard_normal(int(0.05 * sr)), 6000, 9000, sr)
                                                              * np.exp(-np.arange(int(0.05 * sr)) / (0.01 * sr)) * 0.3)
            m = min(d.size, n - i)
            hits[i:i + m] += d[:m]
            k += 1
        y += hits * rng.uniform(0.4, 1.0)
    return y


def kitchen(sr, seconds, rng=None):
    """Cuisine : eau qui coule, tintements isolés (et parfois en série irrégulière), planche à découper."""
    rng = _rng(rng)
    n = int(sr * seconds)
    y = 0.05 * _band(rng.standard_normal(n), 400, 6000, sr) * (rng.uniform() < 0.5)
    t = rng.exponential(2.5)
    while t < seconds - 1:
        kind = rng.choice(["verre", "bureau", "chop"])
        if kind == "chop":  # 5 à 15 coups réguliers
            p, _ = pattern(sr, "bureau", int(rng.integers(5, 15)), ioi=rng.uniform(0.2, 0.35), rng=rng)
        else:
            p, _ = pattern(sr, kind, int(rng.integers(1, 3)), ioi=rng.uniform(0.05, 1.5), jitter=0.6, rng=rng)
        i = int(t * sr)
        m = min(p.size, n - i)
        y[i:i + m] += p[:m] * rng.uniform(0.2, 1.0)
        t += p.size / sr + rng.exponential(3)
    return y


def footsteps(sr, seconds, rng=None):
    """Pas : chocs sourds réguliers (0,45-0,65 s), séries de 4 à 12."""
    rng = _rng(rng)
    n = int(sr * seconds)
    y = np.zeros(n)
    t = rng.uniform(0, 2)
    while t < seconds - 1:
        steps = int(rng.integers(4, 13))
        ioi = rng.uniform(0.45, 0.65)
        for _ in range(steps):
            i = int(t * sr)
            L = int(0.2 * sr)
            tt = np.arange(L) / sr
            s = (np.sin(2 * np.pi * rng.uniform(60, 140) * tt) * np.exp(-tt / 0.03)
                 + 0.1 * _band(rng.standard_normal(L), 800, 3000, sr) * np.exp(-tt / 0.01))
            m = min(L, n - i)
            if m <= 0:
                break
            y[i:i + m] += s[:m] * rng.uniform(0.6, 1.0)
            t += ioi * (1 + rng.uniform(-0.05, 0.05))
        t += rng.uniform(2, 8)
    return y


def fan(sr, seconds, rng=None):
    rng = _rng(rng)
    n = int(sr * seconds)
    w = rng.standard_normal(n)
    return lfilter([1], [1, -0.97], w) * 0.02


def tv(sr, seconds, rng=None):
    rng = _rng(rng)
    return speech(sr, seconds, rng) + 0.4 * music(sr, seconds, rng, drums=rng.uniform() < 0.5)


AMBIENT = {"parole": speech, "musique": music, "tv": tv, "cuisine": kitchen, "pas": footsteps, "ventilation": fan}


def ambient(sr, seconds, kind, rng=None, rms=0.02):
    """Fond de `kind` au niveau `rms`, plus un bruit de pièce et de micro 35 dB sous ce niveau (jamais de silence
    numérique : un vrai micro ne rend jamais zéro)."""
    rng = _rng(rng)
    y = AMBIENT[kind](sr, seconds, rng)
    if not np.all(np.isfinite(y)) or np.abs(y).max() > 1e6:  # filtre instable (rare) : fond de ventilation
        y = fan(sr, seconds, rng)
    r = np.sqrt((y ** 2).mean()) + 1e-12
    y = y * (rms / r)
    return y + rng.standard_normal(y.size) * rms * 10 ** (-35 / 20)


def mix(background, sig, at_s, sr, snr_db):
    """Ajoute `sig` à `background` à `at_s`, le pic de `sig` à `snr_db` au-dessus du RMS du fond."""
    out = background.copy()
    rms = np.sqrt((background ** 2).mean()) + 1e-6
    sig = sig / (np.abs(sig).max() + 1e-12) * rms * 10 ** (snr_db / 20)
    i = int(at_s * sr)
    m = min(sig.size, out.size - i)
    out[i:i + m] += sig[:m]
    return out
