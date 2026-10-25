"""Modération des pages publiques, côté nokker.ai (docs/LOKKAL.md §7.26, point 6).

Une page publique vit sur la machine de son auteur ; nokker.ai n'en voit que ce que le relais lui montre. D'où :

- **Registre** (`lokkal_public_pages`, migration 040, HORS RLS comme `lokkal_machines` : l'opérateur le lit pour tous
  les locataires, le relais le consulte avant de connaître le locataire) : une ligne par page publique servie au moins
  une fois par le relais (machine en ligne : `POST /v1/gateway/public-page/check` ; machine éteinte : la copie
  publique du miroir), avec la machine, le locataire, le `page_id` (dit par la machine, en-tête `X-Nokker-Page-Id`),
  l'EMPREINTE de l'id public (jamais l'id), la taille, la première et la dernière vue, et l'heure du retrait.
  Jamais le contenu, le titre, ni l'adresse ou l'identifiant du compte.
- **Retrait** (`python -m nokker pages unpublish`) : par l'adresse publique (`https://<nom>--pub.nokker.app/<id>`), par
  l'adresse de la page sur l'origine privée (`https://<nom>--pages.nokker.app/<page_id>`) ou par le `page_id`. Le relais
  demande au registre AVANT de servir une page publique : retirée → 410 (machine en ligne comme copie du miroir), même
  republiée sous un autre id public tant que le `page_id` est connu. La publication de la copie du miroir est effacée.
  La machine apprend ses pages retirées au contrôle qu'elle fait déjà (`GET /v1/gateway/machine` →
  `moderated_pages`, `moderated_public`) : elle dépublie, garde la page à son auteur marquée `moderated_at` et refuse
  de la republier tant que l'opérateur ne l'a pas rendue (`restore`).
- **Audit** (`lokkal_page_moderation`, migration 040, ajout seul) : page, empreinte, machine, locataire, action, motif,
  date. Le motif est obligatoire ; une adresse e-mail qu'il contiendrait est remplacée avant d'être rangée.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

REMOVED = "retirée par la modération"
SEEN_WRITE_EVERY_S = 300.0  # une vue ne réécrit la ligne que toutes les 5 min (le relais peut servir 1200 vues/min)
MAX_REASON = 500
_PAGE_ID = re.compile(r"^pg_[0-9a-f]{16}$")
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9_-]{24}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_COLS = ("id", "machine_id", "tenant_id", "page_id", "public_sha256", "size", "first_seen_at", "last_seen_at",
         "moderated_at")


class ModerationError(ValueError):
    """Cible illisible ou inconnue, motif absent : rien n'est fait."""


def public_sha(public_id: str) -> str:
    return hashlib.sha256(public_id.encode()).hexdigest()


def is_page_id(value: str | None) -> bool:
    return bool(_PAGE_ID.match(value or ""))


def clean_reason(reason: str | None) -> str:
    """Motif d'un retrait : obligatoire, 500 caractères au plus, sans adresse e-mail (la sortie et l'audit n'en portent
    jamais)."""
    text = _EMAIL.sub("[adresse retirée]", " ".join(str(reason or "").split()))
    if not text:
        raise ModerationError("motif obligatoire (--reason)")
    if len(text) > MAX_REASON:
        raise ModerationError(f"motif de {MAX_REASON} caractères au plus")
    return text


def _plan(match: list[dict[str, Any]], page_id: str | None) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Lignes d'une même page (par `page_id` ou par empreinte) : celle qu'on garde (celle du `page_id` d'abord) et
    celles qui s'y fondent (une page retirée par son adresse publique, puis vue avec son `page_id`)."""
    if not match:
        return None, []
    keep = next((r for r in match if page_id and r["page_id"] == page_id), None) or match[0]
    return keep, [r for r in match if r is not keep]


def _conflict(keep: dict[str, Any], page_id: str | None, public_sha256: str | None) -> bool:
    """Vue qui CONTREDIT une identité déjà connue de la ligne (autre page_id sur la même adresse, ou l'inverse)."""
    return bool((page_id and keep["page_id"] and keep["page_id"] != page_id)
                or (public_sha256 and keep["public_sha256"] and keep["public_sha256"] != public_sha256))


def _blocked_extras(match: list[dict[str, Any]], page_id: str | None, public_sha256: str | None,
                    ) -> list[tuple[str | None, str | None]]:
    """Identités annoncées qu'aucune ligne ne porte encore : inscrites retirées à leur tour (jamais en réécrivant une
    ligne retirée, sans quoi une machine qui ment sur le page_id libérerait la page d'origine)."""
    out: list[tuple[str | None, str | None]] = []
    if page_id and not any(r["page_id"] == page_id for r in match):
        out.append((page_id, None))
    if public_sha256 and not any(r["public_sha256"] == public_sha256 for r in match):
        out.append((None, public_sha256))
    return out


def _fold(keep: dict[str, Any], drop: list[dict[str, Any]]) -> None:
    for r in drop:
        keep["first_seen_at"] = min(keep["first_seen_at"], r["first_seen_at"])
        keep["last_seen_at"] = max(keep["last_seen_at"], r["last_seen_at"])
        if r["moderated_at"] is not None:
            keep["moderated_at"] = max(keep["moderated_at"] or 0.0, r["moderated_at"])
        keep["page_id"] = keep["page_id"] or r["page_id"]
        keep["public_sha256"] = keep["public_sha256"] or r["public_sha256"]


# ─────────────────────────────── magasins ───────────────────────────────


class MemoryPublicPages:
    """Registre et audit (tests, noyau sans Postgres) ; `PgPublicPages` a la même interface."""

    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.log: list[dict[str, Any]] = []
        self._next = 1
        self.lock = threading.Lock()

    def _match(self, machine_id: str, page_id: str | None, sha: str | None) -> list[dict[str, Any]]:
        return [r for r in self.rows.values() if r["machine_id"] == machine_id
                and ((page_id and r["page_id"] == page_id) or (sha and r["public_sha256"] == sha))]

    def _insert(self, row: dict[str, Any]) -> dict[str, Any]:
        row = {"id": self._next, **row}
        self.rows[self._next] = row
        self._next += 1
        return row

    def view(self, machine_id: str, tenant_id: str, public_sha256: str, page_id: str | None, size: int,
             now: float) -> bool:
        """Une vue publique servie (ou sur le point de l'être) : la page est inscrite ; rend True si elle est retirée."""
        with self.lock:
            match = self._match(machine_id, page_id, public_sha256)
            blocked = any(r["moderated_at"] is not None for r in match)
            keep, drop = _plan(match, page_id)
            if keep is None:
                self._insert({"machine_id": machine_id, "tenant_id": tenant_id, "page_id": page_id,
                              "public_sha256": public_sha256, "size": int(size), "first_seen_at": now,
                              "last_seen_at": now, "moderated_at": None})
                return False
            if blocked and any(_conflict(r, page_id, public_sha256) for r in match):
                at = max(r["moderated_at"] for r in match if r["moderated_at"] is not None)
                for r in match:  # tout ce qui a été vu avec une identité retirée est retiré ; rien n'est réécrit
                    r["last_seen_at"] = now
                    r["moderated_at"] = r["moderated_at"] or at
                for pid, sha in _blocked_extras(match, page_id, public_sha256):
                    self._insert({"machine_id": machine_id, "tenant_id": tenant_id, "page_id": pid,
                                  "public_sha256": sha, "size": int(size), "first_seen_at": now, "last_seen_at": now,
                                  "moderated_at": at})
                return True
            _fold(keep, drop)
            for r in drop:
                del self.rows[r["id"]]
            keep.update(public_sha256=public_sha256, page_id=page_id or keep["page_id"], size=int(size),
                        last_seen_at=now)
            return blocked

    def moderated_of(self, machine_id: str) -> dict[str, list[str]]:
        with self.lock:
            rows = [r for r in self.rows.values() if r["machine_id"] == machine_id and r["moderated_at"] is not None]
        return {"page_ids": sorted(r["page_id"] for r in rows if r["page_id"]),
                "public_sha256": sorted(r["public_sha256"] for r in rows if r["public_sha256"])}

    def listing(self, moderated: bool = False) -> list[dict[str, Any]]:
        with self.lock:
            rows = [dict(r) for r in self.rows.values() if (r["moderated_at"] is not None) is moderated]
        return sorted(rows, key=lambda r: (r["first_seen_at"], r["id"]))

    def find(self, *, page_id: str | None = None, public_sha256: str | None = None) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.rows.values()
                    if (page_id and r["page_id"] == page_id) or (public_sha256 and r["public_sha256"] == public_sha256)]

    def set_moderation(self, machine_id: str, tenant_id: str, page_id: str | None, public_sha256: str | None,
                       action: str, reason: str, now: float) -> dict[str, Any]:
        """`unpublish` : la page (connue ou non du registre) est retirée ; `restore` : elle ne l'est plus. Une ligne
        d'audit dans les deux cas."""
        if action not in ("unpublish", "restore"):
            raise ValueError(action)
        with self.lock:
            match = self._match(machine_id, page_id, public_sha256)
            keep, drop = _plan(match, page_id)
            if keep is None:
                if action == "unpublish":
                    self._insert({"machine_id": machine_id, "tenant_id": tenant_id, "page_id": page_id,
                                  "public_sha256": public_sha256, "size": 0, "first_seen_at": now,
                                  "last_seen_at": now, "moderated_at": now})
            else:
                _fold(keep, drop)
                for r in drop:
                    del self.rows[r["id"]]
                keep["moderated_at"] = now if action == "unpublish" else None
                page_id, public_sha256 = keep["page_id"], keep["public_sha256"]
            event = {"at": now, "machine_id": machine_id, "tenant_id": tenant_id, "page_id": page_id,
                     "public_sha256": public_sha256, "action": action, "reason": reason}
            self.log.append(event)
            return dict(event)

    def events(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(e) for e in self.log]


class PgPublicPages:
    """Registre et audit en Postgres (migration 040, hors RLS). Une page à la fois par machine : verrou consultatif de
    la machine pendant la fusion des lignes."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _conn(self, autocommit: bool = True) -> Any:
        import psycopg

        return psycopg.connect(self.dsn, autocommit=autocommit)

    @staticmethod
    def _rows(cur: Any) -> list[dict[str, Any]]:
        return [dict(zip(_COLS, r, strict=True)) for r in cur.fetchall()]

    _SELECT = f"SELECT {', '.join(_COLS)} FROM lokkal_public_pages"

    def _match(self, conn: Any, machine_id: str, page_id: str | None, sha: str | None) -> list[dict[str, Any]]:
        cur = conn.execute(f"{self._SELECT} WHERE machine_id = %s AND (page_id = %s OR public_sha256 = %s) ORDER BY id",
                           (machine_id, page_id, sha))
        return self._rows(cur)

    def _apply(self, conn: Any, keep: dict[str, Any], drop: list[dict[str, Any]]) -> None:
        if drop:
            conn.execute("DELETE FROM lokkal_public_pages WHERE id = ANY(%s)", ([r["id"] for r in drop],))
        conn.execute("UPDATE lokkal_public_pages SET page_id = %s, public_sha256 = %s, size = %s, first_seen_at = %s, "
                     "last_seen_at = %s, moderated_at = %s WHERE id = %s",
                     (keep["page_id"], keep["public_sha256"], keep["size"], keep["first_seen_at"],
                      keep["last_seen_at"], keep["moderated_at"], keep["id"]))

    def _insert(self, conn: Any, row: dict[str, Any]) -> None:
        conn.execute("INSERT INTO lokkal_public_pages (machine_id, tenant_id, page_id, public_sha256, size, "
                     "first_seen_at, last_seen_at, moderated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                     (row["machine_id"], row["tenant_id"], row["page_id"], row["public_sha256"], row["size"],
                      row["first_seen_at"], row["last_seen_at"], row["moderated_at"]))

    @staticmethod
    def _lock(conn: Any, machine_id: str) -> None:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("public_pages:" + machine_id,))

    def view(self, machine_id: str, tenant_id: str, public_sha256: str, page_id: str | None, size: int,
             now: float) -> bool:
        with self._conn() as conn:  # lecture seule d'abord : une page déjà inscrite et vue il y a peu ne réécrit rien
            match = self._match(conn, machine_id, page_id, public_sha256)
        if len(match) == 1:
            r = match[0]
            if r["public_sha256"] == public_sha256 and (page_id is None or r["page_id"] == page_id) \
                    and r["size"] == int(size) and now - r["last_seen_at"] < SEEN_WRITE_EVERY_S:
                return r["moderated_at"] is not None
        with self._conn(autocommit=False) as conn:
            self._lock(conn, machine_id)
            match = self._match(conn, machine_id, page_id, public_sha256)
            blocked = any(r["moderated_at"] is not None for r in match)
            keep, drop = _plan(match, page_id)
            if keep is None:
                self._insert(conn, {"machine_id": machine_id, "tenant_id": tenant_id, "page_id": page_id,
                                    "public_sha256": public_sha256, "size": int(size), "first_seen_at": now,
                                    "last_seen_at": now, "moderated_at": None})
                return False
            if blocked and any(_conflict(r, page_id, public_sha256) for r in match):
                at = max(r["moderated_at"] for r in match if r["moderated_at"] is not None)
                conn.execute("UPDATE lokkal_public_pages SET last_seen_at = %s, moderated_at = COALESCE(moderated_at, %s) "
                             "WHERE id = ANY(%s)", (now, at, [r["id"] for r in match]))
                for pid, sha in _blocked_extras(match, page_id, public_sha256):
                    self._insert(conn, {"machine_id": machine_id, "tenant_id": tenant_id, "page_id": pid,
                                        "public_sha256": sha, "size": int(size), "first_seen_at": now,
                                        "last_seen_at": now, "moderated_at": at})
                return True
            _fold(keep, drop)
            keep.update(public_sha256=public_sha256, page_id=page_id or keep["page_id"], size=int(size),
                        last_seen_at=now)
            self._apply(conn, keep, drop)
            return blocked

    def moderated_of(self, machine_id: str) -> dict[str, list[str]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT page_id, public_sha256 FROM lokkal_public_pages WHERE machine_id = %s "
                                "AND moderated_at IS NOT NULL", (machine_id,)).fetchall()
        return {"page_ids": sorted(r[0] for r in rows if r[0]), "public_sha256": sorted(r[1] for r in rows if r[1])}

    def listing(self, moderated: bool = False) -> list[dict[str, Any]]:
        cond = "IS NOT NULL" if moderated else "IS NULL"
        with self._conn() as conn:
            return self._rows(conn.execute(f"{self._SELECT} WHERE moderated_at {cond} ORDER BY first_seen_at, id"))

    def find(self, *, page_id: str | None = None, public_sha256: str | None = None) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return self._rows(conn.execute(f"{self._SELECT} WHERE page_id = %s OR public_sha256 = %s ORDER BY id",
                                           (page_id, public_sha256)))

    def set_moderation(self, machine_id: str, tenant_id: str, page_id: str | None, public_sha256: str | None,
                       action: str, reason: str, now: float) -> dict[str, Any]:
        if action not in ("unpublish", "restore"):
            raise ValueError(action)
        with self._conn(autocommit=False) as conn:
            self._lock(conn, machine_id)
            keep, drop = _plan(self._match(conn, machine_id, page_id, public_sha256), page_id)
            if keep is None:
                if action == "unpublish":
                    self._insert(conn, {"machine_id": machine_id, "tenant_id": tenant_id, "page_id": page_id,
                                        "public_sha256": public_sha256, "size": 0, "first_seen_at": now,
                                        "last_seen_at": now, "moderated_at": now})
            else:
                _fold(keep, drop)
                keep["moderated_at"] = now if action == "unpublish" else None
                self._apply(conn, keep, drop)
                page_id, public_sha256 = keep["page_id"], keep["public_sha256"]
            conn.execute("INSERT INTO lokkal_page_moderation (at, machine_id, tenant_id, page_id, public_sha256, "
                         "action, reason) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                         (now, machine_id, tenant_id, page_id, public_sha256, action, reason))
        return {"at": now, "machine_id": machine_id, "tenant_id": tenant_id, "page_id": page_id,
                "public_sha256": public_sha256, "action": action, "reason": reason}

    def events(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT at, machine_id, tenant_id, page_id, public_sha256, action, reason "
                                "FROM lokkal_page_moderation ORDER BY id").fetchall()
        cols = ("at", "machine_id", "tenant_id", "page_id", "public_sha256", "action", "reason")
        return [dict(zip(cols, r, strict=True)) for r in rows]


# ─────────────────────────────── route du relais ───────────────────────────────


def check_view(store: Any, links: Any, public_allowed: Callable[[str], bool] | None, body: Any,
               now: float) -> tuple[int, dict[str, Any]]:
    """`POST /v1/gateway/public-page/check {host, public_id, page_id?, size?}` (clé du relais) : la machine a rendu la
    page, le relais demande s'il peut la servir. 200 `{allowed: true}` (la vue est inscrite au registre) ; 410
    `moderated` ; 404 si le nom n'est pas une machine active ou si le compte n'a pas le droit de publier."""
    body = body if isinstance(body, dict) else {}
    public_id = str(body.get("public_id") or "")
    if not _PUBLIC_ID.match(public_id):
        return 404, {"error": {"message": "not found"}}
    m = links.machine_by_host(str(body.get("host") or ""))
    if m is None or public_allowed is None or not public_allowed(m.account):
        return 404, {"error": {"message": "not found"}}
    page_id = body.get("page_id") if is_page_id(body.get("page_id")) else None
    try:
        size = max(0, int(body.get("size") or 0))
    except (TypeError, ValueError):
        size = 0
    if store.view(m.id, m.tenant, public_sha(public_id), page_id, size, now):
        return 410, {"error": {"code": "moderated", "message": f"page {REMOVED}"}}
    return 200, {"allowed": True}


# ─────────────────────────────── opérateur ───────────────────────────────


@dataclass
class Target:
    machine_id: str
    tenant_id: str
    host: str  # <nom>.nokker.app
    page_id: str | None
    public_sha256: str | None

    @property
    def label(self) -> str:
        return self.page_id or f"pub:{(self.public_sha256 or '')[:16]}"


def _iso(ts: float | None) -> str:
    return datetime.fromtimestamp(float(ts), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if ts is not None else ""


@dataclass
class Moderation:
    """Ce que fait la commande d'opérateur, sur des magasins injectés (Postgres en vrai, mémoire en test)."""

    store: Any  # MemoryPublicPages | PgPublicPages
    machine_by_host: Callable[[str], Any]  # « <nom>.nokker.app » → machine (id, tenant, host), révoquée comprise
    machine_by_id: Callable[[str], Any]
    mirror_pages: Any = None  # copies du miroir (publication effacée au retrait) ; None = pas de miroir
    domain: str = "nokker.app"

    def public_name(self, machine_id: str) -> str:
        m = self.machine_by_id(machine_id)
        name = (getattr(m, "host", "") or "").split(".", 1)[0]
        return f"{name}--pub.{self.domain}" if name else "?"

    def _target_of(self, rows: list[dict[str, Any]], what: str) -> Target:
        machines = {r["machine_id"] for r in rows}
        if not machines:
            raise ModerationError(f"{what} inconnu du registre : donne l'adresse https://<nom>--pub.{self.domain}/<id>")
        if len(machines) > 1:
            raise ModerationError(f"{what} ambigu (plusieurs machines) : donne l'adresse publique")
        r = rows[0]
        m = self.machine_by_id(r["machine_id"])
        return Target(r["machine_id"], r["tenant_id"], getattr(m, "host", "") or "", r["page_id"], r["public_sha256"])

    def resolve(self, target: str) -> Target:
        """Adresse publique `https://<nom>--pub.<domaine>/<id public>`, adresse privée
        `https://<nom>--pages.<domaine>/<page_id>`, `page_id` ou id public seuls."""
        target = (target or "").strip()
        if is_page_id(target):
            return self._target_of(self.store.find(page_id=target), "page_id")
        if _PUBLIC_ID.match(target):
            return self._target_of(self.store.find(public_sha256=public_sha(target)), "id public")
        u = urlsplit(target)
        host, path = (u.hostname or "").lower(), u.path.strip("/")
        suffix = f".{self.domain}"
        if u.scheme != "https" or not host.endswith(suffix):
            raise ModerationError(f"adresse illisible : https://<nom>--pub.{self.domain}/<id> attendue")
        label = host[: -len(suffix)]
        name, _, origin = label.partition("--")
        m = self.machine_by_host(f"{name}{suffix}") if name and origin in ("pub", "pages") else None
        if m is None:
            raise ModerationError("aucune machine ne porte ce nom")
        if origin == "pub" and _PUBLIC_ID.match(path):
            sha = public_sha(path)
            known = [r for r in self.store.find(public_sha256=sha) if r["machine_id"] == m.id]
            return Target(m.id, m.tenant, m.host, known[0]["page_id"] if known else None, sha)
        if origin == "pages" and is_page_id(path):
            known = [r for r in self.store.find(page_id=path) if r["machine_id"] == m.id]
            return Target(m.id, m.tenant, m.host, path, known[0]["public_sha256"] if known else None)
        raise ModerationError("chemin illisible : /<id public> sur --pub, /<page_id> sur --pages")

    def list_pages(self, moderated: bool = False) -> list[dict[str, Any]]:
        """Lignes de `pages list` : page, nom public, locataire, date (première vue ; retrait pour `--moderated`),
        taille. Jamais le contenu ni le compte."""
        return [{"page": r["page_id"] or f"pub:{(r['public_sha256'] or '')[:16]}",
                 "public_name": self.public_name(r["machine_id"]), "tenant": r["tenant_id"],
                 "date": _iso(r["moderated_at"] if moderated else r["first_seen_at"]), "size": int(r["size"])}
                for r in self.store.listing(moderated=moderated)]

    def _clear_mirror(self, t: Target) -> int:
        if self.mirror_pages is None:
            return 0
        from nokker.tenancy.context import tenant_scope

        with tenant_scope(t.tenant_id):
            page_id = t.page_id
            if page_id is None and t.public_sha256:
                row = self.mirror_pages.get_public(t.tenant_id, t.machine_id, t.public_sha256)
                page_id = (row or {}).get("page_id")
            return int(bool(page_id and self.mirror_pages.clear_public(t.tenant_id, t.machine_id, page_id)))

    def change(self, target: str, reason: str, *, action: str, apply: bool,
               now: float | None = None) -> dict[str, Any]:
        """`unpublish` | `restore`. À blanc : ce qui serait fait. Rend `{target, already, applied, event?}`."""
        why = clean_reason(reason)
        t = self.resolve(target)
        rows = [r for r in self.store.find(page_id=t.page_id, public_sha256=t.public_sha256)
                if r["machine_id"] == t.machine_id]
        moderated = any(r["moderated_at"] is not None for r in rows)
        already = moderated if action == "unpublish" else not moderated
        out: dict[str, Any] = {"target": t, "already": already, "applied": False, "reason": why}
        if not apply or already:
            return out
        at = time.time() if now is None else now
        event = self.store.set_moderation(t.machine_id, t.tenant_id, t.page_id, t.public_sha256, action, why, at)
        t.page_id, t.public_sha256 = event["page_id"], event["public_sha256"]
        out.update(applied=True, event=event, mirror_cleared=self._clear_mirror(t) if action == "unpublish" else 0)
        return out

    def audit_line(self, out: dict[str, Any]) -> dict[str, Any]:
        """La ligne d'audit imprimée : page, nom public, locataire, motif, date. Jamais d'adresse ni de compte."""
        t, ev = out["target"], out["event"]
        return {"event": "page_unpublished" if ev["action"] == "unpublish" else "page_restored", "page": t.label,
                "public_name": self.public_name(t.machine_id), "tenant": t.tenant_id, "reason": ev["reason"],
                "at": _iso(ev["at"])}


class PgMachines:
    """Machines liées (`lokkal_machines`, hors RLS) vues par l'opérateur : nom de passerelle et locataire seulement."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _one(self, where: str, value: str) -> Any:
        import psycopg

        with psycopg.connect(self.dsn, autocommit=True) as conn:
            r = conn.execute(f"SELECT id, tenant_id, host FROM lokkal_machines WHERE {where} = %s", (value,)).fetchone()
        if r is None:
            return None
        return type("OperatorMachine", (), {"id": r[0], "tenant": r[1], "host": r[2]})()

    def by_host(self, host: str) -> Any:
        return self._one("host", host.lower())

    def by_id(self, machine_id: str) -> Any:
        return self._one("id", machine_id)


def open_operator(config: Any) -> Moderation:
    """La commande d'opérateur sur la base de la configuration (même DSN que `nokker erase`)."""
    from nokker.cloud.pg import PgMirrorPages

    dsn = config.pg_dsn()
    if not dsn:
        raise ModerationError("aucun DSN Postgres configuré")
    machines = PgMachines(dsn)
    domain = str((getattr(config, "cloud_link", None) or {}).get("gateway_domain") or "nokker.app")
    return Moderation(store=PgPublicPages(dsn), machine_by_host=machines.by_host, machine_by_id=machines.by_id,
                      mirror_pages=PgMirrorPages(dsn), domain=domain)


__all__ = ["REMOVED", "MemoryPublicPages", "Moderation", "ModerationError", "PgMachines", "PgPublicPages", "Target",
           "check_view", "clean_reason", "open_operator", "public_sha"]
