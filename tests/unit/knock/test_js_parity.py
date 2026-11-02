"""Parité Python ↔ JavaScript du détecteur v2 (web/knock/knock-worklet.js ↔ nokker/homebridge/knock/v2.py).

Mêmes signaux (synthétiques, graines fixes), mêmes réglages, mêmes poids du réseau : les deux trouvent les mêmes
attaques, avec les mêmes percussivités et les mêmes probabilités, et les mêmes déclenchements. Calibrage : même
règle. Lancé seulement si numpy/scipy (image home-bridge) et node sont là."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from nokker.homebridge.knock import synth
from nokker.homebridge.knock.calibrate import calibrate_v2
from nokker.homebridge.knock.detector import Hit
from nokker.homebridge.knock.v2 import (
    CNN_PATH,
    TRIPLE_PATH,
    KnockCNN,
    KnockDetectorV2,
    KnockSettingsV2,
    TripleScorer,
)

ROOT = Path(__file__).resolve().parents[3]
WORKLET = ROOT / "web" / "knock" / "knock-worklet.js"
WEB_CNN = ROOT / "web" / "knock" / "cnn.json"
WEB_TRIPLE = ROOT / "web" / "knock" / "triple.json"
RUNNER = Path(__file__).with_name("run_js_dsp.mjs")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node absent")


def _signals(sr):
    out = []
    for i, (src, bg) in enumerate([("bois", "ventilation"), ("clap", "ventilation"), ("verre", "parole"),
                                   ("porte", "cuisine"), ("bureau", "musique")]):
        rng = np.random.default_rng(100 + i)
        x = synth.ambient(sr, 5.0, bg, rng)
        p, _ = synth.pattern(sr, src, rng=rng, ioi=0.35)
        out.append(synth.mix(x, p, 1.5, sr, 30).astype(np.float32))
    return out


def _run_js(paths, sr, settings_path="-", cnn_path="-", triple_path="-"):
    res = subprocess.run([NODE, str(RUNNER), str(WORKLET), str(sr), settings_path, str(cnn_path), str(triple_path),
                          *map(str, paths)],
                         capture_output=True, text=True, timeout=300, check=True)
    return [json.loads(line) for line in res.stdout.splitlines()]


def test_web_weights_are_the_same_file():
    assert WEB_CNN.read_bytes() == CNN_PATH.read_bytes()
    assert WEB_TRIPLE.read_bytes() == TRIPLE_PATH.read_bytes()
    assert CNN_PATH.stat().st_size + TRIPLE_PATH.stat().st_size < 200_000  # « ≤ 200 ko »


@pytest.mark.parametrize("sr", [16000, 48000])
def test_same_hits_probs_and_events(tmp_path, sr):
    sigs = _signals(sr)
    paths = []
    for i, x in enumerate(sigs):
        p = tmp_path / f"s{i}.f32"
        p.write_bytes(np.concatenate([x, np.zeros(sr * 2, np.float32)]).tobytes())
        paths.append(p)
    js = _run_js(paths, sr, cnn_path=CNN_PATH, triple_path=TRIPLE_PATH)
    cnn = KnockCNN.load()
    scorer = TripleScorer.load(TRIPLE_PATH)
    fired = 0
    for x, j in zip(sigs, js):
        hits: list[Hit] = []
        d = KnockDetectorV2(sr, cnn=cnn, scorer=scorer, on_hit=hits.append)
        ev = d.process(np.concatenate([x, np.zeros(sr * 2, np.float32)]).astype(np.float64))
        assert len(hits) == len(j["hits"]), (len(hits), len(j["hits"]))
        for h, jh in zip(hits, j["hits"]):
            assert abs(h.t - jh["t"]) < 1e-9
            assert abs(h.features["odf"] - jh["features"]["odf"]) < 1e-6
            assert abs(h.features["prob"] - jh["features"]["prob"]) < 1e-6
            assert abs(h.features["level"] - jh["features"]["level"]) < 1e-6
            assert h.accepted == jh["accepted"]
        assert [round(e.t, 4) for e in ev] == [round(e["t"], 4) for e in j["events"]]
        fired += len(ev)
    assert fired >= 3  # le jeu contient bien des motifs reconnus (sinon la parité ne prouverait rien)


def test_calibration_same_rule(tmp_path):
    sr = 16000
    rng = np.random.default_rng(7)
    x = synth.ambient(sr, 4.0, "ventilation", rng)
    p, _ = synth.pattern(sr, "bois", rng=rng, ioi=0.3)
    x = synth.mix(x, p, 1.0, sr, 30).astype(np.float32)
    listen = KnockSettingsV2(odf_min_db=2.5, prob_min=0.0)
    hits: list[Hit] = []
    KnockDetectorV2(sr, listen, on_hit=hits.append).process(np.concatenate([x, np.zeros(sr)]).astype(np.float64))
    py = calibrate_v2(hits)
    assert py is not None
    f = tmp_path / "c.f32"
    f.write_bytes(np.concatenate([x, np.zeros(sr, np.float32)]).tobytes())
    s = tmp_path / "listen.json"
    s.write_text(json.dumps(listen.to_dict()))
    jh = _run_js([f], sr, str(s), CNN_PATH, TRIPLE_PATH)[0]["hits"]
    script = ("const vm=require('vm'),fs=require('fs');const sb={};sb.globalThis=sb;"
              f"vm.runInNewContext(fs.readFileSync({json.dumps(str(WORKLET))},'utf8'),sb);"
              "const hits=JSON.parse(fs.readFileSync(0,'utf8'));"
              "process.stdout.write(JSON.stringify(sb.NokkerKnockDSP.calibrate(hits,{})));")
    out = subprocess.run([NODE, "-e", script], input=json.dumps(jh), capture_output=True, text=True, check=True)
    js = json.loads(out.stdout)
    for k in ("odf_min_db", "prob_min", "ioi_min_s", "ioi_max_s"):
        assert abs(getattr(py, k) - js[k]) < 1e-6, (k, getattr(py, k), js[k])


def test_knock_listener_main_thread():
    """API du fil principal (web/knock/knock.js) avec des API navigateur simulées : micro sans traitement, aucune
    sortie audio, relais des faits seulement, mute, calibrage retenu (réglages), arrêt du micro."""
    res = subprocess.run([NODE, str(Path(__file__).with_name("knock_listener_check.mjs")),
                          str(ROOT / "web" / "knock" / "knock.js")], capture_output=True, text=True, timeout=60,
                         check=False)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "ok"
