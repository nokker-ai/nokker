"""Texte streamé d'UN appel modèle, caviardé avant de sortir (P1, 19.10.2026).

Règle : jamais un secret en clair dans le flux. Le provider remet des morceaux BRUTS (`feed`) ; on caviarde
le texte CUMULÉ de l'appel avec `Invariants.scrub_output` (un secret coupé entre deux morceaux est donc vu
entier), et on n'émet que ce qui est sûr :

- **retenue** : les `holdback` derniers caractères du texte caviardé ne sortent pas tant que l'appel n'est pas
  fini (≈ 80, relevé à 2 × le plus long secret connu du coffre : la variante « espacée » d'une valeur connue
  fait jusqu'à 2 × sa longueur) ;
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

    def __init__(self, scrub: Callable[[str], str], sink: TurnStream, holdback: int = DEFAULT_HOLDBACK) -> None:
        self._scrub = scrub
        self._sink = sink
        self._holdback = holdback
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
        end = len(scrubbed) - self._holdback
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


__all__ = ["DEFAULT_HOLDBACK", "ScrubbedDeltas", "holdback_for"]
