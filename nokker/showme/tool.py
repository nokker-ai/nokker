"""Mode « montre-moi » (docs/SHOW-ME.md) : l'outil `point_at` pose un REPÈRE au lieu d'agir.

- Effet `READ` : rien n'est cliqué, saisi ni envoyé ; aucune carte. Le seul appel fait pour le compte du modèle est
  `browser_find` (lecture) pour vérifier qu'une référence `eN` existe sur la page courante et lire son libellé.
- Cibles : `ref` (élément du dernier instantané du navigateur du fil), `ui` (élément de l'interface Nokker, liste
  fermée `texts.UI_TARGETS`), `image` (n-ième image jointe AU FIL, la plus récente d'abord, point x/y en %).
- Toute valeur d'origine non fiable (libellé lu sur la page, nom de fichier, légende écrite par le modèle) passe par
  `qa.untrusted.inline` : sur une ligne, sans balise ni crochet, bornée.
- L'état résolu est gardé pour le fil jusqu'à ce que le hub le prenne (`take`) et en fasse la bulle `pointer`
  (une seule ouverte par fil, mise à jour à chaque étape), publiée par `bubble.created`/`bubble.updated` sur
  `/v1/events`.
"""
from __future__ import annotations

import logging
import math
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.payments.detect import refs_of
from nokker.qa.untrusted import inline
from nokker.showme import texts as T

logger = logging.getLogger("nokker.showme")

_REF = re.compile(r"e\d{1,6}")
CAPTION_MAX = 160
LABEL_MAX = 80  # libellé lu sur la page (non fiable) : coupé court, dans le résultat ET dans la bulle
NAME_MAX = 80
STEPS_MAX = 20
IMAGES_LOOKBACK = 50  # tours relus pour retrouver une image du fil


def _parameters() -> dict[str, Any]:
    p = T.POINT_PARAMS
    return {
        "type": "object",
        "properties": {
            "caption": {"type": "string", "description": p["caption"]},
            "ref": {"type": "string", "description": p["ref"]},
            "ui": {"type": "string", "enum": list(T.UI_TARGETS), "description": p["ui"]},
            "image": {"type": "integer", "minimum": 1, "description": p["image"]},
            "x": {"type": "number", "description": p["x"]},
            "y": {"type": "number", "description": p["y"]},
            "step": {"type": "integer", "minimum": 1, "description": p["step"]},
            "steps": {"type": "integer", "minimum": 1, "description": p["steps"]},
        },
        "required": ["caption"],
    }


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f  # NaN et infini refusés


def _int(v: Any) -> int | None:
    f = _num(v)
    return int(f) if f is not None and f == int(f) else None


def _fmt(v: float) -> str:
    return f"{round(v, 1):g}"


def _refused(text: str) -> ToolResult:
    return ToolResult(call_id="", name=T.POINT_TOOL, content=text, is_error=True)


@dataclass
class ShowMeSource:
    """Source de l'outil `point_at` (drapeau `show_me`). `scrub` : caviardage du noyau (`scrub_output`), posé au
    câblage ; il s'applique à tout ce qui part dans la bulle."""

    scrub: Any = None
    source_name: str = "builtin:show_me"
    _pending: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def specs(self) -> list[ToolSpec]:
        return [ToolSpec(name=T.POINT_TOOL, description=T.POINT_DESCRIPTION, parameters=_parameters(),
                         effect=Effect.READ, source=self.source_name)]

    def take(self, thread_id: str) -> dict[str, Any] | None:
        """État du dernier repère posé dans ce fil (une fois) : lu par le hub juste après l'appel."""
        with self._lock:
            return self._pending.pop(thread_id, None)

    def _clean(self, value: Any, limit: int) -> str:
        text = inline(value, limit)
        return self.scrub(text) if self.scrub is not None else text

    # ── appel ──
    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if name != T.POINT_TOOL:
            return ToolResult(call_id="", name=name, content=f"outil inconnu : {name}", is_error=True)
        args = arguments or {}
        caption = self._clean(args.get("caption") or "", CAPTION_MAX).strip()
        if not caption:
            return _refused(T.NO_CAPTION)
        given = [k for k in ("ref", "ui", "image") if args.get(k) not in (None, "")]
        if len(given) != 1:
            return _refused(T.NO_TARGET)
        step, steps = args.get("step"), args.get("steps")
        step_i = _int(step) if step is not None else None
        steps_i = _int(steps) if steps is not None else None
        if (step is not None and (step_i is None or not 1 <= step_i <= STEPS_MAX)) or (
                steps is not None and (steps_i is None or not 1 <= steps_i <= STEPS_MAX)) or (
                step_i is not None and steps_i is not None and step_i > steps_i) or (
                steps_i is not None and step_i is None):
            return _refused(T.BAD_STEP)
        kind = given[0]
        if kind == "ref":
            target, what = self._ref_target(str(args["ref"]).strip(), ctx)
        elif kind == "ui":
            target, what = self._ui_target(str(args["ui"]).strip())
        else:
            target, what = self._image_target(args, ctx)
        if target is None:
            return _refused(what)
        text = T.POINTED.format(what=what, caption=caption)
        if step_i is not None:
            text += T.STEP.format(step=step_i, steps=steps_i) if steps_i else T.STEP_OPEN.format(step=step_i)
        title = (T.TITLE_STEP.format(step=step_i, steps=steps_i) if steps_i
                 else T.TITLE_STEP_OPEN.format(step=step_i) if step_i else T.TITLE)
        state = {"target": target, "caption": caption, "step": step_i, "steps": steps_i, "title": title,
                 "waiting": False, "answer": None}
        with self._lock:
            self._pending[ctx.thread_id] = state
        return ToolResult(call_id="", name=T.POINT_TOOL, content=text)

    # ── cibles ──
    def _ref_target(self, ref: str, ctx: ToolContext) -> tuple[dict[str, Any] | None, str]:
        if not _REF.fullmatch(ref):
            return None, T.BAD_REF.format(ref=inline(ref, 24))
        tools = getattr(getattr(ctx, "kernel", None), "tools", None)
        if tools is None or tools.spec("browser_find") is None:
            return None, T.NO_BROWSER
        try:
            res = tools.call("browser_find", {"query": rf"\[ref={ref}\]", "regex": True, "limit": 1}, ctx)
        except Exception:
            logger.warning("point_at : browser_find en échec", exc_info=True)
            return None, T.STALE_REF.format(ref=ref)
        label = refs_of(res.content if isinstance(res.content, str) else "").get(ref)
        if getattr(res, "is_error", False) or label is None:
            return None, T.STALE_REF.format(ref=ref)
        label = self._clean(label, LABEL_MAX)
        return {"kind": "browser", "ref": ref, "label": label}, T.WHAT_REF.format(label=label, ref=ref)

    @staticmethod
    def _ui_target(ui: str) -> tuple[dict[str, Any] | None, str]:
        if ui not in T.UI_TARGETS:
            return None, T.BAD_UI.format(ui=inline(ui, 40))
        return {"kind": "ui", "ui": ui}, T.WHAT_UI.format(ui=ui)

    def _image_target(self, args: dict[str, Any], ctx: ToolContext) -> tuple[dict[str, Any] | None, str]:
        n = _int(args.get("image"))
        x, y = _num(args.get("x")), _num(args.get("y"))
        if x is None or y is None or not (0 <= x <= 100 and 0 <= y <= 100):
            return None, T.BAD_POINT
        if n is None or n < 1:
            return None, T.NO_IMAGE.format(n=inline(args.get("image"), 12))
        images = thread_images(ctx)
        if n > len(images):
            return None, T.NO_IMAGE.format(n=n)
        img = images[n - 1]
        x, y = round(x, 1), round(y, 1)
        return ({"kind": "image", "sha256": img.sha256, "media_type": img.media_type,
                 "name": self._clean(img.name, NAME_MAX), "x": x, "y": y},
                T.WHAT_IMAGE.format(n=n, x=_fmt(x), y=_fmt(y)))


def thread_images(ctx: ToolContext) -> list[Any]:
    """Images jointes AU FIL de l'appel, la plus récente d'abord (tour courant, puis l'historique de CE fil seulement :
    jamais celles d'un autre fil ni d'un autre locataire)."""
    out: list[Any] = []
    seen: set[str] = set()

    def add(imgs: Any) -> None:
        for img in reversed(list(imgs or [])):
            sha = getattr(img, "sha256", "")
            if sha and sha not in seen:
                seen.add(sha)
                out.append(img)

    turn = getattr(ctx, "turn", None)
    if turn is not None and getattr(turn, "thread_id", ctx.thread_id) == ctx.thread_id:
        add(getattr(turn, "images", None))
    store = getattr(getattr(ctx, "kernel", None), "store", None)
    if store is not None:
        try:
            history = store.history(ctx.thread_id, limit=IMAGES_LOOKBACK)
        except Exception:
            logger.warning("point_at : historique illisible", exc_info=True)
            history = []
        for t in reversed(history):
            if getattr(t, "thread_id", ctx.thread_id) == ctx.thread_id and t is not turn:
                add(getattr(t, "images", None))
    return out


__all__ = ["ShowMeSource", "thread_images"]
