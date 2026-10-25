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
from nokker.secrets.base import SecretsError

logger = logging.getLogger("nokker.connectors")


class ConnectorProvider:
    """`resolve` (Lokkal, lot X4) : l'identifiant rangé peut être une référence `secret:NOM`, lue au coffre ici, au
    moment de construire la source ; nom absent ou coffre scellé → connecteur sauté et journalisé (sans valeur)."""

    def __init__(self, store: ConnectorStore, account_for_tenant: Callable[[str], Any],
                 resolve: Callable[[str], str] | None = None, locality: str = "cloud") -> None:
        # `locality` (docs/LOKKAL.md §6.7) : « cloud » chez nokker.ai, « local » dans Lokkal ; portée par chaque outil.
        self.store, self._account_for_tenant, self._resolve = store, account_for_tenant, resolve
        self.locality = locality

    def _account(self, tenant_id: str) -> str | None:
        found = self._account_for_tenant(tenant_id)
        return getattr(found, "account_id", found) or None  # un Account (tenancy) ou directement son id

    def _build(self, account_id: str, kind: str) -> ToolSource | None:
        """Le connecteur `kind` du compte, tel qu'un tour le reçoit : réglages du magasin, identifiant déchiffré, puis
        lu au coffre s'il est une référence `secret:NOM`. Lève `ConnectorError` / `SecretsError` (sans valeur)."""
        cred = self.store.get(account_id, kind)
        if cred is None:
            return None
        secret = self._resolve(cred.secret) if self._resolve else cred.secret
        return build_connector(kind, cred.settings, secret, locality=self.locality)

    def sources_for(self, tenant_id: str) -> list[ToolSource]:
        account_id = self._account(tenant_id)
        if not account_id:
            return []
        out: list[ToolSource] = []
        for kind in KINDS:
            try:
                src = self._build(account_id, kind)
                if src is not None:
                    out.append(src)
            except ConnectorError as exc:
                logger.warning("connecteur %s ignoré pour %s : %s", kind, tenant_id, exc.code)
            except SecretsError as exc:
                logger.warning("connecteur %s ignoré pour %s : %s", kind, tenant_id, exc)
        return out

    def mail_for(self, tenant_id: str) -> Any:
        """Le connecteur mail du locataire, construit par le MÊME chemin que pour un tour (même compte, même magasin,
        même coffre) : la relève du déclencheur `mail:<filtre>` (`nokker/agents/mailwatch.py`). None : aucune boîte
        reliée. Lève `ConnectorError` / `SecretsError` (identifiant illisible), que l'appelant montre sans valeur."""
        account_id = self._account(tenant_id)
        return self._build(account_id, "mail") if account_id else None
