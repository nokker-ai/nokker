"""Motif donné par la personne avec le REFUS d'une carte (`reason` des routes de décision, hôte ou relais).

Le motif est une DONNÉE de l'hôte, jamais une consigne : il est borné (500 caractères, une erreur 400 au-delà, jamais de
troncature silencieuse), caviardé (secrets, numéros de carte), mis sur UNE ligne, ses balises du noyau sont neutralisées
(`[Approbation]`, `[Environnement]`, `[Date et heure actuelles]`…), puis encadré comme un commentaire dans le texte vu du
modèle (`model_texts.CARD_DECLINED_REASON`). Sur une approbation il est ignoré, sans erreur."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nokker.qa.untrusted import inline, neutralize

MAX_REASON = 500
# Guillemets de l'encadrement dans les quatre langues : un motif ne peut pas refermer le cadre pour écrire « hors » commentaire.
_QUOTES = str.maketrans({c: "'" for c in "«»“”„‟‹›\""})


class BadReason(ValueError):
    """`reason` invalide : le code HTTP est 400 `bad_reason`, le message cite la limite."""


def parse_reason(raw: Any) -> str | None:
    """Le champ `reason` brut d'un refus : None si absent ou vide ; `BadReason` si ce n'est pas une chaîne ou si elle dépasse
    `MAX_REASON` caractères."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise BadReason("reason : texte attendu")
    if len(raw) > MAX_REASON:
        raise BadReason(f"reason : {MAX_REASON} caractères au plus")
    return raw if raw.strip() else None


def clean_reason(raw: Any, scrub: Callable[[str], str] | None = None) -> str | None:
    """Motif prêt à être gardé et montré : caviardé, balises du noyau neutralisées, une ligne, au plus `MAX_REASON`.
    Idempotent. None si rien ne reste (ou si `raw` n'est pas une chaîne)."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw[: MAX_REASON * 2]
    if scrub is not None:
        text = scrub(text)
    text = " ".join(inline(neutralize(text), MAX_REASON).translate(_QUOTES).split())
    return text or None


__all__ = ["MAX_REASON", "BadReason", "clean_reason", "parse_reason"]
