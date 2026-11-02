"""Détection d'une étape de paiement dans le navigateur (docs/PAYMENTS.md §2) — pur, sans état ni réseau.

Trois questions, posées par les portes (`nokker/payments/gates.py`) avant tout appel d'outil navigateur à effet :

- `card_field(label)` : ce champ est-il un champ de carte (numéro, CVV/CVC, expiration, titulaire, code 3-D Secure) ?
- `looks_like_pan(text)` : ce texte contient-il un numéro de carte (13 à 19 chiffres, clé de Luhn valide) ?
- `pay_button(label)` : ce bouton déclenche-t-il le paiement (« Payer », « Place order », « Zahlungspflichtig
  bestellen »…) ? Ajouter au panier, continuer, se connecter : non.

Et une quatrième pour les résultats d'outil (`checkout_page(text)`) : la page lue est-elle une page de paiement ?

Libellés comparés en minuscules, sans accents, espaces et tirets normalisés ; FR, DE, IT, EN. Un faux positif coûte une
carte « à payer » de trop (l'humain reprend la main) ; un faux négatif, au pire, un clic que le marchand bloquera de
toute façon sans numéro de carte, que l'agent ne saisit jamais.
"""
from __future__ import annotations

import re
import unicodedata

# Champs de carte : numéro, code de sécurité, expiration, titulaire, 3-D Secure. Attributs `autocomplete` compris
# (`cc-number`, `cc-csc`, `cc-exp`, `cc-name`), tels qu'un instantané d'accessibilité ou un sélecteur les montre.
CARD_FIELD_TERMS = (
    # numéro
    "card number", "cardnumber", "card no", "numero de carte", "numero de la carte", "no de carte", "n de carte",
    "kartennummer", "numero della carta", "numero di carta", "numero carta", "cc number", "cc-number", "ccnumber",
    "credit card", "debit card", "carte de credit", "carte bancaire", "kreditkarte", "carta di credito",
    # code de sécurité
    "cvv", "cvc", "cvv2", "cvc2", "csc", "cc-csc", "security code", "code de securite", "cryptogramme",
    "sicherheitscode", "prufziffer", "prufnummer", "kartenprufnummer", "codice di sicurezza", "card verification",
    # expiration
    "expiry", "expiration date", "exp date", "expiration", "date d expiration", "valable jusqu", "ablaufdatum",
    "gultig bis", "gueltig bis", "scadenza", "cc-exp", "mm/yy", "mm / yy", "mm/aa", "mm / aa", "mm/jj", "mm / jj",
    # titulaire
    "cardholder", "card holder", "name on card", "titulaire de la carte", "nom sur la carte", "karteninhaber",
    "titolare della carta", "cc-name",
    # 3-D Secure et équivalents
    "3-d secure", "3d secure", "3ds", "verified by visa", "securecode", "mastercard id check", "visa secure",
    "safekey",
)

# Boutons qui DÉCLENCHENT le paiement (pas ceux qui y mènent : panier, livraison, connexion).
PAY_BUTTON_TERMS = (
    "payer", "payer maintenant", "pay", "pay now", "place order", "place your order", "complete purchase",
    "complete order", "submit payment", "confirm payment", "confirm and pay", "buy now", "passer la commande",
    "confirmer la commande", "commander et payer", "valider le paiement", "valider et payer", "confirmer le paiement",
    "acheter maintenant", "jetzt bezahlen", "bezahlen", "zahlungspflichtig bestellen",
    "jetzt kaufen", "kaufen", "bestellung abschicken", "kostenpflichtig bestellen", "paga", "paga ora", "acquista ora",
    "conferma ordine", "conferma il pagamento", "ordina e paga",
)

# Page de paiement : au moins un champ de carte, OU un bouton de paiement avec un mot de total/paiement.
_CHECKOUT_CONTEXT = ("checkout", "paiement", "payment", "zahlung", "kasse", "pagamento", "total", "montant",
                     "summe", "totale", "betrag")

_SPACES = re.compile(r"[\s_\-]+")
_NON_WORD = re.compile(r"[^0-9a-z/ ]+")


def normalize(text: str) -> str:
    """Minuscules, sans accents ni ponctuation (sauf « / »), espaces simples."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("'", " ").replace("’", " ")
    text = _NON_WORD.sub(" ", _SPACES.sub(" ", text))
    return " ".join(text.split())


def _has_term(norm: str, terms: tuple[str, ...]) -> str | None:
    padded = f" {norm} "
    for term in terms:
        t = normalize(term)
        if t and f" {t} " in padded:
            return term
    return None


def card_field(label: str | None) -> str | None:
    """Le terme de carte trouvé dans `label`, ou None. Le tiret et le souligné valent une espace (`cc-number`)."""
    if not label:
        return None
    return _has_term(normalize(label), CARD_FIELD_TERMS)


def pay_button(label: str | None) -> str | None:
    """Le libellé de paiement trouvé dans `label`, ou None. « Pay » seul ou suivi d'un montant (« Pay CHF 12.90 »)
    compte ; « Payment method », « Add to cart », « Continuer » ne comptent pas."""
    if not label:
        return None
    norm = normalize(label)
    if norm.startswith(("ajouter", "add to", "in den warenkorb", "aggiungi", "mode de paiement", "payment method",
                        "zahlungsart", "metodo di pagamento", "moyen de paiement")):
        return None
    return _has_term(norm, PAY_BUTTON_TERMS)


_DIGIT_RUN = re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def looks_like_pan(text: str | None) -> bool:
    """Vrai si `text` contient un numéro de carte plausible : 13 à 19 chiffres (espaces ou tirets permis entre eux),
    clé de Luhn valide. Un IBAN (lettres en tête) ou un numéro de suivi qui échoue à Luhn ne compte pas."""
    if not text:
        return False
    text = str(text)
    for m in _DIGIT_RUN.finditer(text):
        if re.search(r"[A-Za-z]{2}\s?$", text[max(0, m.start() - 3):m.start()]):  # IBAN (CH93 …) : pas une carte
            continue
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return True
    return False


def looks_like_secret_code(text: str | None) -> bool:
    """3 à 8 chiffres seuls : un CVV, un code 3-D Secure ou un code SMS — refusé dans un champ de carte."""
    return bool(text) and re.fullmatch(r"\s*\d{3,8}\s*", str(text)) is not None


_REF_LINE = re.compile(r"^\s*-?\s*(?P<label>.*?)\s*\[ref=(?P<ref>e\d+)\]", re.MULTILINE)


def refs_of(snapshot: str) -> dict[str, str]:
    """Références `eN` d'un instantané d'accessibilité → leur ligne (rôle et nom accessible), pour savoir ce qu'est
    `ref=e12` quand le modèle ne décrit pas l'élément (`element` absent)."""
    return {m.group("ref"): m.group("label")[:200] for m in _REF_LINE.finditer(snapshot or "")}


def checkout_page(text: str | None) -> bool:
    """Page de paiement : un champ de carte visible, ou un bouton de paiement près d'un mot de total/paiement."""
    if not text:
        return False
    norm = normalize(text[:200_000])
    if _has_term(norm, CARD_FIELD_TERMS):
        return True
    for line in str(text).splitlines():
        if pay_button(line.split("[ref=")[0]) and "button" in line.lower():
            return bool(_has_term(norm, _CHECKOUT_CONTEXT))
    return False


__all__ = ["CARD_FIELD_TERMS", "PAY_BUTTON_TERMS", "card_field", "checkout_page", "looks_like_pan",
           "looks_like_secret_code", "normalize", "pay_button", "refs_of"]
