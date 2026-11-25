"""Navigateur générique de Lokkal : les outils `browser_*` sur `sites-chrome` (docs/BROWSER-LIVE.md §7).

Lokkal n'a pas le pool de la bêta ni le navigateur partagé de l'opérateur. Avec `sites_browser_generic: true` (config
Lokkal) et `NOKKER_SITES_BROWSER_URL` posé, le locataire interne de la machine reçoit les mêmes outils `browser_*` que
les testeurs du pool (schémas `browser_pool_tools.json`), servis par UN enfant de `sites-chrome` réservé à cet usage :
`/s/<id>/mcp`, id `sites_browser_generic_id` (forme `st_` + 10 caractères `[a-z0-9]`, défaut `st_navigateur`), créé
par le gestionnaire au premier appel, distinct de tout connecteur (aucun profil importé, aucun identifiant du coffre).

Tout le reste est celui du pool (`BrowserPoolSource`) : références vérifiées contre le dernier instantané, résultat
clôturé comme contenu de tiers, valeurs des champs sensibles masquées avant le modèle, vue en direct (captures,
`_meta.nokker_action` / `nokker_block`, passation par l'écran de Lokkal), `browser_close` en fin de tour. La sortie
réseau reste celle de `sites-chrome` (adresses privées refusées sauf `SITES_ALLOW_PRIVATE`).
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import Any

from nokker.tenancy.context import INTERNAL_TENANT
from nokker.tools.browser_pool import BrowserPoolSource, _Session

ID_RE = re.compile(r"^st_[a-z0-9]{10}$")  # même règle que le gestionnaire de sites-chrome
DEFAULT_ID = "st_navigateur"


@dataclass
class SitesBrowserSource(BrowserPoolSource):
    child_id: str = DEFAULT_ID
    ops: Any = None  # nokker.browserlive.sites.SitesOps (frame, ticket d'écran, reprise)

    def __post_init__(self) -> None:
        if not ID_RE.match(self.child_id):
            raise ValueError(f"sites_browser_generic_id invalide : {self.child_id!r} (attendu st_ + 10 [a-z0-9])")

    def specs(self):  # noqa: ANN201 — mêmes schémas que le pool, source distincte
        out = super().specs()
        for s in out:
            s.source = "lokkal:sites-browser"
        return out

    def _refuse_tenant(self, tenant: str) -> str | None:
        # Lokkal : la machine n'a que son locataire interne ; un testeur (tn_…) n'a rien à faire ici
        if tenant != INTERNAL_TENANT:
            return "navigateur réservé au propriétaire de cette machine"
        return None

    def _live_tenant(self, tenant: str) -> str | None:
        return None  # vue en direct de Lokkal : session du locataire de la machine (comme browserlive/sites.py)

    def _observe_extra(self, session_id: str) -> dict[str, Any]:
        return {"ops": self.ops, "audience": set(), "auto_resume": True}

    def _open(self, tenant: str) -> _Session | tuple[int, str]:
        # pas de POST /sessions : l'enfant `child_id` est créé par sites-chrome au premier appel MCP
        return _Session(id=self.child_id, mcp_url=f"{self.base_url.rstrip('/')}/s/{self.child_id}/mcp",
                        token=self.api_token(), expires_at=math.inf)


def build_sites_browser(config: Any) -> SitesBrowserSource | None:
    """Source `browser_*` de Lokkal, ou None (drapeau éteint, ou sites-chrome absent)."""
    if not getattr(config, "sites_browser_generic", False):
        return None
    url = os.environ.get("NOKKER_SITES_BROWSER_URL", "")
    if not url:
        return None
    from nokker.browserlive.sites import SitesOps

    def token() -> str:
        return os.environ.get("NOKKER_SITES_BROWSER_TOKEN", "")

    return SitesBrowserSource(base_url=url, api_token=token, ops=SitesOps(url, token),
                              child_id=str(getattr(config, "sites_browser_generic_id", "") or DEFAULT_ID))


__all__ = ["DEFAULT_ID", "ID_RE", "SitesBrowserSource", "build_sites_browser"]
