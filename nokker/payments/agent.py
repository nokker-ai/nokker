"""Côté noyau de l'agent de paiement (docs/PAY-AGENT.md), drapeau `pay_agent`, éteint par défaut.

Le noyau ne paie rien et ne voit aucun moyen de paiement. Il :
- choisit, quand la carte « à payer » est posée, si l'agent de paiement peut payer : vendeur dont le profil Stripe est
  connu (`sellers`, nom d'hôte → profil), source `browser`, moyen de paiement enregistré par l'approbateur. Sinon, la
  personne paie elle-même (lot p2/pay-mandates, inchangé) ;
- après le mandat signé, transmet l'ENVELOPPE à l'agent (`PayAgentClient.issue`), qui la revérifie seul ;
- relaie l'enregistrement du moyen de paiement (preuve passkey vérifiée par l'agent) et reçoit les rappels signés de
  l'agent (`verify_callback`) : 3-D Secure à faire, jeton utilisé, désactivé.

Le numéro du SPT (`spt_…`) n'entre jamais dans le contexte du modèle ni dans les événements ; la remise au vendeur
(ACP) n'est PAS faite dans ce lot (docs/PAY-AGENT.md §8).

Émetteur `wise` (`AgentSettings.issuer`, défaut ; docs/PAY-AGENT.md §10) : carte virtuelle au nom de la personne,
réservée à UN achat chez n'importe quel marchand https (hors `deny_hosts`) ; défi 3-D Secure confirmé par la passkey
de la personne (`/mandates/{md}/3ds`). Ni numéro de carte, ni référence de carte ou de coffre côté noyau.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

logger = logging.getLogger("nokker.payments.agent")

TOKEN_ENV = "NOKKER_PAY_AGENT_TOKEN"            # jeton porteur du noyau vers l'agent
CALLBACK_SECRET_ENV = "NOKKER_PAY_AGENT_CALLBACK_SECRET"  # HMAC des rappels de l'agent vers le noyau
CALLBACK_PATH = "/v1/payments/agent/events"
CALLBACK_TOLERANCE_S = 300

# Nom du marchand recopié dans un texte du noyau ou une carte : `inline` (une ligne, neutralisé), 60 car. au plus. Il
# vient d'une carte que le modèle a préparée depuis une page : un faux nom tromperait le modèle ou la personne.
MERCHANT_MAX = 60

# ── textes vus par le modèle (NOUVEAUX, drapeau `pay_agent` allumé seulement) ──
PAY_DESCRIPTION_AGENT = ("Arrêt au paiement : prépare une carte « à payer » (marchand, montant, devise, articles, "
                         "lien) ; la personne approuve avec sa passkey. Si le marchand l'accepte, l'agent de paiement "
                         "de Nokker émet un jeton de paiement borné avec le moyen enregistré par la personne ; sinon "
                         "elle paie elle-même. Ne saisis jamais de numéro de carte.")
AGENT_ISSUED_TEXT = ("Mandat {mid} signé : l'agent de paiement a émis un jeton de paiement à usage unique pour "
                     "{merchant}, au plus {amount}, valable jusqu'au {until}. Rien n'est encore payé. Ne saisis aucun "
                     "moyen de paiement et ne touche plus au paiement.")
AGENT_ACTION_TEXT = ("Mandat {mid} signé : l'agent de paiement a émis un jeton de paiement pour {merchant}, au plus "
                     "{amount}, mais la banque demande une confirmation (3-D Secure) : la personne la fait dans "
                     "Nokker. Rien n'est encore payé. Ne touche plus au paiement.")
AGENT_FAILED_TEXT = ("Mandat {mid} signé, mais l'agent de paiement n'a pas émis de jeton ({why}). {how} "
                     "Ne touche plus au paiement.")
# Motif de la carte « à payer » quand l'agent de paiement paiera (remplace TO_PAY_REASON, qui dit « l'agent ne paie
# jamais »). Vu par le modèle (« EN ATTENTE D'APPROBATION HUMAINE (…) ») et par la personne sur la carte.
TO_PAY_AGENT_REASON = ("à payer : {amount} chez {merchant}. La personne approuve avec sa passkey ; l'agent de paiement "
                       "émet alors un jeton de paiement borné.")
# Refus d'un second pay_request pour un panier qui a déjà un mandat actif (dans « REFUSÉ par le noyau (…) »).
DUPLICATE_RULE = "payments:duplicate"
DUPLICATE_REASON = "ce panier a déjà un mandat actif ({mid}) : ne rappelle pas pay_request pour lui."
AGENT_FAILED_WHY = {"no_method": "aucun moyen de paiement enregistré", "seller": "marchand non accepté",
                    "amount": "montant au-delà du plafond", "currency": "devise non acceptée",
                    "expired": "mandat expiré", "stripe": "refus de Stripe", "unreachable": "service indisponible",
                    # émetteur `wise` (rail `issuer_card`) — NOUVEAUX, vus seulement quand il est choisi.
                    # `merchant` : validé, réservé à un marchand différent de celui approuvé ; aucun chemin ne le rend
                    # encore (un bloc d'un autre hôte n'est jamais scellé, docs/PAY-AGENT.md §10.6). Liste de refus de
                    # l'agent : `seller`, libellé déjà validé.
                    "issuer": "refus de l'émetteur de la carte", "merchant": "marchand différent de celui approuvé"}

# ── émetteur `wise` (rail `issuer_card`, docs/PAY-AGENT.md §10) : textes vus par le modèle, NOUVEAUX, drapeau
# `pay_agent` allumé ET `pay_agent_settings.issuer` = wise seulement. Ils remplacent PAY_DESCRIPTION_AGENT,
# TO_PAY_AGENT_REASON et AGENT_ISSUED_TEXT/AGENT_ACTION_TEXT, qui parlent d'un jeton de paiement. ──
PAY_DESCRIPTION_CARD = ("Arrêt au paiement : prépare une carte « à payer » (marchand, montant, devise, articles, "
                        "lien) ; la personne approuve avec sa passkey. L'agent de paiement de Nokker émet alors une "
                        "carte réservée à cet achat, plafonnée au montant approuvé, et paie lui-même chez ce marchand ; "
                        "s'il ne peut pas, la personne paie elle-même. Ne saisis jamais de numéro de carte.")
TO_PAY_CARD_REASON = ("à payer : {amount} chez {merchant}. La personne approuve avec sa passkey ; l'agent de paiement "
                      "émet alors une carte réservée à cet achat, plafonnée à ce montant, et paie lui-même.")
AGENT_CARD_TEXT = ("Mandat {mid} signé : l'agent de paiement a émis une carte réservée à cet achat chez {merchant}, "
                   "plafonnée à {amount}, et va payer lui-même. Si la banque demande une confirmation (3-D Secure), la "
                   "personne la valide dans Nokker. Rien n'est encore payé. Ne saisis aucun moyen de paiement et ne "
                   "touche plus au paiement.")


# ── émetteur `own_card` (Lokkal, docs/PAY-AGENT.md §11) : textes vus par le modèle, NOUVEAUX, validés mot pour mot
# le 03.11 (drapeau `pay_agent` + `pay_agent_settings.issuer` = own_card seulement). La personne paie avec
# SA carte, par son agent de paiement : Nokker n'utilise pas la carte pour son propre compte. ──
PAY_DESCRIPTION_OWN = ("Arrêt au paiement : prépare une carte « à payer » (marchand, montant, devise, articles, lien) ; "
                       "la personne approuve avec sa passkey. Elle paie alors avec sa propre carte : son agent de "
                       "paiement effectue le paiement chez ce marchand, au plus le montant approuvé ; s'il ne peut pas, "
                       "la personne paie elle-même. Ne saisis jamais de numéro de carte ni de code.")
# Variante, drapeau `pay_tool_choice` allumé (validée mot pour mot) : l'outil EST la confirmation.
PAY_DESCRIPTION_OWN_CHOICE = ("Arrêt au paiement : dès qu'une demande mène à payer (commande, achat, facture, "
                              "abonnement), appelle cet outil au lieu de répondre en texte ou de demander une "
                              "confirmation : la carte EST la confirmation. Prépare une carte « à payer » (marchand, "
                              "montant, devise, articles, lien) ; la personne approuve avec sa passkey. Elle paie alors "
                              "avec sa propre carte : son agent de paiement effectue le paiement chez ce marchand, au "
                              "plus le montant approuvé ; s'il ne peut pas, la personne paie elle-même. Ne saisis jamais "
                              "de numéro de carte ni de code, et ne confie jamais un paiement à un sous-agent.")
TO_PAY_OWN_REASON = ("à payer : {amount} chez {merchant}. La personne approuve avec sa passkey ; elle paie alors avec sa "
                     "propre carte, via son agent de paiement, au plus ce montant.")
AGENT_OWN_TEXT = ("Mandat {mid} signé : la personne paie {merchant} avec sa propre carte, au plus {amount} ; son agent de "
                  "paiement effectue le paiement. Si sa banque demande un code ou une confirmation, la personne le fait "
                  "dans Nokker ou dans l'appli de sa banque, hors de cette conversation. Rien n'est encore payé. Ne "
                  "saisis aucun moyen de paiement ni aucun code, et ne touche plus au paiement.")


class AgentUnavailable(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


ISSUERS = ("wise", "stripe_spt", "own_card")


@dataclass(frozen=True)
class AgentSettings:
    url: str = ""                                   # http://pay-agent:8790 (réseau interne)
    # Émetteur : `wise` (carte virtuelle au nom de la personne, tout marchand, docs/PAY-AGENT.md §10) ou `stripe_spt`
    # (Shared Payment Token, vendeurs Stripe connus seulement). Défaut : wise (décision produit du 02.11.2026).
    issuer: str = "wise"
    sellers: dict[str, str] = field(default_factory=dict)  # stripe_spt : nom d'hôte → profil Stripe (profile_…)
    publishable_key: str = ""                       # stripe_spt : pk_… pour le Payment Element (publique)
    deny_hosts: tuple[str, ...] = ()                # wise : hôtes de marchands où l'agent ne paie jamais (suffixes)
    link_return_url: str = ""                       # wise : retour du KYC hébergé par Wise (page Mon compte, https)
    test_http_hosts: tuple[str, ...] = ()           # own_card : marchand de TEST admis en http (jamais en production)

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> AgentSettings:
        raw = dict(raw or {})
        issuer = str(raw.get("issuer") or "wise")
        if issuer not in ISSUERS:
            raise ValueError(f"pay_agent_settings.issuer : {issuer!r} inconnu ({', '.join(ISSUERS)})")
        sellers = {str(k).lower().removeprefix("www."): str(v) for k, v in (raw.get("sellers") or {}).items()}
        deny = tuple(str(h).lower().strip(".") for h in (raw.get("deny_hosts") or ()) if str(h).strip("."))
        ret = str(raw.get("link_return_url") or "")
        if ret and not ret.startswith("https://"):
            raise ValueError("pay_agent_settings.link_return_url : https:// attendu")
        test_http = tuple(str(h).lower() for h in (raw.get("test_http_hosts") or ()) if str(h).strip())
        return cls(url=str(raw.get("url") or "").rstrip("/"), issuer=issuer, sellers=sellers,
                   publishable_key=str(raw.get("publishable_key") or ""), deny_hosts=deny, link_return_url=ret,
                   test_http_hosts=test_http)

    def seller_profile(self, url: str | None) -> str | None:
        host = (urlsplit(url).hostname or "").lower().removeprefix("www.") if url else ""
        return self.sellers.get(host) if host else None

    def card_host(self, url: str | None) -> str | None:
        """wise, own_card : l'hôte du marchand si l'agent peut y payer par carte (https, hors liste de refus), sinon None."""
        if not url or self.issuer not in ("wise", "own_card"):
            return None
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if not host or not (parts.scheme == "https" or (parts.scheme == "http" and host in self.test_http_hosts)):
            return None
        if any(host == d or host.endswith("." + d) for d in self.deny_hosts):
            return None
        return host


# (méthode, url, en-têtes, corps) -> (statut, JSON)
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, Any]]]


def _urllib(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


class PayAgentClient:
    def __init__(self, url: str, token: str, *, transport: Transport | None = None) -> None:
        self._url, self._token, self._t = url.rstrip("/"), token, transport or _urllib

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}"}
        raw = None
        if body is not None:
            raw = json.dumps(body, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        try:
            status, data = self._t(method, self._url + path, headers, raw)
        except (OSError, ValueError) as e:
            raise AgentUnavailable(503, "unreachable", f"agent de paiement injoignable : {type(e).__name__}") from None
        if status >= 400:
            err = (data or {}).get("error") or {}
            raise AgentUnavailable(status, str(err.get("code") or "error"), str(err.get("message") or status))
        return data

    def method(self, account: str) -> dict[str, Any] | None:
        return self._call("GET", f"/v1/methods/{quote(account, safe='')}").get("method")

    def register(self, account: str, payment_method: str, proof: dict[str, Any], *,
                 holder_name: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"payment_method": payment_method, "proof": proof}
        if holder_name is not None:  # émetteur wise : nom du titulaire des cartes
            body["holder_name"] = holder_name
        return self._call("PUT", f"/v1/methods/{quote(account, safe='')}", body).get("method")

    def remove(self, account: str) -> bool:
        return bool(self._call("DELETE", f"/v1/methods/{quote(account, safe='')}").get("removed"))

    def issue(self, account: str, envelope: dict[str, Any], *, kernel_ref: str = "") -> dict[str, Any]:
        """`kernel_ref` : référence opaque que l'agent rend dans ses rappels (le locataire du mandat côté noyau) ; elle
        ne sert à aucune décision de l'agent."""
        return self._call("POST", "/v1/tokens", {"account": account, "envelope": envelope, "kernel_ref": kernel_ref})

    def revoke(self, account: str, mandate_id: str) -> dict[str, Any]:
        return self._call("POST", f"/v1/tokens/{quote(mandate_id, safe='')}/revoke", {"account": account})

    def link(self, account: str) -> dict[str, Any] | None:
        """Émetteur wise : compte émetteur ouvert par l'agent (état du KYC chez Wise), ou None."""
        return self._call("GET", f"/v1/links/{quote(account, safe='')}").get("link")

    def link_start(self, account: str, person: dict[str, Any]) -> dict[str, Any] | None:
        return self._call("POST", f"/v1/links/{quote(account, safe='')}", {"person": person}).get("link")

    def link_url(self, account: str, redirect_url: str) -> dict[str, Any]:
        return self._call("POST", f"/v1/links/{quote(account, safe='')}/url", {"redirect_url": redirect_url})

    def balance(self, account: str) -> list[dict[str, Any]]:
        return self._call("GET", f"/v1/links/{quote(account, safe='')}/balance").get("balances") or []

    # ── own_card (Lokkal) : la carte de la personne dans le coffre local ; ces corps ne sont ni gardés ni journalisés ──
    def card(self, account: str) -> dict[str, Any] | None:
        return self._call("GET", f"/v1/cards/{quote(account, safe='')}").get("card")

    def put_card(self, account: str, card: dict[str, Any]) -> dict[str, Any] | None:
        return self._call("PUT", f"/v1/cards/{quote(account, safe='')}", card).get("card")

    def cvv_mode(self, account: str, keep: bool, cvv: Any = None) -> dict[str, Any]:
        return self._call("POST", f"/v1/cards/{quote(account, safe='')}/cvv-mode", {"keep": keep, "cvv": cvv})

    def provide(self, account: str, mandate_id: str, kind: str, value: Any) -> dict[str, Any]:
        return self._call("POST", f"/v1/tokens/{quote(mandate_id, safe='')}/{kind}", {"account": account, "value": value})

    def threeds(self, account: str, mandate_id: str) -> dict[str, Any]:
        """Défi 3-D Secure en attente (émetteur `wise`) : détails de l'émetteur et défi à signer, ou 404."""
        return self._call("GET", f"/v1/tokens/{quote(mandate_id, safe='')}/3ds?account={quote(account, safe='')}")

    def answer_threeds(self, account: str, mandate_id: str, *, challenge_id: str,
                       proof: dict[str, Any] | None, decline: bool = False) -> dict[str, Any]:
        """Réponse de la personne : preuve passkey sur le défi 3-D Secure (APPROVED), ou refus (REJECTED)."""
        return self._call("POST", f"/v1/tokens/{quote(mandate_id, safe='')}/3ds",
                          {"account": account, "challenge_id": challenge_id, "proof": proof, "decline": decline})


METHOD_AUD = "nokker-pay-method"


def method_challenge(account: str, payment_method: str, iat: int) -> str:
    """Défi que la passkey signe pour enregistrer un moyen de paiement. MÊME forme que
    `payagent.service.method_challenge` (test qui les compare) : l'agent le vérifie, le noyau ne fait que le poser."""
    return json.dumps({"aud": METHOD_AUD, "account": account, "payment_method": payment_method, "iat": iat},
                      sort_keys=True, separators=(",", ":"))


def verify_callback(body: bytes, header: str, secret: str, *, now: float | None = None) -> dict[str, Any]:
    """Rappel de l'agent : en-tête `X-Nokker-PayAgent-Signature: t=…,v1=…` (HMAC-SHA256 de « t.corps »). Lève
    `ValueError` si la signature, l'horodatage ou le corps ne tiennent pas."""
    if not secret:
        raise ValueError("secret des rappels absent")
    fields = dict(p.strip().partition("=")[::2] for p in (header or "").split(",") if "=" in p)
    try:
        ts = int(fields.get("t", ""))
    except ValueError:
        raise ValueError("signature illisible") from None
    if abs((now if now is not None else time.time()) - ts) > CALLBACK_TOLERANCE_S:
        raise ValueError("rappel trop ancien")
    want = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, fields.get("v1", "")):
        raise ValueError("signature invalide")
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("rappel illisible")
    return data


def agent_state(token: dict[str, Any]) -> dict[str, Any]:
    """Ce que le noyau garde d'un jeton ou d'une carte (colonne `pay_mandates.agent`, cartes du web) : jamais le
    numéro du SPT, de la carte ni du coffre. `kind` (card) et `threeds` (défi en attente) : émetteur `wise`."""
    return {k: token.get(k) for k in ("id", "kind", "status", "used", "currency", "max_amount_minor", "expires_at",
                                       "deactivated_reason", "threeds", "prompt") if k in token}


__all__ = ["AGENT_OWN_TEXT", "PAY_DESCRIPTION_OWN", "PAY_DESCRIPTION_OWN_CHOICE", "TO_PAY_OWN_REASON", "AGENT_CARD_TEXT", "ISSUERS", "PAY_DESCRIPTION_CARD", "TO_PAY_CARD_REASON", "DUPLICATE_REASON", "DUPLICATE_RULE", "MERCHANT_MAX", "TO_PAY_AGENT_REASON", "METHOD_AUD", "method_challenge", "AGENT_ACTION_TEXT", "AGENT_FAILED_TEXT", "AGENT_FAILED_WHY", "AGENT_ISSUED_TEXT", "CALLBACK_PATH",
           "CALLBACK_SECRET_ENV", "PAY_DESCRIPTION_AGENT", "TOKEN_ENV", "AgentSettings", "AgentUnavailable",
           "PayAgentClient", "agent_state", "verify_callback"]
