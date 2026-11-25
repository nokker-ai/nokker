"""`Store` Postgres (`core.ports.Store`, psycopg 3) — PLAN-P1 §2.7 : la session EST le pilier (a), elle
naît en Postgres dès le jour 1 (D3), pas en SQLite. Un `Turn` est sérialisé en JSONB (colonnes dédiées
`verdicts`/`llm_calls` en plus, pour que le Ledger — F6 — les requête sans reparser le tour).

`turns` est append-only : la migration `migrations/001_init.sql` pose un trigger qui refuse toute
UPDATE/DELETE. `PgStore.append()` ne fait donc qu'un INSERT, jamais un UPSERT.

`MemoryStore` : backend `Store` en mémoire, pour les tests unitaires (aucun réseau). Le `Ledger` avec
coût/CHF réel est `nokker.ledger.costs` (F6, `InMemoryLedger`/`PostgresLedger` — mêmes noms, module
différent ; `api/server.py` les branche, pas ceux d'ici)."""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from nokker.core.turn import (
    Decision,
    Gate,
    ImageRef,
    InputCheck,
    LLMCall,
    Message,
    Tier,
    ToolCall,
    Turn,
    Usage,
    Verdict,
)
from nokker.store.conn import TenantConn, migrate

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def migration_files() -> list[Path]:
    """Toutes les migrations du noyau : celles des piliers (`nokker/<pilier>/migrations/`, tables indépendantes) puis
    celles d'ici, dans l'ordre alphabétique — `006_tenant_rls.sql` passe donc après la création de toutes les tables."""
    pillars = sorted(p for p in MIGRATIONS_DIR.parent.parent.glob("*/migrations/*.sql") if p.parent != MIGRATIONS_DIR)
    return [*pillars, *sorted(MIGRATIONS_DIR.glob("*.sql"))]


def apply_migrations(dsn: str, *, strict: bool = False) -> bool:
    """Applique `migration_files()` en propriétaire (`NOKKER_PG_OWNER_DSN` s'il est posé, sinon `dsn` :
    nokker/store/conn.py::migrate). Idempotent (IF NOT EXISTS, CREATE OR REPLACE, états vérifiés) — rejoué sans danger
    au démarrage. Sans les droits : False et un log (le job d'init s'en est chargé), ou exception si `strict`."""
    return migrate(dsn, *(p.read_text(encoding="utf-8") for p in migration_files()), rls=False, strict=strict)


# ──────────────────────────── sérialisation Turn ↔ JSON ────────────────────────────


def _enc_usage(u: Usage) -> dict[str, Any]:
    d = {"prompt": u.prompt, "completion": u.completion, "cached": u.cached, "reasoning": u.reasoning,
         "cached_reported": u.cached_reported, "reasoning_reported": u.reasoning_reported}
    if u.cache_write:  # amont au format Anthropic seulement : sans écriture de cache, le JSON est celui d'avant
        d["cache_write"] = u.cache_write
        if u.cache_ttl != "5m":  # défaut non écrit : le JSON des tours d'avant ne change pas
            d["cache_ttl"] = u.cache_ttl
    return d


def _dec_usage(d: dict[str, Any]) -> Usage:
    return Usage(prompt=d.get("prompt", 0), completion=d.get("completion", 0), cached=d.get("cached", 0),
                 reasoning=d.get("reasoning", 0), cached_reported=d.get("cached_reported", False),
                 reasoning_reported=d.get("reasoning_reported", False), cache_write=d.get("cache_write", 0), cache_ttl=d.get("cache_ttl", "5m"))


def _enc_tool_call(tc: ToolCall) -> dict[str, Any]:
    return {"id": tc.id, "name": tc.name, "arguments": tc.arguments, "raw_arguments": tc.raw_arguments}


def _dec_tool_call(d: dict[str, Any]) -> ToolCall:
    return ToolCall(id=d["id"], name=d["name"], arguments=d.get("arguments", {}),
                     raw_arguments=d.get("raw_arguments", ""))


def _enc_message(m: Message) -> dict[str, Any]:
    d = {"role": m.role, "content": m.content, "tool_calls": [_enc_tool_call(t) for t in m.tool_calls],
         "tool_call_id": m.tool_call_id, "name": m.name}
    if m.thinking_blocks:  # JSONB : clé ajoutée seulement quand il y en a, les anciens tours se relisent à l'identique
        d["thinking_blocks"] = m.thinking_blocks
    return d


def _dec_message(d: dict[str, Any]) -> Message:
    return Message(role=d["role"], content=d.get("content", ""),
                    tool_calls=[_dec_tool_call(t) for t in d.get("tool_calls", [])],
                    tool_call_id=d.get("tool_call_id"), name=d.get("name"),
                    thinking_blocks=list(d.get("thinking_blocks") or []))


def _enc_call(c: LLMCall) -> dict[str, Any]:
    return {"tier": c.tier.value, "model": c.model, "reasoning": c.reasoning,
            "prefix_sha256": c.prefix_sha256, "layer_tokens": c.layer_tokens, "usage": _enc_usage(c.usage),
            "latency_s": c.latency_s, "route_reason": c.route_reason, "chf": c.chf,
            "reusable_tokens": c.reusable_tokens, "call_id": c.call_id,
            **({"model_served": c.model_served} if c.model_served else {}),
            **({"upstream": c.upstream} if c.upstream else {})}


def _dec_call(d: dict[str, Any]) -> LLMCall:
    return LLMCall(tier=Tier(d["tier"]), model=d["model"], reasoning=d["reasoning"],
                   prefix_sha256=d["prefix_sha256"], layer_tokens=d.get("layer_tokens", {}),
                   usage=_dec_usage(d["usage"]), latency_s=d.get("latency_s", 0.0),
                   route_reason=d.get("route_reason", ""), chf=d.get("chf"),
                   reusable_tokens=d.get("reusable_tokens"), **({"call_id": d["call_id"]} if d.get("call_id") else {}),
                   model_served=d.get("model_served") or None, upstream=d.get("upstream") or None)


def _enc_verdict(v: Verdict) -> dict[str, Any]:
    return {"gate": v.gate.value, "decision": v.decision.value, "layer": v.layer, "rule": v.rule,
            "reason": v.reason, "probability": v.probability, "judge_model": v.judge_model,
            "latency_s": v.latency_s, "subject": v.subject, "strength": v.strength}


def _dec_verdict(d: dict[str, Any]) -> Verdict:
    return Verdict(gate=Gate(d["gate"]), decision=Decision(d["decision"]), layer=d["layer"],
                   rule=d["rule"], reason=d.get("reason", ""), probability=d.get("probability"),
                   judge_model=d.get("judge_model"), latency_s=d.get("latency_s", 0.0),
                   subject=d.get("subject"), strength=d.get("strength"))


def _enc_input(i: InputCheck) -> dict[str, Any]:
    out = {"text": i.text, "redacted": i.redacted, "domain_flags": sorted(i.domain_flags)}
    if i.origin:
        out["origin"] = i.origin
    return out


def _dec_input(d: dict[str, Any]) -> InputCheck:
    return InputCheck(text=d["text"], redacted=d.get("redacted", []), domain_flags=set(d.get("domain_flags", [])),
                      origin=str(d.get("origin") or ""))


def turn_to_dict(turn: Turn) -> dict[str, Any]:
    out = {
        "thread_id": turn.thread_id, "index": turn.index, "input": _enc_input(turn.input),
        "channel": turn.channel, "sender": turn.sender,
        "reply": turn.reply, "messages": [_enc_message(m) for m in turn.messages],
        "calls": [_enc_call(c) for c in turn.calls], "verdicts": [_enc_verdict(v) for v in turn.verdicts],
        "escalated": turn.escalated, "pending_approvals": [_enc_tool_call(t) for t in turn.pending_approvals],
        "error": turn.error, "injected": list(turn.injected), "head": turn.head,
        "images": [i.to_dict() for i in turn.images],  # références seules (X7), jamais les octets
    }
    if turn.texts:  # pièces texte de l'appareil (P9), absentes des tours d'avant
        out["texts"] = [dict(t) for t in turn.texts]
    if turn.lang_seen:  # dernière langue reconnue sur le fil (absente des tours d'avant et des tours sans message humain)
        out["lang"] = turn.lang_seen
    return out


def turn_from_dict(d: dict[str, Any]) -> Turn:
    return Turn(
        thread_id=d["thread_id"], index=d["index"], input=_dec_input(d["input"]),
        channel=d.get("channel", "api"), sender=d.get("sender"), reply=d.get("reply", ""),
        messages=[_dec_message(m) for m in d.get("messages", [])],
        calls=[_dec_call(c) for c in d.get("calls", [])],
        verdicts=[_dec_verdict(v) for v in d.get("verdicts", [])],
        escalated=d.get("escalated", False),
        pending_approvals=[_dec_tool_call(t) for t in d.get("pending_approvals", [])],
        error=d.get("error"), injected=list(d.get("injected", [])), head=d.get("head", ""),
        images=[ImageRef.from_dict(i) for i in d.get("images", [])],
        texts=[dict(t) for t in d.get("texts", [])], lang_seen=str(d.get("lang") or ""),
    )


# ──────────────────────────────── Store Postgres ────────────────────────────────


SEARCH_TIMEOUT_MS = 2000  # `GET /v1/threads/search` : réponse en 2 s au plus (contrat C3 ; le client coupe à 3 s)


def _like_escape(text: str, *, wrap: bool = True) -> str:
    """Texte littéral pour LIKE/ILIKE (échappement `\\` par défaut de Postgres) ; `wrap` : contient."""
    esc = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%" if wrap else esc


@dataclass
class PgStore:
    dsn: str

    def __post_init__(self) -> None:
        self._db = TenantConn(self.dsn)
        self._lock = threading.Lock()

    @property
    def _conn(self) -> Any:
        return self._db.get()  # app.tenant_id = locataire courant, reposé à chaque usage (NoTenant hors scope)

    def next_index(self, thread_id: str) -> int:
        with self._lock, self._conn.cursor() as cur:
            cur.execute("INSERT INTO threads (thread_id) VALUES (%s) ON CONFLICT DO NOTHING", (thread_id,))
            cur.execute("SELECT COALESCE(MAX(turn_index), -1) + 1 FROM turns WHERE thread_id = %s",
                        (thread_id,))
            row = cur.fetchone()
            return row[0] if row else 0

    def history(self, thread_id: str, limit: int | None = None) -> list[Turn]:
        with self._lock, self._conn.cursor() as cur:
            if limit is None:
                cur.execute("SELECT turn FROM turns WHERE thread_id = %s ORDER BY turn_index", (thread_id,))
                rows = cur.fetchall()
            else:
                cur.execute("SELECT turn FROM turns WHERE thread_id = %s ORDER BY turn_index DESC LIMIT %s",
                            (thread_id, limit))
                rows = list(reversed(cur.fetchall()))
        return [turn_from_dict(r[0]) for r in rows]

    def history_from(self, thread_id: str, first_index: int) -> list[Turn]:
        """Les tours d'indice >= `first_index` (fenêtre du contexte : jamais tout le journal, cf docs/NEVER-CLEAR.md)."""
        with self._lock, self._conn.cursor() as cur:
            cur.execute("SELECT turn FROM turns WHERE thread_id = %s AND turn_index >= %s ORDER BY turn_index",
                        (thread_id, first_index))
            rows = cur.fetchall()
        return [turn_from_dict(r[0]) for r in rows]

    def append(self, turn: Turn) -> None:
        with self._lock, self._conn.cursor() as cur:
            cur.execute("INSERT INTO threads (thread_id) VALUES (%s) ON CONFLICT DO NOTHING",
                        (turn.thread_id,))
            cur.execute("INSERT INTO turns (thread_id, turn_index, turn) VALUES (%s, %s, %s)",
                        (turn.thread_id, turn.index, Jsonb(turn_to_dict(turn))))
            for v in turn.verdicts:
                cur.execute("INSERT INTO verdicts (thread_id, turn_index, verdict) VALUES (%s, %s, %s)",
                            (turn.thread_id, turn.index, Jsonb(_enc_verdict(v))))
            for c in turn.calls:
                cur.execute("INSERT INTO llm_calls (thread_id, turn_index, call) VALUES (%s, %s, %s)",
                            (turn.thread_id, turn.index, Jsonb(_enc_call(c))))

    def last_turn_at(self, thread_id: str) -> datetime | None:
        """Date d'enregistrement (fin) du dernier tour du fil, ou None (`GET /v1/local/sessions`, §7.4.1)."""
        with self._lock, self._conn.cursor() as cur:
            cur.execute("SELECT MAX(created_at) FROM turns WHERE thread_id = %s", (thread_id,))
            row = cur.fetchone()
        return row[0] if row else None

    def turns_after(self, after: tuple[str, str, int], limit: int) -> list[tuple[str, str, int, dict[str, Any]]]:
        """(created_at ISO, fil, indice, tour) des tours du locataire courant (RLS) après le curseur `after`, dans
        l'ordre — pour le miroir web ⇄ Lokkal (nokker/cloud/mirror.py)."""
        with self._lock, self._conn.cursor() as cur:
            cur.execute("SELECT created_at, thread_id, turn_index, turn FROM turns "
                        "WHERE (created_at, thread_id, turn_index) > (%s::timestamptz, %s::text, %s::int) "
                        "ORDER BY created_at, thread_id, turn_index LIMIT %s", (*after, limit))
            return [(r[0].isoformat(), r[1], r[2], r[3]) for r in cur.fetchall()]

    def search_turns(self, thread_ids: list[str], needle: str, limit: int, prefix: str | None = None
                     ) -> list[tuple[str, int, datetime | None, str, str]]:
        """(fil, indice, date, texte de l'humain, réponse) des tours des fils DONNÉS (ceux que l'appelant voit), et des
        fils qui commencent par `prefix` (copies du miroir), dont le texte ou la réponse contient `needle` (sans
        casse), les plus récents d'abord ; locataire courant (RLS). `GET /v1/threads/search`, LOKKAL-PHONE.md §1.2."""
        from nokker.threads.search import FOLD_FROM, FOLD_TO, fold

        if not needle or not (thread_ids or prefix):
            return []
        like = _like_escape(fold(needle))  # sans accents (translate) ni casse (ILIKE)
        try:
            return self._search_turns(thread_ids, prefix, like, FOLD_FROM, FOLD_TO, limit)
        except psycopg.errors.QueryCanceled:
            raise TimeoutError("recherche plus longue que 2 s") from None

    def _search_turns(self, thread_ids: list[str], prefix: str | None, like: str, f_from: str, f_to: str,
                      limit: int) -> list[tuple[str, int, datetime | None, str, str]]:
        FOLD_FROM, FOLD_TO = f_from, f_to  # noqa: N806
        with self._lock:
            conn = self._conn  # `get()` SOUS le verrou : une connexion prise avant serait celle d'avant une reconnexion
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("SELECT set_config('statement_timeout', %s, true)", (str(SEARCH_TIMEOUT_MS),))
                cur.execute("SELECT thread_id, turn_index, created_at, turn->'input'->>'text', turn->>'reply' FROM turns "
                            "WHERE (thread_id = ANY(%s) OR (%s::text IS NOT NULL AND thread_id LIKE %s)) "
                            "AND (translate(turn->'input'->>'text', %s, %s) ILIKE %s "
                            "OR translate(turn->>'reply', %s, %s) ILIKE %s) "
                            "ORDER BY created_at DESC, turn_index DESC LIMIT %s",
                            (list(thread_ids), prefix, (_like_escape(prefix, wrap=False) + "%") if prefix else None,
                             FOLD_FROM, FOLD_TO, like, FOLD_FROM, FOLD_TO, like, int(limit)))
                return [(r[0], r[1], r[2], r[3] or "", r[4] or "") for r in cur.fetchall()]

    def erase_mirror_copies(self, prefix: str) -> int:
        """Efface les COPIES du miroir dont le fil commence par `prefix` (`lokkal-<machine>-` chez nokker.ai,
        `mirror-web-` sur une machine), chez le locataire courant seulement : fonction `nokker_mirror_erase`
        (migration 015), qui refuse tout autre préfixe. Rend le nombre de tours effacés."""
        with self._lock, self._conn.cursor() as cur:
            cur.execute("SELECT nokker_mirror_erase(%s)", (prefix,))
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def close(self) -> None:
        self._db.close()


# ──────────────────────── backend mémoire (tests) ────────────────────────


@dataclass
class MemoryStore:
    """`Store` en mémoire — tests unitaires uniquement, aucune persistance."""

    _by_thread: dict[str, list[Turn]] = field(default_factory=dict)
    _order: list[tuple[str, int]] = field(default_factory=list)
    _at: dict[str, datetime] = field(default_factory=dict)  # fil -> date d'ajout de son dernier tour

    def history(self, thread_id: str, limit: int | None = None) -> list[Turn]:
        turns = self._by_thread.get(thread_id, [])
        return list(turns if limit is None else turns[-limit:])

    def history_from(self, thread_id: str, first_index: int) -> list[Turn]:
        return [t for t in self._by_thread.get(thread_id, []) if t.index >= first_index]

    def next_index(self, thread_id: str) -> int:
        return len(self._by_thread.get(thread_id, []))

    def append(self, turn: Turn) -> None:
        self._by_thread.setdefault(turn.thread_id, []).append(turn)
        self._order.append((turn.thread_id, turn.index))
        self._at[turn.thread_id] = datetime.now(timezone.utc)

    def last_turn_at(self, thread_id: str) -> datetime | None:
        return self._at.get(thread_id)

    def search_turns(self, thread_ids: list[str], needle: str, limit: int, prefix: str | None = None
                     ) -> list[tuple[str, int, datetime | None, str, str]]:
        """Comme `PgStore.search_turns` ; l'ordre d'ajout tient lieu d'horodatage (date : celle du fil)."""
        from nokker.threads.search import fold

        wanted, n = set(thread_ids), fold(needle)
        if not n or not (wanted or prefix):
            return []
        by_key = {(t.thread_id, t.index): t for turns in self._by_thread.values() for t in turns}
        out = []
        for key in reversed(self._order):
            t = by_key.get(key)
            if t is None or not (t.thread_id in wanted or (prefix and t.thread_id.startswith(prefix))):
                continue
            if n in fold(t.input.text or "") or n in fold(t.reply or ""):
                out.append((t.thread_id, t.index, self._at.get(t.thread_id), t.input.text or "", t.reply or ""))
                if len(out) >= limit:
                    break
        return out

    def erase_mirror_copies(self, prefix: str) -> int:
        """Comme `PgStore.erase_mirror_copies` (même refus : copies du miroir seulement)."""
        bare = prefix.split(":", 1)[1] if ":" in prefix else prefix
        if not re.fullmatch(r"(lokkal-[A-Za-z0-9_]+-|mirror-web-)", bare):
            raise ValueError(f"préfixe refusé ({prefix}), seulement des copies du miroir")
        gone = [t for t in self._by_thread if t.startswith(prefix)]
        n = sum(len(self._by_thread.pop(t)) for t in gone)
        self._order = [k for k in self._order if k[0] not in gone]
        return n

    def turns_after(self, after: tuple[str, str, int], limit: int) -> list[tuple[str, str, int, dict[str, Any]]]:
        """Comme `PgStore.turns_after` ; l'ordre d'ajout tient lieu d'horodatage (rang à 12 chiffres)."""
        out = []
        by_key = {(t.thread_id, t.index): t for turns in self._by_thread.values() for t in turns}
        for rank, key in enumerate(self._order):
            row = (f"{rank:012d}", key[0], key[1])
            if row > after and key in by_key:
                out.append((*row, turn_to_dict(by_key[key])))
                if len(out) >= limit:
                    break
        return out


__all__ = [
    "MIGRATIONS_DIR",
    "MemoryStore",
    "PgStore",
    "apply_migrations",
    "migration_files",
    "turn_from_dict",
    "turn_to_dict",
]
