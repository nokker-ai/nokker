"""Sortie audio de la passerelle du foyer (opération `audio.play`, docs/AMBIENT.md §2.4).

La pièce → ses enceintes (`rooms.<pièce>.speakers` de home.yaml), essayées dans l'ordre jusqu'à la première qui joue :
  - `music_assistant` : Home Assistant `media_player.play_media` avec `announce: true` sur l'entité Music Assistant
    (carillon, baisse puis reprise de la musique gérés par Music Assistant) ; l'audio est un extrait servi par la
    passerelle (`/clip/<id>`), lu par Home Assistant ;
  - `sonos` : API locale UPnP du lecteur (`:1400`, AVTransport `SetAVTransportURI` puis `Play`) sur l'extrait ;
    pas de reprise de la musique en cours (passer par Music Assistant pour ça) ;
  - `snapcast` : JSON-RPC du serveur (`:1705`) : le groupe passe sur le flux « TTS » (source `tcp` du serveur), le PCM
    y est poussé, puis le groupe revient à son flux d'avant ;
  - `airplay` : `pyatv` (paquet facultatif de l'image de la passerelle), `stream_file` de l'extrait.
Toute destination passe par la liste blanche. Les extraits vivent en mémoire 2 minutes, sous un identifiant tiré au
hasard ; rien n'est écrit sur disque.
"""
from __future__ import annotations

import asyncio
import secrets
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from xml.sax.saxutils import escape

CLIP_TTL_S = 120.0
MAX_AUDIO = 8 * 1024 * 1024
PCM_RATE, PCM_CHANNELS, PCM_BYTES = 48000, 2, 2  # format par défaut d'une source tcp de snapserver (48000:16:2)


class SinkError(RuntimeError):
    pass


@dataclass
class ClipStore:
    clock: Callable[[], float] = time.monotonic
    _clips: dict[str, tuple[bytes, str, float]] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def put(self, audio: bytes, mime: str) -> str:
        cid = secrets.token_urlsafe(24)
        with self._lock:
            now = self.clock()
            for k in [k for k, v in self._clips.items() if v[2] < now]:
                self._clips.pop(k, None)
            self._clips[cid] = (audio, mime, now + CLIP_TTL_S)
        return cid

    def get(self, cid: str) -> tuple[bytes, str] | None:
        with self._lock:
            v = self._clips.get(cid)
            if v is None or v[2] < self.clock():
                return None
            return v[0], v[1]


class Sink(Protocol):
    kind: str

    def play(self, audio: bytes, mime: str, clip_url: str) -> None: ...


@dataclass
class MusicAssistantSink:
    ha: Any  # nokker.homebridge.ha.HomeAssistant
    target: str
    kind: str = "music_assistant"

    def play(self, audio: bytes, mime: str, clip_url: str) -> None:
        if not clip_url:
            raise SinkError("lan.url absent : Home Assistant ne peut pas lire l'extrait")
        self.ha.call_service("media_player", "play_media", {
            "entity_id": self.target, "media_content_id": clip_url, "media_content_type": "music", "announce": True})


_SOAP = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
         's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
         '<u:{action} xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">{args}</u:{action}></s:Body></s:Envelope>')


@dataclass
class SonosSink:
    lan: Any  # Lan
    host: str
    port: int = 1400
    kind: str = "sonos"

    def _soap(self, action: str, args: str) -> None:
        body = _SOAP.format(action=action, args=args).encode("utf-8")
        status, _ = self.lan.http("POST", f"http://{self.host}:{self.port}/MediaRenderer/AVTransport/Control", body, {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"'})
        if status >= 400:
            raise SinkError(f"Sonos {action} : réponse {status}")

    def play(self, audio: bytes, mime: str, clip_url: str) -> None:
        if not clip_url:
            raise SinkError("lan.url absent : le lecteur Sonos ne peut pas lire l'extrait")
        self._soap("SetAVTransportURI", f"<InstanceID>0</InstanceID><CurrentURI>{escape(clip_url)}</CurrentURI>"
                                        "<CurrentURIMetaData></CurrentURIMetaData>")
        self._soap("Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")


def ffmpeg_to_pcm(audio: bytes) -> bytes:
    """OGG/Opus → PCM s16le 48 kHz stéréo (ffmpeg de l'image de la passerelle)."""
    done = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-f", "s16le", "-ar", str(PCM_RATE),
                           "-ac", str(PCM_CHANNELS), "pipe:1"], input=audio, capture_output=True, timeout=30,
                          check=False, env={"PATH": "/usr/bin:/bin"})
    if done.returncode != 0 or not done.stdout:
        raise SinkError("décodage audio en échec")
    return done.stdout


@dataclass
class SnapcastSink:
    lan: Any  # Lan
    host: str
    group: str
    stream: str = "tts"
    rpc_port: int = 1705
    source_port: int = 4953
    decode: Callable[[bytes], bytes] = ffmpeg_to_pcm
    sleep: Callable[[float], None] = time.sleep
    kind: str = "snapcast"
    _id: int = 0

    def _rpc(self, sock: Any, method: str, params: dict[str, Any] | None = None) -> Any:
        import json

        self._id += 1
        msg = {"id": self._id, "jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        sock.sendall((json.dumps(msg) + "\r\n").encode("utf-8"))
        buf = b""
        while True:  # réponses et notifications, une par ligne : on attend la nôtre
            chunk = sock.recv(65536)
            if not chunk:
                raise SinkError("Snapcast : connexion fermée")
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    out = json.loads(line)
                except ValueError:
                    continue
                if isinstance(out, dict) and out.get("id") == self._id:
                    if "error" in out:
                        raise SinkError(f"Snapcast {method} : refus")
                    return out.get("result")

    def play(self, audio: bytes, mime: str, clip_url: str) -> None:
        pcm = self.decode(audio)
        rpc = self.lan.tcp(self.host, self.rpc_port)
        try:
            status = self._rpc(rpc, "Server.GetStatus") or {}
            groups = ((status.get("server") or {}).get("groups")) or []
            prev = next((g.get("stream_id") for g in groups if g.get("id") == self.group), None)
            if prev is None:
                raise SinkError("Snapcast : groupe inconnu")
            self._rpc(rpc, "Group.SetStream", {"id": self.group, "stream_id": self.stream})
            try:
                src = self.lan.tcp(self.host, self.source_port)
                try:
                    src.sendall(pcm)
                finally:
                    src.close()
                self.sleep(len(pcm) / (PCM_RATE * PCM_CHANNELS * PCM_BYTES) + 0.5)
            finally:
                if prev != self.stream:
                    self._rpc(rpc, "Group.SetStream", {"id": self.group, "stream_id": prev})
        finally:
            rpc.close()


def pyatv_stream(ip: str, url: str) -> None:  # pragma: no cover — appareil réel
    try:
        import pyatv
    except ImportError as e:
        raise SinkError("pyatv absent de l'image de la passerelle") from e

    async def go() -> None:
        loop = asyncio.get_running_loop()
        confs = await pyatv.scan(loop, hosts=[ip], timeout=5)
        if not confs:
            raise SinkError("AirPlay : appareil introuvable")
        atv = await pyatv.connect(confs[0], loop)
        try:
            await atv.stream.stream_file(url)
        finally:
            atv.close()

    asyncio.run(go())


@dataclass
class AirPlaySink:
    allow: Any  # Allowlist
    host: str
    port: int = 7000
    stream: Callable[[str, str], None] = pyatv_stream
    kind: str = "airplay"

    def play(self, audio: bytes, mime: str, clip_url: str) -> None:
        if not clip_url:
            raise SinkError("lan.url absent : l'appareil AirPlay ne peut pas lire l'extrait")
        ip = self.allow.check(self.host, self.port)
        self.stream(ip, clip_url)


def build_sink(spec: dict[str, Any], *, lan: Any, ha: Any) -> Sink:
    kind = str(spec.get("sink") or "")
    if kind == "music_assistant":
        if ha is None:
            raise SinkError("music_assistant : home_assistant absent de home.yaml")
        return MusicAssistantSink(ha=ha, target=str(spec["target"]))
    if kind == "sonos":
        return SonosSink(lan=lan, host=str(spec["host"]), port=int(spec.get("port", 1400)))
    if kind == "snapcast":
        return SnapcastSink(lan=lan, host=str(spec["host"]), group=str(spec["group"]),
                            stream=str(spec.get("stream") or "tts"), rpc_port=int(spec.get("rpc_port", 1705)),
                            source_port=int(spec.get("source_port", 4953)))
    if kind == "airplay":
        return AirPlaySink(allow=lan.allow, host=str(spec["host"]), port=int(spec.get("port", 7000)))
    raise SinkError(f"enceinte de type inconnu : {kind!r}")


@dataclass
class AudioRouter:
    rooms: dict[str, list[Sink]]
    clips: ClipStore
    lan_url: str = ""

    def play(self, room: str, audio: bytes, mime: str) -> dict[str, Any]:
        sinks = self.rooms.get(room) or []
        if not sinks:
            return {"played": False, "reason": "no_speaker"}
        if not audio or len(audio) > MAX_AUDIO:
            return {"played": False, "reason": "bad_audio"}
        cid = self.clips.put(audio, mime)
        clip_url = f"{self.lan_url}/clip/{cid}" if self.lan_url else ""
        errors = []
        for s in sinks:
            try:
                s.play(audio, mime, clip_url)
                return {"played": True, "sinks": [s.kind]}
            except Exception as e:  # noqa: BLE001 — essayer l'enceinte suivante ; message sans secret
                errors.append(f"{s.kind}:{type(e).__name__}")
        return {"played": False, "reason": "sinks_failed", "errors": errors}


__all__ = ["AirPlaySink", "AudioRouter", "ClipStore", "MusicAssistantSink", "SinkError", "SnapcastSink",
           "SonosSink", "build_sink"]
