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

from nokker.core.ports import ToolContext, ToolSource, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.pages.store import PageError, PageMeta, PageStore
from nokker.tools.checkpoints import WORKSPACE_MOUNT, _written_paths, host_path

logger = logging.getLogger("nokker.pages.tools")

SOURCE = "builtin:pages"
PAGE_TOOLS = ("page_create",)
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
    def __init__(self, store: PageStore, root_of: Callable[[str], Path | None], mount: str = WORKSPACE_MOUNT) -> None:
        self.store, self.root_of, self.mount = store, root_of, mount

    def specs(self) -> list[ToolSpec]:
        return [_SPEC]

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def out(text: str, error: bool = False) -> ToolResult:
            return ToolResult(call_id="", name=name, content=text, is_error=error)

        if name != "page_create":
            return out(f"outil inconnu : {name}", True)
        root = self.root_of(ctx.thread_id)
        if root is None:
            return out("aucun dossier de travail pour ce fil", True)
        path = str(arguments.get("path") or "").strip()
        if not path:
            return out("argument manquant : path", True)
        rel = _relative(path, self.mount)
        full = Path(host_path(Path(root), rel, self.mount)).resolve()
        if full != Path(root).resolve() and Path(root).resolve() not in full.parents:
            return out(f"REFUSÉ : {path!r} est hors du dossier de travail", True)
        if not full.is_file():
            return out(f"fichier introuvable : {path} (écris-le d'abord avec write_file)", True)
        try:
            meta = self.store.create(ctx.thread_id, rel, str(arguments.get("title") or ""), full.read_bytes())
        except PageError as e:
            return out(f"REFUSÉ : {e}", True)
        return out(f"page {meta.page_id} « {meta.title} », version {meta.version}, privée ; elle est affichée dans "
                   f"le chat et suit les modifications de {rel}.\n{page_block(meta)}")


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
