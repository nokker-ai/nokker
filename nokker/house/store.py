"""Journal de la salle Maison (docs/HOUSE-ROOM.md §4) : append-only, jamais vidé (never clear), un par foyer.

Une ligne = un message : d'un membre (`member`), d'un agent (`agent`, au nom du membre qui l'a fait parler), ou un
événement entrant (`event` : incident caméra, courrier arrivé, rapport mémoire, carte en attente). Le locataire de la
ligne est celui du FOYER (le payeur). Postgres (migration 057) :

- `tenant_isolation` : le payeur (locataire du foyer) ; les événements y sont écrits ;
- `house_room_read` (SELECT) : un membre ACTIF du foyer, et `audience = adults` jamais pour un profil mineur
  (`nokker_long_memory_visible`, migration 049, lue en direct : un départ ferme la salle au message suivant) ;
- `house_room_write` (INSERT) : un membre actif, à SON nom (`author_account = app.account_id`), kind member|agent ;
- trigger : aucune ligne ne change (append-only) ; foyer supprimé → ses messages partent ; `nokker erase` d'un membre
  : ses messages (et ceux des agents qu'il a fait parler) partent avec lui.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

KINDS = ("member", "agent", "event")
ORIGINS = ("room", "ask_house", "event")


@dataclass
class RoomMessage:
    id: int
    household_id: str
    tenant_id: str  # locataire du foyer
    kind: str
    text: str
    at: float
    author_account: str | None = None  # membre (member), membre qui a fait parler l'agent (agent), None (event)
    author_label: str = ""  # « le payeur (adulte) », « membre B (ado, 13-17 ans) »
    agent: str | None = None  # slug de l'agent (agent, event)
    audience: str = "all"
    origin: dict[str, Any] = field(default_factory=lambda: {"kind": "room"})
    reply_to: int | None = None
    event: str | None = None  # type d'événement (`camera.incident`, `mail.arrived`, `memory.report`, `card.pending`)
    approvals: list[str] = field(default_factory=list)  # cartes posées par ce message d'agent (ids)

    def to_json(self, me: str | None) -> dict[str, Any]:
        """Vue client (contrat docs/HOUSE-ROOM.md §6). Jamais le locataire ni le compte d'un autre membre."""
        out: dict[str, Any] = {"id": self.id, "kind": self.kind, "text": self.text, "at": self.at,
                               "agent": self.agent, "author": self.author_label or None,
                               "mine": bool(me) and self.author_account == me, "audience": self.audience,
                               "origin": {k: v for k, v in self.origin.items() if k in ("kind", "channel")},
                               "reply_to": self.reply_to, "event": self.event}
        if out["mine"] and self.approvals:
            out["approvals"] = list(self.approvals)  # ses cartes : à lui seul de les décider
        return out


class MemoryRoomStore:
    """Journal en mémoire (tests, dev). `visible(...)` applique les mêmes règles que la RLS de `PgRoomStore`."""

    def __init__(self, clock: Any = time.time) -> None:
        self.rows: list[RoomMessage] = []
        self.lock = threading.Lock()
        self.clock = clock

    def append(self, m: RoomMessage, *, actor: str | None = None) -> RoomMessage:
        with self.lock:
            m.id = len(self.rows) + 1
            m.at = m.at or self.clock()
            self.rows.append(m)
            return m

    def list(self, household_id: str, tenant_id: str, *, adults: bool, after: int = 0, limit: int = 100,
             before: int | None = None, actor: str | None = None) -> list[RoomMessage]:
        with self.lock:
            rows = [r for r in self.rows if r.household_id == household_id and r.tenant_id == tenant_id
                    and r.id > after and (before is None or r.id < before) and (adults or r.audience == "all")]
        return rows[-limit:] if before is not None or not after else rows[:limit]

    def delete_household(self, household_id: str, tenant_id: str) -> int:
        with self.lock:
            n = len(self.rows)
            self.rows = [r for r in self.rows if not (r.household_id == household_id and r.tenant_id == tenant_id)]
            return n - len(self.rows)


_COLS = ("id, household_id, tenant_id, kind, text, extract(epoch from at) AS at, author_account, author_label, agent, "
         "audience, origin, reply_to, event, approvals")


def _row(r: dict[str, Any]) -> RoomMessage:
    origin = r["origin"] if isinstance(r["origin"], dict) else json.loads(r["origin"] or "{}")
    return RoomMessage(id=int(r["id"]), household_id=r["household_id"], tenant_id=r["tenant_id"], kind=r["kind"],
                       text=r["text"], at=float(r["at"]), author_account=r["author_account"],
                       author_label=r["author_label"] or "", agent=r["agent"], audience=r["audience"], origin=origin,
                       reply_to=r["reply_to"], event=r["event"], approvals=list(r["approvals"] or []))


class PgRoomStore:
    """Journal sous RLS. Chaque opération : connexion du noyau (`app.tenant_id` = locataire COURANT, celui du membre ou
    du payeur pour un événement) + `app.account_id` = le compte qui agit. Le locataire de la ligne est toujours celui
    du foyer, jamais deviné : il est passé par l'appelant, qui le tient de l'adhésion lue en base."""

    def __init__(self, dsn: str, *, migrate_now: bool = True) -> None:
        if not dsn:
            raise ValueError("PgRoomStore requiert un DSN Postgres")
        self.dsn = dsn
        if migrate_now:
            from pathlib import Path

            from nokker.store.conn import migrate

            mig = Path(__file__).parent.parent / "store" / "migrations"
            migrate(dsn, (mig / "057_house_room.sql").read_text(encoding="utf-8"))

    def _conn(self, account_id: str | None):
        from psycopg.rows import dict_row

        from nokker.store.conn import connect

        conn = connect(self.dsn, row_factory=dict_row)
        try:
            conn.execute("SELECT set_config('app.account_id', %s, false)", (account_id or "",))
            conn.commit()
        except BaseException:
            conn.close()
            raise
        return conn

    def append(self, m: RoomMessage, *, actor: str | None = None) -> RoomMessage:
        with self._conn(actor or m.author_account) as c:
            r = c.execute(
                f"INSERT INTO house_room_messages (household_id, tenant_id, kind, text, author_account, author_label, "
                f"agent, audience, origin, reply_to, event, approvals) "
                f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s) RETURNING {_COLS}",
                (m.household_id, m.tenant_id, m.kind, m.text, m.author_account, m.author_label, m.agent, m.audience,
                 json.dumps(m.origin), m.reply_to, m.event, list(m.approvals))).fetchone()
        return _row(r)

    def list(self, household_id: str, tenant_id: str, *, adults: bool, after: int = 0, limit: int = 100,
             before: int | None = None, actor: str | None = None) -> list[RoomMessage]:
        """Messages du foyer visibles par `actor` (RLS) ; `adults` faux filtre en plus côté requête."""
        aud = "" if adults else " AND audience = 'all'"
        with self._conn(actor) as c:
            if before is not None or not after:
                rows = c.execute(
                    f"SELECT * FROM (SELECT {_COLS} FROM house_room_messages WHERE household_id = %s AND tenant_id = %s "
                    f"AND id > %s AND id < %s{aud} ORDER BY id DESC LIMIT %s) t ORDER BY id",
                    (household_id, tenant_id, after, before if before is not None else 2**62, limit)).fetchall()
            else:
                rows = c.execute(
                    f"SELECT {_COLS} FROM house_room_messages WHERE household_id = %s AND tenant_id = %s AND id > %s{aud} "
                    f"ORDER BY id LIMIT %s", (household_id, tenant_id, after, limit)).fetchall()
        return [_row(r) for r in rows]


__all__ = ["KINDS", "ORIGINS", "MemoryRoomStore", "PgRoomStore", "RoomMessage"]
