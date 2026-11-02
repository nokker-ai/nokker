"""Service d'écoute du heurtoir (conteneur `home-audio` : `network_mode: none`, `/dev/snd`) — docs/KNOCK.md §4.

Boucle : micro → tampon circulaire EN MÉMOIRE de quelques secondes → détecteur → au déclencheur, UN événement
(« toc détecté », pièce, heure, confiance) poussé à home-bridge par la socket Unix `/run/home/audio.sock`.

Vie privée (exigences produit) :
- aucun son n'est écrit sur disque ni envoyé, avant comme après le déclencheur : ce service ne sait envoyer que des
  événements (le flux de la session vocale, quand il existera, sera une opération distincte demandée par le noyau) ;
- le tampon est remis à zéro au déclencheur, au mute et à l'arrêt ;
- un indicateur visible (LED ou script) suit l'état `listening` / `muted` / `off` ;
- mute logiciel (commande `knock.mute` relayée par home-bridge, ou fichier `mute_file`) et mute matériel (interrupteur
  du micro lu par `hw_mute_cmd`, ou GPIO) : le mute matériel ne se lève pas par logiciel.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from nokker.homebridge.knock.detector import KnockEvent
from nokker.homebridge.knock.v2 import KnockDetectorV2, KnockSettingsV2

logger = logging.getLogger("nokker.homebridge.knock.audio")


class RingBuffer:
    """Tampon circulaire float32 de `seconds` secondes. Jamais copié ailleurs, jamais écrit sur disque."""

    def __init__(self, seconds: float, sample_rate: int):
        self.buf = np.zeros(max(1, int(seconds * sample_rate)), dtype=np.float32)
        self.pos = 0
        self.filled = 0

    def write(self, x: np.ndarray) -> None:
        x = np.asarray(x, dtype=np.float32).reshape(-1)[-self.buf.size:]
        n = x.size
        end = self.pos + n
        if end <= self.buf.size:
            self.buf[self.pos:end] = x
        else:
            k = self.buf.size - self.pos
            self.buf[self.pos:] = x[:k]
            self.buf[: n - k] = x[k:]
        self.pos = end % self.buf.size
        self.filled = min(self.buf.size, self.filled + n)

    def wipe(self) -> None:
        self.buf.fill(0.0)
        self.pos = 0
        self.filled = 0

    def is_blank(self) -> bool:
        return self.filled == 0 and not self.buf.any()


def arecord_blocks(device: str, sample_rate: int, block: int) -> Iterator[np.ndarray]:
    """Capture ALSA par `arecord` (S16_LE mono, sur la sortie standard, jamais dans un fichier)."""
    argv = ["arecord", "-q", "-D", device, "-f", "S16_LE", "-c", "1", "-r", str(sample_rate), "-t", "raw"]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        assert proc.stdout is not None
        while True:
            data = proc.stdout.read(block * 2)
            if not data:
                break
            yield np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    finally:
        proc.terminate()


@dataclass
class AudioConfig:
    room: str
    device: str = "default"
    sample_rate: int = 16000
    block: int = 320                   # 20 ms
    ring_s: float = 3.0
    socket_path: str = "/run/home/audio.sock"
    mute_file: str = ""                # présent = mute logiciel ; défaut /run/home/knock-mute-<pièce> (op knock.mute)
    hw_mute_cmd: list[str] = field(default_factory=list)  # argv qui imprime 1 si l'interrupteur coupe le micro
    led_path: str = ""                 # /sys/class/leds/<x>/brightness : 1 = écoute
    indicator_cmd: list[str] = field(default_factory=list)  # argv + état en dernier argument
    settings: KnockSettingsV2 = field(default_factory=KnockSettingsV2)


class Listener:
    """L'écoute d'UNE pièce. `feed` est séparé de la capture pour les tests (aucun micro requis)."""

    def __init__(self, cfg: AudioConfig, emit: Callable[[dict], None], clock: Callable[[], float] = time.time):
        if not cfg.mute_file:
            cfg.mute_file = str(Path(cfg.socket_path).parent / f"knock-mute-{cfg.room}")
        self.cfg = cfg
        self.emit = emit
        self.clock = clock
        self.ring = RingBuffer(cfg.ring_s, cfg.sample_rate)
        self.detector = KnockDetectorV2(cfg.sample_rate, cfg.settings)
        self.state = "off"
        self.soft_muted = False
        self.hw_muted = False
        self._t0: float | None = None  # horloge murale du premier échantillon du détecteur

    # ------------------------------------------------------------------------------------------------ état -----
    def _set_state(self, state: str) -> None:
        if state == self.state:
            return
        self.state = state
        self._indicate(state)
        self.emit({"type": "state", "room": self.cfg.room, "state": state, "at": self.clock()})

    def _indicate(self, state: str) -> None:
        if self.cfg.led_path:
            try:
                Path(self.cfg.led_path).write_text("1" if state == "listening" else "0")
            except OSError as e:
                logger.warning("voyant %s : %s", self.cfg.led_path, e)
        if self.cfg.indicator_cmd:
            try:
                subprocess.run([*self.cfg.indicator_cmd, state], timeout=2, check=False)
            except (OSError, subprocess.SubprocessError) as e:
                logger.warning("indicateur : %s", e)

    def set_soft_mute(self, muted: bool) -> None:
        self.soft_muted = bool(muted)
        self._apply_mute()

    def _apply_mute(self) -> None:
        if self.soft_muted or self.hw_muted:
            self.ring.wipe()
            self.detector.reset()
            self._t0 = None
            self._set_state("muted")
        else:
            self._set_state("listening")

    def poll_mutes(self) -> None:
        soft = bool(self.cfg.mute_file) and os.path.exists(self.cfg.mute_file)
        hw = False
        if self.cfg.hw_mute_cmd:
            try:
                out = subprocess.run(self.cfg.hw_mute_cmd, capture_output=True, text=True, timeout=2, check=False)
                hw = out.stdout.strip() in ("1", "on", "muted", "true")
            except (OSError, subprocess.SubprocessError):
                hw = True  # interrupteur illisible : on considère le micro coupé (échec fermé)
        if soft != self.soft_muted or hw != self.hw_muted or self.state == "off":
            self.soft_muted, self.hw_muted = soft, hw
            self._apply_mute()

    # ----------------------------------------------------------------------------------------------- audio -----
    def feed(self, block: np.ndarray) -> list[dict]:
        if self.state != "listening":
            return []  # muet : le bloc est jeté sans être lu
        if self._t0 is None:
            self._t0 = self.clock()
        self.ring.write(block)
        out = []
        for ev in self.detector.process(block):
            out.append(self._fire(ev))
        return out

    def _fire(self, ev: KnockEvent) -> dict:
        hf = float(np.mean([h.features["hf"] for h in ev.hits]))
        kind = "clap" if hf >= -6.0 else "knock"  # un clap met plus d'un quart de son énergie au-dessus de 2 kHz
        msg = {"type": "knock", "event_id": f"{self.cfg.room}-{uuid.uuid4().hex[:16]}", "kind": kind,
               "room": self.cfg.room, "at": round((self._t0 or self.clock()) + ev.t, 3),
               "source": f"mic:{self.cfg.room}", "confidence": ev.confidence,
               "intervals": list(ev.intervals)}
        # Rien de ce qui a précédé le déclencheur ne survit (ni son, ni état du détecteur).
        self.ring.wipe()
        self.detector.reset()
        self._t0 = None
        self.emit(msg)
        return msg

    def stop(self) -> None:
        self.ring.wipe()
        self.detector.reset()
        self._set_state("off")


class UnixEmitter:
    """Envoie des lignes JSON à home-bridge (`/run/home/audio.sock`). Reconnecte au besoin ; perd plutôt que d'attendre."""

    def __init__(self, path: str):
        self.path = path
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()

    def __call__(self, msg: dict) -> None:
        line = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
        with self._lock:
            for _ in range(2):
                try:
                    if self._sock is None:
                        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        s.settimeout(2.0)
                        s.connect(self.path)
                        self._sock = s
                    self._sock.sendall(line)
                    return
                except OSError as e:
                    logger.warning("home-bridge injoignable (%s) : %s", self.path, e)
                    if self._sock is not None:
                        self._sock.close()
                    self._sock = None


def run(cfg: AudioConfig, blocks: Iterator[np.ndarray] | None = None, emit: Callable[[dict], None] | None = None,
        stop: threading.Event | None = None) -> None:
    emit = emit or UnixEmitter(cfg.socket_path)
    lis = Listener(cfg, emit)
    stop = stop or threading.Event()
    lis.poll_mutes()
    last_poll = time.monotonic()
    try:
        for block in blocks or arecord_blocks(cfg.device, cfg.sample_rate, cfg.block):
            if stop.is_set():
                break
            if time.monotonic() - last_poll >= 1.0:
                lis.poll_mutes()
                last_poll = time.monotonic()
            lis.feed(block)
    finally:
        lis.stop()


class SatelliteEmitter:
    """Satellite (Raspberry Pi) : le détecteur tourne SUR le satellite ; seul l'événement part vers l'écoute du foyer
    de home-bridge (`POST /knock/event`, jeton du satellite). Les états restent locaux (voyant du satellite)."""

    def __init__(self, url: str, sat_id: str, token: str):
        self.url = url.rstrip("/") + "/knock/event"
        self.sat_id = sat_id
        self._token = token

    def __call__(self, msg: dict) -> None:
        import urllib.request

        if msg.get("type") != "knock":
            return
        body = {k: msg[k] for k in ("event_id", "kind", "at", "confidence") if k in msg}
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Satellite": self.sat_id,
                                              "Authorization": f"Bearer {self._token}"})
        try:
            urllib.request.urlopen(req, timeout=3).close()
        except OSError as e:
            logger.warning("passerelle injoignable : %s", type(e).__name__)


def _config_from_env(env: dict[str, str]) -> AudioConfig:
    import shlex

    room = env.get("KNOCK_ROOM", "")
    if not room:
        raise SystemExit("KNOCK_ROOM requis (pièce de ce micro, [a-z0-9_])")
    settings = KnockSettingsV2.from_dict(json.loads(env["KNOCK_SETTINGS"])) if env.get("KNOCK_SETTINGS") else \
        KnockSettingsV2()
    return AudioConfig(room=room, device=env.get("KNOCK_DEVICE", "default"),
                       sample_rate=int(env.get("KNOCK_RATE", "16000")),
                       socket_path=env.get("KNOCK_SOCKET", "/run/home/audio.sock"),
                       hw_mute_cmd=shlex.split(env.get("KNOCK_HW_MUTE_CMD", "")),
                       led_path=env.get("KNOCK_LED", ""),
                       indicator_cmd=shlex.split(env.get("KNOCK_INDICATOR_CMD", "")),
                       settings=settings)


def calibrate_main(cfg: AudioConfig, seconds: float = 30.0) -> int:
    """« Frappe trois fois » : écoute `seconds` s, imprime les réglages calibrés (JSON) à poser dans KNOCK_SETTINGS."""
    from dataclasses import replace

    from nokker.homebridge.knock.calibrate import calibrate_v2
    from nokker.homebridge.knock.detector import Hit

    hits: list[Hit] = []
    det = KnockDetectorV2(cfg.sample_rate, replace(cfg.settings, odf_min_db=2.5, prob_min=0.0), on_hit=hits.append)
    print("Frappe trois fois (ou trois claps), à ton rythme habituel…", flush=True)
    t_end = time.monotonic() + seconds
    for block in arecord_blocks(cfg.device, cfg.sample_rate, cfg.block):
        det.process(block)
        if time.monotonic() > t_end:
            break
    s = calibrate_v2(hits, cfg.settings)
    del hits
    if s is None:
        print("Trois coups nets non reconnus : réglages inchangés.", flush=True)
        return 1
    print(json.dumps(s.to_dict()), flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    """`home-bridge audio [calibrate | satellite]` — configuration par variables d'environnement (docs/KNOCK.md §4)."""
    argv = list(argv or [])
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = _config_from_env(dict(os.environ))
    if argv[:1] == ["calibrate"]:
        return calibrate_main(cfg)
    if argv[:1] == ["satellite"]:
        url, sat = os.environ.get("KNOCK_BRIDGE_URL", ""), os.environ.get("KNOCK_SATELLITE_ID", "")
        token_file = os.environ.get("KNOCK_SATELLITE_TOKEN_FILE", "")
        if not (url and sat and token_file):
            raise SystemExit("KNOCK_BRIDGE_URL, KNOCK_SATELLITE_ID et KNOCK_SATELLITE_TOKEN_FILE requis")
        run(cfg, emit=SatelliteEmitter(url, sat, Path(token_file).read_text(encoding="utf-8").strip()))
        return 0
    run(cfg)
    return 0
