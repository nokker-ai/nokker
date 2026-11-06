"""`show_on_screen` / `list_screens` (docs/SCREEN-CAST.md) : écrans, demandes d'approbation, affichages en cours.

Règles (§6) :
  - premier affichage sur un écran jamais approuvé → demande `new_screen`, décidée dans la bulle par le membre qui
    demande l'affichage (ou par le propriétaire de la machine, route locale) ; l'écran est ensuite mémorisé ;
  - écran personnel d'un autre membre → demande `consent`, décidée par le membre dont c'est le contenu (et lui seul) ;
    l'accord est mémorisé pour (membre, écran), révocable ;
  - `private: true` → refus ; élément `private` → masqué ; tout texte caviardé par le noyau ;
  - le lien (jeton) n'existe que dans le home-bridge (nokker/cast/bridge.py) : le noyau ne le voit jamais, ni le
    résultat d'outil (il part au modèle), ni la bulle (elle peut passer par nokker.ai).

Le noyau ne touche pas le réseau local : découverte, envoi aux appareils et pages éphémères passent par `bridge`
(`HttpBridge`, API interne authentifiée ; `CastEngine` en direct dans les tests seulement)."""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nokker.cast import texts as T
from nokker.cast.bridge import BridgeError, HttpBridge, view_of
from nokker.cast.render import Rendered, RenderError, page, render
from nokker.cast.screens import IMAGE, PAGE, Screen, ScreenBook
from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult

logger = logging.getLogger("nokker.cast")

SOURCE = "builtin:cast"
DEFAULT_MEMBER = "local-owner"  # = nokker.local.api.LOCAL_OWNER : tour sans expéditeur sur la machine
BUBBLE_HTML_MAX = 256 * 1024
# Cadre de l'aperçu servi par le noyau (`GET /v1/cast/{id}/frame`) : SA propre CSP, sans `allow-same-origin` — la page
# rendue ne lit jamais les cookies ni le stockage de l'application, et le web peut poser une CSP stricte sur la sienne.
FRAME_CSP = ("sandbox allow-scripts; default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
             "img-src data: https:")
FRAME_HEADERS = {"Content-Type": "text/html; charset=utf-8", "Content-Security-Policy": FRAME_CSP,
                 "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
FRAME_URL_TTL_S = 120
STATUS_OF = {"pending": "idle", "showing": "working", "stopped": "done", "expired": "done", "refused": "failed",
             "failed": "failed"}


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def _default_scrub(text: str) -> str:
    from nokker.qa.secrets import redact

    return redact(text).text


@dataclass
class Request:
    id: str
    reason: str  # new_screen | consent
    decider: str


@dataclass
class Cast:
    cast_id: str
    thread_id: str
    owner: str
    screen: Screen
    rendered: Rendered
    status: str = "pending"
    request: Request | None = None
    version: int = 1
    error: str | None = None
    expires_at: float | None = None


@dataclass
class CastService:
    book: ScreenBook
    bridge: Any  # HttpBridge (ou CastEngine dans les tests) : screens / show / update / stop / pair
    scrub: Callable[[str], str] = _default_scrub
    root_of: Callable[[str], Path | str | None] | None = None  # fil → dossier de travail (images)
    fetch_web: Callable[[str], tuple[str, str]] | None = None
    ttl_s: float = 900.0
    max_uses: int = 4
    pin_screen_ip: bool = True
    clock: Callable[[], float] = time.time
    on_change: Callable[[str, str], None] | None = None  # (cast_id, thread_id) : la bulle suit
    _screens: dict[str, Screen] = field(default_factory=dict, repr=False)
    _discovered_at: float = 0.0
    _casts: dict[str, Cast] = field(default_factory=dict, repr=False)
    _requests: dict[str, str] = field(default_factory=dict, repr=False)  # request id → cast id
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    # clé des URL signées du cadre (`frame_url`) : propre au processus, jamais écrite ; un redémarrage les invalide
    frame_secret: bytes = field(default_factory=lambda: os.urandom(32), repr=False)

    # ─────────────────────────── écrans ───────────────────────────

    def discover(self, refresh: bool = False) -> list[Screen]:
        with self._lock:
            if self._screens and not refresh:
                return [self.book.merge(s) for s in self._screens.values()]
        found: dict[str, Screen] = {}
        try:
            rows = self.bridge.screens(refresh)
        except BridgeError as e:
            logger.info("écrans : home-bridge indisponible (%s)", e.code)
            rows = []
        for d in rows:
            sid = str(d.get("id") or "")
            if sid:
                found[sid] = Screen(id=sid, name=str(d.get("name") or sid), kind=str(d.get("kind") or ""),
                                    model=str(d.get("model") or ""), caps=tuple(d.get("caps") or (IMAGE,)),
                                    paired=d.get("paired"))
        with self._lock:
            self._screens = found
            self._discovered_at = self.clock()
        return [self.book.merge(s) for s in found.values()]

    def screen(self, screen_id: str) -> Screen | None:
        with self._lock:
            s = self._screens.get(screen_id)
        return self.book.merge(s) if s is not None else None

    def resolve(self, wanted: str) -> tuple[Screen | None, list[Screen]]:
        screens = self.discover()
        w = wanted.strip().lower()
        exact = [s for s in screens if s.id.lower() == w]
        if exact:
            return exact[0], exact
        hits = [s for s in screens if w and w in (s.name.lower(), (s.room or "").lower())]
        if len(hits) == 1:
            return hits[0], hits
        if not hits:
            hits = [s for s in screens if w and (w in s.name.lower() or w in (s.room or "").lower())]
        return (hits[0] if len(hits) == 1 else None), hits

    # ─────────────────────────── outils ───────────────────────────

    def list_text(self, ctx: ToolContext, refresh: bool) -> str:
        screens = self.discover(refresh)
        if not screens:
            return T.LIST_EMPTY
        me = ctx.sender or DEFAULT_MEMBER
        lines = [T.LIST_HEAD]
        for s in sorted(screens, key=lambda x: (x.room or x.name).lower()):
            state = T.STATE_APPROVED if s.approved else T.STATE_NEW
            if s.owner is not None:
                state += T.STATE_MINE if s.owner == me else T.STATE_OTHER
            if s.kind == "airplay" and s.paired is False:
                state += T.STATE_UNPAIRED
            caps = ", ".join(T.CAPS[c] for c in s.caps)
            lines.append(T.LIST_LINE.format(id=s.id, name=self.scrub(s.name), room=self.scrub(s.room or s.name),
                                            kind=s.kind, caps=caps, state=state))
        return "\n".join(lines)

    def _read_image(self, thread_id: str) -> Callable[[str], bytes]:
        def read(path: str) -> bytes:
            from nokker.pages.tools import _relative
            from nokker.tools.checkpoints import WORKSPACE_MOUNT, host_path

            root = self.root_of(thread_id) if self.root_of is not None else None
            if not root:
                raise RenderError(T.WHY_NO_ROOT)
            rel = _relative(path, WORKSPACE_MOUNT)
            base = Path(root).resolve()
            full = Path(host_path(base, rel, WORKSPACE_MOUNT)).resolve()
            if full != base and base not in full.parents:
                raise RenderError(T.WHY_OUTSIDE.format(path=path[:200]))
            if not full.is_file():
                raise RenderError(T.WHY_NOT_FOUND.format(path=path[:200]))
            from nokker.cast.render import MAX_IMAGE_BYTES

            if full.stat().st_size > MAX_IMAGE_BYTES:
                raise RenderError(T.WHY_IMAGE_SIZE.format(path=path[:200], mb=MAX_IMAGE_BYTES // (1024 * 1024)))
            return full.read_bytes()

        return read

    def _render(self, args: dict[str, Any], thread_id: str) -> Rendered:
        return render(args, scrub=self.scrub, read_image=self._read_image(thread_id), fetch_web=self.fetch_web)

    def _block(self, c: Cast) -> str:
        return T.BLOCK.format(data=json.dumps({"cast_id": c.cast_id, "status": c.status}, ensure_ascii=False))

    def _masked(self, r: Rendered) -> str:
        return T.MASKED.format(n=r.masked) if r.masked else ""

    def show(self, args: dict[str, Any], ctx: ToolContext) -> tuple[str, bool]:
        """Rend (texte du résultat, erreur ?)."""
        me = ctx.sender or DEFAULT_MEMBER
        if args.get("private") is True:
            return T.PRIVATE_REFUSED, True
        cast_id = str(args.get("cast_id") or "").strip()
        if cast_id:
            return self._again(cast_id, args, ctx, me)
        wanted = str(args.get("screen") or "").strip()
        if not wanted:
            return T.MISSING_SCREEN, True
        s, hits = self.resolve(wanted)
        if s is None:
            if hits:
                names = ", ".join(f"{h.id} « {self.scrub(h.name)} »" for h in hits[:8])
                return T.AMBIGUOUS_SCREEN.format(screen=self.scrub(wanted)[:80], names=names), True
            return T.UNKNOWN_SCREEN.format(screen=self.scrub(wanted)[:80]), True
        kind = str(args.get("kind") or "")
        if PAGE not in s.caps and kind != "image":
            return T.IMAGES_ONLY.format(name=self.scrub(s.name), kind=s.kind, what=kind[:20]), True
        try:
            r = self._render(args, ctx.thread_id)
        except RenderError as e:
            return T.REFUSED.format(why=str(e)), True
        c = Cast(cast_id=f"cast_{secrets.token_hex(6)}", thread_id=ctx.thread_id, owner=me, screen=s, rendered=r)
        with self._lock:
            self._casts[c.cast_id] = c
        return self._advance(c)

    def _advance(self, c: Cast) -> tuple[str, bool]:
        """Demande suivante à poser, ou diffusion."""
        s = self.screen(c.screen.id) or c.screen
        c.screen = s
        name = self.scrub(s.name)
        if not s.approved:
            self._ask(c, "new_screen")
            return T.PENDING_SCREEN.format(cast_id=c.cast_id, name=name) + "\n" + self._block(c), False
        if s.owner is not None and s.owner != c.owner and not self.book.consented(c.owner, s.id):
            self._ask(c, "consent")
            return T.PENDING_CONSENT.format(cast_id=c.cast_id, name=name) + "\n" + self._block(c), False
        return self._start(c)

    def _ask(self, c: Cast, reason: str) -> None:
        with self._lock:
            if c.request is not None:
                self._requests.pop(c.request.id, None)
            c.request = Request(id=f"cr_{secrets.token_hex(6)}", reason=reason, decider=c.owner)
            c.status = "pending"
            self._requests[c.request.id] = c.cast_id

    def _send(self, c: Cast, update: bool) -> tuple[str, bool] | None:
        """Envoi au bridge ; None si tout va bien, sinon (texte, erreur)."""
        name = self.scrub(c.screen.name)
        kw = {"ttl_s": self.ttl_s, "max_uses": self.max_uses, "pin_screen_ip": self.pin_screen_ip}
        try:
            out = (self.bridge.update(c.cast_id, view_of(c.rendered), **kw) if update
                   else self.bridge.show(c.cast_id, c.screen.id, view_of(c.rendered), **kw))
        except BridgeError as e:
            if e.code == "gone":
                c.status = "expired"
                return T.NO_CAST.format(cast_id=c.cast_id), True
            c.status = "failed"
            if e.code == "pairing":
                c.error = "pairing"
                return T.NEEDS_PAIRING.format(name=name), True
            if e.code == "images_only":
                return T.IMAGES_ONLY.format(name=name, kind=c.screen.kind, what=c.rendered.kind), True
            c.error = self.scrub(str(e))[:200]
            return T.FAILED.format(name=name, error=c.error), True
        c.expires_at = float(out.get("expires_at") or (self.clock() + self.ttl_s))
        c.status, c.request = "showing", None
        self._arm_expiry(c)
        return None

    def _arm_expiry(self, c: Cast) -> None:
        """La bulle passe « expirée » à l'heure dite, sans attendre un autre appel."""
        delay = max(0.0, (c.expires_at or 0) - self.clock())
        version = c.version

        def fire() -> None:
            if c.status == "showing" and c.version == version and self.clock() >= (c.expires_at or 0):
                c.status = "expired"
                self._changed(c)

        t = threading.Timer(delay + 0.5, fire)
        t.daemon = True
        t.start()

    def _start(self, c: Cast) -> tuple[str, bool]:
        failed = self._send(c, update=False)
        if failed is not None:
            return failed
        s = c.screen
        return (T.SHOWN.format(name=self.scrub(s.name), room=self.scrub(s.room or s.name), cast_id=c.cast_id,
                               minutes=int(self.ttl_s // 60)) + self._masked(c.rendered) + "\n" + self._block(c),
                False)

    def _mine(self, cast_id: str, ctx: ToolContext, me: str) -> Cast | None:
        with self._lock:
            c = self._casts.get(cast_id)
        if c is None or c.thread_id != ctx.thread_id or c.owner != me:
            return None  # même réponse qu'un affichage inconnu : rien d'un autre fil ou d'un autre membre
        return c

    def _again(self, cast_id: str, args: dict[str, Any], ctx: ToolContext, me: str) -> tuple[str, bool]:
        c = self._mine(cast_id, ctx, me)
        if c is None or self._expired(c) or c.status in ("stopped", "refused", "failed"):
            return T.NO_CAST.format(cast_id=cast_id[:40]), True
        if args.get("stop") is True:
            self._stop(c)
            return T.STOPPED.format(cast_id=c.cast_id) + "\n" + self._block(c), False
        if PAGE not in c.screen.caps and str(args.get("kind") or "") != "image":
            return T.IMAGES_ONLY.format(name=self.scrub(c.screen.name), kind=c.screen.kind,
                                        what=str(args.get("kind") or "")[:20]), True
        try:
            r = self._render(args, ctx.thread_id)
        except RenderError as e:
            return T.REFUSED.format(why=str(e)), True
        c.rendered, c.version = r, c.version + 1
        if c.status == "pending":
            return T.PENDING_UPDATED.format(cast_id=c.cast_id) + "\n" + self._block(c), False
        failed = self._send(c, update=True)
        if failed is not None:
            return failed
        return T.UPDATED.format(cast_id=c.cast_id, name=self.scrub(c.screen.name)) + self._masked(r) + "\n" \
            + self._block(c), False

    def _stop(self, c: Cast) -> None:
        try:
            self.bridge.stop(c.cast_id)
        except BridgeError:
            logger.debug("écrans : arrêt par le bridge en échec", exc_info=True)
        c.status, c.request = "stopped", None

    def _expired(self, c: Cast) -> bool:
        if c.status == "showing" and c.expires_at is not None and self.clock() >= c.expires_at:
            c.status = "expired"
        return c.status == "expired"

    def _changed(self, c: Cast) -> None:
        if self.on_change is not None:
            try:
                self.on_change(c.cast_id, c.thread_id)
            except Exception:
                logger.debug("bulle cast non mise à jour", exc_info=True)

    # ─────────────────────────── décisions ───────────────────────────

    def decide(self, request_id: str, approve: bool, user: str | None, *, machine_owner: bool = False
               ) -> dict[str, Any]:
        """Décision d'une demande. `user` : membre qui clique dans la bulle. `machine_owner` : route locale (jeton de
        la machine) — elle peut approuver un ÉCRAN, jamais l'accord d'un autre membre sur son contenu."""
        with self._lock:
            c = self._casts.get(self._requests.get(request_id, ""))
        if c is None or c.request is None or c.request.id != request_id:
            raise LookupError("demande inconnue")
        req = c.request
        allowed = user == req.decider or (machine_owner and (req.reason == "new_screen"
                                                             or req.decider == DEFAULT_MEMBER))
        if not allowed:
            raise PermissionError("cette décision appartient au membre dont c'est le contenu")
        with self._lock:
            self._requests.pop(request_id, None)
        if not approve:
            c.status, c.request = "refused", None
            self._changed(c)
            return self.bubble_state(c.cast_id) or {}
        now = self.clock()
        if req.reason == "new_screen":
            self.book.remember(c.screen, by=user or DEFAULT_MEMBER, now=now)
        else:
            self.book.consent(c.owner, c.screen.id, now=now)
        c.request = None
        self._advance(c)
        self._changed(c)
        return self.bubble_state(c.cast_id) or {}

    def bubble_action(self, state: dict[str, Any], action: str, user: str | None) -> dict[str, Any]:
        cast_id = str(state.get("cast_id") or "")
        with self._lock:
            c = self._casts.get(cast_id)
        if c is None:
            raise LookupError("affichage inconnu")
        if action == "stop":
            if user != c.owner:
                raise PermissionError("seul le membre qui a lancé l'affichage l'arrête")
            self._stop(c)
            return self.bubble_state(cast_id) or {}
        if c.request is None:
            raise LookupError("aucune demande en attente")
        return self.decide(c.request.id, action == "approve", user)

    # ─────────────────────────── bulle ───────────────────────────

    def bubble_state(self, cast_id: str) -> dict[str, Any] | None:
        """État de la bulle `cast` (contrat docs/SCREEN-CAST.md §7) : jamais le lien ni l'hôte de l'écran."""
        with self._lock:
            c = self._casts.get(cast_id)
        if c is None:
            return None
        self._expired(c)
        html = page(c.rendered, live=False)
        too_big = len(html.encode()) > BUBBLE_HTML_MAX
        actions = ([{"id": "approve", "label": "Approuver", "primary": True}, {"id": "refuse", "label": "Refuser"}]
                   if c.status == "pending" else [{"id": "stop", "label": "Arrêter"}] if c.status == "showing" else [])
        return {"cast_id": c.cast_id, "status": c.status, "bubble_status": STATUS_OF[c.status],
                "screen": c.screen.public(),
                "request": {"id": c.request.id, "reason": c.request.reason} if c.request else None,
                "view": dict(c.rendered.summary), "html": None if too_big else html, "html_too_large": too_big,
                "masked": c.rendered.masked, "version": c.version,
                "expires_at": _iso(c.expires_at) if c.status == "showing" else None,
                "error": c.error, "actions": actions}

    # ─────────────────────────── cadre de l'aperçu (CSP propre) ───────────────────────────

    def _frame_sig(self, c: Cast, exp: str) -> str:
        import hashlib
        import hmac

        return hmac.new(self.frame_secret, f"{c.cast_id}.{c.owner}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]

    def frame_url(self, cast_id: str, user: str) -> dict[str, Any]:
        """URL signée (2 min) du cadre de l'aperçu, pour `<iframe src>` (qui ne pose jamais `Authorization`). Le
        PROPRIÉTAIRE de l'aperçu seulement : `LookupError` (404) pour tout autre, sans dire si l'aperçu existe."""
        with self._lock:
            c = self._casts.get(cast_id)
        if c is None or c.owner != user:
            raise LookupError("aperçu inconnu")
        exp = str(int(self.clock() + FRAME_URL_TTL_S))
        return {"url": f"/v1/cast/{cast_id}/frame?exp={exp}&sig={self._frame_sig(c, exp)}",
                "expires_at": _iso(float(exp))}

    def frame_html(self, cast_id: str, *, exp: str = "", sig: str = "", user: str | None = None) -> str:
        """HTML du cadre : EXACTEMENT celui de la bulle (`bubble_state["html"]`). Accès : signature valide et non
        échue, OU propriétaire authentifié. `LookupError` sinon (404) ; `ValueError` au-delà de 256 Ko (413)."""
        import hmac

        with self._lock:
            c = self._casts.get(cast_id)
        if c is None:
            raise LookupError("aperçu inconnu")
        if user is not None:
            if c.owner != user:
                raise LookupError("aperçu inconnu")
        else:
            try:
                fresh = int(exp) >= self.clock()
            except (TypeError, ValueError):
                fresh = False
            if not fresh or not hmac.compare_digest(self._frame_sig(c, str(exp)), str(sig or "")):
                raise LookupError("aperçu inconnu")
        self._expired(c)
        html = page(c.rendered, live=False)
        if len(html.encode()) > BUBBLE_HTML_MAX:
            raise ValueError("html_too_large")
        return html

    # ─────────────────────────── routes locales ───────────────────────────

    def screens_view(self, refresh: bool = False) -> list[dict[str, Any]]:
        return [{**s.public(), "owner": s.owner, "paired": s.paired} for s in self.discover(refresh)]

    def pair(self, screen_id: str, pin: str | None) -> dict[str, Any]:
        try:
            out = self.bridge.pair(screen_id, pin)
        except BridgeError as e:
            raise (LookupError(str(e)) if e.code == "unknown_screen" else RuntimeError(e.code)) from None
        return {"screen": screen_id, "state": out.get("state")}

    def tool_source(self) -> CastToolSource:
        return CastToolSource(self)

    def close(self) -> None:
        self.bridge.close()


def _spec_show() -> ToolSpec:
    P = T.SHOW_PARAMS
    point = {"type": "object", "properties": {"lat": {"type": "number"}, "lon": {"type": "number"},
                                              "label": {"type": "string"}, "private": {"type": "boolean"}}}
    slide = {"type": "object", "properties": {"title": {"type": "string"}, "text": {"type": "string"},
                                              "image": {"type": "string"}, "private": {"type": "boolean"}}}
    return ToolSpec(
        name="show_on_screen", description=T.SHOW_DESCRIPTION, effect=Effect.WRITE, source=SOURCE, locality="local",
        parameters={"type": "object", "properties": {
            "screen": {"type": "string", "description": P["screen"]},
            "kind": {"type": "string", "enum": ["diagram", "table", "image", "map", "slides", "web"],
                     "description": P["kind"]},
            "title": {"type": "string", "description": P["title"]},
            "svg": {"type": "string", "description": P["svg"]},
            "columns": {"type": "array", "items": {"type": "string"}, "description": P["columns"]},
            "rows": {"type": "array", "description": P["rows"]},
            "path": {"type": "string", "description": P["path"]},
            "lat": {"type": "number", "description": P["lat"]},
            "lon": {"type": "number", "description": P["lon"]},
            "zoom": {"type": "integer", "description": P["zoom"]},
            "markers": {"type": "array", "items": point, "description": P["markers"]},
            "slides": {"type": "array", "items": slide, "description": P["slides"]},
            "interval_s": {"type": "integer", "description": P["interval_s"]},
            "url": {"type": "string", "description": P["url"]},
            "private": {"type": "boolean", "description": P["private"]},
            "cast_id": {"type": "string", "description": P["cast_id"]},
            "stop": {"type": "boolean", "description": P["stop"]}}})


def _spec_list() -> ToolSpec:
    return ToolSpec(name="list_screens", description=T.LIST_DESCRIPTION, effect=Effect.READ, source=SOURCE,
                    locality="local", fresh=True,
                    parameters={"type": "object", "properties": {
                        "refresh": {"type": "boolean", "description": T.LIST_REFRESH}}})


CAST_TOOLS = ("list_screens", "show_on_screen")


@dataclass
class CastToolSource:
    service: CastService

    def specs(self) -> list[ToolSpec]:
        return [_spec_list(), _spec_show()]

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if name == "list_screens":
            return ToolResult(call_id="", name=name, content=self.service.list_text(ctx, arguments.get("refresh")
                                                                                    is True))
        if name == "show_on_screen":
            text, err = self.service.show(arguments or {}, ctx)
            return ToolResult(call_id="", name=name, content=text, is_error=err)
        return ToolResult(call_id="", name=name, content=f"outil inconnu : {name}", is_error=True)


def default_fetch_web(url: str) -> tuple[str, str]:
    """Page web en lecture : récupération gardée des aperçus (SSRF, PII, taille), texte extrait par trafilatura."""
    from nokker.previews.fetch import guarded_get
    from nokker.previews.parse import extract_excerpt, extract_meta

    try:
        res = guarded_get(url, max_bytes=2 * 1024 * 1024)
    except Exception:  # noqa: BLE001 — refus SSRF/PII, délai, transport : un seul motif pour le modèle
        raise RenderError(T.WHY_WEB.format(url=url[:200])) from None
    if res.status_code >= 400 or "html" not in res.content_type:
        raise RenderError(T.WHY_WEB.format(url=url[:200]))
    body = res.body.decode("utf-8", errors="replace")
    meta = extract_meta(body, res.final_url)
    return (getattr(meta, "title", "") or ""), extract_excerpt(body, res.final_url, max_chars=12000)


def build_cast(conf: dict[str, Any], state_dir: Path, *, vault: Any = None,
               root_of: Callable[[str], Any] | None = None) -> CastService:
    """`local.cast` : {enabled, ttl_s (≤ 3600), max_uses, pin_screen_ip}. Le noyau ne parle qu'à home-bridge :
    `NOKKER_HOME_BRIDGE_URL` (défaut http://home-bridge:8800, posé par le profil `home`) et `NOKKER_HOME_BRIDGE_TOKEN`
    (lu à chaque appel) ; `bridge_url` / `bridge_token_env` du bloc les remplacent (tests, déploiements à part)."""
    env = str(conf.get("bridge_token_env") or "NOKKER_HOME_BRIDGE_TOKEN")
    url = str(conf.get("bridge_url") or os.environ.get("NOKKER_HOME_BRIDGE_URL") or "http://home-bridge:8800")
    bridge = HttpBridge(base_url=url, token=lambda: os.environ.get(env, ""))
    return CastService(book=ScreenBook(Path(state_dir) / "cast" / "screens.json"), bridge=bridge,
                       root_of=root_of, fetch_web=default_fetch_web,
                       ttl_s=min(3600.0, float(conf.get("ttl_s") or 900)), max_uses=int(conf.get("max_uses") or 4),
                       pin_screen_ip=bool(conf.get("pin_screen_ip", True)))


__all__ = [
    "CAST_TOOLS",
    "DEFAULT_MEMBER",
    "IMAGE",
    "Cast",
    "CastService",
    "CastToolSource",
    "build_cast",
    "default_fetch_web",
]
