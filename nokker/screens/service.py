"""Écrans du mur (lot V7, docs/SCREENS.md) : outil `screen_draft`, bulle `screen`, carte forte `wall:publish`, rendu pour
le mur. Le modèle compose (catalogue fermé), la personne publie (bouton de la bulle, puis carte forte), le mur lit."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from nokker.core import model_texts as mt
from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.screens import texts as T
from nokker.screens.catalog import SpecError, validate
from nokker.screens.render import MAX_HTML, render
from nokker.screens.store import Screen, ScreenStore

logger = logging.getLogger("nokker.screens")

SOURCE = "builtin:screens"
PUBLISH_TOOL = "wall_publish"
PUBLISH_RULE = "wall:publish"
PUBLISH_CHANNEL = "screens"
LOCAL_MEMBER = "local-owner"  # = nokker.local.api.LOCAL_OWNER : tour sans expéditeur sur la machine (comme cast)
_ID = re.compile(r"^scr_[0-9a-f]{12}$")
_FRAME_KEY = re.compile(r"^[A-Za-z0-9_-]{22,64}$")
# Cadre servi par le noyau (`GET /v1/screens/frame/{clé}`) : la page d'un `srcdoc` hériterait de la CSP du web
# (`style-src 'self'` bloque son <style>) ; servie ici, elle a SA CSP : sandbox SANS allow-scripts, rien d'autre que son
# propre style (par empreinte). Même modèle que le cadre de cast (docs/SCREEN-CAST.md).
FRAME_TTL_S = 300
FRAME_MAX = 256
_BLOCK = re.compile(r"<!-- nokker:screen -->\s*(\{.*?\})\s*<!-- /nokker:screen -->", re.S)


class NoIdentity(LookupError):
    """Ni expéditeur ni locataire hors de Lokkal : aucun repli, l'écran n'est ni écrit ni lu."""


def _has_event(spec: dict[str, Any]) -> bool:
    return any(b.get("type") == "next_event" or _has_event({"blocks": b.get("blocks") or []})
               for b in spec.get("blocks") or [])


def spec_sha(spec: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def screen_ids(content: str) -> list[dict[str, Any]]:
    out = []
    for m in _BLOCK.finditer(content or ""):
        try:
            out.append(json.loads(m.group(1)))
        except ValueError:
            continue
    return out


@dataclass
class ScreenService:
    store: ScreenStore
    approvals: Any = None
    tz: str = "Europe/Zurich"
    clock: Callable[[], float] = time.time
    # Lokkal seulement (`local_api`) : un tour sans expéditeur est celui du propriétaire de la machine (`local-owner`,
    # comme cast) et le locataire par défaut est l'interne. Hors Lokkal (noyau multi-locataire) : AUCUN repli.
    local: bool = False
    on_change: Callable[[str, str], Any] | None = None      # (screen_id, thread_id) : la bulle suit
    _pending: dict[str, str] = field(default_factory=dict, init=False)  # screen_id → id de la carte en attente
    frame_secret: bytes = field(default_factory=lambda: secrets.token_bytes(32), repr=False)
    _frames: dict[str, tuple[str, float]] = field(default_factory=dict, init=False, repr=False)

    # ── identité (le seul endroit où un repli existe, et seulement sur Lokkal) ──
    def tenant(self) -> str:
        from nokker.tenancy.context import INTERNAL_TENANT, current_tenant_or_none

        t = current_tenant_or_none()
        if t:
            return t
        if self.local:
            return INTERNAL_TENANT
        raise NoIdentity("aucun locataire")

    def member(self, user: str | None) -> str:
        if user:
            return user
        if self.local:
            return LOCAL_MEMBER
        raise NoIdentity("aucun expéditeur")

    # ── modèle ──
    def tool_source(self) -> ScreenToolSource:
        return ScreenToolSource(self)

    def draft(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        def out(text: str, error: bool = False) -> ToolResult:
            return ToolResult(call_id="", name=T.TOOL, content=text, is_error=error)

        try:
            tenant, member = self.tenant(), self.member(ctx.sender)
        except NoIdentity:
            return out(T.OFF, True)
        raw = {k: args[k] for k in ("title", "theme", "layout", "blocks") if k in args}
        try:
            spec = validate(raw)
        except SpecError as e:
            return out(T.INVALID.format(problems="\n".join(f"- {p}" for p in e.problems)), True)
        now = self.clock()
        sid = str(args.get("screen_id") or "").strip()
        if sid:
            cur = self.store.get(tenant, sid) if _ID.match(sid) else None
            if cur is None or cur.thread_id != ctx.thread_id or cur.account_id != member:
                return out(T.UNKNOWN.format(screen_id=sid[:40]), True)
            s = Screen(**{**cur.__dict__, "title": spec["title"], "spec": spec, "version": cur.version + 1,
                          "updated_at": now})
        else:
            s = Screen(screen_id="scr_" + secrets.token_hex(6), account_id=member, tenant_id=tenant,
                       thread_id=ctx.thread_id, title=spec["title"], spec=spec, created_at=now, updated_at=now)
        self.store.save(s)
        block = json.dumps({"screen_id": s.screen_id, "version": s.version})
        return out(T.RESULT.format(screen_id=s.screen_id, title=s.title, version=s.version)
                   + f"\n<!-- nokker:screen -->{block}<!-- /nokker:screen -->")

    # ── bulle `screen` ──
    def _now(self) -> datetime:
        try:
            return datetime.fromtimestamp(self.clock(), ZoneInfo(self.tz))
        except Exception:  # noqa: BLE001 — fuseau inconnu : UTC
            return datetime.fromtimestamp(self.clock(), timezone.utc)

    def bubble_state(self, tenant_id: str, screen_id: str) -> dict[str, Any] | None:
        s = self.store.get(tenant_id, screen_id)
        if s is None:
            return None
        html = render(s.spec, now=self._now(), preview=True)
        live = s.status == "published" and s.published_version == s.version
        if screen_id in self._pending:
            status = "pending"
        elif live:
            status = "published"
        else:
            status = "draft"
        actions = []
        if status == "draft":
            actions.append({"id": "publish", "label": T.PUBLISH_LABEL, "primary": True})
        if s.status == "published":
            actions.append({"id": "unpublish", "label": "Retirer du mur"})
        return {"screen_id": s.screen_id, "version": s.version, "title": s.title, "status": status,
                "published_version": s.published_version,
                "html": html if len(html) <= MAX_HTML else None, "html_too_large": len(html) > MAX_HTML,
                "bubble_status": "done" if status == "published" else "idle", "actions": actions}

    def bubble_action(self, tenant_id: str, state: dict[str, Any], action: str, user: str | None) -> dict[str, Any]:
        sid = str(state.get("screen_id") or "")
        s = self.store.get(tenant_id, sid)
        if s is None:
            raise LookupError("écran inconnu")
        try:
            who = self.member(user)
        except NoIdentity:
            raise PermissionError("aucune personne identifiée") from None
        if who != s.account_id:
            raise PermissionError("seule la personne qui a composé l'écran le publie ou le retire")
        if action == "publish":
            if self.approvals is None:
                raise LookupError("cartes indisponibles")
            self._request_publish(s, user)
        elif action == "unpublish":
            self.unpublish(tenant_id, s)
        else:
            raise LookupError(f"action inconnue : {action}")
        return self.bubble_state(tenant_id, sid) or {}

    # ── carte forte ──
    def _request_publish(self, s: Screen, by: str | None) -> Any:
        from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict

        args = {"screen_id": s.screen_id, "title": s.title, "version": s.version, "sha256": spec_sha(s.spec),
                "tenant_id": s.tenant_id}
        call = ToolCall(id=f"wall-{s.screen_id}-v{s.version}", name=PUBLISH_TOOL, arguments=args)
        verdict = Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=PUBLISH_RULE,
                          reason=T.CARD_REASON.format(title=s.title, version=s.version,
                                                      event=T.CARD_EVENT if _has_event(s.spec) else ""),
                          subject=call.id,
                          strength="strong")
        turn = Turn(thread_id=s.thread_id, index=0, input=InputCheck(text=""), channel=PUBLISH_CHANNEL, sender=by)
        card = self.approvals.request(turn, call, verdict, None)
        self._pending[s.screen_id] = getattr(card, "id", "")
        return card

    def publish_handler(self) -> Callable[[Any], str]:
        """Décision d'une carte `wall_publish` : approuvée → CETTE version (même empreinte) devient l'écran du mur ;
        l'écran publié avant est retiré. Refusée ou expirée → rien."""

        def handle(approval: Any) -> str:
            a = getattr(approval, "arguments", None) or {}
            sid, tenant = str(a.get("screen_id") or ""), str(a.get("tenant_id") or "")
            self._pending.pop(sid, None)
            s = self.store.get(tenant, sid)
            if getattr(approval, "status", "") != "approved" or s is None:
                self._changed(s)
                return ""
            if s.version != int(a.get("version") or 0) or spec_sha(s.spec) != a.get("sha256"):
                self._changed(s)
                return "écran modifié depuis la carte : rien n'a été publié, redemandez la publication"
            self.store.publish(tenant, sid, s.version, spec_sha(s.spec), s.spec, self.clock())
            self._changed(s)
            return f"écran « {s.title} » publié sur le mur"

        return handle

    def unpublish(self, tenant_id: str, s: Screen) -> None:
        self.store.unpublish(tenant_id, s.screen_id)
        self._changed(s)

    def _changed(self, s: Screen | None) -> None:
        if s is not None and self.on_change is not None:
            try:
                self.on_change(s.screen_id, s.thread_id)
            except Exception:  # noqa: BLE001
                logger.exception("écrans : bulle de %s non mise à jour", s.screen_id)

    # ── cadre servi par le noyau (CSP propre) ──
    def _frame_sig(self, key: str, exp: str) -> str:
        import hmac

        return hmac.new(self.frame_secret, f"{key}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]

    def frame_url_for(self, html: str) -> str:
        """URL signée (5 min) d'un rendu déjà fait, pour `<iframe src>` : la clé est aléatoire, le rendu est gardé en
        mémoire jusqu'à l'échéance (au plus 256 cadres)."""
        now = self.clock()
        for k in [k for k, (_h, e) in self._frames.items() if e < now]:
            del self._frames[k]
        while len(self._frames) >= FRAME_MAX:
            del self._frames[next(iter(self._frames))]
        key = secrets.token_urlsafe(18)
        exp = str(int(now + FRAME_TTL_S))
        self._frames[key] = (html, float(exp))
        return f"/v1/screens/frame/{key}?exp={exp}&sig={self._frame_sig(key, exp)}"

    def frame_html(self, key: str, exp: str, sig: str) -> str:
        """HTML d'un cadre signé ; `LookupError` (404) si la clé, la signature ou l'échéance ne vont pas."""
        import hmac

        found = self._frames.get(key) if _FRAME_KEY.match(key or "") else None
        try:
            fresh = int(exp) >= self.clock()
        except (TypeError, ValueError):
            fresh = False
        if found is None or not fresh or not hmac.compare_digest(self._frame_sig(key, str(exp)), str(sig or "")):
            raise LookupError("cadre inconnu")
        return found[0]

    def preview_frame_url(self, tenant_id: str, screen_id: str, user: str | None) -> dict[str, Any]:
        """`POST /v1/screens/{id}/frame-url` : l'aperçu (sans données du foyer) de la DERNIÈRE version, pour l'auteur."""
        s = self.store.get(tenant_id, screen_id)
        if s is None or (user is not None and user != s.account_id):
            raise LookupError("écran inconnu")
        if user is None and not self.local:  # « tout écran de la machine » : le jeton local de Lokkal seulement
            raise LookupError("écran inconnu")
        return {"url": self.frame_url_for(render(s.spec, now=self._now(), preview=True)), "version": s.version}

    # ── mur ──
    def for_wall(self, tenant_id: str, account_id: str, next_event: dict[str, Any] | None) -> dict[str, Any] | None:
        """L'écran publié du compte, rendu MAINTENANT avec l'heure et le prochain événement déjà filtrés par le mur."""
        try:
            who = self.member(account_id)
        except NoIdentity:
            return None
        s = self.store.published_for(tenant_id, who)
        if s is None or not s.published_spec:
            return None
        html = render(s.published_spec, now=self._now(), next_event=next_event)
        if len(html) > MAX_HTML:
            return None
        return {"id": s.screen_id, "version": s.published_version, "title": s.published_spec.get("title", s.title),
                "html": html, "frame_url": self.frame_url_for(html)}


class ScreenToolSource:
    def __init__(self, svc: ScreenService) -> None:
        self.svc = svc

    def specs(self) -> list[ToolSpec]:
        return [ToolSpec(name=T.TOOL, description=T.DESCRIPTION, parameters=T.PARAMETERS, effect=Effect.READ,
                         core=False, source=SOURCE)]

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if name != T.TOOL:
            return ToolResult(call_id="", name=name, content=mt.pick(mt.UNKNOWN_TOOL_BARE).format(name=name), is_error=True)
        return self.svc.draft(ctx, arguments or {})


__all__ = ["PUBLISH_RULE", "PUBLISH_TOOL", "ScreenService", "ScreenToolSource", "screen_ids", "spec_sha"]
