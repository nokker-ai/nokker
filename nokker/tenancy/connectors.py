"""Connecteurs d'un locataire (mail, agenda…) : le port que le registre d'outils interroge pour un testeur
(docs/TENANCY.md §5). L'implémentation réelle (comptes connectés, jetons OAuth chiffrés dans `connector_credentials`)
vit hors du noyau ; le noyau ne connaît que ce Protocol et `NoConnectors`, le défaut.
"""
from __future__ import annotations

from typing import Protocol

from nokker.core.ports import ToolSource


class ConnectorProvider(Protocol):
    """Sources d'outils des connecteurs d'UN locataire. Appelé par `TenantRegistry` à la première demande du locataire,
    puis après `TenantRegistry.forget(tenant_id)` (connecteur ajouté ou retiré). Le fournisseur garde la propriété des
    sources qu'il rend (fermeture comprise) ; elles ne doivent servir QUE ce locataire."""

    def sources_for(self, tenant_id: str) -> list[ToolSource]: ...


class NoConnectors:
    """Aucun connecteur : un testeur n'a que les cœurs, le web, les bulles et la mémoire de son fil."""

    def sources_for(self, tenant_id: str) -> list[ToolSource]:
        return []


__all__ = ["ConnectorProvider", "NoConnectors"]
