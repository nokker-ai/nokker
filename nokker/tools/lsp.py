"""nokker/tools/lsp.py — diagnostics de langage à l'édition (lot 13, docs/LOKKAL-LSP.md) : après un `write_file` ou un
`patch` réussi, l'agent apprend s'il a cassé le typage ou la syntaxe du fichier, sans relancer la suite de tests.

Un VRAI serveur de langage (LSP) par (fil, langage), lancé DANS le bac à sable du fil, là où sont les dépendances du
dépôt (venv, node_modules) : `docker exec -i <container du fil>` (backend docker), `kubectl exec -i <pod>` (backend
kubernetes), processus local (backend local). JSON-RPC sur stdio, le minimum : initialize/initialized,
textDocument/didOpen, didChange (texte entier), publishDiagnostics, shutdown/exit. Python : `pyright-langserver
--stdio` ; TypeScript/JavaScript : `typescript-language-server --stdio`. Rien n'est téléchargé à la volée : binaire
absent du bac à sable (`command -v` y échoue) = fonction absente, en silence (pas de bloc, `language_servers: []`).

Drapeaux (config locale, ÉTEINTS par défaut tous les deux) :
  - `local.lsp.enabled` : serveurs démarrés à la première édition d'un fichier de leur langage, événements
    `lsp.diagnostics`, outil `lsp_diagnostics` (READ, hors préfixe stable, appelable par POST /v1/local/tools/call).
    Sans `inline`, l'édition n'attend RIEN : la synchronisation part sur le fil de travail du serveur.
  - `local.lsp.inline` : le résultat de `write_file`/`patch` reçoit en FIN un bloc à balises fixes (ajout pur, aucun
    texte existant ne bouge) ; attente bornée à 6 s (20 s au premier démarrage d'un serveur). Délai dépassé, serveur
    absent ou en échec : aucun bloc, les diagnostics tardifs partent en événement, jamais dans le texte.
Éteints tous les deux : `CodeToolSource.lsp` vaut None, aucun chemin de ce module n'est pris (textes identiques).

Bloc (forme demandée par la relecture du 01.11, docs/LOKKAL-LSP.md) : ERREURS seulement, fichiers touchés par CET
appel, tri chemin puis ligne, 20 lignes au plus puis `… N de plus` ; `N erreurs corrigées` (singulier `1 erreur corrigée`) = erreurs
connues avant l'appel et disparues après (comparées par code et message : les numéros de ligne bougent avec l'édition).
« Avant » = les derniers diagnostics publiés pour ce fichier par le serveur du fil : un fichier jamais ouvert n'a pas
d'avant (limite connue : la première édition qui répare n'annonce rien de réparé).

Plafonds : 2 serveurs par fil, 6 par instance de noyau ; au-delà, le plus ancien inactif est arrêté (un fil occupé par
une synchronisation n'est jamais choisi). Arrêt avec le bac à sable du fil (`Workspace.close`) : shutdown, exit, stdin
fermé (le serveur sort à la fin de son entrée, même quand le client `docker exec` ne le tue pas).

Sécurité : le serveur tourne dans le bac à sable avec les limites de `terminal` ; pyright et tsserver lisent le dépôt
sans en exécuter le code ; aucune configuration du dépôt ne choisit la commande lancée."""
from __future__ import annotations

import json
import logging
import os
import shlex
import signal
import subprocess
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, unquote, urlparse

from nokker.core.ports import ToolSpec
from nokker.core.turn import Effect

logger = logging.getLogger(__name__)

LSP_TOOL = "lsp_diagnostics"
WAIT_S = 6.0  # attente des diagnostics après une édition
FIRST_WAIT_S = 20.0  # premier démarrage d'un serveur (pyright sur un gros dépôt), une fois par (fil, langage)
MAX_PER_THREAD = 2
MAX_TOTAL = 6
MAX_LINES = 20  # lignes d'erreur du bloc, puis « … N more »
MAX_MESSAGE = 200  # message ramené sur une ligne puis coupé
MAX_EVENT_ITEMS = 20
MAX_TOOL_ITEMS = 50  # par fichier, dans le résultat de lsp_diagnostics
MAX_FILE_BYTES = 2_000_000  # au-delà, le fichier n'est pas envoyé au serveur
MAX_RESTARTS = 3  # serveur mort (pod rendu pour inactivité…) : relancé, puis tenu pour absent

# extension -> (serveur, languageId LSP)
LANGUAGES: dict[str, tuple[str, str]] = {
    ".py": ("python", "python"), ".pyi": ("python", "python"),
    ".ts": ("typescript", "typescript"), ".mts": ("typescript", "typescript"), ".cts": ("typescript", "typescript"),
    ".tsx": ("typescript", "typescriptreact"),
    ".js": ("typescript", "javascript"), ".mjs": ("typescript", "javascript"), ".cjs": ("typescript", "javascript"),
    ".jsx": ("typescript", "javascriptreact"),
}
SERVERS: dict[str, tuple[str, ...]] = {
    "python": ("pyright-langserver", "--stdio"),
    "typescript": ("typescript-language-server", "--stdio"),
}
# nom rendu dans `language_servers` (spec validée le 01.11) ; un serveur hors de cette table : le nom de son binaire
SERVER_NAMES: dict[str, str] = {"python": "pyright", "typescript": "typescript-language-server"}
_SEVERITY = {1: "error", 2: "warning", 3: "information", 4: "hint"}

SPEC = ToolSpec(
    name=LSP_TOOL,
    # texte validé mot pour mot par la relecture du 01.11 (docs/LOKKAL-LSP.md) ; en français comme la famille code
    description="Diagnostics du serveur de langage (erreurs de type et de syntaxe) pour des fichiers du dossier de travail. "
                "À utiliser après une édition pour vérifier qu'on n'a pas cassé les types ou les imports, avant de lancer "
                "toute la suite de tests.",
    parameters={"type": "object", "properties": {
        "paths": {"type": "array", "items": {"type": "string"},
                  "description": "relatifs au dossier de travail ; défaut : les fichiers ouverts dans ce fil"},
        "severity": {"type": "string", "enum": ["error", "warning"], "default": "error"}}},
    effect=Effect.READ, core=False, source="builtin:code", fresh=True)


def language_of(path: str) -> tuple[str, str] | None:
    """(serveur, languageId) d'un chemin d'après son extension, ou None (langage non suivi)."""
    return LANGUAGES.get(os.path.splitext(path)[1].lower())


def path_to_uri(path: str) -> str:
    return "file://" + quote(path)


def uri_to_path(uri: str) -> str:
    return os.path.normpath(unquote(urlparse(uri).path))


# ─────────────────────────────── diagnostics et bloc ───────────────────────────────


@dataclass(frozen=True)
class Diag:
    path: str  # relatif au dossier courant du fil
    line: int  # base 1
    col: int  # base 1
    severity: str  # error | warning | information | hint
    code: str
    message: str  # une ligne, ≤ MAX_MESSAGE

    @classmethod
    def from_lsp(cls, path: str, d: dict[str, Any]) -> Diag:
        start = ((d.get("range") or {}).get("start") or {})
        code = d.get("code")
        if isinstance(code, dict):  # forme { value, target } de LSP 3.16
            code = code.get("value")
        return cls(path=path, line=int(start.get("line") or 0) + 1, col=int(start.get("character") or 0) + 1,
                   # sévérité absente : le client choisit, on prend la plus prudente
                   severity=_SEVERITY.get(d.get("severity") or 1, "error"),
                   code="" if code is None else str(code),
                   message=" ".join(str(d.get("message") or "").split())[:MAX_MESSAGE])

    def key(self) -> tuple[str, str]:
        return self.code, self.message

    def text(self) -> str:
        head = f"{self.path}:{self.line}:{self.col} {self.severity}"
        return f"{head} {self.code}: {self.message}" if self.code else f"{head}: {self.message}"

    def view(self) -> dict[str, Any]:
        return {"line": self.line, "col": self.col, "severity": self.severity, "code": self.code,
                "message": self.message}


def _order(d: Diag) -> tuple[str, int, int]:
    return d.path, d.line, d.col


def errors_fixed(before: list[Diag], after: list[Diag]) -> int:
    """Erreurs d'avant absentes d'après, comptées par (code, message) avec leur multiplicité."""
    gone = Counter(d.key() for d in before if d.severity == "error") - \
        Counter(d.key() for d in after if d.severity == "error")
    return sum(gone.values())


def format_block(after: list[Diag], fixed: int = 0) -> str:
    """Le bloc ajouté en fin de résultat, précédé d'une ligne vide ; "" s'il n'y a ni erreur ni réparation."""
    errors = sorted((d for d in after if d.severity == "error"), key=_order)
    if not errors and fixed <= 0:
        return ""
    lines = [d.text() for d in errors[:MAX_LINES]]
    if len(errors) > MAX_LINES:
        lines.append(f"… {len(errors) - MAX_LINES} de plus")
    if fixed > 0:
        lines.append("1 erreur corrigée" if fixed == 1 else f"{fixed} erreurs corrigées")
    return "\n\n[Diagnostics]\n" + "\n".join(lines) + "\n[/Diagnostics]"


# ─────────────────────────────── client JSON-RPC ───────────────────────────────


class LspError(RuntimeError):
    pass


class LspClient:
    """Un serveur de langage sur stdio : `argv` lancé ici (dans `cwd` pour le backend local ; `docker exec -i` ou
    `kubectl exec -i` sinon). Un fil de lecture range réponses et diagnostics ; les requêtes du serveur reçoivent une
    réponse neutre (configuration vide, enregistrements acceptés). `on_publish(chemin, diagnostics)` à chaque
    publication, hors verrou."""

    def __init__(self, argv: list[str], cwd: str | None = None,
                 on_publish: Callable[[str, list[dict[str, Any]]], Any] | None = None,
                 popen: Callable[..., Any] = subprocess.Popen) -> None:
        self.argv = list(argv)
        self.on_publish = on_publish
        self.proc = popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          cwd=cwd, start_new_session=True)
        self.alive = True
        self.last_used = time.monotonic()
        self._cv = threading.Condition()
        self._wlock = threading.Lock()
        self._next_id = 1
        self._responses: dict[int, dict[str, Any]] = {}
        self._seq = 0
        # chemin -> (numéro de publication, version annoncée ou None, diagnostics bruts)
        self.published: dict[str, tuple[int, int | None, list[dict[str, Any]]]] = {}
        self.versions: dict[str, int] = {}  # chemin -> dernière version envoyée (= fichiers ouverts)
        self._reader = threading.Thread(target=self._read_loop, name="lsp-read", daemon=True)
        self._reader.start()

    @property
    def name(self) -> str:
        return os.path.basename(self.argv[0]) if self.argv else ""

    # ── transport ──
    def _send(self, msg: dict[str, Any]) -> None:
        body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        try:
            with self._wlock:
                self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
                self.proc.stdin.flush()
        except (OSError, ValueError) as e:  # tuyau fermé : le serveur est parti
            self._dead()
            raise LspError(f"serveur de langage arrêté ({type(e).__name__})") from e

    def _read_loop(self) -> None:
        out = self.proc.stdout
        try:
            while True:
                length = None
                while True:
                    line = out.readline()
                    if not line:
                        return
                    line = line.strip()
                    if not line:
                        break
                    key, _, value = line.decode("ascii", "replace").partition(":")
                    if key.strip().lower() == "content-length":
                        length = int(value.strip())
                if length is None:
                    continue
                body = out.read(length)
                if len(body) < length:
                    return
                try:
                    msg = json.loads(body)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    self._dispatch(msg)
        except Exception:  # noqa: BLE001 — flux illisible : le serveur est tenu pour arrêté
            logger.debug("lecture du serveur de langage interrompue", exc_info=True)
        finally:
            self._dead()

    def _dead(self) -> None:
        with self._cv:
            self.alive = False
            self._cv.notify_all()

    def _dispatch(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if method is None:  # réponse à l'une de nos requêtes
            with self._cv:
                if isinstance(msg.get("id"), int):
                    self._responses[msg["id"]] = msg
                    self._cv.notify_all()
            return
        if "id" in msg:  # requête du serveur : réponse neutre
            result: Any = None
            if method == "workspace/configuration":
                result = [None] * len((msg.get("params") or {}).get("items") or [])
            elif method == "workspace/workspaceFolders":
                result = []
            try:
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
            except LspError:
                pass
            return
        if method == "textDocument/publishDiagnostics":
            params = msg.get("params") or {}
            path = uri_to_path(str(params.get("uri") or ""))
            diags = [d for d in (params.get("diagnostics") or []) if isinstance(d, dict)]
            version = params.get("version")
            with self._cv:
                self._seq += 1
                self.published[path] = (self._seq, version if isinstance(version, int) else None, diags)
                self._cv.notify_all()
            if self.on_publish is not None:
                try:
                    self.on_publish(path, diags)
                except Exception:  # noqa: BLE001 — un abonné en panne ne casse jamais le client
                    logger.debug("diffusion des diagnostics en échec", exc_info=True)

    # ── protocole ──
    def request(self, method: str, params: Any, timeout: float) -> Any:
        with self._cv:
            rid = self._next_id
            self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        with self._cv:
            while rid not in self._responses:
                left = deadline - time.monotonic()
                if not self.alive or left <= 0:
                    raise LspError(f"{method} : pas de réponse" + ("" if self.alive else " (serveur arrêté)"))
                self._cv.wait(left)
            resp = self._responses.pop(rid)
        if resp.get("error"):
            raise LspError(f"{method} : {str((resp['error'] or {}).get('message') or resp['error'])[:200]}")
        return resp.get("result")

    def notify(self, method: str, params: Any) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def initialize(self, root: str, timeout: float) -> None:
        uri = path_to_uri(root)
        self.request("initialize", {
            "processId": None,  # le client n'est pas dans l'espace de processus du serveur (bac à sable)
            "clientInfo": {"name": "nokker"},
            "rootUri": uri, "rootPath": root,
            "workspaceFolders": [{"uri": uri, "name": os.path.basename(root.rstrip("/")) or "workspace"}],
            "capabilities": {
                "textDocument": {"synchronization": {"dynamicRegistration": False, "didSave": False},
                                 "publishDiagnostics": {"versionSupport": True, "relatedInformation": False}},
                "workspace": {"configuration": True, "workspaceFolders": True}},
        }, timeout)
        self.notify("initialized", {})
        # pyright ne lance son analyse qu'à la première configuration reçue (mesuré, 1.1.390) : réglages vides, il
        # les redemande alors par workspace/configuration (réponse neutre ci-dessus)
        self.notify("workspace/didChangeConfiguration", {"settings": {}})

    def sync(self, path: str, language_id: str, text: str) -> tuple[int, int]:
        """didOpen (première fois) ou didChange (texte entier). Rend (repère de publication, version envoyée)."""
        with self._cv:
            mark = self._seq
            version = self.versions.get(path, 0) + 1
            first = path not in self.versions
            self.versions[path] = version
        self.last_used = time.monotonic()
        uri = path_to_uri(path)
        if first:
            self.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": language_id,
                                                                  "version": version, "text": text}})
        else:
            self.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": version},
                                                   "contentChanges": [{"text": text}]})
        return mark, version

    def wait(self, path: str, mark: int, version: int, deadline: float) -> list[dict[str, Any]] | None:
        """Diagnostics publiés pour `path` APRÈS `mark` (et pour `version` au moins quand le serveur la donne), ou
        None au délai / serveur arrêté."""
        with self._cv:
            while True:
                got = self.published.get(path)
                if got is not None and got[0] > mark and (got[1] is None or got[1] >= version):
                    return got[2]
                left = deadline - time.monotonic()
                if not self.alive or left <= 0:
                    return None
                self._cv.wait(left)

    def current(self, path: str) -> list[dict[str, Any]] | None:
        with self._cv:
            got = self.published.get(path)
            return got[2] if got is not None else None

    def close(self, timeout: float = 2.0) -> None:
        if self.alive:
            try:
                self.request("shutdown", None, timeout)
                self.notify("exit", None)
            except LspError:
                pass
        try:
            self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=timeout)
        except Exception:  # noqa: BLE001 — il ne sort pas : son groupe part
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except Exception:  # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        self._dead()


# ─────────────────────────────── lancement dans le bac à sable ───────────────────────────────


def sandbox_argv(env: Any, cwd: str, argv: list[str]) -> tuple[list[str], str | None]:
    """(argv à lancer ici, dossier du processus local) pour un processus LONG en stdio dans l'environnement du fil.
    docker : `docker exec -i -w <cwd> <container> …` (le container du fil, sous son utilisateur) ; kubernetes :
    `kubectl exec -i <pod> -c <container> -- sh -c 'cd <cwd> && exec …'` ; local : le processus lui-même, dans `cwd`.
    Un environnement prêté à un sous-agent (`SharedEnv`) est celui de son parent."""
    inner = getattr(env, "_inner", env)
    cid = getattr(inner, "_container_id", None)
    if isinstance(cid, str) and cid:
        exe = getattr(inner, "_docker_exe", None) or "docker"
        return [exe, "exec", "-i", "-w", cwd, "-e", "HOME=/tmp", cid, *argv], None
    kubectl, pod = getattr(inner, "kubectl", None), getattr(inner, "pod", None)
    if kubectl is not None and isinstance(pod, str) and pod:
        return kubectl.exec_argv(pod, ["sh", "-c", f"cd {shlex.quote(cwd)} && exec {shlex.join(argv)}"],
                                 stdin=True), None
    return list(argv), cwd


# ─────────────────────────────── serveurs d'un fil ───────────────────────────────


@dataclass
class EditDiagnostics:
    after: list[Diag]
    fixed: int
    complete: bool  # chaque fichier suivi a reçu ses diagnostics dans le délai
    pending: list[str] = field(default_factory=list)  # fichiers sans réponse dans le délai (relatifs)
    answered: list[str] = field(default_factory=list)  # fichiers qui ont reçu leurs diagnostics (relatifs)


class ThreadLsp:
    """Les serveurs d'UN fil (un `Workspace`) : démarrés paresseusement, synchronisés sur un fil de travail unique
    (l'ordre des éditions est celui des appels), arrêtés avec le bac à sable."""

    def __init__(self, manager: LspManager, thread_id: str, env: Any, cwd: str) -> None:
        self.manager, self.thread_id, self.env, self.cwd = manager, thread_id, env, cwd
        self.servers: dict[str, LspClient] = {}  # muté sous manager._lock
        self.absent: set[str] = set()
        self.deaths: Counter[str] = Counter()
        self.busy = False
        self.closed = False
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lsp")

    def rel(self, path: str) -> str:
        return os.path.relpath(path, self.cwd)

    # ── démarrage ──
    def _present(self, binary: str) -> bool:
        try:
            r = self.env.execute(f"command -v {shlex.quote(binary)}", timeout=20)
        except Exception:  # noqa: BLE001 — bac à sable injoignable : fonction absente
            return False
        return isinstance(r, dict) and r.get("returncode") == 0

    def _client(self, lang: str, deadline: float, keep: set[str]) -> LspClient | None:
        with self.manager._lock:
            c = self.servers.get(lang)
            if c is not None and not c.alive:
                self.servers.pop(lang, None)
                self.deaths[lang] += 1
                if self.deaths[lang] >= MAX_RESTARTS:
                    self.absent.add(lang)
                c = None
        if c is not None:
            return c
        if lang in self.absent or self.closed:
            return None
        argv = self.manager.servers.get(lang)
        if not argv:
            self.absent.add(lang)
            return None
        if not self._present(argv[0]):
            self.absent.add(lang)
            return None
        if not self.manager.make_room(self, keep):
            return None  # plafonds atteints, rien d'inactif à arrêter : pas cette fois
        client = None
        try:
            run, cwd = sandbox_argv(self.env, self.cwd, list(argv))
            client = self.manager.launch(run, cwd, lambda path, diags: self._published(path, diags))
            client.initialize(self.cwd, max(0.5, deadline - time.monotonic()))
        except Exception:  # noqa: BLE001 — serveur qui ne démarre pas : absent pour ce fil
            logger.info("serveur de langage %s : démarrage en échec dans le fil %s", argv[0], self.thread_id,
                        exc_info=True)
            self.absent.add(lang)
            if client is not None:
                client.close(timeout=1)
            return None
        with self.manager._lock:
            if self.closed:
                stale = client
            else:
                self.servers[lang], stale = client, None
        if stale is not None:
            stale.close(timeout=1)
            return None
        return client

    # ── synchronisation ──
    def _run(self, files: list[tuple[str, str]], wait: bool) -> EditDiagnostics:
        self.busy = True
        try:
            groups: dict[str, list[tuple[str, str, str]]] = {}
            for path, text in files:
                lang = language_of(path)
                if lang is not None:
                    groups.setdefault(lang[0], []).append((path, lang[1], text))
            if not groups:
                return EditDiagnostics([], 0, False)
            t0 = time.monotonic()
            first = any(lang not in self.servers and lang not in self.absent for lang in groups)
            deadline = t0 + (self.manager.first_wait_s if first else self.manager.wait_s)
            sent: list[tuple[LspClient, str, int, int]] = []
            before: list[Diag] = []
            for lang, items in groups.items():
                client = self._client(lang, deadline, set(groups))
                if client is None:
                    continue
                for path, language_id, text in items:
                    rel = self.rel(path)
                    before.extend(Diag.from_lsp(rel, d) for d in (client.current(path) or []))
                    mark, version = client.sync(path, language_id, text)
                    sent.append((client, path, mark, version))
            if not wait or not sent:
                return EditDiagnostics([], 0, False)
            after: list[Diag] = []
            pending: list[str] = []
            answered: list[str] = []
            for client, path, mark, version in sent:
                got = client.wait(path, mark, version, deadline)
                if got is None:
                    pending.append(self.rel(path))
                    continue
                answered.append(self.rel(path))
                after.extend(Diag.from_lsp(self.rel(path), d) for d in got)
            return EditDiagnostics(after, errors_fixed(before, after), not pending, pending, answered)
        finally:
            self.busy = False

    def touch(self, files: list[tuple[str, str]], wait: bool) -> EditDiagnostics | None:
        """Envoie le texte des fichiers touchés. `wait` : attend leurs diagnostics (délai borné) ; sinon rend None
        tout de suite (la synchronisation suit son cours, les diagnostics partent en événement)."""
        if self.closed:
            return None
        fut = self._exec.submit(self._run, files, wait)
        if not wait:
            return None
        try:
            # le délai est tenu par `_run` ; ceci ne garde que d'une file bouchée par une synchronisation précédente
            return fut.result(timeout=self.manager.first_wait_s + 5)
        except FutureTimeout:
            return None

    def opened(self) -> list[tuple[LspClient, str]]:
        with self.manager._lock:
            clients = list(self.servers.values())
        return [(c, p) for c in clients for p in list(c.versions)]

    def _published(self, path: str, diags: list[dict[str, Any]]) -> None:
        self.manager.publish(self, path, diags)

    def stop(self, lang: str) -> None:
        with self.manager._lock:
            c = self.servers.pop(lang, None)
        if c is not None:
            c.close()

    def close(self) -> None:
        with self.manager._lock:
            self.closed = True
            clients = list(self.servers.values())
            self.servers.clear()
        self.manager.forget(self)
        for c in clients:
            c.close()
        self._exec.shutdown(wait=False, cancel_futures=True)


# ─────────────────────────────── instance ───────────────────────────────


class LspManager:
    """Les serveurs de langage d'une instance de noyau (drapeau `local.lsp.enabled`) : plafonds, lancement,
    diffusion `lsp.diagnostics`. `inline` = drapeau `local.lsp.inline`. `emit(nom, données)` et `scrub(texte)` sont
    posés par le serveur (bus d'événements, caviardage des `tool.result`)."""

    def __init__(self, inline: bool = False, servers: dict[str, tuple[str, ...]] | None = None,
                 wait_s: float = WAIT_S, first_wait_s: float = FIRST_WAIT_S, max_per_thread: int = MAX_PER_THREAD,
                 max_total: int = MAX_TOTAL, emit: Callable[[str, dict[str, Any]], Any] | None = None,
                 scrub: Callable[[str], str] | None = None,
                 launch: Callable[[list[str], str | None, Callable[..., Any]], LspClient] | None = None) -> None:
        self.inline = inline
        self.servers = dict(SERVERS if servers is None else servers)
        self.wait_s, self.first_wait_s = wait_s, first_wait_s
        self.max_per_thread, self.max_total = max_per_thread, max_total
        self.emit, self.scrub = emit, scrub
        self._launch = launch
        self._threads: list[ThreadLsp] = []
        self._lock = threading.RLock()

    def thread(self, thread_id: str, env: Any, cwd: str) -> ThreadLsp:
        t = ThreadLsp(self, thread_id, env, cwd)
        with self._lock:
            self._threads.append(t)
        return t

    def forget(self, t: ThreadLsp) -> None:
        with self._lock:
            if t in self._threads:
                self._threads.remove(t)

    def launch(self, argv: list[str], cwd: str | None, on_publish: Callable[..., Any]) -> LspClient:
        if self._launch is not None:
            return self._launch(argv, cwd, on_publish)
        return LspClient(argv, cwd=cwd, on_publish=on_publish)

    def live(self) -> int:
        with self._lock:
            return sum(len(t.servers) for t in self._threads)

    def make_room(self, t: ThreadLsp, keep: set[str]) -> bool:
        """Place pour un serveur de plus dans `t` : au-delà des plafonds, arrête le plus ancien inactif (du fil, puis
        de l'instance ; jamais un langage de `keep` ni un fil occupé ailleurs). False si rien n'est arrêtable."""
        picks: list[tuple[ThreadLsp, str]] = []
        with self._lock:
            if len(t.servers) >= self.max_per_thread:
                mine = [(c.last_used, lang) for lang, c in t.servers.items() if lang not in keep]
                if not mine:
                    return False
                picks.append((t, min(mine)[1]))
            if sum(len(x.servers) for x in self._threads) - len(picks) >= self.max_total:
                others = [(c.last_used, id(x), x, lang) for x in self._threads if x is t or not x.busy
                          for lang, c in x.servers.items()
                          if not (x is t and lang in keep) and (x, lang) not in picks]
                if not others:
                    return False
                _, _, x, lang = min(others, key=lambda o: o[0])
                picks.append((x, lang))
            victims = [x.servers.pop(lang) for x, lang in picks]
        for v in victims:
            v.close()
        return True

    def publish(self, t: ThreadLsp, path: str, diags: list[dict[str, Any]]) -> None:
        """`lsp.diagnostics {thread_id, path, counts:{error, warning}, items}` (20 items au plus, erreurs d'abord,
        caviardés comme les `tool.result`)."""
        if self.emit is None or t.closed:
            return
        rel = t.rel(path)
        parsed = sorted((Diag.from_lsp(rel, d) for d in diags), key=lambda d: (d.severity != "error", d.line, d.col))
        items = []
        for d in parsed[:MAX_EVENT_ITEMS]:
            item = d.view()
            if self.scrub is not None:
                item["message"] = self.scrub(item["message"])
            items.append(item)
        counts = {"error": sum(d.severity == "error" for d in parsed),
                  "warning": sum(d.severity == "warning" for d in parsed)}
        try:
            self.emit("lsp.diagnostics", {"thread_id": t.thread_id, "path": rel, "counts": counts, "items": items})
        except Exception:  # noqa: BLE001 — un abonné en panne ne casse jamais un outil
            logger.debug("emit lsp.diagnostics a levé", exc_info=True)

    # ── appels de code.py ──
    def after_edit(self, t: ThreadLsp, files: list[tuple[str, str]]) -> str:
        """Après un `write_file`/`patch` réussi : synchronise ; avec `inline`, rend le bloc (ou "")."""
        if not files:
            return ""
        res = t.touch(files, wait=self.inline)
        if not self.inline or res is None or not res.complete:
            return ""
        block = format_block(res.after, res.fixed)
        return self.scrub(block) if block and self.scrub is not None else block

    def diagnostics(self, t: ThreadLsp, files: list[tuple[str, str]] | None, severity: str = "error"
                    ) -> dict[str, Any]:
        """L'outil `lsp_diagnostics` : les fichiers demandés (ouverts et attendus), sinon les fichiers déjà ouverts.
        `{files: [{path, errors, warnings, items: [{line, col, severity, code, message}] (50 au plus)}],
        language_servers, pending?}` ; `pending` (fichiers sans réponse dans le délai) seulement s'il est non vide."""
        keep = ("error", "warning") if severity == "warning" else ("error",)
        pending: list[str] = []
        found: list[Diag] = []
        paths: list[str] = []
        if files is not None:
            res = t.touch(files, wait=True) if files else None
            if res is not None:
                found, pending, paths = res.after, sorted(res.pending), list(res.answered)
        else:
            for client, path in t.opened():
                got = client.current(path)
                if got is None:
                    pending.append(t.rel(path))  # ouvert, rien de publié encore
                    continue
                paths.append(t.rel(path))
                found.extend(Diag.from_lsp(t.rel(path), d) for d in got)
            pending.sort()
        with self._lock:
            names = sorted({SERVER_NAMES.get(lang, c.name) for lang, c in t.servers.items()})
        out_files = []
        for path in sorted(set(paths)):
            mine = [d for d in found if d.path == path]
            items = []
            for d in sorted((d for d in mine if d.severity in keep), key=_order)[:MAX_TOOL_ITEMS]:
                item = d.view()
                if self.scrub is not None:
                    item["message"] = self.scrub(item["message"])
                items.append(item)
            out_files.append({"path": path, "errors": sum(d.severity == "error" for d in mine),
                              "warnings": sum(d.severity == "warning" for d in mine), "items": items})
        out: dict[str, Any] = {"files": out_files, "language_servers": names}
        if pending:
            out["pending"] = pending
        return out

    def close(self) -> None:
        with self._lock:
            threads = list(self._threads)
        for t in threads:
            t.close()


__all__ = ["Diag", "LANGUAGES", "LSP_TOOL", "LspClient", "LspError", "LspManager", "SERVERS", "SERVER_NAMES", "SPEC",
           "ThreadLsp", "errors_fixed", "format_block", "language_of", "sandbox_argv"]
