"""Charge d'une machine Lokkal (lot X5, docs/LOKKAL.md §7.2) : mesure locale, envoi à nokker.ai, nettoyage côté nokker.ai.

- **Mesure** (`measure`) : RAM totale et libre (limite cgroup v2 du container s'il y en a une, sinon `/proc/meminfo`),
  CPU (nombre effectif, charge 1 min), GPU si `nvidia-smi` est présent (sinon liste vide, jamais d'échec), sessions de
  code ouvertes et fils dont un tour est en cours, version du noyau. Lecture de fichiers de `/proc` et `/sys` seulement :
  quelques millisecondes ; `nvidia-smi` (un processus, borné à 2 s) ne tourne que dans le fil de fond, pas en requête.
- **Envoi** (`LoadReporter`, un fil de fond) : `POST /v1/gateway/machine/load` (Bearer jeton de machine) toutes les
  60 s et à chaque ouverture du tunnel (`kick`). Pas de jeton (machine non liée) : rien n'est envoyé. nokker.ai
  injoignable, jeton révoqué : on retente au passage suivant, sans bruit (journal en DEBUG).
- **Nettoyage** (`clean_load`) : nokker.ai ne garde que les champs connus, bornés ; la machine n'est pas crue sur
  la forme de ce qu'elle envoie.
- **Inventaire des ressources** (lot X21, `nokker/local/resources.py`) : envoyé dans le même fil, à
  `POST /v1/gateway/machine/resources`, quand il change et au moins toutes les 10 min ; noms seulement.
"""
from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import hashlib
import json
import time

import httpx

logger = logging.getLogger("nokker.local.load")

LOAD_VERSION = 1  # forme de la charge (docs/LOKKAL.md §7.2) ; un champ ajouté ne change pas ce numéro
INTERVAL_S = 60.0
RESOURCES_EVERY_S = 600.0  # inventaire renvoyé au moins toutes les 10 min, même inchangé (nokker.ai le date)
DECK_EVERY_S = 300.0  # deck (X15) renvoyé au moins toutes les 5 min, même inchangé ; sinon à chaque changement
MAX_GPUS = 16
_GPU_QUERY = ("--query-gpu=name,memory.total,memory.free,utilization.gpu", "--format=csv,noheader,nounits")


# ─────────────────────────────── mesure ───────────────────────────────


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _meminfo(proc: Path) -> dict[str, int]:
    """Valeurs de `/proc/meminfo` en Kio."""
    out: dict[str, int] = {}
    for line in (_read(proc / "meminfo") or "").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[key.strip()] = int(parts[0])
    return out


def _cgroup_dirs(proc: Path, cgroup_root: Path) -> list[Path]:
    """Dossiers cgroup v2 du processus, du plus précis au plus large : celui de `/proc/self/cgroup` (hôte, slice
    systemd), puis la racine montée (container avec son propre espace de noms cgroup)."""
    dirs = []
    for line in (_read(proc / "self" / "cgroup") or "").splitlines():
        if line.startswith("0::"):
            rel = line[3:].strip().lstrip("/")
            if rel:
                dirs.append(cgroup_root / rel)
    dirs.append(cgroup_root)
    return dirs


def _cgroup_memory(dirs: list[Path]) -> tuple[int, int] | None:
    """(limite, utilisé hors cache de fichiers inactif) en octets, au premier cgroup qui pose une limite."""
    for d in dirs:
        limit = _read(d / "memory.max")
        if not limit or limit == "max" or not limit.isdigit():
            continue
        current = _read(d / "memory.current")
        if not current or not current.isdigit():
            continue
        inactive = 0
        for line in (_read(d / "memory.stat") or "").splitlines():
            k, _, v = line.partition(" ")
            if k == "inactive_file" and v.strip().isdigit():
                inactive = int(v)
        return int(limit), max(0, int(current) - inactive)
    return None


def _cgroup_cpus(dirs: list[Path]) -> float | None:
    """Nombre de CPU accordé par `cpu.max` (quota / période), au premier cgroup qui pose un quota."""
    for d in dirs:
        raw = _read(d / "cpu.max")
        if not raw:
            continue
        quota, _, period = raw.partition(" ")
        if quota == "max" or not quota.isdigit() or not period.strip().isdigit() or int(period) == 0:
            continue
        return round(int(quota) / int(period), 2)
    return None


def _host_cpus() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def _int(s: str) -> int | None:
    try:
        return int(float(s.strip()))
    except ValueError:
        return None  # « [N/A] », « [Not Supported] »


def nvidia_gpus(run: Callable[..., Any] = subprocess.run, which: Callable[[str], str | None] = shutil.which,
                timeout_s: float = 2.0) -> list[dict[str, Any]]:
    """GPU NVIDIA par `nvidia-smi` (lecture d'état seulement) ; liste vide sans l'outil ou s'il échoue."""
    exe = which("nvidia-smi")
    if not exe:
        return []
    try:
        r = run([exe, *_GPU_QUERY], capture_output=True, text=True, timeout=timeout_s, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if getattr(r, "returncode", 1) != 0:
        return []
    gpus = []
    for line in (r.stdout or "").splitlines():
        cols = [c.strip() for c in line.split(",")]
        if len(cols) < 4 or not cols[0]:
            continue
        gpus.append({"name": cols[0][:80], "memory_total_mb": _int(cols[1]), "memory_free_mb": _int(cols[2]),
                     "util_pct": _int(cols[3])})
    return gpus[:MAX_GPUS]


def measure(*, version: str = "", sessions: int = 0, active_turns: int = 0,
            gpus: Callable[[], list[dict[str, Any]]] | None = None, proc: Path = Path("/proc"),
            cgroup_root: Path = Path("/sys/fs/cgroup"),
            loadavg: Callable[[], tuple[float, float, float]] = os.getloadavg,
            host_cpus: Callable[[], int] = _host_cpus) -> dict[str, Any]:
    """La charge de cette machine, forme exacte : docs/LOKKAL.md §7.2. `gpus` : None = pas de GPU mesuré (la requête
    en direct reste légère) ; le fil d'envoi passe `nvidia_gpus`."""
    dirs = _cgroup_dirs(proc, cgroup_root)
    info = _meminfo(proc)
    host_total = info.get("MemTotal", 0) * 1024
    host_free = info.get("MemAvailable", info.get("MemFree", 0)) * 1024
    ram_source, total, free = "host", host_total, host_free
    cg = _cgroup_memory(dirs)
    if cg is not None and (not host_total or cg[0] < host_total):  # une limite au-dessus de l'hôte n'en est pas une
        ram_source, total = "cgroup", cg[0]
        free = max(0, cg[0] - cg[1])
        if host_total:
            free = min(free, host_free)  # l'hôte peut manquer de mémoire avant que le container n'atteigne sa limite
    cpus, cpu_source = _cgroup_cpus(dirs), "cgroup"
    count = host_cpus()
    if cpus is None or cpus >= count:
        cpus, cpu_source = count, "host"
    try:
        load1 = round(float(loadavg()[0]), 2)
    except (OSError, AttributeError):
        load1 = None
    try:
        gpu_list = list(gpus()) if gpus is not None else []
    except Exception:  # noqa: BLE001 — un GPU illisible n'empêche pas d'envoyer la charge
        gpu_list = []
    return {"v": LOAD_VERSION, "version": version,
            "ram": {"total_mb": total // (1024 * 1024), "free_mb": free // (1024 * 1024), "source": ram_source},
            "cpu": {"count": cpus, "load1": load1, "source": cpu_source},
            "gpus": gpu_list, "sessions": int(sessions), "active_turns": int(active_turns)}


# ─────────────────────────────── fils actifs ───────────────────────────────


@dataclass
class ActiveTurns:
    """Fils dont un tour est en cours : `wrap(kernel.run_turn)` compte chaque tour, exception comprise."""

    _by_thread: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def wrap(self, run_turn: Callable[..., Any]) -> Callable[..., Any]:
        def counted(inp: Any, *args: Any, **kwargs: Any) -> Any:
            tid = str(getattr(inp, "thread_id", "") or "")
            with self._lock:
                self._by_thread[tid] = self._by_thread.get(tid, 0) + 1
            try:
                return run_turn(inp, *args, **kwargs)
            finally:
                with self._lock:
                    n = self._by_thread.get(tid, 1) - 1
                    if n > 0:
                        self._by_thread[tid] = n
                    else:
                        self._by_thread.pop(tid, None)

        return counted

    def count(self) -> int:
        with self._lock:
            return len(self._by_thread)

    def threads(self) -> set[str]:
        """Les fils dont un tour est en cours (deck X15 : session « working »)."""
        with self._lock:
            return set(self._by_thread)


# ─────────────────────────────── envoi ───────────────────────────────


@dataclass
class LoadReporter:
    base_url: str  # https://app.nokker.ai
    token: Callable[[], str | None]  # jeton de machine (coffre local) ; None ou vide : machine non liée
    measure: Callable[[], dict[str, Any]]
    interval_s: float = INTERVAL_S
    client: Any = None  # httpx.Client injectable (tests)
    last_status: int | None = None  # dernier statut HTTP de nokker.ai (None : rien envoyé)
    resources: Callable[[], dict[str, Any]] | None = None  # inventaire par noms (X21) ; None = pas d'inventaire
    resources_every_s: float = RESOURCES_EVERY_S
    deck: Callable[[], dict[str, Any]] | None = None  # deck de la machine (X15, nokker/local/crew.py) ; None = aucun
    deck_every_s: float = DECK_EVERY_S
    _res_sha: str | None = field(default=None, init=False, repr=False)
    _res_at: float = field(default=0.0, init=False, repr=False)
    _deck_sha: str | None = field(default=None, init=False, repr=False)
    _deck_at: float = field(default=0.0, init=False, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _kick: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    def start(self) -> None:
        threading.Thread(target=self._run, name="nokker-load", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._kick.set()

    def kick(self) -> None:
        """Envoi immédiat (ouverture du tunnel) ; sans attendre, depuis n'importe quel fil."""
        self._kick.set()

    def send_once(self) -> bool:
        """Mesure et envoie ; True si nokker.ai l'a gardée. Jamais d'exception."""
        try:
            token = self.token()
            if not token:
                return False
            load = self.measure()
        except Exception:
            logger.debug("charge : jeton ou mesure en échec", exc_info=True)
            return False
        if self.client is None:
            self.client = httpx.Client(timeout=10.0)  # un seul client pour la vie du fil
        try:
            r = self.client.post(
                self.base_url.rstrip("/") + "/v1/gateway/machine/load", json=load,
                headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            logger.debug("charge : envoi reporté (%s)", type(e).__name__)
            return False
        self.last_status = r.status_code
        if r.status_code != 200:
            logger.debug("charge : refusée par nokker.ai (HTTP %d)", r.status_code)
        self.send_resources(token)
        self.send_deck(token)
        return r.status_code == 200

    def send_resources(self, token: str, force: bool = False) -> bool:
        """Inventaire des ressources `local` (noms seulement), s'il a changé ou s'il date de plus de 10 min. True si
        nokker.ai l'a gardé. Jamais d'exception."""
        if self.resources is None:
            return False
        try:
            inv = self.resources()
        except Exception:  # noqa: BLE001
            logger.debug("inventaire : collecte en échec", exc_info=True)
            return False
        sha = hashlib.sha256(json.dumps(inv, sort_keys=True).encode()).hexdigest()
        if not force and sha == self._res_sha and time.monotonic() - self._res_at < self.resources_every_s:
            return False
        if self.client is None:
            self.client = httpx.Client(timeout=10.0)
        try:
            r = self.client.post(self.base_url.rstrip("/") + "/v1/gateway/machine/resources", json=inv,
                                 headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            logger.debug("inventaire : envoi reporté (%s)", type(e).__name__)
            return False
        if r.status_code == 200:
            self._res_sha, self._res_at = sha, time.monotonic()
        return r.status_code == 200

    def send_deck(self, token: str, force: bool = False) -> bool:
        """Deck de la machine (X15 : sessions, agents, cartes en attente — noms et états), s'il a changé ou s'il date de
        plus de 5 min. True si nokker.ai l'a gardé. Jamais d'exception."""
        if self.deck is None:
            return False
        try:
            deck = self.deck()
        except Exception:  # noqa: BLE001
            logger.debug("deck : collecte en échec", exc_info=True)
            return False
        sha = hashlib.sha256(json.dumps(deck, sort_keys=True, default=str).encode()).hexdigest()
        if not force and sha == self._deck_sha and time.monotonic() - self._deck_at < self.deck_every_s:
            return False
        if self.client is None:
            self.client = httpx.Client(timeout=10.0)
        try:
            r = self.client.post(self.base_url.rstrip("/") + "/v1/gateway/machine/deck", json=deck,
                                 headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            logger.debug("deck : envoi reporté (%s)", type(e).__name__)
            return False
        if r.status_code == 200:
            self._deck_sha, self._deck_at = sha, time.monotonic()
        return r.status_code == 200

    def _run(self) -> None:
        while not self._stop.is_set():
            self.send_once()
            self._kick.wait(self.interval_s)
            self._kick.clear()


# ─────────────────────────────── côté nokker.ai ───────────────────────────────


def _num(v: Any, lo: float = 0, hi: float = 1e9, integer: bool = True) -> int | float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if math.isnan(v) or v < lo or v > hi:
        return None
    return int(v) if integer else round(float(v), 2)


def clean_load(raw: Any) -> dict[str, Any] | None:
    """La charge reçue d'une machine, réduite aux champs connus et bornés (forme : docs/LOKKAL.md §7.2) ; None si ce
    n'est pas un objet portant au moins `ram` ou `cpu`."""
    if not isinstance(raw, dict) or not (isinstance(raw.get("ram"), dict) or isinstance(raw.get("cpu"), dict)):
        return None
    ram = raw.get("ram") if isinstance(raw.get("ram"), dict) else {}
    cpu = raw.get("cpu") if isinstance(raw.get("cpu"), dict) else {}
    gpus = []
    for g in (raw.get("gpus") if isinstance(raw.get("gpus"), list) else [])[:MAX_GPUS]:
        if isinstance(g, dict):
            gpus.append({"name": str(g.get("name") or "")[:80], "memory_total_mb": _num(g.get("memory_total_mb")),
                         "memory_free_mb": _num(g.get("memory_free_mb")), "util_pct": _num(g.get("util_pct"), hi=100)})
    source = {"cgroup", "host"}
    return {"v": _num(raw.get("v"), hi=1000) or LOAD_VERSION, "version": str(raw.get("version") or "")[:40],
            "ram": {"total_mb": _num(ram.get("total_mb")), "free_mb": _num(ram.get("free_mb")),
                    "source": ram.get("source") if ram.get("source") in source else None},
            "cpu": {"count": _num(cpu.get("count"), hi=4096, integer=False), "load1": _num(cpu.get("load1"),
                                                                                           hi=1e5, integer=False),
                    "source": cpu.get("source") if cpu.get("source") in source else None},
            "gpus": gpus, "sessions": _num(raw.get("sessions"), hi=1e5) or 0,
            "active_turns": _num(raw.get("active_turns"), hi=1e5) or 0}


__all__ = ["INTERVAL_S", "LOAD_VERSION", "ActiveTurns", "LoadReporter", "clean_load", "measure", "nvidia_gpus"]
