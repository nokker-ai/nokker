"""`browser_point` (lot V3, docs/BROWSER-LIVE.md §8) : Nina montre du doigt ce dont elle parle.

Outil READ, aucun geste sur la page : le modèle désigne un élément du DERNIER instantané (`ref`) et dit pourquoi
(`why`, ≤ 120 car.) ; le noyau appelle `browser_box {ref, scroll: true}` du navigateur (élément amené dans la vue,
capture FRAÎCHE, rien n'est cliqué : `_meta.nokker_box = {ref, x, y, w, h, in_view, frame_seq}`), publie la capture en
`browser.frame` (avec `frame_seq`), puis `browser.point {session_id, seq: frame_seq, ts, box, label, why}`. Refusé : cible
sensible, aucune frame non sensible, passation en cours, référence inconnue, boîte introuvable.
"""
from __future__ import annotations

from typing import Any

from nokker.browserlive import texts as T
from nokker.browserlive.live import live
from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.tenancy.context import current_tenant_or_none


class BrowserPointSource:
    """Source d'un seul outil, `browser_point` ; vit à côté de la source du navigateur (pool, sites-chrome)."""

    def specs(self) -> list[ToolSpec]:
        return [ToolSpec(name=T.POINT_TOOL, description=T.POINT_DESCRIPTION, effect=Effect.READ, source="builtin",
                         fresh=True, parameters={
                             "type": "object",
                             "properties": {"ref": {"type": "string", "description": T.POINT_REF},
                                            "why": {"type": "string", "maxLength": 120, "description": T.POINT_WHY}},
                             "required": ["ref", "why"]})]

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        ref = str((arguments or {}).get("ref") or "").strip()
        why = str((arguments or {}).get("why") or "").strip()
        lv = live()
        if name != T.POINT_TOOL or not ref or not why:
            return ToolResult(call_id="", name=name, is_error=True, content=T.POINT_BAD_ARGS)
        if lv is None:
            return ToolResult(call_id="", name=name, is_error=True, content=T.POINT_NO_SESSION)
        ok, text = lv.point(current_tenant_or_none(), ctx.thread_id, ref, why)
        return ToolResult(call_id="", name=name, is_error=not ok, content=text)

    def turn_done(self, thread_id: str) -> None:
        return None


__all__ = ["BrowserPointSource"]
