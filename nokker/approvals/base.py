"""Logique commune aux deux backends `Approvals` (`memory.InMemoryApprovals`, `pg.PgApprovals`) —
docs/APPROVALS.md §2-§4. Un backend concret n'a qu'à fournir les primitives de stockage ci-dessous
(`_load_*`/`_store_*`/`_consume_nonce`/...) ; toute la logique de preuve (signature, TOTP, Telegram,
anti-rejeu, anti-falsification des arguments) vit ICI, une seule fois, pour ne jamais diverger entre
les deux implémentations.

`BaseApprovals` couvre aussi l'enrôlement d'appareil et les sessions (docs/CLIENT-API.md) : ce ne sont
pas des méthodes du Protocol `Approvals` (core/ports.py, qui ne porte que ce dont `Kernel` a besoin),
mais `nokker/api/server.py` et `nokker/cli.py` en ont besoin sur le MÊME backend (une seule source de
vérité pour devices/sessions/nonces/totp)."""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from nokker.approvals.crypto import (
    SignatureError, TelegramInitDataError, WebAuthnError, args_sha256, new_nonce, totp_matching_step,
    verify_p1363_signature, verify_telegram_init_data, verify_webauthn_assertion,
)
from nokker.approvals.errors import (
    ApprovalExpired, ApprovalNotFound, ApprovalNotPending, DeviceNotApproved, DeviceNotFound, ProofInvalid,
    StrongDeviceRequired,
)
from nokker.approvals.notify import ApprovalNotifier, NullNotifier
from nokker.approvals.types import Device, Session
from nokker.core.ports import Approval, Challenge, Proof
from nokker.core.turn import ToolCall, Turn, Verdict
from nokker.tenancy.context import INTERNAL_TENANT, current_tenant_or_none, tenant_of_thread

logger = logging.getLogger("nokker.approvals")

DEFAULT_TTL_S = 24 * 3600.0
CHALLENGE_TTL_S = 120.0
SESSION_TTL_S = 12 * 3600.0


# Appareils nés d'une simple connexion (code e-mail, et Google plus tard) : ils ouvrent une session, ils ne décident
# jamais d'une approbation (décision de Nick, 21.10.2026 — scope §16).
LOGIN_ONLY_VIA = ("email-code", "google")


def _require_deciding_device(device: Device) -> None:
    if (device.approved_via or "").startswith(LOGIN_ONLY_VIA):
        raise StrongDeviceRequired("strong_device_required")


def _default_authorized(user: str | None) -> bool:
    from nokker.qa.invariants import GoAuthorizedSenders

    return GoAuthorizedSenders().authorized(user)


@dataclass
class BaseApprovals:
    notifier: ApprovalNotifier = field(default_factory=NullNotifier)
    is_authorized: Callable[[str | None], bool] = _default_authorized
    telegram_bot_token: str | None = None
    telegram_max_age_s: int = 600
    # Origine attendue dans `clientDataJSON.origin` pour une preuve WebAuthn (web/passkey, ADR
    # `nokker-apps/docs/adr/0001-webauthn-vs-ecdsa-contrat.md`) — `None` = WebAuthn refusé (pas une
    # preuve qu'on peut valider sans savoir quelle origine est légitime).
    webauthn_origin: str | None = None
    default_ttl_s: float = DEFAULT_TTL_S
    challenge_ttl_s: float = CHALLENGE_TTL_S
    session_ttl_s: float = SESSION_TTL_S
    clock: Callable[[], float] = time.time
    # Publié à chaque transition notable (`approval.requested`/`approval.decided`, docs/CLIENT-API.md
    # §Événements) — `nokker/api/server.py` y branche l'EventBus de `/v1/events` (SSE). `None` = silence
    # (tous les tests unitaires de ce module).
    on_event: Callable[[str, dict], None] | None = None
    # Multi-locataire (docs/TENANCY.md §5) : locataire d'un compte `us_…` actif, `None` sinon
    # (`nokker.tenancy.resolve.account_tenant_fn`, câblé par `serve`). `None` = aucun testeur ne décide jamais rien.
    account_tenant: Callable[[str | None], str | None] | None = None
    # Carte d'un testeur : notifiée à SON compte (son Telegram relié, `telegram/notify.py`), jamais à `notifier` qui écrit
    # aux opérateurs internes. `None` = pas de notification pour les testeurs (la carte reste visible dans l'app web).
    tester_notifier: Callable[[Approval], None] | None = None

    # ─────────────────────── primitives de stockage (sous-classe) ───────────────────────

    def _load_approval(self, approval_id: str) -> Approval | None:
        raise NotImplementedError

    def _store_approval(self, approval: Approval) -> None:
        raise NotImplementedError

    def _list_pending_approvals(self, user: str | None) -> list[Approval]:
        raise NotImplementedError

    def _append_event(self, approval_id: str, event_type: str, data: dict) -> None:
        raise NotImplementedError

    def _reserve_nonce(self, nonce: str, scope_id: str, exp: float) -> None:
        raise NotImplementedError

    def _consume_nonce(self, nonce: str, scope_id: str) -> bool:
        """Consomme un nonce à usage unique : `True` s'il existait, n'était pas expiré, n'avait pas déjà
        été consommé, et portait bien `scope_id` — en marquant atomiquement l'opération faite. `False`
        sinon (rejeu, expiration, ou nonce inconnu)."""
        raise NotImplementedError

    def _load_device(self, device_id: str) -> Device | None:
        raise NotImplementedError

    def _load_device_by_code(self, code: str) -> Device | None:
        raise NotImplementedError

    def _store_device(self, device: Device) -> None:
        raise NotImplementedError

    def _list_devices(self, user: str | None) -> list[Device]:
        raise NotImplementedError

    def _delete_device(self, device_id: str) -> None:
        raise NotImplementedError

    def _load_totp(self, user: str) -> tuple[str, int | None] | None:
        """`(secret_b32, dernier_step_consommé_ou_None)`."""
        raise NotImplementedError

    def _store_totp_secret(self, user: str, secret_b32: str) -> None:
        raise NotImplementedError

    def _set_totp_last_step(self, user: str, step: int) -> None:
        raise NotImplementedError

    def _store_session(self, session: Session) -> None:
        raise NotImplementedError

    def _load_session(self, token: str) -> Session | None:
        raise NotImplementedError

    # ─────────────────────── événements ───────────────────────

    def _emit(self, event_type: str, data: dict) -> None:
        if self.on_event is not None:
            try:
                self.on_event(event_type, data)
            except Exception:  # noqa: BLE001 — un abonné qui plante ne doit jamais casser le flux métier
                logger.exception("on_event a levé pour %s", event_type)

    # ─────────────────────── locataire (docs/TENANCY.md §3, §5) ───────────────────────

    @staticmethod
    def tenant_of(approval: Approval) -> str:
        """Locataire d'une carte : celui du fil où elle a été posée."""
        return tenant_of_thread(approval.thread_id)

    @staticmethod
    def visible(approval: Approval) -> bool:
        """Une carte n'existe que dans son locataire. Hors de tout `tenant_scope` (tests unitaires du backend,
        CLI d'exploitation), pas de filtre : la frontière est ailleurs."""
        tid = current_tenant_or_none()
        return tid is None or BaseApprovals.tenant_of(approval) == tid

    def user_tenant(self, user: str | None) -> str | None:
        """Locataire d'un utilisateur d'appareil ou d'un décideur : un `us_…` par son compte, un utilisateur
        autorisé par la config → interne, tout autre → `None`."""
        if user and user.startswith("us_"):
            return self.account_tenant(user) if self.account_tenant is not None else None
        return INTERNAL_TENANT if self.is_authorized(user) else None

    def decider_allowed(self, decided_by: str | None, card_tenant: str) -> bool:
        """Un décideur n'est accepté que dans le locataire de la carte : les opérateurs internes sur les cartes internes,
        le compte du testeur sur les SIENNES ; jamais Nick chez un testeur, jamais un testeur chez un autre."""
        return bool(decided_by) and self.user_tenant(decided_by) == card_tenant

    def device_visible(self, device: Device) -> bool:
        tid = current_tenant_or_none()
        return tid is None or self.user_tenant(device.user) == tid

    # ─────────────────────── port Approvals (core/ports.py) ───────────────────────

    def request(self, turn: Turn, call: ToolCall, verdict: Verdict, advice: str | None) -> Approval:
        now = self.clock()
        approval = Approval(
            id=str(uuid.uuid4()), thread_id=turn.thread_id, turn_index=turn.index, channel=turn.channel,
            tool_name=call.name, arguments=dict(call.arguments), args_sha256=args_sha256(call.arguments),
            strength=verdict.strength or "standard", rule=verdict.rule, reason=verdict.reason, advice=advice,
            status="pending", created_at=now, expires_at=now + self.default_ttl_s,
        )
        self._store_approval(approval)
        self._append_event(approval.id, "requested", {"rule": approval.rule, "strength": approval.strength})
        self._emit("approval.requested", {"approval": approval})
        if self.tenant_of(approval) != INTERNAL_TENANT:
            # Le notificateur Telegram écrit aux opérateurs internes : une carte de testeur ne leur part jamais.
            if self.tester_notifier is not None:
                try:
                    self.tester_notifier(approval)
                except Exception:  # noqa: BLE001
                    logger.exception("notification testeur en échec pour l'approbation %s", approval.id)
            return approval
        try:
            self.notifier.notify(approval)
        except Exception:  # noqa: BLE001 — une notification ratée ne bloque jamais la demande elle-même
            logger.exception("notifier.notify a échoué pour l'approbation %s", approval.id)
        return approval

    def _load_visible(self, approval_id: str) -> Approval | None:
        approval = self._load_approval(approval_id)
        return approval if approval is not None and self.visible(approval) else None

    def get(self, approval_id: str) -> Approval | None:
        approval = self._load_visible(approval_id)
        if approval is not None:
            self._expire_if_needed(approval)
        return approval

    def pending(self, user: str | None = None) -> list[Approval]:
        items = [a for a in self._list_pending_approvals(user) if self.visible(a)]
        for a in items:
            self._expire_if_needed(a)
        return [a for a in items if a.status == "pending"]

    def _expire_if_needed(self, approval: Approval) -> None:
        if approval.status == "pending" and self.clock() > approval.expires_at:
            approval.status = "expired"
            self._store_approval(approval)
            self._append_event(approval.id, "expired", {})

    def mark_executed(self, approval_id: str, result_summary: str) -> Approval:
        """Posé par `nokker.approvals.executor.ApprovalExecutor` APRÈS un `decide(..., 'approve')`
        réussi et l'exécution réelle de l'outil — pas dans le Protocol `Approvals` (core/ports.py, qui ne
        porte que ce dont `Kernel` a besoin), seulement sur le backend concret."""
        approval = self._load_approval(approval_id)
        if approval is None:
            raise ApprovalNotFound(approval_id)
        approval.status, approval.result_summary, approval.error = "executed", result_summary, None
        self._store_approval(approval)
        self._append_event(approval.id, "executed", {})
        return approval

    def mark_failed(self, approval_id: str, error: str) -> Approval:
        approval = self._load_approval(approval_id)
        if approval is None:
            raise ApprovalNotFound(approval_id)
        approval.status, approval.error = "failed", error
        self._store_approval(approval)
        self._append_event(approval.id, "failed", {"error": error})
        return approval

    def challenge(self, approval_id: str) -> Challenge:
        approval = self._load_visible(approval_id)
        if approval is None:
            raise ApprovalNotFound(approval_id)
        self._expire_if_needed(approval)
        if approval.status == "expired":
            raise ApprovalExpired(approval_id)
        if approval.status != "pending":
            raise ApprovalNotPending(approval.status)
        now = self.clock()
        nonce = new_nonce()
        exp = now + self.challenge_ttl_s
        self._reserve_nonce(nonce, approval_id, exp)
        return Challenge(approval_id=approval_id, args_sha256=approval.args_sha256, nonce=nonce, exp=exp)

    def decide(self, approval_id: str, decision: str, proof: Proof) -> Approval:
        if decision not in ("approve", "reject"):
            raise ProofInvalid(f"décision inconnue : {decision!r}")
        approval = self._load_visible(approval_id)
        if approval is None:
            raise ApprovalNotFound(approval_id)
        self._expire_if_needed(approval)
        if approval.status == "expired":
            raise ApprovalExpired(approval_id)
        if approval.status != "pending":
            raise ApprovalNotPending(approval.status)

        if not proof.challenge:
            raise ProofInvalid("preuve sans défi (`challenge` manquant)")
        try:
            challenge_obj = json.loads(proof.challenge)
        except json.JSONDecodeError as e:
            raise ProofInvalid(f"défi illisible : {e}") from e
        if challenge_obj.get("approval_id") != approval_id:
            raise ProofInvalid("le défi porte sur une autre approbation")
        if challenge_obj.get("aud") != "nokker-approval":
            raise ProofInvalid("audience du défi invalide")
        # Anti-falsification : l'empreinte dans le défi ET l'empreinte recalculée MAINTENANT sur les
        # arguments stockés doivent toutes les deux valoir l'empreinte posée à la demande — une
        # divergence de l'une ou l'autre dit que quelque chose a changé entre la demande et la décision.
        fresh_hash = args_sha256(approval.arguments)
        if fresh_hash != approval.args_sha256 or challenge_obj.get("args_sha256") != approval.args_sha256:
            raise ProofInvalid("empreinte des arguments modifiée depuis la demande (args_sha256)")
        nonce = challenge_obj.get("nonce")
        if not nonce or not isinstance(nonce, str) or not self._consume_nonce(nonce, approval_id):
            raise ProofInvalid("nonce de défi invalide, expiré, ou déjà utilisé")

        decided_by, decided_via = self._verify_proof(approval, decision, proof)
        if not self.decider_allowed(decided_by, self.tenant_of(approval)):
            raise ProofInvalid(f"utilisateur non autorisé à décider : {decided_by!r}")

        now = self.clock()
        approval.status = "approved" if decision == "approve" else "rejected"
        approval.decided_by = decided_by
        approval.decided_via = decided_via
        approval.decided_at = now
        self._store_approval(approval)
        self._append_event(approval.id, approval.status, {"decided_by": decided_by, "decided_via": decided_via})
        self._emit("approval.decided",
                   {"approval_id": approval.id, "decision": approval.status, "by": decided_by, "via": decided_via})
        return approval

    def decide_local(self, approval_id: str, decision: str, decided_by: str) -> Approval:
        """Décision par le propriétaire de la machine (Nokker Lokkal, jeton local, docs/LOKKAL.md §7.4.3) : mêmes
        contrôles d'état et d'empreinte des arguments que `decide`, sans signature d'appareil. Appelée seulement par
        la route locale, jamais par une route qu'on peut joindre depuis la passerelle."""
        if decision not in ("approve", "reject"):
            raise ProofInvalid(f"décision inconnue : {decision!r}")
        approval = self._load_visible(approval_id)
        if approval is None:
            raise ApprovalNotFound(approval_id)
        self._expire_if_needed(approval)
        if approval.status == "expired":
            raise ApprovalExpired(approval_id)
        if approval.status != "pending":
            raise ApprovalNotPending(approval.status)
        if args_sha256(approval.arguments) != approval.args_sha256:
            raise ProofInvalid("empreinte des arguments modifiée depuis la demande (args_sha256)")
        approval.status = "approved" if decision == "approve" else "rejected"
        approval.decided_by, approval.decided_via, approval.decided_at = decided_by, "local-token", self.clock()
        self._store_approval(approval)
        self._append_event(approval.id, approval.status, {"decided_by": decided_by, "decided_via": "local-token"})
        self._emit("approval.decided",
                   {"approval_id": approval.id, "decision": approval.status, "by": decided_by, "via": "local-token"})
        return approval

    def _verify_proof(self, approval: Approval, decision: str, proof: Proof) -> tuple[str, str]:
        """Renvoie `(decided_by, decided_via)` ou lève `ProofInvalid`. `proof.challenge` a déjà été
        validé (nonce/audience/args_sha256) par `decide()` — ici on ne vérifie que l'IDENTITÉ."""
        if proof.totp:
            if approval.strength == "strong":
                raise ProofInvalid("TOTP non accepté pour une approbation « strong » — clé d'appareil requise")
            user = (proof.totp_user or "").strip().lower()
            if not user:
                raise ProofInvalid("`totp_user` manquant")
            secret_info = self._load_totp(user)
            if secret_info is None:
                raise ProofInvalid(f"aucun secret TOTP enrôlé pour {user!r}")
            secret_b32, last_step = secret_info
            step = totp_matching_step(secret_b32, proof.totp)
            if step is None:
                raise ProofInvalid("code TOTP invalide")
            if last_step is not None and step <= last_step:
                raise ProofInvalid("code TOTP déjà utilisé")
            self._set_totp_last_step(user, step)
            return user, "totp"

        if proof.webauthn_signature and proof.device_id:
            device = self._load_device(proof.device_id)
            if device is None:
                raise ProofInvalid("appareil inconnu")
            if device.status != "approved":
                raise ProofInvalid(f"appareil non approuvé (statut={device.status!r})")
            _require_deciding_device(device)
            if not self.webauthn_origin:
                raise ProofInvalid("vérification WebAuthn non configurée côté serveur (origine attendue absente)")
            expected_challenge = hashlib.sha256(f"{proof.challenge}.{decision}".encode("utf-8")).digest()
            try:
                verify_webauthn_assertion(
                    device.public_key_spki, proof.webauthn_client_data_json, proof.webauthn_authenticator_data,
                    proof.webauthn_signature, expected_challenge=expected_challenge,
                    expected_origin=self.webauthn_origin, require_uv=approval.strength == "strong")
            except WebAuthnError as e:
                raise ProofInvalid(str(e)) from e
            return device.user, "webauthn"

        if proof.signature and proof.device_id:
            device = self._load_device(proof.device_id)
            if device is None:
                raise ProofInvalid("appareil inconnu")
            if device.status != "approved":
                raise ProofInvalid(f"appareil non approuvé (statut={device.status!r})")
            _require_deciding_device(device)
            message = f"{proof.challenge}.{decision}".encode("utf-8")
            try:
                verify_p1363_signature(device.public_key_spki, message, proof.signature)
            except SignatureError as e:
                raise ProofInvalid(str(e)) from e
            via = "signature"
            if proof.telegram_init_data:
                if not self.telegram_bot_token:
                    raise ProofInvalid("validation Telegram non configurée côté serveur")
                try:
                    info = verify_telegram_init_data(proof.telegram_init_data, self.telegram_bot_token,
                                                     self.telegram_max_age_s)
                except TelegramInitDataError as e:
                    raise ProofInvalid(str(e)) from e
                tg_user_id = str((info.get("user") or {}).get("id", ""))
                if not device.telegram_user_id or tg_user_id != device.telegram_user_id:
                    raise ProofInvalid("initData Telegram ne correspond pas à l'appareil")
                via = "signature+telegram"
            if approval.strength == "strong" and via != "signature+telegram":
                # Une clé logicielle (CLI : trousseau ou fichier 0600) n'est PAS libérée par biométrie : elle ne
                # suffit qu'au niveau standard. strong = Mini App (clé libérée par BiometricManager, initData
                # vérifié), passkey avec UV, ou plus tard app native attestée (docs/APPROVALS.md §1).
                raise ProofInvalid("niveau « strong » : signature d'une clé logicielle refusée — approuver depuis "
                                   "la Mini App Telegram, une passkey ou l'app native")
            return device.user, via

        raise ProofInvalid("aucune preuve exploitable (signature d'appareil, WebAuthn, ou TOTP)")

    # ─────────────────────── appareils (docs/CLIENT-API.md) ───────────────────────

    def enroll_device(self, user: str, name: str, public_key_spki: str,
                      telegram_user_id: str | None = None) -> Device:
        device = Device(id=str(uuid.uuid4()), code=uuid.uuid4().hex[:6], user=user.strip().lower(), name=name,
                        public_key_spki=public_key_spki, status="pending", created_at=self.clock(),
                        telegram_user_id=telegram_user_id)
        self._store_device(device)
        return device

    def get_device(self, device_id: str) -> Device | None:
        device = self._load_device(device_id)
        return device if device is not None and self.device_visible(device) else None

    def list_devices(self, user: str | None = None) -> list[Device]:
        return [d for d in self._list_devices(user) if self.device_visible(d)]

    def delete_device(self, device_id: str) -> None:
        self._delete_device(device_id)

    def confirm_device_totp(self, device_id: str, totp: str, totp_user: str) -> Device:
        device = self._load_device(device_id)
        if device is None:
            raise DeviceNotFound(device_id)
        user = totp_user.strip().lower()
        secret_info = self._load_totp(user)
        if secret_info is None:
            raise ProofInvalid(f"aucun secret TOTP enrôlé pour {user!r}")
        secret_b32, last_step = secret_info
        step = totp_matching_step(secret_b32, totp)
        if step is None:
            raise ProofInvalid("code TOTP invalide")
        if last_step is not None and step <= last_step:
            raise ProofInvalid("code TOTP déjà utilisé")
        self._set_totp_last_step(user, step)
        if not self.is_authorized(user):
            raise ProofInvalid(f"utilisateur non autorisé à confirmer un appareil : {user!r}")
        if device.user != user:
            # Enrôlement web (docs/APPROVALS.md §3.1bis) : l'appareil DÉCLARE son utilisateur ; seul le TOTP de CET
            # utilisateur le confirme (sinon le TOTP d'un opérateur confirmerait l'appareil d'un autre).
            raise ProofInvalid("le TOTP doit être celui du propriétaire de l'appareil")
        device.status, device.approved_at, device.approved_via = "approved", self.clock(), "totp"
        self._store_device(device)
        return device

    def confirm_device_cli(self, code: str) -> Device:
        """`python -m nokker devices approve <code>` (nokker/cli.py) — confirmation locale sur l'hôte,
        pas d'identité réseau à vérifier (celui qui a un shell root EST déjà un opérateur de confiance)."""
        device = self._load_device_by_code(code)
        if device is None:
            raise DeviceNotFound(f"aucun appareil en attente avec le code {code!r}")
        device.status, device.approved_at, device.approved_via = "approved", self.clock(), "cli"
        self._store_device(device)
        return device

    def confirm_device_by_invitation(self, device_id: str, invitation_id: str | int) -> Device:
        """Confirme l'appareil enrôlé dans le parcours d'invitation (nokker/onboarding). L'APPELANT a déjà vérifié et
        consommé le jeton (une invitation = un appareil) ; ici on ne confirme qu'un appareil encore en attente, et la
        trace de l'invitation reste dans `approved_via` (« invitation:<id> »)."""
        device = self._load_device(device_id)
        if device is None:
            raise DeviceNotFound(device_id)
        if device.status != "pending":
            raise ProofInvalid(f"appareil {device_id!r} déjà {device.status} : une invitation ne confirme qu'un appareil en attente")
        device.status, device.approved_at, device.approved_via = "approved", self.clock(), f"invitation:{invitation_id}"
        self._store_device(device)
        return device

    def confirm_device_by_email_code(self, device_id: str) -> Device:
        """Confirme un appareil après un code e-mail valide (nokker/onboarding/emailcode.py, qui a vérifié et brûlé le
        code). L'appareil sert à se connecter, jamais à décider : `approved_via` commence par « email-code »."""
        device = self._load_device(device_id)
        if device is None:
            raise DeviceNotFound(device_id)
        if device.status != "pending":
            raise ProofInvalid(f"appareil {device_id!r} déjà {device.status}")
        device.status, device.approved_at, device.approved_via = "approved", self.clock(), "email-code"
        self._store_device(device)
        return device

    # ─────────────────────── TOTP (provisionnement) ───────────────────────

    def enroll_totp_secret(self, user: str, secret_b32: str) -> None:
        """Pose le secret TOTP d'un utilisateur. Hors API (docs/CLIENT-API.md n'expose aucune route pour
        ça) — provisionné par un outil d'exploitation direct sur le backend (Vaultwarden distribue le
        secret au vrai utilisateur séparément). Jamais relu par `decide()`/l'API : seul le CODE est
        vérifié, jamais le secret lui-même renvoyé."""
        self._store_totp_secret(user.strip().lower(), secret_b32)

    # ─────────────────────── sessions (docs/CLIENT-API.md §Authentification) ───────────────────────

    def _require_live_account(self, device: Device) -> None:
        """Appareil d'un testeur (`us_…`, confirmé par invitation) : son compte doit exister et être actif dans
        `Tenancy`. Un compte inconnu ou suspendu n'ouvre plus de session. Les appareils internes ne changent pas."""
        if device.user.startswith("us_") and self.user_tenant(device.user) is None:
            raise ProofInvalid(f"compte inconnu ou inactif : {device.user!r}")

    def login_challenge(self, device_id: str) -> Challenge:
        device = self._load_device(device_id)
        if device is None:
            raise DeviceNotFound(device_id)
        if device.status != "approved":
            raise DeviceNotApproved(f"appareil non approuvé (statut={device.status!r})")
        self._require_live_account(device)
        now = self.clock()
        nonce = new_nonce()
        exp = now + self.challenge_ttl_s
        scope = f"login:{device_id}"
        self._reserve_nonce(nonce, scope, exp)
        return Challenge(approval_id=scope, args_sha256="", nonce=nonce, exp=exp, aud="nokker-login")

    def create_session(self, device_id: str, challenge: str, signature: str | None = None, *,
                       webauthn: dict[str, str] | None = None) -> Session:
        """`signature` : chemin brut (CLI, Mini App — signe `challenge + ".login"`, IEEE P1363).
        `webauthn` : chemin web/passkey (ADR `nokker-apps/docs/adr/0001-*.md`) —
        `{"client_data_json", "authenticator_data", "signature"}` (signature DER) ; PAS de suffixe
        `.decision` pour une connexion, le challenge WebAuthn est `sha256(challenge)` tel quel."""
        device = self._load_device(device_id)
        if device is None:
            raise DeviceNotFound(device_id)
        if device.status != "approved":
            raise DeviceNotApproved(f"appareil non approuvé (statut={device.status!r})")
        self._require_live_account(device)
        try:
            challenge_obj = json.loads(challenge)
        except json.JSONDecodeError as e:
            raise ProofInvalid(f"défi illisible : {e}") from e
        scope = f"login:{device_id}"
        if challenge_obj.get("approval_id") != scope or challenge_obj.get("aud") != "nokker-login":
            raise ProofInvalid("défi de connexion invalide pour cet appareil")
        nonce = challenge_obj.get("nonce")
        if not nonce or not self._consume_nonce(nonce, scope):
            raise ProofInvalid("nonce de connexion invalide, expiré, ou déjà utilisé")

        if webauthn is not None:
            if not self.webauthn_origin:
                raise ProofInvalid("vérification WebAuthn non configurée côté serveur (origine attendue absente)")
            expected_challenge = hashlib.sha256(challenge.encode("utf-8")).digest()
            try:
                verify_webauthn_assertion(
                    device.public_key_spki, webauthn.get("client_data_json", ""),
                    webauthn.get("authenticator_data", ""), webauthn.get("signature", ""),
                    expected_challenge=expected_challenge, expected_origin=self.webauthn_origin, require_uv=False)
            except WebAuthnError as e:
                raise ProofInvalid(str(e)) from e
        elif signature:
            message = f"{challenge}.login".encode("utf-8")
            try:
                verify_p1363_signature(device.public_key_spki, message, signature)
            except SignatureError as e:
                raise ProofInvalid(str(e)) from e
        else:
            raise ProofInvalid("aucune preuve de connexion (ni signature, ni webauthn)")

        now = self.clock()
        session = Session(token=uuid.uuid4().hex + uuid.uuid4().hex, user=device.user, device_id=device.id,
                          created_at=now, expires_at=now + self.session_ttl_s)
        self._store_session(session)
        return session

    def verify_session(self, token: str) -> Session | None:
        session = self._load_session(token)
        if session is None or self.clock() > session.expires_at:
            return None
        return session


__all__ = ["BaseApprovals", "CHALLENGE_TTL_S", "DEFAULT_TTL_S", "SESSION_TTL_S"]
