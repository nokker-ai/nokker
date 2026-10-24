"""Producteurs de bulles : résultat d'outil → bulle (docs/BUBBLES.md §2 « Qui crée les bulles »).

Table outil → kind :
  - `web_search`                                → `search` (state §2 : query, took_ms, engines, results)
  - `infra_gpu_info`, `infra_disk_*`, `infra_*_info`/`*_usage`/`*_status` en lecture → `monitor`
  - `session_send` → `notify` côté destinataire : produit par `hub.BubbleHub.send_message`, pas ici.

Aperçus (§5, chantier `nokker/previews/`, en parallèle) : le producteur `search` appelle
`Producers.request_previews(bubble)` après création — no-op par défaut, injectable (le service d'aperçus
remplit `results[i].preview` et publie `bubble.updated` via `BubbleHub.update_from_kernel`)."""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.bubbles.types import Bubble
from nokker.core.ports import ToolSpec
from nokker.core.turn import Effect, ToolCall, ToolResult

SEARCH_RESULTS_MAX = 10
_WEB_DATA = re.compile(r"<!-- nokker:web_search:data -->\s*(.*?)\s*<!-- /nokker:web_search:data -->", re.S)
_PERCENT = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
_MONITOR_NAMES = frozenset({"infra_gpu_info", "infra_disk_usage"})
_MONITOR_SUFFIXES = ("_usage", "_info", "_status", "_stats")


def _noop_previews(_bubble: Bubble) -> None:
    return None


@dataclass
class ProducedBubble:
    kind: str
    title: str
    state: dict[str, Any]
    status: str = "done"
    actions: list[dict[str, Any]] = field(default_factory=list)


def is_monitor_tool(name: str, spec: ToolSpec | None) -> bool:
    if name in _MONITOR_NAMES or name.startswith("infra_disk_"):
        return spec is None or spec.effect is Effect.READ
    if name.startswith("infra_") and name.endswith(_MONITOR_SUFFIXES):
        return spec is not None and spec.effect is Effect.READ
    return False


def search_state(arguments: dict[str, Any], content: str, took_ms: int | None = None) -> dict[str, Any] | None:
    m = _WEB_DATA.search(content or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    results = []
    for r in (data.get("results") or [])[:SEARCH_RESULTS_MAX]:
        if not isinstance(r, dict) or not r.get("url"):
            continue
        results.append({"url": r.get("url"), "title": r.get("title") or "", "snippet": r.get("snippet") or "",
                        "site": r.get("domain") or r.get("site") or ""})
    return {"query": data.get("query") or str(arguments.get("query", "")), "took_ms": took_ms,
            "engines": list(data.get("engines_queried") or []), "results": results}


def monitor_state(name: str, arguments: dict[str, Any], content: str) -> dict[str, Any]:
    host = arguments.get("host") or arguments.get("target") or ""
    path = arguments.get("path") or arguments.get("mount") or ""
    target = ":".join(str(x) for x in (host, path) if x) or name.removeprefix("infra_")
    metric = "gpu" if "gpu" in name else ("disk" if "disk" in name else name.removeprefix("infra_"))
    state: dict[str, Any] = {"target": target, "metric": metric, "value": None, "unit": None}
    parsed: Any = None
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        for k in ("value", "unit", "max"):
            if k in parsed:
                state[k] = parsed[k]
        if isinstance(parsed.get("items"), list):
            state["items"] = parsed["items"][:20]
    if state["value"] is None:
        m = _PERCENT.search(content or "")
        if m:
            state["value"] = float(m.group(1).replace(",", "."))
            state["unit"] = "%"
            state["max"] = 100
    if "items" not in state:
        lines = [ln.strip() for ln in (content or "").splitlines() if ln.strip()]
        if len(lines) > 1 or state["value"] is None:
            state["items"] = [{"label": ln[:120], "value": ""} for ln in lines[:10]]
    return state


@dataclass
class Producers:
    request_previews: Callable[[Bubble], None] = _noop_previews

    def produce(self, call: ToolCall, spec: ToolSpec | None, result: ToolResult,
                took_ms: int | None = None) -> ProducedBubble | None:
        if result.is_error:
            return None
        if call.name == "web_search":
            st = search_state(call.arguments, result.content, took_ms)
            if st is None:
                return None
            return ProducedBubble(kind="search", title=st["query"][:200], state=st)
        if is_monitor_tool(call.name, spec):
            st = monitor_state(call.name, call.arguments, result.content)
            title = f"{st['metric']} {st['target']}".strip()
            return ProducedBubble(kind="monitor", title=title[:200], state=st)
        return None

    def after_create(self, bubble: Bubble) -> None:
        if bubble.kind == "search":
            self.request_previews(bubble)


__all__ = ["ProducedBubble", "Producers", "is_monitor_tool", "monitor_state", "search_state"]
