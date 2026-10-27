"""Approbations fortes par la passerelle, côté nokker.ai (lot X10, docs/LOKKAL.md §7.3) : une boîte aux lettres.

nokker.ai ne décide JAMAIS une carte. Il garde la carte déposée par la machine liée (`deposit`), la montre au compte,
garde telle quelle la preuve de passkey signée par un appareil du compte (`decide` → `forwarded`), la rend à la machine
(`decisions`, attente longue), et n'affiche `approved|rejected|expired` que quand la machine l'a dit (`result`), après
avoir vérifié la preuve elle-même contre la clé publique de l'appareil. Sans clé privée d'appareil, nokker.ai (ou un
compte volé, ou le jeton de machine) ne peut produire aucune preuve de passkey valable.

Une seule exception, voulue (26.10) : la preuve Telegram de la Mini App, que la machine ne sait pas vérifier (elle n'a pas
le jeton du bot). nokker.ai la vérifie (`nokker/cloud/attest.py`) et met dans la décision relevée une attestation signée
de sa clé de passerelle, liée à cette machine, ce compte, cette carte, ce défi et cette décision, 120 s ; la machine la
vérifie à son tour et décide (`decide_attested`). La machine fait alors confiance à nokker.ai pour cette carte-là ;
`local.remote_approvals_attested: false` le lui interdit.

Statuts : `pending` (déposée, à signer) → `forwarded` (preuve transmise, la machine vérifie) → `approved|rejected`
(dit par la machine) ; `expired` (passé `expires_at` sans décision, ou dit par la machine). Une preuve refusée par la
machine ramène la carte à `pending` avec `error` : l'appareil légitime peut encore signer (le défi n'est pas brûlé).

Ce que tu signes (lot X20c, §7.12 point 8) : une carte forte d'un connecteur RELAYÉ est déposée avec ses `arguments`
(ils passent de toute façon par nokker.ai à l'appel). nokker.ai recalcule leur empreinte, refuse le dépôt (400) si elle
ne colle pas à `args_sha256` du défi, et rend LUI-MÊME le résumé affiché (`summary`, `summary_source: "nokker.ai"`,
`nokker/cloud/summary.py`) ; le titre et le motif de la machine deviennent secondaires. Une carte LOCALE (arguments
restés sur la machine, §6.7) porte `summary_source: "machine"` : la page la montre « décrite par ta machine », avec
l'empreinte. Les arguments ne sont gardés que jusqu'à la décision (statut final rendu par la machine) ou l'expiration,
puis effacés ; l'empreinte reste.

Magasins : `MemoryRelayStore` (tests, dev) et `nokker.cloud.pg.PgRelayStore` (migrations 021 et 027, sous RLS du
locataire).
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

STATUSES = ("pending", "forwarded", "approved", "rejected", "expired")
FINAL = ("approved", "rejected", "expired")
RE_APPROVAL_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_TITLE, MAX_REASON, MAX_CHALLENGE, MAX_PROOF_FIELD = 200, 600, 1024, 4096
MAX_TTL_S = 24 * 3600 + 300  # une carte vit 24 h au plus côté noyau (docs/APPROVALS.md §2), plus un peu d'horloge
MAX_WAIT_S = 25.0
MAX_ARGS_BYTES = 256 * 1024  # comme un appel relayé (nokker/cloud/relay.py::MAX_CALL_BYTES)
SOURCES = ("nokker.ai", "machine")
# Champs d'une preuve gardés tels quels (docs/APPROVALS.md §3.3, §3.4) ; tout le reste est jeté.
PROOF_FIELDS = ("device_id", "signature", "webauthn_client_data_json", "webauthn_authenticator_data",
                "webauthn_signature")
OUTCOMES = ("executed", "failed")


class RelayError(ValueError):
    """Requête refusée : `status` HTTP et message (en français, montré tel quel)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class RelayedApproval:
    machine_id: str
    approval_id: str
    tenant: str
    account: str
    title: str
    reason: str
    strength: str
    challenge: str
    expires_at: float
    created_at: float
    status: str = "pending"
    advice: str | None = None  # avis du juge (X8), caviardé par la machine ; None tant qu'il n'existe pas
    decision: str | None = None  # approve | reject, tant que `forwarded` (et gardé après pour le journal)
    proof: dict[str, str] | None = None
    decided_at: float | None = None  # heure de nokker.ai à la réception de la preuve
    result_at: float | None = None  # heure de nokker.ai à la réception du résultat de la machine
    outcome: str | None = None  # executed | failed, quand la machine a exécuté une carte approuvée
    error: str | None = None  # pourquoi la machine a refusé la dernière preuve
    # X20c — empreinte des arguments (celle du défi, gardée après l'effacement) ; arguments déposés par la machine pour
    # une carte relayée (None : carte locale, ou effacés après la décision ou l'expiration) ; résumé rendu par
    # nokker.ai (ou squelette d'une carte locale) ; qui décrit la carte à la page : « nokker.ai » ou « machine ».
    args_sha256: str | None = None
    arguments: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None
    summary_source: str = "machine"


def iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def view(a: RelayedApproval, now: float) -> dict[str, Any]:
    """Forme lue par la page `https://app.nokker.ai/approve/<machine_id>/<approval_id>` (dates en ISO 8601)."""
    from nokker.cloud.summary import erased, machine_summary

    status = "expired" if a.status == "pending" and now > a.expires_at else a.status
    summary = a.summary if a.summary is not None else machine_summary(_challenge_tool(a.challenge))
    if a.arguments is None or status in FINAL:  # effacés (ou à effacer) : jamais montrés après la décision
        summary = erased(summary)
    return {"approval_id": a.approval_id, "title": a.title, "reason": a.reason, "advice": a.advice,
            "strength": a.strength, "expires_at": iso(a.expires_at), "challenge": a.challenge, "status": status,
            "outcome": a.outcome, "error": a.error, "created_at": iso(a.created_at),
            "args_sha256": a.args_sha256, "summary_source": a.summary_source, "summary": summary}


def _challenge(text: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _challenge_tool(text: str) -> str | None:
    tool = (_challenge(text) or {}).get("tool")
    return tool if isinstance(tool, str) else None


class MemoryRelayStore:
    """Même interface que `PgRelayStore` ; filtrage par locataire comme la RLS de la migration 021."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], RelayedApproval] = {}
        self.lock = threading.Lock()

    def put(self, a: RelayedApproval) -> RelayedApproval | None:
        """Dépose ou redépose : remplace une carte non finale (nouveau défi : la machine a redémarré, ou l'ancien a
        expiré) ; rend None si la carte est déjà décidée (approuvée ou refusée) — on ne la rouvre pas."""
        with self.lock:
            old = self.rows.get((a.machine_id, a.approval_id))
            if old is not None and (old.tenant != a.tenant or old.status in ("approved", "rejected")):
                return None
            self.rows[(a.machine_id, a.approval_id)] = replace(a)
            return replace(a)

    def get(self, tenant: str, machine_id: str, approval_id: str) -> RelayedApproval | None:
        with self.lock:
            a = self.rows.get((machine_id, approval_id))
            return replace(a) if a is not None and a.tenant == tenant else None

    def of_machine(self, tenant: str, machine_id: str) -> list[RelayedApproval]:
        with self.lock:
            return sorted((replace(a) for a in self.rows.values() if a.machine_id == machine_id and a.tenant == tenant),
                          key=lambda a: -a.created_at)

    def forward(self, tenant: str, machine_id: str, approval_id: str, decision: str, proof: dict[str, str],
                now: float) -> RelayedApproval | None:
        """`pending` non expirée → `forwarded`, atomiquement ; None sinon (déjà transmise, décidée, expirée)."""
        with self.lock:
            a = self.rows.get((machine_id, approval_id))
            if a is None or a.tenant != tenant or a.status != "pending" or now > a.expires_at:
                return None
            a.status, a.decision, a.proof, a.decided_at, a.error = "forwarded", decision, dict(proof), now, None
            return replace(a)

    def forwarded(self, tenant: str, machine_id: str) -> list[RelayedApproval]:
        with self.lock:
            return [replace(a) for a in self.rows.values()
                    if a.machine_id == machine_id and a.tenant == tenant and a.status == "forwarded"]

    def set_result(self, tenant: str, machine_id: str, approval_id: str, status: str, outcome: str | None,
                   error: str | None, now: float) -> RelayedApproval | None:
        with self.lock:
            a = self.rows.get((machine_id, approval_id))
            if a is None or a.tenant != tenant:
                return None
            if status == "pending":  # preuve refusée par la machine : on peut signer à nouveau
                a.proof, a.decision, a.decided_at = None, None, None
            a.status, a.result_at, a.error = status, now, error
            a.outcome = outcome if outcome is not None else a.outcome
            if status in FINAL:  # décidée ou expirée : les arguments s'effacent, l'empreinte reste
                _erase(a)
            return replace(a)

    def purge(self, tenant: str, now: float) -> int:
        """Efface les arguments des cartes expirées du locataire (sans résultat de la machine : machine éteinte)."""
        with self.lock:
            n = 0
            for a in self.rows.values():
                if a.tenant == tenant and a.arguments is not None and now > a.expires_at:
                    _erase(a)
                    n += 1
            return n


def _erase(a: RelayedApproval) -> None:
    from nokker.cloud.summary import erased

    a.arguments, a.summary = None, erased(a.summary)


def _text(v: Any, limit: int) -> str:
    return (v if isinstance(v, str) else "").strip()[:limit]


def _epoch(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v:
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    return None


@dataclass
class RelayService:
    """Règles de la boîte aux lettres, au-dessus d'un magasin. Les routes (`nokker/cloud/http.py`) authentifient : la
    machine par son jeton (dépôt, relève, résultat, appareils), le compte par sa session (lecture, décision)."""

    store: Any = field(default_factory=MemoryRelayStore)
    clock: Any = time.time
    known: Any = None  # KnownSecrets du noyau de nokker.ai (caviardage du résumé, `nokker/qa/secrets.py`)
    _cond: threading.Condition = field(default_factory=threading.Condition, init=False, repr=False)

    # ─────────────── machine (jeton de machine) ───────────────

    def deposit(self, machine: Any, body: dict[str, Any]) -> tuple[RelayedApproval, bool]:
        """Carte forte posée sur la machine. Rend (carte, nouvelle) ; `nouvelle` = premier dépôt (à notifier)."""
        now = self.clock()
        aid = body.get("approval_id")
        if not isinstance(aid, str) or not RE_APPROVAL_ID.match(aid):
            raise RelayError(400, "approval_id invalide")
        challenge = body.get("challenge")
        if not isinstance(challenge, str) or not challenge or len(challenge) > MAX_CHALLENGE:
            raise RelayError(400, "challenge invalide")
        strength = body.get("strength")
        if strength not in ("strong", "standard"):
            raise RelayError(400, "strength invalide (strong|standard)")
        expires_at = _epoch(body.get("expires_at"))
        if expires_at is None or expires_at <= now or expires_at > now + MAX_TTL_S:
            raise RelayError(400, "expires_at invalide (futur, 24 h au plus)")
        title = _text(body.get("title"), MAX_TITLE)
        if not title:
            raise RelayError(400, "title requis")
        advice = _text(body.get("advice"), MAX_REASON) or None
        digest, arguments, summary, source = self._what_you_sign(aid, challenge, body)
        self._purge(machine.tenant, now)
        old = self.store.get(machine.tenant, machine.id, aid)
        card = RelayedApproval(machine_id=machine.id, approval_id=aid, tenant=machine.tenant, account=machine.account,
                               title=title, reason=_text(body.get("reason"), MAX_REASON), strength=strength,
                               challenge=challenge, expires_at=expires_at, advice=advice,
                               created_at=old.created_at if old is not None else now, args_sha256=digest,
                               arguments=arguments, summary=summary, summary_source=source)
        stored = self.store.put(card)
        if stored is None:
            raise RelayError(409, "carte déjà décidée")
        return stored, old is None

    def _what_you_sign(self, aid: str, challenge: str,
                       body: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None, str]:
        """(empreinte, arguments, résumé, source) d'un dépôt. Avec `arguments` (carte d'un connecteur relayé) : le défi
        doit porter sur cette carte et sur un outil, et l'empreinte des arguments REÇUS doit être la sienne, sinon 400 ;
        le résumé est alors rendu ici. Sans eux (carte locale) : l'empreinte du défi seule, décrite par la machine."""
        from nokker.approvals.crypto import args_sha256, canonical_json
        from nokker.cloud.summary import machine_summary, summarize

        ch = _challenge(challenge) or {}
        digest = ch.get("args_sha256") if isinstance(ch.get("args_sha256"), str) else None
        tool = ch.get("tool") if isinstance(ch.get("tool"), str) else None
        if "arguments" not in body:
            return digest, None, machine_summary(tool), "machine"
        arguments = body["arguments"]
        if not isinstance(arguments, dict):
            raise RelayError(400, "arguments invalides (objet attendu)")
        if len(canonical_json(arguments).encode()) > MAX_ARGS_BYTES:
            raise RelayError(413, "arguments trop grands")
        if ch.get("approval_id") != aid or ch.get("aud") != "nokker-approval" or not tool or not digest:
            raise RelayError(400, "défi illisible pour des arguments : il doit porter la carte, l'outil et "
                                  "l'empreinte des arguments")
        if args_sha256(arguments) != digest:
            raise RelayError(400, "arguments refusés : leur empreinte n'est pas celle du défi (args_sha256)")
        return digest, dict(arguments), summarize(tool, arguments, self.known), "nokker.ai"

    def _purge(self, tenant: str, now: float) -> None:
        purge = getattr(self.store, "purge", None)
        if callable(purge):
            purge(tenant, now)

    def decisions(self, machine: Any, wait_s: float = MAX_WAIT_S) -> list[RelayedApproval]:
        """Preuves transmises en attente de vérification par la machine ; attend jusqu'à `wait_s` (25 s au plus) qu'il
        y en ait une. Une preuve reste rendue tant que la machine n'a pas posté son résultat : une relève perdue ne
        perd rien, et la machine n'applique jamais deux fois (sa carte n'est plus en attente)."""
        deadline = time.monotonic() + max(0.0, min(float(wait_s), MAX_WAIT_S))
        self._purge(machine.tenant, self.clock())
        while True:
            found = self.store.forwarded(machine.tenant, machine.id)
            left = deadline - time.monotonic()
            if found or left <= 0:
                return found
            with self._cond:  # réveil immédiat par `decide` du même processus, sinon relecture chaque seconde
                self._cond.wait(min(1.0, left))

    def result(self, machine: Any, approval_id: str, body: dict[str, Any]) -> RelayedApproval:
        status = body.get("status")
        if status not in ("approved", "rejected", "expired", "pending"):
            raise RelayError(400, "status invalide (approved|rejected|expired|pending)")
        outcome = body.get("outcome")
        if outcome is not None and (outcome not in OUTCOMES or status != "approved"):
            raise RelayError(400, "outcome invalide (executed|failed, carte approuvée seulement)")
        current = self.store.get(machine.tenant, machine.id, approval_id)
        if current is None:
            raise RelayError(404, "carte inconnue")
        if status == "pending" and current.status != "forwarded":
            raise RelayError(409, "seule une preuve transmise peut être refusée")
        if current.status in FINAL and status != current.status:
            raise RelayError(409, f"carte déjà {current.status}")
        error = _text(body.get("error"), 300) or None
        out = self.store.set_result(machine.tenant, machine.id, approval_id, status, outcome, error, self.clock())
        if out is None:
            raise RelayError(404, "carte inconnue")
        return out

    # ─────────────── compte (session) ───────────────

    def list(self, tenant: str, machine_id: str) -> list[dict[str, Any]]:
        now = self.clock()
        self._purge(tenant, now)
        return [view(a, now) for a in self.store.of_machine(tenant, machine_id)]

    def get(self, tenant: str, machine_id: str, approval_id: str) -> dict[str, Any] | None:
        now = self.clock()
        self._purge(tenant, now)
        a = self.store.get(tenant, machine_id, approval_id)
        return view(a, now) if a is not None else None

    def decide(self, tenant: str, machine_id: str, approval_id: str, body: dict[str, Any]) -> RelayedApproval:
        """Garde la preuve TELLE QUELLE (jamais crue ici) et la passe à `forwarded`. Forme vérifiée seulement."""
        decision = body.get("decision")
        if decision not in ("approve", "reject"):
            raise RelayError(400, "decision invalide (approve|reject)")
        raw = body.get("proof")
        if not isinstance(raw, dict):
            raise RelayError(400, "proof requise")
        proof = {k: raw[k] for k in PROOF_FIELDS if isinstance(raw.get(k), str) and raw[k]}
        if any(len(v) > MAX_PROOF_FIELD for v in proof.values()):
            raise RelayError(400, "proof trop longue")
        webauthn = all(k in proof for k in PROOF_FIELDS[2:])
        if "device_id" not in proof or not (webauthn or "signature" in proof):
            raise RelayError(400, "proof : {device_id, webauthn_*} ou {device_id, signature}")
        if webauthn:
            proof.pop("signature", None)  # une seule preuve, la plus forte
        else:
            for k in PROOF_FIELDS[2:]:
                proof.pop(k, None)
        return self._forward(tenant, machine_id, approval_id, decision, proof)

    def card(self, tenant: str, machine_id: str, approval_id: str) -> RelayedApproval | None:
        """La carte telle que gardée (défi exact compris), pour que nokker.ai vérifie une preuve Telegram contre ELLE."""
        return self.store.get(tenant, machine_id, approval_id)

    def decide_attested(self, tenant: str, machine_id: str, approval_id: str, decision: str,
                        proof: dict[str, str]) -> RelayedApproval:
        """Décision par la Mini App Telegram : `proof` est l'attestation que nokker.ai vient de signer
        (`nokker/cloud/attest.py`), pas une preuve du client. Passe la carte à `forwarded` ; la machine l'accepte (ou la
        refuse, et la carte revient à `pending`)."""
        if decision not in ("approve", "reject"):
            raise RelayError(400, "decision invalide (approve|reject)")
        return self._forward(tenant, machine_id, approval_id, decision, proof)

    def _forward(self, tenant: str, machine_id: str, approval_id: str, decision: str,
                 proof: dict[str, str]) -> RelayedApproval:
        current = self.store.get(tenant, machine_id, approval_id)
        if current is None:
            raise RelayError(404, "carte inconnue")
        out = self.store.forward(tenant, machine_id, approval_id, decision, proof, self.clock())
        if out is None:
            now_status = view(current, self.clock())["status"]
            raise RelayError(409, f"carte déjà {now_status}")
        with self._cond:
            self._cond.notify_all()
        return out


@dataclass
class RelayedApprovalNotifier:
    """Carte relayée → Telegram du compte, s'il est appairé (bot de nokker.ai, `sendMessage` seulement) : la phrase de
    tête rendue par nokker.ai (X20c) quand la carte porte ses arguments, sinon le titre et le motif marqués « décrit
    par ta machine », et un bouton vers la page de décision, plus (carte forte, Mini App configurée) un bouton qui ouvre
    la carte dans la Mini App Telegram. Ni destinataire ni contenu dans Telegram : le détail est sur la page. Rien ne se
    décide dans le message : la preuve se donne dans la page ou la Mini App, avec la clé de l'appareil."""

    tenancy: Any
    bot: Any  # BotApi : send_with_buttons (Mini App) ou send_with_button(chat_id, text, label, url), sinon send(chat_id, text)
    app_url: str = "https://app.nokker.ai"
    # Page de la Mini App (`…/miniapp/approve.html`) : avec elle, un bouton ouvre CETTE carte dans la Mini App (Face ID),
    # à côté du lien vers la page web. Vide : le seul lien web, comme avant.
    mini_app_url: str = ""

    def __call__(self, machine: Any, card: RelayedApproval) -> None:
        account = self.tenancy.account(card.account) if hasattr(self.tenancy, "account") else None
        chats = self.tenancy.identities_of(card.account, "telegram")
        if not chats:
            return
        fr = getattr(account, "lang", "fr") != "en"
        head = (f"Action forte à approuver sur {machine.name}" if fr
                else f"Strong action awaiting your approval on {machine.name}")
        summary = getattr(card, "summary", None) or {}
        headline = summary.get("headline") if getattr(card, "summary_source", None) == "nokker.ai" else None
        said = "décrit par ta machine" if fr else "described by your machine"
        if headline:
            text = f"{head}\n{headline}\n({said} : {card.title})"
        else:
            text = f"{head}\n{said} : {card.title}" + (f"\n{card.reason}" if card.reason else "")
        url = f"{self.app_url.rstrip('/')}/approve/{card.machine_id}/{card.approval_id}"
        mini = f"{self.mini_app_url}?machine={card.machine_id}&approval={card.approval_id}" if self.mini_app_url else ""
        for chat in chats:
            send_buttons = getattr(self.bot, "send_with_buttons", None)
            if mini and callable(send_buttons) and card.strength == "strong":
                send_buttons(chat, text, [{"text": "Signer (Face ID)" if fr else "Sign (Face ID)", "web_app": {"url": mini}},
                                          {"text": "Page web" if fr else "Web page", "url": url}])
                continue
            send = getattr(self.bot, "send_with_button", None)
            if callable(send):
                send(chat, text, "Décider" if fr else "Decide", url)
            else:
                self.bot.send(chat, f"{text}\n{url}")


__all__ = ["FINAL", "MAX_ARGS_BYTES", "SOURCES", "STATUSES", "MemoryRelayStore", "RelayError", "RelayService",
           "RelayedApproval", "RelayedApprovalNotifier", "view"]
