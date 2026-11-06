"""Texte streamé d'UN appel modèle, caviardé avant de sortir (P1, 19.10.2026).

Règle : jamais un secret en clair dans le flux. Le provider remet des morceaux BRUTS (`feed`) ; on caviarde
le texte CUMULÉ de l'appel avec `Invariants.scrub_output` (un secret coupé entre deux morceaux est donc vu
entier), et on n'émet que ce qui est sûr :

- **retenue** : les `holdback` derniers caractères du texte caviardé ne sortent pas tant que l'appel n'est pas
  fini (80), PLUS la plus longue fin du texte qui est un DÉBUT propre d'une valeur connue ou d'une variante que le
  scrubber reconnaît (exacte, base64 standard ou URL-safe avec ou sans `=`, espacée par des espaces ou tabulations en
  nombre quelconque) — `secret_tail`. Avant le 05.11 : 2 × le plus long secret connu, partout, si bien qu'une réponse
  plus courte que la retenue (≈ 250 caractères sur un Lokkal lié, ≈ 800 avec une clé SSH au coffre) ne sortait jamais
  en flux, et que l'espacement par plusieurs blancs n'était pas couvert ;
- **mot entier** : la coupe recule jusqu'au dernier blanc — un jeton (clé, mot de passe, motif d'entropie)
  n'est émis qu'une fois TERMINÉ, quand le verdict du scrubber sur lui ne peut plus changer ;
- **bloc PEM ouvert** : rien après un `-----BEGIN` sans son `-----END` ;
- **divergence** : si le caviardage du cumul ne commence plus par ce qui est déjà parti (un motif reconnu
  tard a réécrit une partie émise), on n'émet PLUS rien pour cet appel (`dead`). La réponse finale du tour,
  caviardée en entier, fait foi de toute façon.

On n'émet jamais la retenue en fin d'appel : le dernier chunk du tour porte la réponse finale (après la porte
bascule), c'est elle qui complète le texte.
"""
from __future__ import annotations

import re
from collections.abc import Callable

from nokker.core.ports import TurnStream

DEFAULT_HOLDBACK = 80
_WS = re.compile(r"\s")
# Blancs que la variante « espacée » du scrubber insère entre les caractères (`qa/secrets.py::redact`, étape 1c).
_SPACING = str.maketrans("", "", " \t")
_VARIANTS: dict[str, list[tuple[str, list[int]]]] = {}  # valeur connue → [(variante sans blancs, table KMP)]
_VARIANTS_MAX = 4096


def _failure(p: str) -> list[int]:
    f = [0] * len(p)
    k = 0
    for i in range(1, len(p)):
        while k and p[i] != p[k]:
            k = f[k - 1]
        if p[i] == p[k]:
            k += 1
        f[i] = k
    return f


def _variants(value: str) -> list[tuple[str, list[int]]]:
    """Variantes d'une valeur connue que le scrubber caviarde (exacte, base64), blancs `[ \t]` retirés : comparer sans
    blancs couvre aussi la variante espacée, et retient plutôt plus que moins (sur-ensemble, jamais l'inverse)."""
    got = _VARIANTS.get(value)
    if got is None:
        from nokker.qa.secrets import _b64_variants

        seen: list[str] = []
        for w in (value, *_b64_variants(value)):
            w = w.translate(_SPACING)
            if w and w not in seen:
                seen.append(w)
        got = [(w, _failure(w)) for w in seen]
        if len(_VARIANTS) >= _VARIANTS_MAX:
            _VARIANTS.clear()
        _VARIANTS[value] = got
    return got


def _longest_tail_prefix(tail: str, pat: str, fail: list[int]) -> int:
    """Plus grand k < len(pat) tel que `tail` finit par pat[:k] (automate KMP, linéaire)."""
    k = 0
    for ch in tail:
        while k and (k == len(pat) or pat[k] != ch):
            k = fail[k - 1]
        if pat[k] == ch:
            k += 1
    if k == len(pat):  # la variante entière est là : le scrubber l'a déjà remplacée ; on retient sa fin propre
        k = fail[k - 1]
    return k


def secret_tail(text: str, known: object) -> int:
    """Caractères à retenir en fin de `text` (déjà caviardé) : depuis le début de la plus longue fin qui est un DÉBUT
    propre d'une valeur connue ou d'une de ses variantes, blancs `[ \t]` ignorés. 0 si aucune, ou pas de coffre."""
    if not text or known is None:
        return 0
    try:
        items = known.items_by_length_desc()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — coffre illisible : la retenue de base reste
        return 0
    current = {value for _n, value in items if value}
    for gone in [v for v in _VARIANTS if v not in current]:  # coffre changé (rotation, suppression) : rien de l'ancien
        _VARIANTS.pop(gone, None)
    variants = [v for value in current for v in _variants(value)]
    if not variants:
        return 0
    span = max(len(w) for w, _f in variants) - 1
    pos: list[int] = []  # positions des derniers caractères hors blancs `[ \t]`, de la fin vers le début
    i = len(text) - 1
    while i >= 0 and len(pos) < span:
        if text[i] not in " \t":
            pos.append(i)
        i -= 1
    if not pos:
        return 0
    stripped = "".join(text[j] for j in reversed(pos))
    best = 0
    for w, fail in variants:
        m = len(w) - 1
        if m <= 0:
            continue
        k = _longest_tail_prefix(stripped[-m:], w, fail)
        if k:
            best = max(best, len(text) - pos[k - 1])
    return best


def holdback_for(invariants: object, base: int = DEFAULT_HOLDBACK) -> int:
    """Retenue pour ces invariants : `base`, relevée à 2 × le plus long secret connu (+8) si le coffre est lisible."""
    known = getattr(invariants, "known", None)
    longest = 0
    try:
        for _name, value in known.items_by_length_desc():  # type: ignore[union-attr]
            longest = len(value)
            break
    except Exception:  # noqa: BLE001 — pas de coffre lisible : retenue de base
        longest = 0
    return max(base, 2 * longest + 8)


class ScrubbedDeltas:
    """Adaptateur `on_delta` du provider → `TurnStream`, pour UN appel modèle."""

    # Caviarder le cumul coûte O(n) (regex du coffre comprises) : on ne le refait qu'après `min_step`
    # caractères bruts de plus — le flux reste fluide, la charge reste linéaire en pratique.
    min_step = 24

    def __init__(self, scrub: Callable[[str], str], sink: TurnStream, holdback: int = DEFAULT_HOLDBACK,
                 known: object = None) -> None:
        self._scrub = scrub
        self._sink = sink
        self._holdback = holdback
        self._known = known  # valeurs connues (`invariants.known`) : retenue en plus sur un début de secret
        self._raw = ""
        self._scrubbed_at = 0  # longueur brute au dernier caviardage (on ne recaviarde pas à chaque jeton)
        self.emitted = ""
        self.dead = False

    def feed(self, piece: str) -> None:
        if not piece:
            return
        self._raw += piece
        if self.dead or len(self._raw) - self._scrubbed_at < self.min_step:
            return
        self._scrubbed_at = len(self._raw)
        scrubbed = self._scrub(self._raw)
        if not scrubbed.startswith(self.emitted):
            self.dead = True  # le caviardage a réécrit une partie déjà émise : plus rien pour cet appel
            return
        end = len(scrubbed) - self._holdback - secret_tail(scrubbed, self._known)
        pem = scrubbed.rfind("-----BEGIN")
        if pem != -1 and "-----END" not in scrubbed[pem:]:
            end = min(end, pem)
        # Recule jusqu'au dernier blanc (inclus) : jamais un jeton à moitié.
        while end > len(self.emitted) and not _WS.match(scrubbed[end - 1]):
            end -= 1
        if end <= len(self.emitted):
            return
        out = scrubbed[len(self.emitted):end]
        self.emitted += out
        self._sink.delta(out)

    def discard(self) -> None:
        """Le texte de cet appel n'est pas la réponse : le client vide ce qu'il a reçu (si quelque chose est parti)."""
        if self.emitted:
            self._sink.reset()
        self.emitted = ""
        self.dead = True


__all__ = ["DEFAULT_HOLDBACK", "ScrubbedDeltas", "holdback_for", "secret_tail"]
