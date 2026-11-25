"""Nœud `desktop` (docs/DESKTOP.md §1-§3) : exécute une table FERMÉE d'opérations sur le bureau par le CLI
agent-desktop, pour le noyau Lokkal qui l'a inscrit. Jamais le MCP amont, jamais ses skills, jamais `jev`.

- Binaire épinglé : son sha256 est recalculé au démarrage contre `agent-desktop.lock.json` ; sinon refus.
- Santé : le JSON de `status` (le code retour vaut 0 même en panne) plus `list-windows` (preuve du WM EWMH).
- État amont éphémère : `AGENT_DESKTOP_HOME` dans un dossier privé PAR TÂCHE (une conversation), effacé à la fin
  (inactivité), à l'échéance et au démarrage ; pas de session amont, donc pas de trace ; aucune capture.
- Masquage P9b sur le nœud : tout texte lu à l'écran passe par les règles de l'appareil avant de partir ; les valeurs
  restent dans la mémoire du nœud, pour la tâche.
- Repli : headless, puis physique (`--headed`, puis presse-papiers) seulement si la politique de la tâche le permet.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from nokker.desktop import policy as P
from nokker.desktop.snapshot import Rendered, one_line, render, short_ref

logger = logging.getLogger("nokker.desktop.node")

OPS = ("desktop.status", "desktop.list_windows", "desktop.launch", "desktop.focus", "desktop.snapshot",
       "desktop.find", "desktop.get", "desktop.click", "desktop.type", "desktop.set_value", "desktop.select",
       "desktop.check", "desktop.press", "desktop.wait", "desktop.observe")
ACT_OPS = ("desktop.click", "desktop.type", "desktop.set_value", "desktop.select", "desktop.check", "desktop.press")
WAIT_EVENTS = ("window-opened", "window-closed", "focus-changed")
JOB_IDLE_S = 600.0          # une tâche sans opération pendant 10 min est purgée
CMD_TIMEOUT_S = 30.0
LAUNCH_TIMEOUT_MS = 20_000
TEXT_MAX = 4096             # type / set_value
GET_MAX = 8192              # get value
FIND_MAX = 20
SNAPSHOT_CAP_DEFAULT = 12 * 1024
SNAPSHOT_CAP_MAX = 32 * 1024
KIND_CODE = {"phone": "PHONE", "email": "EMAIL", "iban": "IBAN", "avs": "AHV", "card": "CARD",
             "birthdate": "BIRTHDATE", "address": "ADDRESS", "secret": "SECRET"}
_JOB = re.compile(r"^[0-9a-f]{16,64}$")
_WINDOW = re.compile(r"^[A-Za-z0-9._:\-]{1,200}$")
_SNAP = re.compile(r"^[A-Za-z0-9]{4,40}$")
_EREF = re.compile(r"^e[0-9]{1,6}$")


class NodeError(Exception):
    """Refus ou échec d'une opération : `code` ∈ les clés de `texts.WHY` ; `detail` : champs de mise en forme."""

    def __init__(self, reason: str, /, **detail: Any) -> None:
        # positionnel seul : un détail peut s'appeler `code` (code amont), relevé en B1b (TypeError sur tout échec amont)
        super().__init__(reason)
        self.code, self.detail = reason, detail


# ─────────────────────────────── binaire et appels ───────────────────────────────
def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_pin(binary: str | Path, lock: dict[str, Any]) -> None:
    """Le binaire doit porter le sha256 épinglé de sa plateforme ; sinon `binary_mismatch` (le nœud ne démarre pas)."""
    import platform

    arch = platform.machine().lower()
    want = (lock.get("binaries") or {}).get(f"linux-{'x86_64' if arch in ('x86_64', 'amd64') else arch}", {}) \
        .get("agent-desktop")
    if not want:
        raise NodeError("unsupported_arch", arch=arch)
    got = sha256_file(binary)
    if got != want:
        raise NodeError("binary_mismatch", want=want, got=got)


@dataclass
class AgentDesktop:
    """Appels au CLI : une enveloppe JSON par commande (`{ok, data | error}`). Le texte saisi passe par `--stdin`,
    jamais par les arguments du processus. `home` : `AGENT_DESKTOP_HOME` de la tâche."""

    binary: str = "agent-desktop"
    env: dict[str, str] = field(default_factory=dict)
    runner: Callable[..., Any] = subprocess.run

    def __call__(self, args: list[str], *, home: str | None = None, headed: bool = False, stdin: str | None = None,
                 timeout_s: float = CMD_TIMEOUT_S) -> dict[str, Any]:
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY",
                                                             "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR",
                                                             "HOME", "LANG", "LC_ALL", "AT_SPI_BUS_ADDRESS")}
        env.update(self.env)
        env.pop("AGENT_DESKTOP_SESSION", None)
        if home:
            env["AGENT_DESKTOP_HOME"] = home
        argv = [self.binary, *(["--headed"] if headed else []), *args]
        try:
            r = self.runner(argv, input=stdin, capture_output=True, text=True, timeout=timeout_s, env=env)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": {"code": "TIMEOUT"}}
        except OSError as e:
            return {"ok": False, "error": {"code": "SPAWN_FAILED", "message": str(e)}}
        try:
            out = json.loads(r.stdout or "{}")
        except ValueError:
            return {"ok": False, "error": {"code": "BAD_OUTPUT"}}
        return out if isinstance(out, dict) else {"ok": False, "error": {"code": "BAD_OUTPUT"}}


def health(ad: AgentDesktop) -> dict[str, Any]:
    """`{desktop: on|degraded, reason}` d'après le JSON de `status` et une réponse de `list-windows` (le code retour de
    `status` ne dit rien : 0 même sans bus)."""
    st = ad(["status"])
    data = st.get("data") or {}
    # Forme réelle (1.2.0, relevée en B1b) : data.permissions.accessibility.state ; data.accessibility en repli.
    a11y = (data.get("permissions") or {}).get("accessibility") or data.get("accessibility") or {}
    state = a11y.get("state") if isinstance(a11y, dict) else a11y
    if not st.get("ok") or state != "granted":
        sugg = str((a11y or {}).get("suggestion") or "") if isinstance(a11y, dict) else ""
        return {"desktop": "degraded", "reason": "no_dbus" if "D-Bus" in sugg else "a11y_denied"}
    if not data.get("supported_surfaces"):
        return {"desktop": "degraded", "reason": "no_surfaces"}
    lw = ad(["list-windows"])
    if not lw.get("ok"):
        kind = str(((lw.get("error") or {}).get("details") or {}).get("kind") or "")
        code = str((lw.get("error") or {}).get("code") or "")
        return {"desktop": "degraded", "reason": "no_wm" if "native_window" in kind or code == "ACTION_NOT_SUPPORTED"
                else "list_windows_failed"}
    return {"desktop": "on", "reason": None}


# ─────────────────────────────── masquage P9b ───────────────────────────────
@dataclass
class Masker:
    """Masque les valeurs sensibles d'un texte produit sur la machine (règles de l'appareil, `nokker/redact/rules.py`) :
    chaque valeur devient `⟦CODE-n⟧`, numérotée à partir du plancher du fil donné par le noyau ; la même valeur garde son
    marqueur pendant la tâche. Le dictionnaire ne quitte jamais le nœud."""

    floor: dict[str, int] = field(default_factory=dict)
    values: dict[tuple[str, str], str] = field(default_factory=dict)
    _rules: list[dict[str, Any]] | None = None

    def rules(self) -> list[dict[str, Any]]:
        if self._rules is None:
            from nokker.redact.rules import device_rules

            self._rules = device_rules()["rules"]
        return self._rules

    def mask(self, text: str) -> str:
        if not text:
            return text
        from nokker.redact.rules import find_sensitive

        spans = find_sensitive(text, self.rules())
        if not spans:
            return text
        out, last = [], 0
        for s in spans:
            code = KIND_CODE.get(s["kind"], "SECRET")
            key = (code, s["value"])
            marker = self.values.get(key)
            if marker is None:
                n = self.floor.get(code, 0) + 1
                self.floor[code] = n
                marker = f"⟦{code}-{n}⟧"
                self.values[key] = marker
            out.append(text[last:s["start"]])
            out.append(marker)
            last = s["end"]
        out.append(text[last:])
        return "".join(out)


# ─────────────────────────────── tâches (état éphémère) ───────────────────────────────
@dataclass
class Job:
    id: str
    home: str
    used_at: float
    masker: Masker = field(default_factory=Masker)
    snaps: dict[str, Rendered] = field(default_factory=dict)          # snap_id → rendu (refs et éléments)
    snap_window: dict[str, tuple[str, str]] = field(default_factory=dict)  # snap_id → (fenêtre, appli)
    windows: dict[str, str] = field(default_factory=dict)             # fenêtre → appli
    # Alias courts des fenêtres (w1, w2…) montrés au modèle à la place des id amont : un id amont porte un UUID que le
    # caviardage du noyau prend pour un secret (entropie) dans le tour de suite d'une carte (relevé à la répétition du
    # 06.11 : « fenêtre w-1.11-org-gnome-TextEditor-a11y-[secret:entropy-…] »). Stables pour la tâche.
    alias_of: dict[str, str] = field(default_factory=dict)            # id amont → alias
    real_of: dict[str, str] = field(default_factory=dict)             # alias → id amont


def runtime_root() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(base) / "nokker-desktop"


@dataclass
class Jobs:
    """Une tâche active à la fois (un bureau ne se partage pas). Dossiers sous `root` (tmpfs), 0700."""

    root: Path = field(default_factory=runtime_root)
    clock: Callable[[], float] = time.time
    idle_s: float = JOB_IDLE_S
    _job: Job | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def sweep_all(self) -> None:
        """Au démarrage : tout dossier de tâche restant (crash) est effacé."""
        if self.root.is_dir():
            for p in self.root.iterdir():
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def expire(self) -> None:
        with self._lock:
            j = self._job
            if j is not None and self.clock() - j.used_at > self.idle_s:
                self._end(j)

    def end(self, job_id: str) -> bool:
        with self._lock:
            if self._job is not None and self._job.id == job_id:
                self._end(self._job)
                return True
            return False

    def _end(self, j: Job) -> None:
        shutil.rmtree(j.home, ignore_errors=True)
        j.masker.values.clear()
        j.snaps.clear()
        self._job = None

    def get(self, job_id: str, floor: dict[str, int] | None) -> Job:
        if not _JOB.match(job_id or ""):
            raise NodeError("bad_args", detail="job")
        with self._lock:
            now = self.clock()
            j = self._job
            if j is not None and j.id != job_id:
                if now - j.used_at <= self.idle_s:
                    raise NodeError("busy")
                self._end(j)
                j = None
            if j is None:
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                home = self.root / job_id
                home.mkdir(mode=0o700, exist_ok=True)
                os.chmod(home, 0o700)
                j = Job(id=job_id, home=str(home), used_at=now)
                self._job = j
            for code, n in (floor or {}).items():  # plancher des marqueurs du fil (jamais en arrière)
                if code in KIND_CODE.values() and isinstance(n, int) and n > j.masker.floor.get(code, 0):
                    j.masker.floor[code] = n
            j.used_at = now
            return j


# ─────────────────────────────── exécution ───────────────────────────────
def _err(env: dict[str, Any]) -> str:
    return str((env.get("error") or {}).get("code") or "FAILED")


OBSERVE_MAX_S = 300
OBSERVE_POLL_S = 1.0


@dataclass
class Executor:
    """La table fermée. `handle(op, args, policy)` → résultat JSON (petit, masqué) ; lève `NodeError`.
    `policy` vient du noyau avec chaque tâche : `{apps: [...], physical: allow|never, snapshot_max: octets}` ;
    `mode` est celui du nœud (virtual | session)."""

    ad: AgentDesktop
    jobs: Jobs
    mode: str = "virtual"
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    def handle(self, op: str, args: dict[str, Any], pol: dict[str, Any]) -> dict[str, Any]:
        if op not in OPS:
            raise NodeError("bad_args", detail=f"opération inconnue {op}")
        if op == "desktop.status":
            return {**health(self.ad), "mode": self.mode}
        self.jobs.expire()
        job = self.jobs.get(str(args.get("job") or ""), args.get("marks_floor"))
        apps = tuple(pol.get("apps") or ())
        physical = P.physical_mode(pol.get("physical"), self.mode) == "allow"
        fn = getattr(self, "_" + op.split(".", 1)[1])
        if isinstance(args.get("window"), str) and args["window"]:
            args = {**args, "window": self._real_window(job, args["window"], apps)}
        try:
            return self._aliased(job, fn(job, args, apps, physical, pol))
        except NodeError as e:
            if isinstance(e.detail.get("window"), str):
                e.detail["window"] = self._alias(job, e.detail["window"])
            raise

    # ── alias des fenêtres (frontière avec le noyau) ──
    @staticmethod
    def _alias(job: Job, real: str) -> str:
        if real in job.alias_of:
            return job.alias_of[real]
        alias = f"w{len(job.alias_of) + 1}"
        job.alias_of[real], job.real_of[alias] = alias, real
        return alias

    def _real_window(self, job: Job, window: str, apps: tuple) -> str:
        """Alias → id amont ; un id amont déjà connu passe tel quel ; un alias inconnu relit la liste une fois."""
        if window in job.real_of:
            return job.real_of[window]
        if window in job.alias_of or not re.fullmatch(r"w[0-9]{1,4}", window):
            return window
        self._list_windows(job, {}, apps, False, {})
        return job.real_of.get(window, window)

    def _aliased(self, job: Job, out: dict[str, Any]) -> dict[str, Any]:
        if isinstance(out.get("window"), str) and out["window"]:
            out = {**out, "window": self._alias(job, out["window"])}
        if isinstance(out.get("windows"), list):
            out = {**out, "windows": [{**w, "window": self._alias(job, w["window"])} if isinstance(w, dict) and
                                      isinstance(w.get("window"), str) else w for w in out["windows"]]}
        return out

    # ── lecture ──
    def _list_windows(self, job: Job, args: dict, apps: tuple, physical: bool, pol: dict) -> dict[str, Any]:
        env = self.ad(["list-windows", *(["--app", str(args["app"])] if args.get("app") else [])], home=job.home)
        if not env.get("ok"):
            raise NodeError("failed", code=_err(env))
        rows = []
        data = env.get("data")
        items = data.get("windows") if isinstance(data, dict) else data
        for w in items or ():
            if not isinstance(w, dict):
                continue
            app = str(w.get("app") or w.get("app_name") or w.get("application") or "")
            wid = str(w.get("id") or w.get("window_id") or "")
            if not wid or P.app_verdict(app, apps) is not None:
                continue  # fenêtres d'applis non permises : invisibles
            job.windows[wid] = app
            rows.append({"window": wid, "app": P.norm_app(app),
                         "title": job.masker.mask(one_line(w.get("title"), 120))})
        return {"windows": rows[:50]}

    def _window_app(self, job: Job, window: str, apps: tuple) -> str:
        if not _WINDOW.match(window or ""):
            raise NodeError("bad_args", detail="window")
        app = job.windows.get(window)
        if app is None:
            self._list_windows(job, {}, apps, False, {})
            app = job.windows.get(window)
        if app is None:
            raise NodeError("unknown_window")
        v = P.app_verdict(app, apps)
        if v is not None:
            raise NodeError(v, app=P.norm_app(app))
        return app

    def _snapshot(self, job: Job, args: dict, apps: tuple, physical: bool, pol: dict) -> dict[str, Any]:
        window = str(args.get("window") or "")
        app = self._window_app(job, window, apps)
        cap = min(int(pol.get("snapshot_max") or SNAPSHOT_CAP_DEFAULT), SNAPSHOT_CAP_MAX)
        base = ["snapshot", "--window-id", window, "-i", "--compact"]
        root = args.get("root")
        if root:
            base += ["--root", self._qualified(job, str(args.get("snap_id") or ""), str(root))]
        r = self._snap(job, base, cap, apps, skeleton=False)
        if r.cut and not root:
            r = self._snap(job, [*base, "--skeleton"], cap, apps, skeleton=True)
        if r.cut:
            raise NodeError("snapshot_too_large", total=r.total)
        job.snaps[r.snap_id] = r
        job.snap_window[r.snap_id] = (window, app)
        return {"snap_id": r.snap_id, "window": window, "app": P.norm_app(app), "shown": r.shown, "total": r.total,
                "skeleton": r.skeleton, "text": job.masker.mask(r.text)}

    def _snap(self, job: Job, argv: list[str], cap: int, apps: tuple, *, skeleton: bool) -> Rendered:
        env = self.ad(argv, home=job.home)
        if not env.get("ok"):
            raise NodeError(_code_of(env), code=_err(env))
        data = env.get("data") or {}
        if data.get("app"):  # l'appli que l'amont a VRAIMENT lue fait foi, pas celle de la liste des fenêtres
            v = P.app_verdict(data["app"], apps)
            if v is not None:
                raise NodeError(v, app=P.norm_app(data["app"]))
        return render(env, cap, skeleton=skeleton)

    def _find(self, job: Job, args: dict, apps: tuple, physical: bool, pol: dict) -> dict[str, Any]:
        window = str(args.get("window") or "")
        self._window_app(job, window, apps)
        argv = ["find", "--window-id", window, "--limit", str(FIND_MAX)]
        for k in ("role", "name", "text"):
            if args.get(k):
                argv += [f"--{k}", one_line(args[k], 200)]
        if len(argv) == 6:
            raise NodeError("bad_args", detail="role, name ou text")
        env = self.ad(argv, home=job.home)
        if not env.get("ok"):
            raise NodeError(_code_of(env), code=_err(env))
        rows, snap_id = [], ""
        for m in (env.get("data") or {}).get("matches") or ():
            sr = short_ref(m.get("ref_id"))
            role, name = one_line(m.get("role"), 40), one_line(m.get("name"), 120)
            if sr is not None:
                snap_id = sr[0]
                rows.append(f"{sr[1]} {role} « {name} »" if name else f"{sr[1]} {role}")
                rend = job.snaps.setdefault(sr[0], Rendered(snap_id=sr[0], text="", shown=0, total=0, cut=False,
                                                            skeleton=False))
                from nokker.desktop.snapshot import Element

                st = tuple(str(s).lower() for s in m.get("states") or ())
                rend.refs[sr[1]] = Element(sr[1], role, name, st, P.is_secret_field(role, name, st))
                job.snap_window[sr[0]] = (window, job.windows[window])
            elif name:
                rows.append(f"- {role} « {name} »")
        return {"snap_id": snap_id, "n": len(rows), "text": job.masker.mask("\n".join(rows[:FIND_MAX]))}

    def _get(self, job: Job, args: dict, apps: tuple, physical: bool, pol: dict) -> dict[str, Any]:
        snap_id, ref = str(args.get("snap_id") or ""), str(args.get("ref") or "")
        el = self._element(job, snap_id, ref, apps)
        if el.secret:
            raise NodeError("secure_field")
        prop = str(args.get("property") or "value")
        if prop not in ("value", "name", "states"):
            raise NodeError("bad_args", detail="property")
        env = self.ad(["get", self._qualified(job, snap_id, ref), "--property", prop], home=job.home)
        if not env.get("ok"):
            raise NodeError(_code_of(env), code=_err(env))
        data = env.get("data") or {}
        value = data.get("value", data.get(prop))
        text = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value or "")
        if len(text) > GET_MAX:
            text = text[:GET_MAX] + "…"
        return {"ref": ref, "text": job.masker.mask(text)}

    def _wait(self, job: Job, args: dict, apps: tuple, physical: bool, pol: dict) -> dict[str, Any]:
        event = str(args.get("event") or "")
        if event not in WAIT_EVENTS:
            raise NodeError("bad_args", detail="event")
        timeout_s = max(1, min(30, int(args.get("timeout_s") or 10)))
        argv = ["wait", "--event", event, "--timeout", str(timeout_s * 1000)]
        if args.get("app"):
            v = P.app_verdict(args["app"], apps)
            if v is not None:
                raise NodeError(v, app=P.norm_app(args["app"]))
            argv += ["--app", str(args["app"])]
        env = self.ad(argv, home=job.home, timeout_s=timeout_s + 5)
        if not env.get("ok"):
            if _err(env) in ("TIMEOUT", "WAIT_TIMEOUT"):
                return {"event": event, "timeout_s": timeout_s, "occurred": False}
            raise NodeError(_code_of(env), code=_err(env))
        d = env.get("data") or {}
        w = d.get("window") if isinstance(d.get("window"), dict) else {}
        detail = one_line(w.get("title") or d.get("title") or "", 120)
        return {"event": event, "occurred": True, "detail": job.masker.mask(detail)}

    def _observe(self, job: Job, args: dict, apps: tuple, physical: bool, pol: dict) -> dict[str, Any]:
        """« Je le fais moi-même » (B2, docs/DESKTOP.md §12.1) : regarde SEULEMENT la cible (existence, états, empreinte
        de sa valeur si elle n'est pas secrète) jusqu'au changement ou au délai. Aucune écoute d'événements (GTK4 fait
        fuir la frappe des mots de passe sur le bus) ; rien n'est gardé ni rendu, sauf « changé / pas changé »."""
        import hashlib

        snap_id, ref = str(args.get("snap_id") or ""), str(args.get("ref") or "")
        el = self._element(job, snap_id, ref, apps)
        window, app = job.snap_window[snap_id]
        self._same_app(window, app, args)
        timeout_s = max(1, min(OBSERVE_MAX_S, int(args.get("timeout_s") or OBSERVE_MAX_S)))

        def probe() -> tuple[Any, ...]:
            if el.name:
                env = self.ad(["find", "--window-id", window, "--role", el.role, "--name", el.name, "--exact",
                               "--limit", "2"], home=job.home)
                live = [m for m in (env.get("data") or {}).get("matches") or () if m.get("ref_id")]
                if len(live) != 1:
                    return ("gone", len(live))
                target, states = str(live[0]["ref_id"]), tuple(sorted(str(x) for x in live[0].get("states") or ()))
            else:
                target, states = self._qualified(job, snap_id, ref), ()
            if el.secret or P.is_secret_field(el.role, el.name, states):
                return ("there", states, None)
            got = self.ad(["get", target, "--property", "value"], home=job.home)
            value = (got.get("data") or {}).get("value")
            digest = hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest() if got.get("ok") \
                else None
            return ("there", states, digest)

        base = probe()
        end = self.clock() + timeout_s
        while self.clock() < end:
            self.sleep(OBSERVE_POLL_S)
            if probe() != base:
                return {"changed": True}
        return {"changed": False}

    # ── fenêtres ──
    def _launch(self, job: Job, args: dict, apps: tuple, physical: bool, pol: dict) -> dict[str, Any]:
        app = str(args.get("app") or "")
        v = P.app_verdict(app, apps)
        if v is not None:
            raise NodeError(v, app=P.norm_app(app))
        env = self.ad(["launch", app, "--activate", "--timeout", str(LAUNCH_TIMEOUT_MS)], home=job.home,
                      timeout_s=LAUNCH_TIMEOUT_MS / 1000 + 5)
        if not env.get("ok"):
            raise NodeError(_code_of(env), code=_err(env))
        d = env.get("data") or {}
        w = d.get("window") if isinstance(d.get("window"), dict) else {}
        wid = str(w.get("id") or d.get("window_id") or "")
        if wid:
            job.windows[wid] = app
        return {"app": P.norm_app(app), "window": wid}

    def _focus(self, job: Job, args: dict, apps: tuple, physical: bool, pol: dict) -> dict[str, Any]:
        window = str(args.get("window") or "")
        self._window_app(job, window, apps)
        env = self.ad(["focus-window", "--window-id", window], home=job.home)
        if not env.get("ok"):
            raise NodeError(_code_of(env), code=_err(env))
        return {"window": window}

    # ── actions ──
    def _element(self, job: Job, snap_id: str, ref: str, apps: tuple):
        if not _SNAP.match(snap_id) or snap_id not in job.snaps:
            raise NodeError("unknown_snapshot")
        if not _EREF.match(ref):
            raise NodeError("bad_args", detail="ref")
        window, app = job.snap_window[snap_id]
        v = P.app_verdict(app, apps)
        if v is not None:
            raise NodeError(v, app=P.norm_app(app))
        el = job.snaps[snap_id].refs.get(ref)
        if el is None:
            raise NodeError("stale_target")
        return el

    @staticmethod
    def _same_app(window: str, app: str, args: dict) -> None:
        """L'appli annoncée par l'appel (celle de la carte et de la règle « pour cette session ») doit être celle de
        la fenêtre : sinon une règle donnée pour une appli couvrirait une autre."""
        if P.norm_app(args.get("app")) != P.norm_app(app):
            raise NodeError("wrong_app", window=window, app=P.norm_app(args.get("app")))

    @staticmethod
    def _qualified(job: Job, snap_id: str, ref: str) -> str:
        if not _SNAP.match(snap_id or "") or not _EREF.match(ref or ""):
            raise NodeError("bad_args", detail="ref")
        return f"@{snap_id}:{ref}"

    def _resolve(self, job: Job, args: dict, apps: tuple) -> tuple[str, Any, str]:
        """La cible VIVANTE de l'action : la ref du snapshot doit encore porter le rôle et le nom attendus, puis la
        recherche exacte (rôle, nom) dans la fenêtre doit rendre UN élément (ref fraîche). Nom vide : la ref du
        snapshot telle quelle (l'amont refuse lui-même une identité changée)."""
        snap_id, ref = str(args.get("snap_id") or ""), str(args.get("ref") or "")
        el = self._element(job, snap_id, ref, apps)
        want_role = one_line(args.get("expect_role"), 40)
        want_name = one_line(args.get("expect_name"), 120)
        if (want_role and want_role != el.role) or want_name != el.name:
            raise NodeError("stale_target")
        window, app = job.snap_window[snap_id]
        self._same_app(window, app, args)
        if window != str(args.get("window") or window):
            raise NodeError("stale_target")
        if not el.name:
            return self._qualified(job, snap_id, ref), el, window
        env = self.ad(["find", "--window-id", window, "--role", el.role, "--name", el.name, "--exact",
                       "--limit", "3"], home=job.home)
        if not env.get("ok"):
            raise NodeError(_code_of(env), code=_err(env))
        live = [m for m in (env.get("data") or {}).get("matches") or () if m.get("ref_id")]
        if not live:
            raise NodeError("stale_target")
        if len(live) > 1:
            raise NodeError("ambiguous_target")
        return str(live[0]["ref_id"]), el, window

    def _act(self, job: Job, argv: list[str], *, stdin: str | None, physical: bool,
             fallback_clip: str | None = None, window: str = "") -> tuple[dict[str, Any], str]:
        """Repli fixe : headless, puis `--headed`, puis presse-papiers + ctrl+v (texte seulement), ces deux derniers
        seulement si la politique permet le physique. Rend (enveloppe, mode)."""
        env = self.ad(argv, home=job.home, stdin=stdin)
        if env.get("ok"):
            return env, "headless"
        code = _err(env)
        if code not in ("ACTION_NOT_SUPPORTED", "POLICY_DENIED", "WRITE_IGNORED"):
            return env, "headless"
        if not physical:
            raise NodeError("physical_denied")
        env = self.ad(argv, home=job.home, stdin=stdin, headed=True)
        if env.get("ok") or fallback_clip is None:
            return env, "physical"
        clip = self.ad(["clipboard-set", "--stdin"], home=job.home, stdin=fallback_clip)
        if not clip.get("ok"):
            return env, "physical"
        env = self.ad(["press", "ctrl+v", "--window-id", window], home=job.home, headed=True)
        self.ad(["clipboard-clear"], home=job.home)
        return env, "physical"

    def _done(self, job: Job, env: dict[str, Any], mode: str, action: str, el: Any) -> dict[str, Any]:
        if not env.get("ok"):
            raise NodeError(_code_of(env), code=_err(env))
        d = env.get("data") or {}
        disp = d.get("disposition") or {}
        post = d.get("post_state") or {}
        if any(str((st or {}).get("mechanism") or "").startswith("physical") for st in d.get("steps") or ()):
            mode = "physical"  # l'amont dit comment il a livré : il fait foi
        target = f"{el.role} « {el.name} »" if el is not None and el.name else (el.role if el is not None else "")
        out = {"action": action, "delivery": str(disp.get("delivery") or "delivered_unverified"),
               "retry": str(disp.get("retry") or "unsafe"), "mode": mode,
               "target": job.masker.mask(one_line(target, 160))}
        if isinstance(post, dict) and post.get("role"):
            focused = one_line(post.get("name") or post.get("role"), 120)
            out["focused"] = job.masker.mask(focused)
        return out

    def _click(self, job: Job, args: dict, apps: tuple, physical: bool, pol: dict) -> dict[str, Any]:
        ref, el, _w = self._resolve(job, args, apps)
        env, mode = self._act(job, ["click", ref], stdin=None, physical=physical)
        return self._done(job, env, mode, "click", el)

    def _text_value(self, args: dict) -> str:
        text = args.get("text")
        if not isinstance(text, str) or len(text) > TEXT_MAX:
            raise NodeError("bad_args", detail="text")
        return text

    def _type(self, job: Job, args: dict, apps: tuple, physical: bool, pol: dict) -> dict[str, Any]:
        text = self._text_value(args)
        ref, el, window = self._resolve(job, args, apps)
        if el.secret:
            raise NodeError("secure_field")
        env, mode = self._act(job, ["type", ref, "--stdin"], stdin=text, physical=physical,
                              fallback_clip=text, window=window)
        return self._done(job, env, mode, "type", el)

    def _set_value(self, job: Job, args: dict, apps: tuple, physical: bool, pol: dict) -> dict[str, Any]:
        text = self._text_value(args)
        ref, el, window = self._resolve(job, args, apps)
        if el.secret:
            raise NodeError("secure_field")
        env, mode = self._act(job, ["set-value", ref, "--stdin"], stdin=text, physical=physical,
                              fallback_clip=None, window=window)
        return self._done(job, env, mode, "set_value", el)

    def _select(self, job: Job, args: dict, apps: tuple, physical: bool, pol: dict) -> dict[str, Any]:
        option = self._text_value(args)
        ref, el, _w = self._resolve(job, args, apps)
        env, mode = self._act(job, ["select", ref, option], stdin=None, physical=physical)
        return self._done(job, env, mode, "select", el)

    def _check(self, job: Job, args: dict, apps: tuple, physical: bool, pol: dict) -> dict[str, Any]:
        if not isinstance(args.get("checked"), bool):
            raise NodeError("bad_args", detail="checked")
        ref, el, _w = self._resolve(job, args, apps)
        env, mode = self._act(job, ["check" if args["checked"] else "uncheck", ref], stdin=None, physical=physical)
        if not env.get("ok") and physical and _err(env) in ("ACTION_NOT_SUPPORTED", "POLICY_DENIED"):
            env, mode = self.ad(["click", ref], home=job.home, headed=True), "physical"  # case GTK4 (spike)
        return self._done(job, env, mode, "check", el)

    def _press(self, job: Job, args: dict, apps: tuple, physical: bool, pol: dict) -> dict[str, Any]:
        combo = P.parse_keys(args.get("keys"))
        if combo is None:
            raise NodeError("bad_keys", keys=one_line(args.get("keys"), 40))
        window = str(args.get("window") or "")
        self._same_app(window, self._window_app(job, window, apps), args)
        # Sous X11, `press` passe par XTEST (`physical_synthetic`, relevé en B1b) même sans --headed : les touches vont
        # à la fenêtre qui a le focus. Donc : mode physique permis, fenêtre cible mise au premier plan et VÉRIFIÉE.
        if not physical:
            raise NodeError("physical_denied")
        self.ad(["focus-window", "--window-id", window], home=job.home)
        if not self._focused(job, window):
            raise NodeError("failed", code="focus_lost")
        if combo.split("+")[-1] in ("return", "space") and set(combo.split("+")[:-1]) <= {"shift"}:
            # Entrée ou Espace sur l'élément qui a le focus : s'il envoie ou détruit, on exige le clic (sa carte).
            focused = self.ad(["find", "--window-id", window, "--state", "focused", "--limit", "1"], home=job.home)
            if not focused.get("ok"):
                raise NodeError("failed", code="focus_unknown")  # sans le focus, on ne sait pas ce que fait Entrée
            for m in (focused.get("data") or {}).get("matches") or ():
                if P.target_class(m.get("role"), m.get("name")) is not P.Effect.WRITE \
                        and str(m.get("role") or "").lower() in P.CLICKABLE_ROLES:
                    raise NodeError("press_on_sensitive",
                                    target=job.masker.mask(one_line(f"{m.get('role')} « {m.get('name')} »", 120)))
        env, mode = self._act(job, ["press", combo, "--window-id", window], stdin=None, physical=physical)
        return self._done(job, env, "physical", "press", None)

    def _focused(self, job: Job, window: str) -> bool:
        env = self.ad(["list-windows"], home=job.home)
        data = env.get("data")
        items = data.get("windows") if isinstance(data, dict) else data
        return any(isinstance(w, dict) and str(w.get("id")) == window and w.get("is_focused") for w in items or ())


def _code_of(env: dict[str, Any]) -> str:
    """Code amont → code de refus du nœud."""
    code = _err(env)
    return {"STALE_REF": "stale_target", "AMBIGUOUS_TARGET": "ambiguous_target", "WINDOW_NOT_FOUND": "unknown_window",
            "ELEMENT_NOT_FOUND": "not_found", "SNAPSHOT_NOT_FOUND": "unknown_snapshot",
            "ACTION_NOT_SUPPORTED": "not_supported", "TIMEOUT": "timeout"}.get(code, "failed")


# ─────────────────────────────── client de la file ───────────────────────────────
def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


@dataclass
class NodeKey:
    """Clé P-256 du nœud, dans un fichier 0600 de son dossier d'état (jamais dans le dossier de tâche)."""

    path: Path

    def load(self) -> Any:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        if self.path.is_file():
            return serialization.load_pem_private_key(self.path.read_bytes(), password=None)
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        return key

    @staticmethod
    def spki(key: Any) -> str:
        from cryptography.hazmat.primitives import serialization

        return base64.b64encode(key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()

    @staticmethod
    def proof(key: Any, method: str, host: str, path_qs: str, body: bytes) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec

        from nokker.desktop.proof import proof_message

        ts, nonce = str(int(time.time())), secrets.token_urlsafe(18)
        sig = key.sign(proof_message(method, host, path_qs, ts, nonce, body), ec.ECDSA(hashes.SHA256()))
        return f"v1;ts={ts};nonce={nonce};sig={_b64u(sig)}"


ENROLL_FAST_S = 5.0          # relevé de la carte pendant les 2 premières minutes
ENROLL_FAST_FOR_S = 120.0
ENROLL_SLOW_S = 30.0         # ensuite (et avant une nouvelle demande après 404/410)
ENROLL_RETRY_S = 300.0       # 429 et erreurs du noyau : pause longue, jamais de sortie


@dataclass
class NodeClient:
    """Boucle du nœud : inscription (carte forte côté noyau), battement, relève de la file, résultats.
    `base` : URL du noyau local (`http://core:8742` dans le compose, `http://127.0.0.1:8742` en session)."""

    base: str
    state_dir: Path
    executor: Executor
    name: str = "bureau"
    # `Host` envoyé : le noyau n'accepte que ses hôtes déclarés (protection DNS rebinding) ; dans le compose, le nœud
    # joint `core:8742` en annonçant `127.0.0.1:8742`, comme l'agent de paiement. Vide : celui de `base`.
    host_header: str = ""
    http: Callable[..., tuple[int, dict[str, Any]]] | None = None
    stop: threading.Event = field(default_factory=threading.Event)
    now: Callable[[], float] = time.monotonic

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None, *, token: str | None = None,
                 key: Any = None, timeout: float = 35.0) -> tuple[int, dict[str, Any]]:
        from urllib.parse import urlsplit

        raw = json.dumps(body, ensure_ascii=False).encode() if body is not None else b""
        headers = {"Content-Type": "application/json"}
        host = self.host_header or urlsplit(self.base).netloc
        headers["Host"] = host
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if key is not None:
            from nokker.desktop.proof import PROOF_HEADER

            headers[PROOF_HEADER] = NodeKey.proof(key, method, host, path, raw)
        if self.http is not None:
            return self.http(method, path, raw, headers)
        import urllib.error
        import urllib.request

        req = urllib.request.Request(self.base.rstrip("/") + path, data=raw if method != "GET" else None,
                                     method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — URL du noyau local (config)
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}
        except (urllib.error.URLError, OSError, ValueError):
            return 0, {}  # noyau injoignable (redémarrage) : statut 0, la boucle réessaie

    def _pace(self, started: float) -> float:
        """Relevé de la carte : toutes les 5 s pendant 2 min (la personne est souvent devant), puis toutes les 30 s."""
        return ENROLL_FAST_S if self.now() - started < ENROLL_FAST_FOR_S else ENROLL_SLOW_S

    def credentials(self) -> tuple[Any, str]:
        """(clé, jeton) ; inscrit le nœud si besoin. Ne SORT jamais sur une réponse du noyau (correctif « rafale de
        cartes », 09.11) : sortir faisait relancer le conteneur, et chaque relance posait une nouvelle carte, donc une
        notification. 410/404 (inscription expirée ou inconnue) : nouvelle demande après une pause, que le noyau
        rattache à la carte vivante de la même clé s'il y en a une ; 429 et autres erreurs : pause longue ;
        403 (refusée) : le nœud dort jusqu'à son arrêt, sans redemander."""
        key = NodeKey(self.state_dir / "node-key.pem").load()
        tok_path = self.state_dir / "node-token"
        while not self.stop.is_set():
            if tok_path.is_file():
                return key, tok_path.read_text().strip()
            status, out = self._request("POST", "/v1/desktop/node/enroll", {
                "name": self.name, "mode": self.executor.mode, "public_key_spki": NodeKey.spki(key)})
            if status != 202:
                logger.warning("inscription du bureau : réponse %s, nouvelle tentative dans %d s", status,
                               ENROLL_RETRY_S)
                self.stop.wait(ENROLL_RETRY_S if status else ENROLL_FAST_S)
                continue
            approval = str(out.get("approval_id") or "")
            logger.info("inscription du bureau : carte %s à approuver (clé %s)", approval, out.get("key_fingerprint"))
            started = self.now()
            while not self.stop.is_set():
                status, out = self._request("GET", f"/v1/desktop/node/enroll/{approval}", key=key)
                if status == 201:
                    token = str(out["token"])
                    fd = os.open(tok_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w") as f:
                        f.write(token)
                    return key, token
                if status == 403:
                    logger.warning("inscription du bureau refusée : le nœud attend son arrêt, sans redemander")
                    self.stop.wait()
                    break
                if status in (404, 410):
                    logger.info("inscription %s %s : nouvelle demande dans %d s", approval, status, ENROLL_SLOW_S)
                    self.stop.wait(ENROLL_SLOW_S)
                    break
                self.stop.wait(self._pace(started) if status in (0, 202) else ENROLL_RETRY_S)
        raise NodeError("failed", code="stopped")

    def run(self) -> None:
        self.executor.jobs.sweep_all()
        key, token = self.credentials()
        last_beat = 0.0
        while not self.stop.is_set():
            if time.time() - last_beat > 60:
                self._request("POST", "/v1/desktop/node/beat", {"v": 1, **health(self.executor.ad),
                                                                "mode": self.executor.mode}, token=token, key=key)
                last_beat = time.time()
            self.executor.jobs.expire()
            status, out = self._request("GET", "/v1/desktop/node/tasks?wait=25", token=token, key=key)
            if status == 401:
                # Retiré par la personne : ne pas sortir (docker relancerait le nœud en boucle sur un jeton mort),
                # ne pas redemander non plus. Le jeton est effacé : un redémarrage voulu refera UNE demande.
                logger.warning("bureau retiré : le nœud attend son arrêt")
                (self.state_dir / "node-token").unlink(missing_ok=True)
                self.stop.wait()
                break
            if status != 200:
                self.stop.wait(5)
                continue
            for task in out.get("tasks") or ():
                self._run_task(task, token, key)

    def _run_task(self, task: dict[str, Any], token: str, key: Any) -> None:
        tid = str(task.get("task_id") or "")
        try:
            result = {"status": "done", "result": self.executor.handle(str(task.get("op") or ""),
                                                                       dict(task.get("args") or {}),
                                                                       dict(task.get("policy") or {}))}
        except NodeError as e:
            result = {"status": "failed", "error": {"code": e.code, "detail": {k: str(v)[:200]
                                                                                for k, v in e.detail.items()}}}
        except Exception:  # noqa: BLE001 — une opération cassée ne tue pas le nœud
            logger.exception("opération %s en échec", task.get("op"))
            result = {"status": "failed", "error": {"code": "failed", "detail": {"code": "INTERNAL"}}}
        if task.get("end_job") and (task.get("args") or {}).get("job"):
            self.executor.jobs.end(str(task["args"]["job"]))
        self._request("POST", f"/v1/desktop/node/tasks/{tid}/result", result, token=token, key=key)


def main(argv: list[str] | None = None) -> int:
    """`python -m nokker.desktop.node --core URL --state DIR --mode virtual|session [--health]`."""
    import argparse

    ap = argparse.ArgumentParser(prog="nokker-desktop")
    ap.add_argument("--core", default=os.environ.get("NOKKER_CORE_URL", "http://core:8742"))
    ap.add_argument("--state", default=os.environ.get("NOKKER_DESKTOP_STATE", str(Path.home() / ".nokker-desktop")))
    ap.add_argument("--mode", choices=P.MODES, default=os.environ.get("NOKKER_DESKTOP_MODE", "virtual"))
    ap.add_argument("--binary", default=os.environ.get("NOKKER_DESKTOP_BIN", "/usr/local/bin/agent-desktop"))
    ap.add_argument("--lock", default=os.environ.get("NOKKER_DESKTOP_LOCK",
                                                     "/usr/local/share/nokker-desktop/agent-desktop.lock.json"))
    ap.add_argument("--name", default=os.environ.get("NOKKER_DESKTOP_NAME", "bureau"))
    ap.add_argument("--core-host", default=os.environ.get("NOKKER_CORE_HOST", ""),
                    help="Host annoncé au noyau (compose : 127.0.0.1:8742)")
    ap.add_argument("--health", action="store_true", help="contrôle de santé : code 0 si le bureau est prêt")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        check_pin(a.binary, json.loads(Path(a.lock).read_text()))
    except (OSError, ValueError, NodeError) as e:
        logger.error("binaire agent-desktop refusé : %s", getattr(e, "code", e))
        return 2
    ad = AgentDesktop(binary=a.binary)
    if a.health:
        h = health(ad)
        print(json.dumps(h))
        return 0 if h["desktop"] == "on" else 1
    client = NodeClient(base=a.core, state_dir=Path(a.state), name=a.name, host_header=a.core_host,
                        executor=Executor(ad=ad, jobs=Jobs(), mode=a.mode))
    try:
        client.run()
    except NodeError as e:
        logger.error("nœud arrêté : %s", e.code)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
