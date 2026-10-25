"""Magasin Postgres du lien Lokkal (tables des migrations 014, 018, 019, 020, 021, 025, 028 et 033), même interface que
`MemoryLinkStore` (et `MemoryRelayStore` pour 021, `MemoryHandoffStore` et `MemoryMirrorNotes` pour 025).

`lokkal_machines` et les tables de la passerelle sont hors RLS (cherchées par empreinte) ; la charge des machines
(`lokkal_machine_load`, 020) et les approbations relayées (`lokkal_relay_approvals`, 021) sont sous RLS : lues et
écrites dans le `tenant_scope` du locataire de la machine ou du compte."""
from __future__ import annotations

import time
from typing import Any

import psycopg

from nokker.cloud.link import Machine, _Request


class PgLinkStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _q(self, sql: str, params: tuple = ()) -> list[tuple]:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            cur = conn.execute(sql, params)
            return cur.fetchall() if cur.description else []

    @staticmethod
    def _req(r: tuple) -> _Request:
        return _Request(device_sha=r[0], user_code=r[1], machine_name=r[2], expires_at=r[3], state=r[4],
                        machine_id=r[5])

    @staticmethod
    def _mac(r: tuple) -> Machine:
        return Machine(id=r[0], account=r[1], tenant=r[2], name=r[3], host=r[4], token_sha256=r[5], created_at=r[6],
                       revoked=r[7] is not None, last_seen_at=r[8] if len(r) > 8 else None)

    _RCOLS = "device_sha256, user_code, machine_name, expires_at, state, machine_id"
    _MCOLS = "id, account_id, tenant_id, name, host, token_sha256, created_at, revoked_at, last_seen_at"

    def add_request(self, r: _Request) -> None:
        self._q(f"INSERT INTO lokkal_link_requests ({self._RCOLS}) VALUES (%s, %s, %s, %s, %s, %s)",
                (r.device_sha, r.user_code, r.machine_name, r.expires_at, r.state, r.machine_id))

    def request(self, device_sha: str) -> _Request | None:
        rows = self._q(f"SELECT {self._RCOLS} FROM lokkal_link_requests WHERE device_sha256 = %s", (device_sha,))
        return self._req(rows[0]) if rows else None

    def pending_by_code(self, user_code: str) -> _Request | None:
        rows = self._q(f"SELECT {self._RCOLS} FROM lokkal_link_requests WHERE user_code = %s AND state = 'pending'",
                       (user_code,))
        return self._req(rows[0]) if rows else None

    def save_request(self, r: _Request) -> None:
        self._q("UPDATE lokkal_link_requests SET state = %s, machine_id = %s WHERE device_sha256 = %s",
                (r.state, r.machine_id, r.device_sha))

    def drop_request(self, device_sha: str) -> None:
        self._q("DELETE FROM lokkal_link_requests WHERE device_sha256 = %s OR expires_at < %s",
                (device_sha, time.time() - 3600))  # et les flux échus depuis plus d'une heure

    def hosts(self) -> set[str]:
        return {r[0] for r in self._q("SELECT host FROM lokkal_machines")}

    def add_machine(self, m: Machine) -> None:
        self._q(f"INSERT INTO lokkal_machines ({self._MCOLS}) VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, NULL)",
                (m.id, m.account, m.tenant, m.name, m.host, m.token_sha256, m.created_at))

    def machine(self, machine_id: str) -> Machine | None:
        rows = self._q(f"SELECT {self._MCOLS} FROM lokkal_machines WHERE id = %s", (machine_id,))
        return self._mac(rows[0]) if rows else None

    def set_token(self, machine_id: str, token_sha: str) -> None:
        self._q("UPDATE lokkal_machines SET token_sha256 = %s WHERE id = %s", (token_sha, machine_id))

    def by_token(self, token_sha: str) -> Machine | None:
        rows = self._q(f"SELECT {self._MCOLS} FROM lokkal_machines WHERE token_sha256 = %s AND revoked_at IS NULL",
                       (token_sha,))
        return self._mac(rows[0]) if rows else None

    def by_host(self, host: str) -> Machine | None:
        rows = self._q(f"SELECT {self._MCOLS} FROM lokkal_machines WHERE host = %s AND revoked_at IS NULL "
                       "ORDER BY created_at DESC LIMIT 1", (host,))
        return self._mac(rows[0]) if rows else None

    def revoke(self, machine_id: str) -> None:
        self._q("UPDATE lokkal_machines SET revoked_at = %s WHERE id = %s", (time.time(), machine_id))

    def seen(self, machine_id: str, at: float) -> None:
        self._q("UPDATE lokkal_machines SET last_seen_at = %s WHERE id = %s", (at, machine_id))

    def of_account(self, account: str) -> list[Machine]:
        return [self._mac(r) for r in self._q(
            f"SELECT {self._MCOLS} FROM lokkal_machines WHERE account_id = %s AND revoked_at IS NULL", (account,))]

    def tenant_has_machine(self, tenant: str) -> bool:
        return bool(self._q("SELECT 1 FROM lokkal_machines WHERE tenant_id = %s AND revoked_at IS NULL "
                            "AND token_sha256 <> '' LIMIT 1", (tenant,)))

    def _tq(self, tenant: str, sql: str, params: tuple = ()) -> list[tuple]:
        """Requête sous la RLS de `tenant` (`app.tenant_id` posé par `connect`)."""
        from nokker.store.conn import connect
        from nokker.tenancy.context import tenant_scope

        with tenant_scope(tenant), connect(self.dsn, autocommit=True) as conn:
            cur = conn.execute(sql, params)
            return cur.fetchall() if cur.description else []

    def set_load(self, machine_id: str, tenant: str, load: dict[str, Any], at: float) -> None:
        from psycopg.types.json import Jsonb

        self._tq(tenant, "INSERT INTO lokkal_machine_load (machine_id, tenant_id, load, at) VALUES (%s, %s, %s, %s) "
                 "ON CONFLICT (machine_id) DO UPDATE SET load = EXCLUDED.load, at = EXCLUDED.at",
                 (machine_id, tenant, Jsonb(load), at))

    def loads_of(self, tenant: str, machine_ids: list[str]) -> dict[str, tuple[dict[str, Any], float]]:
        if not machine_ids:
            return {}
        return {r[0]: (r[1], r[2]) for r in self._tq(
            tenant, "SELECT machine_id, load, at FROM lokkal_machine_load WHERE machine_id = ANY(%s)",
            (list(machine_ids),))}

    def set_resources(self, machine_id: str, tenant: str, resources: dict[str, Any], at: float) -> None:
        from psycopg.types.json import Jsonb

        self._tq(tenant, "INSERT INTO lokkal_machine_resources (machine_id, tenant_id, resources, at) "
                 "VALUES (%s, %s, %s, %s) ON CONFLICT (machine_id) DO UPDATE SET resources = EXCLUDED.resources, "
                 "at = EXCLUDED.at", (machine_id, tenant, Jsonb(resources), at))

    def resources_of(self, tenant: str, machine_ids: list[str]) -> dict[str, tuple[dict[str, Any], float]]:
        if not machine_ids:
            return {}
        return {r[0]: (r[1], r[2]) for r in self._tq(
            tenant, "SELECT machine_id, resources, at FROM lokkal_machine_resources WHERE machine_id = ANY(%s)",
            (list(machine_ids),))}


class PgGatewayAuthStore:
    """Codes et jetons de renouvellement de la passerelle (migration 019), même interface que
    `MemoryGatewayAuthStore`. Usage unique par `DELETE … RETURNING` / `UPDATE … RETURNING` (atomiques)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _q(self, sql: str, params: tuple = ()) -> list[tuple]:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            cur = conn.execute(sql, params)
            return cur.fetchall() if cur.description else []

    def put_code(self, sha: str, c: Any) -> None:
        self._q("DELETE FROM lokkal_gateway_codes WHERE expires_at < %s", (time.time(),))
        self._q("INSERT INTO lokkal_gateway_codes (code_sha256, account_id, tenant_id, machine_id, host, challenge, "
                "expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (sha, c.account, c.tenant, c.machine_id, c.host, c.challenge, c.expires_at))

    def take_code(self, sha: str) -> Any:
        from nokker.cloud.gateway_auth import _Code

        rows = self._q("DELETE FROM lokkal_gateway_codes WHERE code_sha256 = %s RETURNING account_id, tenant_id, "
                       "machine_id, host, challenge, expires_at", (sha,))
        return _Code(*rows[0]) if rows else None

    def put_refresh(self, sha: str, r: Any) -> None:
        self._q("INSERT INTO lokkal_gateway_refresh (token_sha256, family, account_id, tenant_id, machine_id, host, "
                "expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (sha, r.family, r.account, r.tenant, r.machine_id, r.host, r.expires_at))

    def use_refresh(self, sha: str) -> Any:
        from nokker.cloud.gateway_auth import _Refresh

        # L'état AVANT le marquage : une seule requête gagne used_at NULL → non NULL.
        rows = self._q("UPDATE lokkal_gateway_refresh r SET used_at = COALESCE(r.used_at, %s) FROM "
                       "(SELECT token_sha256, used_at AS was_used FROM lokkal_gateway_refresh WHERE token_sha256 = %s "
                       "FOR UPDATE) old WHERE r.token_sha256 = old.token_sha256 RETURNING r.family, r.account_id, "
                       "r.tenant_id, r.machine_id, r.host, r.expires_at, old.was_used IS NOT NULL, "
                       "r.revoked_at IS NOT NULL", (time.time(), sha))
        if not rows:
            return None
        f = rows[0]
        return _Refresh(f[0], f[1], f[2], f[3], f[4], f[5], used=bool(f[6]), revoked=bool(f[7]))

    def revoke_family(self, family: str) -> None:
        self._q("UPDATE lokkal_gateway_refresh SET revoked_at = %s WHERE family = %s AND revoked_at IS NULL",
                (time.time(), family))

    def family_of(self, sha: str) -> str | None:
        rows = self._q("SELECT family FROM lokkal_gateway_refresh WHERE token_sha256 = %s", (sha,))
        return rows[0][0] if rows else None


def _tq(dsn: str, tenant: str, sql: str, params: tuple = ()) -> list[tuple]:
    """Requête sous la RLS de `tenant` (`app.tenant_id` posé par `connect`)."""
    from nokker.store.conn import connect
    from nokker.tenancy.context import tenant_scope

    with tenant_scope(tenant), connect(dsn, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


class PgRelayStore:
    """Approbations fortes relayées (migrations 021 et 027, lots X10 et X20c), même interface que `MemoryRelayStore`.
    Sous RLS : chaque requête porte le locataire. Passages d'état atomiques (`UPDATE … WHERE status = … RETURNING`).
    Les arguments d'une carte relayée s'effacent avec son statut final ou son expiration ; l'empreinte reste."""

    _COLS = ("machine_id, approval_id, tenant_id, account_id, title, reason, strength, challenge, expires_at, "
             "created_at, status, advice, decision, proof, decided_at, result_at, outcome, error, "
             "args_sha256, arguments, summary, summary_source")
    # Effacement des arguments (X20c) : les lignes du résumé partent avec eux ; outil, classe et phrase de tête restent.
    _ERASE = ("arguments = NULL, summary = CASE WHEN summary_source = 'nokker.ai' AND summary IS NOT NULL "
              "THEN summary || '{\"lines\": [], \"erased\": true}'::jsonb ELSE summary END")

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    @staticmethod
    def _row(r: tuple) -> Any:
        from nokker.cloud.approvals import RelayedApproval

        return RelayedApproval(*r)

    def put(self, a: Any) -> Any:
        from psycopg.types.json import Jsonb

        rows = _tq(self.dsn, a.tenant,
                   f"INSERT INTO lokkal_relay_approvals ({self._COLS}) VALUES "
                   "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, NULL, NULL, NULL, NULL, NULL, NULL, "
                   "%s, %s, %s, %s) "
                   "ON CONFLICT (machine_id, approval_id) DO UPDATE SET title = EXCLUDED.title, "
                   "reason = EXCLUDED.reason, strength = EXCLUDED.strength, challenge = EXCLUDED.challenge, "
                   "expires_at = EXCLUDED.expires_at, advice = EXCLUDED.advice, status = 'pending', decision = NULL, "
                   "proof = NULL, decided_at = NULL, result_at = NULL, outcome = NULL, error = NULL, "
                   "args_sha256 = EXCLUDED.args_sha256, arguments = EXCLUDED.arguments, summary = EXCLUDED.summary, "
                   "summary_source = EXCLUDED.summary_source "
                   "WHERE lokkal_relay_approvals.status NOT IN ('approved', 'rejected') "
                   f"RETURNING {self._COLS}",
                   (a.machine_id, a.approval_id, a.tenant, a.account, a.title, a.reason, a.strength, a.challenge,
                    a.expires_at, a.created_at, a.advice, a.args_sha256,
                    Jsonb(a.arguments) if a.arguments is not None else None,
                    Jsonb(a.summary) if a.summary is not None else None, a.summary_source))
        return self._row(rows[0]) if rows else None

    def get(self, tenant: str, machine_id: str, approval_id: str) -> Any:
        rows = _tq(self.dsn, tenant, f"SELECT {self._COLS} FROM lokkal_relay_approvals "
                   "WHERE machine_id = %s AND approval_id = %s", (machine_id, approval_id))
        return self._row(rows[0]) if rows else None

    def of_machine(self, tenant: str, machine_id: str) -> list[Any]:
        return [self._row(r) for r in _tq(self.dsn, tenant, f"SELECT {self._COLS} FROM lokkal_relay_approvals "
                                          "WHERE machine_id = %s ORDER BY created_at DESC LIMIT 100", (machine_id,))]

    def forward(self, tenant: str, machine_id: str, approval_id: str, decision: str, proof: dict[str, str],
                now: float) -> Any:
        from psycopg.types.json import Jsonb

        rows = _tq(self.dsn, tenant,
                   "UPDATE lokkal_relay_approvals SET status = 'forwarded', decision = %s, proof = %s, decided_at = %s, "
                   "error = NULL WHERE machine_id = %s AND approval_id = %s AND status = 'pending' AND expires_at >= %s "
                   f"RETURNING {self._COLS}", (decision, Jsonb(proof), now, machine_id, approval_id, now))
        return self._row(rows[0]) if rows else None

    def forwarded(self, tenant: str, machine_id: str) -> list[Any]:
        return [self._row(r) for r in _tq(self.dsn, tenant, f"SELECT {self._COLS} FROM lokkal_relay_approvals "
                                          "WHERE machine_id = %s AND status = 'forwarded'", (machine_id,))]

    def set_result(self, tenant: str, machine_id: str, approval_id: str, status: str, outcome: str | None,
                   error: str | None, now: float) -> Any:
        from nokker.cloud.approvals import FINAL

        clear = ", proof = NULL, decision = NULL, decided_at = NULL" if status == "pending" else ""
        if status in FINAL:  # décidée ou expirée : les arguments s'effacent, l'empreinte reste (X20c)
            clear += f", {self._ERASE}"
        rows = _tq(self.dsn, tenant,
                   f"UPDATE lokkal_relay_approvals SET status = %s, result_at = %s, error = %s, "
                   f"outcome = COALESCE(%s, outcome){clear} WHERE machine_id = %s AND approval_id = %s "
                   f"RETURNING {self._COLS}", (status, now, error, outcome, machine_id, approval_id))
        return self._row(rows[0]) if rows else None

    def purge(self, tenant: str, now: float) -> int:
        """Efface les arguments des cartes expirées du locataire (index partiel de 027 : seulement les lignes qui en
        portent encore)."""
        return len(_tq(self.dsn, tenant, f"UPDATE lokkal_relay_approvals SET {self._ERASE} "
                       "WHERE arguments IS NOT NULL AND expires_at < %s RETURNING approval_id", (now,)))


class PgHandoffStore:
    """Demandes de relais web → Lokkal (migration 025, lot X21), même interface que `MemoryHandoffStore`. Sous RLS :
    chaque requête porte le locataire. Passages d'état atomiques (`UPDATE … WHERE state = ANY(…) RETURNING`)."""

    _COLS = ("id", "tenant_id", "account_id", "web_thread", "kind", "name", "task", "machine_id", "machine_name",
             "state", "created_at", "updated_at", "online", "lokkal_thread", "reply", "error", "turn_index",
             "pending_approvals", "attempts")
    _FIELDS = {"tenant": "tenant_id", "account": "account_id"}  # champ de Handoff → colonne

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    @staticmethod
    def _row(r: tuple) -> Any:
        from nokker.cloud.handoff import Handoff

        return Handoff(*r)

    def put(self, h: Any) -> None:
        vals = (h.id, h.tenant, h.account, h.web_thread, h.kind, h.name, h.task, h.machine_id, h.machine_name, h.state,
                h.created_at, h.updated_at, h.online, h.lokkal_thread, h.reply, h.error, h.turn_index,
                h.pending_approvals, h.attempts)
        _tq(self.dsn, h.tenant, f"INSERT INTO lokkal_handoffs ({', '.join(self._COLS)}) VALUES "
            f"({', '.join(['%s'] * len(self._COLS))})", vals)

    def get(self, tenant: str, handoff_id: str) -> Any:
        rows = _tq(self.dsn, tenant, f"SELECT {', '.join(self._COLS)} FROM lokkal_handoffs WHERE id = %s", (handoff_id,))
        return self._row(rows[0]) if rows else None

    def transition(self, tenant: str, handoff_id: str, expect: tuple[str, ...], **changes: Any) -> Any:
        cols = [self._FIELDS.get(k, k) for k in changes]
        bad = [c for c in cols if c not in self._COLS or c in ("id", "tenant_id", "account_id")]
        if bad:
            raise ValueError(f"colonnes refusées : {bad}")
        sets = ", ".join(f"{c} = %s" for c in cols)
        guard, params = "", [*changes.values(), handoff_id, list(expect)]
        if changes.get("turn_index") is not None:
            guard = " AND turn_index < %s"  # résultat déjà rangé : rien ne change
            params.append(changes["turn_index"])
        rows = _tq(self.dsn, tenant, f"UPDATE lokkal_handoffs SET {sets} WHERE id = %s AND state = ANY(%s){guard} "
                   f"RETURNING {', '.join(self._COLS)}", tuple(params))
        return self._row(rows[0]) if rows else None

    def of_account(self, tenant: str, account: str, web_thread: str | None = None) -> list[Any]:
        sql = f"SELECT {', '.join(self._COLS)} FROM lokkal_handoffs WHERE account_id = %s"
        params: list[Any] = [account]
        if web_thread is not None:
            sql += " AND web_thread = %s"
            params.append(web_thread)
        return [self._row(r) for r in _tq(self.dsn, tenant, sql + " ORDER BY created_at DESC LIMIT 100",
                                          tuple(params))]

    def waiting_for(self, tenant: str, machine_id: str) -> list[Any]:
        return [self._row(r) for r in _tq(self.dsn, tenant, f"SELECT {', '.join(self._COLS)} FROM lokkal_handoffs "
                                          "WHERE machine_id = %s AND state = 'waiting'", (machine_id,))]


class PgMirrorNotes:
    """Notes en miroir (migration 025, lot X21), même interface que `nokker.cloud.mirror.MemoryMirrorNotes`."""

    _COLS = ("kind", "scope", "locality", "title", "text", "created", "derived_from")

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def put(self, tenant: str, machine: str, note: dict[str, Any], at: float) -> None:
        cols = ", ".join(self._COLS)
        upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in (*self._COLS, "updated_at"))
        _tq(self.dsn, tenant, f"INSERT INTO lokkal_mirror_notes (machine_id, project, name, tenant_id, {cols}, "
            f"updated_at) VALUES (%s, %s, %s, %s, {', '.join(['%s'] * len(self._COLS))}, %s) "
            f"ON CONFLICT (machine_id, project, name) DO UPDATE SET {upd}",
            (machine, note["project"], note["name"], tenant, *(note[c] for c in self._COLS), at))

    def forget(self, tenant: str, machine: str, project: str, name: str) -> bool:
        return bool(_tq(self.dsn, tenant, "DELETE FROM lokkal_mirror_notes WHERE machine_id = %s AND project = %s "
                        "AND name = %s RETURNING name", (machine, project, name)))

    def of_tenant(self, tenant: str) -> list[dict[str, Any]]:
        cols = ("machine_id", "project", "name", *self._COLS, "updated_at")
        return [dict(zip(cols, r, strict=True)) for r in _tq(self.dsn, tenant, f"SELECT {', '.join(cols)} FROM lokkal_mirror_notes "
                                                "ORDER BY updated_at")]

    def keys(self, tenant: str, machine: str) -> list[tuple[str, str]]:
        return [(r[0], r[1]) for r in _tq(self.dsn, tenant, "SELECT project, name FROM lokkal_mirror_notes "
                                          "WHERE machine_id = %s ORDER BY project, name", (machine,))]

    def erase(self, tenant: str, machine: str) -> int:
        return len(_tq(self.dsn, tenant, "DELETE FROM lokkal_mirror_notes WHERE machine_id = %s RETURNING name",
                       (machine,)))


class PgMirrorPages:
    """Copies des pages (migration 033, docs/LOKKAL.md §7.23), même interface que
    `nokker.cloud.mirror_pages.MemoryMirrorPages` ; sous RLS : lues et écrites dans le locataire de la machine."""

    _LIST = "page_id, title, version, size, sha256, page_updated_at, mirrored_at"

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def put(self, tenant: str, machine: str, page_id: str, row: dict[str, Any], quota: int) -> int:
        """Une transaction sous le verrou consultatif DU LOCATAIRE : deux copies simultanées ne dépassent pas le
        plafond à elles deux."""
        from nokker.cloud.mirror_pages import QuotaExceeded
        from nokker.store.conn import connect
        from nokker.tenancy.context import tenant_scope

        with tenant_scope(tenant), connect(self.dsn) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("mirror_pages:" + tenant,))
            used = int(conn.execute("SELECT coalesce(sum(size), 0) FROM lokkal_mirror_pages WHERE NOT "
                                    "(machine_id = %s AND page_id = %s)", (machine, page_id)).fetchone()[0])
            if used + row["size"] > quota:
                raise QuotaExceeded(used, quota)
            conn.execute(
                "INSERT INTO lokkal_mirror_pages (machine_id, page_id, tenant_id, title, version, size, sha256, html, "
                "page_updated_at, mirrored_at, public_sha256, public_index) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (machine_id, page_id) DO UPDATE SET title = EXCLUDED.title, version = EXCLUDED.version, "
                "size = EXCLUDED.size, sha256 = EXCLUDED.sha256, html = EXCLUDED.html, "
                "page_updated_at = EXCLUDED.page_updated_at, mirrored_at = EXCLUDED.mirrored_at, "
                "public_sha256 = EXCLUDED.public_sha256, public_index = EXCLUDED.public_index",
                (machine, page_id, tenant, row["title"], row["version"], row["size"], row["sha256"], row["html"],
                 row["page_updated_at"], row["mirrored_at"], row.get("public_sha256"),
                 bool(row.get("public_index"))))
            return used + row["size"]

    def get(self, tenant: str, machine: str, page_id: str) -> dict[str, Any] | None:
        rows = _tq(self.dsn, tenant, f"SELECT {self._LIST}, html FROM lokkal_mirror_pages WHERE machine_id = %s AND "
                   "page_id = %s", (machine, page_id))
        if not rows:
            return None
        r = rows[0]
        return {"title": r[1], "version": r[2], "size": r[3], "sha256": r[4], "page_updated_at": r[5],
                "mirrored_at": r[6], "html": bytes(r[7])}

    def delete(self, tenant: str, machine: str, page_id: str) -> bool:
        return bool(_tq(self.dsn, tenant, "DELETE FROM lokkal_mirror_pages WHERE machine_id = %s AND page_id = %s "
                        "RETURNING page_id", (machine, page_id)))

    def get_public(self, tenant: str, machine: str, public_sha256: str) -> dict[str, Any] | None:
        """Copie publique (migration 035, §7.26) de cette machine par l'empreinte de son id public."""
        rows = _tq(self.dsn, tenant, f"SELECT {self._LIST}, html, public_index FROM lokkal_mirror_pages WHERE "
                   "machine_id = %s AND public_sha256 = %s LIMIT 1", (machine, public_sha256))
        if not rows:
            return None
        r = rows[0]
        return {"page_id": r[0], "title": r[1], "version": r[2], "size": r[3], "sha256": r[4], "page_updated_at": r[5],
                "mirrored_at": r[6], "html": bytes(r[7]), "public_sha256": public_sha256, "public_index": bool(r[8])}

    def clear_public(self, tenant: str, machine: str, page_id: str) -> bool:
        return bool(_tq(self.dsn, tenant, "UPDATE lokkal_mirror_pages SET public_sha256 = NULL, public_index = false "
                        "WHERE machine_id = %s AND page_id = %s AND public_sha256 IS NOT NULL RETURNING page_id",
                        (machine, page_id)))

    def of_machine(self, tenant: str, machine: str) -> list[dict[str, Any]]:
        cols = ("page_id", "title", "version", "size", "sha256", "page_updated_at", "mirrored_at")
        return [dict(zip(cols, r, strict=True)) for r in _tq(
            self.dsn, tenant, f"SELECT {self._LIST} FROM lokkal_mirror_pages WHERE machine_id = %s ORDER BY page_id",
            (machine,))]

    def used(self, tenant: str) -> int:
        return int(_tq(self.dsn, tenant, "SELECT coalesce(sum(size), 0) FROM lokkal_mirror_pages")[0][0])

    def erase(self, tenant: str, machine: str) -> int:
        return len(_tq(self.dsn, tenant, "DELETE FROM lokkal_mirror_pages WHERE machine_id = %s RETURNING page_id",
                       (machine,)))


class PgUsedJti:
    """Jti des jetons de pages présentés au relais, toutes répliques (migration 035, §7.26), même interface que
    `nokker.cloud.mirror_pages.MemoryUsedJti`. HORS RLS (cherché par empreinte, aucun locataire, aucune donnée
    personnelle : l'empreinte d'un identifiant aléatoire de 60 s). Premier usage atomique par `INSERT … ON CONFLICT`."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._swept = 0.0

    def first_use(self, jti_sha256: str, expires_at: float, now: float) -> bool:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            if now - self._swept > 60:
                self._swept = now
                conn.execute("DELETE FROM lokkal_relay_used_jti WHERE expires_at < %s", (now,))
            cur = conn.execute(
                "INSERT INTO lokkal_relay_used_jti (jti_sha256, expires_at) VALUES (%s, %s) ON CONFLICT (jti_sha256) "
                "DO UPDATE SET expires_at = EXCLUDED.expires_at WHERE lokkal_relay_used_jti.expires_at < %s "
                "RETURNING jti_sha256", (jti_sha256, expires_at, now))
            return cur.fetchone() is not None


__all__ = ["PgGatewayAuthStore", "PgHandoffStore", "PgLinkStore", "PgMirrorNotes", "PgMirrorPages", "PgRelayStore",
           "PgUsedJti"]


class PgDeckStore:
    """Deck courant de chaque machine (lot X15, migration 028), même interface que `MemoryDeckStore` ; sous RLS : lu et
    écrit dans le locataire de la machine ou du compte."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def set_deck(self, machine_id: str, tenant: str, deck: dict[str, Any], at: float) -> None:
        from psycopg.types.json import Jsonb

        _tq(self.dsn, tenant, "INSERT INTO lokkal_machine_deck (machine_id, tenant_id, deck, at) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (machine_id) DO UPDATE SET deck = EXCLUDED.deck, at = EXCLUDED.at",
            (machine_id, tenant, Jsonb(deck), at))

    def decks_of(self, tenant: str, machine_ids: list[str]) -> dict[str, tuple[dict[str, Any], float]]:
        if not machine_ids:
            return {}
        return {r[0]: (r[1], r[2]) for r in _tq(
            self.dsn, tenant, "SELECT machine_id, deck, at FROM lokkal_machine_deck WHERE machine_id = ANY(%s)",
            (list(machine_ids),))}
