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
`nokker_mirror_erase` (migration 015) qui n'atteint que des copies.

Notes de mémoire (lot X21, docs/LOKKAL.md §6.7) : la machine pousse le TEXTE de ses notes `both` (notes de
l'utilisateur, notes d'un projet mis en miroir) ; elles sont gardées par (machine, projet, nom) sous la RLS du
locataire (`lokkal_mirror_notes`, migration 025). Seconde barrière : une note qui se dit `local` (ou sans localité)
est refusée ici aussi, même si la machine l'envoie. L'index de recherche côté web se recalcule à partir du texte (pas
encore branché sur le rappel des sessions web).
"""
from __future__ import annotations

import dataclasses
import re
import threading
import time
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


# ─────────────────────────────── notes (X21) ───────────────────────────────

_NOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$")
_PROJECT = re.compile(r"^(u-local|p-[0-9a-f]{16})$")
MAX_NOTE_TEXT = 4000


def _clean_note(raw: Any) -> dict[str, Any] | None:
    """Note reçue d'une machine, réduite aux champs connus et bornés ; None si elle ne peut pas sortir (localité)
    ou si sa forme est fausse."""
    from nokker.core.locality import may_leave

    if not isinstance(raw, dict) or not may_leave(raw.get("locality")):
        return None
    project, name = str(raw.get("project") or ""), str(raw.get("name") or "")
    if not _PROJECT.match(project) or not _NOTE_NAME.match(name):
        return None
    scope = "user" if raw.get("scope") == "user" else "project"
    return {"project": project, "name": name, "kind": str(raw.get("kind") or "")[:20], "scope": scope,
            "locality": str(raw["locality"]), "title": str(raw.get("title") or "")[:200],
            "text": str(raw.get("text") or "")[:MAX_NOTE_TEXT], "created": str(raw.get("created") or "")[:40],
            "derived_from": str(raw.get("derived_from") or "")[:20]}


class MemoryMirrorNotes:
    """Notes en miroir (tests, dev) ; `PgMirrorNotes` (nokker/cloud/pg.py) a la même interface."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str, str], dict[str, Any]] = {}  # (locataire, machine, projet, nom) → note
        self.lock = threading.Lock()

    def put(self, tenant: str, machine: str, note: dict[str, Any], at: float) -> None:
        with self.lock:
            self.rows[(tenant, machine, note["project"], note["name"])] = {**note, "updated_at": at}

    def forget(self, tenant: str, machine: str, project: str, name: str) -> bool:
        with self.lock:
            return self.rows.pop((tenant, machine, project, name), None) is not None

    def of_tenant(self, tenant: str) -> list[dict[str, Any]]:
        with self.lock:
            return [{**v, "machine_id": k[1]} for k, v in self.rows.items() if k[0] == tenant]

    def keys(self, tenant: str, machine: str) -> list[tuple[str, str]]:
        """(projet, nom) des notes gardées pour cette machine (resynchronisation complète)."""
        with self.lock:
            return sorted((k[2], k[3]) for k in self.rows if k[0] == tenant and k[1] == machine)

    def erase(self, tenant: str, machine: str) -> int:
        with self.lock:
            keys = [k for k in self.rows if k[0] == tenant and k[1] == machine]
            for k in keys:
                del self.rows[k]
            return len(keys)


def push_notes(notes_store: Any, tenant: str, machine: str, body: Any) -> dict[str, int]:
    """Range les notes poussées par une machine ; rend {accepted, refused, forgotten}."""
    body = body if isinstance(body, dict) else {}
    accepted = refused = forgotten = 0
    now = time.time()
    for raw in list(body.get("notes") or [])[:MAX_BATCH]:
        note = _clean_note(raw)
        if note is None:
            refused += 1  # `local` ou illisible : rien n'est gardé
            continue
        notes_store.put(tenant, machine, note, now)
        accepted += 1
    for f in list(body.get("forget") or [])[:MAX_BATCH]:
        if isinstance(f, dict) and notes_store.forget(tenant, machine, str(f.get("project") or ""),
                                                      str(f.get("name") or "")):
            forgotten += 1
    return {"accepted": accepted, "refused": refused, "forgotten": forgotten}


__all__ = ["MemoryMirrorNotes", "erase", "mirror_thread", "pull", "push", "push_notes"]
