"""Calendrier familial (docs/FAMILY-CALENDAR.md) : une photo ou un PDF de planning (école, club, convocation,
horaires de déchetterie) → un BROUILLON relu par le noyau → UNE carte de relecture → l'agenda du compte.

Parcours :
1. `propose` : le modèle a lu le document (image jointe au message, PDF importé dans les Papiers du foyer, ou pièce
   jointe texte) et donne les événements ; le noyau relit chaque ligne (`review.py`), cherche les doublons dans
   l'agenda existant (`calendar_events`) et range le brouillon (révision 1). Rien n'est écrit dans l'agenda.
2. `edit` (outil, ou carte web `PATCH …/lines/{n}`) : rejeter, rétablir, garder un doublon, corriger une ligne. Chaque
   modification fait monter la révision.
3. `commit` : SEULEMENT sous une carte approuvée (portes `gates.py`, outil `family_calendar_commit` avec la révision
   montrée sur la carte : une carte posée sur la révision 3 n'écrit rien si le brouillon est passé en révision 4).
   Écrit les lignes valides dans l'agenda CalDAV du compte (aucune invitation), sinon prépare un fichier .ics.

Cloisonnement : brouillons et réglages dans le locataire du compte (`store.py`) ; l'agenda écrit et relu est TOUJOURS
celui du locataire du tour (`calendar_for(current_tenant())`). Une ligne ne porte ni agenda ni compte cible : un
document qui « demande » d'écrire chez quelqu'un d'autre ne peut que nommer une personne, relue contre le foyer.
Journaux : identifiants seulement (`fc_…`), jamais un titre ni un lieu.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from nokker.household.famcal import ics as ics_mod
from nokker.household.famcal import review as rv
from nokker.household.famcal.store import DEFAULT_TZ, Draft, Settings
from nokker.qa.untrusted import inline
from nokker.tenancy.context import current_tenant

logger = logging.getLogger("nokker.household.famcal")

MAX_PEOPLE = 12
MAX_DOC_CHARS = 60_000
ACTIONS = ("reject", "restore", "keep", "update")



def _origin_line(origin: str) -> str:
    """Ligne d'origine écrite dans l'agenda : le libellé peut être le nom d'un fichier tiers, donc sur une ligne, sans
    balise ni crochet, coupé (comme la carte, `review.MAX_SHOWN`). Relue plus tard par `calendar_events`."""
    return f"Ajouté par Nokker (calendrier familial) depuis {inline(origin, rv.MAX_SHOWN)}."


class FamcalError(ValueError):
    """Demande refusée (400 / 404 / 409 côté route, texte d'erreur côté outil), sans contenu du document."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _ctx() -> Any:
    from nokker.core.ports import ToolContext

    return ToolContext(thread_id="family-calendar", turn_index=0, channel="family-calendar")


@dataclass
class FamilyCalendar:
    store: Any  # MemoryFamcalStore | PgFamcalStore
    # Locataire → compte actif (un compte = un locataire en bêta), None sinon.
    account_of: Callable[[str], str | None]
    # Locataire → sources d'outils agenda du compte (connecteurs). Toujours appelé avec le locataire DU TOUR.
    calendars_for: Callable[[str], list[Any]] | None = None
    # Papiers du foyer (drapeau `household_paper`), pour lire un PDF importé (`document_id`) ; None sinon.
    paper: Any = None
    clock: Callable[[], float] = time.time
    new_id: Callable[[], str] = lambda: "fc_" + uuid.uuid4().hex[:16]

    # ─────────── identité, réglages ───────────

    def account(self) -> str:
        acc = self.account_of(current_tenant())
        if not acc:
            raise FamcalError("aucun compte pour ce locataire", 403)
        return acc

    def settings(self) -> Settings:
        return self.store.settings(self.account())

    def put_settings(self, body: dict[str, Any]) -> Settings:
        s = self.settings()
        if "people" in body:
            people = body.get("people")
            if not isinstance(people, list) or len(people) > MAX_PEOPLE:
                raise FamcalError(f"people : une liste d'au plus {MAX_PEOPLE} prénoms")
            s.people = [p for p in dict.fromkeys(" ".join(str(x).split())[:60] for x in people) if p]
        if "timezone" in body:
            tz = str(body.get("timezone") or "").strip()
            if not ics_mod.supported_tz(tz):
                raise FamcalError("timezone : un fuseau Europe/… (ou UTC)")
            s.timezone = tz
        self.store.put_settings(s)
        return s

    def people(self) -> list[str]:
        acc = self.account()
        own = self.store.settings(acc).people
        if own or self.paper is None:
            return own
        try:
            return list(self.paper.store.settings(acc).people)
        except Exception:  # noqa: BLE001 — réglages des papiers illisibles : pas de liste, pas de contrôle de personne
            return []

    def timezone(self) -> str:
        return self.store.settings(self.account()).timezone or DEFAULT_TZ

    def today(self, tzname: str | None = None) -> date:
        return datetime.fromtimestamp(self.clock(), ZoneInfo(tzname or self.timezone())).date()

    # ─────────── agenda du locataire du tour ───────────

    def _sources(self) -> list[Any]:
        return list(self.calendars_for(current_tenant()) or []) if self.calendars_for is not None else []

    @staticmethod
    def _has(src: Any, tool: str) -> bool:
        try:
            return any(s.name == tool for s in src.specs())
        except Exception:  # noqa: BLE001
            return False

    def reader(self) -> Any:
        return next((s for s in self._sources() if self._has(s, "calendar_events")), None)

    def writer(self) -> Any:
        return next((s for s in self._sources() if self._has(s, "calendar_create")), None)

    def target(self) -> str:
        return "l'agenda du compte" if self.writer() is not None else \
            "un fichier .ics à importer (aucun agenda CalDAV relié)"

    def _existing(self, lines: list[rv.Line], today: date) -> tuple[list[dict[str, Any]] | None, str]:
        src = self.reader()
        if src is None:
            return None, "aucun agenda relié : doublons non vérifiés"
        try:
            res = src.call("calendar_events", {"days": rv.window_days(lines, today), "calendar": "all"}, _ctx())
            if res.is_error:
                raise ValueError("lecture refusée")
            evs = json.loads(res.content).get("events") or []
        except Exception:  # noqa: BLE001 — agenda illisible : on le dit, on ne bloque pas
            return None, "agenda illisible : doublons non vérifiés"
        note = "doublons vérifiés sur un an seulement" if rv.beyond_window(lines, today) else ""
        return [e for e in evs if isinstance(e, dict)], note

    # ─────────── brouillon ───────────

    def _document(self, arguments: dict[str, Any], turn: Any) -> tuple[str | None, str]:
        """(texte du document pour relire les sources, libellé d'origine). Un PDF importé : son texte ; une pièce
        jointe texte du message : son bloc ; une image : pas de texte (la carte est la relecture)."""
        doc_id = arguments.get("document_id")
        if doc_id:
            if self.paper is None:
                raise FamcalError("document_id : les Papiers du foyer ne sont pas allumés sur ce compte")
            try:
                doc = self.paper.document(str(doc_id))
            except ValueError as e:  # PaperError (404 d'un papier d'un autre compte compris)
                raise FamcalError(str(e), getattr(e, "status", 400)) from None
            return (doc.text or None), f"« {doc.filename} »"
        attached = str(getattr(turn, "attached", "") or "")
        if attached:
            return attached[:MAX_DOC_CHARS], "pièce jointe du message"
        if getattr(turn, "images", None):
            return None, "image jointe au message"
        return None, "document lu par l'assistant"

    def propose(self, arguments: dict[str, Any], turn: Any = None) -> Draft:
        acc = self.account()
        text, origin = self._document(arguments, turn)
        tzname = self.timezone()
        today = self.today(tzname)
        try:
            lines = rv.review(arguments.get("events"), people=self.people(), document_text=text, today=today)
        except ValueError as e:
            raise FamcalError(str(e)) from None
        existing, note = self._existing(lines, today)
        rv.mark_duplicates(lines, existing)
        now = self.clock()
        d = Draft(draft_id=self.new_id(), account_id=acc, created_at=now, updated_at=now,
                  revision=1, status="open", origin=origin, lines=[x.to_json() for x in lines],
                  result={"note": note} if note else {}, timezone=tzname)
        self.store.create(d)
        logger.info("calendrier familial : brouillon %s (%d ligne(s))", d.draft_id, len(lines))
        return d

    def draft(self, draft_id: Any) -> Draft:
        d = self.store.get(str(draft_id or "")) if draft_id else None
        if d is None or d.account_id != self.account():
            raise FamcalError("brouillon introuvable", 404)
        return d

    @staticmethod
    def lines(d: Draft) -> list[rv.Line]:
        return [rv.Line.from_json(x) for x in d.lines]

    def edit(self, draft_id: Any, line_numbers: Any, action: str, fields: dict[str, Any] | None = None) -> Draft:
        d = self.draft(draft_id)
        if d.status != "open":
            raise FamcalError("brouillon déjà écrit dans l'agenda : rien à modifier", 409)
        if action not in ACTIONS:
            raise FamcalError(f"action : une de {', '.join(ACTIONS)}")
        nums = line_numbers if isinstance(line_numbers, list) else [line_numbers]
        lines = self.lines(d)
        try:
            idx = sorted({int(n) - 1 for n in nums})
        except (TypeError, ValueError):
            raise FamcalError("line : numéro de ligne (1 à n)") from None
        if not idx or any(not 0 <= i < len(lines) for i in idx):
            raise FamcalError(f"line : numéro de ligne entre 1 et {len(lines)}")
        if action == "update" and (len(idx) != 1 or not isinstance(fields, dict) or not fields):
            raise FamcalError("update : une seule ligne et des champs à corriger (fields)")
        tzname = d.timezone or DEFAULT_TZ
        for i in idx:
            line = lines[i]
            if action == "reject":
                line.rejected = True
            elif action == "restore":
                line.rejected, line.keep_duplicate = False, False
            elif action == "keep":
                line.rejected, line.keep_duplicate = False, True
            else:
                rv.revise(line, fields or {}, people=self.people(), today=self.today(tzname))
        if action == "update":
            keep = {i for i, x in enumerate(lines) if x.keep_duplicate}
            existing, note = self._existing(lines, self.today(tzname))
            rv.mark_duplicates(lines, existing)
            for i in keep:
                lines[i].keep_duplicate = True
            d.result = {"note": note} if note else {}
        expected = d.revision
        d.lines, d.revision, d.updated_at = [x.to_json() for x in lines], d.revision + 1, self.clock()
        if not self.store.save(d, expected):
            raise FamcalError("brouillon modifié en même temps : relire et recommencer", 409)
        return d

    def card(self, draft_id: Any, revision: Any) -> tuple[Draft, list[rv.Line], list[rv.Line]]:
        """(brouillon, lignes à écrire, lignes NON écrites) pour la carte ; lève si la révision n'est pas la bonne."""
        d = self.draft(draft_id)
        if d.status != "open":
            raise FamcalError("brouillon déjà écrit dans l'agenda", 409)
        try:
            rev = int(revision)
        except (TypeError, ValueError):
            raise FamcalError("revision : entier rendu par family_calendar_propose ou family_calendar_edit") from None
        if rev != d.revision:
            raise FamcalError(f"le brouillon est en révision {d.revision} (pas {rev}) : rappeler avec revision "
                              f"{d.revision}", 409)
        lines = self.lines(d)
        return d, [x for x in lines if x.writable], [x for x in lines if not x.writable]

    def commit(self, draft_id: Any, revision: Any) -> dict[str, Any]:
        """Écrit les lignes valides. Appelé SEULEMENT sous une carte approuvée (outil, `ctx.approval_id`)."""
        d, ok, bad = self.card(draft_id, revision)
        not_written = [{"line": x.index + 1, "title": x.title, "why": x.why_not()} for x in bad]
        if not ok:
            return {"written": 0, "not_written": not_written, "message": "aucune ligne valide : rien n'est écrit"}
        tzname = d.timezone or DEFAULT_TZ
        origin = _origin_line(d.origin)
        src = self.writer()
        done: list[int] = []
        failed: list[int] = []
        to_ics: list[rv.Line] = []
        for line in ok:
            if src is None:
                to_ics.append(line)
                continue
            try:
                if hasattr(src, "put_new_event"):
                    uid = ics_mod.new_uid()
                    src.put_new_event(uid, ics_mod.build([line], tzname=tzname, description=origin, uids=[uid]))
                elif line.recurrence:
                    to_ics.append(line)  # agenda sans écriture de série (connecteur relayé) : la série part en .ics
                    continue
                else:
                    start, end = ics_mod.span(line, ZoneInfo(tzname))
                    if isinstance(end, datetime):
                        a = {"start": start.isoformat(timespec="minutes"), "end": end.isoformat(timespec="minutes")}
                    else:
                        a = {"start": line.date, "end": line.date}  # journée entière : fin incluse (connecteur)
                    res = src.call("calendar_create", {"summary": line.summary(), **a,
                                                       "location": line.location or None, "description": origin},
                                   _ctx())
                    if res.is_error:
                        raise ValueError("écriture refusée")
                done.append(line.index + 1)
            except Exception:  # noqa: BLE001 — une ligne en échec n'arrête pas les autres ; rien du contenu au journal
                failed.append(line.index + 1)
        result: dict[str, Any] = {"written": len(done), "lines": done, "failed": failed,
                                  "target": "agenda du compte" if src is not None else "fichier .ics"}
        if to_ics:
            result["ics_lines"] = [x.index + 1 for x in to_ics]
            result["download"] = f"/v1/family-calendar/drafts/{d.draft_id}/ics"
            result["written"] += len(to_ics)
        expected = d.revision
        d.status, d.result, d.updated_at = "committed", {**result, "not_written": not_written}, self.clock()
        if not self.store.save(d, expected):
            raise FamcalError("brouillon modifié en même temps : rien de plus n'est écrit", 409)
        logger.info("calendrier familial : brouillon %s écrit (%d ligne(s), %d échec(s))", d.draft_id, len(done),
                    len(failed))
        return {**result, "not_written": not_written}

    def ics(self, draft_id: Any) -> str:
        """Fichier .ics des lignes rangées pour téléchargement : seulement après la carte (brouillon écrit)."""
        d = self.draft(draft_id)
        if d.status != "committed" or not d.result.get("ics_lines"):
            raise FamcalError("aucun fichier .ics : la carte n'est pas encore approuvée", 409)
        wanted = set(d.result["ics_lines"])
        lines = [x for x in self.lines(d) if x.index + 1 in wanted]
        return ics_mod.build(lines, tzname=d.timezone or DEFAULT_TZ,
                             description=_origin_line(d.origin))

    def drafts(self, status: str | None = "open") -> list[Draft]:
        return self.store.list(self.account(), status)

    @staticmethod
    def draft_json(d: Draft) -> dict[str, Any]:
        lines = [rv.Line.from_json(x) for x in d.lines]
        return {"id": d.draft_id, "revision": d.revision, "status": d.status, "origin": d.origin,
                "timezone": d.timezone, "created_at": d.created_at, "updated_at": d.updated_at,
                "lines": [{**x.to_json(), "number": x.index + 1, "text": x.text(),
                           "why_not": x.why_not() if not x.writable else ""} for x in lines],
                "result": d.result}


__all__ = ["ACTIONS", "FamcalError", "FamilyCalendar"]
