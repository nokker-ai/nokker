"""Navigateur de checkout de l'agent de paiement (docs/PAY-AGENT.md §10.5). Le modèle ne le pilote pas.

Il paie UNE carte émise pour UN mandat, sur la page de paiement du marchand du mandat :
1. vérifie que la page est sur l'hôte du mandat (`merchant_host`, ou un sous-domaine) en https ;
2. lit le total affiché : même devise, au plus le montant du mandat — sinon il ne remplit rien ;
3. remplit les champs carte avec les JETONS du coffre (le proxy du coffre les remplace en sortie, vers les hôtes
   autorisés seulement) et clique « Payer » ;
4. pendant un défi 3-D Secure (iframe de l'émetteur), il NE TOUCHE À RIEN : il attend que la page en sorte, au plus
   jusqu'à l'échéance du défi plus une marge, pendant que la personne confirme dans Nokker ;
5. constate succès, échec, ou passage de main (défi non délégué : code SMS à taper) ; capture l'écran de fin.

`CheckoutPage` est l'interface vers un vrai navigateur (lane dédié derrière le proxy du coffre) ; ce lot fournit la
logique et des faux. Les captures et journaux ne contiennent que des jetons.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from payagent.vault import CheckoutFields

# États qu'une page sait reconnaître (l'adaptateur du navigateur les déduit du DOM et de l'URL)
FORM, PROCESSING, CHALLENGE, OTP, SUCCESS, FAILURE = "form", "processing", "challenge", "otp", "success", "failure"


class CheckoutPage(Protocol):
    def url(self) -> str: ...

    def total(self) -> tuple[int, str] | None:
        """Total à payer affiché par le marchand (unités mineures, devise), ou None s'il est illisible."""

    def fill_card(self, fields: CheckoutFields) -> None: ...

    def submit(self) -> None: ...

    def state(self) -> str:
        """FORM, PROCESSING, CHALLENGE (iframe 3-D Secure), OTP (code à taper), SUCCESS ou FAILURE."""

    def screenshot(self) -> bytes: ...


@dataclass(frozen=True)
class CheckoutResult:
    outcome: str            # paid | failed | handoff | refused | timeout
    reason: str
    receipt_png: bytes | None = None


# Hôtes du marchand de TEST admis en http (deploy/lokkal/testshop, `PAYAGENT_TEST_HTTP_HOSTS`) ; vide par défaut :
# partout ailleurs, https seulement.
TEST_HTTP_HOSTS: set[str] = set()


def host_ok(url: str, merchant_host: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    scheme_ok = parts.scheme == "https" or (parts.scheme == "http" and host in TEST_HTTP_HOSTS)
    return scheme_ok and bool(host) and (host == merchant_host or host.endswith("." + merchant_host))


def precheck(page: CheckoutPage, *, merchant_host: str, max_amount_minor: int, currency: str) -> CheckoutResult | None:
    """Avant de demander quoi que ce soit au coffre : hôte du mandat, total affiché ≤ mandat, et pas de chiffrement
    de la carte dans le navigateur par le prestataire (CSE : la main revient à la personne, sans contournement)."""
    if not host_ok(page.url(), merchant_host):
        return CheckoutResult("refused", "page hors de l'hôte du marchand du mandat")
    total = page.total()
    if total is None:
        return CheckoutResult("refused", "total illisible")
    amount, cur = total
    if cur != currency or not 0 < amount <= max_amount_minor:
        return CheckoutResult("refused", "total différent du mandat")
    cse = getattr(page, "client_side_encryption", None)
    if callable(cse) and cse():
        return CheckoutResult("handoff", "le prestataire chiffre la carte dans le navigateur : la personne paie")
    return None


def run_checkout(page: CheckoutPage, *, fields: CheckoutFields, merchant_host: str, max_amount_minor: int,
                 currency: str, challenge_deadline: Callable[[], float | None],
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 settle_s: float = 120.0, poll_s: float = 1.0, margin_s: float = 30.0,
                 ask_otp: Callable[[], str | None] | None = None,
                 on_bank_app: Callable[[], None] | None = None) -> CheckoutResult:
    """`challenge_deadline()` : échéance du défi 3-D Secure délégué en attente (émetteur wise), ou None.
    own_card : `ask_otp()` rend le code de la banque tapé par la personne dans Nokker (ou None : délai passé) — le
    COMPOSANT le saisit dans le défi, jamais le modèle ; `on_bank_app()` dit une fois « confirme dans l'appli de ta
    banque » pendant que le navigateur attend sans rien toucher (échéance `bank_app_s`)."""
    hop = getattr(page, "to_checkout", None)
    if callable(hop):
        hop()  # lien du panier : vers la page de paiement, même hôte (vérifié par precheck juste après)
    pre = precheck(page, merchant_host=merchant_host, max_amount_minor=max_amount_minor, currency=currency)
    if pre is not None:
        return pre
    if clock() >= fields.expires_at:
        return CheckoutResult("refused", "jetons du coffre expirés")
    page.fill_card(fields)
    page.submit()
    deadline = clock() + settle_s
    told_app = otp_done = False
    while True:
        st = page.state()
        if not host_ok(page.url(), merchant_host) and st not in (CHALLENGE, PROCESSING, SUCCESS, FAILURE):
            # redirection hors du marchand qui n'est ni un défi ni une fin connue : on n'agit plus
            return CheckoutResult("handoff", "page inattendue après le paiement", _shot(page))
        if st == SUCCESS:
            return CheckoutResult("paid", "paiement confirmé par le marchand", _shot(page))
        if st == FAILURE:
            return CheckoutResult("failed", "paiement refusé", _shot(page))
        if st == OTP:
            fill = getattr(page, "fill_otp", None)
            if ask_otp is None or otp_done or not callable(fill):
                # wise, ou code déjà donné une fois : l'agent ne tape pas de code, la personne prend la main
                return CheckoutResult("handoff", "la banque demande un code : la personne prend la main", _shot(page))
            code = ask_otp()  # attend la personne (bandeau « Entre le code reçu de ta banque »), jamais le modèle
            if not code:
                return CheckoutResult("handoff", "code de la banque non donné à temps : la personne prend la main",
                                      _shot(page))
            fill(code)
            otp_done = True
            deadline = clock() + settle_s
            sleep(poll_s)
            continue
        if st == CHALLENGE:
            # attente passive : on recule l'échéance tant que le défi délégué court
            due = challenge_deadline()
            if due is not None:
                deadline = max(deadline, due + margin_s)
            elif on_bank_app is not None:
                if not told_app:  # confirmation dans l'appli de la banque : on attend sans rien toucher
                    on_bank_app()
                    told_app = True
                    deadline = max(deadline, clock() + 600)
        if clock() >= deadline:
            return CheckoutResult("timeout", "pas de réponse du marchand dans le délai", _shot(page))
        sleep(poll_s)


def _shot(page: CheckoutPage) -> bytes | None:
    try:
        return page.screenshot()
    except Exception:
        return None


__all__ = ["TEST_HTTP_HOSTS", "CHALLENGE", "FAILURE", "FORM", "OTP", "PROCESSING", "SUCCESS", "CheckoutPage", "CheckoutResult",
           "host_ok", "precheck", "run_checkout"]
