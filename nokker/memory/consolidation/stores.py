"""Magasins de la consolidation nocturne (migration 053) : les notes du compte (`nokker_long_memory`, 049), le journal
(nuits et opérations avec leur inverse), l'archive, les annotations, les épingles et les réglages.

Toute lecture et toute écriture se font pour UN acteur (compte, locataire courant) et sur les seules notes qu'il
DÉTIENT : sa mémoire de profil et, s'il est payeur, la mémoire de son foyer. En base, c'est la RLS qui le garantit
(`tenant_isolation` = locataire ET compte, sur 049 comme sur 053) ; chaque requête filtre AUSSI par compte (défense en
profondeur). Une opération s'applique (ou se défait) dans UNE transaction : tout ou rien.

Une note est manipulée sous forme d'instantané (`dict`, clés `SNAP`) : la ligne entière, plongement compris, pour que
défaire rende l'état EXACT d'avant."""
from __future__ import annotations

import copy
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from nokker.tenancy.context import current_tenant

SNAP = ("name", "scope", "household_id", "audience", "kind", "title", "body", "author_account", "origin",
        "embed_model", "embedding", "created_at", "updated_at")
MIGRATION = Path(__file__).resolve().parents[2] / "store" / "migrations" / "053_memory_consolidation.sql"


class StoreError(Exception):
    pass


def _check_actor(actor: Any) -> None:
    if actor.tenant_id != current_tenant():
        raise StoreError("acteur hors du locataire courant")


# ─────────────────────────────── en mémoire (tests, instance sans base) ───────────────────────────────


class MemoryConsolidationStore:
    """Sur un `MemoryLongStore` (nokker/memory/tenant_long.py) : mêmes règles que les politiques de 049/053, écrites en
    Python — un acteur ne voit que les notes dont il est détenteur, et ses propres lignes de journal."""

    def __init__(self, long_store: Any) -> None:
        self.long = long_store
        self.tables: dict[str, dict[tuple[str, str], dict[Any, Any]]] = {
            t: {} for t in ("runs", "ops", "archive", "annotations", "pins", "settings")}
        self._tx = threading.RLock()
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    @contextmanager
    def lock(self, actor: Any) -> Iterator[bool]:
        with self._locks_guard:
            lk = self._locks.setdefault(actor.tenant_id, threading.Lock())
        got = lk.acquire(blocking=False)
        try:
            yield got
        finally:
            if got:
                lk.release()

    @contextmanager
    def transaction(self, actor: Any) -> Iterator[_MemTx]:
        _check_actor(actor)
        with self._tx:
            saved_notes = copy.deepcopy(self.long.notes)
            saved_tables = copy.deepcopy(self.tables)
            try:
                yield _MemTx(self, actor)
            except BaseException:
                self.long.notes.clear()
                self.long.notes.update(saved_notes)
                self.tables = saved_tables
                raise


def _snap_of(n: Any) -> dict[str, Any]:
    return {"name": n.name, "scope": n.scope, "household_id": n.household_id, "audience": n.audience, "kind": n.kind,
            "title": n.title, "body": n.text, "author_account": n.author_account, "origin": n.origin,
            "embed_model": n.embed_model, "embedding": list(n.embedding) if n.embedding is not None else None,
            "created_at": float(n.created_at), "updated_at": float(n.updated_at)}


class _MemTx:
    def __init__(self, store: MemoryConsolidationStore, actor: Any) -> None:
        self.s, self.a = store, actor
        self.key = (actor.tenant_id, actor.account_id)

    def _t(self, name: str) -> dict[Any, Any]:
        return self.s.tables[name].setdefault(self.key, {})

    # ── notes détenues ──
    def notes(self) -> list[dict[str, Any]]:
        return [_snap_of(n) for (t, a, _), n in list(self.s.long.notes.items()) if (t, a) == self.key]

    def note(self, name: str) -> dict[str, Any] | None:
        n = self.s.long.notes.get((*self.key, name))
        return _snap_of(n) if n is not None else None

    def insert(self, snap: dict[str, Any]) -> None:
        from nokker.memory.tenant_long import LongNote

        k = (*self.key, snap["name"])
        if k in self.s.long.notes:
            raise StoreError(f"note déjà présente : {snap['name']}")
        emb = snap.get("embedding")
        self.s.long.notes[k] = LongNote(
            name=snap["name"], scope=snap["scope"], kind=snap["kind"], title=snap["title"], text=snap["body"],
            account_id=self.a.account_id, tenant_id=self.a.tenant_id, author_account=snap["author_account"],
            household_id=snap.get("household_id"), audience=snap.get("audience") or "all",
            origin=snap.get("origin") or "", created_at=float(snap["created_at"]),
            updated_at=float(snap["updated_at"]), embed_model=snap.get("embed_model"),
            embedding=list(emb) if isinstance(emb, list) else None)

    def update(self, snap: dict[str, Any]) -> None:
        n = self.s.long.notes.get((*self.key, snap["name"]))
        if n is None:
            raise StoreError(f"note absente : {snap['name']}")
        if (n.scope, n.household_id, n.author_account) != (snap["scope"], snap.get("household_id"),
                                                           snap["author_account"]):
            raise StoreError("une note ne change ni de portée, ni de foyer, ni d'auteur")
        emb = snap.get("embedding")
        n.title, n.text, n.kind, n.audience = snap["title"], snap["body"], snap["kind"], snap["audience"]
        n.origin, n.embed_model = snap.get("origin") or "", snap.get("embed_model")
        n.embedding = list(emb) if isinstance(emb, list) else None
        n.created_at, n.updated_at = float(snap["created_at"]), float(snap["updated_at"])

    def delete(self, name: str) -> None:
        self.s.long.notes.pop((*self.key, name), None)

    # ── archive ──
    def archive_put(self, snap: dict[str, Any], op_id: str, reason: str, at: float) -> None:
        self._t("archive")[(op_id, snap["name"])] = {"note": copy.deepcopy(snap), "reason": reason,
                                                     "archived_at": at, "restored_at": None}

    def archived(self, name: str, op_id: str) -> dict[str, Any] | None:
        r = self._t("archive").get((op_id, name))
        return copy.deepcopy(r) if r is not None else None

    def archive_restored(self, name: str, op_id: str, at: float) -> None:
        self._t("archive")[(op_id, name)]["restored_at"] = at

    def archive_list(self) -> list[dict[str, Any]]:
        return [{**copy.deepcopy(r), "op_id": k[0], "name": k[1]} for k, r in self._t("archive").items()]

    # ── annotations ──
    def annotate(self, ann: dict[str, Any]) -> None:
        self._t("annotations")[ann["id"]] = {**ann, "undone_at": None}

    def annotation_undo(self, ann_id: str, at: float) -> None:
        r = self._t("annotations").get(ann_id)
        if r is not None and r["undone_at"] is None:
            r["undone_at"] = at

    def annotations(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._t("annotations").values() if r["undone_at"] is None]

    # ── épingles ──
    def pins(self) -> set[str]:
        return set(self._t("pins"))

    def pin(self, name: str, at: float) -> None:
        self._t("pins")[name] = at

    def unpin(self, name: str) -> bool:
        return self._t("pins").pop(name, None) is not None

    # ── nuits ──
    def run_insert(self, run: dict[str, Any]) -> bool:
        runs = self._t("runs")
        if run["trigger"] == "schedule" and not run["dry_run"] and any(
                r["night"] == run["night"] and r["trigger"] == "schedule" and not r["dry_run"] for r in runs.values()):
            return False
        runs[run["id"]] = copy.deepcopy(run)
        return True

    def run_update(self, run_id: str, **fields: Any) -> None:
        self._t("runs")[run_id].update(copy.deepcopy(fields))

    def run_get(self, run_id: str) -> dict[str, Any] | None:
        r = self._t("runs").get(run_id)
        return copy.deepcopy(r) if r is not None else None

    def runs(self, limit: int) -> list[dict[str, Any]]:
        return sorted((copy.deepcopy(r) for r in self._t("runs").values()), key=lambda r: -r["started_at"])[:limit]

    def run_for_night(self, night: str) -> dict[str, Any] | None:
        for r in self._t("runs").values():
            if r["night"] == night and r["trigger"] == "schedule" and not r["dry_run"]:
                return copy.deepcopy(r)
        return None

    # ── opérations ──
    def op_insert(self, op: dict[str, Any]) -> None:
        self._t("ops")[op["id"]] = copy.deepcopy(op)

    def ops(self, run_id: str) -> list[dict[str, Any]]:
        return sorted((copy.deepcopy(o) for o in self._t("ops").values() if o["run_id"] == run_id),
                      key=lambda o: o["seq"])

    def op_get(self, op_id: str) -> dict[str, Any] | None:
        o = self._t("ops").get(op_id)
        return copy.deepcopy(o) if o is not None else None

    def op_undone(self, op_id: str, at: float) -> None:
        self._t("ops")[op_id]["undone_at"] = at

    def live_ops(self, kind: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(o) for o in self._t("ops").values() if o["kind"] == kind and o["undone_at"] is None]

    # ── réglages ──
    def settings_get(self) -> dict[str, Any] | None:
        r = self._t("settings").get("row")
        return dict(r) if r is not None else None

    def settings_put(self, row: dict[str, Any]) -> None:
        self._t("settings")["row"] = dict(row)


# ─────────────────────────────── Postgres (nokker.ai) ───────────────────────────────

_NOTE_COLS = ("name, scope, household_id, audience, kind, title, body, author_account, origin, embed_model, "
              "created_at, updated_at")
_RUN_COLS = ("id, night, tz, trigger, status, dry_run, model, started_at, finished_at, counts, skipped, error, "
             "undone_at, notified")
_OP_COLS = "id, run_id, seq, kind, scope, household_id, reason, evidence, changes, authors, at, undone_at"


def _vec_literal(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


class PgConsolidationStore:
    """nokker.ai : rôle `nokker_app` (sans BYPASSRLS). Une connexion par transaction : `app.tenant_id` = locataire
    courant (`store/conn.py::connect`), puis `app.account_id` = l'acteur."""

    def __init__(self, dsn: str, *, migrate_now: bool = True) -> None:
        if not dsn:
            raise ValueError("PgConsolidationStore requiert un DSN Postgres")
        self.dsn = dsn
        self._vector: tuple[float, bool] | None = None
        if migrate_now:
            from nokker.store.conn import migrate

            migrate(dsn, MIGRATION.read_text(encoding="utf-8"))

    @property
    def vector(self) -> bool:
        now = time.monotonic()
        if self._vector is not None and now - self._vector[0] < 300:
            return self._vector[1]
        from nokker.store.conn import connect_admin

        with connect_admin(self.dsn) as c:
            ok = bool(c.execute("SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = "
                                "to_regclass('nokker_long_memory') AND attname = 'embedding' AND NOT attisdropped)"
                                ).fetchone()[0])
        self._vector = (now, ok)
        return ok

    @contextmanager
    def lock(self, actor: Any) -> Iterator[bool]:
        """Verrou consultatif PAR LOCATAIRE (une seule consolidation à la fois, tous pods confondus), tenu le temps du
        passage sur une connexion à part. Ne lit aucune table : rien à voir avec la RLS."""
        from nokker.store.conn import connect_admin

        key = f"nokker-consolidation:{actor.tenant_id}"
        conn = connect_admin(self.dsn, autocommit=True)
        try:
            got = bool(conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (key,)).fetchone()[0])
            try:
                yield got
            finally:
                if got:
                    conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))
        finally:
            conn.close()

    @contextmanager
    def transaction(self, actor: Any) -> Iterator[_PgTx]:
        from nokker.store.conn import connect

        _check_actor(actor)
        vector = self.vector
        conn = connect(self.dsn)
        try:
            conn.execute("SELECT set_config('app.account_id', %s, false)", (actor.account_id,))
            yield _PgTx(conn, actor, vector)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def _jsonb(v: Any) -> Any:
    from psycopg.types.json import Jsonb

    return Jsonb(v)


class _PgTx:
    def __init__(self, conn: Any, actor: Any, vector: bool) -> None:
        self.c, self.a, self.vector = conn, actor, vector
        self.k = (actor.tenant_id, actor.account_id)

    def _snap(self, r: tuple) -> dict[str, Any]:
        d = dict(zip(SNAP[:10] + SNAP[11:], r[:12], strict=True))
        d["embedding"] = r[12] if self.vector else None
        d["created_at"], d["updated_at"] = float(d["created_at"]), float(d["updated_at"])
        return d

    def _sel(self) -> str:
        return f"SELECT {_NOTE_COLS}, " + ("embedding::text" if self.vector else "NULL") + " FROM nokker_long_memory"

    def notes(self) -> list[dict[str, Any]]:
        rows = self.c.execute(self._sel() + " WHERE tenant_id = %s AND account_id = %s ORDER BY created_at",
                              self.k).fetchall()
        return [self._snap(r) for r in rows]

    def note(self, name: str) -> dict[str, Any] | None:
        r = self.c.execute(self._sel() + " WHERE tenant_id = %s AND account_id = %s AND name = %s",
                           (*self.k, name)).fetchone()
        return self._snap(r) if r else None

    def _vals(self, snap: dict[str, Any]) -> list[Any]:
        return [snap["scope"], snap.get("household_id"), snap.get("audience") or "all", snap["kind"], snap["title"],
                snap["body"], snap["author_account"], snap.get("origin") or "",
                snap.get("embed_model") if self.vector and snap.get("embedding") is not None else None,
                float(snap["created_at"]), float(snap["updated_at"])]

    def insert(self, snap: dict[str, Any]) -> None:
        cols = "tenant_id, account_id, name, scope, household_id, audience, kind, title, body, author_account, " \
               "origin, embed_model, created_at, updated_at"
        vals = [*self.k, snap["name"], *self._vals(snap)]
        marks = ", ".join(["%s"] * len(vals))
        if self.vector:
            cols += ", embedding"
            marks += ", %s::vector"
            vals.append(_vec_literal(snap.get("embedding")))
        self.c.execute(f"INSERT INTO nokker_long_memory ({cols}) VALUES ({marks})", vals)

    def update(self, snap: dict[str, Any]) -> None:
        v = self._vals(snap)
        sets = ("audience = %s, kind = %s, title = %s, body = %s, origin = %s, embed_model = %s, created_at = %s, "
                "updated_at = %s")
        args = [v[2], v[3], v[4], v[5], v[7], v[8], v[9], v[10]]
        if self.vector:
            sets += ", embedding = %s::vector"
            args.append(_vec_literal(snap.get("embedding")))
        n = self.c.execute(f"UPDATE nokker_long_memory SET {sets} WHERE tenant_id = %s AND account_id = %s "
                           "AND name = %s AND scope = %s AND household_id IS NOT DISTINCT FROM %s "
                           "AND author_account = %s",
                           [*args, *self.k, snap["name"], snap["scope"], snap.get("household_id"),
                            snap["author_account"]]).rowcount
        if n != 1:
            raise StoreError(f"note absente ou déplacée : {snap['name']}")

    def delete(self, name: str) -> None:
        self.c.execute("DELETE FROM nokker_long_memory WHERE tenant_id = %s AND account_id = %s AND name = %s",
                       (*self.k, name))

    def archive_put(self, snap: dict[str, Any], op_id: str, reason: str, at: float) -> None:
        self.c.execute("INSERT INTO nokker_memory_archive (tenant_id, account_id, name, op_id, scope, household_id, "
                       "author_account, note, reason, archived_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                       (*self.k, snap["name"], op_id, snap["scope"], snap.get("household_id"), snap["author_account"],
                        _jsonb(snap), reason, at))

    def archived(self, name: str, op_id: str) -> dict[str, Any] | None:
        r = self.c.execute("SELECT note, reason, archived_at, restored_at FROM nokker_memory_archive WHERE "
                           "tenant_id = %s AND account_id = %s AND op_id = %s AND name = %s",
                           (*self.k, op_id, name)).fetchone()
        return {"note": r[0], "reason": r[1], "archived_at": r[2], "restored_at": r[3]} if r else None

    def archive_restored(self, name: str, op_id: str, at: float) -> None:
        self.c.execute("UPDATE nokker_memory_archive SET restored_at = %s WHERE tenant_id = %s AND account_id = %s "
                       "AND op_id = %s AND name = %s", (at, *self.k, op_id, name))

    def archive_list(self) -> list[dict[str, Any]]:
        rows = self.c.execute("SELECT op_id, name, note, reason, archived_at, restored_at FROM nokker_memory_archive "
                              "WHERE tenant_id = %s AND account_id = %s ORDER BY archived_at", self.k).fetchall()
        return [{"op_id": r[0], "name": r[1], "note": r[2], "reason": r[3], "archived_at": r[4],
                 "restored_at": r[5]} for r in rows]

    def annotate(self, ann: dict[str, Any]) -> None:
        self.c.execute("INSERT INTO nokker_memory_annotations (tenant_id, account_id, id, name, run_id, op_id, text, "
                       "at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                       (*self.k, ann["id"], ann["name"], ann["run_id"], ann["op_id"], ann["text"], ann["at"]))

    def annotation_undo(self, ann_id: str, at: float) -> None:
        self.c.execute("UPDATE nokker_memory_annotations SET undone_at = %s WHERE tenant_id = %s AND account_id = %s "
                       "AND id = %s AND undone_at IS NULL", (at, *self.k, ann_id))

    def annotations(self) -> list[dict[str, Any]]:
        rows = self.c.execute("SELECT id, name, run_id, op_id, text, at FROM nokker_memory_annotations WHERE "
                              "tenant_id = %s AND account_id = %s AND undone_at IS NULL ORDER BY at", self.k).fetchall()
        return [dict(zip(("id", "name", "run_id", "op_id", "text", "at"), r, strict=True)) for r in rows]

    def pins(self) -> set[str]:
        return {r[0] for r in self.c.execute("SELECT name FROM nokker_memory_pins WHERE tenant_id = %s AND "
                                             "account_id = %s", self.k).fetchall()}

    def pin(self, name: str, at: float) -> None:
        self.c.execute("INSERT INTO nokker_memory_pins (tenant_id, account_id, name, pinned_at) VALUES (%s, %s, %s, %s) "
                       "ON CONFLICT DO NOTHING", (*self.k, name, at))

    def unpin(self, name: str) -> bool:
        return self.c.execute("DELETE FROM nokker_memory_pins WHERE tenant_id = %s AND account_id = %s AND name = %s",
                              (*self.k, name)).rowcount > 0

    def _run(self, r: tuple) -> dict[str, Any]:
        return dict(zip(("id", "night", "tz", "trigger", "status", "dry_run", "model", "started_at", "finished_at",
                         "counts", "skipped", "error", "undone_at", "notified"), r, strict=True))

    def run_insert(self, run: dict[str, Any]) -> bool:
        import psycopg

        try:
            with self.c.transaction():  # point de sauvegarde : la violation d'unicité n'annule pas la transaction
                self.c.execute(
                    f"INSERT INTO nokker_consolidation_runs (tenant_id, account_id, {_RUN_COLS}) VALUES "
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (*self.k, run["id"], run["night"], run["tz"], run["trigger"], run["status"], run["dry_run"],
                     run.get("model"), run["started_at"], run.get("finished_at"), _jsonb(run.get("counts") or {}),
                     run.get("skipped", 0), run.get("error"), run.get("undone_at"),
                     _jsonb(run.get("notified") or {})))
        except psycopg.errors.UniqueViolation:
            return False
        return True

    def run_update(self, run_id: str, **fields: Any) -> None:
        sets, args = [], []
        for k, v in fields.items():
            if k not in _RUN_COLS.split(", ") or k == "id":
                raise StoreError(f"champ de nuit inconnu : {k}")
            sets.append(f"{k} = %s")
            args.append(_jsonb(v) if k in ("counts", "notified") else v)
        self.c.execute(f"UPDATE nokker_consolidation_runs SET {', '.join(sets)} WHERE tenant_id = %s AND "
                       "account_id = %s AND id = %s", (*args, *self.k, run_id))

    def run_get(self, run_id: str) -> dict[str, Any] | None:
        r = self.c.execute(f"SELECT {_RUN_COLS} FROM nokker_consolidation_runs WHERE tenant_id = %s AND "
                           "account_id = %s AND id = %s", (*self.k, run_id)).fetchone()
        return self._run(r) if r else None

    def runs(self, limit: int) -> list[dict[str, Any]]:
        rows = self.c.execute(f"SELECT {_RUN_COLS} FROM nokker_consolidation_runs WHERE tenant_id = %s AND "
                              "account_id = %s ORDER BY started_at DESC LIMIT %s", (*self.k, int(limit))).fetchall()
        return [self._run(r) for r in rows]

    def run_for_night(self, night: str) -> dict[str, Any] | None:
        r = self.c.execute(f"SELECT {_RUN_COLS} FROM nokker_consolidation_runs WHERE tenant_id = %s AND "
                           "account_id = %s AND night = %s AND trigger = 'schedule' AND NOT dry_run",
                           (*self.k, night)).fetchone()
        return self._run(r) if r else None

    def _op(self, r: tuple) -> dict[str, Any]:
        d = dict(zip(_OP_COLS.split(", "), r, strict=True))
        d["authors"] = list(d["authors"] or [])
        return d

    def op_insert(self, op: dict[str, Any]) -> None:
        self.c.execute(f"INSERT INTO nokker_consolidation_ops (tenant_id, account_id, {_OP_COLS}) VALUES "
                       "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                       (*self.k, op["id"], op["run_id"], op["seq"], op["kind"], op["scope"], op.get("household_id"),
                        op.get("reason") or "", _jsonb(op.get("evidence") or {}), _jsonb(op["changes"]),
                        list(op.get("authors") or []), op["at"], op.get("undone_at")))

    def ops(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.c.execute(f"SELECT {_OP_COLS} FROM nokker_consolidation_ops WHERE tenant_id = %s AND "
                              "account_id = %s AND run_id = %s ORDER BY seq", (*self.k, run_id)).fetchall()
        return [self._op(r) for r in rows]

    def op_get(self, op_id: str) -> dict[str, Any] | None:
        r = self.c.execute(f"SELECT {_OP_COLS} FROM nokker_consolidation_ops WHERE tenant_id = %s AND "
                           "account_id = %s AND id = %s", (*self.k, op_id)).fetchone()
        return self._op(r) if r else None

    def op_undone(self, op_id: str, at: float) -> None:
        self.c.execute("UPDATE nokker_consolidation_ops SET undone_at = %s WHERE tenant_id = %s AND account_id = %s "
                       "AND id = %s", (at, *self.k, op_id))

    def live_ops(self, kind: str) -> list[dict[str, Any]]:
        rows = self.c.execute(f"SELECT {_OP_COLS} FROM nokker_consolidation_ops WHERE tenant_id = %s AND "
                              "account_id = %s AND kind = %s AND undone_at IS NULL", (*self.k, kind)).fetchall()
        return [self._op(r) for r in rows]

    def settings_get(self) -> dict[str, Any] | None:
        r = self.c.execute("SELECT enabled, tz, cron, updated_at FROM nokker_consolidation_settings WHERE "
                           "tenant_id = %s AND account_id = %s", self.k).fetchone()
        return {"enabled": r[0], "tz": r[1], "cron": r[2], "updated_at": r[3]} if r else None

    def settings_put(self, row: dict[str, Any]) -> None:
        self.c.execute("INSERT INTO nokker_consolidation_settings (tenant_id, account_id, enabled, tz, cron, "
                       "updated_at) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (tenant_id, account_id) DO UPDATE SET "
                       "enabled = EXCLUDED.enabled, tz = EXCLUDED.tz, cron = EXCLUDED.cron, "
                       "updated_at = EXCLUDED.updated_at",
                       (*self.k, bool(row["enabled"]), row.get("tz"), row.get("cron"), row["updated_at"]))


__all__ = ["SNAP", "MemoryConsolidationStore", "PgConsolidationStore", "StoreError"]

