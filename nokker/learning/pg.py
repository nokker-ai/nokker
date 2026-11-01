"""`PgLearningStore` — apprentissage des comptes de nokker.ai (migration 046), sous RLS : chaque requête porte le
locataire courant (`app.tenant_id`, `nokker/store/conn.py::connect`), même interface que `MemoryLearningStore`."""
from __future__ import annotations

from psycopg.types.json import Jsonb

from nokker.learning.store import JournalEntry, LearningConflict, PendingNote, SkillVersion

_SK = "name, version, content, sha256, op, active, reverted_to, thread_id, turn_index, actor, approval_id, at"
_PN = ("id, source, kind, title, body, scope, derived_from, project, links, thread_id, turn_index, at, status, "
       "decided_at, decided_by, result")
_JR = "id, kind, subject, at, actor, version, thread_id, turn_index, detail"


def _sk(r: tuple) -> SkillVersion:
    name, version, content, sha, op, active, reverted_to, thread_id, turn_index, actor, approval_id, at = r
    return SkillVersion(name=name, version=int(version), content=content, sha256=sha, op=op, active=bool(active),
                        reverted_to=reverted_to, thread_id=thread_id, turn_index=turn_index, actor=actor,
                        approval_id=approval_id, at=at)


def _pn(r: tuple) -> PendingNote:
    (nid, source, kind, title, body, scope, derived, project, links, thread_id, turn_index, at, status, decided_at,
     decided_by, result) = r
    return PendingNote(id=nid, source=source, kind=kind, title=title, text=body, scope=scope, derived_from=derived,
                       project=project, links=list(links or []), thread_id=thread_id, turn_index=turn_index, at=at,
                       status=status, decided_at=decided_at, decided_by=decided_by, result=result)


class PgLearningStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _q(self, sql: str, params: tuple = ()) -> list[tuple]:
        from nokker.store.conn import connect

        with connect(self.dsn, autocommit=True) as conn:  # NoTenant hors d'un tenant_scope
            cur = conn.execute(sql, params)
            return cur.fetchall() if cur.description else []

    # ── skills ──
    def skill_versions(self, account: str, name: str) -> list[SkillVersion]:
        return [_sk(r) for r in self._q(f"SELECT {_SK} FROM nokker_learned_skills WHERE account_id = %s AND name = %s "
                                        "ORDER BY version", (account, name))]

    def skill_names(self, account: str) -> list[str]:
        return [r[0] for r in self._q("SELECT DISTINCT name FROM nokker_learned_skills WHERE account_id = %s "
                                      "ORDER BY name", (account,))]

    def add_skill_version(self, account: str, v: SkillVersion) -> SkillVersion:
        import psycopg

        try:
            self._q(f"INSERT INTO nokker_learned_skills (account_id, {_SK}) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (account, v.name, v.version, v.content, v.sha256, v.op, v.active, v.reverted_to, v.thread_id,
                     v.turn_index, v.actor, v.approval_id, v.at))
        except psycopg.errors.UniqueViolation as e:
            raise LearningConflict(f"{v.name} : version {v.version} déjà posée") from e
        return v

    # ── notes en attente ──
    def put_pending(self, account: str, n: PendingNote) -> None:
        self._q(f"INSERT INTO nokker_memory_pending (account_id, {_PN}) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (account, n.id, n.source, n.kind, n.title, n.text, n.scope, n.derived_from, n.project,
                 Jsonb(list(n.links)), n.thread_id, n.turn_index, n.at, n.status, n.decided_at, n.decided_by,
                 n.result))

    def get_pending(self, account: str, note_id: str) -> PendingNote | None:
        rows = self._q(f"SELECT {_PN} FROM nokker_memory_pending WHERE account_id = %s AND id = %s", (account, note_id))
        return _pn(rows[0]) if rows else None

    def list_pending(self, account: str, status: str | None = "pending") -> list[PendingNote]:
        if status is None:
            rows = self._q(f"SELECT {_PN} FROM nokker_memory_pending WHERE account_id = %s ORDER BY at", (account,))
        else:
            rows = self._q(f"SELECT {_PN} FROM nokker_memory_pending WHERE account_id = %s AND status = %s ORDER BY at",
                           (account, status))
        return [_pn(r) for r in rows]

    def decide_pending(self, account: str, note_id: str, status: str, by: str, at: str,
                       result: str | None = None) -> PendingNote | None:
        rows = self._q(f"UPDATE nokker_memory_pending SET status = %s, decided_by = %s, decided_at = %s, result = %s "
                       f"WHERE account_id = %s AND id = %s AND status = 'pending' RETURNING {_PN}",
                       (status, by, at, result, account, note_id))
        return _pn(rows[0]) if rows else None

    # ── journal ──
    def journal_append(self, account: str, e: JournalEntry) -> None:
        self._q(f"INSERT INTO nokker_learning_journal (account_id, {_JR}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s)", (account, e.id, e.kind, e.subject, e.at, e.actor, e.version, e.thread_id, e.turn_index,
                        Jsonb(dict(e.detail))))

    def journal(self, account: str, limit: int = 200) -> list[JournalEntry]:
        rows = self._q(f"SELECT {_JR} FROM nokker_learning_journal WHERE account_id = %s ORDER BY seq DESC LIMIT %s",
                       (account, int(limit)))
        out: list[JournalEntry] = []
        for nid, kind, subject, at, actor, version, thread_id, turn_index, detail in rows:
            out.append(JournalEntry(id=nid, kind=kind, subject=subject, at=at, actor=actor, version=version,
                                    thread_id=thread_id, turn_index=turn_index, detail=dict(detail or {})))
        return out


__all__ = ["PgLearningStore"]
