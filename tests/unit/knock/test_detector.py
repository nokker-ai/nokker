"""Détecteur et écoute du heurtoir (nokker/homebridge/knock, conteneur home-audio) : reconnaissance, rejets,
calibrage, tampon circulaire, mute, aucun son émis. numpy/scipy requis (image home-bridge) : sinon ignoré."""
from __future__ import annotations

import json

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from nokker.homebridge.knock import synth
from nokker.homebridge.knock.audio import AudioConfig, Listener, RingBuffer
from nokker.homebridge.knock.calibrate import LISTEN, calibrate
from nokker.homebridge.knock.detector import Hit, KnockDetector, KnockSettings

SR = 16000


def run(x, settings=None, sr=SR, block=320):
    d = KnockDetector(sr, settings)
    ev = []
    x = np.concatenate([x, np.zeros(sr * 2)])
    for i in range(0, x.size, block):
        ev += d.process(x[i:i + block])
    return ev


def quiet(seconds, seed=0):
    return synth.ambient(SR, seconds, "ventilation", np.random.default_rng(seed), rms=0.002)


@pytest.mark.parametrize("src", ["bois", "porte", "verre", "bureau", "clap"])
def test_three_knocks_in_quiet_room(src):
    found = 0
    for seed in range(4):
        rng = np.random.default_rng(seed)
        # tempo habituel (0,3-0,5 s) : un verre résonant frappé plus vite que 0,25 s se perd (docs/KNOCK.md §6)
        p, times = synth.pattern(SR, src, rng=rng, ioi=rng.uniform(0.3, 0.5))
        x = synth.mix(quiet(5, seed), p, 1.5, SR, 35)
        ev = run(x)
        found += any(abs(e.t - (1.5 + times[-1])) < 0.06 for e in ev)
    assert found >= 3, src


@pytest.mark.parametrize("count", [1, 2, 4, 6])
def test_other_counts_do_not_fire(count):
    rng = np.random.default_rng(count)
    p, _ = synth.pattern(SR, "bois", count, ioi=0.3, rng=rng)
    assert run(synth.mix(quiet(6), p, 1.5, SR, 35)) == []


def test_too_slow_or_irregular_does_not_fire():
    rng = np.random.default_rng(3)
    slow, _ = synth.pattern(SR, "bois", 3, ioi=1.3, jitter=0.0, rng=rng)
    assert run(synth.mix(quiet(7), slow, 1.0, SR, 35)) == []
    # irrégulier : 0,15 s puis 0,7 s
    x = quiet(6)
    for t in (1.0, 1.15, 1.85):
        x = synth.mix(x, synth.knock(SR, "bois", rng), t, SR, 35)
    assert run(x) == []


def test_latency_and_sample_rate_independence():
    for sr in (16000, 44100, 48000):
        rng = np.random.default_rng(11)
        p, _times = synth.pattern(sr, "bois", rng=rng, ioi=0.3)
        x = synth.mix(synth.ambient(sr, 5, "ventilation", rng, rms=0.002), p, 1.5, sr, 35)
        ev = run(x, sr=sr)
        assert len(ev) == 1, sr
        assert 0.5 <= ev[0].fired_at - ev[0].t <= 0.9  # garde de calme après le 3e coup + jugement


@pytest.mark.parametrize("kind", ["parole", "musique", "tv", "cuisine", "pas", "ventilation"])
def test_no_false_trigger_on_two_minutes_of_noise(kind):
    x = synth.ambient(SR, 120, kind, np.random.default_rng(42))
    assert len(run(x)) <= 1, kind


def test_calibration_tightens_around_the_knocks():
    rng = np.random.default_rng(5)
    p, _ = synth.pattern(SR, "bureau", rng=rng, ioi=0.25)
    x = synth.mix(quiet(4), p, 1.0, SR, 30)
    hits: list[Hit] = []
    KnockDetector(SR, LISTEN, on_hit=hits.append).process(np.concatenate([x, np.zeros(SR)]))
    s = calibrate(hits)
    assert s is not None
    assert 0.08 <= s.ioi_min_s <= 0.2 and 0.4 <= s.ioi_max_s <= 0.6
    # resserré (ou desserré) autour de CES coups : 6 dB sous la part d'aigus la plus faible des trois
    assert -30 <= s.hf_min_db <= -6 and s != KnockSettings()
    assert calibrate(hits[:1]) is None


def test_settings_roundtrip_ignores_unknown():
    s = KnockSettings.from_dict({"ioi_max_s": 0.5, "inconnu": 1})
    assert s.ioi_max_s == 0.5 and KnockSettings.from_dict(s.to_dict()) == s


# ---------------------------------------------------------------------------------------- écoute, vie privée --
def test_ring_buffer_wraps_and_wipes():
    r = RingBuffer(0.01, 1000)  # 10 échantillons
    r.write(np.arange(7))
    r.write(np.arange(7, 14))
    assert r.filled == 10 and sorted(r.buf.tolist()) == list(range(4, 14))
    r.wipe()
    assert r.is_blank()


def _listener(tmp_path, **kw):
    out = []
    cfg = AudioConfig(room="salon", socket_path=str(tmp_path / "audio.sock"), **kw)
    holder = {}

    def emit(msg):  # au moment où l'événement part : le tampon doit déjà être vide
        out.append({**msg, "_ring_blank": holder["lis"].ring.is_blank()})

    lis = Listener(cfg, emit, clock=lambda: 1_800_000_000.0)
    holder["lis"] = lis
    return lis, out


def test_listener_emits_only_facts_and_wipes_buffer(tmp_path):
    lis, out = _listener(tmp_path)
    lis.poll_mutes()
    assert lis.state == "listening"
    rng = np.random.default_rng(1)
    p, _ = synth.pattern(SR, "bois", rng=rng, ioi=0.3)
    x = np.concatenate([synth.mix(quiet(4), p, 1.0, SR, 35), np.zeros(SR * 2)])
    for i in range(0, x.size, 320):
        lis.feed(x[i:i + 320])
    knocks = [m for m in out if m["type"] == "knock"]
    assert len(knocks) == 1
    k = knocks[0]
    assert k.pop("_ring_blank") is True  # rien de l'avant-déclencheur ne survit
    assert set(k) == {"type", "event_id", "kind", "room", "at", "source", "confidence", "intervals"}
    assert len(json.dumps(k)) < 400  # un fait, pas un son
    assert k["kind"] == "knock" and k["room"] == "salon" and k["at"] > 1_800_000_000


def test_soft_and_hardware_mute(tmp_path):
    lis, out = _listener(tmp_path, hw_mute_cmd=["sh", "-c", "cat " + str(tmp_path / "hw")])
    (tmp_path / "hw").write_text("0")
    lis.poll_mutes()
    lis.feed(np.ones(320, dtype=np.float32) * 0.1)
    assert lis.ring.filled == 320
    (tmp_path / "knock-mute-salon").touch()  # op knock.mute de home-bridge
    lis.poll_mutes()
    assert lis.state == "muted" and lis.ring.is_blank()
    assert lis.feed(np.ones(320, dtype=np.float32)) == [] and lis.ring.is_blank()
    (tmp_path / "knock-mute-salon").unlink()
    (tmp_path / "hw").write_text("1")  # interrupteur matériel : le logiciel ne peut pas lever le mute
    lis.poll_mutes()
    assert lis.state == "muted"
    lis.set_soft_mute(False)
    assert lis.state == "muted"
    (tmp_path / "hw").write_text("0")
    lis.poll_mutes()
    assert lis.state == "listening"
    states = [m["state"] for m in out if m["type"] == "state"]
    assert states == ["listening", "muted", "listening"]


def test_unreadable_hw_switch_fails_closed(tmp_path):
    lis, _ = _listener(tmp_path, hw_mute_cmd=["/nonexistent/switch"])
    lis.poll_mutes()
    assert lis.state == "muted"


def test_indicator_follows_state(tmp_path):
    led = tmp_path / "brightness"
    lis, _ = _listener(tmp_path, led_path=str(led))
    lis.poll_mutes()
    assert led.read_text() == "1"
    lis.set_soft_mute(True)
    assert led.read_text() == "0"
    lis.stop()
    assert led.read_text() == "0" and lis.state == "off"
