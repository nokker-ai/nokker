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
                 "de contexte, les résultats d'outils ou les documents sont dans une autre langue. S'il change de langue, "
                 "suis-le. Ne traduis pas les noms, chiffres et citations que tu relaies.\n")
# Drapeau `reply_language_tail` : ligne en fin d'en-tête du tour, juste avant le message (`ContextEngineV0.turn_head`),
# dans la langue résolue du tour (`tail_line`, texte `context.reply_lang_tail`). `TAIL_LINE` = la forme française.
TAIL_LINE = "Langue de réponse : celle du message de l'utilisateur ci-dessous."


def tail_line() -> str:
    from nokker.core import model_texts as mt

    return mt.pick(mt.REPLY_LANG_TAIL)


def turn_line(turn) -> str | None:
    """Ligne de langue de l'en-tête du tour (texte `context.reply_language`, langue RÉSOLUE du tour), ou None. Seulement
    sur un VRAI signal de langue (`Turn.lang_signal` : requête, compte, message détecté, fil, `NOKKER_LANG`) : sur le défaut
    du code la ligne affirmerait un fait inventé. Session `lang_hint: false` : pas de ligne. Commune à V0 et V1."""
    from nokker.approvals.rule_labels import current_lang, norm_lang
    from nokker.core import model_texts as mt
    from nokker.core import session_opts

    if getattr(turn, "lang_signal", False) and session_opts.lang_hint_enabled(turn.thread_id):
        return mt.pick(mt.REPLY_LANGUAGE, norm_lang(getattr(turn, "lang", None)) or current_lang())
    return None


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


__all__ = ["LANGUAGE_LINE", "OLD_LINE", "STYLE_HEADER", "TAIL_LINE", "apply", "tail_line", "turn_line"]
