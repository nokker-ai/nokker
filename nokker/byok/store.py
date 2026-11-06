"""Réglages BYOK d'un locataire (docs/CLOUD-BYOK.md §3) : modèle par défaut et points d'accès, SANS clé (la clé est au
coffre, `nokker/byok/service.py::ByokSecrets`). Une ligne par locataire, lue et écrite pour le locataire COURANT
(`tenant_scope`) : Postgres sous RLS forcée (migration 066), ou mémoire (dev, tests)."""
from __future__ import annotations

import copy
import json
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol

from nokker.tenancy.context import current_tenant


@dataclass
class ByokSettings:
    default: str = "router"  # router | byok:<nom>
    entries: list[dict[str, Any]] = field(default_factory=list)  # [{name, base_url, model, ok, models?}]

    def entry(self, name: str) -> dict[str, Any] | None:
        return next((e for e in self.entries if e.get("name") == name), None)


class ByokStore(Protocol):
    def get(self) -> ByokSettings: ...

    def put(self, settings: ByokSettings) -> None: ...


class MemoryByokStore:
    def __init__(self) -> None:
        self._rows: dict[str, ByokSettings] = {}
        self._lock = threading.Lock()

    def get(self) -> ByokSettings:
        tid = current_tenant()
        with self._lock:
            got = self._rows.get(tid)
            return copy.deepcopy(got) if got is not None else ByokSettings()

    def put(self, settings: ByokSettings) -> None:
        tid = current_tenant()
        with self._lock:
            self._rows[tid] = copy.deepcopy(settings)


class PgByokStore:
    """Table `tenant_byok` (migration 066). `connect()` pose `app.tenant_id` : la RLS forcée ne montre que la ligne du
    locataire courant, et l'écriture porte explicitement ce locataire (WITH CHECK)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def get(self) -> ByokSettings:
        from nokker.store.conn import connect

        with connect(self.dsn, autocommit=True) as conn:
            row = conn.execute("SELECT default_model, entries FROM tenant_byok WHERE tenant_id = %s",
                               (current_tenant(),)).fetchone()
        if row is None:
            return ByokSettings()
        entries = row[1] if isinstance(row[1], list) else json.loads(row[1] or "[]")
        return ByokSettings(default=str(row[0] or "router"), entries=list(entries))

    def put(self, settings: ByokSettings) -> None:
        from nokker.store.conn import connect

        with connect(self.dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO tenant_byok (tenant_id, default_model, entries, updated_at) VALUES (%s, %s, %s::jsonb, now()) "
                "ON CONFLICT (tenant_id) DO UPDATE SET default_model = EXCLUDED.default_model, "
                "entries = EXCLUDED.entries, updated_at = now()",
                (current_tenant(), settings.default, json.dumps(settings.entries, ensure_ascii=False)))


__all__ = ["ByokSettings", "ByokStore", "MemoryByokStore", "PgByokStore"]
