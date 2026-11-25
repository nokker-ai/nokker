"""ACCORD — appairage, connexions, file d'une carte à la fois et décisions (docs/ACCORD-DEVICE.md v1.1 §2-§4).

Sans réseau : le serveur WSS (`nokker/accord/server.py`) et le simulateur passent les messages JSON à `Session.handle`
et envoient ce que rend `Session.poll`. Tout l'état vit ici (fenêtre d'appairage, demandes en attente, verrou après
3 codes faux, nonces à usage unique, débit) ; l'horloge est celle de l'HÔTE (`clock`), jamais le `ts` de l'appareil.

La décision passe par l'API d'approbation existante (`ApprovalsBase.decide_local` : même ledger, mêmes planchers),
`via = "accord:<device_id>"` (au ledger) ; `decision_reason` reste VIDE : aucun identifiant dans ce que voit le modèle. AUTORISER une carte `strong` est
refusé ici (`strong_card`) avant tout appel ; REFUSER est toujours permis."""
from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.accord import protocol as P
from nokker.accord.policy import level_of, title_of
from nokker.accord.store import Device
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

logger = logging.getLogger(__name__)

WINDOW_S = 120.0          # fenêtre d'appairage ouverte par « Ajouter un appareil »
PENDING_MAX = 3           # demandes d'appairage en attente, au plus
LOCK_AFTER = 3            # codes faux avant verrou
LOCK_S = 600.0            # verrou de l'appairage
RATE_MAX = 5              # décisions par minute et par appareil
RATE_WINDOW_S = 60.0
SEEN_EVERY_S = 60.0       # last_seen_at écrite au plus une fois par minute et par appareil
DECIDER = "local-owner"   # identité locale de Lokkal (nokker/local/api.py LOCAL_OWNER)

# Clé PUBLIQUE de test des vecteurs (graine publiée dans docs/accord-vectors.json) : n'importe qui peut signer avec.
# Jamais appairée, sauf `local.accord.allow_test_key` (tests et démo émulée seulement).
def _test_pubkey() -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from nokker.accord.vectors import TEST_SEED

    return P.raw_public(Ed25519PrivateKey.from_private_bytes(TEST_SEED))


TEST_PUBKEY = _test_pubkey()


class PairError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class PairRequest:
    pubkey: str
    mac: str
    at: float
    notify: Callable[[dict[str, Any]], None]


@dataclass
class Sent:
    card: dict[str, Any]
    used: bool = False


@dataclass
class Session:
    """Une connexion d'appareil. `handle(msg)` rend les messages à envoyer tout de suite ; `poll()` ceux qui viennent de
    la file (nouvelle carte, `withdraw`). `outbox` reçoit ce que l'hôte pousse hors d'une réponse (`paired`)."""

    hub: AccordHub
    outbox: deque = field(default_factory=deque)
    pubkey: str | None = None
    challenge: str | None = None
    device: Device | None = None
    current: Sent | None = None
    closed: bool = False
    revoked: bool = False

    def handle(self, msg: Any) -> list[dict[str, Any]]:
        return self.hub.handle(self, msg)

    def poll(self) -> list[dict[str, Any]]:
        out = list(self.outbox)
        self.outbox.clear()
        return out + self.hub.poll(self)


def _err(code: str, card_id: str | None = None) -> dict[str, Any]:
    return {"type": "error", "code": code, **({"card_id": card_id} if card_id else {})}


class AccordHub:
    def __init__(self, approvals: Any, store: Any, host_fp: str, *, clock: Callable[[], float] = time.time,
                 allow_test_key: bool = False, tenant: str = INTERNAL_TENANT,
                 execute: Callable[[Any], Any] | None = None) -> None:
        if not P.is_host_fp(host_fp):
            raise ValueError("host_fp : SHA-256 hex attendu")
        self.approvals, self.store, self.host_fp, self.clock = approvals, store, host_fp, clock
        self.allow_test_key, self.tenant, self.execute = allow_test_key, tenant, execute
        self._lock = threading.RLock()
        self._window_until = 0.0
        self._pending: list[PairRequest] = []
        self._wrong = 0
        self._locked_until = 0.0
        self._sessions: list[Session] = []
        self._rate: dict[str, deque] = {}
        self._seen: dict[str, float] = {}  # dernière écriture de last_seen_at, par appareil (au plus 1/min)

    # ─────────────────────── appairage (côté app : routes /v1/local/accord*) ───────────────────────

    def status(self) -> dict[str, Any]:
        now = self.clock()
        with self._lock, tenant_scope(self.tenant):
            return {"enabled": True, "host_fp": self.host_fp, "host_fp8": self.host_fp[:8],
                    "pairing": {"open": now < self._window_until,
                                "closes_in_s": max(0, int(self._window_until - now)),
                                "locked": now < self._locked_until,
                                "locked_for_s": max(0, int(self._locked_until - now)),
                                "requests": [{"key8": P.key8(r.pubkey)} for r in self._live_requests(now)]},
                    "devices": [d.to_json() for d in self.store.list()]}

    def open_window(self) -> dict[str, Any]:
        now = self.clock()
        with self._lock:
            if now < self._locked_until:
                raise PairError("pairing_locked")
            self._window_until, self._pending = now + WINDOW_S, []
        return self.status()

    def _live_requests(self, now: float) -> list[PairRequest]:
        if now >= self._window_until:
            self._pending = []
        self._pending = [r for r in self._pending if now - r.at < WINDOW_S]
        return self._pending

    def confirm_code(self, code: Any) -> Device:
        """Code à 6 chiffres saisi dans l'app : la SEULE demande dont le mac correspond est appairée."""
        now = self.clock()
        with self._lock:
            if now < self._locked_until:
                raise PairError("pairing_locked")
            if now >= self._window_until:
                raise PairError("pairing_closed")
            code = str(code or "").strip()
            live = self._live_requests(now)
            hits = [r for r in live if P.mac_matches(code, r.pubkey, self.host_fp, r.mac)]
            if len(hits) > 1:  # plusieurs demandes pour un code : quelqu'un inonde le LAN → on ferme
                self._window_until, self._pending = 0.0, []
                raise PairError("ambiguous")
            if not hits:
                self._wrong += 1
                if self._wrong >= LOCK_AFTER:
                    self._wrong, self._locked_until = 0, now + LOCK_S
                    self._window_until, self._pending = 0.0, []
                    raise PairError("pairing_locked")
                raise PairError("bad_code")
            req = hits[0]
            device = Device(device_id="acc_" + secrets.token_hex(6), pubkey=req.pubkey, key8=P.key8(req.pubkey),
                            host_fp=self.host_fp, paired_at=now)
            with tenant_scope(self.tenant):
                self.store.add(device)
            self._wrong, self._window_until, self._pending = 0, 0.0, []
        logger.info("accord : appareil %s appairé (clé %s)", device.device_id, device.key8)
        req.notify({"type": "paired", "device_id": device.device_id, "host_fp": self.host_fp, "key8": device.key8})
        return device

    def revoke(self, device_id: str) -> bool:
        with self._lock, tenant_scope(self.tenant):
            ok = self.store.revoke(device_id, self.clock())
            for s in self._sessions:
                if s.device is not None and s.device.device_id == device_id:
                    s.closed, s.revoked, s.device, s.current = True, True, None, None
        if ok:
            logger.info("accord : appareil %s révoqué", device_id)
        return ok

    def rename(self, device_id: str, raw: Any) -> Any:
        """Nom donné par la personne (≤ 40, NFC, sans contrôle) ; vide → nom par défaut. None si inconnu ou révoqué ;
        ValueError("bad_name") si refusé."""
        from nokker.accord.store import clean_name

        name = clean_name(raw)
        with self._lock, tenant_scope(self.tenant):
            if not self.store.rename(device_id, name):
                return None
            return self.store.get(device_id)

    def _seen_now(self, device_id: str) -> None:
        now = self.clock()
        if now - self._seen.get(device_id, float("-inf")) < SEEN_EVERY_S:
            return
        self._seen[device_id] = now
        try:
            with tenant_scope(self.tenant):
                self.store.touch(device_id, now)
        except Exception:  # noqa: BLE001 — une date de connexion ne doit jamais couper l'appareil
            logger.warning("accord : last_seen_at non écrit pour %s", device_id, exc_info=True)

    # ─────────────────────── connexion d'un appareil ───────────────────────

    def session(self) -> Session:
        s = Session(hub=self)
        with self._lock:
            self._sessions = [x for x in self._sessions if not x.closed] + [s]
        return s

    def close(self, s: Session) -> None:
        with self._lock:
            s.closed = True
            self._sessions = [x for x in self._sessions if x is not s]

    def handle(self, s: Session, msg: Any) -> list[dict[str, Any]]:
        if s.closed:  # appareil révoqué pendant la connexion : toute réponse est refusée, et dite
            return [_err("revoked")] if s.revoked else []
        if not isinstance(msg, dict):
            return [_err("bad_card")]
        kind = msg.get("type")
        if kind == "hello":
            return self._hello(s, msg)
        if kind == "auth":
            return self._auth(s, msg)
        if kind == "pair":
            return self._pair(s, msg)
        if kind == "decision":
            return self._decision(s, msg)
        if kind == "error":  # l'appareil refuse une carte (`bad_card`) : on passe à la suivante
            if s.current is not None and msg.get("card_id") == s.current.card["card_id"]:
                logger.warning("accord : l'appareil refuse la carte %s (%s)", msg.get("card_id"), msg.get("code"))
                s.current = None
            return []
        return [_err("bad_card")]

    def _hello(self, s: Session, msg: dict[str, Any]) -> list[dict[str, Any]]:
        if msg.get("v") != P.VERSION:
            return [_err("bad_version")]
        pubkey = msg.get("pubkey")
        try:
            P.public_key_of(pubkey)
        except (ValueError, TypeError):
            return [_err("bad_sig")]
        with tenant_scope(self.tenant):
            device = self.store.by_pubkey(pubkey)
        if device is None:
            return [_err("unpaired")]
        if not device.active:
            return [_err("revoked")]
        s.pubkey, s.device, s.challenge = pubkey, None, P.b64u(secrets.token_bytes(16))
        return [{"type": "challenge", "nonce": s.challenge}]

    def _auth(self, s: Session, msg: dict[str, Any]) -> list[dict[str, Any]]:
        if s.pubkey is None or s.challenge is None:
            return [_err("unpaired")]
        nonce, s.challenge = s.challenge, None  # un défi ne sert qu'une fois
        if not P.verify(s.pubkey, P.hello_bytes(nonce, self.host_fp), msg.get("sig") or ""):
            return [_err("bad_sig")]
        with tenant_scope(self.tenant):
            device = self.store.by_pubkey(s.pubkey)
        if device is None or not device.active:
            return [_err("revoked")]
        s.device = device
        self._seen_now(device.device_id)
        return [{"type": "ready", "device_id": device.device_id}]

    def _pair(self, s: Session, msg: dict[str, Any]) -> list[dict[str, Any]]:
        pubkey, mac = msg.get("pubkey"), msg.get("mac")
        try:
            P.public_key_of(pubkey)
            P.unb64u(mac)
        except (ValueError, TypeError):
            return [_err("bad_sig")]
        if pubkey == TEST_PUBKEY and not self.allow_test_key:
            return [_err("test_key")]
        now = self.clock()
        with self._lock:
            if now < self._locked_until:
                return [_err("pairing_locked")]
            live = self._live_requests(now)
            if now >= self._window_until:
                return [_err("pairing_closed")]
            with tenant_scope(self.tenant):
                known = self.store.by_pubkey(pubkey)
            if known is not None and known.active:
                return [{"type": "paired", "device_id": known.device_id, "host_fp": self.host_fp,
                         "key8": known.key8}]
            if len(live) >= PENDING_MAX:
                return [_err("pairing_closed")]
            live.append(PairRequest(pubkey=pubkey, mac=mac, at=now, notify=s.outbox.append))
        return []

    # ─────────────────────── cartes ───────────────────────

    def _pending_cards(self) -> list[Any]:
        with tenant_scope(self.tenant):
            items = self.approvals.pending()
        return sorted(items, key=lambda a: (a.created_at, a.id))

    def _card_for(self, approval: Any) -> dict[str, Any] | None:
        card = {"type": "card", "card_id": approval.id, "title": title_of(approval), "level": level_of(approval),
                "expire_at": int(approval.expires_at), "nonce": P.b64u(secrets.token_bytes(16))}
        try:
            P.check_card(card)
        except P.BadCard as e:  # jamais envoyée : un 0x1F ou un texte non NFC rendrait `shown` ambigu
            logger.warning("accord : carte %s non envoyée (%s)", approval.id, e)
            return None
        return card

    def poll(self, s: Session) -> list[dict[str, Any]]:
        """Une carte à la fois : retire la carte montrée si elle n'attend plus (tranchée ailleurs, expirée), puis montre
        la plus ancienne carte en attente."""
        if s.device is None or s.closed:
            return []
        out: list[dict[str, Any]] = []
        now = self.clock()
        pending = self._pending_cards()
        ids = {a.id for a in pending}
        if s.current is not None:
            cid = s.current.card["card_id"]
            if cid not in ids or s.current.card["expire_at"] <= now:
                out.append({"type": "withdraw", "card_id": cid})
                s.current = None
        if s.current is None:
            for a in pending:
                if a.expires_at <= now:
                    continue
                card = self._card_for(a)
                if card is not None:
                    s.current = Sent(card)
                    out.append(card)
                    break
        return out

    def _rate_ok(self, device_id: str, now: float) -> bool:
        q = self._rate.setdefault(device_id, deque())
        while q and now - q[0] >= RATE_WINDOW_S:
            q.popleft()
        if len(q) >= RATE_MAX:
            return False
        q.append(now)
        return True

    def _decision(self, s: Session, msg: dict[str, Any]) -> list[dict[str, Any]]:
        card_id = msg.get("card_id") if isinstance(msg.get("card_id"), str) else None
        if s.device is None:
            return [_err("unpaired", card_id)]
        with tenant_scope(self.tenant):
            device = self.store.get(s.device.device_id)
        if device is None or not device.active:  # 1. appareil appairé, non révoqué
            s.closed = True
            return [_err("revoked", card_id)]
        sent = s.current
        if sent is None or card_id != sent.card["card_id"]:
            return [_err("unknown_card", card_id)]
        card = sent.card
        decision, ts = msg.get("decision"), msg.get("ts")
        try:
            data = P.decision_bytes(card_id, decision, str(msg.get("nonce") or ""), str(msg.get("shown") or ""), ts)
        except (ValueError, TypeError):
            return [_err("bad_sig", card_id)]
        if not P.verify(device.pubkey, data, msg.get("sig") or ""):  # 1. signature
            return [_err("bad_sig", card_id)]
        if msg.get("nonce") != card["nonce"] or sent.used:  # 2. nonce de CETTE carte, une seule fois
            return [_err("bad_nonce", card_id)]
        sent.used = True
        now = self.clock()
        if card["expire_at"] <= now:  # 2. délai jugé à l'horloge de l'hôte
            s.current = None
            return [_err("expired", card_id)]
        if msg.get("shown") != P.shown(card["title"], card["level"], card["expire_at"]):  # 3. ce qui a été montré
            sent.used = False
            return [_err("bad_shown", card_id)]
        if decision == "approve" and card["level"] == "strong":  # 4. AUTORISER refusé pour une carte forte
            sent.used = False
            return [_err("strong_card", card_id)]
        with self._lock:
            if not self._rate_ok(device.device_id, now):  # 5. débit
                sent.used = False
                return [_err("rate_limited", card_id)]
        tag = f"accord:{device.device_id}"
        try:
            with tenant_scope(self.tenant):  # 6. API d'approbation existante, même ledger
                # decision_reason VIDE (dev, 18.11) : le motif d'un refus remonte au modèle ; seul `via` (ledger) nomme l'appareil.
                decided = self.approvals.decide_local(card_id, "approve" if decision == "approve" else "reject",
                                                      DECIDER, via=tag)
        except Exception as e:  # noqa: BLE001 — ApprovalExpired, ApprovalNotPending, ProofInvalid (planchers)…
            logger.info("accord : décision %s sur %s refusée par les approbations (%s)", decision, card_id, e)
            s.current = None
            name = type(e).__name__
            # Carte tranchée AILLEURS entre l'envoi et la décision (CLI, web, autre appareil) : l'appareil doit afficher
            # « déjà tranchée », jamais « accepté » ; rien n'est écrit (la décision gagnante est déjà au ledger).
            return [_err("expired" if "Expired" in name else "strong_card" if "Proof" in name
                         else "already_decided" if "NotPending" in name else "unknown_card", card_id)]
        s.current = None
        logger.info("accord : carte %s %s par %s", card_id, decided.status, device.device_id)
        if self.execute is not None:
            try:
                self.execute(decided)
            except Exception:
                logger.exception("accord : exécution de la carte %s", card_id)
        return []
