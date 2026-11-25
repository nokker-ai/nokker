"""Outils des pages de session (docs/BUBBLES.md §10) : `page_create`, et l'enveloppe qui prend une nouvelle version
quand un `write_file`/`patch` touche une page du fil.

Le résultat d'outil porte un bloc `<!-- nokker:page -->{state}<!-- /nokker:page -->` que lit le producteur de bulles
(`nokker/bubbles/producers.py` → bulle `artifact`), comme `web_search` porte ses données à la bulle `search`.
"""
from __future__ import annotations

import json
import logging
import posixpath
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nokker.core import model_texts as mt
from nokker.core.ports import ToolContext, ToolSource, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.pages.store import PageError, PageMeta, PageStore
from nokker.tools.checkpoints import WORKSPACE_MOUNT, _written_paths, host_path

logger = logging.getLogger("nokker.pages.tools")

SOURCE = "builtin:pages"
PAGE_TOOLS = ("page_create", "design_check")
_DATA = re.compile(r"<!-- nokker:page -->\s*(\{.*?\})\s*<!-- /nokker:page -->", re.S)
_SPEC = ToolSpec(
    name="page_create",
    description=("Montre une page HTML du dossier de travail dans le chat (bulle « artefact », aperçu vivant) : un "
                 "fichier .html autonome (CSS, JS, images en ligne ou en data:, liens relatifs seulement : la page est "
                 "servie depuis une autre origine), qui reste privé. Ses modifications suivantes mettent l'aperçu à "
                 "jour d'elles-mêmes."),
    parameters={"type": "object", "required": ["path"], "properties": {
        "path": {"type": "string", "description": "le fichier .html, relatif au dossier de travail"},
        "title": {"type": "string", "description": "titre court de la bulle"}}},
    effect=Effect.WRITE, core=True, source=SOURCE)
# Lot V8 : plancher de design d'une page (nokker/design/floor.py). Texte vu par le modèle : relu avant le gel.
_CHECK_SPEC = ToolSpec(
    name="design_check",
    description=("Vérifie une page HTML du dossier de travail contre le plancher de design (jetons de couleur, focus "
                 "visible, mouvement réduit, contraste, étiquettes, ressources externes…) sans la modifier. Rend les "
                 "écarts « plancher » (à corriger) et « réflexe » (à corriger ou à justifier). Un résultat propre "
                 "n'est pas une preuve de qualité : relis aussi le code de la page, et ce qui ne se voit qu'au rendu, "
                 "demande-le à la personne."),
    parameters={"type": "object", "required": ["path"], "properties": {
        "path": {"type": "string", "description": "le fichier .html, relatif au dossier de travail"}}},
    effect=Effect.READ, core=False, source=SOURCE)


def page_block(meta: PageMeta) -> str:
    return f"<!-- nokker:page -->{json.dumps(meta.state(), ensure_ascii=False)}<!-- /nokker:page -->"


def page_states(content: str) -> list[dict[str, Any]]:
    out = []
    for m in _DATA.finditer(content or ""):
        try:
            out.append(json.loads(m.group(1)))
        except ValueError:
            continue
    return out


def _relative(path: str, mount: str) -> str:
    """Chemin d'une page tel qu'on le garde : relatif au dossier de travail, quelle que soit la forme donnée."""
    p = posixpath.normpath(path.strip())
    if p == mount or p.startswith(mount + "/"):
        p = p[len(mount):].lstrip("/") or "."
    return p


class PageToolSource:
    def __init__(self, store: PageStore, root_of: Callable[[str], Path | None], mount: str = WORKSPACE_MOUNT,
                 design_check: bool = False) -> None:
        self.store, self.root_of, self.mount = store, root_of, mount
        self.design_check = design_check  # `local.design_check` (V8), éteint par défaut

    def specs(self) -> list[ToolSpec]:
        return [_SPEC, _CHECK_SPEC] if self.design_check else [_SPEC]

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def out(text: str, error: bool = False) -> ToolResult:
            return ToolResult(call_id="", name=name, content=text, is_error=error)

        if name not in PAGE_TOOLS or (name == "design_check" and not self.design_check):
            return out(mt.pick(mt.UNKNOWN_TOOL_BARE).format(name=name), True)
        root = self.root_of(ctx.thread_id)
        if root is None:
            return out(mt.pick(mt.NO_WORKSPACE), True)
        path = str(arguments.get("path") or "").strip()
        if not path:
            return out(mt.pick(mt.MISSING_ARG).format(name="path"), True)
        rel = _relative(path, self.mount)
        full = Path(host_path(Path(root), rel, self.mount)).resolve()
        if full != Path(root).resolve() and Path(root).resolve() not in full.parents:
            return out(mt.pick(mt.PAGE_OUTSIDE_ROOT).format(path=repr(path)), True)
        if not full.is_file():
            return out(mt.pick(mt.PAGE_FILE_NOT_FOUND).format(path=path), True)
        if name == "design_check":
            from nokker.design.floor import MAX_BYTES, check, report

            if full.stat().st_size > MAX_BYTES:
                return out(mt.pick(mt.PAGE_TOO_BIG).format(path=rel), True)
            return out(report(check(full.read_text(encoding="utf-8", errors="replace"))))
        try:
            meta = self.store.create(ctx.thread_id, rel, str(arguments.get("title") or ""), full.read_bytes())
        except PageError as e:
            return out(f"REFUSÉ : {e}", True)
        return out(mt.pick(mt.PAGE_PUBLISHED).format(page_id=meta.page_id, title=meta.title, version=meta.version, rel=rel)
                   + f"\n{page_block(meta)}")


class PagingToolSource:
    """Enveloppe d'un `ToolSource` de code : après un `write_file`/`patch` réussi sur une page du fil, nouvelle
    version et bloc de page ajouté au résultat (le producteur met la bulle à jour). Un échec ne casse jamais l'appel."""

    def __init__(self, inner: ToolSource, store: PageStore, root_of: Callable[[str], Path | None],
                 mount: str | None = None) -> None:
        self.inner, self.store, self.root_of = inner, store, root_of
        self.mount = mount or getattr(inner, "mount", WORKSPACE_MOUNT)

    def specs(self) -> list[ToolSpec]:
        return self.inner.specs()

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        result = self.inner.call(name, arguments, ctx)
        if name not in ("write_file", "patch") or result.is_error:
            return result
        try:
            blocks = []
            root = self.root_of(ctx.thread_id)
            for p in _written_paths(name, arguments):
                rel = _relative(p, self.mount)
                meta = self.store.by_path(ctx.thread_id, rel)
                if meta is None or root is None:
                    continue
                full = Path(host_path(Path(root), rel, self.mount))
                if full.is_file():
                    blocks.append(page_block(self.store.snapshot(meta.page_id, full.read_bytes())))
            if blocks:
                result.content = f"{result.content}\n" + "\n".join(blocks)
        except Exception:  # noqa: BLE001
            logger.warning("version de page après %s en échec", name, exc_info=True)
        return result

    def __getattr__(self, attr: str) -> Any:  # turn_done, close, mount… : ceux de la source enveloppée
        return getattr(self.inner, attr)


__all__ = ["PAGE_TOOLS", "PageToolSource", "PagingToolSource", "page_block", "page_states"]
