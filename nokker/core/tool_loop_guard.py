"""Garde-fou natif contre les boucles d'outils de LECTURE (docs/HOST-EMBED.md §14).

Mesuré sur un banc réel (Ship 27B, 3 passes) : `docs_search` ×4 à ×19 avec le même mot et un `k` qui double (10, 20, … 200000),
`ui_targets` ×15 à ×17 avec un filtre qui change à chaque appel : le résultat, lui, ne bouge JAMAIS (même texte, ou vide). Le
signal qui compte est donc « le même outil rend un résultat déjà vu (ou vide) » ; la proximité des arguments sert à repérer
les résultats presque identiques (classement qui bouge d'un score, horodatage).

Un appel est dit STAGNANT quand le résultat de la lecture est vide, ou identique (à la normalisation près) à celui d'un appel
précédent du même outil dans ce tour, ou presque identique (similarité ≥ 0,9) à celui d'un appel aux arguments voisins.
`n` = nombre d'appels stagnants de la grappe, le premier compris. À `note_after` (4 par défaut) une note est ajoutée au résultat ;
à `suspend_after` (8) l'outil est suspendu pour le reste du tour (non exécuté, texte fixe). Les outils à effet de bord, les lectures
`fresh` (journal d'un processus) et les cartes (`approval_repeat`, détecteur à part) ne sont pas concernés. Aucun argument ni
résultat n'est journalisé ni envoyé à l'hôte.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

NOTE_AFTER = 4
SUSPEND_AFTER = 8
CODE = "bad_tool_loop_guard"
ERROR = ("tool_loop_guard : absent (défaut), false (éteint) ou {\"note_after\": 2..50, \"suspend_after\": 3..200} "
         "avec suspend_after > note_after")
_KEYS = {"note_after", "suspend_after"}
_NEAR_RESULT = 0.9  # similarité de deux résultats
_RESULT_CHARS = 2000  # borne de la comparaison de résultats : déterministe et peu coûteux
_WINDOW = 8  # appels comparés par outil
_EMPTY_TEXT = re.compile(r"^\W*(no |aucun|0 |keine|nessun|not found|introuvable|nothing)[^\n]{0,100}$", re.IGNORECASE)


@dataclass(frozen=True)
class Thresholds:
    note_after: int = NOTE_AFTER
    suspend_after: int = SUSPEND_AFTER


def validate(raw: object) -> tuple[Any, str | None]:
    """(valeur, erreur). `None` = défaut ; `False` = éteint ; `True` = seuils par défaut ; `{note_after, suspend_after}` bornés
    (une clé absente prend son défaut ; suspend_after doit dépasser note_after). Le résultat est `None`, `False` ou `Thresholds`."""
    if raw is None or raw is True:
        return (None if raw is None else Thresholds()), None
    if raw is False:
        return False, None
    if not isinstance(raw, dict) or not set(raw) <= _KEYS or not raw:
        return None, ERROR
    n1, n2 = raw.get("note_after", NOTE_AFTER), raw.get("suspend_after", SUSPEND_AFTER)
    for v in (n1, n2):
        if isinstance(v, bool) or not isinstance(v, int):
            return None, ERROR
    if not (2 <= n1 <= 50 and 3 <= n2 <= 200 and n2 > n1):
        return None, ERROR
    return Thresholds(n1, n2), None


def resolve(session_value: Any, global_value: Any) -> Thresholds | None:
    """Seuils en vigueur : la session d'abord (`False` = éteint, `Thresholds`), sinon le réglage global `local.tool_loop_guard`
    déjà validé, sinon le défaut. `None` = garde éteinte."""
    for value in (session_value, global_value):
        if value is False:
            return None
        if isinstance(value, Thresholds):
            return value
    return Thresholds()


# ── normalisation ───────────────────────────────────────────────────────────────────────────────────────────────────
def _norm_text(s: str) -> str:
    return " ".join(s.casefold().split())


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def args_near(a: dict, b: dict) -> bool:
    """Mêmes clés, mêmes chaînes (casse et espaces ignorés), nombres quelconques (un `k` qui double reste la même requête),
    autres valeurs égales. Les chaînes ne sont PAS comparées « à peu près » : deux chemins (`src/m1.py`, `src/m2.py`) ou deux
    identifiants sont proches au sens des caractères et pourtant différents (mesuré : des fichiers sans rapport à 0,9 de
    similarité), et quand c'est le MOT de la requête qui change le résultat, lui, reste identique (grappe par résultat)."""
    if set(a) != set(b):
        return False
    for k, va in a.items():
        vb = b[k]
        if isinstance(va, str) and isinstance(vb, str):
            if _norm_text(va) != _norm_text(vb):
                return False
        elif _is_number(va) and _is_number(vb):
            continue
        elif json.dumps(va, sort_keys=True, default=str) != json.dumps(vb, sort_keys=True, default=str):
            return False
    return True


def _empty_json(v: Any) -> bool:
    if v is None or v is False or v == 0 or v == "":
        return True
    if isinstance(v, (list, tuple)):
        return all(_empty_json(x) for x in v)
    if isinstance(v, dict):
        return all(_empty_json(x) for x in v.values())
    return False


def is_empty_result(content: str) -> bool:
    text = (content or "").strip()
    if not text:
        return True
    if text[0] in "[{" or text in ("null", "None"):
        try:
            return _empty_json(json.loads(text))
        except ValueError:
            pass
    return bool(len(text) < 120 and _EMPTY_TEXT.match(text))


@dataclass
class _Seen:
    args: dict
    text: str


@dataclass
class ToolLoopGuard:
    """État d'UN tour. `suspended` : l'outil est-il suspendu ? `after` : compte une lecture et rend l'action à prendre."""

    limits: Thresholds
    _fps: dict[str, Counter] = field(default_factory=dict)
    _seen: dict[str, list[_Seen]] = field(default_factory=dict)
    _suspended: set[str] = field(default_factory=set)

    def suspended(self, tool: str) -> bool:
        return tool in self._suspended

    def after(self, tool: str, arguments: dict, content: str) -> tuple[str | None, int]:
        """Compte l'appel (lecture exécutée ou resservie). Rend (None | "note" | "suspend", n) ; "suspend" quand `n`
        atteint `suspend_after` : l'outil est suspendu pour les appels SUIVANTS (celui-ci a déjà son résultat)."""
        empty = is_empty_result(content)
        text = _norm_text(content or "")[:_RESULT_CHARS]
        # Empreinte du résultat normalisé ; tout résultat VIDE (y compris « aucun résultat pour 'x' », qui cite la requête)
        # forme une seule grappe par outil.
        fp = "∅" if empty else hashlib.sha256(text.encode()).hexdigest()
        counter = self._fps.setdefault(tool, Counter())
        n = 1
        if (content or "").strip():  # un contenu littéralement vide (fichier vide) ne forme une grappe que par ses arguments voisins
            counter[fp] += 1
            n = counter[fp]
        window = self._seen.setdefault(tool, [])
        near = 1 + sum(1 for s in window
                       if args_near(s.args, arguments)
                       and (difflib.SequenceMatcher(None, s.text, text).ratio() >= _NEAR_RESULT
                            if s.text and text else s.text == text))
        window.append(_Seen(dict(arguments), text))
        del window[:-_WINDOW]
        n = max(n, near)
        if n >= self.limits.suspend_after:
            self._suspended.add(tool)
            return "suspend", n
        if n >= self.limits.note_after:
            return "note", n
        return None, n


__all__ = ["CODE", "ERROR", "NOTE_AFTER", "SUSPEND_AFTER", "Thresholds", "ToolLoopGuard", "args_near",
           "is_empty_result", "resolve", "validate"]
