"""Passkey LOCALE de Lokkal (docs/PAY-AGENT.md §11, décision du 03.11.2026) : WebAuthn sur la machine, rpId `localhost`,
contexte sûr `http://localhost:<port du web>`, sans compte cloud. Sert à UNE chose : signer une carte « à payer » dont
le mandat est own_card (`BaseApprovals.decide_local_passkey`) ; toute autre carte forte se signe chez nokker.ai.

- `options()` : défi d'enregistrement (5 min, usage unique) pour `navigator.credentials.create` ;
- `register(body)` : vérifie `clientDataJSON` (type `webauthn.create`, ce défi, l'origine locale), exige ES256
  (`public_key_alg` -7, clé SPKI rendue par `getPublicKey()`), enrôle un appareil du propriétaire local, confirmé sur
  place (le propriétaire tient déjà le jeton local, sur SA machine) ;
- l'identifiant de la passkey (`credential_id`) est gardé dans l'état de la machine (`local-passkeys.json`), à côté de
  l'appareil des approbations (pas de colonne de plus).
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

RP_ID = "localhost"
RP_NAME = "Nokker (cette machine)"
ES256 = -7
CHALLENGE_TTL_S = 300


class PasskeyError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def local_origin(cfg: dict[str, Any] | None) -> str:
    """Origine du web local : `local.passkey_origin`, sinon `NOKKER_LOCAL_PASSKEY_ORIGIN`, sinon le port web du compose."""
    raw = (cfg or {}).get("passkey_origin") or os.environ.get("NOKKER_LOCAL_PASSKEY_ORIGIN") \
        or f"http://localhost:{os.environ.get('NOKKER_PORT') or 7420}"
    parts = urlsplit(str(raw))
    if parts.scheme not in ("http", "https") or parts.hostname != RP_ID:
        raise ValueError("local.passkey_origin : http://localhost:<port> attendu (rpId localhost)")
    return f"{parts.scheme}://{parts.netloc}"


class LocalPasskeys:
    def __init__(self, approvals: Any, state_path: Path, origin: str, owner: str,
                 clock: Callable[[], float] = time.time) -> None:
        self.approvals, self.path, self.origin, self.owner, self.clock = approvals, Path(state_path), origin, owner, clock
        self._pending: dict[str, float] = {}
        self._lock = threading.Lock()

    # ── état de la machine ──
    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def list(self) -> list[dict[str, Any]]:
        out = []
        for device_id, meta in sorted(self._load().items(), key=lambda kv: kv[1].get("created_at", 0)):
            dev = self.approvals.get_device(device_id)
            if dev is not None and dev.status == "approved" and dev.user == self.owner:
                out.append({"device_id": device_id, "credential_id": meta.get("credential_id"),
                            "name": meta.get("name") or "", "created_at": meta.get("created_at")})
        return out

    def allow_credentials(self) -> list[str]:
        return [p["credential_id"] for p in self.list() if p.get("credential_id")]

    # ── enregistrement ──
    def options(self) -> dict[str, Any]:
        challenge = _b64url(secrets.token_bytes(32))
        now = self.clock()
        with self._lock:
            self._pending = {c: t for c, t in self._pending.items() if t > now}
            self._pending[challenge] = now + CHALLENGE_TTL_S
        return {"challenge": challenge, "rp_id": RP_ID, "rp_name": RP_NAME,
                "user": {"id": _b64url(self.owner.encode()), "name": self.owner, "display_name": "Propriétaire"},
                "pub_key_cred_params": [{"type": "public-key", "alg": ES256}],
                "exclude_credentials": self.allow_credentials(), "timeout_ms": CHALLENGE_TTL_S * 1000}

    def register(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise PasskeyError(400, "invalid", "corps JSON attendu")
        challenge = str(body.get("challenge") or "")
        with self._lock:
            due = self._pending.pop(challenge, None)
        if due is None or due < self.clock():
            raise PasskeyError(403, "proof_invalid", "défi d'enregistrement inconnu, expiré ou déjà utilisé")
        try:
            client = json.loads(_b64url_decode(str(body.get("client_data_json") or "")))
        except (ValueError, TypeError):
            raise PasskeyError(400, "invalid", "clientDataJSON illisible") from None
        if not isinstance(client, dict) or client.get("type") != "webauthn.create" \
                or client.get("challenge") != challenge or client.get("origin") != self.origin:
            raise PasskeyError(403, "proof_invalid", "enregistrement refusé (type, défi ou origine)")
        if body.get("public_key_alg") != ES256:
            raise PasskeyError(400, "unsupported", "algorithme non pris en charge : ES256 (P-256) attendu")
        cred = str(body.get("credential_id") or "")
        if not 16 <= len(cred) <= 1024:
            raise PasskeyError(400, "invalid", "credential_id illisible")
        try:
            spki_der = _b64url_decode(str(body.get("public_key_spki") or ""))
            from nokker.approvals.crypto import load_p256_public_key_spki

            spki_b64 = base64.b64encode(spki_der).decode()
            load_p256_public_key_spki(spki_b64)
        except Exception:  # noqa: BLE001 — clé illisible ou autre courbe
            raise PasskeyError(400, "invalid", "clé publique P-256 illisible") from None
        data = self._load()
        if any(m.get("credential_id") == cred for m in data.values()):
            raise PasskeyError(409, "exists", "cette passkey est déjà enregistrée")
        name = str(body.get("name") or "Passkey de cette machine")[:64]
        dev = self.approvals.enroll_device(self.owner, name, spki_b64)
        dev = self.approvals.confirm_device_cli(dev.code)  # le propriétaire, sur sa machine, au jeton local
        now = self.clock()
        data[dev.id] = {"credential_id": cred, "name": name, "created_at": now}
        self._save(data)
        return {"device_id": dev.id, "credential_id": cred, "name": name, "created_at": now}

    def delete(self, device_id: str) -> bool:
        data = self._load()
        if device_id not in data:
            return False
        try:
            self.approvals.delete_device(device_id)
        finally:
            data.pop(device_id, None)
            self._save(data)
        return True


__all__ = ["ES256", "RP_ID", "LocalPasskeys", "PasskeyError", "local_origin"]
