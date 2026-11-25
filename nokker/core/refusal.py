"""Gabarit unique d'un résultat d'outil refusé par les portes, tel que le lit le modèle.

Un seul préfixe, `REFUSED_PREFIX`, pour tous les sites (boucle, appel direct, appel d'app, script, outils) : le code qui
doit reconnaître un refus (relecture, bilan du tour, traces) passe par `is_refusal`, jamais par un texte recopié. Le
préfixe ne nomme pas qui refuse : un modèle qui lit « le noyau » le répète à la personne."""
from __future__ import annotations

from nokker.core import model_texts as mt

# Préfixe français (figé) ; le texte vu du modèle suit la langue du tour : `refused_prefix()`.
REFUSED_PREFIX = mt.REFUSED_PREFIX["fr"]
# Anciens refus en majuscules (« REFUSÉ : … », outils qui ne passent pas par les portes) : reconnus au même endroit, dans
# les quatre langues (`mt.REFUSED_UPPER`).
_REFUSED_UPPER = "REFUSÉ"
# Consigne générique qui suit le motif d'un refus de porte (le mode plan et le todo ont la leur).
RETRY_ADVICE = mt.RETRY_ADVICE["fr"]


def refused_prefix(lang: str | None = None) -> str:
    """« Refusé : » dans la langue du tour (français par défaut)."""
    return mt.pick(mt.REFUSED_PREFIX, lang)


def refused_text(why: str, lang: str | None = None) -> str:
    """« Refusé : {motif}. » — le motif est celui de `refusal_why` ; un point final déjà là n'est pas doublé."""
    return f"{refused_prefix(lang)}{why}" + ("" if why.endswith((".", "!", "?")) else ".")


# + carte refusée par la personne pendant le tour (`mt.CARD_DECLINED`, « Refusé par la personne ({who}) … »).
_REFUSAL_STARTS = (*mt.REFUSED_PREFIX.values(), *mt.REFUSED_UPPER.values(), *mt.marks(mt.CARD_DECLINED, head=" ("))


def is_refusal(content: str | None) -> bool:
    """Le résultat d'outil est un refus (gabarit commun ou « REFUSÉ … » d'un outil), dans n'importe laquelle des quatre
    langues."""
    text = content or ""
    return text.startswith(_REFUSAL_STARTS)


__all__ = ["REFUSED_PREFIX", "RETRY_ADVICE", "is_refusal", "refused_prefix", "refused_text"]
