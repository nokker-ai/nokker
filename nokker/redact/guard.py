"""Garde `masked_value` (docs/LOKKAL-PHONE.md §4.3.2, lot P9b) : un marqueur ne sort jamais du fil.

Un marqueur `⟦CODE-n⟧` n'est pas la valeur, et il est numéroté PAR FIL : relu ailleurs, ⟦PHONE-1⟧ désignerait une autre
valeur, ou aucune. L'appel qui l'emporterait hors du fil est refusé AVANT toute porte (provenance, invariants, carte
d'approbation) et toute exécution, sans carte : rien à approuver, la valeur n'existe pas côté noyau. Deux refus :

  - `REFUSAL` : effet externe — envoi (`SEND_EXTERNAL`, `SEND_BULK`), paiement (`SPEND`), écriture ailleurs ou
    suppression (`DESTRUCTIVE`), saisie dans une page ou un service tiers même classée `WRITE`
    (`nokker/tools/effects.py::third_party_input`) ;
  - `PERSIST_REFUSAL` : l'appel garderait le marqueur au-delà du fil — tout `WRITE` sauf ce qui reste dans le fil
    (`delegate` : sous-agent sur `<fil>/sa-<id>`, dont chaque appel repasse ici), et les outils classés `READ` qui
    emportent leur texte ailleurs (`nokker/tools/effects.py::persists_beyond_thread`).

Effet de CET appel : `nokker/core/ports.py::call_effect` (sans spec : `WRITE`, défaut prudent). `READ` et `SECRET`
(lecture d'un secret) passent : leur résultat reste dans le fil.
"""
from __future__ import annotations

from typing import Any

from nokker.core import model_texts as mt
from nokker.core.ports import call_effect
from nokker.core.turn import Effect
from nokker.redact.markers import markers_in
from nokker.tools.effects import persists_beyond_thread, third_party_input

EXTERNAL_EFFECTS = frozenset({Effect.SEND_EXTERNAL, Effect.SEND_BULK, Effect.SPEND, Effect.DESTRUCTIVE})

RULE = "masked_value"

# Textes vus par le modèle, validés mot pour mot (empreintes figées dans tests/unit/redact/test_device_redact.py).
REFUSAL = mt.MASKED_REFUSAL["fr"]  # figés ; langue du tour : `model_texts`
PERSIST_REFUSAL = mt.MASKED_PERSIST_REFUSAL["fr"]


def refusal(name: str, spec: Any, arguments: Any) -> str | None:
    """Texte du refus si l'appel `name(arguments)` porte un marqueur et l'emporterait hors du fil ; None sinon."""
    found = markers_in(arguments)
    if not found:
        return None
    effect = call_effect(spec, arguments if isinstance(arguments, dict) else {})
    # Ordre (aligné sur le faux noyau de l'app) : saisie chez un tiers, puis persistance AVANT l'envoi — un appel qui
    # relève des deux (`calendar_create` avec invités : outil WRITE, appel SEND_EXTERNAL) rend le texte de persistance.
    if third_party_input(name, spec, effect):
        template = mt.pick(mt.MASKED_REFUSAL)
    elif persists_beyond_thread(name, effect, getattr(spec, "effect", None)):
        template = mt.pick(mt.MASKED_PERSIST_REFUSAL)
    elif effect in EXTERNAL_EFFECTS:
        template = mt.pick(mt.MASKED_REFUSAL)
    else:
        return None
    from nokker.qa.untrusted import inline

    return template.format(outil=inline(name, 120), marqueurs=", ".join(found))


__all__ = ["EXTERNAL_EFFECTS", "PERSIST_REFUSAL", "REFUSAL", "RULE", "refusal"]
