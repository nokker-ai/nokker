"""Sites connectés — magasin local de `sitesd` (docs/CONNECTED-SITES.md §4.2, §5.5).

Fichier JSON 0600 sous l'état de Lokkal (ou en mémoire pour les tests) : fiches des connecteurs, accords CGU,
journal des lectures d'identifiant (ajout seul). JAMAIS de mot de passe, de code ni de cookie ici : le mot de passe est
au coffre (`credentials.py`), la session dans l'archive chiffrée du profil (`profiles.py`).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from nokker.sites.model import MAX_PER_ACCOUNT, Connector, LabelError, check_label, new_id


class StoreError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SitesStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.max_per_account = MAX_PER_ACCOUNT  # portails emploi (pack Emploi, §6.2) : 50, dans un magasin à part
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"connectors": {}, "consents": [], "credential_reads": []}
        if self.path and self.path.exists():
            self._data.update(json.loads(self.path.read_text(encoding="utf-8")))

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    # ── connecteurs ──
    def all(self) -> list[Connector]:
        with self._lock:
            return [Connector.from_json(d) for d in self._data["connectors"].values()]

    def of(self, account: str) -> list[Connector]:
        return sorted([c for c in self.all() if c.account == account], key=lambda c: (not c.default, c.created_at))

    def get(self, cid: str) -> Connector | None:
        with self._lock:
            d = self._data["connectors"].get(cid)
            return Connector.from_json(d) if d else None

    def put(self, conn: Connector) -> Connector:
        with self._lock:
            self._data["connectors"][conn.id] = conn.to_json()
            self._save()
        return conn

    def add(self, *, account: str, site: str, site_name: str, username: str, label: str | None = None,
            url: str | None = None, hosts: list[str] | None = None) -> Connector:
        with self._lock:
            mine = self.of(account)
            if len(mine) >= self.max_per_account:
                raise StoreError("too_many", f"au plus {self.max_per_account} sites connectés par profil")
            try:
                text = check_label(label or site_name)
            except LabelError as e:
                raise StoreError("bad_label", str(e)) from None
            if not label:  # libellé par défaut déjà pris : « LinkedIn 2 », « LinkedIn 3 »…
                n, base = 2, text
                while any(c.label.casefold() == text.casefold() for c in mine):
                    text, n = f"{base} {n}", n + 1
            elif any(c.label.casefold() == text.casefold() for c in mine):
                raise StoreError("label_taken", f"libellé déjà pris : « {text} »")
            if not str(username or "").strip():
                raise StoreError("bad_username", "identifiant requis (e-mail ou nom d'utilisateur)")
            conn = Connector(id=new_id(), account=account, site=site, site_name=site_name, label=text,
                             username=username.strip(), url=url, hosts=list(hosts or []),
                             default=not any(c.site == site for c in mine))
            return self.put(conn)

    def rename(self, cid: str, label: str) -> Connector:
        with self._lock:
            conn = self.get(cid)
            if conn is None:
                raise StoreError("not_found", "connecteur inconnu")
            try:
                text = check_label(label)
            except LabelError as e:
                raise StoreError("bad_label", str(e)) from None
            if any(c.id != cid and c.label.casefold() == text.casefold() for c in self.of(conn.account)):
                raise StoreError("label_taken", f"libellé déjà pris : « {text} »")
            conn.label = text
            return self.put(conn)

    def set_default(self, cid: str) -> Connector:
        with self._lock:
            conn = self.get(cid)
            if conn is None:
                raise StoreError("not_found", "connecteur inconnu")
            for c in self.of(conn.account):
                if c.site == conn.site and c.default != (c.id == cid):
                    c.default = c.id == cid
                    self.put(c)
            return self.get(cid)  # type: ignore[return-value]

    def delete(self, cid: str) -> bool:
        with self._lock:
            conn = self._data["connectors"].pop(cid, None)
            if conn and conn.get("default"):
                rest = [c for c in self.of(conn["account"]) if c.site == conn["site"]]
                if rest:
                    rest[0].default = True
                    self._data["connectors"][rest[0].id] = rest[0].to_json()
            self._save()
            return conn is not None

    # ── accords CGU (§4.1), journal des lectures d'identifiant (§5.5) ──
    def consent(self, account: str, site: str, version: str) -> None:
        with self._lock:
            self._data["consents"].append({"account": account, "site": site, "version": version, "at": time.time()})
            self._save()

    def has_consent(self, account: str, site: str, version: str) -> bool:
        return any(c["account"] == account and c["site"] == site and c["version"] == version
                   for c in self._data["consents"])

    def consented_sites(self, account: str, version: str) -> list[str]:
        return sorted({c["site"] for c in self._data["consents"] if c["account"] == account and c["version"] == version})

    def log_credential_read(self, conn: Connector, reason: str) -> None:
        with self._lock:
            self._data["credential_reads"].append({"connector": conn.id, "site": conn.site, "account": conn.account,
                                                   "at": time.time(), "reason": reason})
            self._save()

    def credential_reads(self, cid: str | None = None) -> list[dict[str, Any]]:
        return [r for r in self._data["credential_reads"] if cid is None or r["connector"] == cid]


__all__ = ["SitesStore", "StoreError"]
