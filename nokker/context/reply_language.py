"""Drapeau `reply_language` : consigne de langue du préfixe produit (`prompts/prefix-nokker.md`, section Style).

Allumé : la ligne « Réponds dans la langue de l'utilisateur… » est retirée et `LANGUAGE_LINE` est posée EN TÊTE de la
section Style (le modèle répondait dans la langue du préfixe ou des documents au lieu de celle de la personne : banc
du palier, relais EN/DE/IT, 03.11.2026). Éteint : le préfixe est lu tel quel, à l'octet près. Appliqué au chargement
des couches (`ContextEngineV0.__post_init__`), avant tout calcul d'empreinte : le hash du préfixe reste figé par
locataire. Un préfixe sans section Style ou sans la ligne d'origine (préfixe de code Lokkal) est rendu inchangé.
Le bloc [Session vocale] (`knock/policy.py`) garde sa propre ligne Langue."""
from __future__ import annotations

STYLE_HEADER = "## Style\n\n"
OLD_LINE = "- Réponds dans la langue de l'utilisateur ; s'il change de langue, suis-le.\n"
LANGUAGE_LINE = ("- **Langue** : réponds dans la langue dans laquelle l'utilisateur t'écrit, même si ce préfixe, les blocs "
                 "du noyau, les résultats d'outils ou les documents sont dans une autre langue. S'il change de langue, "
                 "suis-le. Ne traduis pas les noms, chiffres et citations que tu relaies.\n")
# Drapeau `reply_language_tail` : ligne en fin d'en-tête du tour, juste avant le message (`ContextEngineV0.turn_head`).
TAIL_LINE = "Langue de réponse : celle du message de l'utilisateur ci-dessous."


def apply(text: str) -> str:
    """Préfixe rendu avec le drapeau allumé ; inchangé si la section Style ou la ligne d'origine manque."""
    start = text.find(STYLE_HEADER)
    if start < 0:
        return text
    body = start + len(STYLE_HEADER)
    end = text.find("\n## ", body)
    end = len(text) if end < 0 else end
    section = text[body:end]
    if section.count(OLD_LINE) != 1:
        return text
    return text[:body] + LANGUAGE_LINE + section.replace(OLD_LINE, "", 1) + text[end:]


__all__ = ["LANGUAGE_LINE", "OLD_LINE", "STYLE_HEADER", "TAIL_LINE", "apply"]
