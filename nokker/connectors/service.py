"""Cartes Connecteurs (T4) : relier, tester, révoquer. Un connecteur n'est enregistré qu'APRÈS un test réussi, pour qu'une
carte « reliée » veuille dire « a répondu au dernier essai ». Le secret entre une fois et ne ressort jamais."""
from __future__ import annotations

from typing import Any

from nokker.connectors import TestResult, test_connection
from nokker.connectors.errors import ConnectorError
from nokker.connectors.mail import PRESETS, normalize_settings
from nokker.connectors.store import KINDS, ConnectorStore, Status


def public_settings(kind: str, settings: dict[str, Any]) -> dict[str, Any]:
    """Ce que la carte peut afficher : le compte relié et le serveur, jamais l'URL secrète d'un agenda iCal."""
    if kind == "mail":
        return {k: settings.get(k) for k in ("preset", "address", "username", "imap_host", "smtp_host") if settings.get(k)}
    return {k: settings.get(k) for k in ("mode", "label", "username", "url") if settings.get(k)}


def card(st: Status | None, kind: str) -> dict[str, Any]:
    if st is None:
        return {"kind": kind, "state": "absent"}
    return {"kind": kind, "state": "ok" if st.last_ok_at else "unverified", "settings": public_settings(kind, st.settings),
            "connected_at": st.created_at.isoformat(), "last_ok_at": st.last_ok_at.isoformat() if st.last_ok_at else None}


class Connectors:
    def __init__(self, store: ConnectorStore, tester=test_connection) -> None:
        self.store, self._test = store, tester

    def presets(self) -> dict[str, Any]:
        return {"mail": sorted(PRESETS), "calendar": ["ical", "caldav"]}

    def cards(self, account_id: str) -> list[dict[str, Any]]:
        by_kind = {s.kind: s for s in self.store.statuses(account_id)}
        return [card(by_kind.get(k), k) for k in KINDS]

    def connect(self, account_id: str, kind: str, settings: dict[str, Any], secret: str) -> dict[str, Any]:
        """Teste puis enregistre. Lève `ConnectorError` si le test échoue (rien n'est écrit)."""
        if kind not in KINDS:
            raise ConnectorError("bad_settings", f"nature inconnue : {kind}")
        if kind == "mail":
            settings = normalize_settings(settings)
        secret = str(secret or "").strip()
        r: TestResult = self._test(kind, settings, secret)
        if not r.ok:
            raise ConnectorError(r.code or "protocol_error", r.message)
        self.store.put(account_id, kind, settings, secret)
        self.store.mark_ok(account_id, kind)
        return {**card(next(s for s in self.store.statuses(account_id) if s.kind == kind), kind), "message": r.message}

    def test(self, account_id: str, kind: str) -> dict[str, Any]:
        cred = self.store.get(account_id, kind)
        if cred is None:
            raise ConnectorError("not_configured", "connecteur non relié")
        r = self._test(kind, cred.settings, cred.secret)
        if r.ok:
            self.store.mark_ok(account_id, kind)
        return {"ok": r.ok, "message": r.message, "code": r.code}

    def revoke(self, account_id: str, kind: str) -> bool:
        return self.store.delete(account_id, kind)
