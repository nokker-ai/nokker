"""Heurtoir dans home-bridge (docs/KNOCK.md §8) : file d'événements, socket de home-audio, satellites, opérations
nommées, et bout en bout pompe du noyau → hub. Sans réseau réel (socket Unix dans tmp_path, client HTTP local)."""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from nokker.homebridge.config import from_dict
from nokker.homebridge.knock.events import KnockBridge, KnockConfig, KnockEvents, clean_event
from nokker.homebridge.server import Bridge, OpError, serve
from nokker.knock.bridge import KnockPump
from nokker.knock.hub import KnockHub
from nokker.knock.policy import KnockConfig as CoreKnockConfig

TOKEN = "t" * 32
SAT_TOKEN = "s" * 32


def test_clean_event_keeps_only_known_fields():
    ev = clean_event({"kind": "knock", "room": "salon", "at": 1.0, "confidence": 3, "audio_b64": "AAAA",
                      "pcm": [1, 2], "event_id": "salon-1", "source": "mic:salon"})
    assert ev == {"event_id": "salon-1", "kind": "knock", "room": "salon", "at": 1.0, "confidence": 1.0,
                  "source": "mic:salon"}
    assert clean_event({"kind": "cri", "room": "salon", "at": 1}) is None
    assert clean_event({"kind": "knock", "room": "Salon", "at": 1}) is None
    assert clean_event({"kind": "knock", "room": "salon", "at": "x"}) is None
    # un satellite ne choisit pas sa pièce
    assert clean_event({"kind": "clap", "room": "chambre", "at": 2}, room="cuisine", source="sat:p")["room"] == "cuisine"


def test_long_poll_returns_new_events_only():
    q = KnockEvents()
    assert q.wait(0, 0) == {"cursor": 0, "events": []}
    c = q.push({"event_id": "a"})
    got = q.wait(0, 0)
    assert got["cursor"] == c and [e["event_id"] for e in got["events"]] == ["a"]
    assert q.wait(c, 0)["events"] == []
    t0 = time.monotonic()
    threading.Timer(0.2, lambda: q.push({"event_id": "b"})).start()
    got = q.wait(c, 5)
    assert [e["event_id"] for e in got["events"]] == ["b"] and time.monotonic() - t0 < 2


def _bridge(tmp_path: Path, satellites=True) -> Bridge:
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "knock_cuisine_pi").write_text(SAT_TOKEN)
    raw = {"api_listen": "127.0.0.1:0", "lan": {"listen": "127.0.0.1:0"},
           "knock": {"socket": str(tmp_path / "audio.sock"), "mute_dir": str(tmp_path),
                     **({"satellites": {"cuisine_pi": {"room": "cuisine"}}} if satellites else {})}}
    return Bridge.build(from_dict(raw, token=TOKEN, base_dir=tmp_path), sinks={})


def test_ops_absent_without_knock_block(tmp_path):
    b = Bridge.build(from_dict({}, token=TOKEN, base_dir=tmp_path), sinks={})
    with pytest.raises(OpError) as e:
        b.op("knock.events", {})
    assert e.value.status == 404


def test_audio_socket_to_core_hub_end_to_end(tmp_path):
    """home-audio écrit sur la socket → home-bridge garde le fait → la pompe du noyau le tire par l'API authentifiée →
    le hub ouvre une session vocale. Le champ « audio » glissé dans le message n'atteint jamais le noyau."""
    b = _bridge(tmp_path)
    api, lan = serve(b)
    try:
        for _ in range(50):
            if Path(b.knock.cfg.socket_path).exists():
                break
            time.sleep(0.02)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(b.knock.cfg.socket_path)
        now = time.time()
        s.sendall(json.dumps({"type": "state", "room": "salon", "state": "listening"}).encode() + b"\n")
        s.sendall(json.dumps({"type": "knock", "event_id": "salon-x1", "kind": "knock", "room": "salon", "at": now,
                              "confidence": 0.8, "source": "mic:salon", "audio_b64": "SECRET"}).encode() + b"\n")
        s.sendall(b"pas du json\n")
        s.close()
        for _ in range(50):
            if b.knock.events.wait(0, 0)["events"]:
                break
            time.sleep(0.02)

        from nokker.ambient.bridge import HomeBridge

        client = HomeBridge(url=f"http://127.0.0.1:{api.server_address[1]}", token=lambda: TOKEN, timeout_s=5)
        voice = []
        hub = KnockHub(CoreKnockConfig.from_raw({"enabled": True, "rooms": {
            "salon": {"thread_id": "knock-salon", "sender": "alice"}}}), voice=lambda r, k: voice.append((r.id, k)))
        pump = KnockPump(client, hub, wait_s=0)
        assert pump.run_once() == 1
        assert voice == [("salon", "knock")]
        status = client.op("knock.status", {})
        assert status["rooms"]["salon"]["state"] == "listening"
        # mauvais jeton : refus
        bad = HomeBridge(url=client.url, token=lambda: "x" * 32, timeout_s=5)
        from nokker.ambient.bridge import BridgeError

        with pytest.raises(BridgeError):
            bad.op("knock.events", {})
        # mute logiciel : fichier lu par home-audio
        client.op("knock.mute", {"room": "salon", "muted": True})
        assert (tmp_path / "knock-mute-salon").exists()
        client.op("knock.mute", {"room": "salon", "muted": False})
        assert not (tmp_path / "knock-mute-salon").exists()
    finally:
        api.shutdown()
        lan.shutdown()


def test_satellite_needs_its_token_and_gets_its_room(tmp_path):
    b = _bridge(tmp_path)
    api, lan = serve(b)
    url = f"http://127.0.0.1:{lan.server_address[1]}/knock/event"

    def post(sat, token, body):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"X-Satellite": sat, "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        assert post("cuisine_pi", "faux" * 8, {"kind": "knock", "at": 1}) == 401
        assert post("inconnu", SAT_TOKEN, {"kind": "knock", "at": 1}) == 401
        assert post("cuisine_pi", SAT_TOKEN, {"kind": "knock", "at": 1, "room": "chambre"}) == 202
        evs = b.knock.events.wait(0, 0)["events"]
        assert len(evs) == 1 and evs[0]["room"] == "cuisine" and evs[0]["source"] == "sat:cuisine_pi"
    finally:
        api.shutdown()
        lan.shutdown()


def test_satellite_token_too_short_refused(tmp_path):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "knock_p").write_text("court")
    with pytest.raises(ValueError):
        KnockConfig.from_raw({"satellites": {"p": {"room": "salon"}}},
                             lambda rel: (tmp_path / rel).read_text())
    assert KnockConfig.from_raw(None, lambda rel: "") is None
    assert isinstance(KnockBridge(KnockConfig()).op_status({}), dict)
