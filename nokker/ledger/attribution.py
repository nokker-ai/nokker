"""Attribution d'une dépense à un projet d'équipe (docs/LOKKAL.md §7.15, budget de projet).

Le noyau pose l'étiquette pour la durée d'un tour rattaché à un projet (`Kernel.run_turn`, via le budget de projet) ;
le ledger la lit à chaque appel modèle et la range avec la ligne (`project_id`, `project_tenant`, `account`). Le budget
se lit ensuite dans le ledger : aucun second compteur. L'étiquette voyage dans une ContextVar, comme le locataire :
un sous-agent lancé dans le tour (pool avec `submit_in_context`) dépense pour le même projet.
"""
from __future__ import annotations

import contextlib
import contextvars
import dataclasses
from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class SpendTag:
    project_id: str | None
    project_tenant: str | None  # locataire du PROJET (son propriétaire) : un id de projet ne vaut qu'avec lui
    account: str  # le compte qui dépense (l'expéditeur du tour, le propriétaire de l'agent)
    # Foyer (lot X23, nokker/household/quota.py) : tout tour d'un membre compte au quota commun, qu'il soit ou non
    # rattaché à un projet d'équipe. Même étiquette, deux rattachements possibles.
    household_id: str | None = None
    household_tenant: str | None = None


def merge_tags(project: SpendTag | None, household: SpendTag | None) -> SpendTag | None:
    """L'étiquette d'un tour : celle du projet (budget) et/ou celle du foyer (quota), pour le même compte."""
    if project is None or household is None:
        return project or household
    return dataclasses.replace(project, household_id=household.household_id,
                               household_tenant=household.household_tenant)


_tag: contextvars.ContextVar[SpendTag | None] = contextvars.ContextVar("nokker_spend_tag", default=None)


def current_tag() -> SpendTag | None:
    return _tag.get()


@contextlib.contextmanager
def spend_tag(tag: SpendTag | None) -> Iterator[SpendTag | None]:
    """Étiquette des appels du bloc. `None` garde l'étiquette courante (un fil de sous-agent ne la retire pas)."""
    if tag is None:
        yield _tag.get()
        return
    token = _tag.set(tag)
    try:
        yield tag
    finally:
        _tag.reset(token)


__all__ = ["SpendTag", "current_tag", "merge_tags", "spend_tag"]
