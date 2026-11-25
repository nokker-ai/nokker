"""Coffre des données de carte (docs/PAY-AGENT.md §10.4) : le PAN, le CVV et la date d'expiration d'une carte émise
pour l'agent ne passent JAMAIS en clair par l'agent, le noyau, le modèle ni le navigateur.

Le coffre (sous-traitant PCI DSS) :
1. `capture_card` : appelle LUI-MÊME l'émetteur par son proxy sortant (clé de chiffrement, puis données sensibles),
   fabrique et déchiffre le JWE de l'émetteur, garde la carte, et rend des JETONS (références opaques) ;
2. `checkout_fields` : rend, pour UN checkout, des valeurs à taper dans le formulaire du marchand (jetons au format
   carte) que son proxy de navigation remplace par les vraies valeurs vers les seuls hôtes autorisés (marchand du
   mandat et son prestataire de paiement), pour une durée bornée ;
3. `forget` : efface la carte (après l'achat, à l'échéance, sur révocation).

Interface + un faux (tests) + l'adaptateur du fournisseur retenu, Evervault, en SQUELETTE (`EvervaultVault`), sans
appel réel : choix argumenté dans docs/PAY-AGENT.md §10.4 (seul proxy de navigation documenté qui substitue les jetons
vers des hôtes listés ; Basis Theory n'accepte pas de clé RSA et son « Browser Proxy » rejoue ou injecte le PAN dans le
navigateur). Contrat et accès à obtenir avant tout branchement.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol


class VaultError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class VaultCard:
    """Carte gardée par le coffre : références opaques seulement (aucun chiffre de la carte).

    `sealed` : valeurs chiffrées par le coffre (Evervault : `ev:encrypted:…`), que l'agent garde sans pouvoir les
    lire — Evervault ne stocke rien chez lui ; les effacer chez nous efface la carte."""

    vault_ref: str          # identifiant de la carte dans le coffre (ou chez nous si le coffre ne stocke pas)
    issuer: str
    card_ref: str           # identifiant chez l'émetteur (Wise : cardToken)
    sealed: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckoutFields:
    """Valeurs à taper dans le formulaire du marchand : JETONS, remplacés par le proxy du coffre en sortie vers
    `allowed_hosts` seulement, jusqu'à `expires_at`. Aucune n'est un vrai numéro."""

    number: str
    cvc: str
    expiry: str             # MM/AA (jeton ou valeur réelle si le coffre la juge non sensible ; cf. contrat)
    holder: str
    allowed_hosts: tuple[str, ...]
    expires_at: float
    proxy: dict[str, Any] = field(default_factory=dict)  # réglages du proxy de navigation (URL, CA) pour le lane


class Vault(Protocol):
    name: str

    def capture_card(self, *, issuer: str, card_ref: str, key_url: str, details_url: str,
                     headers: dict[str, str]) -> VaultCard: ...

    def checkout_fields(self, card: VaultCard, *, allowed_hosts: tuple[str, ...], ttl_s: int) -> CheckoutFields: ...

    def forget(self, card: VaultCard) -> None: ...


class FakeVault:
    """Coffre de test : enregistre les appels, rend des jetons reconnaissables, ne voit aucun numéro."""

    name = "fake"

    def __init__(self, *, fail: VaultError | None = None, clock: Any = time.time) -> None:
        self.captures: list[dict[str, Any]] = []
        self.forgotten: list[str] = []
        self.cards: dict[str, VaultCard] = {}
        self.fail, self.clock = fail, clock
        self._lock = threading.Lock()

    def capture_card(self, *, issuer: str, card_ref: str, key_url: str, details_url: str,
                     headers: dict[str, str]) -> VaultCard:
        if self.fail is not None:
            raise self.fail
        with self._lock:
            self.captures.append({"issuer": issuer, "card_ref": card_ref, "key_url": key_url,
                                  "details_url": details_url, "headers": dict(headers)})
            c = VaultCard(vault_ref="vc_" + secrets.token_hex(6), issuer=issuer, card_ref=card_ref,
                          sealed={"number": "ev:encrypted:fake-number", "cvc": "ev:encrypted:fake-cvc"})
            self.cards[c.vault_ref] = c
            return c

    def checkout_fields(self, card: VaultCard, *, allowed_hosts: tuple[str, ...], ttl_s: int) -> CheckoutFields:
        if card.vault_ref not in self.cards:
            raise VaultError("unknown", "carte absente du coffre")
        return CheckoutFields(number=f"tok_num_{card.vault_ref}", cvc=f"tok_cvc_{card.vault_ref}",
                              expiry=f"tok_exp_{card.vault_ref}", holder="tok_holder",
                              allowed_hosts=allowed_hosts, expires_at=self.clock() + ttl_s)

    def forget(self, card: VaultCard) -> None:
        with self._lock:
            self.cards.pop(card.vault_ref, None)
            self.forgotten.append(card.vault_ref)


class EvervaultVault:
    """SQUELETTE de l'adaptateur Evervault (docs.evervault.com, lu le 02.11.2026). Aucun appel réel tant que le contrat
    n'est pas signé et les accès donnés : chaque méthode lève `VaultError("not_configured")`. Le plan :

    - `capture_card` : une Evervault Function (`function`) garde la clé AES de la requête, récupère la clé RSA de Wise
      (`key_url`), fabrique le JWE, appelle `details_url` avec `headers`, déchiffre la réponse et rend
      `context.encrypt(...)` : des valeurs `ev:encrypted:…` (numéro, CVC, expiration) gardées dans `VaultCard.sealed`.
      Recette assemblée de briques documentées, pas une recette Evervault (à valider avec eux).
    - `checkout_fields` : `POST https://api.evervault.com/browser-tokens` (basic `app_id:api_key`) avec `data[]`
      `{category: card.number|card.cvv, value: ev:encrypted…, id}`, `permissions[]` `{action: "decrypt", target: hôte}`
      par hôte autorisé, `ttl` en secondes ; réponse `data[].value` (jetons Luhn, BIN gardé) et `proxyConfiguration`
      `{hostname, port, username, password}` pour le lane (proxy CONNECT `eu-ie.browser.relay.evervault.app`, CA de
      https://ca.evervault.com). Pas de révocation documentée : `ttl` court.
    - `forget` : rien à appeler chez Evervault (il ne stocke rien) ; l'agent efface `sealed` de sa base.
    """

    name = "evervault"

    def __init__(self, *, app_id: str = "", api_key: str = "", function: str = "",
                 browser_proxy: str = "eu-ie.browser.relay.evervault.app") -> None:
        self._app, self._key, self._function, self._proxy = app_id, api_key, function, browser_proxy

    def _todo(self, what: str) -> VaultError:
        return VaultError("not_configured", f"coffre Evervault : {what} pas encore branché (contrat et accès à obtenir)")

    def capture_card(self, *, issuer: str, card_ref: str, key_url: str, details_url: str,
                     headers: dict[str, str]) -> VaultCard:
        raise self._todo("capture par Function")

    def checkout_fields(self, card: VaultCard, *, allowed_hosts: tuple[str, ...], ttl_s: int) -> CheckoutFields:
        raise self._todo("Browser Tokens")

    def forget(self, card: VaultCard) -> None:
        return None  # rien chez Evervault : l'agent efface `sealed` de sa base


__all__ = ["CheckoutFields", "EvervaultVault", "FakeVault", "Vault", "VaultCard", "VaultError"]
