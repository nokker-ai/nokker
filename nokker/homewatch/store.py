"""État de la veille, PAR LOCATAIRE (docs/HOME-WATCH.md §6) : éléments de revue vus (texte seulement, rétention
courte), incidents déjà notifiés, appareils connus du réseau, nouveaux appareils, dernier état des NAS.

Aucune image, aucune URL de média. Un locataire ne lit jamais l'état d'un autre : toute lecture passe par le
locataire courant (`current_tenant_or_none()`), toute écriture par celui du foyer. Persistance facultative : un
fichier JSON 0600 dans le dossier d'état de Lokkal, réécrit atomiquement.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_REVIEWS = 500
MAX_NEW_DEVICES = 50
MAX_KNOWN = 2000


def _empty() -> dict[str, Any]:
    return {"reviews": {}, "known_macs": [], "baseline": False, "new_devices": [], "nas": None, "nas_at": None,
            "clients": None, "clients_at": None, "errors": {}}


@dataclass
class WatchStore:
    path: Path | None = None
    retention_s: float = 72 * 3600.0
    clock: Any = time.time
    _data: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.path is not None and self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self._data = {str(t): {**_empty(), **v} for t, v in (raw or {}).items() if isinstance(v, dict)}
            except (OSError, ValueError):
                self._data = {}

    def _t(self, tenant: str) -> dict[str, Any]:
        return self._data.setdefault(tenant, _empty())

    def _save(self) -> None:
        if self.path is None:
            return
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _prune(self, t: dict[str, Any]) -> None:
        cut = self.clock() - self.retention_s
        reviews = {k: v for k, v in t["reviews"].items() if (v.get("start") or v.get("seen_at") or 0) >= cut}
        if len(reviews) > MAX_REVIEWS:
            keep = sorted(reviews.values(), key=lambda r: r.get("start") or 0)[-MAX_REVIEWS:]
            reviews = {r["id"]: r for r in keep}
        t["reviews"] = reviews
        t["new_devices"] = [d for d in t["new_devices"] if (d.get("first_seen") or 0) >= cut][-MAX_NEW_DEVICES:]

    # ── caméras ──
    def upsert_reviews(self, tenant: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Range les éléments ; rend ceux qui DEVIENNENT dangereux et n'ont pas encore été notifiés (marqués)."""
        fresh: list[dict[str, Any]] = []
        with self._lock:
            t = self._t(tenant)
            for it in items:
                rid = str(it.get("id") or "")
                if not rid:
                    continue
                prev = t["reviews"].get(rid) or {}
                row = {**it, "seen_at": prev.get("seen_at") or self.clock(), "notified": prev.get("notified", False)}
                if row.get("niveau") == "dangereux" and not row["notified"]:
                    row["notified"] = True
                    fresh.append(dict(row))
                t["reviews"][rid] = row
            self._prune(t)
            self._save()
        return fresh

    def reviews(self, tenant: str, since: float) -> list[dict[str, Any]]:
        with self._lock:
            t = self._data.get(tenant)
            if t is None:
                return []
            rows = [dict(r) for r in t["reviews"].values() if (r.get("start") or r.get("seen_at") or 0) >= since]
        return sorted(rows, key=lambda r: r.get("start") or 0, reverse=True)

    def review(self, tenant: str, review_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = (self._data.get(tenant) or {}).get("reviews", {}).get(review_id)
            return dict(r) if r else None

    # ── réseau ──
    def update_clients(self, tenant: str, clients: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Premier passage = référence (aucun « nouveau ») ; ensuite, une MAC jamais vue = nouvel appareil inconnu."""
        new: list[dict[str, Any]] = []
        with self._lock:
            t = self._t(tenant)
            known = set(t["known_macs"])
            for c in clients:
                mac = c.get("mac") or ""
                if not mac or mac in known:
                    continue
                known.add(mac)
                if t["baseline"]:
                    d = {"mac": mac, "name": c.get("name") or "", "ip": c.get("ip") or "", "type": c.get("type") or "",
                         "first_seen": self.clock()}
                    t["new_devices"].append(d)
                    new.append(d)
            t["known_macs"] = sorted(known)[-MAX_KNOWN:]
            t["baseline"] = True
            t["clients"], t["clients_at"] = clients, self.clock()
            self._prune(t)
            self._save()
        return new

    def clients(self, tenant: str) -> tuple[list[dict[str, Any]] | None, float | None]:
        with self._lock:
            t = self._data.get(tenant) or {}
            return t.get("clients"), t.get("clients_at")

    def new_devices(self, tenant: str, since: float) -> list[dict[str, Any]]:
        with self._lock:
            t = self._data.get(tenant) or {}
            return [dict(d) for d in t.get("new_devices") or [] if (d.get("first_seen") or 0) >= since]

    def is_new(self, tenant: str, mac: str, since: float) -> bool:
        return any(d["mac"] == mac for d in self.new_devices(tenant, since))

    # ── NAS ──
    def set_nas(self, tenant: str, systems: list[dict[str, Any]]) -> None:
        with self._lock:
            t = self._t(tenant)
            t["nas"], t["nas_at"] = systems, self.clock()
            self._save()

    def nas(self, tenant: str) -> tuple[list[dict[str, Any]] | None, float | None]:
        with self._lock:
            t = self._data.get(tenant) or {}
            return t.get("nas"), t.get("nas_at")

    def set_error(self, tenant: str, what: str, message: str | None) -> None:
        with self._lock:
            errs = self._t(tenant)["errors"]
            if message is None:
                errs.pop(what, None)
            else:
                errs[what] = message[:200]

    def errors(self, tenant: str) -> dict[str, str]:
        with self._lock:
            return dict((self._data.get(tenant) or {}).get("errors") or {})

    def tenants(self) -> list[str]:
        with self._lock:
            return sorted(self._data)


__all__ = ["WatchStore"]
