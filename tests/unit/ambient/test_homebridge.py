"""Passerelle du foyer : liste blanche, jeton du noyau, opérations nommées, enceintes factices (Snapcast, Sonos, Music
Assistant via Home Assistant en mock, AirPlay), présence depuis Home Assistant en mock, écrans servis puis révoqués.
Aucun appareil réel : les serveurs HTTP de la passerelle écoutent sur 127.0.0.1, port éphémère."""
from __future__ import annotations

import datetime as dt
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from nokker.ambient.bridge import HomeBridge
from nokker.ambient.displays import Displays, Membership, MemoryDisplayStore, sha
from nokker.ambient.presence import RoomsConfig, StatesPresence
from nokker.ambient.publisher import EinkPublisher
from nokker.ambient.raster import png_info
from nokker.ambient.room_audio import RoomAudio
from nokker.homebridge.audio import (
    AirPlaySink,
    AudioRouter,
    ClipStore,
    MusicAssistantSink,
    SnapcastSink,
    SonosSink,
)
from nokker.homebridge.config import Allowlist, BridgeConfigError, Refused, from_dict
from nokker.homebridge.server import Bridge, serve
from tests.unit.ambient.helpers import OGG, FakeTTS

TOKEN = "t" * 40
ALICE = Membership(account_id="local", household_id="local", household_tenant="tn_aaaaaaaaaaaa", profile="adult")
NOW = dt.datetime(2026, 11, 2, 7, 30)


def allow(**kw) -> Allowlist:
    a = Allowlist.from_config([{"host": "ha.lan", "ports": [8123]},
                               {"cidr": "192.168.1.0/24", "ports": [1400, 1705, 4953, 7000]}])
    a.resolve = lambda h: {"ha.lan": ["192.168.1.10"], "evil.example": ["93.184.216.34"],
                           "rebind.lan": ["192.168.1.20", "8.8.8.8"]}.get(h, [])
    return a


# ─────────────────────── liste blanche ───────────────────────

def test_allowlist():
    a = allow()
    assert a.check("ha.lan", 8123) == "192.168.1.10"
    assert a.check("192.168.1.30", 1400) == "192.168.1.30"
    for host, port in (("192.168.1.30", 22), ("10.0.0.5", 1400), ("evil.example", 8123), ("rebind.lan", 1400),
                       ("127.0.0.1", 1400), ("inconnu.lan", 1400)):
        with pytest.raises(Refused):
            a.check(host, port)
    with pytest.raises(BridgeConfigError):
        Allowlist.from_config([{"cidr": "8.8.8.0/24", "ports": [80]}])  # plage publique : refusée à la lecture
    assert Allowlist.from_config(None).rules == []  # livré vide : tout refusé


# ─────────────────────── enceintes factices ───────────────────────

class FakeLan:
    def __init__(self, a=None):
        self.allow = a or allow()
        self.http_calls, self.sockets = [], []

    def http(self, method, url, body=None, headers=None):
        from urllib.parse import urlsplit

        p = urlsplit(url)
        self.allow.check(p.hostname, p.port or 80)
        self.http_calls.append((method, url, body, headers))
        return 200, b""

    def tcp(self, host, port):
        self.allow.check(host, port)
        s = FakeSnapSocket(port)
        self.sockets.append(s)
        return s


class FakeSnapSocket:
    """Serveur Snapcast factice : JSON-RPC sur :1705, source TCP sur :4953."""

    streams = {"grp1": "musique"}

    def __init__(self, port):
        self.port, self.sent, self.out, self.closed = port, b"", b"", False

    def sendall(self, data):
        self.sent += data
        if self.port == 1705:
            msg = json.loads(data)
            if msg["method"] == "Server.GetStatus":
                res = {"server": {"groups": [{"id": g, "stream_id": s} for g, s in self.streams.items()]}}
            else:
                self.streams[msg["params"]["id"]] = msg["params"]["stream_id"]
                res = {"stream_id": msg["params"]["stream_id"]}
            note = {"jsonrpc": "2.0", "method": "Client.OnVolumeChanged", "params": {}}  # notification à ignorer
            self.out += (json.dumps(note) + "\r\n" + json.dumps({"id": msg["id"], "result": res}) + "\r\n").encode()

    def recv(self, n):
        out, self.out = self.out[:n], self.out[n:]
        return out

    def close(self):
        self.closed = True


def test_snapcast_switches_to_tts_stream_pushes_pcm_and_restores():
    lan = FakeLan()
    seen = []
    sink = SnapcastSink(lan=lan, host="192.168.1.40", group="grp1", decode=lambda a: b"\x01\x02" * 96000,
                        sleep=seen.append)
    sink.play(OGG, "audio/ogg", "")
    rpc, src = lan.sockets
    methods = [json.loads(line)["params"]["stream_id"] for line in rpc.sent.decode().splitlines()
               if "Group.SetStream" in line]
    assert methods == ["tts", "musique"]  # bascule puis retour au flux d'avant
    assert src.port == 4953 and src.sent == b"\x01\x02" * 96000 and src.closed and rpc.closed
    assert seen and seen[0] == pytest.approx(1.5)  # 192 000 octets = 1 s à 48 kHz stéréo 16 bits, + 0,5 s


def test_sonos_local_upnp():
    lan = FakeLan()
    SonosSink(lan=lan, host="192.168.1.30").play(OGG, "audio/ogg", "http://192.168.1.5:8801/clip/abc&x")
    (_m1, u1, b1, h1), (_m2, _, _b2, h2) = lan.http_calls
    assert u1 == "http://192.168.1.30:1400/MediaRenderer/AVTransport/Control"
    assert h1["SOAPACTION"].endswith('#SetAVTransportURI"') and b"<CurrentURI>http://192.168.1.5:8801/clip/abc&amp;x" in b1
    assert h2["SOAPACTION"].endswith('#Play"')


def test_music_assistant_announce_through_home_assistant_mock():
    class HA:
        calls = []

        def call_service(self, domain, service, data):
            self.calls.append((domain, service, data))

    ha = HA()
    MusicAssistantSink(ha=ha, target="media_player.salon").play(OGG, "audio/ogg", "http://192.168.1.5:8801/clip/x")
    assert ha.calls == [("media_player", "play_media", {"entity_id": "media_player.salon", "announce": True,
                                                         "media_content_id": "http://192.168.1.5:8801/clip/x",
                                                         "media_content_type": "music"})]


def test_airplay_through_allowlist():
    got = []
    AirPlaySink(allow=allow(), host="192.168.1.50", stream=lambda ip, url: got.append((ip, url))).play(
        OGG, "audio/ogg", "http://192.168.1.5:8801/clip/y")
    assert got == [("192.168.1.50", "http://192.168.1.5:8801/clip/y")]
    with pytest.raises(Refused):
        AirPlaySink(allow=allow(), host="10.1.1.1", stream=lambda ip, url: None).play(OGG, "audio/ogg", "http://x/c")


def test_router_falls_back_to_next_speaker_and_serves_clip():
    class Broken:
        kind = "sonos"

        def play(self, *a):
            raise OSError("down")

    class Ok:
        kind = "snapcast"
        got = None

        def play(self, audio, mime, url):
            self.got = url

    ok = Ok()
    clips = ClipStore()
    r = AudioRouter(rooms={"salon": [Broken(), ok]}, clips=clips, lan_url="http://192.168.1.5:8801")
    assert r.play("salon", OGG, "audio/ogg") == {"played": True, "sinks": ["snapcast"]}
    assert clips.get(ok.got.rsplit("/", 1)[1]) == (OGG, "audio/ogg")
    assert r.play("cave", OGG, "audio/ogg")["reason"] == "no_speaker"


# ─────────────────────── serveur : jeton, opérations, écoutes séparées ───────────────────────

@pytest.fixture()
def running(tmp_path: Path):
    raw = {"allow": [], "rooms": {"salon": {"occupancy": ["binary_sensor.salon"]}},
           "members": {"us_alice": {"room_sensor": "sensor.alice_piece"}}, "guests": "input_boolean.invites",
           "api_listen": "127.0.0.1:0", "lan": {"listen": "127.0.0.1:0"}}
    cfg = from_dict(raw, token=TOKEN, base_dir=tmp_path)

    class HAMock:
        states_value = [{"entity_id": "binary_sensor.salon", "state": "on"},
                        {"entity_id": "sensor.alice_piece", "state": "Salon"},
                        {"entity_id": "input_boolean.invites", "state": "off"},
                        {"entity_id": "lock.porte", "state": "locked"}]

        def states(self):
            return self.states_value

    played = []

    class Sink:
        kind = "fake"

        def play(self, audio, mime, url):
            played.append(audio)

    ha = HAMock()
    bridge = Bridge.build(cfg, ha=ha, sinks={"salon": [Sink()]})
    api, lan = serve(bridge)
    yield {"bridge": bridge, "api": f"http://127.0.0.1:{api.server_address[1]}",
           "lan": f"http://127.0.0.1:{lan.server_address[1]}", "played": played, "ha": ha}
    api.shutdown()
    lan.shutdown()


def _req(url, *, token=None, body=None, headers=None, method=None):
    h = dict(headers or {})
    if token:
        h["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=h, method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_api_requires_token_and_named_ops(running):
    api = running["api"]
    assert _req(f"{api}/v1/op/presence.get", body={})[0] == 401
    assert _req(f"{api}/v1/op/presence.get", body={}, token="x" * 40)[0] == 401
    assert _req(f"{api}/v1/op/http.get", body={"url": "http://192.168.1.1"}, token=TOKEN)[0] == 404  # pas de proxy
    st, raw = _req(f"{api}/v1/health", token=TOKEN)
    assert st == 200 and "audio.play" in json.loads(raw)["ops"]
    # la présence ne remonte QUE la pièce, les membres localisés et le mode invités (jamais la serrure)
    st, raw = _req(f"{api}/v1/op/presence.get", body={}, token=TOKEN)
    out = json.loads(raw)
    assert st == 200 and out["rooms"]["salon"] == {"members": ["us_alice"], "occupied": True, "unknown": False}
    assert "lock" not in raw.decode() and out["guests"] is False


def test_lan_listener_exposes_no_core_operation(running):
    st, _ = _req(f"{running['lan']}/v1/op/presence.get", body={}, token=TOKEN)
    assert st == 404
    assert _req(f"{running['lan']}/v1/health", token=TOKEN)[0] == 404


def test_core_room_audio_end_to_end_through_bridge(running):
    """Noyau (RoomAudio + client HomeBridge) → passerelle HTTP → enceinte factice ; puis invités → annonce neutre."""
    core_bridge = HomeBridge(url=running["api"], token=lambda: TOKEN)
    tts = FakeTTS()
    ra = RoomAudio(bridge=core_bridge, tts=tts, scrub=lambda s: s, rooms=frozenset({"salon"}), enabled=True)
    r = ra.say("auto", "Ton train a dix minutes de retard.", recipient="us_alice")
    assert (r.status, r.room) == ("spoken", "salon") and running["played"][-1] == OGG + b"Ton train a dix minutes de retard."
    running["ha"].states_value = running["ha"].states_value[:2] + [{"entity_id": "input_boolean.invites",
                                                                    "state": "on"}]
    r = ra.say("auto", "Ton train a dix minutes de retard.", recipient="us_alice")
    assert (r.status, r.reason) == ("neutral", "guests") and b"train" not in running["played"][-1]
    bad = HomeBridge(url=running["api"], token=lambda: "y" * 40)
    assert RoomAudio(bridge=bad, tts=tts, scrub=lambda s: s, rooms=frozenset({"salon"}), enabled=True).say(
        "salon", "x", privacy="common").reason == "bridge"


def test_eink_publish_serve_and_revoke_on_lan(running):
    """Noyau (Displays + EinkPublisher) → passerelle → appareils du LAN ; révocation : 401 tout de suite."""
    core_bridge = HomeBridge(url=running["api"], token=lambda: TOKEN)
    d = Displays(store=MemoryDisplayStore(), now_local=lambda: NOW)
    pub = EinkPublisher(displays=d, bridge=core_bridge)
    d.on_revoke = pub.revoked
    d.setup_offer = lambda m, t, ttl: core_bridge.op("eink.setup_offer", {"mac_sha256": m, "token": t, "ttl_s": ttl})
    token = d.register(ALICE, {"model": "kindle_600x800"})["token"]
    d.register(ALICE, {"model": "trmnl", "mac": "AA:BB:CC:DD:EE:03"})
    d.put_board(ALICE, {"courses": ["Lait"]})
    assert pub.tick() == 2 and pub.tick() == 0  # rien de neuf : rien de republié
    lan = running["lan"]
    st, png = _req(f"{lan}/eink/{token}.png")
    assert st == 200 and png_info(png)[:3] == (600, 800, 2)
    assert all(e.token_sha256 != token and e.token_sha256 for e in running["bridge"].frames._by_id.values())
    st, raw = _req(f"{lan}/api/setup", headers={"ID": "AA:BB:CC:DD:EE:03"})
    trmnl_token = json.loads(raw)["api_key"]
    assert st == 200 and _req(f"{lan}/api/setup", headers={"ID": "AA:BB:CC:DD:EE:03"})[0] == 404
    st, raw = _req(f"{lan}/api/display", headers={"Access-Token": trmnl_token})
    disp = json.loads(raw)
    assert st == 200 and disp["refresh_rate"] == 900 and "/eink/" in disp["image_url"]
    assert png_info(_req(disp["image_url"])[1])[:2] == (800, 480)
    d.revoke(ALICE, d.resolve(token).id)
    assert _req(f"{lan}/eink/{token}.png")[0] == 401
    trmnl = d.resolve(trmnl_token)
    d.revoke(ALICE, trmnl.id)
    assert _req(f"{lan}/api/display", headers={"Access-Token": trmnl_token})[0] == 401
    pub.tick()
    assert running["bridge"].frames._by_id == {}


def test_bridge_frames_expire_and_retain(running):
    fc = running["bridge"].frames
    fc.publish("dp_1", sha("nkd_" + "a" * 40), b"\x89PNG", "f", 900, 60)
    assert fc.lookup("nkd_" + "a" * 40) is not None
    fc.clock = lambda: 1e12
    assert fc.lookup("nkd_" + "a" * 40) is None
    fc.clock = __import__("time").monotonic
    fc.publish("dp_2", sha("nkd_" + "b" * 40), b"\x89PNG", "f", 900, 900)
    assert fc.retain([]) == 1 and fc.lookup("nkd_" + "b" * 40) is None


def test_presence_from_ha_mock_unknown_occupant_and_unavailable():
    cfg = RoomsConfig.from_config({"rooms": {"salon": {"occupancy": ["binary_sensor.s"]},
                                             "cuisine": {"occupancy": ["binary_sensor.c"]}},
                                   "members": {"us_alice": {"room_sensor": "sensor.a"}}})
    p = StatesPresence(states=lambda: [{"entity_id": "binary_sensor.s", "state": "on"},
                                       {"entity_id": "binary_sensor.c", "state": "unavailable"},
                                       {"entity_id": "sensor.a", "state": "not_home"}], cfg=cfg).presence()
    assert p.rooms["salon"].unknown and not p.rooms["cuisine"].occupied and p.room_of("us_alice") is None
    boom = StatesPresence(states=lambda: 1 / 0, cfg=cfg).presence()
    assert boom.ok is False


def test_bridge_refuses_to_start_without_token(tmp_path):
    from nokker.homebridge.config import load

    with pytest.raises(BridgeConfigError):
        load({"HOME_BRIDGE_TOKEN": "", "HOME_CONFIG": str(tmp_path / "home.yaml")})
    cfg = load({"HOME_BRIDGE_TOKEN": TOKEN, "HOME_CONFIG": str(tmp_path / "absent.yaml")})
    assert cfg.allow.rules == []


def test_op_payload_errors_are_400_without_detail(running):
    st, raw = _req(f"{running['api']}/v1/op/audio.play", body={"room": "salon", "audio_b64": "%%%"}, token=TOKEN)
    assert st == 400
    st, _raw = _req(f"{running['api']}/v1/op/eink.publish", body={"display_id": "dp_x"}, token=TOKEN)
    assert st == 400


def test_documentation_and_special_ranges_are_not_the_home_lan():
    a = Allowlist.from_config([{"cidr": "192.168.1.0/24", "ports": [80]}])
    for ip in ("203.0.113.7", "198.51.100.7", "192.0.2.7", "240.0.0.1", "100.64.0.1", "::ffff:203.0.113.7",
               "2001:db8::1", "127.0.0.1", "0.0.0.0"):
        with pytest.raises(Refused):
            a.check(ip, 80)
    for cidr in ("203.0.113.0/24", "198.51.100.0/24", "192.0.2.0/24", "2001:db8::/32", "192.168.0.0/15"):
        with pytest.raises(BridgeConfigError):
            Allowlist.from_config([{"cidr": cidr, "ports": [80]}])
    assert a.check("::ffff:192.168.1.9", 80) == "192.168.1.9"
