"""`ConnectorProvider.sources_for(tenant_id)` : les ToolSource (mail, agenda) du locataire courant, pour le registre du tour.
Lit `connector_credentials` ; en bêta, la connexion du noyau est celle du locataire (RLS). Une source illisible
(clé changée, réglages périmés) est sautée et journalisée : un connecteur cassé ne casse jamais le tour."""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from nokker.connectors import build_connector
from nokker.connectors.errors import ConnectorError
from nokker.connectors.store import KINDS, ConnectorStore
from nokker.core.ports import ToolSource

logger = logging.getLogger("nokker.connectors")


class ConnectorProvider:
    def __init__(self, store: ConnectorStore, account_for_tenant: Callable[[str], Any]) -> None:
        self.store, self._account_for_tenant = store, account_for_tenant

    def sources_for(self, tenant_id: str) -> list[ToolSource]:
        found = self._account_for_tenant(tenant_id)
        account_id = getattr(found, "account_id", found)  # un Account (tenancy) ou directement son id
        if not account_id:
            return []
        out: list[ToolSource] = []
        for kind in KINDS:
            try:
                cred = self.store.get(account_id, kind)
                if cred is not None:
                    out.append(build_connector(kind, cred.settings, cred.secret))
            except ConnectorError as exc:
                logger.warning("connecteur %s ignoré pour %s : %s", kind, tenant_id, exc.code)
        return out
