"""Clôture `<untrusted-data>` des textes d'origine non fiable recopiés dans un prompt du noyau.

Même mécanisme que `nokker/attachments.py`, `nokker/home/agent.py` et `nokker/folder/tools.py` : les balises
`untrusted-data` du texte sont neutralisées (« [balise retirée] »), l'identifiant du bloc dérive du contenu (sha256,
déterministe) et la balise fermante le répète : le texte encadré ne peut pas refermer son bloc.
"""
from __future__ import annotations

import hashlib
import re

UNTRUSTED_TAG = re.compile(r"(?i)</?\s*untrusted-data")


def neutralize(text: str) -> str:
    return UNTRUSTED_TAG.sub("[balise retirée]", text or "")


_TAG = re.compile(r"</?\s*[A-Za-z][\w:-]*[^<>]*>")
_INLINE_MAP = str.maketrans({"[": "(", "]": ")", "<": "‹", ">": "›"})


def inline(value: object, limit: int = 0) -> str:
    """Une valeur d'origine non fiable recopiée DANS une raison du noyau (« REFUSÉ par le noyau (…) », motif de
    carte) : sur une ligne (retours à la ligne et caractères de contrôle → espace), balises retirées, crochets et
    chevrons neutralisés (`[` `]` → `(` `)`, `<` `>` → `‹` `›`). Elle ne peut fabriquer ni un faux « [/…] », ni un faux
    en-tête « [Bloc] », ni une balise. Une valeur sans aucun de ces caractères ressort inchangée. `limit` > 0 : coupée
    (« … »)."""
    text = _TAG.sub("", str(value if value is not None else ""))
    text = "".join(" " if (ord(c) < 32 or 0x7F <= ord(c) < 0xA0 or c in "\u2028\u2029\u0085") else c
                   for c in text).translate(_INLINE_MAP)
    if limit and len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def fence(kind: str, text: str) -> str:
    """`text` comme DONNÉE, dans un bloc `<untrusted-data kind=… id=…>` qu'il ne peut pas refermer."""
    body = neutralize(text)
    tag = hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()[:16]
    return f"<untrusted-data kind=\"{kind}\" id=\"{tag}\">\n{body}\n</untrusted-data id=\"{tag}\">"


__all__ = ["UNTRUSTED_TAG", "fence", "inline", "neutralize"]
