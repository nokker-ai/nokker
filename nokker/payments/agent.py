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
                    "expired": "mandat expiré", "stripe": "refus de Stripe", "unreachable": "service indisponible"}


class AgentUnavailable(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class AgentSettings:
    url: str = ""                                   # http://pay-agent:8790 (réseau interne)
    sellers: dict[str, str] = field(default_factory=dict)  # nom d'hôte → profil Stripe (profile_…)
    publishable_key: str = ""                       # pk_… pour le Payment Element (publique)

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> AgentSettings:
        raw = dict(raw or {})
        sellers = {str(k).lower().removeprefix("www."): str(v) for k, v in (raw.get("sellers") or {}).items()}
        return cls(url=str(raw.get("url") or "").rstrip("/"), sellers=sellers,
                   publishable_key=str(raw.get("publishable_key") or ""))

    def seller_profile(self, url: str | None) -> str | None:
        host = (urlsplit(url).hostname or "").lower().removeprefix("www.") if url else ""
        return self.sellers.get(host) if host else None


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

    def register(self, account: str, payment_method: str, proof: dict[str, Any]) -> dict[str, Any]:
        return self._call("PUT", f"/v1/methods/{quote(account, safe='')}",
                          {"payment_method": payment_method, "proof": proof}).get("method")

    def remove(self, account: str) -> bool:
        return bool(self._call("DELETE", f"/v1/methods/{quote(account, safe='')}").get("removed"))

    def issue(self, account: str, envelope: dict[str, Any], *, kernel_ref: str = "") -> dict[str, Any]:
        """`kernel_ref` : référence opaque que l'agent rend dans ses rappels (le locataire du mandat côté noyau) ; elle
        ne sert à aucune décision de l'agent."""
        return self._call("POST", "/v1/tokens", {"account": account, "envelope": envelope, "kernel_ref": kernel_ref})

    def revoke(self, account: str, mandate_id: str) -> dict[str, Any]:
        return self._call("POST", f"/v1/tokens/{quote(mandate_id, safe='')}/revoke", {"account": account})


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
    """Ce que le noyau garde d'un jeton (colonne `pay_mandates.agent`, cartes du web) : jamais le numéro du SPT."""
    return {k: token.get(k) for k in ("id", "status", "used", "currency", "max_amount_minor", "expires_at",
                                       "deactivated_reason") if k in token}


__all__ = ["DUPLICATE_REASON", "DUPLICATE_RULE", "MERCHANT_MAX", "TO_PAY_AGENT_REASON", "METHOD_AUD", "method_challenge", "AGENT_ACTION_TEXT", "AGENT_FAILED_TEXT", "AGENT_FAILED_WHY", "AGENT_ISSUED_TEXT", "CALLBACK_PATH",
           "CALLBACK_SECRET_ENV", "PAY_DESCRIPTION_AGENT", "TOKEN_ENV", "AgentSettings", "AgentUnavailable",
           "PayAgentClient", "agent_state", "verify_callback"]
