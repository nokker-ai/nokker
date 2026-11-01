"""`execute_code` : un script Python, écrit par le modèle, qui appelle PLUSIEURS outils du noyau en un seul appel
(docs/LOKKAL.md §7.31). Idée reprise de l'amont (`tools/code_execution_tool.py`), mécanique à nous :

- **Où** : DANS le bac à sable du fil (`CodeToolSource.script_command` : `docker exec -i` dans le container du fil,
  `kubectl exec -i` dans son pod). Jamais sur l'hôte du noyau : backend local → refus. Aucun port, aucun fichier
  d'échange : le canal est le stdin/stdout de CE processus, que le noyau tient ouvert.
- **Appels d'outils** : le script écrit une trame JSON par appel sur le canal ; le noyau la vérifie (jeton de CET appel,
  échéance, liste blanche, plafond d'appels) puis la passe à `Kernel.script_tool_call` : les MÊMES portes que la boucle
  (effet de l'appel, règles, invariants, carte prouvée, escalade) et le MÊME caviardage, sur le tour en cours. Un outil
  qui pose une carte rend `pending` tout de suite (le script n'attend pas : `ApprovalPending`) ; la carte vit sa vie
  ordinaire (décision, exécution, tour de suite avec le bloc `[Approbation]`).
- **Sortie** : un bloc FIXE, toujours les trois sections, dans cet ordre, une section vide valant `(vide)` :

      [execute_code]
      stdout:
      …
      return:
      …
      error:
      …
      [/execute_code]

  Caviardage (`Invariants.scrub_output`) PUIS troncature déterministe (tête 2/3 + queue 1/3, coupe reculée au
  dernier saut de ligne ou blanc s'il est proche, marqueur « [… N caractères coupés …] ») : même entrée, même bloc.

Le jeton : 32 octets aléatoires par appel, envoyé au script sur stdin (jamais en argument ni en variable
d'environnement), valable jusqu'à l'échéance du script ; une trame sans lui, ou après, est refusée sans exécuter.
"""
from __future__ import annotations

import hmac
import json
import logging
import queue
import secrets
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult

logger = logging.getLogger("nokker.tools.execute_code")

TOOL = "execute_code"
# Liste blanche par défaut : des LECTURES (code, mémoire, web, points de retour, processus, skills). Un outil à effet
# ajouté par la config (`execute_code.tools`) passe par ses portes comme dans la boucle : carte, refus.
DEFAULT_TOOLS: tuple[str, ...] = (
    "read_file", "search_files", "memory_search", "memory_get", "web_search", "web_extract", "process_list",
    "process_logs", "checkpoint_list", "checkpoint_diff", "skill_list", "skill_view")
# Jamais appelables depuis un script, même ajoutés à la liste : récursion, outils natifs de la boucle, sous-agents.
NEVER: frozenset[str] = frozenset({
    TOOL, "tool_find", "escalate_to_human", "clarify", "delegate", "delegate_task", "delegate_status",
    "delegate_cancel"})

EMPTY = "(vide)"
OPEN, CLOSE = "[execute_code]", "[/execute_code]"
_SNAP = 200  # la coupe recule au dernier saut de ligne (sinon blanc) s'il est à moins de _SNAP caractères
_TRANSFER_BYTES = 256 * 1024  # ce que le script renvoie au plus par section : tête + queue de cette taille
_MAX_FRAME = 4 * 1024 * 1024  # une trame plus longue = protocole rompu, processus arrêté
_GRACE_S = 5  # au-delà de l'échéance du script, le noyau coupe le processus


# ─────────────────────────────── bloc de sortie ───────────────────────────────


def _snap_head(text: str, n: int) -> int:
    cut = max(text.rfind("\n", 0, n), -1)
    if cut < 0 or n - cut > _SNAP:
        cut = text.rfind(" ", 0, n)
    return cut + 1 if 0 <= cut and n - cut <= _SNAP else n


def _snap_tail(text: str, n: int) -> int:
    start = len(text) - n
    nl = text.find("\n", start)
    if nl < 0 or nl - start > _SNAP:
        nl = text.find(" ", start)
    return nl + 1 if 0 <= nl and nl - start <= _SNAP else start


def clip(text: str, limit: int) -> str:
    """Troncature déterministe : le texte tel quel s'il tient, sinon sa tête (2/3) et sa queue (1/3) autour de
    « [… N caractères coupés …] », N = ce qui manque. Fonction pure du texte et de la limite."""
    if len(text) <= limit:
        return text
    h = _snap_head(text, limit * 2 // 3)
    t = _snap_tail(text, limit // 3)
    t = max(t, h)
    return f"{text[:h].rstrip(chr(10))}\n[… {t - h} caractères coupés …]\n{text[t:].lstrip(chr(10))}"


def format_block(stdout: str, ret: str, error: str, limits: tuple[int, int, int],
                 scrub: Callable[[str], str] | None = None) -> str:
    """Le bloc fixe vu par le modèle. Chaque section : caviardée, PUIS coupée, puis `(vide)` si rien."""
    parts = []
    for label, text, limit in (("stdout", stdout, limits[0]), ("return", ret, limits[1]), ("error", error, limits[2])):
        text = (text or "").replace("\r\n", "\n").strip("\n")
        if scrub is not None and text:
            text = scrub(text)
        text = clip(text, limit) if text.strip() else ""
        parts.append(f"{label}:\n{text or EMPTY}")
    return "\n".join([OPEN, *parts, CLOSE])


# ─────────────────────────────── côté bac à sable ───────────────────────────────

# Lancé par `python3 -I -u -c BOOTSTRAP` (Python ≥ 3.8 dans l'image du bac à sable). -I : aucun module du dépôt ne
# remplace json, os… du bootstrap ; le dossier de travail est ensuite ajouté au chemin pour le code du modèle.
BOOTSTRAP = r'''
import ast, json, linecache, os, signal, sys, tempfile, traceback
_rin = os.fdopen(os.dup(0), "r", encoding="utf-8")
_rout = os.fdopen(os.dup(1), "w", encoding="utf-8")
_init = json.loads(_rin.readline())
_TOKEN, _SRC, _T, _CAP = _init["token"], _init["code"], int(_init["timeout"]), int(_init["cap"])
_dn = os.open(os.devnull, os.O_RDONLY)
os.dup2(_dn, 0)
os.close(_dn)
_fd, _capfile = tempfile.mkstemp(prefix="nokker-xc-")
sys.stdout.flush()
sys.stderr.flush()
os.dup2(_fd, 1)
os.dup2(_fd, 2)
os.close(_fd)
sys.stdin = open(os.devnull)
sys.path.insert(0, os.getcwd())
linecache.cache["<execute_code>"] = (len(_SRC), None, _SRC.splitlines(True), "<execute_code>")


class ToolError(Exception):
    """L'outil a rendu une erreur."""


class ToolRefused(Exception):
    """Le noyau a refusé l'appel (règle, liste blanche, plafond, jeton)."""


class ApprovalPending(Exception):
    """L'appel attend une décision humaine : rien n'a été exécuté."""

    def __init__(self, message, approval_id=None):
        Exception.__init__(self, message)
        self.approval_id = approval_id


class _Timeout(BaseException):
    pass


_seq = [0]


def call_tool(name, args=None, **kwargs):
    a = dict(args or {})
    a.update(kwargs)
    _seq[0] += 1
    _rout.write(json.dumps({"t": "call", "n": _seq[0], "token": _TOKEN, "name": str(name), "args": a},
                           ensure_ascii=False, default=str) + "\n")
    _rout.flush()
    line = _rin.readline()
    if not line:
        raise ToolError("canal du noyau fermé")
    r = json.loads(line)
    status, content = r.get("status"), r.get("content", "")
    if status == "ok":
        try:
            return json.loads(content)
        except ValueError:
            return content
    if status == "pending":
        raise ApprovalPending(content, r.get("approval_id"))
    if status in ("refused", "denied"):
        raise ToolRefused(content)
    raise ToolError(content)


class _Tools(object):
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda **kwargs: call_tool(name, kwargs)


def _canon(v, depth=0):
    if depth > 32:
        return repr(v)
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, dict):
        return dict((str(k), _canon(x, depth + 1)) for k, x in v.items())
    if isinstance(v, (list, tuple)):
        return [_canon(x, depth + 1) for x in v]
    if isinstance(v, (set, frozenset)):
        return sorted((_canon(x, depth + 1) for x in v), key=lambda x: json.dumps(x, sort_keys=True, default=repr))
    return repr(v)


def _fmt(e):
    lines = []
    frames = [f for f in traceback.extract_tb(e.__traceback__) if f.filename == "<execute_code>"]
    if frames:
        lines.append("Traceback (dernier appel en dernier) :")
        for f in frames:
            lines.append("  ligne %d, dans %s" % (f.lineno, f.name))
            if f.line:
                lines.append("    " + f.line.strip())
    lines.extend(x.rstrip("\n") for x in traceback.format_exception_only(type(e), e))
    return "\n".join(lines)


def _cut(text):
    b = text.encode("utf-8", "replace")
    if len(b) <= 2 * _CAP:
        return [text, "", len(b)]
    return [b[:_CAP].decode("utf-8", "replace"), b[-_CAP:].decode("utf-8", "replace"), len(b)]


def _on_alarm(signum, frame):
    raise _Timeout()


_g = {"__name__": "__main__", "__builtins__": __builtins__, "call_tool": call_tool, "tools": _Tools(),
      "ToolError": ToolError, "ToolRefused": ToolRefused, "ApprovalPending": ApprovalPending}
_ret, _err = None, None
signal.signal(signal.SIGALRM, _on_alarm)
signal.setitimer(signal.ITIMER_REAL, _T)
try:
    _tree = ast.parse(_SRC, "<execute_code>", "exec")
    _last = None
    if _tree.body and isinstance(_tree.body[-1], ast.Expr):
        _last = ast.Expression(_tree.body.pop().value)
    exec(compile(_tree, "<execute_code>", "exec"), _g)
    if _last is not None:
        _v = eval(compile(_last, "<execute_code>", "eval"), _g)
        if _v is not None:
            _ret = json.dumps(_canon(_v), ensure_ascii=False, sort_keys=True)
except _Timeout:
    _err = "TimeoutError: délai dépassé (%d s), script arrêté" % _T
except SystemExit as e:
    if e.code not in (None, 0):
        _err = "SystemExit: %s" % (e.code,)
except BaseException as e:
    _err = _fmt(e)
finally:
    signal.setitimer(signal.ITIMER_REAL, 0)
try:
    sys.stdout.flush()
    sys.stderr.flush()
except Exception:
    pass
with open(_capfile, "rb") as _f:
    _size = os.fstat(_f.fileno()).st_size
    if _size <= 2 * _CAP:
        _out = [_f.read().decode("utf-8", "replace"), "", _size]
    else:
        _head = _f.read(_CAP)
        _f.seek(_size - _CAP)
        _out = [_head.decode("utf-8", "replace"), _f.read().decode("utf-8", "replace"), _size]
os.unlink(_capfile)
_rout.write(json.dumps({"t": "done", "stdout": _out, "return": _cut(_ret) if _ret is not None else None,
                        "error": _cut(_err) if _err is not None else None}, ensure_ascii=False) + "\n")
_rout.flush()
'''


def _join(part: Any) -> str:
    """[tête, queue, octets] du script → texte ; la queue n'est envoyée que si le texte a été coupé en transit."""
    if not part:
        return ""
    head, tail, total = str(part[0]), str(part[1] or ""), int(part[2] or 0)
    if not tail:
        return head
    gap = max(0, total - len(head.encode("utf-8", "replace")) - len(tail.encode("utf-8", "replace")))
    return f"{head}\n[… {gap} octets coupés …]\n{tail}"


# ─────────────────────────────── côté noyau ───────────────────────────────


@dataclass
class ScriptSession:
    """Le canal d'UN appel d'`execute_code` : son jeton, son échéance, sa liste blanche, son plafond d'appels. Chaque
    trame `call` est vérifiée ici AVANT d'atteindre les portes du noyau (`dispatch`)."""

    allowed: frozenset[str]
    max_calls: int
    expires_at: float
    dispatch: Callable[[str, dict[str, Any]], dict[str, Any]]
    clock: Callable[[], float] = time.monotonic
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    calls: int = 0
    closed: bool = False
    names: list[str] = field(default_factory=list)  # outils appelés (exécutés ou non), dans l'ordre

    def handle(self, frame: dict[str, Any]) -> dict[str, Any]:
        n = frame.get("n")
        if self.closed:
            return {"n": n, "status": "denied", "content": "jeton révoqué : l'appel execute_code est terminé"}
        if not hmac.compare_digest(str(frame.get("token") or ""), self.token):
            return {"n": n, "status": "denied", "content": "jeton invalide"}
        if self.clock() > self.expires_at:
            return {"n": n, "status": "denied", "content": "jeton expiré"}
        name, args = str(frame.get("name") or ""), frame.get("args")
        if not isinstance(args, dict):
            return {"n": n, "status": "error", "content": "arguments : un objet JSON est attendu"}
        if name in NEVER or name not in self.allowed:
            return {"n": n, "status": "refused",
                    "content": f"outil non appelable depuis execute_code : {name} "
                               f"(appelables : {', '.join(sorted(self.allowed)) or 'aucun'})"}
        if self.calls >= self.max_calls:
            return {"n": n, "status": "refused", "content": f"plafond d'appels d'outils atteint ({self.max_calls})"}
        self.calls += 1
        self.names.append(name)
        try:
            out = self.dispatch(name, args)
        except Exception as e:
            logger.warning("execute_code : appel %s en échec", name, exc_info=True)
            out = {"status": "error", "content": f"{type(e).__name__}: {e}"}
        return {"n": n, **out}


@dataclass
class ExecuteCodeConfig:
    """Bloc `execute_code` de la config (drapeau `execute_code_enabled` à part). Clés inconnues ignorées."""

    tools: tuple[str, ...] = DEFAULT_TOOLS
    timeout_s: int = 60
    max_timeout_s: int = 300
    max_calls: int = 20
    stdout_chars: int = 8000
    return_chars: int = 4000
    error_chars: int = 2000
    python: str = "python3"

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> ExecuteCodeConfig:
        d = dict(d or {})
        known = set(cls.__dataclass_fields__)
        cfg = cls(**{k: v for k, v in d.items() if k in known})
        cfg.tools = tuple(dict.fromkeys(str(t) for t in cfg.tools if str(t) not in NEVER))
        cfg.max_timeout_s = max(1, int(cfg.max_timeout_s))
        cfg.timeout_s = max(1, min(int(cfg.timeout_s), cfg.max_timeout_s))
        cfg.max_calls = max(0, int(cfg.max_calls))
        return cfg


def _spec(cfg: ExecuteCodeConfig) -> ToolSpec:
    listing = ", ".join(cfg.tools) or "aucun"
    return ToolSpec(
        name=TOOL,
        description=("Exécute un script Python dans le bac à sable du fil ; le script peut appeler plusieurs outils "
                     "en un seul appel et rend sa sortie, sa dernière valeur et son erreur."),
        parameters={"type": "object", "required": ["code"], "properties": {
            "code": {"type": "string", "description": (
                "Script Python 3. call_tool(\"nom\", {arguments}) ou tools.nom(arg=...) appelle un outil et rend son "
                "résultat (objet si JSON, texte sinon) ; lève ToolError (échec de l'outil), ToolRefused (refus du "
                "noyau) ou ApprovalPending (carte posée : l'action attend l'utilisateur, rien n'est fait). Outils "
                f"appelables : {listing} ; au plus {cfg.max_calls} appels. print() va dans stdout, la valeur de la "
                "dernière expression dans return (JSON).")},
            "timeout": {"type": "integer", "description": f"secondes (défaut {cfg.timeout_s}, max {cfg.max_timeout_s})",
                        "default": cfg.timeout_s}}},
        # Le script exécute du code arbitraire dans le bac à sable : l'effet d'une commande `python3 …` de terminal.
        effect=Effect.WRITE, core=False, source="builtin:code")


@dataclass
class ExecuteCodeSource:
    """`ToolSource` d'`execute_code`. `resolve(ctx)` rend la source de code du fil (`CodeToolSource`, ou tout objet
    qui a `script_command(thread_id, argv) -> argv`) ou None hors session de code."""

    resolve: Callable[[ToolContext], Any]
    cfg: ExecuteCodeConfig = field(default_factory=ExecuteCodeConfig)
    clock: Callable[[], float] = time.monotonic

    @classmethod
    def from_config(cls, resolve: Callable[[ToolContext], Any], block: dict[str, Any] | None) -> ExecuteCodeSource:
        return cls(resolve=resolve, cfg=ExecuteCodeConfig.from_dict(block))

    def specs(self) -> list[ToolSpec]:
        return [_spec(self.cfg)]

    def _limits(self) -> tuple[int, int, int]:
        return self.cfg.stdout_chars, self.cfg.return_chars, self.cfg.error_chars

    def _result(self, stdout: str, ret: str, error: str, scrub: Callable[[str], str] | None) -> ToolResult:
        return ToolResult(call_id="", name=TOOL, content=format_block(stdout, ret, error, self._limits(), scrub),
                          is_error=bool((error or "").strip()))

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        kernel = ctx.kernel
        scrub = getattr(getattr(kernel, "invariants", None), "scrub_output", None)
        if name != TOOL:
            return ToolResult(call_id="", name=name, content=f"outil inconnu : {name}", is_error=True)
        code = arguments.get("code")
        if not isinstance(code, str) or not code.strip():
            return self._result("", "", "argument manquant : code", scrub)
        if kernel is None or not hasattr(kernel, "script_tool_call"):
            return self._result("", "", "execute_code n'est appelable que dans un tour du noyau", scrub)
        try:
            src = self.resolve(ctx)
        except Exception as e:  # noqa: BLE001
            src, why = None, f"{type(e).__name__}: {e}"
        else:
            why = "pas de session de code sur ce fil : execute_code tourne dans son bac à sable"
        if src is None:
            return self._result("", "", why, scrub)
        try:
            timeout = max(1, min(int(arguments.get("timeout") or self.cfg.timeout_s), self.cfg.max_timeout_s))
        except (TypeError, ValueError):
            timeout = self.cfg.timeout_s
        argv = ["timeout", "-k", "2", str(timeout + 3), self.cfg.python, "-I", "-u", "-c", BOOTSTRAP]
        try:
            cmd = src.script_command(ctx.thread_id, argv)
        except PermissionError as e:
            return self._result("", "", f"REFUSÉ : {e}", scrub)
        except Exception as e:  # noqa: BLE001 — bac à sable indisponible : l'erreur revient au modèle
            return self._result("", "", f"bac à sable indisponible : {type(e).__name__}: {e}", scrub)
        turn = kernel.script_turn(ctx)
        session = ScriptSession(allowed=frozenset(self.cfg.tools), max_calls=self.cfg.max_calls,
                                expires_at=self.clock() + timeout + _GRACE_S,
                                dispatch=lambda n, a: kernel.script_tool_call(n, a, turn, ctx), clock=self.clock)
        cancelled = getattr(kernel, "cancel_requested", None)
        stdout, ret, error = run_script(cmd, code, session, timeout,
                                        cancelled=(lambda: cancelled(ctx.thread_id)) if callable(cancelled) else None)
        return self._result(stdout, ret, error, scrub)


def run_script(cmd: list[str], code: str, session: ScriptSession, timeout: int,
               cancelled: Callable[[], bool] | None = None) -> tuple[str, str, str]:
    """Lance `cmd` (le bootstrap dans le bac à sable), lui envoie le code et le jeton, sert ses appels d'outils
    jusqu'à la trame `done`, l'échéance (+ marge) ou l'annulation du fil. Rend (stdout, return, error)."""
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
    except OSError as e:
        session.closed = True
        return "", "", f"bac à sable injoignable : {type(e).__name__}"
    frames: queue.Queue[bytes | None] = queue.Queue()
    err_chunks: list[bytes] = []

    def pump_out() -> None:
        assert proc.stdout is not None
        while True:
            line = proc.stdout.readline(_MAX_FRAME + 1)
            if not line:
                break
            frames.put(line)
        frames.put(None)

    def pump_err() -> None:
        assert proc.stderr is not None
        total = 0
        for chunk in iter(lambda: proc.stderr.read(4096), b""):
            if total < 64 * 1024:
                err_chunks.append(chunk)
                total += len(chunk)

    threads = [threading.Thread(target=pump_out, daemon=True), threading.Thread(target=pump_err, daemon=True)]
    for t in threads:
        t.start()

    def send(obj: dict[str, Any]) -> bool:
        try:
            assert proc.stdin is not None
            proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    deadline = time.monotonic() + timeout + _GRACE_S
    result: tuple[str, str, str] | None = None
    try:
        if not send({"token": session.token, "code": code, "timeout": timeout, "cap": _TRANSFER_BYTES}):
            result = "", "", "le bac à sable n'a pas accepté le script"
        while result is None:
            if cancelled is not None and cancelled():
                result = "", "", "annulé par l'utilisateur"
                break
            left = deadline - time.monotonic()
            if left <= 0:
                result = "", "", f"TimeoutError: délai dépassé ({timeout} s), script arrêté"
                break
            try:
                line = frames.get(timeout=min(left, 0.5))
            except queue.Empty:
                continue
            if line is None:
                proc.wait(timeout=5)
                tail = b"".join(err_chunks).decode("utf-8", "replace").strip()[-800:]
                result = "", "", (f"le script s'est arrêté sans rendre de résultat (code {proc.returncode})"
                                  + (f" : {tail}" if tail else ""))
                break
            if len(line) > _MAX_FRAME or not line.endswith(b"\n"):
                result = "", "", "trame trop longue : script arrêté"
                break
            try:
                frame = json.loads(line)
            except ValueError:
                result = "", "", "trame illisible : script arrêté"
                break
            if not isinstance(frame, dict):
                result = "", "", "trame illisible : script arrêté"
                break
            if frame.get("t") == "call":
                if not send(session.handle(frame)):
                    result = "", "", "canal du script fermé"
                continue
            if frame.get("t") == "done":
                result = _join(frame.get("stdout")), _join(frame.get("return")), _join(frame.get("error"))
                break
            result = "", "", "trame inconnue : script arrêté"
    finally:
        session.closed = True  # jeton révoqué : plus aucune trame servie
        for pipe in (proc.stdin,):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("execute_code : le client du bac à sable ne s'arrête pas (pid %s)", proc.pid)
    return result


__all__ = ["BOOTSTRAP", "DEFAULT_TOOLS", "NEVER", "TOOL", "ExecuteCodeConfig", "ExecuteCodeSource", "ScriptSession",
           "clip", "format_block", "run_script"]
