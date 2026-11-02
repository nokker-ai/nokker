"""Export des traces en lots datés (docs/TRACES.md §3-§6) : `nokker traces export`.

Une ligne JSONL par APPEL MODÈLE :
  {"prompt": [...], "tools": [...], "completion": [...], "meta": {...}}

Sources :
  - `trace_calls` (enregistreur, `traces_record: true`) : prompt EXACT, `meta.reconstructed = false` ;
  - `turns` (historique d'avant l'enregistreur, ou drapeau éteint) : reconstruit depuis `Turn.messages`, prompt partiel
    (sans préfixe, sans historique), `tools = null`, `meta.reconstructed = true`. Jamais pour un tour déjà enregistré.

Filtres : `training_ok` vrai pour le locataire interne ; pour un testeur, seulement si son consentement d'entraînement
(`data_consents`, dernière ligne) est accordé AU MOMENT DE L'EXPORT (un retrait vaut pour le passé). Par défaut, seules
les lignes `training_ok` partent ; `--include-eval` ajoute les autres (évaluation). PII des testeurs : passées par
`pii.py` dans TOUS les cas ; fils des testeurs pseudonymisés. Secrets : scellés à l'enregistrement, puis à nouveau ici
(coffre chargé depuis la config + motifs forts), et une ligne où une valeur connue survivrait est retirée (comptée).

Réponses écrites par le noyau (pas par le modèle) : la réponse « [Correcteur] … » d'un tour `goal_check` et la réponse
d'une commande `/goal` (verdict `turn:synthetic` du tour, `nokker/goals/service.py`). Chacune sort en une ligne à part,
`meta.synthetic = "grader" | "goal_cmd"` (`null` sur une ligne d'appel modèle) ; et dans toute ligne,
`meta.synthetic_in_prompt` liste les positions du prompt (messages `assistant` de l'historique) qui sont ces réponses
du noyau, retrouvées par le verdict de leur tour (même fil, tour antérieur), jamais devinées d'après leur texte.
`meta.flags` : liste triée des drapeaux de config allumés sur le noyau qui a produit la ligne (`nokker/traces/flags.py`) ; `null`
sur une ligne enregistrée avant l'ajout du champ.

Lot : `<out>/traces-<YYYYMMDD>-<n>/` avec `calls.jsonl` (ou `calls-00001.jsonl`… au-delà de `max_lines`),
`manifest.json` et `SHA256SUMS` (format `sha256sum -c`).
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from nokker.qa.secrets import KnownSecrets, redact, redact_messages
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from nokker.traces import pii
from nokker.traces.recorder import TARGETS, copy_json, kernel_sha, triggered, verdict_dict

FORMAT = "nokker-traces/1"
# Fils d'un banc, reconnus même sans `meta.bench` (lignes reconstruites depuis `turns`) : `bench-…`, `improve-…`
# (préfixe du locataire compris, `tn_…:bench-…`). docs/SELF-IMPROVE.md §5.
BENCH_THREAD_PREFIXES = ("bench-", "improve-")
APPROVAL_STATES = {"pending": "created", "expired": "created", "approved": "accepted", "executed": "accepted",
                   "failed": "accepted", "rejected": "refused"}

# Signal de l'utilisateur au tour suivant (règles fixes, sans modèle).
_NO_RE = re.compile(r"^\s*(?:non|no|nope|nein|pas\s+ça|pas\s+du\s+tout|niet)\b", re.IGNORECASE)
_CORRECTION_RE = re.compile(
    r"\b(?:en\s+fait|plutôt|je\s+voulais|c['’]est\s+faux|c['’]est\s+pas\s+ça|ce\s+n['’]est\s+pas|pas\s+ce\s+que|"
    r"tu\s+t['’]es\s+tromp|tu\s+te\s+trompes|erreur|faux|corrige|wrong|actually|i\s+meant|not\s+what|incorrect|"
    r"falsch|eigentlich)\b", re.IGNORECASE)
_NUDGE_RE = re.compile(r"^\s*(?:et\s+alors|alors|relance|\?+|tu\s+es\s+là|t['’]es\s+là|hello\s*\?|ping|réponds|"
                       r"and\s*\?|so\s*\?|still\s+there)\s*[?!.]*\s*$", re.IGNORECASE)


def user_signal(current: str | None, following: str | None) -> dict[str, Any] | None:
    """Ce que dit le tour SUIVANT du même fil : `no` (« non »), `correction`, `relance` (même demande ou relance
    courte), `none` (autre chose). None = pas de tour suivant. Le texte du tour suivant n'est jamais exporté."""
    if following is None:
        return None
    nxt = following.strip()
    if _NO_RE.match(nxt):
        kind = "no"
    elif _CORRECTION_RE.search(nxt):
        kind = "correction"
    elif _NUDGE_RE.match(nxt) or (current and difflib.SequenceMatcher(
            None, current.strip().lower(), nxt.lower()).ratio() >= 0.8):
        kind = "relance"
    else:
        kind = "none"
    return {"kind": kind, "next_turn": True}


class TraceSource(Protocol):
    def tenants(self) -> list[str]: ...

    def recorded(self, tenant: str, since: datetime, until: datetime) -> Iterable[dict[str, Any]]: ...

    def turns(self, tenant: str, since: datetime, until: datetime) -> Iterable[tuple[datetime, dict[str, Any]]]: ...

    def turn(self, tenant: str, thread_id: str, index: int) -> dict[str, Any] | None: ...

    def training_consent(self, tenant: str, account_id: str) -> bool: ...

    def account_email(self, tenant: str, account_id: str) -> str | None: ...

    def approvals(self, tenant: str, thread_id: str, turn_index: int) -> list[dict[str, Any]]: ...

    def synthetic_turns(self, tenant: str, thread_id: str) -> list[dict[str, Any]]: ...


SYNTHETIC_RULE = "turn:synthetic"  # = nokker.goals.service.SYNTHETIC_RULE (import évité : l'export reste léger)


def synthetic_kind(turn: dict[str, Any] | None) -> str | None:
    """`grader` | `goal_cmd` quand la réponse du tour a été écrite par le noyau (verdict `turn:synthetic`), sinon None."""
    for v in (turn or {}).get("verdicts") or []:
        if v.get("rule") == SYNTHETIC_RULE:
            return str(v.get("reason") or "") or None
    return None


def _reply_of(turn: dict[str, Any]) -> str:
    msgs = [m for m in turn.get("messages") or [] if m.get("role") == "assistant"]
    return str((msgs[-1].get("content") if msgs else turn.get("reply")) or "")


@dataclass
class MemoryTraceSource:
    """Source en mémoire (tests) : lignes de `MemoryTraceSink`, tours `(tenant, created_at, turn_dict)`, consentements
    `(tenant, account) -> bool`, adresses, approbations `(tenant, thread, turn) -> [{call_id, tool_name, status}]`."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    turn_rows: list[tuple[str, datetime, dict[str, Any]]] = field(default_factory=list)
    consents: dict[tuple[str, str], bool] = field(default_factory=dict)
    emails: dict[str, str] = field(default_factory=dict)
    approval_rows: dict[tuple[str, str, int], list[dict[str, Any]]] = field(default_factory=dict)

    def tenants(self) -> list[str]:
        seen = {r["tenant_id"] or INTERNAL_TENANT for r in self.rows} | {t for t, _, _ in self.turn_rows}
        return [INTERNAL_TENANT, *sorted(seen - {INTERNAL_TENANT})]

    def recorded(self, tenant, since, until):
        for r in self.rows:
            at = datetime.fromisoformat(r["recorded_at"])
            if (r["tenant_id"] or INTERNAL_TENANT) == tenant and since <= at < until:
                yield {**r, "recorded_at": at}

    def turns(self, tenant, since, until):
        for t, at, d in self.turn_rows:
            if t == tenant and since <= at < until:
                yield at, d

    def turn(self, tenant, thread_id, index):
        for t, _, d in self.turn_rows:
            if t == tenant and d["thread_id"] == thread_id and d["index"] == index:
                return d
        return None

    def training_consent(self, tenant, account_id):
        return bool(self.consents.get((tenant, account_id)))

    def account_email(self, tenant, account_id):
        return self.emails.get(account_id)

    def approvals(self, tenant, thread_id, turn_index):
        return self.approval_rows.get((tenant, thread_id, turn_index), [])

    def synthetic_turns(self, tenant, thread_id):
        return [d for t, _, d in self.turn_rows if t == tenant and d["thread_id"] == thread_id and synthetic_kind(d)]


class PgTraceSource:
    """Base du noyau : chaque locataire lu dans SON `tenant_scope` (RLS), jamais par une valeur joker."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _q(self, tenant: str, sql: str, args: tuple) -> list[dict[str, Any]]:
        from psycopg.rows import dict_row

        from nokker.store.conn import connect

        with tenant_scope(tenant), connect(self.dsn, row_factory=dict_row) as conn:
            return list(conn.execute(sql, args).fetchall())

    def tenants(self) -> list[str]:
        from nokker.store.conn import known_tenants

        return known_tenants(self.dsn)

    def recorded(self, tenant, since, until):
        if not self._q(tenant, "SELECT 1 AS ok WHERE to_regclass('trace_calls') IS NOT NULL", ()):
            return []  # base d'avant la migration 045 : seulement la reconstruction
        return self._q(tenant, "SELECT tenant_id, thread_id, turn_index, call_index, account_id, kernel_sha, target, "
                               "prompt, tools, completion, meta, recorded_at FROM trace_calls "
                               "WHERE recorded_at >= %s AND recorded_at < %s "
                               "ORDER BY recorded_at, thread_id, turn_index, call_index", (since, until))

    def turns(self, tenant, since, until):
        rows = self._q(tenant, "SELECT created_at, turn FROM turns WHERE created_at >= %s AND created_at < %s "
                               "ORDER BY created_at, thread_id, turn_index", (since, until))
        return [(r["created_at"], r["turn"]) for r in rows]

    def turn(self, tenant, thread_id, index):
        rows = self._q(tenant, "SELECT turn FROM turns WHERE thread_id = %s AND turn_index = %s", (thread_id, index))
        return rows[0]["turn"] if rows else None

    def training_consent(self, tenant, account_id):
        rows = self._q(tenant, "SELECT granted FROM data_consents WHERE account_id = %s AND scope = 'training' "
                               "ORDER BY id DESC LIMIT 1", (account_id,))
        return bool(rows and rows[0]["granted"])

    def account_email(self, tenant, account_id):
        rows = self._q(tenant, "SELECT email FROM accounts WHERE id = %s", (account_id,))
        return rows[0]["email"] if rows else None

    def approvals(self, tenant, thread_id, turn_index):
        return self._q(tenant, "SELECT call_id, tool_name, status FROM approvals WHERE thread_id = %s "
                               "AND turn_index = %s ORDER BY created_at", (thread_id, turn_index))

    def synthetic_turns(self, tenant, thread_id):
        rows = self._q(tenant, "SELECT turn FROM turns WHERE thread_id = %s AND turn->'verdicts' @> %s::jsonb "
                               "ORDER BY turn_index", (thread_id, json.dumps([{"rule": SYNTHETIC_RULE}])))
        return [r["turn"] for r in rows if synthetic_kind(r["turn"])]


@dataclass
class ExportOptions:
    since: datetime
    until: datetime
    include_eval: bool = False
    targets: tuple[str, ...] = ()  # vide = toutes
    tenant_types: tuple[str, ...] = ()  # internal | tester ; vide = les deux
    reconstruct: bool = True
    assume_target: str = "unknown"  # cible des lignes reconstruites (le tour ne la porte pas)
    pii_internal: bool = False  # passage PII aussi sur le locataire interne
    max_lines: int = 0  # 0 = un seul calls.jsonl


@dataclass
class ExportResult:
    path: Path
    manifest: dict[str, Any]


def _secret_seal(known: KnownSecrets | None):
    def scrub(text: str) -> str:
        return redact(text, known, weak=False, wire=True).text

    def seal_obj(obj: Any) -> Any:
        if isinstance(obj, str):
            return scrub(obj)
        if isinstance(obj, list):
            if obj and all(isinstance(m, dict) and "role" in m for m in obj):
                obj = redact_messages(obj, scrub=scrub)  # arguments d'outils décodés comme au fil
            return [seal_obj(x) for x in obj]
        if isinstance(obj, dict):
            return {k: seal_obj(v) for k, v in obj.items()}
        return obj

    return seal_obj


def _known_values(known: KnownSecrets | None) -> list[str]:
    return [v for _, v in (known.items_by_length_desc() if known is not None else []) if len(v) >= 6]


def _pseudo(thread_id: str) -> str:
    return "t-" + hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:16]


def _input_text(turn: dict[str, Any] | None) -> str | None:
    return None if turn is None else str((turn.get("input") or {}).get("text") or "")


def _reconstruct(turn: dict[str, Any]) -> list[dict[str, Any]]:
    """Appels d'un tour d'avant l'enregistreur : le k-ième appel ↔ le k-ième message assistant (aligné exactement si
    les nombres concordent, sinon `alignment: approx`)."""
    from nokker.store.pg import turn_from_dict

    t = turn_from_dict(turn)
    msgs = [m.to_openai() for m in t.messages]
    assistant_at = [i for i, m in enumerate(msgs) if m["role"] == "assistant"]
    n = min(len(t.calls), len(assistant_at))
    exact = len(t.calls) == len(assistant_at)
    opening = {"role": "user", "content": (t.head + "\n\n" if t.head else "") + t.input.text}
    verdicts = triggered([verdict_dict(v) for v in t.verdicts])
    out = []
    for k in range(n):
        at = assistant_at[k]
        end = assistant_at[k + 1] if k + 1 < len(assistant_at) else len(msgs)
        c = t.calls[k]
        out.append({
            "call_index": k, "prompt": [opening, *msgs[:at]], "tools": None, "completion": msgs[at:end],
            "meta": {"tier": c.tier.value, "model": c.model, "reasoning": c.reasoning,
                     "latency_s": round(float(c.latency_s or 0.0), 3),
                     "tokens": {"prompt": c.usage.prompt, "cached": c.usage.cached if c.usage.cached_reported else None,
                                "completion": c.usage.completion},
                     "gates_and_invariants": verdicts if k == n - 1 else [], "verdict_scope": "turn",
                     "approvals": [{"call_id": p.id, "tool": p.name, "state": "created"} for p in t.pending_approvals
                                   if any(tc.get("id") == p.id for tc in msgs[at].get("tool_calls") or [])],
                     "escalation": {"call": None, "turn": t.escalated},
                     "secret_seals": {"count": len(t.input.redacted), "names": sorted(t.input.redacted)}
                     if k == 0 else {"count": 0, "names": []},
                     "judge_verdict_v3": None, "turn_error": t.error, "last_call": k == n - 1,
                     "tenant_type": None, "channel": t.channel, "alignment": "exact" if exact else "approx"},
        })
    return out


def export(source: TraceSource, out_dir: str | Path, opts: ExportOptions, *, known: KnownSecrets | None = None,
           exporter_sha: str | None = None, now: datetime | None = None) -> ExportResult:
    now = now or datetime.now(timezone.utc)
    seal = _secret_seal(known)
    values = _known_values(known)
    counts: Counter = Counter()
    pii_counts: Counter = Counter()
    lines: list[tuple[str, dict[str, Any]]] = []  # (clé de tri, ligne)
    turn_cache: dict[tuple[str, str, int], dict[str, Any] | None] = {}
    synthetic_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def synthetic_in_prompt(tenant: str, thread: str, index: int, prompt: Any) -> list[int]:
        """Positions des messages `assistant` du prompt qui sont la réponse d'un tour antérieur du fil écrite par le
        noyau (verdict `turn:synthetic`)."""
        fn = getattr(source, "synthetic_turns", None)
        if fn is None or not isinstance(prompt, list):
            return []
        key = (tenant, thread)
        if key not in synthetic_cache:
            synthetic_cache[key] = list(fn(tenant, thread))
        replies = {_reply_of(d) for d in synthetic_cache[key] if int(d.get("index", -1)) < index}
        return [i for i, m in enumerate(prompt) if isinstance(m, dict) and m.get("role") == "assistant"
                and not m.get("tool_calls") and str(m.get("content") or "") in replies]

    def get_turn(tenant: str, thread: str, index: int) -> dict[str, Any] | None:
        key = (tenant, thread, index)
        if key not in turn_cache:
            turn_cache[key] = source.turn(tenant, thread, index)
        return turn_cache[key]

    for tenant in source.tenants():
        tenant_type = "internal" if tenant == INTERNAL_TENANT else "tester"
        if opts.tenant_types and tenant_type not in opts.tenant_types:
            continue
        consent_cache: dict[str, bool] = {}
        terms_cache: dict[str, list[str]] = {}

        def training_ok(account: str | None, tenant: str = tenant, tenant_type: str = tenant_type,
                        consent_cache: dict[str, bool] = consent_cache) -> bool:
            if tenant_type == "internal":
                return True
            if not account:
                return False
            if account not in consent_cache:
                consent_cache[account] = source.training_consent(tenant, account)
            return consent_cache[account]

        def terms(account: str | None, tenant: str = tenant,
                  terms_cache: dict[str, list[str]] = terms_cache) -> list[str]:
            if not account:
                return []
            if account not in terms_cache:
                terms_cache[account] = pii.name_terms(source.account_email(tenant, account))
            return terms_cache[account]

        def emit(at: datetime, thread: str, index: int, call: int, account: str | None, target: str, sha: str,
                 prompt: Any, tools: Any, completion: Any, meta: dict[str, Any], reconstructed: bool,
                 tenant: str = tenant, tenant_type: str = tenant_type, training_ok: Any = training_ok,
                 terms: Any = terms, synthetic: str | None = None) -> None:
            if opts.targets and target not in opts.targets:
                counts["excluded_target"] += 1
                return
            if meta.get("bench") or str(thread).rsplit(":", 1)[-1].startswith(BENCH_THREAD_PREFIXES):
                # Ligne d'un banc (docs/SELF-IMPROVE.md §5) : jamais exportée, même avec --include-eval — aucun banc
                # ne doit atteindre l'entraînement.
                counts["excluded_bench"] += 1
                return
            ok = training_ok(account)
            if not ok and not opts.include_eval:
                counts["excluded_no_training_consent"] += 1
                return
            in_prompt = synthetic_in_prompt(tenant, thread, index, prompt)  # avant scellés et PII : texte d'origine
            prompt, tools, completion = seal(copy_json(prompt)), copy_json(tools), seal(copy_json(completion))
            if tenant_type == "tester" or opts.pii_internal:
                t = terms(account)
                prompt, tools = pii.scrub_obj(prompt, t, pii_counts), pii.scrub_obj(tools, t, pii_counts)
                completion = pii.scrub_obj(completion, t, pii_counts)
            cur, nxt = get_turn(tenant, thread, index), get_turn(tenant, thread, index + 1)
            signal = user_signal(_input_text(cur), _input_text(nxt))
            approvals = list(meta.get("approvals") or [])
            states = {a.get("call_id"): APPROVAL_STATES.get(a.get("status"), a.get("status"))
                      for a in source.approvals(tenant, thread, index)}
            for a in approvals:
                a["state"] = states.get(a["call_id"], a["state"])
            m = {
                "kernel_sha": sha, "target": target, "tenant_type": tenant_type,
                "thread": thread if tenant_type == "internal" else _pseudo(thread), "turn": index, "call": call,
                "tier": meta.get("tier"), "model": meta.get("model"), "latency_s": meta.get("latency_s"),
                "tokens": meta.get("tokens"), "gates_and_invariants": meta.get("gates_and_invariants") or [],
                "approvals": approvals, "escalation": meta.get("escalation"),
                "secret_seals": meta.get("secret_seals") or {"count": 0, "names": []},
                "judge_verdict_v3": meta.get("judge_verdict_v3"), "user_signal": signal, "training_ok": ok,
                "reconstructed": reconstructed, "at": at.astimezone(timezone.utc).isoformat(),
                "synthetic": synthetic, "synthetic_in_prompt": in_prompt,
                "flags": sorted(meta["flags"]) if isinstance(meta.get("flags"), list) else None,  # lignes d'avant : null
                "extra": {k: meta[k] for k in ("reasoning", "route_reason", "finish_reason", "turn_error", "last_call",
                                               "channel", "verdict_scope", "alignment", "shadow", "side",
                                               "advisor_tier", "advisor_model", "grader_tier", "grader_model")
                          if k in meta},
            }
            line = {"prompt": prompt, "tools": tools, "completion": completion, "meta": m}
            blob = json.dumps(line, ensure_ascii=False)
            if any(v in blob for v in values):  # filet : une valeur du coffre survivante → la ligne ne part pas
                counts["dropped_secret_guard"] += 1
                return
            counts["lines"] += 1
            counts["training_ok" if ok else "eval_only"] += 1
            counts["reconstructed" if reconstructed else "recorded"] += 1
            if synthetic:
                counts[f"synthetic:{synthetic}"] += 1
            counts[f"target:{target}"] += 1
            counts[f"tenant_type:{tenant_type}"] += 1
            lines.append((f"{m['at']}|{thread}|{index:08d}|{call:04d}", line))

        recorded_turns: set[tuple[str, int]] = set()
        for r in source.recorded(tenant, opts.since, opts.until):
            recorded_turns.add((r["thread_id"], r["turn_index"]))
            emit(r["recorded_at"], r["thread_id"], r["turn_index"], r["call_index"], r.get("account_id"),
                 r.get("target") or "unknown", r.get("kernel_sha") or "", r["prompt"], r["tools"], r["completion"],
                 r["meta"], False)
        for at, d in source.turns(tenant, opts.since, opts.until):
            kind = synthetic_kind(d)
            if kind:  # réponse écrite par le noyau : une ligne à part, marquée (jamais prise pour un appel modèle)
                turn_cache[(tenant, d["thread_id"], d["index"])] = d
                emit(at, d["thread_id"], d["index"], 0, d.get("sender"), opts.assume_target, "",
                     [{"role": "user", "content": _input_text(d) or ""}], None,
                     [{"role": "assistant", "content": _reply_of(d)}],
                     {"tier": None, "model": None, "channel": d.get("channel")}, True, synthetic=kind)
                continue
            if opts.reconstruct:
                if (d["thread_id"], d["index"]) in recorded_turns:
                    continue
                turn_cache[(tenant, d["thread_id"], d["index"])] = d
                for c in _reconstruct(d):
                    emit(at, d["thread_id"], d["index"], c["call_index"], d.get("sender"), opts.assume_target, "",
                         c["prompt"], c["tools"], c["completion"], c["meta"], True)

    lines.sort(key=lambda x: x[0])
    return _write_lot(Path(out_dir), [ln for _, ln in lines], opts, counts, pii_counts,
                      exporter_sha if exporter_sha is not None else kernel_sha(), now)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def lot_dir(out_dir: Path, now: datetime) -> Path:
    day = now.astimezone(timezone.utc).strftime("%Y%m%d")
    n = 1
    while (out_dir / f"traces-{day}-{n}").exists():
        n += 1
    return out_dir / f"traces-{day}-{n}"


def _write_lot(out_dir: Path, lines: list[dict[str, Any]], opts: ExportOptions, counts: Counter, pii_counts: Counter,
               sha: str, now: datetime) -> ExportResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = lot_dir(out_dir, now)
    path.mkdir()
    chunks = [lines] if not opts.max_lines or len(lines) <= opts.max_lines else \
        [lines[i:i + opts.max_lines] for i in range(0, len(lines), opts.max_lines)]
    files = []
    for i, chunk in enumerate(chunks, 1):
        name = "calls.jsonl" if len(chunks) == 1 else f"calls-{i:05d}.jsonl"
        with (path / name).open("w", encoding="utf-8") as f:
            for ln in chunk:
                f.write(json.dumps(ln, ensure_ascii=False, separators=(",", ":")) + "\n")
        files.append({"name": name, "lines": len(chunk), "sha256": _sha256(path / name)})
    ats = [ln["meta"]["at"] for ln in lines]
    manifest = {
        "format": FORMAT,
        "kernel_sha": sha,
        "kernel_shas_in_lines": sorted({ln["meta"]["kernel_sha"] for ln in lines if ln["meta"]["kernel_sha"]}),
        "created_at": now.astimezone(timezone.utc).isoformat(),
        "date_range": {"since": opts.since.isoformat(), "until": opts.until.isoformat(),
                       "first_call_at": min(ats) if ats else None, "last_call_at": max(ats) if ats else None},
        "filters": {"include_eval": opts.include_eval, "targets": list(opts.targets) or ["*"],
                    "tenant_types": list(opts.tenant_types) or ["*"], "reconstruct": opts.reconstruct,
                    "assume_target": opts.assume_target, "pii_internal": opts.pii_internal,
                    "max_lines": opts.max_lines},
        "counts": dict(sorted(counts.items())),
        "pii": {"applied_to": "tester" + (" + internal" if opts.pii_internal else ""),
                "replacements": dict(sorted(pii_counts.items())), "covers": pii.COVERED, "not_covered": pii.NOT_COVERED},
        "files": files,
    }
    (path / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sums = [f"{f['sha256']}  {f['name']}" for f in files] + [f"{_sha256(path / 'manifest.json')}  manifest.json"]
    (path / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
    return ExportResult(path=path, manifest=manifest)


def verify_lot(path: str | Path) -> list[str]:
    """Fichiers dont l'empreinte ne correspond pas à SHA256SUMS (liste vide = lot intact)."""
    p = Path(path)
    bad = []
    for line in (p / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        if not (p / name).exists() or _sha256(p / name) != digest:
            bad.append(name)
    return bad


__all__ = [
    "APPROVAL_STATES",
    "FORMAT",
    "TARGETS",
    "ExportOptions",
    "ExportResult",
    "MemoryTraceSource",
    "PgTraceSource",
    "TraceSource",
    "export",
    "lot_dir",
    "user_signal",
    "verify_lot",
]
