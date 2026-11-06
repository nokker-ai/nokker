"""Passage PII des traces de testeurs (docs/TRACES.md §5) — appliqué à TOUTE ligne d'un locataire testeur exportée,
qu'elle serve à l'entraînement (consentement) ou à l'évaluation (`--include-eval`). Les secrets, eux, sont scellés à
part (`nokker/qa/secrets.py`), avant ce passage.

Ce qui est couvert (remplacé par une étiquette entre crochets) :
  - adresses e-mail (`[email]`) — même motif que la garde PII de la recherche web (`nokker/tools/web.py`) ;
  - téléphones (`[téléphone]`) : suisses (international et national), et toute forme internationale `+CC …` d'au
    moins 8 chiffres ;
  - IBAN (`[iban]`, toute forme pays + 2 chiffres + blocs) et numéros AVS (`[avs]`) ;
  - numéros de carte (Luhn, 12 à 19 chiffres) et valeurs des champs de carte : chiffres remplacés par « • » ;
  - adresses postales (`[adresse]`) : rue/avenue/chemin/route/place/boulevard/quai/impasse… suivi d'un nom propre et
    d'un numéro facultatif ; `…strasse|gasse|weg|platz 12` ; `12 Main Street` ; code postal + localité après une
    virgule, un saut de ligne ou `CH-` ;
  - noms de personnes (`[nom]`) : après une civilité (M., Mme, Monsieur, Madame, Herr, Frau, Mr, Mrs, Ms, Dr), après
    « je m'appelle / mon nom est / my name is / ich heisse », en signature après une formule de politesse, et tout
    mot tiré de l'adresse e-mail du compte (partie locale : `prenom.nom@…` → « prenom », « nom »).

Ce qui ne l'est PAS (dit dans le manifeste de chaque lot) : un nom propre isolé sans civilité, sans formule ni lien
avec l'adresse du compte (« j'ai vu Martin hier ») ; une adresse sans mot-clé de voie ni code postal précédé d'une
virgule ; dates de naissance, numéros de plaque, de passeport, de client ; identifiants dans des URL ; contenu
d'images (seule leur empreinte part) ; PII dans les noms de fichiers de pièces jointes déjà référencées.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from nokker.payments.cardmask import mask_card_text
from nokker.tools.web import _AVS_RE, _EMAIL_RE, _PHONE_CH_RE

_UP = "A-ZÀ-ÖØ-Ý"
_WORD = rf"[{_UP}][\w'’\-]+"
_PHONE_INTL_RE = re.compile(r"\+\d{1,3}(?:[ .\-/]?\(?\d{1,4}\)?){2,6}")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}(?:[ ]?[A-Z0-9]{1,3})?\b")
_STREET_FR_RE = re.compile(
    rf"(?:\b\d{{1,4}}(?:\s?(?:bis|ter|[a-c]))?,?\s+)?"
    rf"\b(?i:rue|avenue|av\.|chemin|ch\.|route|rte|place|boulevard|bd|quai|impasse|allée|sentier|passage|"
    rf"ruelle|promenade|viale|piazza)\s+(?:(?i:de\s+la|de\s+l['’]|du|des|de|d['’]|della|del)\s*)?"
    rf"{_WORD}(?:[\s\-]{_WORD}){{0,3}}(?:\s*,?\s*\d{{1,4}}\s?[a-zA-Z]?\b)?")
_STREET_DE_RE = re.compile(
    r"\b[A-ZÄÖÜ][\wäöüß\-]*(?:strasse|straße|gasse|weg|platz|allee|ring|rain|halde)\s+\d{1,4}\s?[a-zA-Z]?\b")
_STREET_EN_RE = re.compile(
    r"\b\d{1,5}\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s+(?:Street|St\.|Avenue|Ave\.|Road|Rd\.|Lane|Ln\.|Drive|Dr\.|"
    r"Boulevard|Blvd\.|Way|Court|Ct\.)")
_POSTAL_RE = re.compile(rf"(,\s*|\n\s*|\bCH-)([1-9]\d{{3}}\s+[{_UP}][a-zà-ÿ]{{2,}}(?:[\s\-][{_UP}][a-zà-ÿ]+)?)")
_HONORIFIC_RE = re.compile(
    rf"\b(M\.|Mme|Mlle|Monsieur|Madame|Mademoiselle|Herr|Frau|Mr\.?|Mrs\.?|Ms\.?|Dr\.?|Prof\.?)(\s+)"
    rf"({_WORD}(?:\s+{_WORD})?)")
_INTRO_RE = re.compile(
    rf"((?i:je\s+m['’]appelle|mon\s+nom\s+est|my\s+name\s+is|ich\s+hei(?:ss|ß)e|mi\s+chiamo)\s+)"
    rf"({_WORD}(?:\s+{_WORD})?)")
_SIGNATURE_RE = re.compile(
    rf"((?i:cordialement|bien\s+à\s+(?:vous|toi)|meilleures\s+salutations|salutations|best\s+regards|"
    rf"kind\s+regards|regards|freundliche\s+gr(?:ü|ue)(?:ss|ß)e|liebe\s+gr(?:ü|ue)(?:ss|ß)e|bises|merci)"
    rf"[,!.]?[ \t]*\n\s*)({_WORD}(?:[ \t]+{_WORD}){{0,2}})")
# Mots de la partie locale d'une adresse qui ne sont pas des noms.
_LOCAL_STOP = {"info", "contact", "admin", "mail", "email", "test", "hello", "bonjour", "user", "support", "office",
               "team", "noreply", "reply", "post", "home", "work", "perso", "pro", "the", "and"}


def name_terms(email: str | None, extra: list[str] | None = None) -> list[str]:
    """Mots à caviarder pour un compte : partie locale de son adresse (`prenom.nom`), plus `extra` (noms connus)."""
    terms: list[str] = []
    if email and "@" in email:
        local = email.split("@", 1)[0].split("+", 1)[0]
        terms += [t for t in re.split(r"[._\-\d]+", local) if len(t) >= 3 and t.isalpha() and t.lower() not in _LOCAL_STOP]
    for e in extra or []:
        terms += [t for t in re.split(r"\s+", e.strip()) if len(t) >= 3]
    return sorted({t.lower() for t in terms}, key=len, reverse=True)


def scrub_pii(text: str, terms: list[str] | None = None, counts: Counter | None = None) -> str:
    """Texte avec les PII couvertes remplacées (voir l'en-tête). `counts` reçoit le nombre de remplacements par type."""
    if not text:
        return text
    c = counts if counts is not None else Counter()

    def sub(pattern: re.Pattern[str], label: str, s: str, repl: Any = None) -> str:
        out, n = pattern.subn(repl or f"[{label}]", s)
        if n:
            c[label] += n
        return out

    # Données de carte (PAYMENTS §2 bis) : chiffres masqués (« • »), avant l'IBAN dont la forme est voisine. Couvre
    # aussi les arguments d'un appel refusé (un numéro tapé par le modèle reste dans le tour stocké).
    masked = mask_card_text(text)
    if masked != text:
        c["carte"] += 1
        text = masked
    text = sub(_EMAIL_RE, "email", text)
    text = sub(_IBAN_RE, "iban", text)
    text = sub(_AVS_RE, "avs", text)
    text = sub(_PHONE_INTL_RE, "téléphone", text, lambda m: "[téléphone]" if sum(ch.isdigit() for ch in m.group(0)) >= 8
               else m.group(0))
    text = sub(_PHONE_CH_RE, "téléphone", text)
    text = sub(_STREET_DE_RE, "adresse", text)
    text = sub(_STREET_EN_RE, "adresse", text)
    text = sub(_STREET_FR_RE, "adresse", text)
    text = sub(_POSTAL_RE, "adresse", text, lambda m: m.group(1) + "[adresse]")
    text = sub(_HONORIFIC_RE, "nom", text, lambda m: m.group(1) + m.group(2) + "[nom]")
    text = sub(_INTRO_RE, "nom", text, lambda m: m.group(1) + "[nom]")
    text = sub(_SIGNATURE_RE, "nom", text, lambda m: m.group(1) + "[nom]")
    for term in terms or []:
        text = sub(re.compile(rf"(?<![\w]){re.escape(term)}(?![\w])", re.IGNORECASE), "nom", text)
    return text


def scrub_obj(obj: Any, terms: list[str] | None = None, counts: Counter | None = None) -> Any:
    """`scrub_pii` sur toutes les chaînes d'une structure JSON (contenus, parties texte, arguments d'outils)."""
    if isinstance(obj, str):
        return scrub_pii(obj, terms, counts)
    if isinstance(obj, list):
        return [scrub_obj(x, terms, counts) for x in obj]
    if isinstance(obj, dict):
        return {k: (v if k in ("role", "type", "tool_call_id", "id") else scrub_obj(v, terms, counts))
                for k, v in obj.items()}
    return obj


COVERED = ["email", "téléphone (CH, international +CC ≥ 8 chiffres)", "iban", "avs",
           "adresse (voie + nom, …strasse 12, 12 Main Street, code postal + localité après virgule/ligne/CH-)",
           "nom (après civilité, « je m'appelle / my name is », en signature, mots de l'adresse e-mail du compte)"]
NOT_COVERED = ["nom propre isolé sans civilité, formule ni lien avec l'adresse du compte",
               "adresse sans mot-clé de voie ni code postal précédé d'une virgule ou d'un saut de ligne",
               "dates de naissance, plaques, passeports, numéros de client",
               "identifiants dans des URL", "contenu des images (seule l'empreinte part)"]

__all__ = ["COVERED", "NOT_COVERED", "name_terms", "scrub_obj", "scrub_pii"]
