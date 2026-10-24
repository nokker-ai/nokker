"""Multi-locataire (bêta) : comptes, locataires, identités d'entrée. Contrat C1 de l'onboarding (session Nokker
onboarding, 21.10.2026) — `Tenancy` ci-dessous ; `MemoryTenancy` (dev, tests) et `PgTenancy` (migration 004)."""
from nokker.tenancy.store import Account, IdentityTaken, MemoryTenancy, PgTenancy, Tenancy

__all__ = ["Account", "IdentityTaken", "MemoryTenancy", "PgTenancy", "Tenancy"]
