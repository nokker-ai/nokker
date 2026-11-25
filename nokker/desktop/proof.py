"""Preuve de clé du nœud `desktop` : même forme que celle du nœud téléphone (`X-Nokker-Node-Proof: v1;ts=;nonce=;sig=`,
ES256 sur `méthode\\nhôte\\nchemin?requête\\nts\\nnonce\\nsha256(corps)`, ±60 s, nonce vu 120 s). Une seule implémentation
du message et de la vérification : celles de `nokker/cloud/phone.py`."""
from __future__ import annotations

from nokker.cloud.phone import NONCE_TTL_S, PROOF_HEADER, PROOF_SKEW_S, PhoneError, proof_message, verify_signature
from nokker.cloud.phone import parse_proof as _parse


def parse_proof(header: str | None) -> tuple[str, str, bytes] | None:
    """(ts, nonce, signature) ou None si l'en-tête est absent ou illisible."""
    try:
        return _parse(header)
    except PhoneError:
        return None


__all__ = ["NONCE_TTL_S", "PROOF_HEADER", "PROOF_SKEW_S", "parse_proof", "proof_message", "verify_signature"]
