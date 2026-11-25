"""« Continuer dans le cloud » : un fil web amorcé par la copie du miroir d'un fil Lokkal (docs/LOKKAL-PHONE.md §1.2
point 3), `POST /v1/threads {title?, seed_from: {machine_id, thread_id}}` → `201 {thread_id, title}`.

- **Propriétaire seulement** : le compte `us_…` propriétaire du locataire (celui dont les machines poussent les copies
  `lokkal-<machine>-<fil>`), sinon 403 `owner_only`. Copie absente : 404 `not_mirrored`.
- Le nouveau fil reçoit, CÔTÉ SERVEUR, les 40 derniers messages de la copie (20 tours : texte de la personne et
  réponse), passés une seconde fois au caviardage du noyau (celui du miroir). Aucun appel d'outil, aucun en-tête de la
  machine (qui peut porter son rappel mémoire), aucune image : seulement le texte des échanges.
- Le premier tour copié porte une ligne de contexte, vue par le modèle dans le bloc `[Contexte reçu]` (format
  docs/CONTEXT-FORMAT.md) : `SEED_LINE` / `SEED_LINE_UNTITLED`, textes relus ; le titre et le nom de la machine (des
  valeurs de tiers) passent par `qa/untrusted.inline()` avant la coupe.
- La copie du miroir reste telle quelle (lecture seule) ; le nouveau fil est un fil web ordinaire, à son propriétaire.
"""
from __future__ import annotations

import re
from typing import Any

from nokker.cloud.mirror import mirror_thread
from nokker.redact.markers import unmark

SEED_MESSAGES = 40  # messages (user + assistant) : 20 tours
# Textes vus par le modèle, relus et corrigés avant fusion (06.11.2026), mot pour mot ; empreinte figée par un test.
SEED_LINE = "suite de « {title} » (session sur {machine}) ; messages recopiés sans appels d'outil ni pièces jointes"
SEED_LINE_UNTITLED = "suite d'une session sur {machine} ; messages recopiés sans appels d'outil ni pièces jointes"
TITLE_DEFAULT = "suite de {title}"
TITLE_UNTITLED = "suite d'une session sur {machine}"
UNLINKED = "une machine déliée"
_MACHINE = re.compile(r"^mc_[0-9a-f]{16}$")
_THREAD = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")


class SeedError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code = status, code


def _clean(v: Any, n: int) -> str:
    s = "".join(ch if ch.isprintable() else " " for ch in str(v or ""))
    return " ".join(s.split())[:n]


def templates_sha256() -> str:
    """Empreinte des gabarits vus par le modèle (JSON trié) : figée par un test, à changer avec une relecture."""
    import hashlib
    import json

    blob = json.dumps({"SEED_LINE": SEED_LINE, "SEED_LINE_UNTITLED": SEED_LINE_UNTITLED, "UNLINKED": UNLINKED},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _model(value: str, n: int) -> str:
    """Valeur d'un tiers (titre d'un fil, nom de machine) dans un texte du modèle : `qa/untrusted.inline()` AVANT la
    coupe (ni faux « [/Contexte] », ni faux en-tête « [Bloc] », ni balise), puis une ligne de `n` caractères au plus."""
    from nokker.qa.untrusted import inline

    return _clean(inline(value), n)


def _origin(crew: Any, links: Any, account: str, tenant: str, machine_id: str, thread_id: str) -> tuple[str, str]:
    """(nom de la machine, titre du fil d'origine), BRUTS : le nom des machines du compte, le titre du deck (déjà sous
    la règle du miroir). Machine déliée : '' ; titre inconnu : ''. Nettoyés ensuite selon leur lecteur."""
    name, title = "", ""
    try:
        for m in links.machines_with_load(account, tenant) if links is not None else []:
            if m.id == machine_id:
                name = str(m.name or "")
    except Exception:  # noqa: BLE001
        pass
    try:
        decks = crew.deck_store.decks_of(tenant, [machine_id]) if crew is not None else {}
        deck = (decks.get(machine_id) or ({}, 0))[0]
        for row in list(deck.get("threads") or []) + list(deck.get("sessions") or []):
            if row.get("thread_id") == thread_id and row.get("title"):
                title = str(row["title"])
                break
    except Exception:  # noqa: BLE001
        pass
    return name, title


def seed_thread(*, hub: Any, store: Any, sender: str, tenant: str, owner: str | None, body: dict[str, Any],
                links: Any = None, crew: Any = None) -> dict[str, Any]:
    from nokker.core.turn import InputCheck, Turn
    from nokker.tenancy.context import tenant_scope

    seed = body.get("seed_from")
    if not isinstance(seed, dict):
        raise SeedError(400, "bad_request", "seed_from : {machine_id, thread_id}")
    machine_id, local = str(seed.get("machine_id") or ""), str(seed.get("thread_id") or "")
    if not _MACHINE.match(machine_id) or not _THREAD.match(local):
        raise SeedError(400, "bad_request", "seed_from : machine_id (mc_…) et thread_id attendus")
    if owner is None or owner != sender:
        raise SeedError(403, "owner_only", "seul le propriétaire du compte continue une session de ses machines")
    copy = mirror_thread(tenant, machine_id, local)
    with tenant_scope(tenant):
        turns = store.history(copy, SEED_MESSAGES // 2) if store is not None else []
    if not turns:
        raise SeedError(404, "not_mirrored", "aucune copie en miroir de cette session")
    raw_machine, raw_title = _origin(crew, links, sender, tenant, machine_id, local)
    # vus par l'utilisateur (titre du fil) : inchangés
    machine, origin_title = _clean(raw_machine, 60) or UNLINKED, _clean(raw_title, 80)
    given = _clean(body.get("title"), 200)
    title = given or (TITLE_DEFAULT.format(title=origin_title) if origin_title
                      else TITLE_UNTITLED.format(machine=machine))
    # vus par le modèle : neutralisés AVANT la coupe
    m_machine, m_title = _model(raw_machine, 60) or UNLINKED, _model(raw_title, 80)
    line = SEED_LINE.format(title=m_title, machine=m_machine) if m_title \
        else SEED_LINE_UNTITLED.format(machine=m_machine)
    kernel_scrub = getattr(hub, "scrub", None) or (lambda s: s)

    def scrub(s: str) -> str:
        # Un NOUVEAU fil : les marqueurs de l'appareil sont numérotés par fil, ils ne passent pas (P9b).
        return unmark(kernel_scrub(s))

    with tenant_scope(tenant):
        t = hub.create_thread(title[:200], sender)
        for i, past in enumerate(turns):
            store.append(Turn(thread_id=t.id, index=i, channel="api", sender=sender,
                              input=InputCheck(text=scrub(past.input.text or ""), redacted=list(past.input.redacted)),
                              reply=scrub(past.reply or ""), injected=[line] if i == 0 else []))
        hub.threads.update(t.id, last_snippet=scrub(turns[-1].reply or ""))
    return {"thread_id": t.id, "title": t.title}


__all__ = ["SEED_LINE", "SEED_LINE_UNTITLED", "SEED_MESSAGES", "SeedError", "TITLE_DEFAULT", "UNLINKED", "seed_thread",
           "templates_sha256"]
