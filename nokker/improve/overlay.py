"""Application des variantes (docs/SELF-IMPROVE.md §6) : textes d'outils, seuils du routeur — et le refus des blocs.

Une variante est un « calque » (overlay) par cible :

- `tools`  : {nom d'outil: {"description": str, "parameters": {champ: description}}} — seuls les TEXTES changent : ni
  nom, ni effet, ni schéma (types, champs requis), ni `core` ;
- `router` : {"deep_tool_failures": int, "code_to_deep": bool, "continuation_max_chars": int} (bornes `ROUTER_KEYS`) ;
- `skills` : {nom du skill appris: {"description": str, "body": str}} — appliqué par le service d'apprentissage
  (nouvelle version du skill, `nokker/learning`), jamais par ce module ;
- `blocks` : {fichier de bloc: texte} — JAMAIS appliqué par ce prototype (gel du format, voir `proposal.py`).

Drapeau `self_improve` éteint : `install_active` n'est pas appelé (`api/server.py::build_kernel`), `Registry.overlay`
reste None, rien n'est lu. Allumé sans version approuvée : calques vides, rien ne change non plus.
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger("nokker.improve")

# Variante candidate pour un banc lancé en sous-processus (`benches.py`) : un fichier JSON {cible: calque}, lu par
# `install_active` À LA PLACE des versions approuvées — et seulement si le drapeau `self_improve` est allumé dans la
# config de CE processus (la config du banc, jamais celle de la production, qui ne pose pas la variable).
CANDIDATE_ENV = "NOKKER_IMPROVE_OVERLAY"

ROUTER_KEYS: dict[str, tuple[type, int | None, int | None]] = {
    "deep_tool_failures": (int, 1, 8),
    "code_to_deep": (bool, None, None),
    "continuation_max_chars": (int, 40, 600),
}


def canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_of(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


# ─────────────────────────────── outils ───────────────────────────────


def tool_value(spec: Any) -> dict[str, Any]:
    """Les textes mutables d'un outil (`ToolSpec` ou dict au format du banc) : description et descriptions de champs."""
    get = (lambda k, d=None: spec.get(k, d)) if isinstance(spec, dict) else (lambda k, d=None: getattr(spec, k, d))
    props = ((get("parameters") or {}).get("properties") or {})
    return {"description": str(get("description") or ""),
            "parameters": {k: str(v.get("description")) for k, v in props.items()
                           if isinstance(v, dict) and isinstance(v.get("description"), str)}}


def validate_tool_value(base: dict[str, Any], value: Any) -> dict[str, Any]:
    """Une variante d'outil garde exactement les champs décrits de la base ; textes non vides, ≤ 1 200 caractères."""
    if not isinstance(value, dict) or not isinstance(value.get("description"), str):
        raise ValueError("variante d'outil : objet {description, parameters} attendu")
    desc = value["description"].strip()
    if not desc or len(desc) > 1200:
        raise ValueError("variante d'outil : description vide ou trop longue (≤ 1 200 caractères)")
    params = value.get("parameters") or {}
    if not isinstance(params, dict) or set(params) != set(base.get("parameters") or {}):
        raise ValueError("variante d'outil : les champs décrits doivent rester ceux de l'outil")
    out = {}
    for k, v in params.items():
        if not isinstance(v, str) or not v.strip() or len(v) > 400:
            raise ValueError(f"variante d'outil : description du champ {k!r} vide ou trop longue (≤ 400)")
        out[k] = v.strip()
    return {"description": desc, "parameters": out}


def apply_tool_overlay(specs: list[Any], overlay: dict[str, Any]) -> list[Any]:
    """Copie des specs avec les textes du calque. Un outil absent du calque est rendu tel quel (même objet) ; un champ
    inconnu du calque est ignoré (l'outil a changé depuis l'approbation : on ne crée jamais de champ)."""
    out = []
    for spec in specs:
        patch = overlay.get(spec.name) if isinstance(overlay, dict) else None
        if not isinstance(patch, dict):
            out.append(spec)
            continue
        params = spec.parameters
        fields = patch.get("parameters") or {}
        props = (params or {}).get("properties") or {}
        if fields and any(k in props for k in fields):
            params = copy.deepcopy(params)
            for k, text in fields.items():
                if isinstance(params["properties"].get(k), dict) and isinstance(text, str):
                    params["properties"][k]["description"] = text
        desc = patch.get("description") if isinstance(patch.get("description"), str) else spec.description
        out.append(dataclasses.replace(spec, description=desc, parameters=params))
    return out


# ─────────────────────────────── routeur ───────────────────────────────


def router_flat(overlay: dict[str, Any] | None) -> dict[str, Any]:
    """La boucle range les seuils sous une seule unité `route` ({"route": {…}}) ; le calque stocké et appliqué est plat."""
    overlay = dict(overlay or {})
    return dict(overlay["route"]) if isinstance(overlay.get("route"), dict) else overlay


def validate_router_value(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - set(ROUTER_KEYS):
        raise ValueError(f"variante du routeur : clés permises {sorted(ROUTER_KEYS)}")
    out: dict[str, Any] = {}
    for k, v in value.items():
        kind, lo, hi = ROUTER_KEYS[k]
        if kind is bool:
            if not isinstance(v, bool):
                raise ValueError(f"{k} : booléen attendu")
        elif isinstance(v, bool) or not isinstance(v, int) or not (lo <= v <= hi):  # type: ignore[operator]
            raise ValueError(f"{k} : entier entre {lo} et {hi} attendu")
        out[k] = v
    return out


def router_base(policy: Any = None) -> dict[str, Any]:
    """Valeurs courantes des seuils (politique donnée, sinon défauts du code)."""
    from nokker.route import classify

    inner = getattr(policy, "inner", policy)
    return {"deep_tool_failures": int(getattr(inner, "deep_tool_failures", 3)),
            "code_to_deep": bool(getattr(inner, "code_to_deep", True)),
            "continuation_max_chars": int(classify._CONTINUATION_MAX_CHARS)}


def apply_router(policy: Any, value: dict[str, Any]) -> None:
    """Pose les seuils sur la politique servie (et la constante de suite courte du classeur, globale au processus)."""
    from nokker.route import classify

    inner = getattr(policy, "inner", policy)
    if "deep_tool_failures" in value and hasattr(inner, "deep_tool_failures"):
        inner.deep_tool_failures = int(value["deep_tool_failures"])
    if "code_to_deep" in value and hasattr(inner, "code_to_deep"):
        inner.code_to_deep = bool(value["code_to_deep"])
    if "continuation_max_chars" in value:
        classify._CONTINUATION_MAX_CHARS = int(value["continuation_max_chars"])


@contextmanager
def router_constants(value: dict[str, Any]) -> Iterator[None]:
    """Banc hors ligne : la constante du classeur le temps d'une évaluation, puis rétablie."""
    from nokker.route import classify

    saved = classify._CONTINUATION_MAX_CHARS
    try:
        if "continuation_max_chars" in value:
            classify._CONTINUATION_MAX_CHARS = int(value["continuation_max_chars"])
        yield
    finally:
        classify._CONTINUATION_MAX_CHARS = saved


# ─────────────────────────────── démarrage ───────────────────────────────


def _candidate_overlays() -> dict[str, Any] | None:
    path = os.environ.get(CANDIDATE_ENV, "").strip()
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else None


def install_active(config: Any, kernel: Any) -> dict[str, int]:
    """Appelé par `build_kernel` drapeau allumé SEULEMENT : pose les calques approuvés (ou la variante candidate d'un
    banc, `CANDIDATE_ENV`) sur le registre d'outils et la politique. Les blocs ne sont jamais posés (gel du format).
    Rend {cible: nombre d'éléments posés} pour le journal."""
    from nokker.improve.store import ImproveStore

    candidate = _candidate_overlays()
    store = ImproveStore.from_config(config)
    overlays = candidate if candidate is not None else {t: store.active(t) for t in ("tools", "router")}
    done: dict[str, int] = {}
    tools_overlay = overlays.get("tools") or {}
    if tools_overlay:
        reg = getattr(kernel.tools, "internal", kernel.tools)
        reg.overlay = dict(tools_overlay)
        kernel.tools.refresh()
        done["tools"] = len(tools_overlay)
    router = router_flat(overlays.get("router"))
    if router:
        apply_router(kernel.policy, validate_router_value(router))
        done["router"] = len(router)
    if overlays.get("blocks"):
        logger.warning("self_improve : calque de blocs ignoré (gel du format, jamais appliqué par le noyau)")
    if done:
        logger.info("self_improve : variantes posées %s (%s)", done, "candidate de banc" if candidate else "approuvées")
    return done


__all__ = [
    "CANDIDATE_ENV",
    "ROUTER_KEYS",
    "apply_router",
    "apply_tool_overlay",
    "canonical",
    "install_active",
    "router_base",
    "router_constants",
    "router_flat",
    "sha256_of",
    "tool_value",
    "validate_router_value",
    "validate_tool_value",
]
