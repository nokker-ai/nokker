"""Modèle RENVOYÉ par l'amont (champ `model` de sa réponse) : lecture bornée et comparaison tolérante au modèle du palier.

Le ledger journalisait le modèle du PALIER (la config). Un routeur peut avoir basculé sur un repli sans que le noyau le
sache ; le champ `model` de la réponse le dit. Il est gardé tel quel dans `LLMCall.model_served` (colonne
`ledger_entries.model_served`, migration 093), ou None : jamais une valeur inventée, jamais le modèle du palier recopié.

`clean_served` borne la valeur : chaîne, ≤ 200 caractères, sans caractère de contrôle ni espace en bord.

`same_model` décide s'il y a une VRAIE différence (événement hôte `route.model_mismatch`, jamais bloquant). Règle de
normalisation, appliquée aux deux côtés avant comparaison :

1. casse : tout en minuscules, espaces de bord retirés ;
2. préfixe de fournisseur : tout ce qui précède le dernier « / » (`qwen/qwen3.8-27b` = `qwen3.8-27b`,
   `accounts/x/models/m` = `m`), et le préfixe Bedrock `[région.]anthropic.` ;
3. suffixe de date ou de version, retiré s'il est en fin : `-AAAAMMJJ`, `-AAAA-MM-JJ`, `@AAAAMMJJ`, `-latest`,
   `-vN` ou `-vN:M` (Bedrock) — `claude-sonnet-5-5-20261101` = `claude-sonnet-5-5`.

Source de la valeur : l'en-tête `X-Model-Served` du Router (en priorité, flux et hors flux) ; sans en-tête, le corps
seulement pour un amont DIRECT (BYOK, vLLM, serveur OpenAI-compatible) ; un amont Router sans en-tête : None (le Router
réécrit `model` : le corps est trompeur). `X-Sokkan-Upstream` (id de l'amont) est gardé à part (`Completion.upstream`).

Rien d'autre n'est assoupli : `qwen3.8-27b` ≠ `qwen3.8-35b`, un nom de repli reste une différence. Sans modèle attendu
(alias, routeur : le palier n'en configure pas) ou sans modèle servi, il n'y a rien à comparer : équivalent.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

MODEL_SERVED_MAX = 200
# En-têtes de RÉPONSE du Router (flux et hors flux) : le modèle réellement servi à l'amont, et l'id de l'amont. Le Router
# réécrit le `model` du CORPS au nom demandé : le corps ne dit donc rien d'un repli, l'en-tête oui.
SERVED_HEADER = "X-Model-Served"
UPSTREAM_HEADER = "X-Sokkan-Upstream"
LINK_INFERENCE_PATH = "/v1/inference"  # fin du `base_url` d'inférence du lien nokker.ai (qui relaie au Router)
ROUTER_HOST = "router.sokkan.ch"  # hôte du Router de Nokker (`local/setup.py::ROUTER_URL`) : un palier dessus parle au Router

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_BEDROCK_PREFIX = re.compile(r"^(?:[a-z]{2,4}\.)?anthropic\.")
_SUFFIXES = (
    re.compile(r"[-@]\d{8}$"),
    re.compile(r"-\d{4}-\d{2}-\d{2}$"),
    re.compile(r"-latest$"),
    re.compile(r"-v\d+(?::\d+)?$"),
)


def clean_served(value: Any) -> str | None:
    """Valeur du champ `model` de la réponse, bornée ; None si absente, pas une chaîne, ou vide une fois nettoyée."""
    if not isinstance(value, str):
        return None
    text = _CONTROL.sub("", value).strip()
    return text[:MODEL_SERVED_MAX] or None


def served_from_headers(headers: Any) -> tuple[str | None, str | None]:
    """(modèle servi, id de l'amont) lus dans les en-têtes de réponse, insensibles à la casse, bornés par `clean_served`.
    Absent, vide ou invalide (pas une chaîne) : None. Sans en-têtes (None, mapping vide) : (None, None)."""
    if headers is None:
        return None, None
    try:
        low = {str(k).lower(): v for k, v in headers.items()}
    except AttributeError:
        return None, None
    return clean_served(low.get(SERVED_HEADER.lower())), clean_served(low.get(UPSTREAM_HEADER.lower()))


def is_router_url(base_url: str) -> bool:
    """Le `base_url` d'un palier passe-t-il par le Router de Nokker ? Oui pour l'hôte `router.sokkan.ch`, et pour le LIEN
    nokker.ai (`inference_base` = `<nokker.ai>/v1/inference`, `local/setup.py`, relayé au Router par `cloud/http.py`)."""
    parts = urlsplit(str(base_url or ""))
    return (parts.hostname or "").lower() == ROUTER_HOST or parts.path.rstrip("/").endswith(LINK_INFERENCE_PATH)


def normalize_model(name: str) -> str:
    """Forme comparable d'un nom de modèle (règle dans le docstring du module)."""
    n = name.strip().lower()
    n = n.rsplit("/", 1)[-1]
    n = _BEDROCK_PREFIX.sub("", n)
    changed = True
    while changed:  # `-v1:0` après une date : les deux partent
        changed = False
        for rx in _SUFFIXES:
            m = rx.sub("", n, count=1)
            if m != n and m:
                n, changed = m, True
    return n


def same_model(expected: str | None, served: str | None) -> bool:
    """Vrai si rien n'est à signaler : un des deux manque, ou les noms sont équivalents après normalisation."""
    if not expected or not served:
        return True
    return normalize_model(expected) == normalize_model(served)


__all__ = ["MODEL_SERVED_MAX", "ROUTER_HOST", "SERVED_HEADER", "UPSTREAM_HEADER", "clean_served", "is_router_url",
           "normalize_model", "same_model", "served_from_headers"]
