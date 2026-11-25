"""Estimation des jetons — UNE fonction pour tout le noyau (`estimate_tokens(text, kind=None)`).

Pourquoi : `len(texte) // 4` (l'ancienne règle) vaut pour de la prose anglaise. Sur le français accentué, le JSON,
les UUID et les sorties numériques, le tokenizer réel compte 1,5 à 2 fois plus de jetons : mesure NDyGen (18.11.2026,
Router, 1er appel d'une reprise importée) — la fenêtre estimée « 8 508 » valait 16 331 jetons réels (×1,9). Points de
référence (octets par jeton comptés par le Router) :

    messages (49 user + 54 assistant + 6 tool_result JSON/UUID)  35 367 o = 16 331 jetons  2,17 o/jeton
      dont les 6 tool_result seuls                                 24 912 o = 12 729 jetons  1,96 o/jeton
    prompt hôte (prose française, markdown)                        25 657 o =  7 296 jetons  3,52 o/jeton
    schémas d'outils (JSON)                                        18 359 o =  5 350 jetons  3,43 o/jeton
    invariants du noyau (prose)                                     1 269 o =    327 jetons  3,88 o/jeton
    mémoire de session (ids, dates, noms propres)                   1 408 o =    483 jetons  2,92 o/jeton

Deux niveaux :
  1. classes de contenu (déterministe, sans état) : le texte est mesuré par classe de caractères (lettres, lettres
     accentuées, chiffres, ponctuation/symboles, blobs hexadécimaux et UUID) et chaque classe a son coût par caractère ;
     l'espace n'en coûte pas (il s'accole au mot suivant). `kind` force un ratio fixe (octets/jeton) quand l'appelant
     connaît la nature du texte : « prose », « fr », « json », « tool », « code », « mixed » ;
  2. ratio auto-calibré par palier (`TokenCalibrator`, optionnel, ÉTEINT par défaut : `ContextBudgets.calibrate_tokens`) :
     moyenne mobile bornée [1,0 ; 2,5] du rapport jetons réels (`prompt_tokens` rendus par l'amont) / jetons estimés de la
     MÊME requête ; en mémoire du processus seulement. Il rend la fenêtre non déterministe d'un processus à l'autre : à
     n'allumer que pour un déploiement qui préfère la justesse à la stabilité du cache entre redémarrages.

Les budgets (`ContextBudgets`) gardent leurs valeurs nominales : elles s'interprètent maintenant avec CET estimateur
(12 000 = environ 12 000 jetons réels, et non plus ≈ 6 000)."""
from __future__ import annotations

import re
import threading

# Octets par jeton, par nature de texte (valeurs de la table ci-dessus, arrondies ; `kind=None` = mesure par classes).
KIND_RATIOS: dict[str, float] = {
    "prose": 3.5,   # prose latine, markdown (prompt hôte, invariants)
    "fr": 3.1,      # français accentué dense (résumés, messages d'utilisateur)
    "json": 2.6,    # JSON de structure (schémas d'outils, arguments d'appel)
    "tool": 2.0,    # sorties d'outils numériques / UUID / JSON de données
    "code": 2.6,    # code source
    "mixed": 2.8,   # défaut quand on ne sait rien
}
DEFAULT_RATIO = KIND_RATIOS["mixed"]

# Jetons par caractère de chaque classe (calage sur les 5 points de référence, voir tests/unit/context/test_tokens.py).
_COST_LETTER = 1 / 3.1    # lettre ASCII
_COST_ACCENT = 1 / 1.5    # lettre accentuée / non ASCII : sépare souvent le mot en deux jetons
_COST_DIGIT = 1 / 1.5     # chiffre
_COST_PUNCT = 1 / 1.9     # ponctuation, symbole, séparateur JSON
_COST_NEWLINE = 0.25      # saut de ligne (les espaces, eux, ne coûtent rien)
_COST_HEX = 1 / 2.0       # blob hexadécimal ≥ 8 car. contenant un chiffre (UUID, sha, ids) : ~2 car./jeton

_HEX_BLOB = re.compile(r"(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{8,}", re.IGNORECASE)
_DIGITS = "0123456789"
_ASCII_LETTERS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_DEL_DIGITS = str.maketrans("", "", _DIGITS)
_DEL_LETTERS = str.maketrans("", "", _ASCII_LETTERS)
_DEL_SPACE = str.maketrans("", "", " \t\r\n")
_PUNCT_ASCII = "".join(c for c in map(chr, range(33, 127)) if not c.isalnum())
_KEEP_PUNCT = str.maketrans("", "", "".join(c for c in map(chr, range(0, 128)) if c not in _PUNCT_ASCII))


def _tokens_by_class(text: str) -> float:
    n = len(text)
    hexn = 0
    if n >= 8:
        blobs = _HEX_BLOB.findall(text)
        if blobs:
            hexn = sum(len(b) for b in blobs)
            text = _HEX_BLOB.sub("", text)
            n = len(text)
    nl = text.count("\n")
    digits = n - len(text.translate(_DEL_DIGITS))
    letters_ascii = n - len(text.translate(_DEL_LETTERS))
    punct = len(text.translate(_KEEP_PUNCT))
    nospace = len(text.translate(_DEL_SPACE))
    other = max(0, nospace - digits - letters_ascii - punct)  # lettres accentuées, symboles Unicode, CJK, emoji
    return (letters_ascii * _COST_LETTER + other * _COST_ACCENT + digits * _COST_DIGIT + punct * _COST_PUNCT
            + nl * _COST_NEWLINE + hexn * _COST_HEX)


class TokenCalibrator:
    """Ratio réel/estimé par palier : moyenne mobile exponentielle bornée [1,0 ; 2,5]. Mémoire du processus seulement."""

    LOW, HIGH, ALPHA = 1.0, 2.5, 0.3

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ratio: dict[str, float] = {}
        self._last: str | None = None

    def observe(self, tier: str, estimated: int, actual: int) -> None:
        """`estimated` = estimation de la requête ENTIÈRE par `estimate_tokens` (sans calibration), `actual` =
        `prompt_tokens` compté par l'amont. Une observation vide ou absurde est ignorée."""
        if estimated <= 0 or actual <= 0:
            return
        r = min(self.HIGH, max(self.LOW, actual / estimated))
        with self._lock:
            prev = self._ratio.get(tier)
            self._ratio[tier] = r if prev is None else min(self.HIGH, max(self.LOW, prev + self.ALPHA * (r - prev)))
            self._last = tier

    def factor(self, tier: str | None = None) -> float:
        """Facteur à appliquer aux estimations ; 1,0 sans observation (= ratio par défaut)."""
        with self._lock:
            key = tier if tier is not None else self._last
            return self._ratio.get(key, 1.0) if key is not None else 1.0

    def reset(self) -> None:
        with self._lock:
            self._ratio.clear()
            self._last = None


CALIBRATOR = TokenCalibrator()


def estimate_tokens(text: str, kind: str | None = None) -> int:
    """Jetons estimés de `text`. `kind` (cf. `KIND_RATIOS`) force un ratio octets/jeton ; sans `kind`, mesure par classes de
    caractères. Jamais négatif ; 0 pour un texte vide. Déterministe, sans état (la calibration est un facteur à part)."""
    if not text:
        return 0
    ratio = KIND_RATIOS.get(kind) if kind else None
    if ratio is not None:
        return max(1, round(len(text.encode("utf-8", "ignore")) / ratio))
    return max(1, round(_tokens_by_class(text)))


def estimate_chars_tokens(chars: int, kind: str | None = None) -> int:
    """Pour les appelants qui n'ont que la LONGUEUR (somme de messages) : ratio du `kind`, ou par défaut 2,8."""
    ratio = KIND_RATIOS.get(kind or "mixed", DEFAULT_RATIO)
    return max(0, round(chars / ratio))


__all__ = ["CALIBRATOR", "DEFAULT_RATIO", "KIND_RATIOS", "TokenCalibrator", "estimate_chars_tokens", "estimate_tokens"]
