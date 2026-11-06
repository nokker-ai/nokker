"""`GET /v1/capabilities` (05.11.2026, docs/CLIENT-API.md §Capacités) : les fonctions actives POUR L'APPELANT, en
drapeaux booléens, pour que le web cesse de sonder `/v1/house`, `/v1/local/cost`, `/v1/speech`… et d'encaisser des 404.

Règles :
  - Un drapeau vaut `true` quand les routes de la fonction répondent à cet appelant (installation ET profil : compte de
    nokker.ai `us_…`, propriétaire de la machine Lokkal, identité interne). `false` sinon, jamais d'erreur : une fonction
    inconnue du noyau (branche pas encore livrée) est simplement absente ou fausse.
  - Rien de secret, rien d'un autre locataire : des booléens, le mode (`cloud`/`lokkal`) et la version du contrat. Pas
    d'identifiant, pas de réglage, pas de quota.
  - Une seule lecture en base (appartenance à un foyer, pour `house`), faite par l'appelant DANS `tenant_scope` du
    locataire de l'appelant ; en panne, `house` vaut `false` (avec un avis), jamais un 500.
  - La config est posée une fois par `serve()` (`configure`) ; sans elle (tests, `make_handler` seul), les drapeaux qui
    ne se lisent qu'en config valent `false`.
"""
from __future__ import annotations

import logging
from typing import Any

from nokker.tenancy.resolve import is_account

logger = logging.getLogger("nokker.api.capabilities")

CONTRACT_VERSION = 1
_config: Any = None


def configure(config: Any) -> None:
    global _config
    _config = config


def _cfg(name: str) -> Any:
    return getattr(_config, name, None) if _config is not None else None


def _block_on(raw: Any) -> bool:
    """Bloc de config `{enabled: …}` (ex. `cloud_byok`) : allumé seulement si `enabled` est vrai."""
    return isinstance(raw, dict) and bool(raw.get("enabled"))


def _connectors_multi() -> bool:
    from nokker.connectors import http as connectors_http

    return connectors_http.multi()


def _connectors_oauth() -> bool:
    from nokker.connectors import http as connectors_http

    svc = connectors_http.service()
    return svc is not None and svc.multi and getattr(svc, "oauth", None) is not None


def _household_member(sender: str) -> bool:
    from nokker.household import http as household_http

    house = household_http.service()
    if house is None:
        return False
    try:
        return house.membership(sender) is not None
    except Exception:  # foyer illisible : la salle n'est pas proposée, pas de 500
        logger.warning("capabilities : foyer illisible", exc_info=True)
        return False


def for_caller(sender: str, *, local: bool, local_owner: bool, ledger: bool, hub: bool, approvals: bool,
               agents: bool, shares: bool) -> dict[str, Any]:
    """Corps de `GET /v1/capabilities`. À appeler DANS `tenant_scope` du locataire de `sender`."""
    from nokker.ambient import http as ambient_http
    from nokker.billing import http as billing_http
    from nokker.code_sessions import http as code_http
    from nokker.guide import http as guide_http
    from nokker.house import http as house_http
    from nokker.household import http as household_http
    from nokker.household.famcal import http as famcal_http
    from nokker.household.paper import http as paper_http
    from nokker.incidents import http as incidents_http
    from nokker.learning import http as learning_http
    from nokker.memory import long_http
    from nokker.memory.consolidation import http as consolidation_http
    from nokker.payments import http as payments_http
    from nokker.plan import http as plan_http
    from nokker.presence import http as presence_http
    from nokker.sso import http as sso_http
    from nokker.team import http as team_http
    from nokker.voice import web as voice_web
    from nokker.webpacks import http as packs_http

    account = is_account(sender)
    # Routes montées par `_account_route` (server.py) : compte `us_…` exigé, sinon 403 account_required.
    household = household_http.enabled() and account
    # Paiements : compte `us_…` en cloud ; en Lokkal, le propriétaire SUR la machine (own_card, PAY-AGENT §11 : par la
    # passerelle, les routes répondent 403 local_only — le web n'y est alors pas le propriétaire local).
    pay = (account or (local and local_owner)) and payments_http.service() is not None
    amb = ambient_http.service()
    glance_on = amb is not None and getattr(amb, "glance", None) is not None
    presence = account and presence_http.enabled()
    features = {
        "account": account,
        "approvals": approvals,
        "agents": agents,
        "billing": account and billing_http.service() is not None,
        "cloud_byok": account and not local and _block_on(_cfg("cloud_byok")),
        "code": account and code_http.service() is not None,  # /v1/code* : compte exigé (code_sessions/http.py)
        "connectors_multi": account and _connectors_multi(),  # docs/CONNECTORS-MULTI.md : plusieurs boîtes, partages
        "connectors_oauth": account and not local and _connectors_oauth(),  # docs/CONNECTORS-OAUTH.md
        "consolidation": account and consolidation_http.enabled(),
        "family_calendar": account and famcal_http.enabled(),
        # /v1/glance/devices : propriétaire de la machine en Lokkal, compte de nokker.ai en cloud.
        "glance": glance_on and ((local and local_owner) or (not local and account)),
        "guide": guide_http.enabled(),
        "house": household and house_http.enabled() and _household_member(sender),
        "household": household,
        "household_paper": account and paper_http.enabled(),
        "learning": account and learning_http.enabled(),
        "local": local,
        "local_cost": local and local_owner and ledger,
        "long_memory": account and long_http.enabled(),
        "operate": incidents_http.service() is not None,  # /v1/incidents*, /v1/alerts* : toute identité vérifiée
        "packs": account and packs_http.enabled(),
        "pay": pay,
        "pay_agent": pay and bool(_cfg("pay_agent")),
        "plan": plan_http.service() is not None,  # /v1/plan* : toute identité vérifiée
        "presence": presence,
        "share": hub and shares,
        "show_me": bool(_cfg("show_me")),
        "speech": voice_web.service() is not None,
        "sso": account and sso_http.enabled(),
        "steer": hub and bool(_cfg("steer_enabled")),
        "team": account and team_http.enabled(),
        "threads": hub,
        "turn_cost": hub,
        "who_does_what": presence,  # même chose que `presence` (nom du drapeau de config)
    }
    return {"version": CONTRACT_VERSION, "mode": "lokkal" if local else "cloud",
            "features": {k: bool(v) for k, v in sorted(features.items())}}


__all__ = ["CONTRACT_VERSION", "configure", "for_caller"]
