"""Approbations fortes depuis le téléphone, côté machine (lot X10, docs/LOKKAL.md §7.3). Le pendant de
`nokker/cloud/approvals.py`.

Machine liée (jeton de machine au coffre, relu à chaque passage) : chaque carte FORTE en attente d'une session locale est
déposée chez nokker.ai (`POST /v1/gateway/approvals`) avec un défi à usage unique de CETTE machine. nokker.ai prévient
le compte (Telegram s'il est appairé), la page https://app.nokker.ai/approve/<machine_id>/<approval_id> fait signer
l'appareil, et nokker.ai garde la preuve sans la croire. Tant qu'une carte attend, la machine relève les preuves
(`GET /v1/gateway/approvals/decisions`, attente longue) et les vérifie ICI :

- clé publique de l'appareil prise dans `GET /v1/gateway/devices` (appareils du compte lié, gardés 5 min, relus sur un
  appareil inconnu), et ÉPINGLÉE à la première vue (`device_pins` de l'état local) : une clé qui change pour le même
  appareil est refusée ;
- `BaseApprovals.decide_remote` : même routine de preuve que la décision signée locale (WebAuthn avec UV pour une carte
  forte, origine et rpId de nokker.ai ; une clé logicielle P1363 ne suffit pas à une carte forte), même défi, nonce
  consommé après une preuve valide seulement : un rejeu, la preuve d'une autre carte ou d'un autre défi, une carte
  expirée ou déjà décidée sont refusés, et une décision ne s'applique qu'une fois ;
- la carte décidée est appliquée par le MÊME chemin que la décision locale (`apply` = `run_decided` de
  `nokker/api/server.py` : l'exécuteur d'approbation) ; le résultat (`approved|rejected|expired`, puis l'issue
  `executed|failed`) est rendu à nokker.ai (`POST /v1/gateway/approvals/{id}/result`). Une preuve refusée rend
  `pending` avec la raison : la carte attend toujours, l'appareil légitime peut signer.

Une carte décidée sur la machine (CLI) pendant qu'elle attend chez nokker.ai : son statut y est rendu aussi. Le jeton de
machine ne décide jamais rien : sans clé privée d'appareil, ni nokker.ai, ni un compte volé, ni le relais ne produisent
de preuve valable.

Connecteurs relayés (X20b) : le défi déposé lie aussi l'outil ; la signature d'une carte approuvée au téléphone, une fois
vérifiée ici, reste en mémoire (`signed_proof`) le temps que l'exécuteur la joigne à l'appel relayé : nokker.ai la
vérifie à son tour avant tout envoi externe ou destructif.

Ce que tu signes (X20c) : une carte forte d'un connecteur RELAYÉ (`RELAY_CARD_RULE`) est déposée avec ses `arguments`,
qui passeront de toute façon par nokker.ai à l'appel ; nokker.ai les confronte à l'empreinte du défi et rend lui-même
le résumé que la page affiche. Une carte forte LOCALE (ssh prod, retour arrière…) ne dépose jamais ses arguments (§6.7) :
la page la montre « décrite par ta machine », avec l'empreinte.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from nokker.approvals.errors import ApprovalExpired, ApprovalNotFound, ApprovalNotPending, ProofInvalid, \
    StrongDeviceRequired
from nokker.approvals.types import Device
from nokker.core.ports import Approval, Proof
from nokker.qa.permissions import RELAY_CARD_RULE
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

logger = logging.getLogger("nokker.local.remote_approvals")

RELAY_TTL_S = 3600.0  # une carte relayée se signe dans l'heure (bornée par l'expiration de la carte elle-même)
DEVICES_TTL_S = 300.0
DEVICES_MIN_REFRESH_S = 5.0  # appareil inconnu : relecture, mais pas plus d'une fois toutes les 5 s
POLL_WAIT_S = 25.0
SCAN_S = 15.0
MAX_TITLE, MAX_REASON = 200, 600
MAX_SIGNED = 256  # signatures vérifiées gardées pour les appels relayés (les plus récentes)
_FINAL = {"approved": "approved", "executed": "approved", "failed": "approved", "rejected": "rejected",
          "expired": "expired"}


def card_title(a: Approval) -> str:
    """Titre lisible d'une carte : l'outil et ce qu'il touche (commande, chemin), sans le reste des arguments."""
    args = a.arguments or {}
    target = args.get("command") or args.get("path") or args.get("cwd") or ""
    return f"{a.tool_name} : {target}" if target else a.tool_name


@dataclass
class RemoteApprovals:
    base_url: str  # https://app.nokker.ai
    token: Callable[[], str | None]  # jeton de machine (coffre local) ; None ou vide : machine non liée
    account: Callable[[], str | None]  # compte lié (état local du lien)
    approvals: Any  # BaseApprovals du noyau local
    apply: Callable[[Approval], Any]  # même chemin que la décision locale (exécuteur d'approbation)
    scrub: Callable[[str], str] = lambda s: s  # caviardage du noyau (invariants.scrub_output)
    origin: str | None = None  # origine WebAuthn attendue ; défaut : base_url (la page de décision)
    decided_by: str = "local-owner"  # identité locale sous laquelle l'outil approuvé s'exécute
    state: dict[str, Any] | None = None  # état local (setup.json) : `device_pins`
    save: Callable[[], None] | None = None
    relay_ttl_s: float = RELAY_TTL_S
    devices_ttl_s: float = DEVICES_TTL_S
    poll_wait_s: float = POLL_WAIT_S
    scan_s: float = SCAN_S
    client: Any = None  # httpx.Client injectable (tests)
    clock: Callable[[], float] = time.time
    last_status: int | None = None  # statut HTTP de la dernière relève (None : aucune)
    # approval_id → défi déposé (texte exact) : la preuve est vérifiée contre NOTRE défi, pas celui que rend nokker.ai
    _cards: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _skip: set[str] = field(default_factory=set, init=False, repr=False)  # refusées au dépôt (déjà décidées là-bas)
    # approval_id → signature vérifiée d'une carte approuvée ici (X20b) : jointe à l'appel relayé par l'exécuteur
    _signed: dict[str, dict[str, str]] = field(default_factory=dict, init=False, repr=False)
    _devices: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _devices_at: float = field(default=0.0, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _kick: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _waiting: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    # ─────────────── fils de fond ───────────────

    def start(self) -> None:
        threading.Thread(target=self._sync_loop, name="nokker-remote-approvals", daemon=True).start()
        threading.Thread(target=self._poll_loop, name="nokker-remote-decisions", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._kick.set()
        self._waiting.set()

    def kick(self, _approval: Any = None) -> None:
        """Une carte vient d'être posée : dépôt sans attendre le prochain passage (appelable depuis n'importe où)."""
        self._kick.set()

    def notify(self, approval: Approval) -> None:  # interface ApprovalNotifier : branché en chaîne par serve
        if approval.strength == "strong":
            self.kick()

    def _sync_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync_once()
            except Exception:  # noqa: BLE001 — un passage raté ne tue pas le fil
                logger.debug("approbations relayées : passage en échec", exc_info=True)
            self._kick.wait(self.scan_s)
            self._kick.clear()

    def _poll_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            with self._lock:
                waiting = bool(self._cards)
            if not waiting or not self.token():
                self._waiting.wait(self.scan_s)
                self._waiting.clear()
                continue
            t0 = time.monotonic()
            try:
                self.poll_once(self.poll_wait_s)
                ok = self.last_status == 200
            except Exception:  # noqa: BLE001 — nokker.ai injoignable : on réessaie, de plus en plus lentement
                logger.debug("approbations relayées : relève en échec", exc_info=True)
                ok = False
            if not ok:  # refus (429, 404 d'un nokker.ai sans la route, 401) ou panne : attente croissante
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            backoff = 1.0
            if time.monotonic() - t0 < 1.0:  # réponse immédiate (preuves rendues) : jamais plus d'une relève par seconde
                self._stop.wait(1.0)

    # ─────────────── HTTP ───────────────

    def _http(self) -> Any:
        if self.client is None:
            self.client = httpx.Client(timeout=httpx.Timeout(10.0, read=self.poll_wait_s + 10.0))
        return self.client

    def _url(self, path: str) -> str:
        return self.base_url.rstrip("/") + path

    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def _result(self, token: str, approval_id: str, status: str, outcome: str | None = None,
                error: str | None = None) -> bool:
        body: dict[str, Any] = {"status": status}
        if outcome:
            body["outcome"] = outcome
        if error:
            body["error"] = error[:300]
        try:
            r = self._http().post(self._url(f"/v1/gateway/approvals/{approval_id}/result"), json=body,
                                  headers=self._headers(token))
        except httpx.HTTPError as e:
            logger.debug("approbations relayées : résultat de %s reporté (%s)", approval_id, type(e).__name__)
            return False
        return r.status_code in (200, 404, 409)  # 404/409 : rien à rendre de plus, on n'insiste pas

    # ─────────────── dépôt et résultats des cartes décidées ici ───────────────

    def sync_once(self) -> int:
        """Dépose les cartes fortes en attente pas encore déposées ; rend le statut de celles décidées ou expirées sur la
        machine entre-temps. Rend le nombre de cartes déposées."""
        token = self.token()
        if not token or not self.account():
            return 0
        deposited = 0
        with tenant_scope(INTERNAL_TENANT):  # le fil de fond n'a pas de requête autour de lui (cf. MirrorWorker)
            for a in self.approvals.pending():
                with self._lock:
                    known = a.id in self._cards or a.id in self._skip
                if a.strength != "strong" or known:
                    continue
                if self._deposit(token, a):
                    deposited += 1
            with self._lock:
                ids = list(self._cards)
            for aid in ids:
                card = self.approvals.get(aid)
                status = "expired" if card is None else card.status
                if status == "pending":
                    continue
                outcome = status if status in ("executed", "failed") else None
                if self._result(token, aid, _FINAL.get(status, "expired"), outcome):
                    with self._lock:
                        self._cards.pop(aid, None)
        if deposited:
            self._waiting.set()
        return deposited

    def _deposit(self, token: str, a: Approval) -> bool:
        try:
            # l'outil entre dans le défi : nokker.ai vérifie qu'une signature d'envoi porte sur CET outil (X20b)
            ch = self.approvals.challenge(a.id, ttl_s=self.relay_ttl_s, bind_tool=True)
        except (ApprovalNotFound, ApprovalExpired, ApprovalNotPending):
            return False
        challenge = ch.canonical()
        body: dict[str, Any] = {
            "approval_id": a.id, "title": self.scrub(card_title(a))[:MAX_TITLE],
            "reason": self.scrub(a.reason or "")[:MAX_REASON], "strength": a.strength, "expires_at": ch.exp,
            "challenge": challenge}
        if a.advice:  # avis du juge (X8) quand il existe : caviardé comme le motif
            body["advice"] = self.scrub(a.advice)[:MAX_REASON]
        if a.rule == RELAY_CARD_RULE:
            # Connecteur relayé (X20c) : les arguments, tels quels (leur empreinte est celle du défi), pour que
            # nokker.ai montre lui-même ce qui sera signé. Jamais pour une carte locale : ils ne sortent pas (§6.7).
            body["arguments"] = dict(a.arguments or {})
        try:
            r = self._http().post(self._url("/v1/gateway/approvals"), json=body, headers=self._headers(token))
        except httpx.HTTPError as e:
            logger.debug("approbations relayées : dépôt de %s reporté (%s)", a.id, type(e).__name__)
            return False
        if r.status_code in (200, 201):
            with self._lock:
                self._cards[a.id] = challenge
            note = getattr(self.approvals, "note_forwarded", None)
            if note is not None:  # les éditeurs disent « attend ton téléphone » sur ce signal, pas avant
                note(a)
            return True
        if r.status_code == 409:  # déjà décidée chez nokker.ai : on ne la redépose pas en boucle
            with self._lock:
                self._skip.add(a.id)
        elif r.status_code in (400, 413):  # refus de forme (arguments trop grands, empreinte) : il ne changera pas
            logger.warning("approbations relayées : dépôt de %s refusé (HTTP %d), la carte reste sur la machine",
                           a.id, r.status_code)
            with self._lock:
                self._skip.add(a.id)
        else:
            logger.debug("approbations relayées : dépôt de %s refusé (HTTP %d)", a.id, r.status_code)
        return False

    def forwarded(self, approval_id: str) -> bool:
        """La carte est-elle déposée chez nokker.ai (lien de décision parti au téléphone) ? Lu par
        `GET /v1/local/approvals` (champ `forwarded`)."""
        with self._lock:
            return approval_id in self._cards

    # ─────────────── relève et vérification des preuves ───────────────

    def poll_once(self, wait_s: float | None = None) -> list[str]:
        """Une relève (attente longue côté nokker.ai) ; chaque preuve est vérifiée et sa carte décidée ici. Rend les
        statuts rendus à nokker.ai, dans l'ordre. Lève `httpx.HTTPError` si nokker.ai est injoignable."""
        token = self.token()
        if not token or not self.account():
            return []
        wait = self.poll_wait_s if wait_s is None else wait_s
        r = self._http().get(self._url("/v1/gateway/approvals/decisions"), params={"wait": wait},
                             headers=self._headers(token))
        self.last_status = r.status_code
        if r.status_code != 200:
            logger.debug("approbations relayées : relève refusée (HTTP %d)", r.status_code)
            return []
        out = []
        with tenant_scope(INTERNAL_TENANT):
            for d in (r.json() or {}).get("decisions") or []:
                if isinstance(d, dict):
                    out.append(self.handle(token, d))
        return out

    def handle(self, token: str, d: dict[str, Any]) -> str:
        """Vérifie UNE preuve relevée et décide la carte comme une décision locale ; rend le statut rendu."""
        aid = str(d.get("approval_id") or "")
        raw = d.get("proof") if isinstance(d.get("proof"), dict) else {}
        with self._lock:
            challenge = self._cards.get(aid)
        # Défi : celui que NOUS avons déposé. Après un redémarrage (carte en base), celui que rend nokker.ai, qui ne
        # passe que si son nonce est encore réservé ici pour CETTE carte (`_consume_nonce`).
        proof = Proof(challenge=challenge or str(d.get("challenge") or ""), device_id=raw.get("device_id"),
                      signature=raw.get("signature"),
                      webauthn_client_data_json=raw.get("webauthn_client_data_json"),
                      webauthn_authenticator_data=raw.get("webauthn_authenticator_data"),
                      webauthn_signature=raw.get("webauthn_signature"))
        origin = (self.origin or self.base_url).rstrip("/")
        try:
            device = self._device(token, str(raw.get("device_id") or ""))
            decided = self.approvals.decide_remote(aid, str(d.get("decision") or ""), proof, device, origin=origin,
                                                   rp_id=urlsplit(origin).hostname, decided_by=self.decided_by)
        except ProofInvalid as e:
            logger.warning("approbation relayée %s : preuve refusée (%s)", aid, e)
            self._result(token, aid, "pending", error=f"preuve refusée par la machine : {e}")
            return "pending"
        except (ApprovalExpired, ApprovalNotFound) as e:
            self._result(token, aid, "expired",
                         error="carte inconnue de la machine" if isinstance(e, ApprovalNotFound) else None)
            self._forget(aid)
            return "expired"
        except ApprovalNotPending:
            card = self.approvals.get(aid)
            status = _FINAL.get(card.status if card is not None else "expired", "expired")
            self._result(token, aid, status)  # déjà décidée (ici ou par une relève précédente) : jamais deux fois
            self._forget(aid)
            return status
        if decided.status == "approved":  # avant l'exécution : l'exécuteur la joint à l'appel relayé
            self._keep_signed(aid, proof)
        self._result(token, aid, decided.status)
        self._forget(aid)
        if decided.status == "approved":
            threading.Thread(target=self._run, args=(token, decided), name="nokker-remote-apply", daemon=True).start()
        else:
            self._run(token, decided)
        return decided.status

    def _run(self, token: str, decided: Approval) -> None:
        """Le chemin de la décision locale (exécuteur), puis l'issue rendue à nokker.ai pour une carte approuvée."""
        try:
            self.apply(decided)
        except Exception:  # noqa: BLE001 — l'échec est sur la carte (mark_failed), comme en local
            logger.exception("approbation relayée %s : exécution en échec", decided.id)
        if decided.status != "approved":
            return
        with tenant_scope(INTERNAL_TENANT):
            card = self.approvals.get(decided.id)
        if card is not None and card.status in ("executed", "failed"):
            self._result(token, decided.id, "approved", outcome=card.status)

    def _forget(self, aid: str) -> None:
        with self._lock:
            self._cards.pop(aid, None)

    def _keep_signed(self, aid: str, proof: Proof) -> None:
        kept = {"challenge": proof.challenge or "", "decision": "approve", "device_id": proof.device_id or ""}
        for k in ("signature", "webauthn_client_data_json", "webauthn_authenticator_data", "webauthn_signature"):
            v = getattr(proof, k)
            if v:
                kept[k] = v
        with self._lock:
            self._signed[aid] = kept
            while len(self._signed) > MAX_SIGNED:
                self._signed.pop(next(iter(self._signed)))

    def signed_proof(self, approval_id: str) -> dict[str, str] | None:
        """Signature d'appareil vérifiée de la carte `approval_id` approuvée au téléphone (X20b), ou None. Ce n'est pas
        un secret (nokker.ai l'a déjà) ; nokker.ai la vérifie lui-même et n'en accepte qu'un usage."""
        with self._lock:
            found = self._signed.get(approval_id)
            return dict(found) if found is not None else None

    # ─────────────── appareils du compte lié ───────────────

    def _refresh_devices(self, token: str) -> None:
        r = self._http().get(self._url("/v1/gateway/devices"), headers=self._headers(token))
        if r.status_code != 200:
            raise ProofInvalid(f"appareils du compte illisibles (HTTP {r.status_code})")
        items = (r.json() or {}).get("devices") or []
        with self._lock:
            self._devices = {str(x["device_id"]): x for x in items
                             if isinstance(x, dict) and x.get("device_id") and x.get("public_key_spki")}
            self._devices_at = self.clock()

    def _device(self, token: str, device_id: str) -> Device:
        if not device_id:
            raise ProofInvalid("preuve sans appareil (`device_id`)")
        now = self.clock()
        with self._lock:
            d = self._devices.get(device_id) if now - self._devices_at < self.devices_ttl_s else None
            may_refresh = now - self._devices_at >= DEVICES_MIN_REFRESH_S
        if d is None and may_refresh:
            try:
                self._refresh_devices(token)
            except httpx.HTTPError as e:
                raise ProofInvalid(f"appareils du compte injoignables ({type(e).__name__})") from e
            with self._lock:
                d = self._devices.get(device_id)
        if d is None:
            raise ProofInvalid("appareil inconnu du compte lié")
        if not d.get("can_decide"):
            raise StrongDeviceRequired("appareil de simple connexion : il ne décide pas d'une approbation")
        spki = str(d["public_key_spki"])
        pins = self.state.setdefault("device_pins", {}) if self.state is not None else {}
        pinned = pins.get(device_id)
        if pinned is not None and pinned != spki:
            # nokker.ai rend une autre clé pour le même appareil : refus net (on ne suit pas une clé qui change).
            raise ProofInvalid("clé de l'appareil changée depuis sa première vue : refusée")
        if pinned is None and self.state is not None:
            pins[device_id] = spki
            if self.save is not None:
                try:
                    self.save()
                except Exception:  # noqa: BLE001 — l'épinglage reste en mémoire
                    logger.debug("épinglage de l'appareil %s non sauvé", device_id, exc_info=True)
        return Device(id=device_id, code="", user=str(self.account() or ""), name=str(d.get("name") or ""),
                      public_key_spki=spki, status=str(d.get("status") or ""), approved_via="nokker.ai")


__all__ = ["RemoteApprovals", "card_title"]
