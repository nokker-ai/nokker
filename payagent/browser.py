"""Navigateur de checkout DÉDIÉ de l'agent de paiement (docs/PAY-AGENT.md §11) : Playwright sur un Chromium du compose
(profil `pay`, joint par CDP), jamais le navigateur du modèle. Implémente `payagent.checkout.CheckoutPage`.

Repérage générique, dans la page et ses cadres de même origine :
- champs de carte par `autocomplete` (cc-number, cc-exp, cc-exp-month, cc-exp-year, cc-csc, cc-name), sinon par nom
  ou identifiant (card, cvc, cvv, exp…) ;
- total : `[data-total-minor][data-currency]` si le marchand le donne, sinon « Total/Montant/Summe/Totale … CHF 42.90 » ;
- CSE (chiffrement de la carte dans le navigateur par le prestataire) : `data-encrypted-name`, ou champs de carte
  seulement dans une iframe d'un prestataire connu → la main revient à la personne, sans contournement ;
- états : succès (« merci », « paiement accepté », « order confirmed »… ou une URL success/confirmation/thank), refus,
  code à taper (champ one-time-code / otp / code SMS, y compris dans l'iframe du défi), défi en cours (iframe
  3ds/acs/challenge sans champ de code), sinon formulaire ou traitement.
Aucune valeur de carte n'est journalisée ; les captures servent de reçu à l'agent seul.
"""
from __future__ import annotations

import re
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from payagent.checkout import CHALLENGE, FAILURE, FORM, OTP, PROCESSING, SUCCESS
from payagent.vault import CheckoutFields

_SUCCESS = re.compile(r"\b(merci|paiement accept|commande confirm|order confirmed|thank you|payment (successful|"
                      r"accepted)|vielen dank|zahlung erfolgreich|grazie|pagamento riuscito)", re.I)
_SUCCESS_URL = re.compile(r"(success|confirmation|thank|merci|danke)", re.I)
_FAILURE = re.compile(r"\b(paiement refus|payment (declined|failed)|carte refus|card declined|zahlung abgelehnt|"
                      r"pagamento rifiutato)", re.I)
_TOTAL = re.compile(r"(total|montant|summe|totale|betrag)[^\d\n]{0,40}?(CHF|EUR|€|Fr\.?)\s*([\d'’ .,]+\d)", re.I)
_CHALLENGE_FRAME = re.compile(r"(3ds|acs|challenge|secure|authenticat)", re.I)
# Panier → page de paiement (un saut) : lien ou bouton dont le texte dit « payer », sur le MÊME hôte seulement.
_TO_PAY = re.compile(r"(passer au paiement|proc[ée]der au paiement|^\s*payer\b|checkout|proceed to (payment|checkout)|"
                     r"zur kasse|zahlung|vai al pagamento|procedi al pagamento)", re.I)
_OTP_SEL = ('input[autocomplete="one-time-code"], input[name*="otp" i], input[id*="otp" i], '
            'input[name*="sms" i], input[name*="tan" i]')

CARD_SELECTORS = {
    "number": 'input[autocomplete="cc-number"], input[name*="cardnumber" i], input[name*="card_number" i], '
              'input[id*="cardnumber" i], input[name="number"]',
    "exp": 'input[autocomplete="cc-exp"], input[name*="expiry" i], input[name="exp"], input[id*="expiry" i]',
    "exp_month": 'input[autocomplete="cc-exp-month"], select[autocomplete="cc-exp-month"]',
    "exp_year": 'input[autocomplete="cc-exp-year"], select[autocomplete="cc-exp-year"]',
    "cvc": 'input[autocomplete="cc-csc"], input[name*="cvc" i], input[name*="cvv" i], input[name*="csc" i]',
    "holder": 'input[autocomplete="cc-name"], input[name*="ccname" i], input[name*="cardholder" i]',
}


def _currency(sym: str) -> str:
    s = sym.upper().rstrip(".")
    return {"€": "EUR", "FR": "CHF"}.get(s, s)


def _minor(text: str) -> int | None:
    t = text.replace("'", "").replace("’", "").replace(" ", "")
    if "," in t and "." in t:
        t = t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".")
    try:
        return int(round(float(t) * 100))
    except ValueError:
        return None


class PlaywrightCheckoutPage:
    """Une page de checkout dans le navigateur dédié. `close()` ferme le contexte (et la connexion CDP)."""

    def __init__(self, page: Any, on_close: Callable[[], None] | None = None) -> None:
        self.page, self._on_close = page, on_close

    # ── lecture ──
    def url(self) -> str:
        return self.page.url

    def _frames(self) -> list[Any]:
        return list(self.page.frames)

    def total(self) -> tuple[int, str] | None:
        el = self.page.query_selector("[data-total-minor][data-currency]")
        if el is not None:
            try:
                return int(el.get_attribute("data-total-minor") or ""), (el.get_attribute("data-currency") or "").upper()
            except ValueError:
                pass
        m = _TOTAL.search(self.page.inner_text("body") or "")
        if not m:
            return None
        minor = _minor(m.group(3))
        return (minor, _currency(m.group(2))) if minor else None

    def client_side_encryption(self) -> bool:
        if self.page.query_selector("[data-encrypted-name]") is not None:
            return True
        # champs de carte seulement dans l'iframe d'un prestataire connu (champs hébergés) : pas de remplissage
        if self._find("number") is None:
            from nokker.payments.cardmask import PSP_FRAME_HOSTS

            return any(any(h in (f.url or "") for h in PSP_FRAME_HOSTS) for f in self._frames()[1:])
        return False

    def _find(self, kind: str) -> Any:
        sel = CARD_SELECTORS[kind]
        for f in self._frames():
            try:
                el = f.query_selector(sel)
            except Exception:  # noqa: BLE001 — cadre détaché
                continue
            if el is not None and el.is_visible():
                return el
        return None

    # ── actions du composant (jamais du modèle) ──
    def to_checkout(self) -> bool:
        """Lien du panier (celui de `pay_request`) : s'il n'y a pas encore de champ de carte, suit UNE fois le lien ou
        bouton « passer au paiement » du même hôte. Rend True s'il a navigué. Les vérifications (hôte, total) se font
        ensuite sur la page de paiement elle-même."""
        if self._find("number") is not None:
            return False
        host = urlsplit(self.page.url).hostname
        for el in self.page.query_selector_all("a[href], button, input[type=submit]"):
            try:
                text = (el.inner_text() or el.get_attribute("value") or "").strip()
                if not text or not _TO_PAY.search(text) or not el.is_visible():
                    continue
                href = el.get_attribute("href")
                if href and urlsplit(urljoin(self.page.url, href)).hostname != host:
                    continue
                el.click()
                self.page.wait_for_load_state("domcontentloaded")
                return True
            except Exception:  # noqa: BLE001 — élément détaché : on essaie le suivant
                continue
        return False

    def fill_card(self, fields: CheckoutFields) -> None:
        number = self._find("number")
        if number is None:
            raise RuntimeError("champ du numéro introuvable")
        number.fill(fields.number)
        month, _, year = fields.expiry.partition("/")
        exp = self._find("exp")
        if exp is not None:
            exp.fill(f"{month}/{year[-2:]}")
        else:
            for kind, value in (("exp_month", month), ("exp_year", year)):
                el = self._find(kind)
                if el is None:
                    continue
                if el.evaluate("e => e.tagName") == "SELECT":
                    el.select_option(value=[value, value[-2:], str(int(value))])
                else:
                    el.fill(value)
        cvc = self._find("cvc")
        if cvc is not None:
            cvc.fill(fields.cvc)
        holder = self._find("holder")
        if holder is not None and fields.holder:
            holder.fill(fields.holder)

    def submit(self) -> None:
        number = self._find("number")
        form_btn = None
        if number is not None:
            form_btn = number.evaluate_handle(
                "e => { const f = e.form; if (!f) return null; return f.querySelector('button[type=submit], "
                "input[type=submit], button:not([type])'); }").as_element()
        (form_btn or self.page.get_by_role("button", name=re.compile(r"pay|payer|bezahlen|paga", re.I)).first).click()

    def _otp_input(self) -> Any:
        for f in self._frames():
            try:
                el = f.query_selector(_OTP_SEL)
            except Exception:  # noqa: BLE001
                continue
            if el is not None and el.is_visible():
                return el
        return None

    def fill_otp(self, code: str) -> None:
        el = self._otp_input()
        if el is None:
            raise RuntimeError("champ du code introuvable")
        el.fill(code)
        btn = el.evaluate_handle("e => e.form && e.form.querySelector('button[type=submit], input[type=submit], "
                                 "button:not([type])')").as_element()
        if btn is not None:
            btn.click()
        else:
            el.press("Enter")

    def state(self) -> str:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:  # noqa: BLE001 — navigation en cours
            return PROCESSING
        text = ""
        try:
            text = self.page.inner_text("body", timeout=2000) or ""
        except Exception:  # noqa: BLE001
            return PROCESSING
        if _SUCCESS.search(text) or _SUCCESS_URL.search(self.page.url.split("?", 1)[0]):
            return SUCCESS
        if _FAILURE.search(text):
            return FAILURE
        if self._otp_input() is not None:
            return OTP
        frames = self._frames()[1:]
        if any(_CHALLENGE_FRAME.search((f.name or "") + " " + (f.url or "")) for f in frames):
            return CHALLENGE
        return FORM if self._find("number") is not None else PROCESSING

    def screenshot(self) -> bytes:
        return self.page.screenshot(full_page=True)

    def close(self) -> None:
        if self._on_close is not None:
            self._on_close()


def cdp_by_ip(cdp_url: str) -> str:
    """Chromium refuse un en-tête Host qui n'est ni une IP ni `localhost` sur son point CDP (anti DNS rebinding) :
    `http://paychrome:9223` est donc résolu en `http://<ip>:9223` avant de s'y joindre."""
    parts = urlsplit(cdp_url)
    host = parts.hostname or ""
    if not host or host == "localhost" or re.fullmatch(r"[0-9.]+|\[?[0-9a-fA-F:]+\]?", host):
        return cdp_url
    ip = socket.getaddrinfo(host, parts.port or 80, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    return urlunsplit((parts.scheme, f"{ip}:{parts.port}" if parts.port else ip, parts.path, parts.query, ""))


def cdp_page_opener(cdp_url: str, *, timeout_ms: int = 30000) -> Callable[[str], PlaywrightCheckoutPage]:
    """`open_page(url)` pour l'agent : se joint au Chromium dédié (CDP), ouvre un contexte neuf (aucune session d'un
    autre paiement), charge `url`. Appelé dans le fil du checkout ; tout est fermé par `page.close()`."""

    def open_page(url: str) -> PlaywrightCheckoutPage:
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        try:
            browser = pw.chromium.connect_over_cdp(cdp_by_ip(cdp_url), timeout=timeout_ms)
            ctx = browser.new_context(locale="fr-CH")
            page = ctx.new_page()
            page.set_default_timeout(timeout_ms)
            page.goto(url, wait_until="domcontentloaded")
        except Exception:
            pw.stop()
            raise

        def close() -> None:
            try:
                ctx.close()
            finally:
                pw.stop()

        return PlaywrightCheckoutPage(page, on_close=close)

    return open_page


def launched_page_opener(*, headless: bool = True, args: list[str] | None = None) -> Callable[[str], PlaywrightCheckoutPage]:
    """Variante tests et développement : lance un Chromium local au lieu de se joindre au service du compose."""

    def open_page(url: str) -> PlaywrightCheckoutPage:
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=headless, args=args or [])
        page = browser.new_page(locale="fr-CH")
        page.set_default_timeout(15000)
        page.goto(url, wait_until="domcontentloaded")

        def close() -> None:
            try:
                browser.close()
            finally:
                pw.stop()

        return PlaywrightCheckoutPage(page, on_close=close)

    return open_page


__all__ = ["CARD_SELECTORS", "PlaywrightCheckoutPage", "cdp_page_opener", "launched_page_opener"]
