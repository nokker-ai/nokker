"""nokker/tools/hooks.py — hooks des sessions de code de Nokker Lokkal (lot X3, docs/LOKKAL.md §7.9).

Un hook est une commande de l'utilisateur lancée à un moment du fil :
  - `session_start` : à l'ouverture de la session (rapporté en événement) ;
  - `pre_tool`      : avant un appel d'outil DÉJÀ permis (règle, mode ou carte approuvée) ; code ≠ 0 bloque l'appel,
                      le résultat est une erreur qui porte la raison du hook ;
  - `post_tool`     : après l'appel ; sa sortie est ajoutée au résultat comme note, pour le modèle ;
  - `turn_end`      : à la fin du tour (rapporté en événement).

Réglages (`settings.json`, clé `hooks` au premier niveau, à côté de `permissions`) :
    {"hooks": {"pre_tool": [{"match": "terminal(git push*)", "command": "./scripts/check-branch.sh", "timeout_s": 10}],
               "post_tool": [{"match": "write_file(src/**)", "command": "ruff check --quiet src"}],
               "subagents": false}}
`match` a la syntaxe des règles de permission (`outil` ou `outil(motif)`, nokker/qa/permissions.py) ; absent ou « * » :
tous les outils (et sans objet pour `session_start` / `turn_end`).

Sécurité :
  - la commande tourne DANS le bac à sable du fil (`docker exec` dans le container de la session), jamais sur l'hôte en
    backend docker ; charge utile JSON sur stdin, délai borné, sortie coupée et caviardée ;
  - un hook ne lève jamais une approbation et ne s'en dispense pas : `pre_tool` ne tourne qu'au moment de l'exécution,
    après les portes (un appel refusé ou en attente de carte ne le déclenche pas) ; il peut bloquer, jamais permettre ;
  - les hooks du DÉPÔT (`.nokker/settings.json`) viennent avec le code, donc d'un tiers possible : ignorés tant que
    l'utilisateur n'a pas déclaré de confiance CE dossier et CETTE empreinte du bloc (`trusted_project_hooks: [{root,
    sha256}]` dans SES réglages, jamais dans ceux du dépôt) ; un bloc modifié a une autre empreinte, donc de nouveau
    ignoré. Le bloc est lu une fois, à l'ouverture : un fichier réécrit pendant la session n'y change rien ;
  - jamais en mode plan ; jamais pendant un sous-agent, sauf bloc qui le demande (`"subagents": true`).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nokker.core.ports import ToolContext
from nokker.core.turn import ToolResult
from nokker.qa.permissions import Mode, Rule

logger = logging.getLogger("nokker.tools.hooks")

EVENTS = ("session_start", "pre_tool", "post_tool", "turn_end")
DEFAULT_TIMEOUT_S = 30
MAX_TIMEOUT_S = 300
OUTPUT_CHARS = 4000  # sortie d'un hook gardée (tête + queue) : note du résultat, raison du blocage
EVENT_OUTPUT_CHARS = 2000  # sortie portée par l'événement `hook.ran` (session_start, turn_end)
PAYLOAD_STR_CHARS = 100_000  # chaîne de la charge utile (argument, résultat) au-delà de quoi on coupe
SUBAGENT_MARK = "/sa-"  # fil d'un sous-agent : `<fil parent>/sa-<id>` (nokker/tools/delegate.py)

# (thread_id, commande, stdin, délai) -> (code de sortie ou None si le hook n'a pas pu tourner, sortie)
Sandbox = Callable[[str, str, str, int], tuple[int | None, str]]


@dataclass(frozen=True)
class Hook:
    event: str
    command: str
    match: str = "*"
    timeout_s: int = DEFAULT_TIMEOUT_S
    source: str = "user"  # user | project
    subagents: bool = False

    def matches(self, name: str, arguments: dict[str, Any]) -> bool:
        if self.match in ("", "*"):
            return True
        from nokker.core.turn import ToolCall

        return Rule.parse(self.match).matches(ToolCall(id="hook", name=name, arguments=arguments))


def parse_hooks(block: Any, source: str) -> list[Hook]:
    """Le bloc `hooks` d'un settings.json → liste de hooks. ValueError sur une forme invalide (le bloc entier est
    alors ignoré : un hook mal écrit ne tourne pas à moitié)."""
    if block in (None, {}):
        return []
    if not isinstance(block, dict):
        raise ValueError("hooks : objet attendu")
    unknown = set(block) - {*EVENTS, "subagents"}
    if unknown:
        raise ValueError(f"hooks : clé inconnue {sorted(unknown)} (attendu : {', '.join(EVENTS)}, subagents)")
    subagents = block.get("subagents", False)
    if not isinstance(subagents, bool):
        raise ValueError("hooks.subagents : booléen attendu")
    out: list[Hook] = []
    for event in EVENTS:
        entries = block.get(event) or []
        if not isinstance(entries, list):
            raise ValueError(f"hooks.{event} : liste attendue")
        for e in entries:
            if not isinstance(e, dict) or not isinstance(e.get("command"), str) or not e["command"].strip():
                raise ValueError(f"hooks.{event} : chaque entrée porte une commande ({{match, command, timeout_s}})")
            match = str(e.get("match") or "*").strip()
            if match != "*":
                Rule.parse(match)  # même syntaxe que les règles de permission ; illisible → ValueError
            try:
                timeout = int(e.get("timeout_s", DEFAULT_TIMEOUT_S))
            except (TypeError, ValueError):
                raise ValueError(f"hooks.{event} : timeout_s entier attendu") from None
            out.append(Hook(event=event, command=e["command"].strip(), match=match,
                            timeout_s=max(1, min(timeout, MAX_TIMEOUT_S)), source=source, subagents=subagents))
    return out


def hooks_sha256(block: Any) -> str:
    """Empreinte d'un bloc `hooks` : JSON canonique (clés triées, sans espaces), comme `args_sha256` des cartes."""
    canon = json.dumps(block, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def read_settings(path: str | Path | None) -> dict[str, Any]:
    """Le settings.json entier ({} s'il est absent). ValueError s'il est illisible."""
    if not path:
        return {}
    p = Path(path).expanduser()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"{p} illisible : {e}") from None
    return data if isinstance(data, dict) else {}


def project_hooks_block(root: str | Path) -> Any:
    """Le bloc `hooks` du `.nokker/settings.json` du dépôt (None s'il n'y en a pas)."""
    return read_settings(Path(root) / ".nokker" / "settings.json").get("hooks")


def is_trusted(user_settings: dict[str, Any], root: str, sha256: str) -> bool:
    entries = user_settings.get("trusted_project_hooks") or []
    return any(isinstance(e, dict) and e.get("root") == root and e.get("sha256") == sha256 for e in entries)


def trust_project_hooks(settings_path: str | Path, root: str, sha256: str) -> None:
    """Écrit la confiance de l'utilisateur dans SES réglages : une entrée par dossier, la nouvelle empreinte remplace
    l'ancienne (un bloc déjà remplacé n'a plus de raison d'être de confiance)."""
    p = Path(settings_path).expanduser()
    data = read_settings(p)
    entries = [e for e in (data.get("trusted_project_hooks") or []) if isinstance(e, dict) and e.get("root") != root]
    data["trusted_project_hooks"] = [*entries, {"root": root, "sha256": sha256}]
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head, tail = text[: limit * 2 // 3], text[-(limit // 3):]
    return f"{head}\n[… {len(text) - len(head) - len(tail)} caractères coupés …]\n{tail}"


@dataclass
class _ThreadHooks:
    root: str
    hooks: list[Hook]
    project: str = "absent"  # absent | trusted | untrusted | invalid
    sha256: str | None = None
    pending: list[Hook] = field(default_factory=list)  # hooks du dépôt en attente de confiance
    notice: str | None = None


@dataclass
class HookRunner:
    """Les hooks des fils de session. `sandbox` exécute une commande dans le bac à sable d'un fil ; `scrub` caviarde
    (valeurs du coffre, motifs) ce qui sort vers le hook et ce qui en revient ; `mode_of(thread_id)` donne le mode de
    permission du fil (plan → aucun hook) ; `emit(nom, données)` publie `hook.ran` et `hook.untrusted`."""

    sandbox: Sandbox
    user_settings: Path | None = None
    scrub: Callable[[str], str] = lambda s: s
    mode_of: Callable[[str], Mode] | None = None
    emit: Callable[[str, dict[str, Any]], Any] | None = None
    # (résultat, spec) -> résultat caviardé : `invariants.redact_result` du noyau, pour ce que reçoit un post_tool
    # (traitement complet d'un outil à effet SECRET, comme avant le contexte du modèle).
    redact: Callable[[ToolResult, Any], ToolResult] | None = None
    background: bool = True  # session_start et turn_end hors du chemin de la réponse (False : tests)
    _threads: dict[str, _ThreadHooks] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    # ── ouverture de session et confiance ──
    def open_session(self, thread_id: str, cwd: str) -> dict[str, Any]:
        """Lit les hooks de l'utilisateur et du dépôt pour CE fil (une fois : la suite de la session n'y relit rien),
        lance `session_start`, rend l'état à montrer au client (`hooks` de la réponse d'ouverture)."""
        root = os.path.realpath(cwd)
        notices: list[str] = []
        try:
            user = read_settings(self.user_settings)
            user_hooks = parse_hooks(user.get("hooks"), "user")
        except ValueError as e:
            user, user_hooks = {}, []
            notices.append(f"hooks de l'utilisateur ignorés : {e}")
        state = _ThreadHooks(root=root, hooks=list(user_hooks))
        try:
            block = project_hooks_block(root)
            project_hooks = parse_hooks(block, "project")
        except ValueError as e:
            block, project_hooks = None, []
            state.project = "invalid"
            notices.append(f"hooks du dépôt ignorés (.nokker/settings.json) : {e}")
        if project_hooks:
            state.sha256 = hooks_sha256(block)
            if is_trusted(user, root, state.sha256):
                state.project, state.hooks = "trusted", [*state.hooks, *project_hooks]
            else:
                state.project, state.pending = "untrusted", project_hooks
                notices.append(
                    f"Ce dépôt déclare {len(project_hooks)} hook(s) dans .nokker/settings.json (empreinte "
                    f"{state.sha256[:12]}…) : ils sont IGNORÉS tant que tu ne leur fais pas confiance. Relis-les, puis "
                    "POST /v1/local/hooks/trust {thread_id, sha256} (jeton local), ou `python -m nokker.cli hooks "
                    f"trust --sha256 {state.sha256}` dans le dossier. Un bloc modifié devra être de nouveau accepté.")
        state.notice = " ".join(notices) or None
        with self._lock:
            self._threads[thread_id] = state
        if state.project == "untrusted":
            self._emit("hook.untrusted", {"thread_id": thread_id, "sha256": state.sha256,
                                          "count": len(state.pending), "notice": state.notice})
        self._later(lambda: self._run_event(thread_id, "session_start", {}))
        return self.status(thread_id) or {}

    def status(self, thread_id: str) -> dict[str, Any] | None:
        state = self._threads.get(thread_id)
        if state is None:
            return None
        return {"enabled": True, "user": sum(1 for h in state.hooks if h.source == "user"),
                "project": state.project, "sha256": state.sha256,
                "project_hooks": [{"event": h.event, "match": h.match, "command": h.command}
                                  for h in (state.pending or [h for h in state.hooks if h.source == "project"])],
                "notice": state.notice}

    def trust(self, thread_id: str, sha256: str) -> tuple[int, dict[str, Any]]:
        """Le propriétaire fait confiance aux hooks du dépôt de CE fil, tels que lus à l'ouverture (`sha256` doit être
        celle qu'il a vue). Écrit dans ses réglages, active les hooks pour le fil."""
        state = self._threads.get(thread_id)
        if state is None or state.project not in ("untrusted", "trusted"):
            return 404, {"error": {"message": "pas de hooks de dépôt sur ce fil"}}
        if sha256 != state.sha256:
            return 409, {"error": {"message": "empreinte différente de celle lue à l'ouverture : relis le bloc"}}
        if self.user_settings is None:
            return 503, {"error": {"message": "pas de réglages de l'utilisateur où écrire la confiance"}}
        trust_project_hooks(self.user_settings, state.root, sha256)
        with self._lock:
            if state.project == "untrusted":
                state.hooks, state.pending, state.project, state.notice = [*state.hooks, *state.pending], [], \
                    "trusted", None
        return 200, {"thread_id": thread_id, "root": state.root, "sha256": sha256, "trusted": True}

    def close(self, thread_id: str) -> None:
        with self._lock:
            self._threads.pop(thread_id, None)

    # ── quels hooks pour cet appel ──
    def hooks_for(self, thread_id: str, event: str, name: str | None = None,
                  arguments: dict[str, Any] | None = None) -> list[Hook]:
        base = thread_id.split(SUBAGENT_MARK, 1)[0]
        child = base != thread_id
        state = self._threads.get(base)
        if state is None:
            return []
        if self.mode_of is not None:
            try:
                if self.mode_of(base) is Mode.PLAN:
                    return []  # plan : lecture seule, aucun code de hook
            except Exception:  # noqa: BLE001 — mode illisible : on ne lance rien
                return []
        return [h for h in state.hooks if h.event == event and (h.subagents or not child)
                and (name is None or h.matches(name, arguments or {}))]

    # ── exécution ──
    def pre_tool(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> str | None:
        """Raison du blocage, ou None : l'appel peut partir. S'arrête au premier hook qui bloque."""
        for h in self.hooks_for(ctx.thread_id, "pre_tool", name, arguments):
            code, output = self._run(ctx.thread_id, h, {"tool": name, "arguments": self._scrub_value(arguments)})
            if code != 0:
                why = output.strip() or ("délai dépassé" if code == 124 else "sans message")
                return (f"BLOQUÉ par un hook pre_tool ({h.source}, {h.match}, code {code}) : {why}\n"
                        "L'outil n'a pas été exécuté.")
        return None

    def post_tool(self, name: str, arguments: dict[str, Any], ctx: ToolContext, result: ToolResult,
                  spec: Any = None) -> str:
        """Notes à ajouter au résultat (chaîne vide : rien)."""
        hooks = self.hooks_for(ctx.thread_id, "post_tool", name, arguments)
        if not hooks:
            return ""
        content = result.content or ""
        if self.redact is not None:
            content = self.redact(dataclasses.replace(result, secret_handles=list(result.secret_handles)),
                                  spec).content or ""
        notes = []
        for h in hooks:
            code, output = self._run(ctx.thread_id, h, {
                "tool": name, "arguments": self._scrub_value(arguments),
                "result": {"content": self._scrub_value(content), "is_error": result.is_error}})
            if output.strip() or code != 0:
                notes.append(f"[hook post_tool {h.match}, code {code}]\n{output.strip()}")
        return ("\n\n" + "\n\n".join(notes)) if notes else ""

    def turn_end(self, turn: Any) -> None:
        payload = {"turn_index": getattr(turn, "index", None), "error": getattr(turn, "error", None),
                   "reply": self._scrub_value(getattr(turn, "reply", "") or "")}
        self._later(lambda: self._run_event(turn.thread_id, "turn_end", payload))

    def _run_event(self, thread_id: str, event: str, payload: dict[str, Any]) -> None:
        for h in self.hooks_for(thread_id, event):
            self._run(thread_id, h, payload, report_output=True)

    def _run(self, thread_id: str, hook: Hook, payload: dict[str, Any],
             report_output: bool = False) -> tuple[int | None, str]:
        from nokker.tools.code import hardline_reason

        stdin = json.dumps({"event": hook.event, "thread_id": thread_id, **payload}, ensure_ascii=False)
        t0 = time.monotonic()
        why = hardline_reason(hook.command)
        if why:  # jamais exécutable, même écrite par l'utilisateur
            code, output = None, f"commande refusée (jamais exécutée) : {why}"
        else:
            try:
                code, output = self.sandbox(thread_id, hook.command, stdin, hook.timeout_s)
            except Exception as e:  # noqa: BLE001 — un bac à sable en panne : le hook a échoué, la session continue
                code, output = None, f"le hook n'a pas pu tourner : {type(e).__name__}: {e}"
        output = _clip(self.scrub(str(output or "")), OUTPUT_CHARS)
        data = {"thread_id": thread_id, "event": hook.event, "match": hook.match, "source": hook.source,
                "exit_code": code, "duration_ms": int((time.monotonic() - t0) * 1000), "timed_out": code == 124}
        if report_output:
            data["output"] = output[-EVENT_OUTPUT_CHARS:]
        self._emit("hook.ran", data)
        return code, output

    def _scrub_value(self, v: Any) -> Any:
        if isinstance(v, str):
            return self.scrub(v[:PAYLOAD_STR_CHARS])
        if isinstance(v, dict):
            return {k: self._scrub_value(x) for k, x in v.items()}
        if isinstance(v, list):
            return [self._scrub_value(x) for x in v]
        return v

    def _emit(self, name: str, data: dict[str, Any]) -> None:
        if self.emit is None:
            return
        try:
            self.emit(name, data)
        except Exception:  # noqa: BLE001 — un événement manqué ne casse jamais un appel d'outil
            logger.debug("emit %s a levé", name, exc_info=True)

    def _later(self, fn: Callable[[], None]) -> None:
        if not self.background:
            fn()
            return
        threading.Thread(target=fn, name="nokker-hook", daemon=True).start()

    def wrap(self, registry: Any) -> HookedTools:
        return HookedTools(inner=registry, runner=self)


@dataclass
class HookedTools:
    """Le registre d'outils du noyau, entouré des hooks : tout `call` (tour du modèle, appel direct du propriétaire,
    exécution d'une carte approuvée) passe par `pre_tool` puis `post_tool`. Le reste est celui du registre."""

    inner: Any
    runner: HookRunner

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        blocked = self.runner.pre_tool(name, arguments, ctx)
        if blocked is not None:
            return ToolResult(call_id="", name=name, content=blocked, is_error=True)
        result = self.inner.call(name, arguments, ctx)
        spec_of = getattr(self.inner, "spec", None)  # un registre en a un ; une source seule (banc, test) non
        note = self.runner.post_tool(name, arguments, ctx, result, spec_of(name) if callable(spec_of) else None)
        if note:  # le noyau caviarde ensuite le résultat entier, note comprise (redact_result)
            result = dataclasses.replace(result, content=(result.content or "") + note)
        return result

    def turn_ended(self, turn: Any) -> None:
        self.runner.turn_end(turn)

    def __getattr__(self, name: str) -> Any:  # spec, find, core, index, turn_done, internal, refresh… : du registre
        if name in ("inner", "runner"):  # pas encore posés (copie, construction) : pas de récursion
            raise AttributeError(name)
        return getattr(self.inner, name)


__all__ = ["EVENTS", "Hook", "HookRunner", "HookedTools", "hooks_sha256", "is_trusted", "parse_hooks",
           "project_hooks_block", "read_settings", "trust_project_hooks"]
