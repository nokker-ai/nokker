"""nokker/tools/processes.py — commandes en arrière-plan d'un fil (lot X6, docs/LOKKAL.md §7.7).

Un serveur de développement, un `watch`, un `tail -f` : une commande qui ne rend pas la main. `terminal` attend la fin
(120 s par défaut) ; ici elle est lancée détachée, DANS l'environnement du fil (le bac à sable docker, ou le dossier
en local), et le modèle la suit par son identifiant `bg_…` : `process_list`, `process_logs`, `process_stop`.

Mécanique, la même dans les deux backends (une commande passée à l'environnement, comme les outils de fichiers) :
  - lancement : `set -m` donne au travail détaché SON groupe de processus (pgid = son pid) ; sa sortie (stdout et
    stderr) va dans un journal, son code de sortie dans un fichier `.exit` ; aucun descripteur du shell appelant n'est
    hérité, l'appel rend la main tout de suite ;
  - marque : le chef du groupe porte `NOKKER_BG=<id>` dans son environnement ; avant tout signal, on vérifie la marque
    dans `/proc/<pgid>/environ` (un pid recyclé n'est jamais tué) ;
  - arrêt : SIGTERM au groupe, 5 s de grâce, puis SIGKILL.

Docker : journaux dans le container (`/tmp/.nokker-bg`, tmpfs du bac à sable) ; la fin du fil supprime le container,
donc tout ce qui y tourne. Local : journaux sous l'état du fil (`<état>/processes/<fil>/`), une fiche par processus
pour que le ménage au démarrage (`reap_processes`) arrête ce qu'un noyau tué a laissé.

Plafond : `MAX_RUNNING` processus en cours par fil. Le journal rendu au modèle est coupé (fin du journal, `tail`
lignes) ; un résultat long est rangé au réservoir par la boucle (`admit_result_in`), comme toute sortie d'outil.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import signal
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

MAX_RUNNING = 4  # processus en cours par fil
MAX_KEPT = 16  # entrées gardées par fil (en cours + terminées, pour lire le journal d'un processus fini)
TAIL_DEFAULT, TAIL_MAX = 200, 2000
LOG_CHARS = 30_000  # journal rendu au modèle (fin du journal) ; le réservoir range un résultat long hors fenêtre
SANDBOX_LOG_DIR = "/tmp/.nokker-bg"
STOP_GRACE_S = 5
PREVIEW_MAX_BYTES = 5 * 1024 * 1024
PREVIEW_TTL_S = 3600.0
_ID = re.compile(r"^bg_[0-9a-f]{8}$")
_PID = re.compile(r"NOKKER_BG_PID=(\d+)")
_SIG_HEX_LEN = 32

# Le chef du groupe est-il bien le nôtre ? Marque dans /proc ; sans /proc (macOS), le groupe vivant suffit.
_OWN = ('own() { if [ -r "/proc/$1/environ" ]; then tr "\\0" "\\n" < "/proc/$1/environ" | grep -qx "NOKKER_BG=$2"; '
        'else kill -0 -- "-$1" 2>/dev/null; fi; }')


def is_process_id(value: str) -> bool:
    return bool(_ID.match(value or ""))


class ProcessError(RuntimeError):
    """Refus ou échec (plafond, id inconnu, lancement raté) : le message revient au modèle tel quel."""


@dataclass
class BgProcess:
    id: str
    name: str
    command: str
    pgid: int
    log: str  # chemin du journal tel que l'environnement le voit
    started_at: float
    port: int | None = None
    state: str = "running"  # running | exited | stopped | lost
    returncode: int | None = None
    ended_at: float | None = None

    @property
    def exit_file(self) -> str:
        return self.log[:-4] + ".exit"

    def view(self) -> dict[str, Any]:
        out = {"id": self.id, "name": self.name, "command": self.command, "state": self.state,
               "started_at": round(self.started_at, 3)}
        if self.port:
            out["port"] = self.port
        if self.returncode is not None:
            out["returncode"] = self.returncode
        if self.ended_at is not None:
            out["ended_at"] = round(self.ended_at, 3)
        return out


def _q(s: str) -> str:
    return shlex.quote(s)


class ProcessTable:
    """Les processus en arrière-plan d'UN fil, dans UN environnement (Hermes : `execute(command, …)`).

    `log_dir` : où vont les journaux, tel que l'environnement le voit. `host_logs` : le noyau voit ce dossier
    (backend local) — lecture des journaux en direct et fiches de ménage ; sinon tout passe par l'environnement."""

    def __init__(self, env: Any, log_dir: str, *, host_logs: bool = False, cwd: str | None = None,
                 max_running: int = MAX_RUNNING, settle_s: float = 0.8, clock=time.time) -> None:
        self.env, self.log_dir, self.host_logs, self.cwd = env, log_dir.rstrip("/"), host_logs, cwd
        self.max_running, self.settle_s, self.clock = max_running, settle_s, clock
        self._procs: dict[str, BgProcess] = {}
        self._mu = threading.RLock()

    # ── environnement ──
    def _sh(self, script: str, timeout: int = 30) -> dict[str, Any]:
        """Un script bash à part (`bash -c`) : `set -m` et les fonctions ne fuient pas dans la session du fil."""
        kw: dict[str, Any] = {"timeout": timeout, "rewrite_compound_background": False}
        if self.cwd:
            kw["cwd"] = self.cwd
        return self.env.execute(f"bash -c {_q(script)}", **kw)

    # ── cycle de vie ──
    def start(self, command: str, name: str = "", port: int | None = None) -> BgProcess:
        command = command.strip()
        if not command:
            raise ProcessError("commande vide")
        with self._mu:
            self._refresh()
            running = [p for p in self._procs.values() if p.state == "running"]
            if len(running) >= self.max_running:
                raise ProcessError(
                    f"déjà {len(running)} processus en cours sur ce fil (plafond {self.max_running}) : arrête-en un "
                    f"(process_stop) — en cours : {', '.join(f'{p.id} {p.name}' for p in running)}")
            pid = f"bg_{secrets.token_hex(4)}"
            log = f"{self.log_dir}/{pid}.log"
            exit_file = log[:-4] + ".exit"
            inner = f"bash -c {_q(command)} > {_q(log)} 2>&1 < /dev/null; echo $? > {_q(exit_file)}"
            # Une ligne par commande : `a && b &` détacherait toute la liste (et ses descripteurs) dans un sous-shell.
            script = (f"mkdir -p {_q(self.log_dir)} && : > {_q(log)} || exit 1\nset -m\n"
                      f"env NOKKER_BG={pid} bash -c {_q(inner)} > /dev/null 2>&1 < /dev/null &\n"
                      # La marque n'est visible qu'une fois `env` passé à bash (execve) : on l'attend (≤ 2 s), sinon
                      # un process_stop juste après le lancement ne reconnaîtrait pas le sien.
                      f"p=$!; i=0\n"
                      f'while [ -r "/proc/$p/environ" ] && ! tr "\\0" "\\n" < "/proc/$p/environ" | '
                      f'grep -qx "NOKKER_BG={pid}" && [ $i -lt 200 ]; do sleep 0.01; i=$((i+1)); done\n'
                      f'echo "NOKKER_BG_PID=$p"')
            r = self._sh(script)
            m = _PID.search(str(r.get("output") or ""))
            if m is None:
                raise ProcessError(f"lancement en échec : {str(r.get('output') or '').strip()[:500]}")
            proc = BgProcess(id=pid, name=(name or command.split()[0])[:60], command=command, pgid=int(m.group(1)),
                             log=log, started_at=self.clock(), port=port)
            self._procs[pid] = proc
            self._write_record(proc)
            self._prune()
        if self.settle_s > 0:
            time.sleep(self.settle_s)  # une commande qui échoue d'emblée (port pris, faute de frappe) se voit au lancement
        return proc

    def get(self, proc_id: str) -> BgProcess:
        with self._mu:
            proc = self._procs.get(proc_id)
        if proc is None:
            raise ProcessError(f"processus inconnu sur ce fil : {proc_id!r} (process_list)")
        return proc

    def list(self) -> list[BgProcess]:
        with self._mu:
            self._refresh()
            return list(self._procs.values())

    def running(self) -> list[BgProcess]:
        with self._mu:
            return [p for p in self._procs.values() if p.state == "running"]

    def _refresh(self) -> None:
        """État des processus marqués en cours : un seul appel à l'environnement pour tous."""
        live = [p for p in self._procs.values() if p.state == "running"]
        if not live:
            return
        lines = [_OWN]
        for p in live:
            lines.append(f'if [ -f {_q(p.exit_file)} ]; then echo "{p.id} exit $(cat {_q(p.exit_file)})"; '
                         f'elif own {p.pgid} {p.id}; then echo "{p.id} run"; else echo "{p.id} lost"; fi')
        out = str(self._sh("\n".join(lines)).get("output") or "")
        seen = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] in self._procs:
                seen[parts[0]] = parts[1:]
        for p in live:
            got = seen.get(p.id)
            if got is None or got[0] == "run":
                continue
            p.state = "exited" if got[0] == "exit" else "lost"
            if got[0] == "exit" and len(got) > 1 and got[1].lstrip("-").isdigit():
                p.returncode = int(got[1])
            p.ended_at = self.clock()
            self._drop_record(p)

    def stop(self, proc_id: str) -> BgProcess:
        with self._mu:
            proc = self.get(proc_id)
            self._refresh()
            if proc.state != "running":
                return proc
            script = (f"{_OWN}\nown {proc.pgid} {proc.id} || {{ echo gone; exit 0; }}\n"
                      f"kill -TERM -- -{proc.pgid} 2>/dev/null; i=0\n"
                      f"while kill -0 -- -{proc.pgid} 2>/dev/null && [ $i -lt {STOP_GRACE_S * 10} ]; do "
                      f"sleep 0.1; i=$((i+1)); done\n"
                      f"kill -KILL -- -{proc.pgid} 2>/dev/null; echo stopped")
            out = str(self._sh(script, timeout=STOP_GRACE_S + 20).get("output") or "")
            proc.state = "stopped" if "stopped" in out else "lost"
            proc.ended_at = self.clock()
            self._drop_record(proc)
            return proc

    def stop_all(self) -> int:
        """Fin du fil : tout ce qui tourne encore est arrêté. Rend le nombre arrêté ; ne lève jamais."""
        n = 0
        for p in self.running():
            try:
                if self.stop(p.id).state == "stopped":
                    n += 1
            except Exception:  # noqa: BLE001 — fermeture au mieux (environnement déjà parti)
                pass
        return n

    # ── journaux ──
    def logs(self, proc_id: str, tail: int = TAIL_DEFAULT) -> dict[str, Any]:
        proc = self.get(proc_id)
        tail = max(1, min(int(tail or TAIL_DEFAULT), TAIL_MAX))
        with self._mu:
            self._refresh()  # l'état rendu avec le journal est celui du moment (fini ? code de sortie ?)
        if self.host_logs:
            size, text = _tail_file(proc.log, tail)
        else:
            out = str(self._sh(f"wc -c < {_q(proc.log)} 2>/dev/null || echo 0; echo NOKKER_BG_LOG; "
                               f"tail -n {tail} {_q(proc.log)} 2>/dev/null").get("output") or "")
            head, _, text = out.partition("NOKKER_BG_LOG\n")
            size = int(head.strip().splitlines()[-1]) if head.strip().splitlines() and \
                head.strip().splitlines()[-1].isdigit() else 0
        cut = len(text) > LOG_CHARS
        text = text[-LOG_CHARS:] if cut else text
        return {**proc.view(), "log_bytes": size, "tail": tail, "truncated": size > len(text.encode()),
                "output": text}

    # ── aperçu d'un port ──
    def fetch(self, proc_id: str, path: str = "/", method: str = "GET") -> tuple[int, str, bytes, str | None]:
        """Requête vers le port déclaré du processus, DEPUIS son environnement (le bac à sable : aucun port publié sur
        l'hôte, aucun réseau partagé avec le noyau). Rend (statut, type, corps, Location). Pas de redirection suivie."""
        proc = self.get(proc_id)
        if not proc.port or proc.state != "running":
            raise ProcessError("pas de port à montrer pour ce processus")
        if not path.startswith("/") or any(c in path for c in "\r\n "):
            raise ProcessError("chemin invalide")
        if self.host_logs:  # local : le noyau est sur la même machine que le processus
            data = _fetch_here(int(proc.port), path, method)
        else:
            script = _FETCH.format(port=int(proc.port), max=PREVIEW_MAX_BYTES)
            cmd = f"python3 -c {_q(script)} {_q(path)} {_q(method)}"
            out = str(self.env.execute(cmd, timeout=30, rewrite_compound_background=False).get("output") or "")
            m = re.search(r"NOKKER_FETCH=(\{.*\})", out)
            if m is None:
                raise ProcessError("port injoignable dans le bac à sable")
            data = json.loads(m.group(1))
        if data.get("error"):
            raise ProcessError(f"port injoignable : {data['error']}")
        return int(data["status"]), str(data.get("type") or "application/octet-stream"), \
            base64.b64decode(data.get("body") or ""), data.get("location")

    # ── fiches de ménage (local) ──
    def _record(self, proc: BgProcess) -> Path | None:
        return Path(self.log_dir) / f"{proc.id}.json" if self.host_logs else None

    def _write_record(self, proc: BgProcess) -> None:
        f = self._record(proc)
        if f is not None:
            try:
                f.write_text(json.dumps(asdict(proc), ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass

    def _drop_record(self, proc: BgProcess) -> None:
        f = self._record(proc)
        if f is not None:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass

    def _prune(self) -> None:
        done = [p for p in self._procs.values() if p.state != "running"]
        for p in done[: max(0, len(self._procs) - MAX_KEPT)]:
            self._procs.pop(p.id, None)


# Client HTTP minimal lancé DANS le bac à sable (python3 est dans l'image) : 127.0.0.1:<port> seulement, sans
# redirection suivie, corps borné, sortie sur une ligne marquée (base64).
_FETCH = """
import base64, json, sys, urllib.request, urllib.error
class N(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
o = urllib.request.build_opener(N, urllib.request.ProxyHandler({{}}))
r = {{}}
try:
    try:
        resp = o.open(urllib.request.Request("http://127.0.0.1:{port}" + sys.argv[1], method=sys.argv[2]), timeout=20)
    except urllib.error.HTTPError as e:
        resp = e
    body = resp.read({max} + 1)
    if len(body) > {max}: r = {{"error": "réponse trop grande"}}
    else: r = {{"status": resp.status if hasattr(resp, "status") else resp.code,
               "type": resp.headers.get("Content-Type"), "location": resp.headers.get("Location"),
               "body": base64.b64encode(body).decode()}}
except Exception as e:
    r = {{"error": type(e).__name__ + ": " + str(e)[:200]}}
print("NOKKER_FETCH=" + json.dumps(r))
"""


def _fetch_here(port: int, path: str, method: str) -> dict[str, Any]:
    """Backend local : la même requête que `_FETCH`, faite par le noyau (même machine que le processus)."""
    import urllib.error
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a: Any, **k: Any) -> None:
            return None

    opener = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))
    try:
        try:
            resp = opener.open(urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method), timeout=20)
        except urllib.error.HTTPError as e:
            resp = e
        body = resp.read(PREVIEW_MAX_BYTES + 1)
        if len(body) > PREVIEW_MAX_BYTES:
            return {"error": "réponse trop grande"}
        return {"status": getattr(resp, "status", None) or resp.code, "type": resp.headers.get("Content-Type"),
                "location": resp.headers.get("Location"), "body": base64.b64encode(body).decode()}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


def _tail_file(path: str, lines: int) -> tuple[int, str]:
    """(taille, dernières lignes) d'un journal du noyau, sans le lire en entier."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 4 * LOG_CHARS))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return 0, ""
    return size, "".join(deque(text.splitlines(keepends=True), maxlen=lines))


def reap_processes(processes_dir: str | Path) -> int:
    """Ménage au démarrage du noyau (backend local) : arrête les processus en arrière-plan qu'un noyau tué a laissés,
    d'après leurs fiches sous `processes_dir`. Seulement ceux dont le chef de groupe porte encore SA marque
    (`NOKKER_BG=<id>` dans /proc) : jamais un pid recyclé. Sans /proc, rien n'est tué (on ne peut pas vérifier)."""
    root = Path(processes_dir)
    if not root.is_dir():
        return 0
    n = 0
    for f in root.glob("*/bg_*.json"):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
            pgid, pid = int(rec["pgid"]), str(rec["id"])
        except (OSError, ValueError, KeyError, TypeError):
            f.unlink(missing_ok=True)
            continue
        if _owned(pgid, pid):
            try:
                os.killpg(pgid, signal.SIGTERM)
                for _ in range(STOP_GRACE_S * 10):
                    time.sleep(0.1)
                    os.killpg(pgid, 0)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            n += 1
        f.unlink(missing_ok=True)
    return n


def _owned(pgid: int, proc_id: str) -> bool:
    try:
        env = Path(f"/proc/{pgid}/environ").read_bytes()
    except OSError:
        return False
    return f"NOKKER_BG={proc_id}".encode() in env.split(b"\0")


# ─────────────────────────────── liens d'aperçu ───────────────────────────────


class PreviewSigner:
    """Liens signés de l'aperçu d'un port (docs/LOKKAL.md §7.7) : la capacité est dans le CHEMIN
    (`/v1/local/preview/<id>/<exp>.<sig>/…`), pour que les URL relatives de l'appli (CSS, JS, images) la portent aussi —
    un `<iframe>` ne pose pas d'`Authorization`. Secret par démarrage du noyau (un redémarrage invalide les liens)."""

    def __init__(self, secret: bytes | None = None, clock=time.time) -> None:
        self.secret = secret or secrets.token_bytes(32)
        self.clock = clock

    def _sig(self, proc_id: str, exp: str) -> str:
        return hmac.new(self.secret, f"preview:{proc_id}.{exp}".encode("ascii"),
                        hashlib.sha256).hexdigest()[:_SIG_HEX_LEN]

    def link(self, proc_id: str, ttl_s: float = PREVIEW_TTL_S) -> tuple[str, float]:
        exp = str(int(self.clock() + ttl_s))
        return f"/v1/local/preview/{proc_id}/{exp}.{self._sig(proc_id, exp)}/", float(exp)

    def verify(self, proc_id: str, cap: str) -> bool:
        exp, _, sig = (cap or "").partition(".")
        try:
            if int(exp) < self.clock():
                return False
        except ValueError:
            return False
        return hmac.compare_digest(self._sig(proc_id, exp), sig)


__all__ = ["BgProcess", "MAX_RUNNING", "PreviewSigner", "ProcessError", "ProcessTable", "is_process_id",
           "reap_processes"]
