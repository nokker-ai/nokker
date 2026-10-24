"""Stockage de la console opérateur (lot A0) : opérateurs, journal d'audit, candidatures.

`PgAdminStore` (tables de `store/migrations/003_admin.sql`) ou `MemoryAdminStore` (dev, tests), même contrat. Toute
écriture métier passe par une méthode qui écrit SA ligne d'audit dans la même transaction (espace-admin §2.5) ; le
journal n'a ni mise à jour ni effacement.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

ROLES = ("owner", "ops", "lecteur")

# Colonnes d'une candidature, dans l'ordre du formulaire (nokker-beta-api, site/beta-api/main.py).
APP_FIELDS = ("prenom", "nom", "email", "pays", "profil", "profil_detail", "taches", "outils", "dispo", "appareils",
              "connu_par", "lang", "utm", "referrer")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def parse_ts(v: Any) -> datetime | None:
    """Horodatage de la SQLite de front1 (`datetime('now')` = « AAAA-MM-JJ HH:MM:SS », UTC)."""
    if not v:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace(" ", "T"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def normalize_application(raw: dict[str, Any]) -> dict[str, Any]:
    """Ligne tirée de front1 → colonnes de `beta_applications` (le champ « source » du formulaire devient
    `connu_par` : `source` désigne ici l'origine technique de la ligne)."""
    row = {k: str(raw.get(k) or "") for k in APP_FIELDS if k != "connu_par"}
    row["connu_par"] = str(raw.get("source") or "")
    row["email"] = row["email"].strip().lower()
    row["source_id"] = int(raw["id"])
    row["received_at"] = parse_ts(raw.get("ts")) or _now()
    row["consent_at"] = parse_ts(raw.get("consent_at"))
    return row


class AdminStore(Protocol):
    def seed_operators(self, operators: dict[str, str], actor: str) -> None: ...

    def role_of(self, email: str) -> str | None: ...

    def max_source_id(self, source: str) -> int: ...

    def count_applications(self) -> int: ...

    def has_imported(self) -> bool: ...

    def import_applications(self, source: str, rows: list[dict[str, Any]], actor: str) -> list[dict[str, Any]]: ...

    def list_applications(self, *, statut: str = "", q: str = "", limit: int = 100,
                          offset: int = 0) -> tuple[list[dict[str, Any]], int]: ...

    def view_application(self, app_id: int, actor: str) -> dict[str, Any] | None: ...

    def stats(self) -> dict[str, Any]: ...

    def audit(self, *, limit: int = 100) -> list[dict[str, Any]]: ...

    def application(self, app_id: int) -> dict[str, Any] | None: ...

    def record(self, actor: str, action: str, obj: str, after: Any = None, reason: str = "") -> None: ...


@dataclass
class MemoryAdminStore:
    operators: dict[str, str] = field(default_factory=dict)
    applications: list[dict[str, Any]] = field(default_factory=list)
    journal: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _audit(self, actor: str, action: str, obj: str = "", before: Any = None, after: Any = None,
               reason: str = "") -> None:
        self.journal.append({"id": len(self.journal) + 1, "at": _now(), "actor": actor, "action": action,
                             "object": obj, "before": before, "after": after, "reason": reason})

    def seed_operators(self, operators: dict[str, str], actor: str) -> None:
        with self._lock:
            for email, role in operators.items():
                email = email.strip().lower()
                if role not in ROLES:
                    raise ValueError(f"rôle inconnu : {role}")
                before = self.operators.get(email)
                if before != role:
                    self.operators[email] = role
                    self._audit(actor, "operator.seed", f"operator:{email}", {"role": before} if before else None,
                                {"role": role}, "config admin_operators")

    def role_of(self, email: str) -> str | None:
        return self.operators.get((email or "").strip().lower())

    def max_source_id(self, source: str) -> int:
        return max((a["source_id"] for a in self.applications if a["source"] == source), default=0)

    def count_applications(self) -> int:
        return len(self.applications)

    def has_imported(self) -> bool:
        return any(j["action"] == "application.import" for j in self.journal)

    def import_applications(self, source: str, rows: list[dict[str, Any]], actor: str) -> list[dict[str, Any]]:
        new: list[dict[str, Any]] = []
        with self._lock:
            seen = {(a["source"], a["source_id"]) for a in self.applications}
            for r in rows:
                if (source, r["source_id"]) in seen:
                    continue
                dup = any(a["email"] == r["email"] for a in self.applications)
                app = {**r, "id": len(self.applications) + 1, "source": source, "imported_at": _now(),
                       "statut": "recue"}
                self.applications.append(app)
                seen.add((source, r["source_id"]))
                self._audit(actor, "application.import", f"application:{app['id']}", None,
                            {"source": source, "source_id": r["source_id"]})
                new.append({**app, "duplicate": dup})
        return new

    def list_applications(self, *, statut: str = "", q: str = "", limit: int = 100,
                          offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        items = [a for a in self.applications if (not statut or a["statut"] == statut)
                 and (not q or q.lower() in " ".join(str(a.get(k, "")) for k in APP_FIELDS).lower())]
        items.sort(key=lambda a: a["received_at"], reverse=True)
        return [dict(a) for a in items[offset:offset + limit]], len(items)

    def view_application(self, app_id: int, actor: str) -> dict[str, Any] | None:
        with self._lock:
            app = next((a for a in self.applications if a["id"] == app_id), None)
            if app is None:
                return None
            self._audit(actor, "application.view", f"application:{app_id}")
            out = dict(app)
            out["duplicates"] = [a["id"] for a in self.applications if a["email"] == app["email"] and a is not app]
            out["history"] = [dict(j) for j in self.journal if j["object"] == f"application:{app_id}"]
            return out

    def stats(self) -> dict[str, Any]:
        def count(key: str) -> dict[str, int]:
            out: dict[str, int] = {}
            for a in self.applications:
                out[a.get(key) or "—"] = out.get(a.get(key) or "—", 0) + 1
            return out

        days: dict[str, int] = {}
        for a in self.applications:
            d = a["received_at"].date().isoformat()
            days[d] = days.get(d, 0) + 1
        return {"total": len(self.applications), "emails": len({a["email"] for a in self.applications}),
                "by_profil": count("profil"), "by_lang": count("lang"), "by_connu_par": count("connu_par"),
                "by_statut": count("statut"), "by_day": dict(sorted(days.items()))}

    def audit(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return [dict(j) for j in reversed(self.journal[-limit:])]

    def application(self, app_id: int) -> dict[str, Any] | None:
        app = next((a for a in self.applications if a["id"] == app_id), None)
        return dict(app) if app else None

    def record(self, actor: str, action: str, obj: str, after: Any = None, reason: str = "") -> None:
        with self._lock:
            self._audit(actor, action, obj, None, after, reason)


_APP_COLS = ("id", "source", "source_id", "received_at", "imported_at", *APP_FIELDS, "consent_at", "statut")


class PgAdminStore:
    """Postgres du noyau (même DSN que le `Store`). Une connexion par opération : la console est peu sollicitée."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _conn(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def _audit(cur, actor: str, action: str, obj: str = "", before: Any = None, after: Any = None,
               reason: str = "") -> None:
        cur.execute("INSERT INTO admin_audit (actor, action, object, before, after, reason) VALUES (%s,%s,%s,%s,%s,%s)",
                    (actor, action, obj, json.dumps(before, default=_iso) if before is not None else None,
                     json.dumps(after, default=_iso) if after is not None else None, reason))

    def seed_operators(self, operators: dict[str, str], actor: str) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            for email, role in operators.items():
                email = email.strip().lower()
                if role not in ROLES:
                    raise ValueError(f"rôle inconnu : {role}")
                cur.execute("SELECT role FROM operators WHERE email=%s FOR UPDATE", (email,))
                row = cur.fetchone()
                if row and row["role"] == role:
                    continue
                cur.execute("INSERT INTO operators (email, role) VALUES (%s,%s) ON CONFLICT (email) DO UPDATE "
                            "SET role=EXCLUDED.role, updated_at=now()", (email, role))
                self._audit(cur, actor, "operator.seed", f"operator:{email}", {"role": row["role"]} if row else None,
                            {"role": role}, "config admin_operators")

    def role_of(self, email: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT role FROM operators WHERE email=%s", ((email or "").strip().lower(),)).fetchone()
        return row["role"] if row else None

    def max_source_id(self, source: str) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COALESCE(MAX(source_id), 0) AS m FROM beta_applications WHERE source=%s",
                               (source,)).fetchone()
        return int(row["m"])

    def count_applications(self) -> int:
        with self._conn() as conn:
            return int(conn.execute("SELECT COUNT(*) AS n FROM beta_applications").fetchone()["n"])

    def has_imported(self) -> bool:
        """Un import a déjà eu lieu (le journal le garde même si la candidature a été effacée depuis)."""
        with self._conn() as conn:
            return bool(conn.execute("SELECT EXISTS (SELECT 1 FROM admin_audit WHERE action='application.import') AS e")
                        .fetchone()["e"])

    def import_applications(self, source: str, rows: list[dict[str, Any]], actor: str) -> list[dict[str, Any]]:
        new: list[dict[str, Any]] = []
        cols = ("source", "source_id", "received_at", *APP_FIELDS, "consent_at")
        with self._conn() as conn, conn.cursor() as cur:
            for r in rows:
                cur.execute(f"INSERT INTO beta_applications ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                            f"ON CONFLICT (source, source_id) DO NOTHING RETURNING {', '.join(_APP_COLS)}",
                            (source, *(r[c] for c in cols[1:])))
                app = cur.fetchone()
                if app is None:
                    continue
                cur.execute("SELECT EXISTS (SELECT 1 FROM beta_applications WHERE email=%s AND id<>%s) AS d",
                            (app["email"], app["id"]))
                dup = bool(cur.fetchone()["d"])
                self._audit(cur, actor, "application.import", f"application:{app['id']}", None,
                            {"source": source, "source_id": r["source_id"]})
                new.append({**app, "duplicate": dup})
        return new

    def list_applications(self, *, statut: str = "", q: str = "", limit: int = 100,
                          offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        where, args = [], []
        if statut:
            where.append("statut=%s")
            args.append(statut)
        if q:
            where.append("(" + " || ' ' || ".join(APP_FIELDS) + ") ILIKE %s")
            args.append(f"%{q}%")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._conn() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) AS n FROM beta_applications {clause}", args).fetchone()["n"])
            rows = conn.execute(f"SELECT {', '.join(_APP_COLS)} FROM beta_applications {clause} "
                                "ORDER BY received_at DESC, id DESC LIMIT %s OFFSET %s", [*args, limit, offset]).fetchall()
        return [dict(r) for r in rows], total

    def view_application(self, app_id: int, actor: str) -> dict[str, Any] | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(_APP_COLS)} FROM beta_applications WHERE id=%s", (app_id,))
            app = cur.fetchone()
            if app is None:
                return None
            self._audit(cur, actor, "application.view", f"application:{app_id}")
            cur.execute("SELECT id FROM beta_applications WHERE email=%s AND id<>%s ORDER BY id", (app["email"], app_id))
            dups = [r["id"] for r in cur.fetchall()]
            cur.execute("SELECT id, at, actor, action, object, before, after, reason FROM admin_audit WHERE object=%s "
                        "ORDER BY id", (f"application:{app_id}",))
            history = [dict(r) for r in cur.fetchall()]
        return {**app, "duplicates": dups, "history": history}

    def stats(self) -> dict[str, Any]:
        with self._conn() as conn:
            def count(col: str) -> dict[str, int]:
                rows = conn.execute(f"SELECT COALESCE(NULLIF({col}, ''), '—') AS k, COUNT(*) AS n FROM beta_applications "
                                    "GROUP BY 1 ORDER BY 2 DESC").fetchall()
                return {r["k"]: int(r["n"]) for r in rows}

            head = conn.execute("SELECT COUNT(*) AS t, COUNT(DISTINCT email) AS e FROM beta_applications").fetchone()
            days = conn.execute("SELECT to_char(received_at AT TIME ZONE 'Europe/Zurich', 'YYYY-MM-DD') AS d, "
                                "COUNT(*) AS n FROM beta_applications GROUP BY 1 ORDER BY 1").fetchall()
            return {"total": int(head["t"]), "emails": int(head["e"]), "by_profil": count("profil"),
                    "by_lang": count("lang"), "by_connu_par": count("connu_par"), "by_statut": count("statut"),
                    "by_day": {r["d"]: int(r["n"]) for r in days}}

    def audit(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT id, at, actor, action, object, before, after, reason FROM admin_audit "
                                "ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def application(self, app_id: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(f"SELECT {', '.join(_APP_COLS)} FROM beta_applications WHERE id=%s", (app_id,)).fetchone()
        return dict(row) if row else None

    def record(self, actor: str, action: str, obj: str, after: Any = None, reason: str = "") -> None:
        """Ligne d'audit d'une action qui touche une AUTRE base (front1) : écrite après l'appel, succès comme échec."""
        with self._conn() as conn, conn.cursor() as cur:
            self._audit(cur, actor, action, obj, None, after, reason)


__all__ = ["APP_FIELDS", "AdminStore", "MemoryAdminStore", "PgAdminStore", "ROLES", "normalize_application",
           "parse_ts"]
