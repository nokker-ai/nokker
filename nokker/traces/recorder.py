"""Enregistreur des appels modèle (docs/TRACES.md §2) : UNE ligne par appel de la boucle du noyau.

Ce qu'aucune table ne gardait avant : les messages EXACTS envoyés au fournisseur à chaque appel (le ledger ne garde que
des jetons par couche, le store que `Turn.messages`, sans le préfixe ni l'historique tels qu'assemblés). Trois crochets
dans `core/loop.py`, tous derrière `Kernel.traces` (None = rien, défaut ; posé seulement si `traces_record: true`) :

  - `sent(turn, messages, tools)`  : dans `_complete`, les messages tels qu'ils partent (après `_wire_messages`, donc
    après le filet de secrets du fil) et les schémas de CET appel ;
  - `answered(turn, comp, call)`   : dans `_record_call`, la réponse du modèle et la position dans le tour (messages et
    verdicts déjà là) — la suite de l'appel est découpée à la fin du tour ;
  - `finish(turn)`                 : après `store.append`, les lignes du tour écrites EN UNE FOIS (puis purge bornée).

Jamais d'effet sur ce que voit le modèle : tout est COPIÉ (aller-retour JSON) avant d'être gardé, rien n'est rendu à
la boucle, et toute erreur de l'enregistreur est journalisée puis avalée par le noyau.

Secrets : à `finish`, prompt et suite repassent par `seal` (= `Invariants.redact_context` : coffre du noyau — y compris
les valeurs scellées par `_seal_secret_result` pendant le tour — et motifs forts), puis par `HANDLE_RE` pour compter
les scellés. Images : une partie `image_url` en data URL est remplacée par sa référence `nokker-image:sha256:<hex>`
(mêmes empreintes que le magasin d'images), jamais les octets.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from nokker.qa.secrets import HANDLE_RE
from nokker.tenancy.context import INTERNAL_TENANT, current_tenant_or_none
from nokker.traces.flags import enabled_flags

logger = logging.getLogger("nokker.traces")

TARGETS = ("beta", "canary", "lokkal", "unknown")
KERNEL_SHA_ENV = "NOKKER_KERNEL_SHA"
# Marque de banc (docs/SELF-IMPROVE.md §5, docs/TRACES.md §4) : un noyau lancé par un banc (bench/conv, bench/keys,
# `nokker improve`) porte `NOKKER_BENCH=<banc>` ; chaque ligne enregistrée reçoit alors `meta.bench` et l'export ne la
# sort JAMAIS (recouvrement nul entre bancs et entraînement). Variable absente : lignes inchangées.
BENCH_ENV = "NOKKER_BENCH"


def bench_mark() -> str:
    """Nom du banc qui fait tourner ce processus, ou "" (production)."""
    return os.environ.get(BENCH_ENV, "").strip()
# Verdicts de routine (passage d'un effet de lecture, porte « ok ») : pas des déclenchements, pas dans la trace.
_ROUTINE_RULES = ("ok", "judge-nul")


def kernel_sha() -> str:
    """Commit du noyau : `NOKKER_KERNEL_SHA` (posé par l'image), sinon `git rev-parse` du dépôt du paquet, sinon ''."""
    env = os.environ.get(KERNEL_SHA_ENV, "").strip()
    if env:
        return env
    root = Path(__file__).resolve().parents[2]
    if (root / ".git").exists():
        try:
            out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                                 timeout=5, check=False)
            return out.stdout.strip() if out.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""
    return ""


def copy_json(obj: Any) -> Any:
    """Copie profonde par JSON : rien de ce que tient la boucle n'est partagé avec la trace."""
    return json.loads(json.dumps(obj, ensure_ascii=False, default=str))


def _image_ref(url: str) -> str:
    head, _, data = url.partition(",")
    try:
        raw = base64.b64decode(data, validate=False) if ";base64" in head else data.encode("utf-8")
    except (ValueError, TypeError):
        raw = url.encode("utf-8")
    return f"nokker-image:sha256:{hashlib.sha256(raw).hexdigest()}"


def strip_images(messages: list[Any]) -> list[Any]:
    """Data URL d'image → référence (en place, sur une COPIE déjà faite par l'appelant)."""
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            img = part.get("image_url")
            if isinstance(img, dict) and isinstance(img.get("url"), str) and img["url"].startswith("data:"):
                img["url"] = _image_ref(img["url"])
    return messages


def message_dict(msg: Any) -> dict[str, Any]:
    if isinstance(msg, dict):
        return copy_json(msg)
    return copy_json(msg.to_openai())


def verdict_dict(v: Any) -> dict[str, Any]:
    def val(x):
        return getattr(x, "value", x)

    return {"gate": val(v.gate), "decision": val(v.decision), "layer": v.layer, "rule": v.rule, "reason": v.reason,
            "subject": v.subject, "strength": v.strength, "probability": v.probability, "judge_model": v.judge_model}


def triggered(verdicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Verdicts qui ont DÉCLENCHÉ quelque chose : toute décision autre que `allow`, et les `allow` qui ne sont pas un
    simple passage (`effect:<effet>` sans suffixe, porte « ok ») — repli de palier, conclusion forcée, GO, allowlist…"""
    out = []
    for v in verdicts:
        rule = str(v.get("rule") or "")
        routine = rule in _ROUTINE_RULES or (rule.startswith("effect:") and rule.count(":") == 1)
        if v.get("decision") != "allow" or not routine:
            out.append(v)
    return out


def secret_handles(obj: Any) -> list[str]:
    text = json.dumps(obj, ensure_ascii=False)
    return sorted({h[len("[secret:"):-1] for h in HANDLE_RE.findall(text)})


class TraceSink(Protocol):
    def write(self, rows: list[dict[str, Any]]) -> None: ...

    def purge(self, retention_days: int) -> int: ...


@dataclass
class MemoryTraceSink:
    """Backend en mémoire (tests, bancs) : mêmes lignes que la table `trace_calls`."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def write(self, rows: list[dict[str, Any]]) -> None:
        with self._lock:
            self.rows.extend(copy_json(rows))

    def purge(self, retention_days: int) -> int:
        cutoff = time.time() - retention_days * 86400
        tenant = current_tenant_or_none()
        with self._lock:
            keep = [r for r in self.rows if not (r.get("tenant_id") == tenant
                                                 and datetime.fromisoformat(r["recorded_at"]).timestamp() < cutoff)]
            n = len(self.rows) - len(keep)
            self.rows[:] = keep
        return n


class PgTraceSink:
    """Table `trace_calls` (migration 045), sous RLS : connexion courte au locataire courant (`connect`)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def write(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        from psycopg.types.json import Jsonb

        from nokker.store.conn import connect

        with connect(self.dsn) as conn, conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    "INSERT INTO trace_calls (thread_id, turn_index, call_index, account_id, kernel_sha, target, "
                    "prompt, tools, completion, meta) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (tenant_id, thread_id, turn_index, call_index) DO NOTHING",
                    (r["thread_id"], r["turn_index"], r["call_index"], r.get("account_id"), r.get("kernel_sha", ""),
                     r.get("target", ""), Jsonb(r["prompt"]), Jsonb(r["tools"]), Jsonb(r["completion"]),
                     Jsonb(r["meta"])))

    def purge(self, retention_days: int) -> int:
        from nokker.store.conn import connect

        with connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM trace_calls WHERE recorded_at < now() - make_interval(days => %s)",
                        (int(retention_days),))
            return cur.rowcount or 0


@dataclass
class TraceRecorder:
    """Voir l'en-tête du module. `seal` : le caviardage du fil (`Invariants.redact_context`) ; None = aucun (tests)."""

    sink: TraceSink
    seal: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None
    kernel_sha: str = ""
    target: str = "unknown"
    retention_days: int = 30
    flags: tuple[str, ...] = ()  # drapeaux allumés (nokker/traces/flags.py), triés : `meta.flags` de chaque ligne
    purge_every_s: float = 3600.0
    bench: str = field(default_factory=bench_mark)  # "" = production ; sinon `meta.bench` sur chaque ligne
    _pending: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict, repr=False)
    _calls: dict[tuple[str, int], list[dict[str, Any]]] = field(default_factory=dict, repr=False)
    # Appels faits à côté de la boucle pendant le tour (conseiller `advise`) : lignes à part, après celles de la boucle,
    # pour ne pas casser `prompt(k+1) = prompt(k) + completion(k)`.
    _side: dict[tuple[str, int], list[dict[str, Any]]] = field(default_factory=dict, repr=False)
    _last_purge: dict[str, float] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.target not in TARGETS:
            raise ValueError(f"traces_target inconnu : {self.target!r} ({' | '.join(TARGETS)})")
        if self.retention_days < 1:
            raise ValueError("traces_retention_days doit valoir au moins 1")

    # ── crochets de la boucle ──
    def sent(self, turn: Any, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> None:
        entry = {"prompt": strip_images(copy_json(messages)), "tools": copy_json(tools or [])}
        with self._lock:
            self._pending[(turn.thread_id, turn.index)] = entry

    def answered(self, turn: Any, comp: Any, call: Any) -> None:
        key = (turn.thread_id, turn.index)
        with self._lock:
            sent = self._pending.pop(key, None)
            if sent is None:  # appel parti sans passer par `_complete` (provider d'un test) : rien d'exact à garder
                return
            usage = call.usage
            self._calls.setdefault(key, []).append({
                **sent,
                "response": message_dict(comp.message),
                "msg_at": len(turn.messages), "verdict_at": len(turn.verdicts),
                "tier": getattr(call.tier, "value", call.tier), "model": call.model, "reasoning": call.reasoning,
                "latency_s": round(float(call.latency_s or 0.0), 3), "route_reason": call.route_reason,
                "tokens": {"prompt": usage.prompt, "cached": usage.cached if usage.cached_reported else None,
                           "completion": usage.completion},
                "finish_reason": getattr(comp, "finish_reason", None),
            })

    def side(self, turn: Any, kind: str, messages: list[dict[str, Any]], comp: Any, call: Any,
             meta: dict[str, Any] | None = None) -> None:
        """Appel fait à côté de la boucle : conseiller (`advisor`) ou correcteur (`grader`). `meta` : champs de plus sur
        la ligne (palier configuré et modèle réel : `advisor_tier`/`advisor_model`, `grader_tier`/`grader_model`)."""
        usage = call.usage
        entry = {"kind": kind, "prompt": strip_images(copy_json(messages)), "response": message_dict(comp.message),
                 "tier": getattr(call.tier, "value", call.tier), "model": call.model,
                 "latency_s": round(float(call.latency_s or 0.0), 3), "route_reason": call.route_reason,
                 "tokens": {"prompt": usage.prompt, "cached": usage.cached if usage.cached_reported else None,
                            "completion": usage.completion},
                 "finish_reason": getattr(comp, "finish_reason", None), "extra_meta": dict(meta or {})}
        with self._lock:
            self._side.setdefault((turn.thread_id, turn.index), []).append(entry)

    def side_rows(self, turn: Any, side: list[dict[str, Any]], start: int) -> list[dict[str, Any]]:
        tenant = current_tenant_or_none()
        tenant_type = "internal" if tenant in (None, INTERNAL_TENANT) else "tester"
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for j, c in enumerate(side):
            prompt, completion = c["prompt"], [c["response"]]
            if self.seal is not None:
                prompt, completion = self.seal(prompt), self.seal(completion)
            rows.append({
                "tenant_id": tenant, "thread_id": turn.thread_id, "turn_index": turn.index, "call_index": start + j,
                "account_id": turn.sender, "kernel_sha": self.kernel_sha, "target": self.target,
                "recorded_at": now, "prompt": prompt, "tools": [], "completion": completion,
                "meta": {"tenant_type": tenant_type, "channel": turn.channel, "side": c["kind"], "tier": c["tier"],
                         "model": c["model"], "reasoning": False, "latency_s": c["latency_s"], "tokens": c["tokens"],
                         "route_reason": c["route_reason"], "finish_reason": c["finish_reason"],
                         "gates_and_invariants": [], "approvals": [], "escalation": {"call": False, "turn": False},
                         "secret_seals": {"count": len(secret_handles(completion)),
                                          "names": secret_handles(completion)},
                         "judge_verdict_v3": None, "turn_error": turn.error, "last_call": False,
                         "flags": list(self.flags),
                         **c.get("extra_meta", {}), **({"bench": self.bench} if self.bench else {})},
            })
        return rows

    def finish(self, turn: Any) -> int:
        key = (turn.thread_id, turn.index)
        with self._lock:
            self._pending.pop(key, None)
            calls = self._calls.pop(key, [])
            side = self._side.pop(key, [])
        if not calls and not side:
            return 0
        rows = self.rows_for(turn, calls) + self.side_rows(turn, side, len(calls))
        self.sink.write(rows)
        self._maybe_purge()
        return len(rows)

    # ── découpage du tour ──
    def rows_for(self, turn: Any, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        tenant = current_tenant_or_none()
        tenant_type = "internal" if tenant in (None, INTERNAL_TENANT) else "tester"
        messages = [message_dict(m) for m in turn.messages]
        verdicts = [verdict_dict(v) for v in turn.verdicts]
        pending_ids = {tc.id: tc.name for tc in getattr(turn, "pending_approvals", [])}
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for k, c in enumerate(calls):
            end_msg = calls[k + 1]["msg_at"] if k + 1 < len(calls) else len(messages)
            end_ver = calls[k + 1]["verdict_at"] if k + 1 < len(calls) else len(verdicts)
            response = c["response"]
            segment = messages[c["msg_at"]:end_msg]
            if segment and segment[0].get("role") == "assistant" and _same_message(segment[0], response):
                segment = segment[1:]  # l'écho de la réponse dans le tour : déjà en tête
            completion = [response, *segment]
            window = verdicts[c["verdict_at"]:end_ver]
            call_ids = {tc.get("id") for tc in response.get("tool_calls") or []}
            approvals = [{"call_id": cid, "tool": name, "state": "created"} for cid, name in pending_ids.items()
                         if cid in call_ids]
            escalated_here = (any(v["decision"] == "escalate" for v in window)
                              or any((tc.get("function") or {}).get("name") == "escalate_to_human"
                                     for tc in response.get("tool_calls") or []))
            prompt = c["prompt"]
            if self.seal is not None:
                prompt, completion = self.seal(prompt), self.seal(completion)
            seals = secret_handles(completion)
            if k == 0:
                seals = sorted({*seals, *getattr(turn.input, "redacted", [])})
            judge_v3 = [v for v in window if str(v.get("rule") or "").startswith("judge:v3")]
            rows.append({
                "tenant_id": tenant, "thread_id": turn.thread_id, "turn_index": turn.index, "call_index": k,
                "account_id": turn.sender, "kernel_sha": self.kernel_sha, "target": self.target,
                "recorded_at": now, "prompt": prompt, "tools": c["tools"], "completion": completion,
                "meta": {
                    "tenant_type": tenant_type, "channel": turn.channel, "tier": c["tier"], "model": c["model"],
                    "reasoning": c["reasoning"], "latency_s": c["latency_s"], "tokens": c["tokens"],
                    "route_reason": c["route_reason"], "finish_reason": c["finish_reason"],
                    "gates_and_invariants": triggered(window),
                    "approvals": approvals,
                    "escalation": {"call": escalated_here, "turn": bool(turn.escalated)},
                    "secret_seals": {"count": len(seals), "names": seals},
                    "judge_verdict_v3": judge_v3[-1] if judge_v3 else None,
                    "turn_error": turn.error,
                    "last_call": k == len(calls) - 1,
                    "flags": list(self.flags),
                    **({"bench": self.bench} if self.bench else {}),
                },
            })
        return rows

    def _maybe_purge(self) -> None:
        tenant = current_tenant_or_none() or ""
        now = time.monotonic()
        with self._lock:
            last = self._last_purge.get(tenant)
            if last is not None and now - last < self.purge_every_s:
                return
            self._last_purge[tenant] = now
        try:
            n = self.sink.purge(self.retention_days)
            if n:
                logger.info("traces : %d appel(s) de plus de %d jours purgé(s)", n, self.retention_days)
        except Exception:  # noqa: BLE001 — une purge manquée se refait au prochain tour
            logger.warning("traces : purge en échec", exc_info=True)


def _same_message(a: dict[str, Any], b: dict[str, Any]) -> bool:
    def ids(m):
        return [tc.get("id") for tc in m.get("tool_calls") or []]

    return (a.get("content") or "") == (b.get("content") or "") and ids(a) == ids(b)


def build_recorder(config: Any, invariants: Any = None) -> TraceRecorder | None:
    """`traces_record: true` et un DSN Postgres → enregistreur sur `trace_calls` ; sinon None (rien n'est enregistré)."""
    if not getattr(config, "traces_record", False):
        return None
    dsn = config.pg_dsn() if hasattr(config, "pg_dsn") else ""
    if not dsn:
        logger.warning("traces_record : aucun DSN Postgres, rien ne sera enregistré")
        return None
    target = (getattr(config, "traces_target", "") or "").strip() or (
        "lokkal" if getattr(config, "local", None) is not None else "unknown")
    seal = getattr(invariants, "redact_context", None)
    return TraceRecorder(sink=PgTraceSink(dsn), seal=seal if callable(seal) else None, kernel_sha=kernel_sha(),
                         target=target, retention_days=int(getattr(config, "traces_retention_days", 30)),
                         flags=tuple(enabled_flags(config)))


__all__ = [
    "BENCH_ENV",
    "KERNEL_SHA_ENV",
    "TARGETS",
    "MemoryTraceSink",
    "PgTraceSink",
    "TraceRecorder",
    "TraceSink",
    "build_recorder",
    "copy_json",
    "kernel_sha",
    "strip_images",
    "triggered",
]
