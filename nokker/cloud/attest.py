"""Décision d'une carte MACHINE par la Mini App Telegram (lot X10, docs/LOKKAL.md §7.3 « attestation »).

Une carte forte d'une machine liée se signe au téléphone de deux façons :

- passkey (WebAuthn UV) : nokker.ai garde la preuve sans la croire, la machine la vérifie elle-même (rien ne change) ;
- Mini App Telegram (Face ID / BiometricManager) : la machine n'a pas le jeton du bot, elle ne peut pas vérifier
  l'`initData` elle-même. NOKKER.AI vérifie donc la preuve Telegram (`TelegramAttestor.attest`) et remet à la machine une
  ATTESTATION signée par la clé de passerelle (`Signer.attestation_token`) : liée à CETTE machine, CE compte, CETTE carte,
  CE défi et CETTE décision, 120 s, usage unique (la machine tient le `jti`). Ici la machine fait confiance à nokker.ai
  pour cette carte-là ; `local.remote_approvals_attested: false` le lui interdit. Plus tard, la machine vérifiera
  elle-même la signature Ed25519 de Telegram sur l'`initData` et n'aura plus besoin de cette confiance.

Ce que nokker.ai vérifie avant de signer : l'`initData` (HMAC du bot, âge), l'appareil (du compte qui possède la machine,
`approved`, pas de simple connexion, `telegram_user_id` = l'utilisateur de l'`initData`), la signature P1363 de l'appareil
sur `"<défi>.<décision>"` avec le défi EXACT que la machine a déposé pour cette carte, une carte forte en attente.

Journal : une ligne `attestation_emise` (compte, machine, carte, appareil, jti, expiration) à chaque attestation émise,
`attestation_refusee` (cause) à chaque refus ; aucune donnée de la carte.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nokker.approvals.crypto import SignatureError, TelegramInitDataError, verify_p1363_signature, \
    verify_telegram_init_data
from nokker.cloud.link import ATTEST_TTL_S, Signer, challenge_sha256

logger = logging.getLogger("nokker.cloud.attest")

MAX_INIT_DATA = 4096


class AttestError(ValueError):
    """Preuve Telegram refusée : `status` HTTP et message (en français, montré tel quel)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class TelegramAttestor:
    signer: Signer
    bot_token: str  # jeton du bot de nokker.ai (celui dont la Mini App est ouverte) ; jamais journalisé
    account_of: Callable[[str], str | None]  # identifiant Telegram → compte nokker.ai appairé, ou None
    devices_of: Callable[[str], list[Any]]  # compte → appareils (backend d'approbations)
    max_age_s: int = 600
    ttl_s: int = ATTEST_TTL_S
    clock: Callable[[], float] = time.time

    def identify(self, init_data: str) -> str | None:
        """Compte appairé à l'utilisateur Telegram d'un `initData` valide ; None si l'`initData` est faux, périmé, ou si
        ce Telegram n'est relié à aucun compte (lecture d'une carte, pas de décision)."""
        try:
            info = self._init_data(init_data)
        except AttestError:
            return None
        tg_id = str((info.get("user") or {}).get("id", ""))
        return self.account_of(tg_id) if tg_id else None

    def _init_data(self, init_data: Any) -> dict[str, Any]:
        if not isinstance(init_data, str) or not init_data or len(init_data) > MAX_INIT_DATA:
            raise AttestError(401, "initData Telegram requis")
        try:
            return verify_telegram_init_data(init_data, self.bot_token, self.max_age_s)
        except TelegramInitDataError:
            raise AttestError(401, "initData Telegram refusé (signature ou âge)") from None

    def attest(self, *, machine_id: str, account: str, card: Any, decision: str,
               proof: dict[str, Any]) -> dict[str, str]:
        """Vérifie la preuve Telegram `{device_id, signature, telegram_init_data}` d'une décision sur `card` (une
        `RelayedApproval` en attente de la machine `machine_id`, du compte `account`) et rend la preuve à garder et à
        remettre à la machine : `{device_id, attestation, via: "telegram"}`. Lève `AttestError`."""
        try:
            return self._attest(machine_id, account, card, decision, proof)
        except AttestError as e:
            logger.warning("attestation_refusee compte=%s machine=%s carte=%s cause=%s", account, machine_id,
                           getattr(card, "approval_id", "?"), e)
            raise

    def _attest(self, machine_id: str, account: str, card: Any, decision: str,
                proof: dict[str, Any]) -> dict[str, str]:
        if decision not in ("approve", "reject"):
            raise AttestError(400, "decision invalide (approve|reject)")
        if card.status != "pending":
            raise AttestError(409, f"carte déjà {card.status}")
        if card.strength != "strong":
            raise AttestError(400, "seule une carte forte se décide par une attestation")
        info = self._init_data(proof.get("telegram_init_data"))
        tg_id = str((info.get("user") or {}).get("id", ""))
        device_id, signature = proof.get("device_id"), proof.get("signature")
        if not tg_id or not isinstance(device_id, str) or not device_id or not isinstance(signature, str) \
                or not signature:
            raise AttestError(400, "proof : {device_id, signature, telegram_init_data}")
        device = next((d for d in self.devices_of(account) or [] if d.id == device_id), None)
        # Même message pour un appareil inconnu et pour celui d'un autre compte : on ne dit pas lequel existe.
        if device is None or device.user != account:
            raise AttestError(403, "appareil inconnu de ce compte")
        from nokker.approvals.base import LOGIN_ONLY_VIA

        if device.status != "approved" or (device.approved_via or "").startswith(LOGIN_ONLY_VIA):
            raise AttestError(403, "appareil non approuvé, ou de simple connexion : il ne décide pas")
        if not device.telegram_user_id or device.telegram_user_id != tg_id:
            raise AttestError(403, "cet appareil n'est pas relié à ce compte Telegram")
        now = self.clock()
        if now > card.expires_at:
            raise AttestError(409, "carte expirée")
        try:
            ch = json.loads(card.challenge)
        except ValueError:
            ch = None
        if not isinstance(ch, dict) or ch.get("approval_id") != card.approval_id or ch.get("aud") != "nokker-approval":
            raise AttestError(409, "défi de la carte illisible")
        if isinstance(ch.get("exp"), (int, float)) and now > float(ch["exp"]):
            raise AttestError(409, "défi expiré : la machine en dépose un nouveau")
        try:
            verify_p1363_signature(device.public_key_spki, f"{card.challenge}.{decision}".encode(), signature)
        except SignatureError:
            raise AttestError(403, "signature de l'appareil invalide pour ce défi") from None
        jti = uuid.uuid4().hex
        token = self.signer.attestation_token(
            account=account, machine=machine_id, approval_id=card.approval_id,
            challenge_sha256=challenge_sha256(card.challenge), decision=decision, device_id=device.id,
            args_sha256=card.args_sha256, ttl_s=self.ttl_s, now=now, jti=jti)
        logger.info("attestation_emise compte=%s machine=%s carte=%s appareil=%s jti=%s exp=%s", account, machine_id,
                    card.approval_id, device.id, jti, int(now) + self.ttl_s)
        return {"device_id": device.id, "attestation": token, "via": "telegram", "jti": jti}


__all__ = ["AttestError", "TelegramAttestor"]
