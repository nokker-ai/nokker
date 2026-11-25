"""Données de carte masquées pour le modèle (docs/PAYMENTS.md §2 bis) — pur, sans état ni réseau.

Le remplissage d'un paiement appartient à l'agent de paiement (service séparé), jamais aux outils `browser_*` du
modèle. Ce module garantit que, même si une page affiche ou contient une carte (formulaire rempli par la personne,
page de confirmation, numéro collé dans le chat), le numéro, le code de sécurité et la date d'expiration ne remontent
ni au modèle, ni au store, ni aux traces :

- `mask_card_text(text)` : (1) toute suite Luhn-valide de 12 à 19 chiffres (espaces ou tirets permis) a ses chiffres
  remplacés par « • » ; (2) sur une ligne d'instantané d'accessibilité dont le libellé est un champ de carte
  (`cc-number`, `cc-csc`, `cc-exp`, numéro, CVV/CVC, expiration…), la VALEUR du champ (après « : », ou `value="…"`)
  est remplacée par « •••• ». Aucun mot n'est ajouté : rien de nouveau n'est lu par le modèle.
- les champs de code à usage unique (`autocomplete=one-time-code`, « code SMS », « verification code »…) ont leur
  valeur masquée de la même façon ;
- `MASK_SCRIPT` / `UNMASK_SCRIPT` : avant une capture d'écran, les champs de carte (`autocomplete=cc-*`, noms
  card/cvc/cvv/expiry…) et les iframes des prestataires de paiement connus sont floutés et leur texte rendu
  illisible ; retirés après la capture (`nokker/tools/registry.py`).

Un faux positif (un numéro de commande Luhn-valide de 16 chiffres) est masqué aussi : c'est voulu, un chiffre masqué
coûte moins qu'un numéro de carte lu. Une seule exception : un UUID complet (8-4-4-4-12 hexadécimaux, borné par des
caractères non hexadécimaux) n'est jamais un numéro de carte et reste lisible (ses chiffres, passant parfois le test de
Luhn, défiguraient les identifiants d'approbation vus du modèle). Autre exception : les chiffres d'un nombre décimal, scientifique ou groupé par milliers
(`0.30000000000000004`, `-3.14e-05`, `1 234 567 890 123`, `nokker/redact/numbers.py`) ne sont pas un numéro de carte (un flottant
à 16 décimales renvoyé par un outil était masqué « 1.•••• » et l'outil relancé) ; un entier nu (`4242424242424242`) reste masqué,
et une suite collée à une lettre ou à un préfixe (`ref4111…`) n'est pas un nombre. Un numéro collé à un UUID reste masqué : l'UUID est
mis de côté, le texte de part et d'autre est masqué comme d'habitude.
"""
from __future__ import annotations

import re

from nokker.payments.detect import card_field
from nokker.redact.numbers import exempt_spans, inside, number_spans, parts

MASK_CHAR = "•"
VALUE_MASK = "••••"

# 12 à 19 chiffres, un espace ou un tiret permis entre deux chiffres ; pas collé à un autre chiffre. Le premier
# chiffre est celui d'un réseau de cartes (2 Mastercard, 3 Amex/JCB/Diners, 4 Visa, 5 Mastercard/Maestro, 6
# Discover/UnionPay/Maestro) : un horodatage en millisecondes (1…) ou un identifiant en 7, 8, 9 reste lisible.
_RUN = re.compile(r"(?<![\d])[2-6](?:[ \-]?\d){11,18}(?![\d])")
# Tête d'IBAN juste avant la suite : deux MAJUSCULES collées (`CH93…` : la suite part de « 93 »), ou suivies des deux
# chiffres de contrôle et d'une espace (`CH93 0076…`). « ma carte 4242… » n'en est pas une.
_IBAN_HEAD = re.compile(r"(?:^|[^A-Za-z])[A-Z]{2}(?:\d{2} ?)?$")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def mask_pans(text: str) -> str:
    """Chiffres de toute suite Luhn-valide de 12 à 19 chiffres remplacés par « • » (séparateurs gardés). Un IBAN
    (`CH93 0076 …`) n'est pas touché ici : la garde PII des traces le traite."""
    return _mask_pans(text)[0]


_HEX = "0-9A-Fa-f"
# UUID complet 8-4-4-4-12, borné par des non-hexadécimaux : jamais un numéro de carte (voir l'en-tête du module).
_UUID = re.compile(rf"(?<![{_HEX}])[{_HEX}]{{8}}-[{_HEX}]{{4}}-[{_HEX}]{{4}}-[{_HEX}]{{4}}-[{_HEX}]{{12}}(?![{_HEX}])")


def _mask_pans(text: str) -> tuple[str, int]:
    if not text or not any(c.isdigit() for c in text):
        return text, 0
    if "-" in text and _UUID.search(text):
        # Les UUID sont mis de côté ; ce qui les entoure est masqué séparément (une suite ne traverse jamais un UUID).
        out, total, last = [], 0, 0
        for m in _UUID.finditer(text):
            part, n = _mask_run(text[last:m.start()])
            out.extend((part, m.group(0)))
            total, last = total + n, m.end()
        part, n = _mask_run(text[last:])
        return "".join((*out, part)), total + n
    return _mask_run(text)


_PLAIN_INT = re.compile(r"[+\-−]?\d+")


def _mask_run(text: str) -> tuple[str, int]:
    if not text or not any(c.isdigit() for c in text):
        return text, 0
    n = 0
    # Carte écrite en notation scientifique (`4.242424242424242e15`) : les chiffres de la mantisse, Luhn-valides, sont masqués.
    for a, b in reversed(number_spans(text)):
        integer, mant, sci = parts(text[a:b])
        if sci and 13 <= len(mant) <= 19 and mant[0] in "23456" and _luhn(mant):
            tok = text[a:b]
            cut = re.search(r"[eE]", tok).start()  # type: ignore[union-attr]
            text = text[:a] + re.sub(r"\d", MASK_CHAR, tok[:cut]) + tok[cut:] + text[b:]
            n += 1
    # Décimal dont la partie entière a 13 chiffres ou plus (`4242424242424242.0`) : pas épargné, la partie entière est
    # jugée comme un entier (masquée si Luhn).
    spans = [(a, b) for a, b in exempt_spans(text, 13) if not _PLAIN_INT.fullmatch(text[a:b])]

    def repl(m: re.Match[str]) -> str:
        nonlocal n
        if spans and inside(spans, m.start(), m.end()):
            return m.group(0)  # chiffres d'un nombre décimal ou scientifique (`0.30000000000000004`) : pas une carte
        if _IBAN_HEAD.search(text[max(0, m.start() - 6):m.start()]):
            return m.group(0)
        digits = re.sub(r"\D", "", m.group(0))
        if not (12 <= len(digits) <= 19 and _luhn(digits)):
            return m.group(0)
        n += 1
        return re.sub(r"\d", MASK_CHAR, m.group(0))

    return _RUN.sub(repl, text), n


# Ligne d'instantané : `- textbox "Card number" [ref=e12]: 4242 4242…`, ou `… value="12/30"`.
_LINE_VALUE = re.compile(r"(\]\s*:\s*)(.+)$")
_ATTR_VALUE = re.compile(r"""(\bvalue\s*[=:]\s*)(["'])(.*?)\2""")


# Champs de code à usage unique (3-D Secure par SMS, code de la banque) : leur valeur est masquée comme une carte.
OTP_TERMS = ("one-time-code", "one time code", "one-time code", "otp", "sms code", "code sms", "code de verification",
             "code de vérification", "verification code", "code recu par sms", "code reçu par sms", "bestatigungscode",
             "bestätigungscode", "sms-code", "codice di verifica", "codice sms", "mobile tan", "mtan", "passcode")


def otp_field(label: str | None) -> bool:
    if not label:
        return False
    from nokker.payments.detect import normalize

    padded = f" {normalize(label)} "
    return any(f" {normalize(t)} " in padded for t in OTP_TERMS)


def mask_card_fields(text: str) -> str:
    """Valeur des champs de carte et de code à usage unique d'un instantané d'accessibilité (ou d'un HTML lu)
    remplacée par « •••• »."""
    return _mask_card_fields(text)[0]


def _mask_card_fields(text: str) -> tuple[str, int]:
    if not text:
        return text, 0
    out, n = [], 0
    for line in text.split("\n"):
        head = line.split("[ref=", 1)[0] if "[ref=" in line else line
        auto = _autocomplete(line)
        if card_field(head) or card_field(auto) or otp_field(head) or otp_field(auto):
            # Un marqueur de l'appareil (`⟦CARD-1⟧`, nokker/redact/markers.py) n'est pas une valeur : il reste.
            new = _LINE_VALUE.sub(lambda m: m.group(0) if _only_marker(m.group(2)) else m.group(1) + VALUE_MASK, line)
            new = _ATTR_VALUE.sub(lambda m: m.group(0) if _only_marker(m.group(3))
                                  else f"{m.group(1)}{m.group(2)}{VALUE_MASK}{m.group(2)}", new)
            if new != line:
                n += 1
            line = new
        out.append(line)
    return "\n".join(out), n


def _only_marker(value: str) -> bool:
    from nokker.redact.markers import MARKER_RE

    return MARKER_RE.fullmatch(value.strip()) is not None


_AUTOCOMPLETE = re.compile(r"""autocomplete\s*[=:]\s*["']?(cc-[a-z\-]+|one-time-code)""", re.IGNORECASE)


def _autocomplete(line: str) -> str:
    m = _AUTOCOMPLETE.search(line)
    return m.group(1) if m else ""


def mask_card_text(text: str) -> str:
    """Les deux masquages : valeurs des champs de carte, puis numéros Luhn-valides. Idempotent."""
    return mask_card_counted(text)[0]


def mask_card_counted(text: str) -> tuple[str, int]:
    """`mask_card_text` et le nombre de NUMÉROS au format carte masqués dans le texte. Les valeurs de champs de carte,
    de CVV ou d'OTP remplacées par « •••• » ne comptent pas : le repère dit « lis-le sur ta page », ce qui ne doit
    jamais pousser à faire dicter un code secret (décision du 02.11.2026). Champs masqués seuls : 0, pas de repère."""
    if not text:
        return text, 0
    t1, _fields = _mask_card_fields(text)
    return _mask_pans(t1)


# ── Textes vus par le modèle, drapeau `card_mask_notice` (validés mot pour mot le 02.11.2026) ──
# Le masquage, lui, est toujours actif ; sans le drapeau, pas de repère et l'ancien texte de capture refusée.
NOTICE_TAG = "[Masque]"
NOTICE_TAG_NEUTRAL = "(Masque)"
MASK_NOTICE = (NOTICE_TAG + " {n} numéro(s) au format carte bancaire remplacé(s) par des « • ». Ne cherche pas à les "
               "retrouver ; si c'est un numéro de commande ou de suivi, dis à la personne de le lire sur sa page.")
SCREENSHOT_REFUSED = ("capture non faite : la page peut afficher un numéro de carte que le masque n'a pas pu couvrir. "
                      "Utilise browser_snapshot, qui le masque.")
SCREENSHOT_REFUSED_OLD = "capture impossible pour l'instant : utilise browser_snapshot"

_settings = {"notice": False}


def configure(*, notice: bool) -> None:
    """Drapeau `card_mask_notice` (posé au démarrage par `nokker/api/server.py::build_invariants`)."""
    _settings["notice"] = bool(notice)


def notice_on() -> bool:
    return _settings["notice"]


def screenshot_refused_text() -> str:
    return SCREENSHOT_REFUSED if notice_on() else SCREENSHOT_REFUSED_OLD


_OWN_NOTICE = re.compile(r"\n" + re.escape(NOTICE_TAG) + r" \d+ numéro\(s\) au format carte bancaire remplacé\(s\) par "
                         r"des « • »\. Ne cherche pas à les retrouver ; si c'est un numéro de commande ou de suivi, dis à "
                         r"la personne de le lire sur sa page\.$")


def mask_tool_text(text: str) -> str:
    """Résultat d'outil (instantané, read_text, recherche web… : tout résultat) : masqué ; avec le drapeau, un
    « [Masque] » écrit par la page devient « (Masque) » et, si au moins un masque est posé, UNE ligne de repère est
    ajoutée à la fin (nombre de masques, jamais le numéro). Aucun démasquage possible : rien n'est gardé."""
    if not text:
        return text
    if not notice_on():
        return mask_card_text(text)
    own = _OWN_NOTICE.search(text)  # déjà passé (résultat relu) : notre ligne reste, le reste est re-vérifié
    body, tail = (text[:own.start()], own.group(0)) if own else (text, "")
    body = body.replace(NOTICE_TAG, NOTICE_TAG_NEUTRAL)
    body, n = mask_card_counted(body)
    if tail:
        return body + tail
    return body + "\n" + MASK_NOTICE.format(n=n) if n else body


# Hôtes des iframes de champs de carte des prestataires de paiement courants (leurs champs hébergés).
PSP_FRAME_HOSTS = ("js.stripe.com", "checkoutshopper-live.adyen.com", "checkoutshopper-test.adyen.com",
                   "assets.braintreegateway.com", "payments.braintree-api.com", "pay.datatrans.com",
                   "pay.sandbox.datatrans.com", "secure.payrexx.com", "checkout.saferpay.com", "www.saferpay.com",
                   "secure.ogone.com", "checkout.mollie.com", "js.mollie.com", "frames.checkout.com",
                   "hostedfields.payone.com", "secure.worldpay.com", "cdn.worldpay.com", "pay.wallee.com",
                   "app-wallee.com", "secure.paypal.com", "www.paypal.com")

_SELECTOR = ", ".join([
    'input[autocomplete^="cc-"]',
    'input[autocomplete="one-time-code"]',
    *(f'input[name*="{w}" i]' for w in ("card", "cvc", "cvv", "csc", "expir", "exp-date", "expdate", "pan")),
    *(f'input[id*="{w}" i]' for w in ("card", "cvc", "cvv", "csc", "expir")),
    *(f'iframe[src*="{h}"]' for h in PSP_FRAME_HOSTS),
])

# Script `browser_evaluate` (fonction sans argument) : une feuille de style qui floute les champs de carte et les
# iframes de PSP, et rend leur texte illisible ; rend le nombre d'éléments couverts.
MASK_SCRIPT = (
    "() => { const id = 'nokker-card-mask'; let s = document.getElementById(id);"
    " if (!s) { s = document.createElement('style'); s.id = id; document.documentElement.appendChild(s); }"
    f" const sel = {_SELECTOR!r};"
    " s.textContent = sel + ' { filter: blur(14px) !important; -webkit-text-security: disc !important;"
    " color: transparent !important; text-shadow: none !important; }';"
    " return document.querySelectorAll(sel).length; }"
)
UNMASK_SCRIPT = "() => { const s = document.getElementById('nokker-card-mask'); if (s) s.remove(); return true; }"


__all__ = ["MASK_NOTICE", "NOTICE_TAG", "NOTICE_TAG_NEUTRAL", "SCREENSHOT_REFUSED", "SCREENSHOT_REFUSED_OLD",
           "configure", "mask_card_counted", "mask_tool_text", "notice_on", "screenshot_refused_text",
           "MASK_CHAR", "MASK_SCRIPT", "OTP_TERMS", "PSP_FRAME_HOSTS", "UNMASK_SCRIPT", "VALUE_MASK",
           "mask_card_fields", "mask_card_text", "mask_pans", "otp_field"]
