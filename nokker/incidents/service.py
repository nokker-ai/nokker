"""Operate — alerte → incident → agent (lot X18, docs/LOKKAL.md §7.18).

Une **source** (`as_…`) est une URL `POST /v1/alerts/<id>` signée par SA clé HMAC (même mécanique que le webhook d'un
agent, BUBBLES §9.5) : en-tête `X-Nokker-Signature: t=<epoch>,v1=<hex>`, `v1 = HMAC-SHA256(clé, "<t>." + corps brut)`,
`t` à ± 5 min ; une signature déjà reçue est refusée (rejeu, 409), y compris après un redémarrage (gardée en base).

Chaque alerte reçue crée ou met à jour un **incident**, regroupé par empreinte (`fingerprint` d'Alertmanager, sinon
nom + étiquettes) : un seul incident vivant par (source, empreinte). `firing` sur un incident vivant = une occurrence de
plus, pas un nouveau passage d'agent (Alertmanager renvoie le groupe toutes les `repeat_interval`) ; `resolved` le
résout (`resolved_by: "source:<nom>"`) ; une alerte qui revient après la résolution ouvre un NOUVEL incident.

Un incident ouvert déclenche les agents `alert:<nom>` par `AgentEngine.fire_event` : le corps de l'alerte entre dans
leur message comme DONNÉE non fiable (bloc `<untrusted-data>` du moteur) et un passage déclenché par un événement ne
lève JAMAIS d'écriture sans carte (`AgentRunGates.event`, garanti par le moteur, vérifié par les tests). Le moteur
publie chaque changement d'état d'un passage (`AgentsHub.run_listeners`) : l'incident en garde le fil (passages,
cartes posées et leur sort, résultat). États : `open` → `in_progress` (un passage démarre, ou un humain prend la
main : `ack`) → `resolved` (la source, ou un humain : `resolve`)."""
from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import logging
import os
import re
import secrets as _secrets
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.incidents.parse import ParsedAlert, parse_alerts
from nokker.incidents.store import BaseIncidents
from nokker.incidents.types import (
    FORMATS, LIVE_STATES, MAX_TIMELINE, STATES, AlertSource, Incident, IncidentError, IncidentNotFound,
    incident_to_json, new_incident_id, new_source_id,
)
from nokker.tenancy.context import current_tenant_or_none, tenant_scope

logger = logging.getLogger("nokker.incidents")

SIG_HEADER = "X-Nokker-Signature"
SIG_TOLERANCE_S = 300.0
# Authentification d'une source : `hmac` (défaut, corps signé, rejeu refusé) ou `bearer` sur option explicite
# (Alertmanager ne sait poser qu'un en-tête `Authorization` ; arbitrage dev du 25.10, LOKKAL §7.18.1).
AUTH_MODES = ("hmac", "bearer")
BEARER_MIN_BYTES = 32
_BEARER = re.compile(r"^\s*[Bb][Ee][Aa][Rr][Ee][Rr]\s+(\S+)\s*$")
ENC_KEY_ENV = "NOKKER_AGENTS_ENC_KEY"  # la même clé Fernet que les webhooks d'agents (X14)
MAX_SOURCES = 50
RATE_MAX, RATE_WINDOW_S = 120, 60.0  # envois signés par source et par minute
SUMMARY_CHARS = 600
_SOURCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")
_RUN_LABELS = {"queued": "en file", "running": "en cours", "waiting_approval": "attend une carte",
               "succeeded": "terminé", "failed": "en échec", "cancelled": "annulé", "skipped": "écarté"}
# Incident en cours d'ouverture dans CE fil : le moteur publie `queued` (et `skipped`) avant de rendre la main, et en
# mode synchrone tout le passage ; ces passages se rattachent à lui avant même d'être rangés dans `runs`.
_FIRING: contextvars.ContextVar[str | None] = contextvars.ContextVar("nokker_incident_firing", default=None)


def seal_key(key: str) -> str:
    enc = os.environ.get(ENC_KEY_ENV, "").strip()
    if enc:
        from cryptography.fernet import Fernet

        return "enc:" + Fernet(enc.encode()).encrypt(key.encode()).decode()
    return "raw:" + key


def open_key(stored: str | None) -> str | None:
    if not stored:
        return None
    if stored.startswith("raw:"):
        return stored[4:]
    if stored.startswith("enc:"):
        from cryptography.fernet import Fernet, InvalidToken

        enc = os.environ.get(ENC_KEY_ENV, "").strip()
        if not enc:
            return None
        try:
            return Fernet(enc.encode()).decrypt(stored[4:].encode()).decode()
        except InvalidToken:
            return None
    return None


def hash_token(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()


def _new_secret(auth: str) -> tuple[str, str]:
    """(clé rendue UNE fois, forme rangée). Bearer : 32 octets aléatoires, rangé haché (une base lue ne donne pas le
    jeton) ; HMAC : la clé, chiffrée si `NOKKER_AGENTS_ENC_KEY` (il faut la relire pour vérifier une signature)."""
    if auth == "bearer":
        token = "nkb_" + _secrets.token_urlsafe(BEARER_MIN_BYTES)
        return token, hash_token(token)
    key = "whsec_" + _secrets.token_urlsafe(32)
    return key, seal_key(key)


def sign(key: str, body: bytes, t: float | None = None) -> str:
    """Valeur de l'en-tête `X-Nokker-Signature` (pour un relais de signature et pour les tests)."""
    ts = str(int(time.time() if t is None else t))
    return f"t={ts},v1=" + hmac.new(key.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()


@dataclass
class Operate:
    store: BaseIncidents
    agents: Any = None  # AgentsHub (son moteur : `agents.engine`) ; None = des incidents, aucun passage
    approvals: Any = None  # BaseApprovals : sort des cartes posées par les passages
    publish: Callable[[str, dict], Any] | None = None  # EventBus.publish
    scrub: Callable[[str], str] | None = None  # invariants du noyau (caviardage)
    tenants: Callable[[], list[str]] | None = None  # locataires connus (Postgres) ; None = store mémoire
    clock: Callable[[], float] = time.time
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _hits: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque), init=False, repr=False)

    def __post_init__(self) -> None:
        listeners = getattr(self.agents, "run_listeners", None)
        if listeners is not None:
            listeners.append(self.on_run)

    # ─────────── utilitaires ───────────

    def _clean(self, text: str) -> str:
        return self.scrub(text) if self.scrub is not None and text else text

    def _emit(self, inc: Incident) -> None:
        if self.publish is None:
            return
        try:
            self.publish("incident.updated", {"incident": incident_to_json(inc, timeline=False)})
        except Exception:  # noqa: BLE001 — un abonné en panne ne casse pas l'incident
            logger.exception("publication de incident.updated impossible")

    def _note(self, inc: Incident, kind: str, text: str, **extra: Any) -> None:
        entry = {"at": self.clock(), "kind": kind, "text": self._clean(text)[:1000]}
        entry.update({k: v for k, v in extra.items() if v is not None})
        inc.timeline.append(entry)
        if len(inc.timeline) > MAX_TIMELINE:
            inc.timeline = [inc.timeline[0], *inc.timeline[-(MAX_TIMELINE - 1):]]

    def _save(self, inc: Incident) -> Incident:
        inc.updated_at = self.clock()
        self.store.put(inc)
        self._emit(inc)
        return inc

    # ─────────── sources ───────────

    def list_sources(self) -> list[AlertSource]:
        return self.store.list_sources()

    def create_source(self, body: dict[str, Any], by: str) -> tuple[AlertSource, str]:
        name = str(body.get("name") or "").strip()
        if not _SOURCE_NAME.match(name):
            raise IncidentError("name : 1 à 64 caractères (lettres, chiffres, espace, _ . -)")
        fmt = str(body.get("format") or "generic")
        if fmt not in FORMATS:
            raise IncidentError(f"format : {' | '.join(FORMATS)}")
        auth = str(body.get("auth") or "hmac")
        if auth not in AUTH_MODES:
            raise IncidentError(f"auth : {' | '.join(AUTH_MODES)} (hmac par défaut)")
        if len(self.store.list_sources()) >= MAX_SOURCES:
            raise IncidentError(f"au plus {MAX_SOURCES} sources", status=409, code="too_many")
        key, stored = _new_secret(auth)
        s = AlertSource(id=new_source_id(), name=name, format=fmt, created_by=by, created_at=self.clock(), key=stored)
        self.store.put_source(s)
        return s, key

    def rotate_secret(self, source_id: str) -> tuple[AlertSource, str]:
        """Nouvelle clé (ou nouveau jeton), même mode d'authentification ; l'ancienne ne vaut plus."""
        s = self._source(source_id)
        key, s.key = _new_secret(s.auth)
        self.store.put_source(s)
        return s, key

    def delete_source(self, source_id: str) -> None:
        self._source(source_id)
        self.store.delete_source(source_id)  # ses incidents restent (le fil de ce qui s'est passé)

    def _source(self, source_id: str) -> AlertSource:
        s = self.store.get_source(source_id)
        if s is None:
            raise IncidentNotFound(f"source inconnue : {source_id}")
        return s

    def _find_tenant(self, source_id: str) -> str | None:
        tid = self.store.tenant_of_source(source_id)
        if tid is not None or self.tenants is None:
            return tid
        for t in self.tenants():
            try:
                with tenant_scope(t):
                    if self.store.get_source(source_id) is not None:
                        return t
            except Exception:  # noqa: BLE001 — un locataire illisible n'empêche pas de chercher ailleurs
                logger.exception("incidents : sources illisibles pour %s", t)
        return None

    def _rate_ok(self, source_id: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[source_id]
            while q and now - q[0] > RATE_WINDOW_S:
                q.popleft()
            if len(q) >= RATE_MAX:
                return False
            q.append(now)
            return True

    # ─────────── réception ───────────

    def receive(self, source_id: str, body: bytes, signature: str | None,
                authorization: str | None = None) -> tuple[int, dict[str, Any]]:
        """`POST /v1/alerts/<id>`, sans identité : la signature de la source (ou son jeton Bearer, source `auth:
        bearer`) en tient lieu. 202 et ce qui a été fait de chaque alerte ; 401 signature ou jeton absent, faux ou hors
        délai ; 404 source inconnue ; 409 rejeu (HMAC seulement) ; 429."""
        nf = (404, {"error": {"code": "not_found", "message": "not found"}})
        tenant = self._find_tenant(source_id)
        if tenant is None:
            return nf
        with tenant_scope(tenant):
            s = self.store.get_source(source_id)
            if s is not None and s.auth == "bearer":
                return self._receive_bearer(s, body, authorization)
            key = open_key(s.key) if s is not None else None
            if s is None or key is None:
                return nf
            parts = dict(p.split("=", 1) for p in (signature or "").replace(" ", "").split(",") if "=" in p)
            try:
                ts = float(parts.get("t", ""))
            except ValueError:
                return 401, {"error": {"code": "signature", "message": f"signature absente ou illisible "
                                                                       f"({SIG_HEADER}: t=…,v1=…)"}}
            now = self.clock()
            if abs(now - ts) > SIG_TOLERANCE_S:
                return 401, {"error": {"code": "signature", "message": "signature hors délai (± 5 min)"}}
            expected = hmac.new(key.encode(), parts["t"].encode() + b"." + body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, parts.get("v1", "")):
                return 401, {"error": {"code": "signature", "message": "signature invalide"}}
            if self.store.seen(s.id, expected, now + 2 * SIG_TOLERANCE_S, now):
                return 409, {"error": {"code": "replay", "message": "déjà reçu (rejeu refusé)"}}
            return self._accept(s, body, now)

    def _receive_bearer(self, s: AlertSource, body: bytes, authorization: str | None) -> tuple[int, dict[str, Any]]:
        """Source `auth: bearer` : `Authorization: Bearer <jeton>`, 32 octets ou plus, haché puis comparé en temps
        constant à la forme rangée. Pas de signature, donc pas de refus de rejeu : une alerte rejouée ne fait qu'ajouter
        une occurrence à l'incident vivant (même empreinte), et un passage déclenché n'écrit rien sans carte."""
        m = _BEARER.match(authorization or "")
        token = m.group(1) if m else ""
        if not token:
            return 401, {"error": {"code": "token", "message": "jeton absent (Authorization: Bearer <jeton>)"}}
        if len(token.encode()) < BEARER_MIN_BYTES or not hmac.compare_digest(hash_token(token), s.key or ""):
            return 401, {"error": {"code": "token", "message": "jeton invalide"}}
        return self._accept(s, body, self.clock())

    def _accept(self, s: AlertSource, body: bytes, now: float) -> tuple[int, dict[str, Any]]:
        if not self._rate_ok(s.id):
            return 429, {"error": {"code": "rate", "message": "trop d'envois, réessayer dans une minute"}}
        try:
            alerts = parse_alerts(s.format, json.loads(body.decode("utf-8") or "null"))
        except (ValueError, UnicodeDecodeError):
            return 400, {"error": {"code": "invalid", "message": "corps JSON illisible"}}
        except IncidentError as e:
            return e.status, {"error": {"code": e.code, "message": e.message}}
        s.last_seen_at = now
        self.store.put_source(s)
        return 202, {"alerts": [self._ingest(s, a) for a in alerts]}

    def _ingest(self, s: AlertSource, a: ParsedAlert) -> dict[str, Any]:
        now = self.clock()
        title, summary = self._clean(a.title), self._clean(a.summary)
        labels = {k: self._clean(v) for k, v in a.labels.items()}
        with self._lock:
            inc = self.store.live_by_fingerprint(s.id, a.fingerprint)
            if a.status == "resolved":
                if inc is None:
                    return {"fingerprint": a.fingerprint, "name": a.name, "result": "ignored"}
                inc.state, inc.resolved_at, inc.resolved_by = "resolved", now, f"source:{s.name}"
                inc.last_seen_at = now
                self._note(inc, "resolved", f"résolue par la source « {s.name} »", by=f"source:{s.name}")
                self._save(inc)
                return {"incident_id": inc.id, "name": a.name, "result": "resolved"}
            if inc is not None:
                inc.occurrences += 1
                inc.last_seen_at = now
                inc.title, inc.summary, inc.labels, inc.severity = title or inc.title, summary, labels, a.severity
                self._save(inc)
                return {"incident_id": inc.id, "name": a.name, "result": "repeated"}
            inc = Incident(id=new_incident_id(), source_id=s.id, source_name=s.name, fingerprint=a.fingerprint,
                           name=a.name, title=title or a.name, severity=a.severity, labels=labels, summary=summary,
                           started_at=a.started_at, first_seen_at=now, last_seen_at=now, created_at=now,
                           updated_at=now)
            self._note(inc, "opened", f"ouverte par la source « {s.name} » : {title or a.name}")
            try:
                self._save(inc)
            except Exception:  # noqa: BLE001 — envoi simultané (index unique des incidents vivants) : une occurrence
                other = self.store.live_by_fingerprint(s.id, a.fingerprint)
                if other is None:
                    raise
                other.occurrences += 1
                other.last_seen_at = now
                self._save(other)
                return {"incident_id": other.id, "name": a.name, "result": "repeated"}
        runs = self._fire(inc, a, s)
        return {"incident_id": inc.id, "name": a.name, "result": "opened", "runs": runs}

    def _fire(self, inc: Incident, a: ParsedAlert, s: AlertSource) -> list[str]:
        engine = getattr(self.agents, "engine", None) if self.agents is not None else None
        if engine is None:
            with self._lock:
                inc = self.store.get(inc.id) or inc
                self._note(inc, "no_agent", "aucun moteur d'agents sur cette instance : pas de passage")
                self._save(inc)
            return []
        payload = {"incident": inc.id, "source": s.name, "alert": {
            "name": a.name, "status": a.status, "severity": a.severity, "title": a.title, "summary": a.summary,
            "labels": a.labels, "fingerprint": a.fingerprint, "started_at": a.started_at}}
        token = _FIRING.set(inc.id)
        try:
            runs = engine.fire_event(f"alert:{a.name}", payload)
        except Exception:  # noqa: BLE001 — un moteur en panne ne perd pas l'incident
            logger.exception("incidents : agents de alert:%s non déclenchés", a.name)
            runs = []
        finally:
            _FIRING.reset(token)
        if not runs:
            with self._lock:
                inc = self.store.get(inc.id) or inc
                self._note(inc, "no_agent", f"aucun agent armé sur alert:{a.name}")
                self._save(inc)
        for run in runs:  # rattache aussi un passage dont aucun événement n'a été vu (moteur sans publication)
            fresh = self.agents.store.get_run(run.id) if self.agents is not None else None
            self.on_run(fresh or run, incident_id=inc.id)  # l'état rangé, pas la copie rendue à la mise en file
        return [r.id for r in runs]

    # ─────────── passages des agents ───────────

    def on_run(self, run: Any, *, incident_id: str | None = None) -> None:
        """Changement d'état d'un passage (`AgentsHub.publish_run`) : met à jour l'incident qui l'a déclenché."""
        if current_tenant_or_none() is None:
            return
        try:
            with self._lock:
                inc = self.store.by_run(run.id)
                if inc is None:
                    fid = incident_id or (_FIRING.get() if str(run.event or "").startswith("alert:") else None)
                    inc = self.store.get(fid) if fid else None
                if inc is None:
                    return
                self._apply_run(inc, run)
        except Exception:  # noqa: BLE001 — l'incident ne casse jamais un passage
            logger.exception("incidents : passage %s non rattaché", getattr(run, "id", "?"))

    def _apply_run(self, inc: Incident, run: Any) -> None:
        entry = next((r for r in inc.runs if r.get("run_id") == run.id), None)
        changed = False
        if entry is None:
            a = self.agents.store.get(run.agent_id) if self.agents is not None else None
            entry = {"run_id": run.id, "agent_id": run.agent_id, "agent": a.name if a is not None else "",
                     "status": None, "summary": None, "thread_id": None, "ended_at": None, "approvals": []}
            inc.runs.append(entry)
            changed = True
        if entry["status"] != run.status:
            label = _RUN_LABELS.get(run.status, run.status)
            text = f"passage de « {entry['agent'] or run.agent_id} » : {label}"
            if run.status in ("succeeded", "failed", "skipped", "cancelled") and run.summary:
                text += f" — {run.summary}"
            self._note(inc, "agent_run", text, run_id=run.id)
            entry["status"] = run.status
            changed = True
        entry["summary"] = self._clean(run.summary or "")[:SUMMARY_CHARS] or None
        entry["thread_id"], entry["ended_at"] = run.thread_id, run.ended_at
        known = {c["id"]: c for c in entry["approvals"]}
        cards = []
        for aid in getattr(run, "approvals", None) or []:
            card = self.approvals.get(aid) if self.approvals is not None else None
            status = card.status if card is not None else "unknown"
            tool = card.tool_name if card is not None else ""
            prev = known.get(aid)
            if prev is None:
                self._note(inc, "card", f"carte posée par le passage : {tool or aid}", run_id=run.id,
                           approval_id=aid)
                changed = True
            elif prev.get("status") != status:
                self._note(inc, "card", f"carte {tool or aid} : {status}", run_id=run.id, approval_id=aid)
                changed = True
            cards.append({"id": aid, "tool": tool, "status": status,
                          "strength": getattr(card, "strength", None) if card is not None else None})
        entry["approvals"] = cards
        if inc.state == "open" and run.status in ("running", "waiting_approval"):
            inc.state = "in_progress"
            changed = True
        if changed:
            self._save(inc)
        else:
            self.store.put(inc)

    # ─────────── lecture, gestes humains ───────────

    def get(self, incident_id: str) -> Incident:
        inc = self.store.get(incident_id)
        if inc is None:
            raise IncidentNotFound(f"incident inconnu : {incident_id}")
        return inc

    def list(self, *, state: str | None = None, limit: int = 50, before: str | None = None
             ) -> tuple[list[Incident], str | None]:
        if state is not None and state not in STATES and state != "live":
            raise IncidentError(f"state : {' | '.join((*STATES, 'live'))}")
        items = self.store.list(None if state in (None, "live") else state)
        if state == "live":
            items = [i for i in items if i.state in LIVE_STATES]
        items.sort(key=lambda i: (i.created_at, i.id), reverse=True)
        if before:
            ids = [i.id for i in items]
            if before not in ids:
                raise IncidentNotFound(f"incident {before}")
            items = items[ids.index(before) + 1:]
        limit = max(1, min(200, int(limit)))
        page = items[:limit]
        return page, (page[-1].id if len(items) > limit else None)

    def ack(self, incident_id: str, by: str) -> Incident:
        with self._lock:
            inc = self.get(incident_id)
            if inc.state == "resolved":
                raise IncidentError("incident déjà résolu", status=409, code="resolved")
            inc.state, inc.acked_by = "in_progress", by
            self._note(inc, "acked", f"pris en main par {by}", by=by)
            return self._save(inc)

    def resolve(self, incident_id: str, by: str, note: str = "") -> Incident:
        with self._lock:
            inc = self.get(incident_id)
            if inc.state == "resolved":
                raise IncidentError("incident déjà résolu", status=409, code="resolved")
            inc.state, inc.resolved_at, inc.resolved_by = "resolved", self.clock(), by
            text = f"résolu par {by}" + (f" : {' '.join(str(note).split())[:800]}" if note else "")
            self._note(inc, "resolved", text, by=by)
            return self._save(inc)

    def open_incidents(self) -> list[Incident]:
        """Incidents vivants du locataire courant (brief du matin)."""
        return sorted((i for i in self.store.list() if i.state in LIVE_STATES), key=lambda i: i.created_at)


__all__ = ["ENC_KEY_ENV", "SIG_HEADER", "Operate", "open_key", "seal_key", "sign"]
