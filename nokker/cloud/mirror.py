"""Mémoire en miroir web ⇄ Lokkal, côté nokker.ai (docs/LOKKAL.md §6.3, phase L5 ; première version, abonnés, derrière
le drapeau `cloud_link.mirror`).

Modèle : chaque fil a UNE maison (la machine, ou le web). L'autre côté en garde une COPIE en lecture seule :
  - poussée (machine → web) : les tours d'un fil Lokkal arrivent sous le fil `lokkal-<machine>-<fil>` du locataire,
    même indice (ajout seul, idempotent : un indice déjà présent est sauté), SANS les appels modèle (déjà comptés par
    l'inférence relayée : pas de double comptage) ;
  - tirage (web → machine) : les tours des fils web du locataire depuis un curseur `(created_at, fil, indice)`, sauf
    les copies `lokkal-*` (une machine ne retire jamais ce qu'elle a poussé).
Les fichiers du dossier de travail ne passent JAMAIS par ici (§6.3). Effacement propagé : l'effacement du compte efface
les copies (même locataire) ; une machine efface les siennes (`erase`, route `POST /v1/mirror/erase`), par la fonction
`nokker_mirror_erase` (migration 015) qui n'atteint que des copies. Les notes CortHeXis suivent dans une version
suivante (aucun locataire de nokker.ai n'a encore de notes).
"""
from __future__ import annotations

import dataclasses
from typing import Any

from nokker.store.pg import turn_from_dict
from nokker.tenancy.context import scoped_thread_id, tenant_scope

MAX_BATCH = 200


def mirror_thread(tenant: str, machine: str, local_thread: str) -> str:
    return scoped_thread_id(tenant, f"lokkal-{machine}-{local_thread}")


def push(store: Any, tenant: str, machine: str, items: list[dict[str, Any]]) -> dict[str, int]:
    """Range les tours poussés par une machine. Rend {accepted, skipped}."""
    accepted = skipped = 0
    with tenant_scope(tenant):
        for it in items[:MAX_BATCH]:
            local_thread, d = str(it["thread_id"]), dict(it["turn"])
            target = mirror_thread(tenant, machine, local_thread)
            index = int(d["index"])
            if store.next_index(target) > index:
                skipped += 1  # déjà là (renvoi après une coupure)
                continue
            d.update(thread_id=target, calls=[])
            turn = turn_from_dict(d)
            store.append(dataclasses.replace(turn, thread_id=target, index=index))
            accepted += 1
    return {"accepted": accepted, "skipped": skipped}


def erase(store: Any, tenant: str, machine: str) -> int:
    """Efface toutes les copies poussées par `machine` chez `tenant` ; rend le nombre de tours effacés."""
    with tenant_scope(tenant):
        return store.erase_mirror_copies(scoped_thread_id(tenant, f"lokkal-{machine}-"))


def pull(store: Any, tenant: str, since: str | None) -> dict[str, Any]:
    """Tours des fils web du locataire après le curseur (sous RLS en Postgres), sans les copies `lokkal-*`."""
    out, cursor = [], since
    with tenant_scope(tenant):
        rows = store.turns_after(_parse_cursor(since), MAX_BATCH * 2)
    for created, tid, idx, d in rows:
        cursor = f"{created}|{tid}|{idx}"  # avance aussi sur les copies sautées, jamais au-delà du dernier lu
        if "lokkal-" in tid:
            continue
        out.append({"thread_id": tid, "turn": d, "cursor": cursor})
        if len(out) >= MAX_BATCH:
            break
    return {"turns": out, "cursor": cursor}


def _parse_cursor(since: str | None) -> tuple[str, str, int]:
    if not since:
        return ("-infinity", "", -1)  # avant tout horodatage (Postgres) et tout rang (mémoire)
    created, tid, idx = since.split("|", 2)
    return (created, tid, int(idx))


__all__ = ["erase", "mirror_thread", "pull", "push"]
