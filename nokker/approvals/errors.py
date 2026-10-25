"""Exceptions du port `Approvals` (docs/APPROVALS.md §4) — levées par `decide()` quand la preuve, le
nonce, l'expiration ou l'expéditeur ne tiennent pas. Jamais de retour « approuvé » dégradé en silence :
toute défaillance de preuve est une exception, à charge de l'appelant (route API) de la traduire en
réponse HTTP (4xx)."""
from __future__ import annotations


class ApprovalError(Exception):
    """Base commune — ne pas lever directement."""


class ApprovalNotFound(ApprovalError):
    pass


class ApprovalNotPending(ApprovalError):
    """L'approbation n'est plus `pending` (déjà décidée, ou déjà exécutée/en échec)."""


class ApprovalExpired(ApprovalError):
    pass


class ProofInvalid(ApprovalError):
    """Signature, TOTP, nonce ou `initData` Telegram invalide — ou niveau de preuve insuffisant
    (ex. TOTP présenté pour une approbation `strong`)."""


class StrongDeviceRequired(ProofInvalid):
    """Preuve trop faible pour cette carte (403, code `strong_device_required`) : appareil de simple connexion (code
    e-mail, Google), qui ne décide jamais ; ou clé logicielle sur une carte « strong » (Mini App avec biométrie,
    passkey avec vérification, ou app native attestée requises)."""


class DeviceError(Exception):
    pass


class DeviceNotFound(DeviceError):
    pass


class DeviceNotApproved(DeviceError):
    pass


class SessionError(Exception):
    pass


__all__ = [
    "ApprovalError", "ApprovalExpired", "ApprovalNotFound", "ApprovalNotPending", "DeviceError", "DeviceNotApproved",
    "DeviceNotFound", "ProofInvalid", "SessionError", "StrongDeviceRequired",
]
