"""Fiche de candidature, plans en attente de carte et journal des dépôts, chez Lokkal (docs/JOBS-PACK.md §6.4, §7.4).

Tout est CHIFFRÉ au repos avec la clé maître des sites (`sites-master`, au coffre système) : la fiche (données de la
personne), les plans (valeurs du formulaire) et les preuves (captures de la page de confirmation). Rien de ceci ne part
au service emploi : il reçoit le type de preuve, l'heure et les empreintes (§13.7).

- `job_deposits` : AJOUT SEUL (une ligne JSON chiffrée par événement) ; l'état d'un dépôt est sa dernière ligne.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from nokker.jobs.apply.model import Plan
from nokker.sites.crypto import open_profile, seal_profile


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


class Vaulted:
    """Un document JSON chiffré par (compte, nom)."""

    def __init__(self, master: bytes, root: str | Path | None) -> None:
        self.master = master
        self.root = Path(root) if root else None
        self._mem: dict[str, bytes] = {}
        self._lock = threading.RLock()

    def _key(self, account: str, name: str) -> str:
        return f"{account}/{name}"

    def put(self, account: str, name: str, obj: Any) -> None:
        blob = seal_profile(self.master, account, f"jobs:{name}", 0, json.dumps(obj, ensure_ascii=False).encode())
        with self._lock:
            if self.root is None:
                self._mem[self._key(account, name)] = blob
            else:
                _write(self.root / _safe(account) / f"{_safe(name)}.bin", blob)

    def get(self, account: str, name: str) -> Any:
        with self._lock:
            if self.root is None:
                blob = self._mem.get(self._key(account, name))
            else:
                p = self.root / _safe(account) / f"{_safe(name)}.bin"
                blob = p.read_bytes() if p.exists() else None
        if blob is None:
            return None
        return json.loads(open_profile(self.master, account, f"jobs:{name}", 0, blob).decode("utf-8"))

    def delete(self, account: str, name: str) -> None:
        with self._lock:
            self._mem.pop(self._key(account, name), None)
            if self.root is not None:
                (self.root / _safe(account) / f"{_safe(name)}.bin").unlink(missing_ok=True)


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in str(s))[:120] or "_"


class SheetStore:
    """Fiche de candidature d'un profil emploi (§6.4) : clés canoniques (`portals.SHEET_LABELS`) et réponses récurrentes
    par libellé. Remplie PAR LA PERSONNE (écran) ; une réponse donnée pour un dépôt est PROPOSÉE pour la fiche, jamais
    enregistrée en silence."""

    def __init__(self, vault: Vaulted) -> None:
        self.vault = vault

    def get(self, account: str, profile: str) -> dict[str, Any]:
        d = self.vault.get(account, f"sheet-{profile}") or {}
        return {"fields": dict(d.get("fields") or {}), "answers": dict(d.get("answers") or {})}

    def put(self, account: str, profile: str, fields: dict[str, str] | None = None,
            answers: dict[str, str] | None = None) -> dict[str, Any]:
        cur = self.get(account, profile)
        cur["fields"].update({k: str(v) for k, v in (fields or {}).items()})
        cur["answers"].update({k: str(v) for k, v in (answers or {}).items()})
        self.vault.put(account, f"sheet-{profile}", cur)
        return cur


class Plans:
    """Plans en attente de carte (24 h), par id ; chiffrés au repos."""

    TTL_S = 24 * 3600.0

    def __init__(self, vault: Vaulted, clock: Any = time.time) -> None:
        self.vault, self.clock = vault, clock

    def put(self, plan: Plan) -> None:
        self.vault.put(plan.owner_account, f"plan-{plan.id}", plan.to_json())

    def get(self, account: str, plan_id: str) -> Plan | None:
        d = self.vault.get(account, f"plan-{plan_id}")
        if d is None:
            return None
        plan = Plan.from_json(d)
        if self.clock() > plan.created_at + self.TTL_S:
            return None
        return plan

    def drop(self, account: str, plan_id: str) -> None:
        self.vault.delete(account, f"plan-{plan_id}")


class DepositJournal:
    """`job_deposits` : ajout seul. Chaque ligne est un événement chiffré ; les captures sont rangées à part."""

    def __init__(self, master: bytes, root: str | Path | None, clock: Any = time.time) -> None:
        self.master, self.clock = master, clock
        self.root = Path(root) if root else None
        self._mem: list[tuple[str, bytes]] = []
        self._captures: dict[str, bytes] = {}
        self._lock = threading.RLock()

    def _path(self) -> Path:
        assert self.root is not None
        return self.root / "job_deposits.jsonl"

    def append(self, account: str, event: dict[str, Any]) -> dict[str, Any]:
        event = {**event, "at": event.get("at") or self.clock()}
        blob = seal_profile(self.master, account, "jobs:deposits", 0, json.dumps(event, ensure_ascii=False).encode())
        line = json.dumps({"account": account, "blob": blob.hex()}) + "\n"
        with self._lock:
            if self.root is None:
                self._mem.append((account, blob))
            else:
                self.root.mkdir(parents=True, exist_ok=True)
                fd = os.open(self._path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(line)
        return event

    def events(self, account: str) -> list[dict[str, Any]]:
        with self._lock:
            if self.root is None:
                rows = [(a, b) for a, b in self._mem]
            else:
                rows = []
                if self._path().exists():
                    for line in self._path().read_text(encoding="utf-8").splitlines():
                        d = json.loads(line)
                        rows.append((d["account"], bytes.fromhex(d["blob"])))
        return [json.loads(open_profile(self.master, a, "jobs:deposits", 0, b).decode("utf-8"))
                for a, b in rows if a == account]

    def accounts(self) -> list[str]:
        with self._lock:
            if self.root is None:
                return sorted({a for a, _ in self._mem})
            if not self._path().exists():
                return []
            return sorted({json.loads(line)["account"]
                           for line in self._path().read_text(encoding="utf-8").splitlines() if line})

    def deposits(self, account: str) -> dict[str, dict[str, Any]]:
        """État courant de chaque dépôt (dernier événement), par id de dépôt."""
        out: dict[str, dict[str, Any]] = {}
        for e in self.events(account):
            out[e["deposit"]] = {**out.get(e["deposit"], {}), **e}
        return out

    def save_capture(self, account: str, deposit: str, data: bytes) -> None:
        blob = seal_profile(self.master, account, f"jobs:capture:{deposit}", 0, data)
        with self._lock:
            if self.root is None:
                self._captures[deposit] = blob
            else:
                _write(self.root / "captures" / f"{_safe(deposit)}.bin", blob)

    def capture(self, account: str, deposit: str) -> bytes | None:
        with self._lock:
            if self.root is None:
                blob = self._captures.get(deposit)
            else:
                p = self.root / "captures" / f"{_safe(deposit)}.bin"
                blob = p.read_bytes() if p.exists() else None
        return None if blob is None else open_profile(self.master, account, f"jobs:capture:{deposit}", 0, blob)


__all__ = ["DepositJournal", "Plans", "SheetStore", "Vaulted"]
