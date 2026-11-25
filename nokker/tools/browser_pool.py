"""Client du pool de navigateurs de la bêta (contrat : docs/BROWSER-POOL.md, fournisseur : le service du pool).

UNE source d'outils pour tous les testeurs : chaque appel lit le locataire du tour (`current_tenant()`), ouvre ou
reprend la session navigateur de CE locataire (`POST /sessions {tenant_id}` : 201 nouvelle, 200 la même si elle vit
encore), puis passe l'appel MCP à `mcp_url` avec le jeton de la session. Un testeur ne voit jamais le Chrome d'un
autre : la session est rangée par locataire, et le gestionnaire ne sert une session qu'avec son jeton.

- Schémas : ceux du canari (`browser_pool_tools.json`, capturés le 21.10), sans `browser_lane`/`browser_profile`
  (retirés par le contrat) ni `browser_file_upload` (aucun fichier du testeur dans le pod). Pas d'appel au pool pour
  lister les outils : ils existent avant toute session.
- Session expirée ou fermée (404, JSON-RPC -32001) : rouverte une fois, l'appel rejoué. Plafond atteint (429) : le
  testeur l'apprend en une phrase (« occupé, réessaie dans N s »), jamais une trace. Démarrage raté (503) : idem.
- Fin de tour : `browser_close` si le tour a utilisé le navigateur (onglets rendus, Chrome et profil gardés ; même
  règle que le canari) ; l'inactivité (5 min) ferme ensuite la session côté pool.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from nokker.core import model_texts as mt
from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.refusal import REFUSED_PREFIX, refused_prefix
from nokker.core.turn import Effect, ToolResult
from nokker.tenancy.context import INTERNAL_TENANT, current_tenant
from nokker.tools.effects import ARGUMENT_EFFECTS, DEFAULT_EFFECTS

logger = logging.getLogger("nokker.tools.browser_pool")
SCHEMAS = Path(__file__).parent / "browser_pool_tools.json"
_EXPIRED = (-32001, -32003)  # session expirée ou fermée ; pod injoignable
# r44 : une référence d'élément n'est acceptée que si le DERNIER instantané rendu par le noyau la contient (une page ne
# peut pas en fabriquer : `result_fence.browser_refs`). Outils qui désignent un élément par sa référence :
_REF_ARGS = {"browser_click": ("ref",), "browser_type": ("ref",), "browser_hover": ("ref",),
             "browser_select_option": ("ref",), "browser_scroll": ("ref",), "browser_read_text": ("ref",),
             "browser_take_screenshot": ("ref",), "browser_evaluate": ("ref",), "browser_drag": ("startRef", "endRef")}
_PAGE_CHANGE = ("browser_navigate", "browser_navigate_back", "browser_tabs")
REF_REFUSED = REFUSED_PREFIX + mt.BROWSER_REF_REFUSED["fr"]  # figé ; langue du tour : `model_texts`


def _asked_refs(name: str, args: dict[str, Any]) -> list[str]:
    refs = [str(args[k]) for k in _REF_ARGS.get(name, ()) if args.get(k) not in (None, "")]
    if name == "browser_fill_form":
        refs += [str(f.get("ref")) for f in args.get("fields") or [] if isinstance(f, dict) and f.get("ref")]
    return refs


@dataclass
class _Session:
    id: str
    mcp_url: str
    token: str
    expires_at: float
    io: Any = None


@dataclass
class BrowserPoolSource:
    base_url: str  # NOKKER_BROWSER_POOL_URL
    api_token: Any  # () -> NOKKER_BROWSER_POOL_TOKEN
    timeout_s: float = 60.0
    client: Any = None  # httpx.Client injectable (tests)
    mcp_factory: Any = None  # (url, headers) -> objet avec call(name, args) -> (texte, is_error) ; tests
    _sessions: dict[str, _Session] = field(default_factory=dict, init=False, repr=False)
    _used: set[tuple[str, str]] = field(default_factory=set, init=False, repr=False)
    _refs: dict[tuple[str, str], set[str]] = field(default_factory=dict, init=False, repr=False)
    _kernels: dict[tuple[str, str], Any] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def specs(self) -> list[ToolSpec]:
        out = []
        for t in json.loads(SCHEMAS.read_text(encoding="utf-8")):
            effect = DEFAULT_EFFECTS.get(t["name"]) or Effect(t.get("effect") or "write")
            out.append(ToolSpec(name=t["name"], description=(t.get("description") or "")[:240],
                                parameters=t["parameters"], effect=effect, source="mcp:browser-pool",
                                effect_of=ARGUMENT_EFFECTS.get(t["name"])))
        return out

    # ── crochets (sous-classe Lokkal : nokker/tools/browser_sites.py) ──
    def _refuse_tenant(self, tenant: str) -> str | None:
        """Raison du refus pour ce locataire, ou None : le pool sert les testeurs, jamais l'interne."""
        if not tenant or tenant == INTERNAL_TENANT:
            return mt.pick(mt.BROWSER_BETA_ONLY)
        return None

    def _live_tenant(self, tenant: str) -> str | None:
        """Locataire sous lequel la vue en direct range la session (le testeur ; None = interne de Lokkal)."""
        return tenant

    def _observe_extra(self, session_id: str) -> dict[str, Any]:
        """Arguments de plus pour `live.observe` (pool : aucun ; sites-chrome : routes frame, écran, reprise)."""
        return {}

    # ── sessions ──
    def _http(self) -> Any:
        return self.client or httpx.Client(timeout=self.timeout_s)

    def _open(self, tenant: str) -> _Session | tuple[int, str]:
        r = self._http().post(self.base_url.rstrip("/") + "/sessions", json={"tenant_id": tenant},
                              headers={"Authorization": f"Bearer {self.api_token()}"})
        if r.status_code in (200, 201):
            d = r.json()
            return _Session(id=d["session_id"], mcp_url=d["mcp_url"], token=d["token"],
                            expires_at=_ts(d.get("expires_at")))
        retry = r.headers.get("Retry-After") or (r.json() if "json" in r.headers.get("content-type", "") else {}).get(
            "retry_after_s", 30)
        return r.status_code, str(retry)

    def _session_for(self, tenant: str, fresh: bool = False) -> _Session | tuple[int, str]:
        with self._lock:
            s = self._sessions.get(tenant)
            if s is not None and not fresh and time.time() < s.expires_at - 5:
                return s
            self._sessions.pop(tenant, None)
        opened = self._open(tenant)
        if isinstance(opened, _Session):
            with self._lock:
                self._sessions[tenant] = opened
        return opened

    def _mcp(self, s: _Session) -> Any:
        if s.io is None:
            headers = {"Authorization": f"Bearer {s.token}"}
            s.io = (self.mcp_factory or _McpCaller)(s.mcp_url, headers)
        return s.io

    # ── appels ──
    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        tenant = current_tenant()
        refused_tenant = self._refuse_tenant(tenant)
        if refused_tenant:
            return ToolResult(call_id="", name=name, is_error=True, content=refused_tenant)
        args = {k: v for k, v in arguments.items() if k != "lane"}  # sans effet dans le pool (contrat)
        key = (tenant, ctx.thread_id)
        lv = _live()
        lt = self._live_tenant(tenant)
        if lv is not None:  # passation en cours (docs/BROWSER-LIVE.md §4) : la main est à l'humain, aucun geste
            refused = lv.refuse(lt, ctx.thread_id, name)
            if refused:
                return ToolResult(call_id="", name=name, is_error=True, content=refused)
        asked = _asked_refs(name, args)
        if asked:
            with self._lock:
                known = self._refs.get(key, set())
            unknown = next((r for r in asked if r not in known), None)
            if unknown is not None:
                from nokker.qa.untrusted import inline

                return ToolResult(call_id="", name=name, is_error=True,
                                  content=refused_prefix() + mt.pick(mt.BROWSER_REF_REFUSED).format(ref=inline(unknown, 40)))
        for attempt in (0, 1):
            s = self._session_for(tenant, fresh=attempt == 1)
            if not isinstance(s, _Session):
                status, retry = s
                why = (mt.pick(mt.BROWSER_BUSY).format(retry=retry)
                       if status == 429 else mt.pick(mt.BROWSER_NO_START))
                return ToolResult(call_id="", name=name, is_error=True, content=why)
            out = self._mcp(s).call(name, args)
            text, is_error, code = out[0], out[1], out[2]
            extra = out[3] if len(out) > 3 and isinstance(out[3], dict) else {}
            if code in _EXPIRED and attempt == 0:
                continue  # session expirée : rouverte une fois, appel rejoué
            with self._lock:
                self._used.add((tenant, ctx.thread_id))
            if code is None:  # texte rendu par le navigateur (page, instantané) : contenu de tiers, clôturé (r44)
                from nokker.tools.result_fence import browser_refs, fence_browser_result

                refs = browser_refs(text)[1]
                with self._lock:
                    if name == "browser_find":  # lignes du même instantané : elles s'ajoutent
                        self._refs.setdefault(key, set()).update(refs)
                    elif refs:  # nouvel instantané : il remplace le précédent
                        self._refs[key] = set(refs)
                    elif name in _PAGE_CHANGE and not is_error:  # page changée sans instantané : plus aucune ref
                        self._refs.pop(key, None)
                # Valeurs des champs sensibles retirées avant le modèle (docs/BROWSER-LIVE.md §4), drapeau ou non
                from nokker.browserlive.mask import mask_values

                text = mask_values(text, lv.sensitive_refs(lt, ctx.thread_id) if lv is not None else ())
                text = fence_browser_result(name, args, text)
                note = self._observe_live(lv, tenant, ctx, name, args, out[0], is_error, extra, s.id)
                if note:
                    text = f"{text}\n{note}"
            return ToolResult(call_id="", name=name, content=text, is_error=is_error)
        if lv is not None:  # session perdue deux fois : la vue en direct se termine sur une erreur
            lv.end_thread(lt, ctx.thread_id, "error")
        return ToolResult(call_id="", name=name, is_error=True, content="session navigateur perdue : réessaie")

    def _observe_live(self, lv: Any, tenant: str, ctx: ToolContext, name: str, args: dict[str, Any], raw: str,
                      is_error: bool, extra: dict[str, Any], pool_session_id: str) -> str | None:
        """Vue navigateur en direct : action, frame, passation (docs/BROWSER-LIVE.md). Jamais bloquant."""
        if lv is None:
            return None
        if ctx.kernel is not None:
            with self._lock:
                self._kernels[(tenant, ctx.thread_id)] = ctx.kernel
        # Pool de la bêta (docs/BROWSER-POOL.md) : ni capture, ni `_meta`, ni écran aujourd'hui — `browser.action`
        # vient de ce que le noyau sait (outil, arguments, en-tête de l'instantané), sans frame ni ticket ; une capture
        # ou un `_meta` rendus un jour par le pod sont lus tels quels (même forme que sites-chrome).
        lt = self._live_tenant(tenant)
        try:
            note = lv.observe(lt, ctx.thread_id, ctx.sender, name, args, raw, is_error, extra,
                              pool_session_id=pool_session_id, **self._observe_extra(pool_session_id))
            live_s = lv.current(lt, ctx.thread_id)
            if live_s is not None and live_s.box_fn is None:  # browser_point : boîte demandée au navigateur
                live_s.box_fn = lambda ref, t=tenant: self._box(t, ref)
            return note
        except Exception:  # noqa: BLE001 — la vue en direct ne casse jamais un appel d'outil
            logger.warning("vue navigateur : observation de %s en échec", name, exc_info=True)
            return None

    def _box(self, tenant: str, ref: str) -> tuple[str, bool, dict[str, Any]]:
        """`browser_box {ref, scroll: true}` du navigateur (aucun clic ; capture fraîche) → (texte, erreur, extra)."""
        with self._lock:
            s = self._sessions.get(tenant)
        if s is None:
            return "session navigateur absente", True, {}
        out = self._mcp(s).call("browser_box", {"ref": ref, "scroll": True})
        return out[0], bool(out[1]), (out[3] if len(out) > 3 and isinstance(out[3], dict) else {})

    def turn_done(self, thread_id: str) -> None:
        tenant = current_tenant()
        lv = _live()
        if lv is not None:  # `browser.ended` TOUJOURS émis en fin de tour (docs/BROWSER-LIVE.md §2)
            with self._lock:
                kernel = self._kernels.pop((tenant, thread_id), None)
            cancelled = False
            try:
                cancelled = bool(kernel is not None and kernel.cancel_requested(thread_id))
            except Exception:  # noqa: BLE001
                cancelled = False
            try:
                lv.end_thread(self._live_tenant(tenant), thread_id, "cancelled" if cancelled else "done")
            except Exception:  # noqa: BLE001
                logger.warning("vue navigateur : fin de session en échec", exc_info=True)
        with self._lock:
            used = (tenant, thread_id) in self._used
            self._used.discard((tenant, thread_id))
            self._refs.pop((tenant, thread_id), None)  # browser_close : onglets rendus, références périmées
            s = self._sessions.get(tenant)
        if used and s is not None:
            try:
                self._mcp(s).call("browser_close", {})
            except Exception:  # noqa: BLE001 — fermeture des onglets au mieux
                logger.warning("pool : browser_close de fin de tour en échec", exc_info=True)


def _live() -> Any:
    from nokker.browserlive.live import live

    return live()


def _extra_of(result: Any) -> dict[str, Any]:
    """Images (WebP) et `_meta` d'un résultat MCP : la vue en direct en tire capture, action et blocage
    (docs/BROWSER-LIVE.md §7, forme de sites-chrome nokker/v3-frames)."""
    import base64

    images = []
    for block in getattr(result, "content", None) or []:
        if getattr(block, "type", None) == "image":
            try:
                bmeta = getattr(block, "meta", None)
                images.append({"mime": getattr(block, "mime_type", None) or getattr(block, "mimeType", ""),
                               "data": base64.b64decode(block.data),
                               "meta": bmeta if isinstance(bmeta, dict) else {}})
            except Exception:  # noqa: BLE001
                continue
    meta = getattr(result, "meta", None)
    return {"images": images, "meta": meta if isinstance(meta, dict) else {}}


class _McpCaller:
    """Appel MCP streamable-HTTP (SDK `mcp`, même client que `McpToolSource`) ; rend (texte, is_error, code JSON-RPC)."""

    def __init__(self, url: str, headers: dict[str, str]) -> None:
        from nokker.tools.mcp import _SyncMcp

        self._io = _SyncMcp(url, headers, 60.0)

    def call(self, name: str, args: dict[str, Any]) -> tuple[str, bool, int | None, dict[str, Any]]:
        from nokker.tools.mcp import text_of

        async def _call():
            session = await self._io.session_async()
            return await session.call_tool(name, args)

        try:
            result = self._io.run(_call())
        except Exception as e:  # noqa: BLE001 — erreur JSON-RPC du gestionnaire, ou réseau
            code = getattr(getattr(e, "error", None), "code", None)
            if code is None and ("404" in str(e) or "-32001" in str(e)):
                code = -32001
            from nokker.qa.untrusted import inline

            return f"navigateur : {inline(str(e), 500)}", True, code, {}  # message du noyau : une ligne, par inline()
        extra = _extra_of(result)
        # Capture d'un geste (`_meta.nokker_action` présent) : pour la vue en direct seulement, jamais dans le texte du
        # modèle (ni pixels ni « [ImageContent] »). Une capture demandée (browser_take_screenshot) garde sa trace.
        gesture = bool((extra.get("meta") or {}).get("nokker_action"))
        return text_of(result, skip_images=gesture), bool(getattr(result, "is_error", False)), None, extra


def _ts(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        from datetime import datetime

        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time() + 300


__all__ = ["BrowserPoolSource"]
