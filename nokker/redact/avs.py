"""Numéro AVS/AHV suisse dans ce que voit le modèle (résultats d'outils) — docs/INVARIANTS.md §1.5.

Appelé par `DeterministicInvariants.redact_result`. Le détecteur de motif reste celui de `traces/pii.py` (journaux) et de
`redact/rules.py` (appareil) : la somme de contrôle est `rules.avs_ean13`, réutilisée telle quelle. Ce module ne fait
que décider QUOI masquer, puis remplace par une poignée `[secret:avs-…]` (le marqueur déjà utilisé pour tout motif de
`qa/secrets.py` : aucun texte nouveau pour le modèle).

Règle :
  - forme à points `756.dddd.dddd.dd` (points aux trois endroits) : masquée, somme de contrôle valide ou non (une faute de
    frappe d'un numéro AVS reste une donnée personnelle) ;
  - toute autre forme (13 chiffres collés, ou séparés par espaces / tirets / points mélangés) : masquée SEULEMENT si le
    préfixe est `756` ET la somme EAN-13 est valide. Un horodatage en millisecondes ou un identifiant à 13 chiffres n'est
    donc jamais masqué (préfixe) ;
  - jamais au milieu d'un nombre : ni lettre/chiffre/« _ » collés avant ou après, ni décimale (`…7897.5`, `1.7561…`).
Les téléphones et les IBAN sans pays ne sont pas traités ici.
"""
from __future__ import annotations

import re
from typing import Any

from nokker.qa.secrets import _handle, _type_name
from nokker.redact.rules import avs_ean13

_SEP = r"[ \t  .\-]?"
_AVS_FORM_RE = re.compile(
    rf"(?<!\w)(?<!\d[.,])756{_SEP}\d{{4}}{_SEP}\d{{4}}{_SEP}\d{{2}}(?!\w)(?![.,]?\d)"
)
_DOTTED_RE = re.compile(r"756\.\d{4}\.\d{4}\.\d{2}")


def mask_avs(text: str) -> tuple[str, list[str]]:
    """`text` avec les numéros AVS remplacés par une poignée ; la liste des poignées posées (sans doublon)."""
    if not text or "756" not in text:
        return text, []
    names: list[str] = []

    def sub(m: re.Match[str]) -> str:
        raw = m.group(0)
        if not (_DOTTED_RE.fullmatch(raw) or avs_ean13(raw)):
            return raw
        name = _type_name("avs", re.sub(r"\D", "", raw))
        if name not in names:
            names.append(name)
        return _handle(name)

    return _AVS_FORM_RE.sub(sub, text), names


def mask_avs_obj(obj: Any) -> tuple[Any, list[str]]:
    """Idem sur toutes les chaînes (valeurs) d'une structure JSON ; les clés ne bougent pas."""
    names: list[str] = []

    def walk(x: Any) -> Any:
        if isinstance(x, str):
            out, n = mask_avs(x)
            names.extend(v for v in n if v not in names)
            return out
        if isinstance(x, list):
            return [walk(v) for v in x]
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        return x

    return walk(obj), names
