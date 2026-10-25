"""Outils de texte du contexte : découpage en mots, mots-clés, bornage. Pur Python, aucun modèle, déterministe."""
from __future__ import annotations

import re
from collections import Counter

_WORD = re.compile(r"[0-9a-zàâäçéèêëîïôöùûüÿœæ][0-9a-zàâäçéèêëîïôöùûüÿœæ_\-]*", re.IGNORECASE)

# Mots vides FR + quelques EN : assez pour qu'un mot-clé soit un mot qui porte du sens ; pas exhaustif.
STOPWORDS = frozenset(["alors", "ainsi", "après", "avec", "avoir", "avons", "avez", "aussi", "autre", "autres", "beaucoup", "bien", "cela", "celui", "ceux", "chaque", "comme", "comment", "dans", "dont", "donc", "elle", "elles", "encore", "entre", "était", "étaient", "être", "fait", "faire", "fois", "leur", "leurs", "mais", "même", "moins", "notre", "nous", "parce", "pour", "pourquoi", "plus", "puis", "quand", "quel", "quelle", "quels", "quelles", "sans", "sera", "sont", "sous", "sur", "tant", "tous", "tout", "toute", "toutes", "très", "trop", "une", "unes", "vers", "voici", "voilà", "votre", "vous", "avez", "cette", "ces", "cet", "ceci", "celle", "ici", "déjà", "depuis", "peut", "peux", "veux", "the", "and", "for", "with", "that", "this", "from", "have", "has", "are", "was", "were", "not", "you", "your", "our", "can", "will"])


def words(text: str) -> list[str]:
    """Mots en minuscules (lettres accentuées, chiffres, `_` et `-` internes), sans filtre."""
    return [m.group(0).lower() for m in _WORD.finditer(text or "")]


def content_words(text: str, min_len: int = 3) -> list[str]:
    return [w for w in words(text) if len(w) >= min_len and w not in STOPWORDS and not w.isdigit()]


def keywords(text: str, n: int = 12) -> list[str]:
    """Les `n` mots les plus fréquents (≥ 4 lettres), égalité → ordre de première apparition."""
    counts: Counter[str] = Counter()
    first: dict[str, int] = {}
    for i, w in enumerate(content_words(text, 4)):
        counts[w] += 1
        first.setdefault(w, i)
    ranked = sorted(counts, key=lambda w: (-counts[w], first[w]))
    return ranked[:n]


_PUNCT = ".,;:!?()«»\"'’[]"
_ACRONYM = re.compile(r"[A-ZÀÂÄÇÉÈÊËÎÏÔÖÙÛÜŸ]{2,}[0-9]*")
_CAPITALIZED = re.compile(r"[A-ZÀÂÄÇÉÈÊËÎÏÔÖÙÛÜŸ][a-zàâäçéèêëîïôöùûüÿœæ]{2,}")


# Marqueurs d'un REVIREMENT de l'utilisateur (il annule ou change une décision prise plus tôt) — banc X9, §5.1 de
# bench/never_clear/RESULTS.md. Formes impératives ou figées seulement : « je reviens » (pause), « réponds « non » »,
# « le retour en arrière » (nom) ne sont pas des revirements. Un faux positif coûte une ligne de contexte ; un oubli
# coûte une consigne annulée ré-appliquée.
_REVOCATION = re.compile(
    r"\bannul(e|ez|er|ons)\b|\brevi(ens|enez|enir|ent) (à|au|aux|en arrière|sur)\b|\bfinalement\b|"
    r"\bremet(s|tez|tre) (le|la|les|l'|un|une|plutôt)|\bdéfai(s|tes|re)\b|\brétabli(s|ssez|r)\b|\bah non\b|"
    r"\ben fait,? non\b|\boublie[sz]? (ça|ce|cette|cela|le|la|les)\b|\bne (veut|veux|veulent|voulons) plus\b|"
    r"\bchang\w* d'avis\b|\bon ne (fait|garde) plus\b|"
    r"\b(revert|undo|roll ?back|scratch that|never ?mind)\b", re.IGNORECASE)


def is_revocation(text: str) -> bool:
    """Vrai si le message (ou la phrase) annule ou change une décision antérieure (marqueurs ci-dessus)."""
    return bool(_REVOCATION.search(text or ""))


# Marqueurs d'une EXIGENCE DE PRÉSERVATION posée au fil d'une tâche : ce qui doit survivre aux changements suivants
# (« réexporte total_ht pour ne rien casser chez les appelants », « X doit rester importable », « la facture d'origine
# ne doit pas être modifiée », « garde le comportement actuel ») — banc X9, RESULTS.md §10.3, NEVER-CLEAR §13. Le
# code qui porte une telle exigence sert souvent des appelants HORS du dépôt : rien ne l'utilise dedans, un nettoyage
# le prend pour du code mort. Pas les spécifications ordinaires (« -1233 doit donner -1235 ») : les tests du projet
# les portent déjà ; pas « toujours… / jamais… » seuls, que le préfixe fait noter en règle (`memory_note`).
_CONSTRAINT = re.compile(
    r"\bpour ne (rien|pas) casser\b|\bsans (rien )?casser\b|\bne (cass|touch)(e|ez|er) (rien|pas|jamais)\b|"
    r"\bré-?export\w*|\b(rétro-?)?compatib\w*|"
    r"\bdoi(t|vent) (toujours |encore )?rester\b|\brest(e|ent|er) (importables?|compatibles?|disponibles?|publi(c|cs|que|ques))\b|"
    r"\bgarde(z|r)? (le comportement|l'api|la signature|l'interface|le nom|les noms|la compatibilité)\b|"
    r"\bne doi(t|vent) (pas|plus|jamais) (être )?(modifi|chang|supprim|bouger|disparaître)\w*|"
    r"\bne doi(t|vent) (pas )?changer\b|"
    r"\b(backwards?[- ]compat\w*|re-?export\w*|must (stay|remain)|do(n't| not) break|keep (it )?(working|importable))\b",
    re.IGNORECASE)


def is_constraint(text: str) -> bool:
    """Vrai si le message (ou la phrase) pose une exigence de préservation (marqueurs ci-dessus)."""
    return bool(_CONSTRAINT.search(text or ""))


def constraint_clauses(text: str) -> str:
    """Les seules propositions (découpe sur les phrases puis sur « ; ») qui portent une exigence, « » si aucune :
    « modeles.py ne garde que les classes, mais réexporte total_ht… » sans le « Crée factura/calculs.py… » d'avant."""
    picked: list[str] = []
    for sent in sentences(text):
        parts = [p.strip() for p in sent.split(";") if p.strip()]
        picked.extend(p for p in parts if is_constraint(p))
    return " ".join(picked)


def salience(line: str) -> float:
    """Densité de « faits » d'une phrase : noms propres (hors début de phrase), sigles, identifiants alphanumériques,
    nombres entiers de 3 chiffres et plus ; les petits nombres et les décimaux (numérotation, heures) comptent peu ;
    une question vaut moins qu'un énoncé. Sert à choisir ce qu'un résumé extractif garde. Une phrase de revirement
    (« Reviens au point-virgule. ») n'a souvent ni nom ni nombre mais porte la décision EN VIGUEUR : elle pèse autant
    qu'un fait fort ; une exigence de préservation (« réexporte … pour ne rien casser ») aussi."""
    toks = [t.strip(_PUNCT) for t in line.split()]
    score = 6.0 if is_revocation(line) or is_constraint(line) else 0.0
    for i, t in enumerate(toks):
        if not t:
            continue
        digits = sum(c.isdigit() for c in t)
        if i > 0 and _CAPITALIZED.fullmatch(t):
            score += 3
        elif _ACRONYM.fullmatch(t):
            score += 2 + (1 if digits else 0)
        elif digits and any(c.isalpha() for c in t):
            score += 3  # identifiant alphanumérique (B204, 9h30)
        elif re.fullmatch(r"\d+[.,]\d+", t):
            score += 0.5  # décimal / numérotation
        elif digits >= 3:
            score += 3
        elif digits == 2:
            score += 1
        elif digits == 1:
            score += 0.5
    if line.rstrip().endswith("?"):
        score -= 3
    return score


def distinctive_words(text: str) -> list[str]:
    """Mots qui servent à retrouver un fait : mots pleins (≥ 4 lettres) ET jetons à chiffres (≥ 2 caractères),
    sans doublon, dans l'ordre d'apparition."""
    out: list[str] = []
    for w in words(text):
        if w in STOPWORDS:
            continue
        has_digit = any(c.isdigit() for c in w)
        if (has_digit and len(w) >= 2) or (len(w) >= 4 and not w.isdigit()):
            out.append(w)
    return list(dict.fromkeys(out))


def sentences(text: str) -> list[str]:
    text = " ".join((text or "").split())
    return [p for p in re.split(r"(?<=[.!?])\s+", text) if p]


def clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: max(0, n - 1)].rstrip() + "…"


def first_sentences(text: str, n: int, max_chars: int) -> str:
    text = " ".join((text or "").split())
    parts = re.split(r"(?<=[.!?])\s+", text)
    return clip(" ".join(parts[:n]), max_chars)


__all__ = ["STOPWORDS", "clip", "constraint_clauses", "content_words", "distinctive_words", "first_sentences",
           "is_constraint", "is_revocation", "keywords", "salience", "sentences", "words"]
