"""`ToolRegistry` (noyau) : agrège des `ToolSource` (builtin, MCP…), sépare outils cœur et outils trouvés
à la demande (PLAN-P1 §2.2/§2.4). Les outils cœur (`core=True`) sont ceux du préfixe — au plus ≤ 8 au
total (budget §2.4) ; `index()` donne la liste courte (nom, description) des autres, pour le bloc du tour ;
leur schéma complet n'est chargé qu'après un `tool_find` (c'est `core/loop.py` qui gère ce chargement,
`loaded_tools` dans `ContextEngine.assemble`).
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.core import model_texts as mt
from nokker.core.loop import ESCALATE_TOOL, FIND_TOOL
from nokker.core.ports import ToolContext, ToolSource, ToolSpec
from nokker.core.turn import Effect, ToolResult

logger = logging.getLogger("nokker.tools.registry")

# Un serveur MCP en panne reste écarté jusqu'à la fin du tour (`turn_done`), ou au plus ce délai si la fin de tour ne
# vient pas : ensuite on retente (un tour sans cache à relire coûte un délai de connexion, pas plus).
SERVER_DOWN_RETRY_S = 60.0


def _mcp_source(src: Any) -> Any:
    """La source MCP sous les enveloppes d'une source (effets, lectures, filtres…), ou None si ce n'en est pas une."""
    from nokker.tools.mcp import McpToolSource
    from nokker.tools.mcp_stdio import StdioMcpToolSource

    for _ in range(8):
        if isinstance(src, (McpToolSource, StdioMcpToolSource)):
            return src
        src = getattr(src, "inner", None)
        if src is None:
            return None
    return None


def _mcp_server_name(src: Any) -> str | None:
    """Nom du serveur MCP sous les enveloppes d'une source, ou None si ce n'en est pas un."""
    base = _mcp_source(src)
    return str(base.name) if base is not None else None


def _short_reason(exc: BaseException) -> str:
    """Raison brève d'une panne, sans adresse ni secret : classe de l'exception et son premier mot-clé de lecture."""
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    text = re.sub(r"https?://\S+", "<adresse>", str(exc).splitlines()[0] if str(exc) else "")
    return f"{type(exc).__name__}: {text[:80]}" if text else type(exc).__name__


@dataclass
class StaticToolSource:
    """Un outil « local », implémenté en Python pur (pas de serveur MCP). Utile pour les tests et pour
    les quelques outils cœur qui n'ont pas de sens côté MCP (`clarify`, `recall_context`)."""

    entries: list[tuple[ToolSpec, Callable[[dict[str, Any], ToolContext], ToolResult]]] = field(
        default_factory=list)

    def specs(self) -> list[ToolSpec]:
        return [spec for spec, _ in self.entries]

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        for spec, fn in self.entries:
            if spec.name == name:
                return fn(arguments, ctx)
        return unknown_tool_result(name)


def unknown_tool_result(name: str) -> ToolResult:
    """Résultat d'un appel à un nom hors catalogue — texte FIXE (appris tel quel à l'entraînement, run6) : il renvoie
    vers `tool_find` au lieu d'inviter à réessayer le même nom inventé (run4 : 30 noms hors catalogue)."""
    return ToolResult(call_id="", name=name, is_error=True,
                      content=mt.pick(mt.UNKNOWN_TOOL).format(name=name))


def _native_stub(name: str, message: str, param: str, param_desc: str
                 ) -> tuple[ToolSpec, Callable[[dict[str, Any], ToolContext], ToolResult]]:
    """`tool_find`/`escalate_to_human` sont interceptés par `core/loop.py` AVANT d'atteindre le registre
    (`Kernel._tool_step`) — ce `call()` ne doit donc jamais s'exécuter en pratique. Il existe pour que le
    nom ait un schéma minimal déclarable au préfixe et pour échouer clairement s'il était atteint quand
    même (p. ex. un appel direct au registre dans un test)."""
    # Le paramètre déclaré est celui que core/loop.py lit (`query` / `reason`) : schéma = contrat d'entraînement
    # de Nina (lot 2c, nokker_tasks.py), à garder identique des deux côtés (cf docs/CONTEXT-FORMAT.md).
    spec = ToolSpec(name=name, description=message,
                    parameters={"type": "object", "properties": {param: {"type": "string", "description": param_desc}},
                                "required": [param]},
                     effect=Effect.READ, core=True, source="builtin")

    def _fail(_args: dict[str, Any], _ctx: ToolContext) -> ToolResult:
        return ToolResult(call_id="", name=name, content=mt.pick(mt.NATIVE_UNREACHED).format(name=name), is_error=True)

    return spec, _fail


def _clarify(args: dict[str, Any], _ctx: ToolContext) -> ToolResult:
    question = str(args.get("question", "")).strip() or mt.pick(mt.EMPTY_QUESTION)
    return ToolResult(call_id="", name="clarify", content=question)


def builtin_core_source(clarify_options: bool = False) -> StaticToolSource:
    """Les outils cœur qui ne viennent pas de CortHeXis (`memory_search`/`memory_get` sont exposés par
    `nokker.memory.corthexis.CorthexisMemory`, pas ici) — le préfixe de l'instance interne, §Outils cœur.

    `recall_context` n'est PAS ici (décision produit, P1-S1, 17.10.2026) : sans réservoir réel il ne pouvait
    que répondre « indisponible », ce qui invite le modèle à réessayer. Il est exposé par
    `nokker.context.tools.RecallContextSource`, ajoutée seulement quand `context_enabled` (ContextManager :
    réservoir, épisodes, résumés de jour réels — api/server.py). Sans `context_enabled` (ContextEngineV0),
    un appel tombe sur « outil inconnu ».

    `clarify_options` (drapeau `host_clarify_options`, éteint par défaut, docs/NOKKAN-HOST-API.md §1) : `clarify` prend
    en plus une liste d'options (`nokker/tools/clarify.py`). Éteint : le schéma et la fonction ci-dessous, inchangés."""
    clarify_spec = ToolSpec(name="clarify", description="Pose une seule question fermée quand il manque "
                             "une information qui change l'action.",
                             parameters={"type": "object", "properties": {
                                 "question": {"type": "string"}}, "required": ["question"]},
                             effect=Effect.READ, core=True, source="builtin")
    find_spec, find_fn = _native_stub(FIND_TOOL, "Trouve un outil métier qui n'est pas encore chargé ; "
                                       "son schéma est chargé au tour suivant.",
                                       "query", "ce que l'outil doit faire (mots-clés)")
    esc_spec, esc_fn = _native_stub(ESCALATE_TOOL, "Inscrit la question dans la file de décision humaine.",
                                    "reason", "ce que l'humain doit trancher, exactement")
    clarify_fn = _clarify
    if clarify_options:
        from nokker.tools.clarify import options_entry

        clarify_spec, clarify_fn = options_entry()
    return StaticToolSource(entries=[
        (clarify_spec, clarify_fn), (find_spec, find_fn), (esc_spec, esc_fn),
    ])


@dataclass
class Registry:
    """Implémentation de `core.ports.ToolRegistry`. Agrège N `ToolSource` ; le premier qui déclare un nom
    gagne (collision journalisée par l'appelant s'il y en a — v0 ne détecte pas les doublons)."""

    sources: list[ToolSource] = field(default_factory=list)
    # Variante de textes d'outils APPROUVÉE sur carte (docs/SELF-IMPROVE.md, `nokker/improve/overlay.py`) :
    # {nom: {description, parameters: {champ: description}}}. None (défaut, drapeau `self_improve` éteint) : les specs
    # des sources telles quelles, rien n'est recopié.
    overlay: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self._cache: list[ToolSpec] | None = None
        # Sources MCP injoignables pour CE tour : id(source) -> (nom du serveur, raison brève, instant). Leurs outils
        # sont retirés, les autres sources continuent ; `turn_done` vide : au tour suivant on retente.
        self._down: dict[int, tuple[str, str, float]] = {}
        # Sondages RÉUSSIS des serveurs requis (`required_unavailable`) : id(source) -> instant. Jamais un échec.
        self._probe_ok: dict[int, float] = {}

    def unavailable(self) -> list[tuple[str, str]]:
        """(serveur, raison brève) des serveurs MCP écartés ce tour. Vide hors panne."""
        return sorted({(name, why) for name, why, _t in self._down.values()})

    def mcp_servers(self) -> list[str]:
        """Noms des serveurs MCP déclarés (joignables ou non), triés."""
        return sorted({n for n in (_mcp_server_name(s) for s in self.sources) if n})

    def required_unavailable(self, names: Any, retry_s: float = 0.0, cache_s: float = 0.0) -> list[tuple[str, str]]:
        """(serveur, raison brève) de ceux de `names` qui ne répondent pas MAINTENANT. Chaque serveur est réellement sondé (liste
        d'outils relue, pas le cache) ; un échec est retenté UNE fois, au plus `retry_s` secondes (0 = pas de nouvel essai). Les
        serveurs écartés restent écartés pour ce tour (même état que `unavailable()`). Un nom sans source dans ce registre est
        indisponible, raison `not_registered`. `cache_s` : un sondage RÉUSSI de moins de `cache_s` secondes n'est pas refait (rafales
        de tours) ; un échec n'est jamais gardé, il est toujours re-sondé."""
        want = set(names)
        out: dict[str, str] = {}
        found: set[str] = set()
        now = time.monotonic()
        for src in self.sources:
            name = _mcp_server_name(src)
            if name not in want:
                continue
            found.add(name)
            seen_ok = self._probe_ok.get(id(src))
            if cache_s > 0 and seen_ok is not None and now - seen_ok < cache_s and id(src) not in self._down:
                continue
            why = self._probe(src, name)
            if why is not None and retry_s > 0:
                why = self._probe_bounded(src, name, retry_s, why)  # None : rétabli à la nouvelle tentative
            if why is None:
                self._probe_ok[id(src)] = time.monotonic()
            else:
                self._probe_ok.pop(id(src), None)
                out[name] = out.get(name) or why
        for name in want - found:
            out[name] = "not_registered"
        return sorted(out.items())

    def _probe(self, src: Any, name: str) -> str | None:
        """Un sondage : cache de la source oublié, liste relue. Raison si le serveur ne répond pas, sinon None."""
        self._down.pop(id(src), None)
        base = _mcp_source(src)
        if base is not None:
            base.refresh()
        self._cache = None
        self._specs_of(src)
        down = self._down.get(id(src))
        return down[1] if down is not None else None

    def _probe_bounded(self, src: Any, name: str, limit_s: float, first_why: str) -> str | None:
        """Le sondage de la nouvelle tentative, dans un fil qui ne retient jamais le tour plus de `limit_s` : au-delà, le serveur
        reste déclaré injoignable (raison du premier essai). Rend la raison, ou None si le serveur répond."""
        import contextvars
        import threading

        prior = self._down.get(id(src))
        box: list[str | None] = []
        worker = threading.Thread(target=contextvars.copy_context().run, args=(lambda: box.append(self._probe(src, name)),),
                                  name=f"mcp-required-{name[:24]}", daemon=True)
        worker.start()
        worker.join(limit_s)
        if worker.is_alive():
            if prior is not None:
                self._down[id(src)] = prior
            return first_why
        return box[0] if box else first_why

    def _mark_down(self, src: Any, name: str, exc: BaseException) -> None:
        why = _short_reason(exc)
        if id(src) not in self._down:
            logger.warning("mcp(%s): serveur indisponible, ses outils sont retirés pour ce tour (%s)", name, why)
        self._down[id(src)] = (name, why, time.monotonic())
        self._probe_ok.pop(id(src), None)
        self._cache = None

    def _specs_of(self, src: Any) -> list[ToolSpec]:
        """`src.specs()`, sauf pour un serveur MCP qui lève (injoignable) : il n'apporte aucun outil ce tour-ci."""
        if id(src) in self._down:
            return []
        try:
            return src.specs()
        except Exception as e:  # filtré plus bas : seul un serveur MCP est écarté
            name = _mcp_server_name(src)
            if name is None:
                raise
            self._mark_down(src, name, e)
            return []

    def refresh(self) -> None:
        """Invalide le cache (nouveaux outils MCP, ex. reconnexion) — à appeler périodiquement si besoin."""
        self._cache = None
        self._down.clear()
        self._probe_ok.clear()
        for src in self.sources:
            refresh = getattr(src, "refresh", None)
            if callable(refresh):
                refresh()

    def turn_done(self, thread_id: str) -> None:
        """Fin de tour : relaie aux sources qui le déclarent (ex. fermeture des onglets du navigateur partagé). Les
        serveurs MCP écartés pendant le tour seront retentés au suivant."""
        if self._down:
            self._down.clear()
            self._cache = None
        for src in self.sources:
            hook = getattr(src, "turn_done", None)
            if callable(hook):
                try:
                    hook(thread_id)
                except Exception:  # noqa: BLE001 — une source en panne ne casse jamais la fin d'un tour
                    logger.warning("turn_done de %s en échec", type(src).__name__, exc_info=True)

    def _all(self) -> list[ToolSpec]:
        if self._down and time.monotonic() - min(t for _n, _w, t in self._down.values()) > SERVER_DOWN_RETRY_S:
            self._down.clear()  # fin de tour jamais venue : on retente
            self._cache = None
        if self._cache is None:
            specs: list[ToolSpec] = []
            seen: set[str] = set()
            for src in self.sources:
                for spec in self._specs_of(src):
                    if spec.name in seen:
                        continue
                    seen.add(spec.name)
                    specs.append(spec)
            if "jobs_apply" in seen and "mail_send" in seen:
                # Pack Emploi (docs/JOBS-PACK.md §5.11) : une candidature part par jobs_apply, jamais par mail_send.
                from dataclasses import replace as _replace

                from nokker.jobs.texts_v2 import MAIL_SEND_SUFFIX

                specs = [_replace(s, description=f"{s.description} {MAIL_SEND_SUFFIX}")
                         if s.name == "mail_send" and MAIL_SEND_SUFFIX not in s.description else s for s in specs]
            if self.overlay:
                from nokker.improve.overlay import apply_tool_overlay

                specs = apply_tool_overlay(specs, self.overlay)
            self._cache = specs
        return self._cache

    def core(self) -> list[ToolSpec]:
        return [s for s in self._all() if s.core]

    def spec(self, name: str) -> ToolSpec | None:
        return next((s for s in self._all() if s.name == name), None)

    def find(self, query: str, limit: int = 5) -> list[ToolSpec]:
        words = [w for w in query.lower().split() if w]
        scored: list[tuple[int, ToolSpec]] = []
        for spec in self._all():
            if spec.core:
                continue
            haystack = f"{spec.name} {spec.description}".lower()
            score = sum(1 for w in words if w in haystack)
            if score > 0 or not words:
                scored.append((score, spec))
        scored.sort(key=lambda t: -t[0])
        return [s for _, s in scored[:limit]]

    def index(self) -> list[tuple[str, str]]:
        return [(s.name, s.description) for s in self._all() if not s.core]

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        for src in self.sources:
            specs = self._specs_of(src)
            if any(s.name == name for s in specs):
                if name == SCREENSHOT_TOOL and any(s.name == EVALUATE_TOOL for s in specs):
                    return _masked_screenshot(src, arguments, ctx)
                try:
                    return src.call(name, arguments, ctx)
                except Exception as e:  # un serveur MCP qui lève à l'appel : retiré pour ce tour
                    server = _mcp_server_name(src)
                    if server is None:
                        raise
                    self._mark_down(src, server, e)
                    from nokker.tools.select import unavailable_line

                    return ToolResult(call_id="", name=name, content=unavailable_line([server]), is_error=True)
        return unknown_tool_result(name)

    def close(self) -> None:
        for src in self.sources:
            close = getattr(src, "close", None)
            if callable(close):
                close()


SCREENSHOT_TOOL = "browser_take_screenshot"
EVALUATE_TOOL = "browser_evaluate"


def _masked_screenshot(src: Any, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Capture d'écran sans données de carte (PAYMENTS §2 bis) : champs de carte et iframes de prestataires de paiement
    floutés le temps de la capture, sur l'onglet de la capture (même `lane`). Si le masque ne peut pas se poser, pas de
    capture : le texte de l'instantané reste disponible."""
    from nokker.payments.cardmask import MASK_SCRIPT, UNMASK_SCRIPT, screenshot_refused_text

    lane = {"lane": arguments["lane"]} if "lane" in arguments else {}
    try:
        masked = src.call(EVALUATE_TOOL, {"function": MASK_SCRIPT, **lane}, ctx)
    except Exception:  # noqa: BLE001 — navigateur injoignable : la capture échouera de même
        masked = None
    if masked is None or getattr(masked, "is_error", True):
        return ToolResult(call_id="", name=SCREENSHOT_TOOL, is_error=True,
                          content=screenshot_refused_text())
    try:
        return src.call(SCREENSHOT_TOOL, arguments, ctx)
    finally:
        try:
            src.call(EVALUATE_TOOL, {"function": UNMASK_SCRIPT, **lane}, ctx)
        except Exception:  # noqa: BLE001 — le masque reste posé : sans effet sur la page, seulement visuel
            pass


__all__ = ["Registry", "StaticToolSource", "builtin_core_source"]
