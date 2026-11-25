"""Parole horodatée pour l'avatar parlant (V6, docs/AVATAR.md) : l'alignement par caractère d'ElevenLabs
(`with-timestamps`) regroupé en mots, au format de TalkingHead (`speakAudio({audio, words, wtimes, wdurations})`).

Un mot = une suite de caractères sans blanc ; son début = début de son premier caractère, sa durée = fin du dernier
moins début du premier, en millisecondes entières. Un alignement absent ou incohérent donne des listes vides : l'avatar
parle alors sans synchro des lèvres (jamais d'erreur pour ça)."""
from __future__ import annotations

from typing import Any

MAX_WORDS = 2000


def words_from_alignment(al: Any) -> tuple[list[str], list[int], list[int]]:
    if not isinstance(al, dict):
        return [], [], []
    chars, starts, ends = al.get("characters"), al.get("character_start_times_seconds"), \
        al.get("character_end_times_seconds")
    if not (isinstance(chars, list) and isinstance(starts, list) and isinstance(ends, list)) \
            or not (len(chars) == len(starts) == len(ends)):
        return [], [], []
    words: list[str] = []
    wtimes: list[int] = []
    wdurations: list[int] = []
    cur, t0, t1 = "", 0.0, 0.0
    try:
        for c, a, b in zip(chars, starts, ends):
            c, a, b = str(c), float(a), float(b)
            if c.isspace():
                if cur:
                    words.append(cur)
                    wtimes.append(round(t0 * 1000))
                    wdurations.append(max(0, round((t1 - t0) * 1000)))
                cur = ""
                continue
            if not cur:
                t0 = a
            cur += c
            t1 = b
        if cur:
            words.append(cur)
            wtimes.append(round(t0 * 1000))
            wdurations.append(max(0, round((t1 - t0) * 1000)))
    except (TypeError, ValueError):
        return [], [], []
    return words[:MAX_WORDS], wtimes[:MAX_WORDS], wdurations[:MAX_WORDS]


__all__ = ["words_from_alignment"]
