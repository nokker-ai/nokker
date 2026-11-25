"""Recherche dans les fils de l'appelant : `GET /v1/threads/search?q=&limit=20` (docs/LOKKAL-PHONE.md §1.2 point 4).

Même contrat dans le noyau cloud et dans Lokkal (même code, même route). Les fils cherchés sont EXACTEMENT ceux que
l'appelant liste (`hub.list_threads`) : propriétaire ou membre, dans le locataire courant. Les ACL du foyer et des
partages valent donc ici comme partout. Il n'y a ni recherche « globale » ni fil d'un autre locataire.

Chez nokker.ai, les COPIES du miroir (`lokkal-<machine>-<fil>`, sans fil d'interface) sont cherchées aussi pour le
propriétaire du locataire seulement (le serveur passe `mirror_prefix`). Le résultat porte alors
`copy_of: {machine_id, thread_id}`.

Extraits caviardés : le texte passe par le caviardage du noyau (`hub.scrub`) AVANT d'y chercher le terme. Un terme
qui ne figure que dans la partie caviardée ne rend rien. On ne peut donc pas sonder un secret par la recherche.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from typing import Any

from nokker.threads.types import ThreadError

Q_MIN, Q_MAX = 2, 200
LIMIT_DEFAULT, LIMIT_MAX = 20, 50
SNIPPET = 160
_MIRROR = re.compile(r"^(?:[^:]+:)?lokkal-(mc_[0-9a-f]{16})-(.+)$")


def _fold_table() -> tuple[str, str]:
    """Lettres latines accentuées (U+00C0 à U+024F) → leur lettre de base minuscule, un caractère pour un : la même
    table sert à `translate()` de Postgres et à `fold()` ici, sans décaler les positions des extraits."""
    src, dst = [], []
    for cp in range(0xC0, 0x250):
        ch = chr(cp)
        base = unicodedata.normalize("NFKD", ch)[:1]
        if base.isascii() and base.isalpha() and base != ch:
            src.append(ch)
            dst.append(base.lower())
    return "".join(src), "".join(dst)


FOLD_FROM, FOLD_TO = _fold_table()
_FOLD = str.maketrans(FOLD_FROM, FOLD_TO)


def fold(text: str) -> str:
    """Sans accents ni casse, MÊME LONGUEUR que `text` (un caractère pour un)."""
    return "".join(c if len(c.lower()) != 1 else c.lower() for c in str(text or "").translate(_FOLD))


class SearchError(ThreadError):
    """Requête invalide (400, comme toute `ThreadError` des routes des fils)."""


def clean_query(q: Any, limit: Any) -> tuple[str, int]:
    text = " ".join(str(q or "").split())
    if not Q_MIN <= len(text) <= Q_MAX:
        raise SearchError(f"q : {Q_MIN} à {Q_MAX} caractères")
    try:
        n = LIMIT_DEFAULT if limit in (None, "") else int(limit)
    except (TypeError, ValueError):
        raise SearchError(f"limit : entier de 1 à {LIMIT_MAX}") from None
    if not 1 <= n <= LIMIT_MAX:
        raise SearchError(f"limit : entier de 1 à {LIMIT_MAX}")
    return text, n


def snippet(text: str, needle: str, scrub: Any = None) -> str | None:
    """Extrait d'au plus `SNIPPET` caractères autour du terme, dans le texte DÉJÀ caviardé ; None s'il n'y est pas."""
    flat = " ".join(str(text or "").split())
    if scrub is not None:
        flat = " ".join(str(scrub(flat)).split())
    at = fold(flat).find(fold(needle))
    if at < 0:
        return None
    start = max(0, at - (SNIPPET - len(needle)) // 2)
    end = min(len(flat), start + SNIPPET)
    start = max(0, end - SNIPPET)
    return ("…" if start > 0 else "") + flat[start:end] + ("…" if end < len(flat) else "")


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=UTC)).astimezone(UTC).isoformat()
    return datetime.fromtimestamp(float(v), tz=UTC).isoformat()


def thread_channel(thread_id: str, origin: dict[str, Any] | None = None, code: set[str] | frozenset[str] = frozenset()
                   ) -> str:
    """Canal d'un fil (contrat C3/C5) : `code` (session de code), `agent` (passage d'agent), `telegram` (`tg-<chat>`),
    sinon `api` (web, app, client). La même règle sert au deck v2 (nokker/local/crew.py)."""
    if thread_id in code:
        return "code"
    if (origin or {}).get("kind") == "agent":
        return "agent"
    if str(thread_id).rsplit(":", 1)[-1].startswith("tg-"):
        return "telegram"
    return "api"


def search(hub: Any, user: str | None, q: Any, limit: Any = None, *, mirror_prefix: str | None = None,
           code: set[str] | frozenset[str] = frozenset()) -> dict[str, Any]:
    """`{results: [{thread_id, title, snippet, turn_index, at, channel, copy_of?}], truncated}`, les plus récents
    d'abord (contrat C3). Un titre trouvé donne une ligne `turn_index: null`. `truncated` : il y avait plus que
    `limit`, ou la base a coupé à 2 s (les titres restent)."""
    needle, n = clean_query(q, limit)
    scrub = getattr(hub, "scrub", None)
    if scrub is None:  # caviardage du noyau, celui du miroir (`invariants.scrub_output`)
        scrub = getattr(getattr(getattr(hub, "kernel", None), "invariants", None), "scrub_output", None)
    threads = {t.id: t for t in hub.list_threads(user)}
    out: list[dict[str, Any]] = []
    truncated = False
    for t in threads.values():
        s = snippet(t.title, needle, scrub) if t.title else None
        if s is not None:
            out.append({"thread_id": t.id, "title": t.title, "snippet": s, "turn_index": None,
                        "at": _iso(t.updated_at), "channel": thread_channel(t.id, t.origin, code)})
    store = getattr(getattr(hub, "kernel", None), "store", None)
    finder = getattr(store, "search_turns", None)
    if finder is not None and (threads or mirror_prefix):
        # marge : des tours trouvés en base peuvent tomber au caviardage (le terme n'était que dans un secret)
        want = n * 3 + 1
        try:
            found = finder(list(threads), needle, want, prefix=mirror_prefix)
        except TimeoutError:
            found, truncated = [], True
        truncated = truncated or len(found) >= want
        for tid, index, at, text, reply in found:
            s = snippet(text, needle, scrub) or snippet(reply, needle, scrub)
            if s is None:
                continue
            t = threads.get(tid)
            m = _MIRROR.match(tid) if t is None else None
            if t is None and m is None:
                continue  # ni fil visible, ni copie du miroir : jamais rendu
            row: dict[str, Any] = {"thread_id": tid, "title": t.title if t is not None else None, "snippet": s,
                                   "turn_index": int(index),
                                   "at": _iso(at) or (_iso(t.updated_at) if t is not None else None),
                                   "channel": thread_channel(tid, t.origin if t is not None else None, code)
                                   if t is not None else thread_channel(m.group(2))}
            if m is not None:
                row["copy_of"] = {"machine_id": m.group(1), "thread_id": m.group(2)}
            out.append(row)
    out.sort(key=lambda r: r["at"] or "", reverse=True)
    return {"results": out[:n], "truncated": truncated or len(out) > n}


__all__ = ["FOLD_FROM", "FOLD_TO", "LIMIT_DEFAULT", "LIMIT_MAX", "Q_MAX", "Q_MIN", "SearchError", "clean_query", "fold",
           "search", "snippet", "thread_channel"]
