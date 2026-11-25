"""Outils du nœud téléphone (P7b, docs/LOKKAL-PHONE.md §3.4-3.5, §4.2, §4.3.1).

- `zone_state {zone?}` : la personne est-elle dans une de SES zones (nokker/zones, migration 075) ? Réponse depuis le
  magasin des zones (dernière arrivée ou dernier départ signalés par le téléphone), sans aller-retour au téléphone ;
  heure suisse (nokker/jobs/localtime.py). Le nom est résolu dans les zones DU MEMBRE seulement (casefold ; « maison »
  et « home » désignent sa zone `home`).
- `ocr_image {back?}` : le texte d'une image jointe au fil (X7), lu SUR le téléphone de la personne (`ocr.image`).
  Placement déterministe (nokker/cloud/placement.py, capacité `ocr`) : le modèle ne choisit jamais la machine.
- `stt_audio` : plomberie seulement (référence `audio:<ref>`, `PhoneService.audio_url`). L'outil reste éteint, sans
  description exposée au modèle, tant qu'aucune pièce jointe audio n'existe.

Attente : 20 s. Au-delà, le résultat d'outil est `phone_pending` et le message assistant porte `phone_tasks` ; quand la
tâche se clôt (fait, échec, expiration), le noyau lance un TOUR DE SUITE (`TurnInput.control = PHONE_CONTINUE`,
écrit par le noyau, jamais accepté d'un client) dont le bloc joint porte l'en-tête du noyau HORS clôture et le
résultat du téléphone DANS une clôture `<untrusted-data>`, caviardé. Le suivi vit en mémoire (un réplica, comme les
offres de P11) : après un redémarrage, la tâche expire sans tour de suite et le journal du téléphone le montre.

Textes vus par le modèle : `TEXTS`, `OP_LABELS`, `CAP_LABELS`, `REASONS`, descriptions des outils ; empreinte figée par
`tests/unit/test_phone_tools.py` (`texts_sha256`).
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from nokker.core import model_texts as mt
from nokker.core.ports import Effect, ToolContext, ToolResult, ToolSpec

logger = logging.getLogger("nokker.cloud.phone_tools")

WAIT_S = 20.0
OCR_DEADLINE_S = 600.0
BACK_MAX = 8
NAME_MAX = 40

TEXTS = {
    "phone_pending": ("phone_pending : « {op} » attend le téléphone « {name} » ; le résultat arrivera dans un tour de "
                      "suite. N'appelle pas l'outil une deuxième fois ; dis à la personne que la lecture est en cours."),
    "phone_unavailable": ("phone_unavailable : aucun téléphone de la personne avec « {cap} » n'est en ligne ; rien "
                          "n'a été lu."),
    "continue_head": "[Téléphone] Résultat de « {op} » envoyé par le téléphone « {name} » :",
    "continue_failed": "[Téléphone] « {op} » n'a pas abouti sur le téléphone « {name} » ({reason}) ; rien n'a été lu.",
    "continue_expired": "[Téléphone] « {op} » a expiré sans réponse du téléphone « {name} » ; rien n'a été lu.",
    "ocr_head": ("Texte lu par le téléphone « {name} » dans l'image demandée (lu sur l'appareil, données sensibles "
                 "masquées avant l'envoi) :"),
    "ocr_empty": "ocr_empty : le téléphone « {name} » n'a trouvé aucun texte dans l'image demandée.",
    "ocr_no_image": "ocr_no_image : aucune image au rang {back} dans cette conversation ; rien n'a été lu.",
    "zone_inside": "La personne est dans la zone « {zone} » depuis le {date} à {time} (heure suisse).",
    "zone_outside": "La personne n'est pas dans la zone « {zone} » (sortie le {date} à {time}, heure suisse).",
    "zone_never": "Zone « {zone} » : jamais vue (aucune arrivée ni aucun départ signalés par le téléphone).",
    "zone_unknown": "zone_unknown : aucune zone « {zone} » chez la personne ; ses zones : {names}.",
    "zone_none": "zone_none : la personne n'a défini aucune zone dans l'app Nokker.",
}
OP_LABELS = {"zone.state": "état de zone", "ocr.image": "texte d'une image", "stt.audio": "transcription",
             "calendar.list": "agenda"}
CAP_LABELS = {"location": "position", "ocr": "lecture de texte", "stt": "transcription",
              "device_calendar": "agenda du téléphone"}
# Codes d'échec que rend le natif pour ces opérations (NokkerNodePlugin) ; les autres : la table de P11.
REASONS = {
    "denied": "accès refusé par le système du téléphone",
    "bad_image": "image illisible sur le téléphone",
    "transcription_failed": "transcription impossible sur le téléphone",
    "redact_rules_unavailable": "règles de masquage absentes sur le téléphone",
    "gone": "lien du fichier expiré",
    "forbidden": "lien du fichier refusé",
    "fetch_failed": "fichier non téléchargé par le téléphone",
    "result_too_large": "résultat trop long",
    "bad_args": "demande mal formée",
    "unknown_op": "opération inconnue du téléphone",
    "bad_result": "réponse illisible du téléphone",
    # Nœud maison (P10a, nokker/cloud/phone_home.py) : codes du natif, puis ceux du noyau
    "not_home_wifi": "le nœud maison n'est pas sur le Wi-Fi de la maison",
    "ha_not_configured": "Home Assistant n'est pas configuré dans l'app Nokker du téléphone",
    "ha_unreachable": "Home Assistant injoignable depuis le nœud maison",
    "ha_auth_failed": "jeton Home Assistant refusé, à saisir à nouveau dans l'app Nokker du téléphone",
    "entity_not_allowed": "entité hors de la liste choisie dans l'app",
    "service_not_allowed": "service hors de la liste choisie dans l'app",
    "ha_error": "erreur de Home Assistant",
    "no_home_node": "aucun nœud maison en ligne sur le Wi-Fi de la maison",
    "decision_required": "action refusée : la politique de la maison ne l'autorise pas sans confirmation",
    "attestation_required": "le nœud maison n'est pas une app Nokker vérifiée : lectures seulement",
    "op_not_allowed": "opération jamais permise depuis le téléphone",
    "home_timeout": ("pas de réponse du nœud maison à temps ; une action a pu être faite quand même, relis l'état "
                     "avant de la refaire"),
}
SPECS = {
    "zone_state": ToolSpec(
        name="zone_state",
        description=("Dit si la personne est dans une de ses zones de l'app Nokker (Maison, Travail…), d'après la "
                     "dernière arrivée ou le dernier départ signalés par son téléphone. Ne donne jamais de position "
                     "précise. Sans zone : toutes ses zones."),
        parameters={"type": "object", "additionalProperties": False, "properties": {
            "zone": {"type": "string", "description": ("Nom de la zone, tel que la personne l'a nommée (« maison » "
                                                       "ou « home » désignent sa zone Maison).")}}},
        effect=Effect.READ, fresh=True),
    "ocr_image": ToolSpec(
        name="ocr_image",
        description=("Lit le texte d'une image jointe à cette conversation, sur le téléphone de la personne (lu sur "
                     "l'appareil, données sensibles masquées avant l'envoi). Sans réponse en 20 s, le résultat arrive "
                     "dans un tour de suite. N'appelle pas l'outil pour une image dont le texte est déjà dans la "
                     "conversation."),
        parameters={"type": "object", "additionalProperties": False, "properties": {
            "back": {"type": "integer", "minimum": 1, "maximum": BACK_MAX,
                     "description": "Rang de l'image depuis la plus récente de la conversation (1 = la dernière)."}}},
        effect=Effect.READ, fresh=True),
}


def texts_sha256() -> str:
    """Empreinte de tout ce que le modèle lit de ces outils : textes, libellés, raisons, descriptions et schémas."""
    from nokker.cloud.egress import REASONS as EGRESS_REASONS, UNKNOWN_REASON

    specs = {k: {"description": s.description, "parameters": s.parameters} for k, s in SPECS.items()}
    blob = json.dumps({"TEXTS": TEXTS, "OP_LABELS": OP_LABELS, "CAP_LABELS": CAP_LABELS, "REASONS": REASONS,
                       "EGRESS_REASONS": EGRESS_REASONS, "UNKNOWN_REASON": UNKNOWN_REASON, "SPECS": specs},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def reason_text(code: Any) -> str:
    """La phrase d'un code d'échec : cette table, puis celle de P11 ; sinon « raison non précisée » (journalisé)."""
    if code in REASONS:
        return REASONS[code]
    from nokker.cloud.egress import reason_text as egress_reason

    return egress_reason(code)


def _inline(v: Any, n: int = NAME_MAX) -> str:
    from nokker.qa.untrusted import inline

    return " ".join(inline(v).split())[:n]


def _when(at: datetime) -> tuple[str, str]:
    from nokker.jobs.localtime import local

    t = local(at.timestamp())
    return t.strftime("%d.%m.%Y"), t.strftime("%H:%M")


def _err(name: str, text: str) -> ToolResult:
    return ToolResult(call_id="", name=name, content=text, is_error=True)


# ─────────────────────────────── zone_state ───────────────────────────────


def zone_line(z: Any) -> str:
    zone = _inline(z.name)
    if z.state == "inside" and z.state_at is not None:
        date, hm = _when(z.state_at)
        return TEXTS["zone_inside"].format(zone=zone, date=date, time=hm)
    if z.state == "outside" and z.state_at is not None:
        date, hm = _when(z.state_at)
        return TEXTS["zone_outside"].format(zone=zone, date=date, time=hm)
    return TEXTS["zone_never"].format(zone=zone)


def find_zone(zones: list[Any], name: str) -> Any | None:
    want = " ".join(str(name or "").split()).casefold()
    if want in ("maison", "home"):
        home = next((z for z in zones if z.home), None)
        if home is not None:
            return home
    return next((z for z in zones if " ".join(z.name.split()).casefold() == want), None)


# ─────────────────────────────── suivi (tours de suite) ───────────────────────────────


@dataclass
class _Follow:
    task_id: str
    thread_id: str
    turn_index: int
    sender: str
    tenant: str
    channel: str
    op: str
    name: str
    machine_id: str
    render: Callable[[Any], str]  # tâche faite → bloc joint (en-tête hors clôture + clôture)
    status: str = "pending"


@dataclass
class PhoneFollow:
    """Tâches du téléphone qui ont dépassé 20 s : `phone_tasks` sur le message assistant, puis le tour de suite.

    Repris tel quel par le Bureau (docs/DESKTOP.md §12.2) : `phone` est alors la source des pas du bureau (même forme :
    `store.task(id)`, `_expire(t, now)`, `clock`, `sleep`), `tasks_field` vaut `desktop_tasks` et `continue_with` rend ses
    propres textes. Les défauts sont ceux du téléphone, inchangés."""

    phone: Any  # PhoneService (ou toute source de la même forme)
    launch: Callable[[str, str, str, str, str], None] | None = None  # (fil, membre, canal, texte, bloc joint)
    poll_s: float = 2.0
    tasks_field: str = "phone_tasks"
    continue_with: Callable[[Any, Any, str], tuple[str, str]] | None = None  # None = `continuation` du téléphone
    thread_name: str = "phone-follow"
    # (suivi, tâche, statut) → False : pas de tour de suite (ex. bureau : un pas clos sans décision de la personne ne relance rien).
    continue_if: Callable[[Any, Any, str], bool] | None = None
    _by_turn: dict[tuple[str, int], list[_Follow]] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def watch(self, f: _Follow) -> None:
        with self._lock:
            self._by_turn.setdefault((f.thread_id, f.turn_index), []).append(f)
            if len(self._by_turn) > 2000:
                for k in list(self._by_turn)[:1000]:
                    del self._by_turn[k]
        threading.Thread(target=self._wait, args=(f,), name=self.thread_name, daemon=True).start()

    def message_fields(self, thread_id: str, index: int) -> dict[str, Any]:
        with self._lock:
            rows = list(self._by_turn.get((thread_id, index), []))
        if not rows:
            return {}
        return {self.tasks_field: [{"task_id": f.task_id, "op": f.op, "machine_id": f.machine_id, "name": f.name,
                              "status": f.status} for f in rows]}

    def _wait(self, f: _Follow) -> None:
        from nokker.tenancy.context import tenant_scope

        try:
            with tenant_scope(f.tenant):
                t = self.phone.store.task(f.task_id)
                left = max(1.0, (t.deadline - self.phone.clock()) + 5.0) if t is not None else 1.0
                done = self._settle(f.task_id, left)
                self.settled(f, done)  # dans le locataire du fil : le tour de suite y tourne
        except Exception:  # noqa: BLE001 — un suivi raté : la tâche reste visible dans le journal du téléphone
            logger.exception("phone_tools : suivi de %s en échec", f.task_id)

    def _settle(self, task_id: str, timeout_s: float) -> Any:
        end = self.phone.clock() + timeout_s
        while True:
            t = self.phone.store.task(task_id)
            if t is not None:
                t = self.phone._expire(t, self.phone.clock())  # noqa: SLF001 — même règle d'échéance que la file
                if t.status in ("done", "failed", "expired", "cancelled"):
                    return t
            if self.phone.clock() >= end:
                return t
            self.phone.sleep(self.poll_s)

    def settled(self, f: _Follow, t: Any) -> None:
        """La tâche est close : le tour de suite (sauf annulée : la personne l'a arrêtée, rien à dire)."""
        status = getattr(t, "status", "expired") if t is not None else "expired"
        if status not in ("done", "failed", "expired", "cancelled"):
            status = "expired"
        f.status = status
        if status == "cancelled" or self.launch is None:
            return
        if self.continue_if is not None and not self.continue_if(f, t, status):
            logger.info("suivi %s : pas de tour de suite (%s, aucune décision de la personne)", f.task_id, status)
            return
        self.launch(f.thread_id, f.sender, f.channel, *(self.continue_with or continuation)(f, t, status))


def continuation(f: _Follow, t: Any, status: str) -> tuple[str, str]:
    """(texte du tour de suite, bloc joint). Le texte est l'en-tête du noyau ; le résultat, clôturé, est joint."""
    from nokker.qa.secrets import redact

    op, name = OP_LABELS.get(f.op, f.op), _inline(f.name)
    if status == "done":
        head = TEXTS["continue_head"].format(op=op, name=name)
        try:
            body = f.render(t)
        except Exception:  # noqa: BLE001 — résultat illisible : un échec, pas un trou
            return TEXTS["continue_failed"].format(op=op, name=name, reason=reason_text("bad_result")), ""
        return head, redact(body).text
    if status == "failed":
        code = (getattr(t, "error", None) or {}).get("code")
        return TEXTS["continue_failed"].format(op=op, name=name, reason=reason_text(code)), ""
    return TEXTS["continue_expired"].format(op=op, name=name), ""


# ─────────────────────────────── la source d'outils ───────────────────────────────


def _ocr_render(name: str) -> Callable[[Any], str]:
    def render(t: Any) -> str:
        from nokker.qa.untrusted import fence_checked, suspect_header

        text = str(json.loads(t.result or "{}").get("text") or "")
        if not text.strip():  # rien à clôturer : un bloc vide pousserait le modèle à inventer
            return TEXTS["ocr_empty"].format(name=_inline(name))
        block, suspect = fence_checked("phone:ocr", text)
        head = TEXTS["ocr_head"].format(name=_inline(name))
        return "\n".join(x for x in (suspect_header(int(suspect)), head, block) if x)

    return render


@dataclass
class PhoneTools:
    """`ToolSource` des testeurs, UN outil par instance (`tool`) : `zone_state` (zones du membre, sans téléphone) ou
    `ocr_image` (téléphone avec la capacité `ocr`). L'outil n'est offert qu'au compte qui peut s'en servir
    (`available_for` : le registre filtre par source)."""

    tool: str  # zone_state | ocr_image
    zones: Callable[[], Any]  # () → ZoneService | None (nokker/zones/http.py::service)
    phone: Any = None  # PhoneService | None (cloud, drapeau `cloud_link.phone_nodes`)
    follow: PhoneFollow | None = None
    images_of: Callable[[str], list[Any]] | None = None  # fil → ImageRef, de la plus ancienne à la plus récente
    account_of: Callable[[str], str | None] | None = None  # locataire → compte (us_…)
    wait_s: float = WAIT_S

    def specs(self) -> list[ToolSpec]:
        return [SPECS[self.tool]]

    def offered(self, tenant_id: str) -> bool:
        """Zones : le membre en a défini. OCR : un de ses téléphones déclare `ocr` (en ligne ou non ; hors ligne, l'outil
        le dit)."""
        acc = self.account_of(tenant_id) if self.account_of is not None else None
        if acc is None:
            return False
        if self.tool == "zone_state":
            zs = self.zones()
            return zs is not None and bool(zs.store.zones(acc))
        return self.phone is not None and any("ocr" in _caps(c) for c in self.phone.candidates(acc))

    def available_for(self, tenant_id: str) -> bool:
        from nokker.tenancy.context import tenant_scope

        try:
            with tenant_scope(tenant_id):
                return self.offered(tenant_id)
        except Exception:  # noqa: BLE001 — sans magasin lisible, pas d'outil
            return False

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        member = str(ctx.sender or "")
        if name != self.tool:
            return _err(name, mt.pick(mt.UNKNOWN_TOOL_BARE).format(name=name))
        if name == "zone_state":
            return self._zone_state(member, arguments)
        if self.phone is not None:
            return self._ocr(member, arguments, ctx)
        return _err(name, mt.pick(mt.UNKNOWN_TOOL_BARE).format(name=name))

    def _zone_state(self, member: str, a: dict[str, Any]) -> ToolResult:
        zs = self.zones()
        zones = zs.store.zones(member) if zs is not None else []
        if not zones:
            return _err("zone_state", TEXTS["zone_none"])
        want = a.get("zone")
        if want is None or not str(want).strip():
            return ToolResult(call_id="", name="zone_state", content="\n".join(zone_line(z) for z in zones))
        z = find_zone(zones, str(want))
        if z is None:
            names = ", ".join(f"« {_inline(x.name)} »" for x in zones)
            return _err("zone_state", TEXTS["zone_unknown"].format(zone=_inline(want), names=names))
        return ToolResult(call_id="", name="zone_state", content=zone_line(z))

    def _ocr(self, member: str, a: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from nokker.tenancy.context import current_tenant

        back = a.get("back", 1)
        back = back if isinstance(back, int) and not isinstance(back, bool) and 1 <= back <= BACK_MAX else 1
        imgs = list(self.images_of(ctx.thread_id)) if self.images_of is not None else []
        if back > len(imgs):
            return _err("ocr_image", TEXTS["ocr_no_image"].format(back=back))
        sha = imgs[-back].sha256
        origin = {"kind": "turn", "thread_id": ctx.thread_id, "turn_index": ctx.turn_index}
        phone, task, done = self.phone.run(
            member, "ocr.image", None, origin=origin, wait_s=self.wait_s, deadline_s=OCR_DEADLINE_S,
            args_of=lambda node: {"image_url": self.phone.image_url(node.machine_id, ctx.thread_id, sha)})
        if phone is None:
            return _err("ocr_image", TEXTS["phone_unavailable"].format(cap=CAP_LABELS["ocr"]))
        render = _ocr_render(phone.name)
        if done is not None:
            if done.status == "done":
                try:
                    return ToolResult(call_id="", name="ocr_image", content=render(done))
                except ValueError:
                    code = "bad_result"
            else:
                code = (done.error or {}).get("code") if done.status == "failed" else None
            op, nm = OP_LABELS["ocr.image"], _inline(phone.name)
            if done.status == "expired":
                return _err("ocr_image", TEXTS["continue_expired"].format(op=op, name=nm))
            return _err("ocr_image", TEXTS["continue_failed"].format(op=op, name=nm, reason=reason_text(code)))
        if self.follow is not None:
            self.follow.watch(_Follow(task_id=task.task_id, thread_id=ctx.thread_id, turn_index=ctx.turn_index,
                                      sender=member, tenant=current_tenant(), channel=ctx.channel, op="ocr.image",
                                      name=phone.name, machine_id=phone.id, render=render))
        return ToolResult(call_id="", name="ocr_image",
                          content=TEXTS["phone_pending"].format(op=OP_LABELS["ocr.image"], name=_inline(phone.name)))


def image_source(images: Any, account_tenant: Callable[[str], str | None]) -> Callable[[str, str], Any]:
    """`PhoneService.media_source` : `img:<fil>|<sha256>` → l'image X7 du fil SI le fil est dans le locataire du compte
    du téléphone (défense en plus de la signature) ; `audio:` et le reste : None (404, rien ne part au téléphone)."""
    from nokker.tenancy.context import tenant_of_thread

    def source(account: str, ref: str) -> tuple[bytes, str] | None:
        kind, _, rest = ref.partition(":")
        if kind != "img" or images is None:
            return None
        thread_id, _, sha = rest.rpartition("|")
        tenant = account_tenant(account)
        if not tenant or not thread_id or tenant_of_thread(thread_id) != tenant:
            return None
        data = images.get(thread_id, sha)
        if data is None:
            return None
        from nokker.images.validate import sniff_media_type

        return data, sniff_media_type(data) or "application/octet-stream"

    return source


def _caps(c: Any) -> list[str]:
    from nokker.cloud.placement import caps_of

    return caps_of(c)


_FOLLOW: PhoneFollow | None = None


def configure(follow: PhoneFollow | None) -> None:
    global _FOLLOW
    _FOLLOW = follow


def follow() -> PhoneFollow | None:
    return _FOLLOW


__all__ = ["CAP_LABELS", "OP_LABELS", "REASONS", "SPECS", "TEXTS", "WAIT_S", "PhoneFollow", "PhoneTools",
           "configure", "continuation", "find_zone", "follow", "image_source", "reason_text", "texts_sha256",
           "zone_line"]
