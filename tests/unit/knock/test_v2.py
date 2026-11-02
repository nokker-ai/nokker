"""Détecteur v2 (nokker/homebridge/knock/v2.py) : reconnaissance en pièce calme et sur fond de parole, rejets,
indépendance à la fréquence d'échantillonnage (décimation), réseau ≤ 200 ko, calibrage. numpy/scipy requis."""
from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from nokker.homebridge.knock import synth
from nokker.homebridge.knock.calibrate import calibrate_v2
from nokker.homebridge.knock.detector import Hit
from nokker.homebridge.knock.v2 import (
    CNN_PATH,
    KnockCNN,
    KnockDetectorV2,
    KnockSettingsV2,
    frame_params,
)

SR = 16000


def run(x, sr=SR, block=800, **kw):
    d = KnockDetectorV2(sr, **kw)
    ev = []
    x = np.concatenate([x, np.zeros(sr * 2)])
    for i in range(0, x.size, block):
        ev += d.process(x[i:i + block])
    return ev


def quiet(seconds, seed=0, sr=SR):
    return synth.ambient(sr, seconds, "ventilation", np.random.default_rng(seed), rms=0.002)


def test_weights_small_and_loadable():
    assert CNN_PATH.stat().st_size < 200_000
    cnn = KnockCNN.load()
    n = sum(np.asarray(getattr(cnn, k)).size for k in ("w1", "b1", "w2", "b2", "w3", "b3", "wd")) + 1
    assert n < 20_000  # ~14 k paramètres (16/32/32 canaux)


@pytest.mark.parametrize("src", ["bois", "porte", "verre", "bureau", "clap"])
def test_three_knocks_in_quiet_room(src):
    found = 0
    for seed in range(4):
        rng = np.random.default_rng(seed)
        p, times = synth.pattern(SR, src, rng=rng, ioi=rng.uniform(0.25, 0.5))
        ev = run(synth.mix(quiet(5, seed), p, 1.5, SR, 35))
        found += any(abs(e.t - (1.5 + times[-1])) < 0.06 for e in ev)
    assert found >= 3, src


@pytest.mark.parametrize("count", [1, 2, 4, 6])
def test_other_counts_do_not_fire(count):
    rng = np.random.default_rng(count)
    p, _ = synth.pattern(SR, "bois", count, ioi=0.3, rng=rng)
    assert run(synth.mix(quiet(6), p, 1.5, SR, 35)) == []


@pytest.mark.parametrize("sr", [16000, 44100, 48000])
def test_sample_rates_and_latency(sr):
    assert frame_params(sr / (3 if sr >= 32000 else 1))[0] in (74, 80)
    rng = np.random.default_rng(11)
    p, times = synth.pattern(sr, "bois", rng=rng, ioi=0.3)
    ev = run(synth.mix(quiet(5, 3, sr), p, 1.5, sr, 35), sr=sr, block=sr // 20)
    assert len(ev) == 1, sr
    assert abs(ev[0].t - (1.5 + times[-1])) < 0.03
    assert ev[0].fired_at - ev[0].t <= 0.8


def test_rules_only_v2_still_needs_three():
    rng = np.random.default_rng(4)
    p, _ = synth.pattern(SR, "clap", 2, ioi=0.3, rng=rng)
    assert run(synth.mix(quiet(5), p, 1.5, SR, 35), use_cnn=False) == []


@pytest.mark.parametrize("kind", ["parole", "musique", "tv", "pas", "ventilation"])
def test_few_false_triggers_on_two_minutes_of_noise(kind):
    x = synth.ambient(SR, 120, kind, np.random.default_rng(42))
    assert len(run(x)) <= 1, kind


def test_calibration_v2():
    rng = np.random.default_rng(5)
    p, _ = synth.pattern(SR, "bureau", rng=rng, ioi=0.25)
    x = synth.mix(quiet(4), p, 1.0, SR, 30)
    hits: list[Hit] = []
    KnockDetectorV2(SR, KnockSettingsV2(odf_min_db=2.5, prob_min=0.0), on_hit=hits.append).process(
        np.concatenate([x, np.zeros(SR)]))
    s = calibrate_v2(hits)
    assert s is not None
    assert 0.08 <= s.ioi_min_s <= 0.2 and 0.4 <= s.ioi_max_s <= 0.6
    assert 0.2 <= s.prob_min <= 0.5 and 2.5 <= s.odf_min_db <= 4.0
    assert calibrate_v2(hits[:1]) is None


def test_no_audio_kept_beyond_window():
    d = KnockDetectorV2(SR)
    d.process(np.random.default_rng(0).standard_normal(SR * 2) * 0.01)
    # seul état « audio » : les 25 ms de la fenêtre glissante ; le reste, ce sont des niveaux mel (32 par trame)
    assert d._samples.size == frame_params(SR)[1] and d._mel.shape[1] == 32
    d.reset()
    assert not d._samples.any() and not d._mel.any()
