"""Connecteurs relayés, côté nokker.ai (lots X20 et X20b, docs/LOKKAL.md §6.7 et §7.12).

Un connecteur de compte en ligne posé au web (mail, agenda) sert dans une session Lokkal SANS que son identifiant quitte
nokker.ai : la machine liée envoie l'appel par le lien de compte (jeton de machine), nokker.ai l'exécute avec
l'identifiant qu'il garde et renvoie le résultat caviardé. Chaque appel est compté au locataire (ledger) et écrit au
journal d'audit (`lokkal_relay_calls`, migrations 022 et 026 ; aucun argument ni résultat, seulement leurs empreintes).

    GET  /v1/gateway/connectors                 → {connectors: [{id, kind, locality, state, label, tools: [...]}]}
    POST /v1/gateway/connectors/{id}/call       {tool, arguments, approval?} → {content, is_error, effect, audit_id}

Classes d'outils (`TOOL_CLASSES`, X20b) — la classe est celle que pose nokker.ai, jamais celle qu'annonce la machine ;
un outil non classé est traité comme un envoi externe :
  - lecture : exécutée sur la foi du jeton de machine ;
  - écriture interne (brouillon, déplacement hors corbeille, événement dans son propre agenda) : la preuve d'une
    carte de la session Lokkal approuvée (`approval` : son id et l'empreinte des arguments approuvés, recalculée ici
    sur les arguments reçus) ; une carte ne sert qu'une fois par machine ; la preuve est journalisée, non signée ;
  - envoi externe (mail à quelqu'un, événement avec des invités) et destructif (corbeille, `mail_move` vers elle
    compris, suppression) : une carte FORTE signée par un appareil du compte (`approval.signed`), que nokker.ai vérifie
    LUI-MÊME avant d'exécuter : défi déposé par CETTE machine (lot X10), lié à l'outil, à l'empreinte des arguments
    reçus et à l'id de la carte, non expiré ; appareil confirmé du compte de la machine et qui décide (pas de simple connexion) ; passkey avec UV,
    origine et rpId d'app.nokker.ai (`BaseApprovals.verify_signed_decision`, la routine de la machine) ; usage unique
    (id de carte par machine, nonce du défi partout) ; et (X20c) carte déposée AVEC ses arguments, résumés par nokker.ai
    sur la page, de même empreinte que l'appel. Sans cela : `403 strong_approval_required`. Le jeton de machine seul,
    même avec une carte locale approuvée, n'envoie donc rien ;
  - envoi de masse, dépense, secret : refusés (`not_relayable`).
"""
from __future__ import annotations

import enum
import hashlib
import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from nokker.connectors import build_connector
from nokker.connectors.errors import ConnectorError, scrub
from nokker.connectors.service import public_settings
from nokker.connectors.store import KINDS
from nokker.core.ports import Approval, Proof, ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult

logger = logging.getLogger("nokker.cloud.relay")

CHANNEL = "lokkal-relay"
MAX_CALL_BYTES = 256 * 1024  # un corps de mail fait au plus quelques dizaines de Kio
MAX_SIGNED_FIELD = 4096  # comme une preuve gardée par la boîte aux lettres (nokker/cloud/approvals.py)
_RE_APPROVAL_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_RE_SHA = re.compile(r"^[0-9a-f]{64}$")
SIGNED_FIELDS = ("challenge", "decision", "device_id", "signature", "webauthn_client_data_json",
                 "webauthn_authenticator_data", "webauthn_signature")
STRONG_MESSAGE = "à signer sur ton téléphone (app.nokker.ai) : une carte forte signée par un appareil du compte est exigée"


class ToolClass(str, enum.Enum):
    """Classe d'un outil de connecteur relayé (X20b) : ce qui décide de la preuve exigée."""

    READ = "read"
    INTERNAL_WRITE = "internal_write"  # reste dans le compte : brouillon, rangement hors corbeille, événement sans invité
    EXTERNAL_SEND = "external_send"  # atteint quelqu'un d'autre : mail, invitation, publication
    DESTRUCTIVE = "destructive"  # corbeille, suppression


def _invites(arguments: dict[str, Any]) -> ToolClass:
    """Un événement avec des invités atteint d'autres personnes (un serveur CalDAV qui planifie peut les inviter) :
    envoi externe ; sans invité, il reste dans son propre agenda."""
    return ToolClass.EXTERNAL_SEND if arguments.get("attendees") else ToolClass.INTERNAL_WRITE


# Chaque outil de connecteur est classé ICI, explicitement ; un outil absent de la table est un envoi externe.
# Valeur : une classe, ou (classe par défaut, arguments qui en font un envoi externe quand ils sont non vides). Par-dessus,
# l'effet par argument du noyau local (`_local_effect`) : `mail_move` vers la corbeille est destructif.
TOOL_CLASSES: dict[str, ToolClass | tuple[ToolClass, tuple[str, ...]]] = {
    "mail_search": ToolClass.READ,
    "mail_read": ToolClass.READ,
    "calendar_events": ToolClass.READ,
    "mail_draft": ToolClass.INTERNAL_WRITE,
    "mail_move": ToolClass.INTERNAL_WRITE,
    "calendar_create": (ToolClass.INTERNAL_WRITE, ("attendees",)),
    "calendar_update": (ToolClass.INTERNAL_WRITE, ("attendees",)),
    "mail_send": ToolClass.EXTERNAL_SEND,
    "mail_trash": ToolClass.DESTRUCTIVE,
    "calendar_delete": ToolClass.DESTRUCTIVE,
}
_RANK = {ToolClass.READ: 0, ToolClass.INTERNAL_WRITE: 1, ToolClass.EXTERNAL_SEND: 2, ToolClass.DESTRUCTIVE: 3}
# Classe minimale qu'impose l'effet déclaré par l'outil (une table en retard sur un connecteur ne l'affaiblit jamais).
_EFFECT_FLOOR = {Effect.READ: ToolClass.READ, Effect.WRITE: ToolClass.INTERNAL_WRITE,
                 Effect.SEND_EXTERNAL: ToolClass.EXTERNAL_SEND, Effect.DESTRUCTIVE: ToolClass.DESTRUCTIVE}
NEVER_RELAYED = frozenset({Effect.SEND_BULK, Effect.SPEND, Effect.SECRET})
STRONG_CLASSES = frozenset({ToolClass.EXTERNAL_SEND, ToolClass.DESTRUCTIVE})
CLASS_EFFECT = {ToolClass.READ: Effect.READ, ToolClass.INTERNAL_WRITE: Effect.WRITE,
                ToolClass.EXTERNAL_SEND: Effect.SEND_EXTERNAL, ToolClass.DESTRUCTIVE: Effect.DESTRUCTIVE}


def _max(a: ToolClass, b: ToolClass) -> ToolClass:
    return a if _RANK[a] >= _RANK[b] else b


def _local_effect(tool: str, arguments: dict[str, Any]) -> Effect | None:
    """Effet que le noyau local donnerait à CET appel d'après ses arguments (`tools/effects.py::ARGUMENT_EFFECTS` :
    `mail_move` vers la corbeille = destructif, un événement avec invités = envoi externe). None : l'outil n'en a pas."""
    from nokker.tools.effects import ARGUMENT_EFFECTS

    of = ARGUMENT_EFFECTS.get(tool)
    return of(arguments or {}) if of is not None else None


def tool_class(spec: ToolSpec, arguments: dict[str, Any] | None = None) -> ToolClass | None:
    """Classe de CET appel (`arguments` None : classe de l'outil sans argument). None : jamais relayé (masse, dépense,
    secret). Non classé : envoi externe. La classe n'est jamais plus faible que l'effet déclaré par l'outil pour ces
    arguments, ni que l'effet que le noyau local donnerait au même appel (`_local_effect`), même si l'outil du
    connecteur ne classe pas lui-même ses appels."""
    effect = spec.effect if arguments is None else spec.effect_for(arguments)
    local = None if arguments is None else _local_effect(spec.name, arguments)
    if effect in NEVER_RELAYED or local in NEVER_RELAYED:
        return None
    entry = TOOL_CLASSES.get(spec.name, ToolClass.EXTERNAL_SEND)
    base, send_if = (entry, ()) if isinstance(entry, ToolClass) else entry
    if arguments is not None and any(arguments.get(k) for k in send_if):
        base = ToolClass.EXTERNAL_SEND
    cls = _max(base, _EFFECT_FLOOR.get(effect, ToolClass.EXTERNAL_SEND))
    return cls if local is None else _max(cls, _EFFECT_FLOOR.get(local, ToolClass.EXTERNAL_SEND))


def call_class(tool: str, arguments: dict[str, Any]) -> ToolClass:
    """Classe d'un appel d'après son seul nom et ses arguments (X20c : résumé d'une carte déposée, sans le connecteur
    sous la main). Même table que `tool_class` et même plancher de l'effet local par argument (la corbeille par
    `mail_move` s'affiche « supprime »), sans l'effet déclaré par l'outil : c'est un affichage ; la porte de l'appel
    (`ConnectorRelay._gate`) reclasse avec l'outil réel. Non classé : envoi externe."""
    entry = TOOL_CLASSES.get(tool, ToolClass.EXTERNAL_SEND)
    base, send_if = (entry, ()) if isinstance(entry, ToolClass) else entry
    cls = ToolClass.EXTERNAL_SEND if any((arguments or {}).get(k) for k in send_if) else base
    local = _local_effect(tool, arguments)
    if local is None:
        return cls
    return _max(cls, _EFFECT_FLOOR.get(local, ToolClass.EXTERNAL_SEND))


class ApprovalReused(Exception):
    """La même carte locale a déjà servi pour un appel relayé de cette machine (ou le même défi signé, ailleurs)."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def args_sha256(arguments: dict[str, Any]) -> str:
    from nokker.approvals.crypto import args_sha256 as _args

    return _args(arguments)


def relayable(spec: ToolSpec) -> bool:
    return tool_class(spec) is not None


def tool_view(spec: ToolSpec) -> dict[str, Any]:
    """Ce que voit la machine d'un outil : nom, description, schéma, classe et l'effet qui en découle (c'est lui qui
    fait poser à la machine une carte FORTE pour un envoi externe), et `send_if` : les arguments qui, non vides, font
    de l'appel un envoi externe. Jamais un identifiant ni un réglage."""
    cls = tool_class(spec)
    entry = TOOL_CLASSES.get(spec.name)
    send_if = list(entry[1]) if isinstance(entry, tuple) else []
    return {"name": spec.name, "description": spec.description, "parameters": spec.parameters,
            "effect": (CLASS_EFFECT[cls] if cls is not None else spec.effect).value, "class": cls.value if cls else None,
            "send_if": send_if, "relayed": cls is not None,
            "approval": "not_relayable" if cls is None else ("none" if cls is ToolClass.READ else (
                "strong" if cls in STRONG_CLASSES else "required"))}


def _label(kind: str, settings: dict[str, Any]) -> str:
    pub = public_settings(kind, settings)
    return str(pub.get("address") or pub.get("label") or pub.get("username") or kind)[:120]


# ─────────────────────────────── journal d'audit ───────────────────────────────


def _clean_approval(raw: Any) -> dict[str, Any] | None:
    """La preuve telle que la machine la décrit, bornée (rien d'autre n'est gardé). Pour une carte forte, ce qui est
    journalisé de la signature : l'appareil, le défi (id, empreinte, outil, nonce, expiration : rien de privé) ; la
    signature elle-même reste dans `signed`, jamais écrite au journal."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for key in ("id", "args_sha256", "decided_via", "decided_by", "strength"):
        v = raw.get(key)
        if isinstance(v, str) and v:
            out[key] = v[:80]
    if isinstance(raw.get("decided_at"), (int, float)):
        out["decided_at"] = float(raw["decided_at"])
    signed = raw.get("signed")
    if isinstance(signed, dict):
        kept = {k: signed[k] for k in SIGNED_FIELDS if isinstance(signed.get(k), str) and signed[k]}
        if all(len(v) <= MAX_SIGNED_FIELD for v in kept.values()):
            out["signed"] = kept
    return out


def _journal_view(approval: dict[str, Any] | None) -> dict[str, Any] | None:
    """La preuve telle qu'écrite au journal : sans les champs de signature (assez de quoi croiser, rien à rejouer)."""
    if approval is None:
        return None
    out = {k: v for k, v in approval.items() if k != "signed"}
    signed = approval.get("signed") or {}
    if signed.get("device_id"):
        out["device_id"] = str(signed["device_id"])[:80]
    return out


class MemoryRelayAudit:
    """Journal en mémoire (tests, dev). `PgRelayAudit` a la même interface."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def begin(self, entry: dict[str, Any]) -> str:
        with self._lock:
            if entry.get("approval_id") and any(
                    r["machine_id"] == entry["machine_id"] and r.get("approval_id") == entry["approval_id"]
                    for r in self.rows):
                raise ApprovalReused(entry["approval_id"])
            if entry.get("strong_nonce") and any(r.get("strong_nonce") == entry["strong_nonce"] for r in self.rows):
                raise ApprovalReused(entry.get("approval_id") or "")  # même défi signé, toutes machines confondues
            row = {**entry, "audit_id": f"rc_{uuid.uuid4().hex[:16]}", "at": time.time()}
            self.rows.append(row)
            return row["audit_id"]

    def finish(self, tenant: str, audit_id: str, outcome: str, code: str | None = None,
               result_sha256: str | None = None) -> None:
        with self._lock:
            for r in self.rows:
                if r["audit_id"] == audit_id and r["tenant_id"] == tenant:
                    r.update(outcome=outcome, code=code, result_sha256=result_sha256)

    def of_tenant(self, tenant: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.rows if r["tenant_id"] == tenant]


class PgRelayAudit:
    """Table `lokkal_relay_calls` (migrations 022 et 026), sous la RLS du locataire de la machine. L'unicité (machine,
    carte) et celle du nonce d'un défi signé (toutes machines, tous locataires : un index unique ne voit pas la RLS)
    sont tenues par la base : deux appels concurrents avec la même carte ou la même signature, un seul passe."""

    _COLS = ("audit_id", "tenant_id", "machine_id", "account_id", "connector", "tool", "effect", "approval_id",
             "args_sha256", "approval", "outcome", "code", "strong_nonce")

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _tq(self, tenant: str, sql: str, params: tuple = ()) -> list[tuple]:
        from nokker.store.conn import connect
        from nokker.tenancy.context import tenant_scope

        with tenant_scope(tenant), connect(self.dsn, autocommit=True) as conn:
            cur = conn.execute(sql, params)
            return cur.fetchall() if cur.description else []

    def begin(self, entry: dict[str, Any]) -> str:
        import psycopg
        from psycopg.types.json import Jsonb

        audit_id = f"rc_{uuid.uuid4().hex[:16]}"
        row = {**entry, "audit_id": audit_id}
        values = tuple(Jsonb(row[c]) if c == "approval" and row.get(c) is not None else row.get(c) for c in self._COLS)
        try:
            self._tq(entry["tenant_id"], f"INSERT INTO lokkal_relay_calls ({', '.join(self._COLS)}, at) "
                     f"VALUES ({', '.join(['%s'] * len(self._COLS))}, %s)", (*values, time.time()))
        except psycopg.errors.UniqueViolation:
            raise ApprovalReused(entry.get("approval_id") or "") from None
        return audit_id

    def finish(self, tenant: str, audit_id: str, outcome: str, code: str | None = None,
               result_sha256: str | None = None) -> None:
        self._tq(tenant, "UPDATE lokkal_relay_calls SET outcome = %s, code = %s, result_sha256 = %s "
                 "WHERE audit_id = %s", (outcome, code, result_sha256, audit_id))

    def of_tenant(self, tenant: str) -> list[dict[str, Any]]:
        cols = (*self._COLS, "result_sha256", "at")
        return [dict(zip(cols, r)) for r in self._tq(
            tenant, f"SELECT {', '.join(cols)} FROM lokkal_relay_calls ORDER BY at")]


# ─────────────────────────────── service ───────────────────────────────


@dataclass
class ConnectorRelay:
    """Exécute les appels relayés d'une machine liée avec les connecteurs de SON compte.

    `store` : le magasin des connecteurs de nokker.ai (`connector_credentials`, sous RLS : lu dans le `tenant_scope` de
    la machine) ; `redact(result, spec)` : les invariants du noyau (`redact_result`) ; `record(tenant, machine_id,
    entry)` : le comptage au ledger du locataire ; `build` : `build_connector` (remplaçable dans les tests)."""

    store: Any
    audit: Any = field(default_factory=MemoryRelayAudit)
    redact: Callable[[ToolResult, ToolSpec | None], ToolResult] | None = None
    record: Callable[[str, str, dict[str, Any]], None] | None = None
    resolve: Callable[[str], str] | None = None
    build: Callable[..., Any] = build_connector
    # Cartes fortes (X20b) : `verifier` = le backend d'approbations de nokker.ai (`verify_signed_decision`, la routine
    # de la machine) ; `devices_of(compte)` = ses appareils (ceux que rend /v1/gateway/devices) ; `cards` = la boîte aux
    # lettres des cartes déposées par les machines (lot X10, `MemoryRelayStore` / `PgRelayStore`) ; `origin` = origine
    # WebAuthn d'app.nokker.ai (rpId = son hôte). Un seul manque : aucun envoi externe ni destructif ne passe.
    verifier: Any = None
    devices_of: Callable[[str], list[Any]] | None = None
    cards: Any = None
    origin: str | None = None
    clock: Callable[[], float] = time.time

    def _sources(self, account: str) -> list[tuple[str, Any, Any]]:
        """(nature, identifiant, source) des connecteurs du compte ; une source illisible est rendue None."""
        out = []
        for kind in KINDS:
            try:
                cred = self.store.get(account, kind)
            except ConnectorError as e:  # secret indéchiffrable (clé changée) : le connecteur n'est pas montré
                logger.warning("connecteur relayé %s illisible : %s", kind, e.code)
                continue
            if cred is None:
                continue
            try:
                secret = self._resolve(cred.secret)
                out.append((kind, cred, self.build(kind, cred.settings, secret, locality="cloud")))
            except ConnectorError as e:
                logger.warning("connecteur relayé %s illisible : %s", kind, e.code)
                out.append((kind, cred, None))
        return out

    def _resolve(self, secret: str) -> str:
        if self.resolve is None:
            return secret
        from nokker.secrets.base import SecretsError

        try:
            return self.resolve(secret)
        except SecretsError as e:
            raise ConnectorError("key_missing", str(e)) from None

    def listing(self, machine: Any) -> dict[str, Any]:
        from nokker.tenancy.context import tenant_scope

        with tenant_scope(machine.tenant):
            found = self._sources(machine.account)
        return {"connectors": [
            {"id": kind, "kind": kind, "locality": "cloud",
             "state": "broken" if src is None else ("ok" if cred.last_ok_at else "unverified"),
             "label": _label(kind, cred.settings),
             "tools": [] if src is None else [tool_view(s) for s in src.specs()]}
            for kind, cred, src in found]}

    def call(self, machine: Any, connector_id: str, body: Any) -> tuple[int, dict[str, Any]]:
        """(statut HTTP, corps). Jamais d'exception : une erreur du connecteur est un résultat `is_error`."""
        from nokker.tenancy.context import tenant_scope

        if not isinstance(body, dict) or not isinstance(body.get("tool"), str) \
                or not isinstance(body.get("arguments", {}), dict):
            return 400, _err("bad_request", "corps attendu : {tool, arguments, approval?}")
        tool, arguments = body["tool"], dict(body.get("arguments") or {})
        if connector_id not in KINDS:
            return 404, _err("not_configured", "connecteur inconnu")
        with tenant_scope(machine.tenant):
            return self._call(machine, connector_id, tool, arguments, body.get("approval"))

    def _call(self, machine: Any, kind: str, tool: str, arguments: dict[str, Any],
              approval_raw: Any) -> tuple[int, dict[str, Any]]:
        try:
            cred = self.store.get(machine.account, kind)
        except ConnectorError as e:
            return 503, _err("connector_unavailable", f"connecteur illisible chez nokker.ai ({e.code})")
        if cred is None:
            return 404, _err("not_configured", "aucun connecteur de ce type sur ce compte")
        try:
            secret = self._resolve(cred.secret)
            src = self.build(kind, cred.settings, secret, locality="cloud")
        except ConnectorError as e:
            return 503, _err("connector_unavailable", f"connecteur illisible chez nokker.ai ({e.code})")
        spec = next((s for s in src.specs() if s.name == tool), None)
        if spec is None:
            return 404, _err("unknown_tool", f"outil inconnu pour ce connecteur : {tool[:60]}")
        effect = spec.effect_for(arguments)
        cls = tool_class(spec, arguments)
        digest = args_sha256(arguments)
        approval = _clean_approval(approval_raw)
        carded = cls is not None and cls is not ToolClass.READ
        entry = {"tenant_id": machine.tenant, "machine_id": machine.id, "account_id": machine.account,
                 "connector": kind, "tool": tool, "effect": effect.value, "args_sha256": digest,
                 "approval_id": (approval or {}).get("id") if carded else None,
                 "approval": _journal_view(approval) if carded else None, "outcome": "started", "code": None,
                 "strong_nonce": None}
        refusal = self._gate(cls, effect, approval, digest)
        if refusal is None and cls in STRONG_CLASSES:
            refusal, nonce, device_id = self._strong(machine, tool, arguments, digest, approval or {})
            entry["strong_nonce"] = nonce
            if entry["approval"] is not None and device_id:
                entry["approval"] = {**entry["approval"], "verified": "webauthn", "device_id": device_id}
        if refusal is not None:
            status, code, message = refusal
            # une carte refusée ne « consomme » rien : ni l'id de la carte, ni le nonce du défi
            entry.update(outcome="refused", code=code, approval_id=None, strong_nonce=None)
            audit_id = self.audit.begin(entry)
            self._count(machine, entry, audit_id)
            return status, {**_err(code, message), "audit_id": audit_id}
        try:
            audit_id = self.audit.begin(entry)
        except ApprovalReused:
            entry.update(outcome="refused", code="approval_reused", approval_id=None, strong_nonce=None)
            audit_id = self.audit.begin(entry)
            self._count(machine, entry, audit_id)
            return 409, {**_err("approval_reused", "cette carte a déjà servi : une carte par appel"),
                         "audit_id": audit_id}
        ctx = ToolContext(thread_id=f"lokkal-relay-{machine.id}", turn_index=0, channel=CHANNEL,
                          sender=machine.account, approval_id=entry["approval_id"])
        try:
            result = src.call(tool, arguments, ctx)
        except Exception as e:  # noqa: BLE001 — un connecteur ne lève pas, mais la réponse ne doit jamais casser
            result = ToolResult(call_id="", name=tool, content=f"erreur interne du connecteur ({type(e).__name__})",
                                is_error=True)
        if self.redact is not None:
            result = self.redact(result, spec)
        content = scrub(result.content, secret, cred.secret)  # filet : l'identifiant ne sort jamais, même cité
        outcome = "error" if result.is_error else "executed"
        self.audit.finish(machine.tenant, audit_id, outcome, None, _sha(content))
        entry["outcome"] = outcome
        self._count(machine, entry, audit_id)
        return 200, {"content": content, "is_error": bool(result.is_error), "effect": effect.value,
                     "class": cls.value if cls is not None else None, "audit_id": audit_id,
                     "secret_handles": list(result.secret_handles)}

    @staticmethod
    def _gate(klass: ToolClass | None, effect: Effect, approval: dict[str, Any] | None,
              digest: str) -> tuple[int, str, str] | None:
        if klass is ToolClass.READ:
            return None
        if klass is None:
            return 403, "not_relayable", f"effet {effect.value} : jamais relayé"
        strong = klass in STRONG_CLASSES
        if not approval or not approval.get("id") or not _RE_APPROVAL_ID.match(approval["id"]):
            if strong:
                return 403, "strong_approval_required", f"{_what(klass)} : {STRONG_MESSAGE}"
            return 403, "approval_required", "outil à effet : la carte de la session Lokkal approuvée est exigée"
        if not _RE_SHA.match(str(approval.get("args_sha256") or "")) or approval["args_sha256"] != digest:
            if strong:
                why = f"{_what(klass)} : la carte ne porte pas ces arguments ; {STRONG_MESSAGE}"
                return 403, "strong_approval_required", why
            return 403, "approval_mismatch", "la carte approuvée ne porte pas ces arguments"
        return None

    def _strong(self, machine: Any, tool: str, arguments: dict[str, Any], digest: str,
                approval: dict[str, Any]) -> tuple[tuple[int, str, str] | None, str | None, str | None]:
        """Vérifie ICI la carte forte d'un envoi externe ou d'un destructif (X20b). Rend (refus ou None, nonce du défi
        à consommer, appareil). Toute faille → `403 strong_approval_required` avec la raison ; rien n'est consommé."""
        from nokker.approvals.errors import ProofInvalid, StrongDeviceRequired

        def refuse(why: str) -> tuple[tuple[int, str, str], None, None]:
            return (403, "strong_approval_required", f"{why} ; {STRONG_MESSAGE}"), None, None

        if self.verifier is None or self.devices_of is None or self.cards is None or not self.origin:
            return refuse("vérification des cartes fortes non configurée chez nokker.ai")
        signed = approval.get("signed")
        if not isinstance(signed, dict) or not signed.get("challenge") or not signed.get("device_id"):
            return refuse("carte locale sans signature d'appareil")
        if signed.get("decision") != "approve":
            return refuse("la signature n'approuve pas")
        aid = approval["id"]
        # Le défi doit être celui que CETTE machine a déposé pour CETTE carte (lot X10) et que l'appareil a signé sur
        # la page de décision : une preuve venue d'ailleurs (carte du web, autre machine) ne passe pas.
        card = self.cards.get(machine.tenant, machine.id, aid)
        if card is None or card.challenge != signed["challenge"]:
            return refuse("aucune carte forte de cette machine ne porte ce défi")
        if card.status not in ("forwarded", "approved") or card.decision != "approve":
            return refuse(f"carte {card.status} chez nokker.ai")
        # Ce que tu signes (X20c) : la carte a été déposée AVEC ses arguments, que nokker.ai a confrontés au défi et
        # résumés lui-même sur la page ; une carte déposée sans eux n'a montré que le texte de la machine. Et l'appel
        # porte exactement ces arguments-là (le défi le dit aussi, `verify_signed_decision` le revérifie plus bas).
        if getattr(card, "summary_source", None) != "nokker.ai" or not getattr(card, "args_sha256", None):
            return refuse("carte déposée sans ses arguments : nokker.ai n'a pas pu te montrer ce que tu signes")
        if card.args_sha256 != digest:
            return refuse("empreinte des arguments de l'appel ≠ celle de la carte déposée : ce ne sont pas les "
                          "arguments que tu as vus")
        if card.proof and any(card.proof.get(k) != signed.get(k) for k in card.proof):
            return refuse("la signature n'est pas celle transmise par la page de décision")
        try:
            ch = json.loads(signed["challenge"])
        except ValueError:
            return refuse("défi illisible")
        if not isinstance(ch, dict) or ch.get("tool") != tool:
            return refuse("le défi ne porte pas sur cet outil")
        exp = ch.get("exp")
        if not isinstance(exp, (int, float)) or isinstance(exp, bool) or self.clock() > float(exp):
            return refuse("défi expiré")
        device = next((d for d in self.devices_of(machine.account) or []
                       if d.id == signed["device_id"] and d.user == machine.account), None)
        if device is None:
            return refuse("appareil inconnu du compte de la machine")
        target = Approval(id=aid, thread_id=f"lokkal-relay-{machine.id}", turn_index=0, channel=CHANNEL,
                          tool_name=tool, arguments=dict(arguments), args_sha256=digest, strength="strong",
                          rule="relay:strong")
        proof = Proof(challenge=signed["challenge"], device_id=signed["device_id"], signature=signed.get("signature"),
                      webauthn_client_data_json=signed.get("webauthn_client_data_json"),
                      webauthn_authenticator_data=signed.get("webauthn_authenticator_data"),
                      webauthn_signature=signed.get("webauthn_signature"))
        origin = self.origin.rstrip("/")
        try:
            nonce, _via = self.verifier.verify_signed_decision(target, "approve", proof, device, origin=origin,
                                                               rp_id=urlsplit(origin).hostname)
        except StrongDeviceRequired:
            return refuse("appareil de simple connexion : il ne décide pas d'une approbation")
        except ProofInvalid as e:
            return refuse(f"signature refusée ({str(e)[:160]})")
        return None, nonce, device.id

    def _count(self, machine: Any, entry: dict[str, Any], audit_id: str) -> None:
        if self.record is None:
            return
        try:
            self.record(machine.tenant, machine.id, {**entry, "audit_id": audit_id})
        except Exception:  # noqa: BLE001 — le comptage ne casse pas la réponse
            logger.exception("comptage d'un appel relayé en échec")


def _err(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _what(cls: ToolClass) -> str:
    return "envoi externe" if cls is ToolClass.EXTERNAL_SEND else "action destructive"


__all__ = [
    "CHANNEL",
    "MAX_CALL_BYTES",
    "STRONG_CLASSES",
    "TOOL_CLASSES",
    "ApprovalReused",
    "ConnectorRelay",
    "MemoryRelayAudit",
    "PgRelayAudit",
    "ToolClass",
    "args_sha256",
    "call_class",
    "relayable",
    "tool_class",
    "tool_view",
]
