"""Émetteurs de l'agent de paiement (docs/PAY-AGENT.md §10) : une interface, un adaptateur par fournisseur.

- `wise` (`issuers/wise.py`) : carte virtuelle au nom de la personne, émise par Wise Platform (modèle Embedded : KYC et
  fonds chez Wise). Valable chez tout marchand ; 3-D Secure délégué par webhook ; PAN remis au coffre, jamais à nous.
- `stripe_spt` (`issuers/stripe_spt.py`) : Shared Payment Tokens de Stripe (vendeur chez Stripe seulement). Gardé tel
  quel ; ce n'est PAS un `Issuer` de cartes, `PayAgent` garde son chemin propre pour lui.

Aucune méthode ne rend un PAN, un CVV ou une date d'expiration : `issue_card` demande à l'émetteur de remettre les
données sensibles au coffre (`payagent/vault.py`), qui rend des jetons au format carte.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class IssuerError(Exception):
    """Refus ou panne de l'émetteur. `status` HTTP de l'émetteur (0 = injoignable)."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class IssuedCard:
    card_ref: str           # identifiant de la carte chez l'émetteur (Wise : `cardToken`) ; jamais le PAN
    status: str             # statut chez l'émetteur (Wise : ACTIVE, FROZEN, BLOCKED…)
    last4: str | None = None
    raw_status: dict[str, Any] = field(default_factory=dict)
    vault_card: Any = None  # payagent.vault.VaultCard rendue par le coffre à l'émission (références opaques)


@dataclass(frozen=True)
class Balance:
    currency: str
    amount_minor: int


@dataclass(frozen=True)
class CardTransaction:
    ref: str
    status: str             # tel que l'émetteur le dit (Wise : IN_PROGRESS, COMPLETED, DECLINED…)
    amount_minor: int
    currency: str
    merchant: str
    created_at: str


@dataclass(frozen=True)
class ThreeDSChallenge:
    """Défi 3-D Secure délégué, tel que l'émetteur l'envoie (champs recopiés, rien de déduit)."""

    challenge_ref: str      # identifiant du défi chez l'émetteur (rendu tel quel dans la réponse)
    card_ref: str
    profile_ref: str
    amount_minor: int
    currency: str
    merchant_name: str
    merchant_url: str | None
    merchant_country: str | None
    expires_at: float       # échéance absolue (horloge de l'agent) : au-delà, la réponse ne sert plus
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IssuerEvent:
    kind: str               # "3ds_challenge" | "card_status" | "transaction" | "ignored"
    event_id: str
    challenge: ThreeDSChallenge | None = None
    card_ref: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


class Issuer(Protocol):
    name: str

    def check_profile(self, profile_ref: str) -> dict[str, Any]:
        """Le compte de la personne chez l'émetteur existe et peut recevoir une carte ; rend ce qui s'affiche
        (devise par défaut, état du KYC…), sans donnée sensible."""

    def issue_card(self, *, profile_ref: str, currency: str, limit_minor: int, idempotency_key: str,
                   vault: Any) -> IssuedCard:
        """Émet (ou attribue) une carte virtuelle dont la limite LIFETIME vaut `limit_minor` dans `currency`, achats
        en ligne seulement. Les données sensibles vont au coffre `vault`, jamais dans la valeur rendue."""

    def block_card(self, *, profile_ref: str, card_ref: str) -> None:
        """Bloque la carte de façon définitive (après l'achat, à l'échéance, sur révocation)."""

    def card_status(self, *, profile_ref: str, card_ref: str) -> IssuedCard: ...

    def balances(self, *, profile_ref: str) -> list[Balance]: ...

    def transactions(self, *, profile_ref: str, card_ref: str) -> list[CardTransaction]: ...

    def parse_webhook(self, payload: bytes, headers: Any) -> IssuerEvent:
        """Vérifie la signature de l'émetteur et rend l'événement. Lève `ValueError` si elle ne tient pas."""

    def challenge_result(self, *, challenge: ThreeDSChallenge, approved: bool) -> None:
        """Réponse au défi 3-D Secure (APPROVED / REJECTED), dans le délai de l'émetteur."""


__all__ = ["Balance", "CardTransaction", "IssuedCard", "Issuer", "IssuerError", "IssuerEvent", "ThreeDSChallenge"]
