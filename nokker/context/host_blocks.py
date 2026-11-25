"""Blocs de contexte fournis par l'HÔTE, par session (`context_blocks` de `POST /v1/local/sessions`, docs/HOST-EMBED.md §20).

L'hôte (une application qui embarque le noyau) connaît ce que le noyau ignore : la date et l'heure de la personne, ce qu'elle a
à l'écran, ses tâches, son projet, son dépôt, ses documents, des épisodes, un rappel, la voix d'un tour. Il les donne en blocs
TYPÉS (kinds fermés) ; le noyau les met dans l'EN-TÊTE du tour (`Turn.head`), jamais dans le préfixe stable.

Cache : l'en-tête est figé au premier assemblage du tour puis rejoué à l'identique (append-only) ; un bloc n'est écrit que s'il
n'est pas déjà visible, inchangé, dans la fenêtre rejouée : un bloc stable n'est donc écrit qu'UNE fois, et `prefix_sha256` ne
bouge jamais. Le budget est appliqué à l'écriture (jamais réécrit après) ; le bloc `recall` n'est JAMAIS coupé.

Gardé EN MÉMOIRE pour le fil (comme `system_prompt`) : une ouverture sans le champ l'efface, `DELETE` aussi, un redémarrage
l'oublie ; un sous-agent n'en hérite pas."""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass

from nokker.qa.untrusted import fence

KINDS = ("date", "screen", "tasks", "project", "repo", "documents", "episodes", "recall", "voice")
# Ordre de COUPE quand le budget est dépassé : le moins précieux d'abord. `recall` n'y figure pas : jamais coupé.
COMPACT_ORDER = ("voice", "episodes", "documents", "repo", "tasks", "project", "screen", "date")
MAX_BYTES = 48 * 1024
DEFAULT_BUDGET_TOKENS = 3000
MIN_BUDGET_TOKENS = 100
MAX_BUDGET_TOKENS = 12000
CODE_KIND = "unknown_context_kind"
CODE_TYPE = "bad_context_blocks"
CODE_TOO_LARGE = "context_blocks_too_large"
CODE_BUDGET = "bad_context_blocks_budget"
MARK = "host_context:"
ELLIPSIS = "…"

# Balises de l'en-tête écrites par le noyau seul : une copie dans un bloc de l'hôte (qui porte du texte d'écran, de
# documents, de voix) devient entre parenthèses. `fence()` neutralise déjà les balises `untrusted-data`, `[Environnement]`,
# la date et `[Approbation]` ; le reste est ici (portage `[Fichier]`/`[Dossier]` de l'en-tête, rappel, contexte reçu).
_HEAD_TAGS = re.compile(
    r"\[\s*/?\s*(Pièces jointes|Fichier|Dossier|Contexte reçu|Rappel mémoire|Rappel|Épisodes pertinents|Mémoire de la session"
    r"|Mémoire|Contexte|Données importées)\b[^\]\n]*\]", re.IGNORECASE)


def _clean(text: str) -> str:
    return _HEAD_TAGS.sub(lambda m: "(" + m.group(0)[1:-1] + ")", text)


@dataclass(frozen=True)
class ContextBlocks:
    """Blocs validés (`{kind: texte}`, texte non vide) et budget en jetons (estimateur `len // 4`)."""

    blocks: tuple[tuple[str, str], ...]
    budget_tokens: int = DEFAULT_BUDGET_TOKENS


def validate(raw: object, budget: object = None) -> tuple[ContextBlocks | None, tuple[int, str, str, dict] | None]:
    """(blocs, erreur). Erreur = (statut HTTP, code, message, champs en plus). Forme : `{kind: texte}` ou liste de
    `{"kind", "text"}`. `null` : aucun. Un texte vide ou blanc retire le bloc. Kind inconnu ou doublon : 400 (avec la liste
    des kinds) ; plus de 48 Ko (UTF-8) au total : 413 ; jamais de troncature à l'entrée."""
    if raw is None:
        return None, None
    pairs: list[tuple[object, object]]
    if isinstance(raw, dict):
        pairs = list(raw.items())
    elif isinstance(raw, list):
        pairs = []
        for item in raw:
            if not isinstance(item, dict) or set(item) - {"kind", "text"} or "kind" not in item:
                return None, (400, CODE_TYPE, "context_blocks : liste d'objets {kind, text} (ou objet {kind: texte}) attendue", {})
            pairs.append((item["kind"], item.get("text")))
    else:
        return None, (400, CODE_TYPE, "context_blocks : objet {kind: texte} ou liste de {kind, text} attendu", {})
    seen: dict[str, str] = {}
    for kind, text in pairs:
        if not isinstance(kind, str) or kind not in KINDS:
            return None, (400, CODE_KIND, f"context_blocks : kind inconnu ({str(kind)[:40]!r}) ; valeurs : {', '.join(KINDS)}",
                          {"valid": list(KINDS)})
        if kind in seen:
            return None, (400, CODE_TYPE, f"context_blocks : kind {kind!r} en double", {})
        if text is None:
            text = ""
        if not isinstance(text, str):
            return None, (400, CODE_TYPE, f"context_blocks.{kind} : chaîne attendue", {})
        seen[kind] = text
    size = sum(len(t.encode("utf-8", "surrogatepass")) for t in seen.values())
    if size > MAX_BYTES:
        return None, (413, CODE_TOO_LARGE, f"context_blocks : {size} octets reçus, {MAX_BYTES} au plus (UTF-8, tous blocs)",
                      {"max_bytes": MAX_BYTES, "received_bytes": size})
    tokens = DEFAULT_BUDGET_TOKENS
    if budget is not None:
        if isinstance(budget, bool) or not isinstance(budget, int) or not MIN_BUDGET_TOKENS <= budget <= MAX_BUDGET_TOKENS:
            return None, (400, CODE_BUDGET, f"context_blocks_budget : entier entre {MIN_BUDGET_TOKENS} et {MAX_BUDGET_TOKENS} "
                          "(jetons) attendu", {})
        tokens = budget
    blocks = tuple((k, seen[k]) for k in KINDS if k in seen and seen[k].strip())
    return (ContextBlocks(blocks, tokens) if blocks else None), None


class HostBlocks:
    def __init__(self) -> None:
        self._by_thread: dict[str, ContextBlocks] = {}
        self._lock = threading.Lock()

    def set(self, thread_id: str, value: ContextBlocks | None) -> None:
        with self._lock:
            if value is None:
                self._by_thread.pop(thread_id, None)
                return
            self._by_thread[thread_id] = value
            while len(self._by_thread) > 5000:  # borne mémoire : on oublie les plus anciens fils
                self._by_thread.pop(next(iter(self._by_thread)), None)

    def clear(self, thread_id: str) -> None:
        self.set(thread_id, None)

    def exact(self, thread_id: str | None) -> ContextBlocks | None:
        """Blocs posés sur CE fil seulement (un sous-agent n'en hérite pas)."""
        if not self._by_thread:
            return None
        with self._lock:
            return self._by_thread.get(thread_id or "")


_REGISTRY = HostBlocks()


def registry() -> HostBlocks:
    return _REGISTRY


def tokens_of(text: str) -> int:
    """Même estimateur que `context.engine.estimate_tokens` (`len // 4`)."""
    return max(0, len(text) // 4)


def fit(blocks: tuple[tuple[str, str], ...], budget_tokens: int) -> list[tuple[str, str]]:
    """Textes nettoyés, ramenés au budget : on coupe d'abord les kinds de `COMPACT_ORDER` (fin du texte retirée, « … »
    ajouté, bloc entier retiré s'il ne reste rien), jamais `recall`. Déterministe : mêmes blocs, même budget, mêmes octets."""
    texts = {k: _clean(t).strip() for k, t in blocks}
    over = sum(tokens_of(t) for t in texts.values()) - budget_tokens
    for kind in COMPACT_ORDER:
        if over <= 0:
            break
        text = texts.get(kind)
        if text is None:
            continue
        cut = min(len(text), over * 4)
        keep = len(text) - cut
        if keep <= len(ELLIPSIS):
            texts.pop(kind)
            over -= tokens_of(text)
        else:
            shorter = text[: keep - len(ELLIPSIS)].rstrip() + ELLIPSIS
            over -= tokens_of(text) - tokens_of(shorter)
            texts[kind] = shorter
    return [(k, texts[k]) for k in KINDS if k in texts]


def render(blocks: ContextBlocks, visible_heads: list[str]) -> list[str]:
    """Blocs à écrire dans l'en-tête de CE tour, dans l'ordre fixe de `KINDS`. `visible_heads` : en-têtes REJOUÉS de la
    fenêtre (du plus récent au plus ancien) ; un bloc dont la dernière version visible est identique n'est pas répété."""
    out: list[str] = []
    for kind, text in fit(blocks.blocks, blocks.budget_tokens):
        fenced = fence(MARK + kind, text)
        ident = fenced.split('id="', 2)[1].split('"', 1)[0]
        marker = f'<untrusted-data kind="{MARK}{kind}" id="'
        latest = next((h for h in visible_heads if marker in h), None)
        if latest is not None and f'{marker}{ident}"' in latest:
            continue
        out.append(fenced)
    return out


__all__ = ["COMPACT_ORDER", "ContextBlocks", "DEFAULT_BUDGET_TOKENS", "KINDS", "MARK", "MAX_BYTES", "fit", "registry",
           "render", "validate"]
