"""Dédoublonnage des notes de mémoire à l'ÉCRITURE (ticket T6, docs/mem-dedup.md) : déterministe, sans appel modèle.

Deux notes sont **équivalentes** quand leurs mots porteurs de sens sont les mêmes, à la casse, aux accents, à la
ponctuation, aux espaces, aux mots-outils et au pluriel près (« L'utilisateur préfère les réunions le matin. » ≡
« Préfère les réunions le matin »). Une seule tolérance : la plus courte est incluse dans l'autre à UN mot près (un
sujet ajouté, « Alice préfère… »), si elle a au moins 3 mots porteurs et si ce mot n'est ni un nombre ni une négation.
Le seuil est volontairement prudent : un mot CHANGÉ (SMS → e-mail, matin → soir, 2 → 3) n'est jamais un doublon, même
dans une note longue ; une vraie nouvelle note n'est jamais avalée. Le rapprochement plus large (reformulations
libres) reste à la consolidation nocturne (`nokker/memory/consolidation`), qui propose et que le noyau valide."""
from __future__ import annotations

import re
import unicodedata

# Mots-outils FR/EN (accents retirés). Les négations n'y sont PAS : « ne pas appeler » ≠ « appeler ».
STOP = frozenset("""
le la les l un une des du de d au aux a et ou en dans par pour sur avec chez ce cet cette ces c ca
se s sa son ses mon ma mes ton ta tes notre nos votre vos leur leurs
je j tu il elle on nous vous ils elles me m te t lui y que qu qui dont
est sont etre etait avoir ai as ont avait fait faire
the an of to in on at for with and or is are be been it its his her their my your our this that
utilisateur utilisatrice user
""".split())
NEGATIONS = frozenset({"ne", "n", "pas", "jamais", "aucun", "aucune", "rien", "sans", "not", "never", "no", "don"})
_WORD = re.compile(r"[a-z0-9]+")


def normalize(text: str) -> str:
    """Casse, accents, ponctuation et espaces neutralisés."""
    s = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return " ".join(_WORD.findall(s))


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w[-1] in "sx" and not w.isdigit() else w


def tokens(text: str) -> frozenset[str]:
    """Mots porteurs de sens : normalisés, sans mots-outils, au singulier grossier (s/x final)."""
    return frozenset(_stem(w) for w in normalize(text).split() if (w not in STOP or w in NEGATIONS))


def equivalent(a: str, b: str) -> bool:
    """Vrai si `a` et `b` disent la même chose au sens prudent décrit en tête de module."""
    if normalize(a) == normalize(b):
        return True
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    extra = big - small
    if len(small) < 3 or not small <= big or len(extra) != 1:
        return False
    (w,) = extra
    return not any(c.isdigit() for c in w) and w not in NEGATIONS


# Mots qui disent la CATÉGORIE et non le sujet, ignorés pour comparer deux titres (« Canal de contact » ≡ « Canal de
# contact préféré »).
SUBJECT_STOP = frozenset({"preference", "prefere", "preferee", "prefer", "preferred", "favori", "favorite"})


def same_subject(title_a: str, title_b: str) -> bool:
    """Deux titres qui nomment le même sujet : mêmes mots porteurs, mots de catégorie retirés (aucune tolérance)."""
    ta, tb = tokens(title_a) - SUBJECT_STOP, tokens(title_b) - SUBJECT_STOP
    return bool(ta) and ta == tb


__all__ = ["equivalent", "normalize", "same_subject", "tokens"]
