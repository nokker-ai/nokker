"""CORS de l'app native sur app.nokker.ai/v1/* (docs/LOKKAL-PHONE.md §1.1 W1 et §1.2 point 1).

L'app native charge le web depuis une origine locale : `capacitor://localhost` sur iOS, `https://localhost` sur
Android. Elle appelle `https://app.nokker.ai/v1/*` en absolu. Le web d'app.nokker.ai reste de même origine, donc
aucune entrée pour lui ici.

Règles :
- **liste blanche exacte** : aucun motif, aucun écho d'une origine inconnue, aucun `null`. Une origine absente de la
  liste ne reçoit AUCUN en-tête CORS, et sa pré-vérification est refusée (403) ;
- **jamais `Access-Control-Allow-Credentials`** : l'authentification passe par l'en-tête `Authorization` seulement,
  sans cookie ;
- noyau **cloud** seulement. Le noyau Lokkal n'en pose pas : le relais `<nom>.nokker.app` les pose lui-même (même
  liste, plus `https://app.nokker.ai`), et deux `Access-Control-Allow-Origin` font échouer le navigateur ;
- `/v1/*` seulement, sauf `/v1/gateway/authorize` (page de consentement) et les webhooks (Stripe, Telegram,
  WhatsApp, agents), qui ne prennent jamais de CORS.

Contrat : nokker-apps docs/APP-CORE-CONTRACTS.md C1. Liste configurable par `cloud_link.cors_origins`, avec des
origines exactes seulement (`schéma://hôte[:port]`) : `*`, `null` et un motif sont refusés au démarrage.
"""
from __future__ import annotations

import re

DEFAULT_ORIGINS: frozenset[str] = frozenset({"capacitor://localhost", "https://localhost"})
ORIGINS: frozenset[str] = DEFAULT_ORIGINS
METHODS = "GET, POST, PUT, PATCH, DELETE"
# En-têtes que le SDK pose (contrat C1 : Authorization, Content-Type, X-Session-Id, X-Nokker-Stream-Tokens,
# Last-Event-ID ; User-Agent aussi, qu'un WebView peut lister dans la pré-vérification ; X-Nokker-Surface : surface du
# client, lue par le bloc d'environnement)
ALLOW_HEADERS = ("Authorization", "Content-Type", "X-Session-Id", "X-Nokker-Stream-Tokens", "Last-Event-ID",
                 "User-Agent", "X-Nokker-Surface")
_ALLOWED_LOWER = frozenset(h.lower() for h in ALLOW_HEADERS)
EXPOSE_HEADERS = "Retry-After"
MAX_AGE_S = 600
_ORIGIN = re.compile(r"^[a-z][a-z0-9+.-]*://[a-z0-9.-]+(:[0-9]{1,5})?$")


def configure(origins: object = None) -> frozenset[str]:
    """Liste des origines (`cloud_link.cors_origins`) ; absente : les deux de l'app native. Lève `ValueError` sur une
    origine qui n'est pas exacte (pas de `*`, pas de `null`, pas de chemin)."""
    global ORIGINS
    if origins is None:
        ORIGINS = DEFAULT_ORIGINS
        return ORIGINS
    if not isinstance(origins, (list, tuple, set, frozenset)):
        raise ValueError("cloud_link.cors_origins : liste d'origines exactes attendue")
    bad = [o for o in origins if not isinstance(o, str) or not _ORIGIN.match(o)]
    if bad:
        raise ValueError(f"cloud_link.cors_origins : origine non exacte refusée : {bad!r}")
    ORIGINS = frozenset(origins)
    return ORIGINS


def applies(path: str) -> bool:
    if not (path == "/v1" or path.startswith("/v1/")):
        return False
    return path != "/v1/gateway/authorize" and "/webhook" not in path


def allowed(origin: str | None, path: str) -> bool:
    return bool(origin) and origin in ORIGINS and applies(path)


def response_headers(origin: str | None, path: str) -> list[tuple[str, str]]:
    """En-têtes d'une réponse ordinaire. `Vary: Origin` sur TOUTE réponse de `/v1/*` concernée, même sans origine ou
    pour une origine inconnue : un cache ne sert jamais à l'app une réponse sans CORS, ni l'inverse. L'origine exacte
    et les en-têtes lisibles, seulement pour une origine de la liste."""
    if not applies(path):
        return []
    if not allowed(origin, path):
        return [("Vary", "Origin")]
    return [("Access-Control-Allow-Origin", str(origin)), ("Vary", "Origin"),
            ("Access-Control-Expose-Headers", EXPOSE_HEADERS)]


def preflight(origin: str | None, path: str, method: str | None, headers: str | None
              ) -> tuple[int, list[tuple[str, str]]]:
    """`OPTIONS` : 204 et les autorisations pour une origine de la liste, une méthode et des en-têtes connus ;
    403 sans aucun en-tête CORS sinon."""
    refused = 403, ([("Vary", "Origin")] if applies(path) else [])
    if not allowed(origin, path):
        return refused
    if (method or "").upper() not in METHODS.split(", "):
        return refused
    asked = [h.strip().lower() for h in (headers or "").split(",") if h.strip()]
    if any(h not in _ALLOWED_LOWER for h in asked):
        return refused
    return 204, [("Access-Control-Allow-Origin", str(origin)), ("Vary", "Origin"),
                 ("Access-Control-Allow-Methods", METHODS),
                 ("Access-Control-Allow-Headers", ", ".join(ALLOW_HEADERS)),
                 ("Access-Control-Max-Age", str(MAX_AGE_S))]


__all__ = ["ALLOW_HEADERS", "DEFAULT_ORIGINS", "METHODS", "ORIGINS", "allowed", "applies", "configure", "preflight",
           "response_headers"]
