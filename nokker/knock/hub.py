"""Heurtoir — ce que le noyau fait d'un déclencheur physique (docs/KNOCK.md §5).

Entrée : un `PhysicalTrigger` venu de home-bridge (toc toc toc, clap, touche, étiquette NFC) : la pièce et l'heure,
jamais de son. Deux sorties possibles :

1. **frapper pour approuver** — si une carte STANDARD de la liste fermée est ARMÉE pour cette pièce (`arm`, par
   l'annonce de la carte dans la pièce ou le bouton « approuver au heurtoir » de l'appli), et qu'un membre autorisé
   du locataire de la carte est présent (Home Assistant ou Wi-Fi, vu il y a moins de `presence_max_age_s`) : la carte
   est approuvée (`decided_via = knock+presence`) puis exécutée par l'exécuteur habituel. Sinon : refus dit dans la
   pièce, la carte reste en attente (passkey, Face ID ou Mini App).
2. **frapper pour appeler** — sinon : une session vocale s'ouvre dans la pièce (fil de la pièce, profil configuré),
   le modèle reçoit le seul bloc `[Session vocale]` (`policy.session_block`) et sa réponse part à la sortie ambiante.

Un déclencheur est accepté une fois (`event_id`), pas plus d'un par pièce toutes les `cooldown_s`, et jamais s'il est
trop vieux (`max_skew_s`). Drapeau `knock.enabled` éteint : `handle` rend `disabled` et ne touche à rien.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nokker.knock.policy import DECIDED_VIA, KnockConfig, Room, clean_label, knock_refusal, session_block
from nokker.qa.untrusted import inline
from nokker.knock.types import (
    INTENTS,
    AmbientOutput,
    NoPresence,
    NullAmbientOutput,
    Outcome,
    PhysicalTrigger,
    Presence,
    SpokenLine,
)
from nokker.tenancy.context import tenant_of_thread, tenant_scope

logger = logging.getLogger("nokker.knock")


@dataclass
class _Armed:
    approval_id: str
    room: str
    until: float
    tenant: str


_TARGET_KEYS = ("path", "file_path", "file", "target", "name")


def spoken_label(approval: Any) -> str:
    """Libellé LU À VOIX HAUTE dans la pièce à l'armement : construit par le noyau (outil + nom de la cible, sans
    dossier), jamais le motif de la carte, qui peut contenir un chemin, un nom ou une valeur recopiée, et qu'un
    interlocuteur non identifié entendrait. Le motif complet reste sur la carte à l'écran. 80 caractères au plus."""
    tool = inline(getattr(approval, "tool_name", "") or "action", 40)
    args = getattr(approval, "arguments", None) or {}
    target = next((str(args[k]) for k in _TARGET_KEYS if isinstance(args.get(k), str) and args.get(k)), "")
    target = inline(target.rstrip("/").rsplit("/", 1)[-1], 36) if target else ""
    return clean_label(f"{tool} {target}".strip(), 80)


class KernelVoice:
    """Ouvre la session vocale : un tour sur le fil de la pièce, dans le locataire de ce fil, et la réponse à la
    sortie ambiante. Le son de la suite de la session (micro de la pièce → transcription) passe par home-bridge
    (`audio.session.open`, docs/KNOCK.md §8), hors de ce module."""

    def __init__(self, kernel: Any, ambient: AmbientOutput):
        self.kernel = kernel
        self.ambient = ambient

    def __call__(self, room: Room, kind: str) -> str | None:
        from nokker.core.turn import TurnInput

        with tenant_scope(tenant_of_thread(room.thread_id)):
            turn = self.kernel.run_turn(TurnInput(thread_id=room.thread_id, user_text=session_block(kind, room.label),
                                                  channel="voice", sender=room.sender))
        reply = (getattr(turn, "reply", "") or "").strip()
        if reply:
            self.ambient.say(SpokenLine(room=room.id, text=reply, private=True, recipient=room.sender))
        return reply or None


class KnockHub:
    def __init__(self, config: KnockConfig, *, approvals: Any = None,
                 execute: Callable[[Any], Any] | None = None, presence: Presence | None = None,
                 ambient: AmbientOutput | None = None, voice: Callable[[Room, str], Any] | None = None,
                 clock: Callable[[], float] = time.time):
        self.config = config
        self.approvals = approvals
        self.execute = execute
        self.presence = presence or NoPresence()
        self.ambient = ambient or NullAmbientOutput()
        self.voice = voice
        self.clock = clock
        self._lock = threading.Lock()
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._last_room: dict[str, float] = {}
        self._armed: dict[str, _Armed] = {}

    # ------------------------------------------------------------------------------------ armement des cartes ----
    def arm(self, approval_id: str, room: str) -> str | None:
        """Arme une carte pour une pièce pendant `approve.window_s`. Rend None si c'est fait, sinon la raison."""
        a = self.config.approve
        if not (self.config.enabled and a.enabled):
            return "approbation au heurtoir éteinte"
        r = self.config.rooms.get(room)
        if r is None:
            return "pièce inconnue"
        approval = self.approvals.get(approval_id) if self.approvals is not None else None
        if approval is None or approval.status != "pending":
            return "carte introuvable ou déjà décidée"
        tenant = tenant_of_thread(approval.thread_id)
        if tenant != tenant_of_thread(r.thread_id):
            return "carte d'un autre locataire que la pièce"
        if approval.thread_id != r.thread_id:
            return "carte née hors de la session vocale de cette pièce"
        why = knock_refusal(approval, a.rules)
        if why:
            return why
        # La carte est lue à voix haute dans la pièce AU MOMENT où elle est armée : la fenêtre part de cette lecture.
        self.ambient.say(SpokenLine(room=room, text=f"Carte à approuver : {spoken_label(approval)}. "
                                                    "Pour approuver, frappe trois fois."))
        with self._lock:
            self._armed[approval_id] = _Armed(approval_id, room, self.clock() + a.window_s, tenant)
        return None

    def disarm(self, approval_id: str) -> None:
        with self._lock:
            self._armed.pop(approval_id, None)

    def armed_in(self, room: str) -> list[str]:
        now = self.clock()
        with self._lock:
            for k in [k for k, v in self._armed.items() if v.until < now]:
                del self._armed[k]
            return [v.approval_id for v in self._armed.values() if v.room == room]

    # ---------------------------------------------------------------------------------------- déclencheurs ----
    def handle(self, trig: PhysicalTrigger) -> Outcome:
        cfg = self.config
        if not cfg.enabled:
            return Outcome("disabled")
        room = cfg.rooms.get(trig.room)
        if room is None:
            return Outcome("ignored", "pièce inconnue", trig.room)
        if trig.kind not in cfg.kinds:
            return Outcome("ignored", f"déclencheur {trig.kind!r} non permis", room.id)
        if trig.intent is not None and trig.intent not in INTENTS:
            return Outcome("ignored", "intention inconnue", room.id)
        now = self.clock()
        if abs(now - trig.at) > cfg.max_skew_s:
            return Outcome("ignored", "événement trop vieux ou daté dans le futur", room.id)
        with self._lock:
            if trig.event_id in self._seen:
                return Outcome("ignored", "déjà traité", room.id)
            self._seen[trig.event_id] = now
            while len(self._seen) > 512:
                self._seen.popitem(last=False)
            if now - self._last_room.get(room.id, -1e18) < cfg.cooldown_s:
                return Outcome("ignored", "pièce en pause (anti-rebond)", room.id)
            self._last_room[room.id] = now
        armed = self.armed_in(room.id)
        if trig.intent == "approve" or (trig.intent is None and armed):
            return self._approve(trig, room, armed)
        return self._call(trig, room)

    def _call(self, trig: PhysicalTrigger, room: Room) -> Outcome:
        self.ambient.say(SpokenLine(room=room.id, chime="listening"))
        if self.voice is None:
            return Outcome("voice_session", "pas de session vocale branchée", room.id, thread_id=room.thread_id)
        try:
            self.voice(room, trig.kind)
        except Exception as e:
            logger.exception("session vocale de %s impossible", room.id)
            return Outcome("voice_session", f"échec : {type(e).__name__}", room.id, thread_id=room.thread_id)
        return Outcome("voice_session", "", room.id, thread_id=room.thread_id)

    def _refuse(self, room: Room, why: str, approval_id: str | None = None) -> Outcome:
        self.ambient.say(SpokenLine(room=room.id, chime="refused",
                                    text="Je ne peux pas approuver au heurtoir : valide sur ton téléphone."))
        logger.info("heurtoir : refus dans %s (%s)", room.id, why)
        return Outcome("refused", why, room.id, approval_id=approval_id)

    def _approve(self, trig: PhysicalTrigger, room: Room, armed: list[str]) -> Outcome:
        a = self.config.approve
        if not a.enabled or self.approvals is None:
            return self._refuse(room, "approbation au heurtoir éteinte")
        if len(armed) != 1:
            return self._refuse(room, "aucune carte armée dans cette pièce" if not armed
                                else "plusieurs cartes armées : ambigu")
        approval_id = armed[0]
        with self._lock:
            entry = self._armed.get(approval_id)
        approval = self.approvals.get(approval_id)
        if entry is None or approval is None:
            return self._refuse(room, "carte introuvable", approval_id)
        if approval.thread_id != room.thread_id:
            return self._refuse(room, "carte née hors de la session vocale de cette pièce", approval_id)
        card_tenant = tenant_of_thread(approval.thread_id)
        if card_tenant != entry.tenant or card_tenant != tenant_of_thread(room.thread_id):
            return self._refuse(room, "locataire de la carte différent de celui de la pièce", approval_id)
        why = knock_refusal(approval, a.rules)
        if why:
            return self._refuse(room, why, approval_id)
        member, facts = self._present_member(approval, card_tenant, room)
        if member is None:
            return self._refuse(room, facts or "aucun membre autorisé présent", approval_id)
        evidence = {"room": room.id, "trigger": trig.kind, "source": trig.source, "event_id": trig.event_id,
                    "presence": facts}
        try:
            with tenant_scope(card_tenant):
                decided = self.approvals.decide_physical(approval_id, decided_by=member, evidence=evidence)
        except Exception as e:  # noqa: BLE001 — ProofInvalid, ApprovalExpired, ApprovalNotPending…
            return self._refuse(room, f"{type(e).__name__}: {e}", approval_id)
        self.disarm(approval_id)
        self.ambient.say(SpokenLine(room=room.id, chime="approved", text="Approuvé."))
        if self.execute is not None:
            try:
                self.execute(decided)
            except Exception:
                logger.exception("exécution de la carte %s après le heurtoir", approval_id)
        return Outcome("approved", "", room.id, approval_id=approval_id, decided_by=member,
                       details={"via": DECIDED_VIA})

    def _present_member(self, approval: Any, tenant: str, room: Room) -> tuple[str | None, Any]:
        """Le membre présent qui décidera : le propriétaire de la carte s'il est présent, sinon un autre membre autorisé
        du même locataire. Présence injoignable = personne (échec fermé)."""
        a = self.config.approve
        now = self.clock()
        try:
            with tenant_scope(tenant):
                facts = list(self.presence.present(tenant))
        except Exception as e:  # noqa: BLE001
            logger.warning("présence injoignable : %s", e)
            return None, "présence injoignable"
        fresh = [f for f in facts if f.source in a.sources and 0 <= now - f.seen_at <= a.presence_max_age_s
                 and (not a.same_room or f.room in (None, room.id))]
        allowed = [f for f in fresh if self.approvals.decider_allowed(f.member, tenant, approval)]
        if not allowed:
            return None, "aucun membre autorisé présent"
        owner = self.approvals._owner(approval.thread_id) if hasattr(self.approvals, "_owner") else None
        pick = next((f for f in allowed if owner and f.member == owner), allowed[0])
        return pick.member, [{"member": f.member, "source": f.source, "age_s": round(now - f.seen_at, 1)}
                             for f in allowed]
