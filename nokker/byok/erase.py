"""Effacement des clés BYOK d'un locataire au coffre du noyau (`nokker erase`, nokker/admin/erase.py) : les noms
`byok.<tn_…>.*` de la portée `system` du coffre configuré par `cloud_byok.secrets`. À blanc par défaut : les noms sont
listés, rien n'est supprimé ; `apply` les supprime. Jamais une valeur : seul `list()` (noms) est lu."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from nokker.byok.service import secret_name

TENANT_RE = re.compile(r"^tn_[A-Za-z0-9_-]{1,64}$")


def provider_from_config(raw: dict[str, Any]) -> Any:
    """Le même fournisseur que `configure_cloud_byok` (nokker/api/server.py) : mêmes réglages, même état."""
    from nokker.secrets.vault import build_provider, chosen_provider

    scfg = dict(raw.get("secrets") or {})
    state_dir = Path(str(raw.get("state_dir") or "~/.config/nokker/byok")).expanduser()
    return build_provider(chosen_provider(scfg), "system", scfg, state_dir)


def erase_byok_tenant(raw: dict[str, Any] | None, tenant: str, *, apply: bool, provider: Any = None) -> list[str]:
    """Noms des clés BYOK du locataire (supprimées si `apply`). [] sans bloc `cloud_byok` allumé (ni `provider`)."""
    if provider is None:
        if not raw or not isinstance(raw, dict) or not raw.get("enabled"):
            return []
        provider = provider_from_config(raw)
    if not isinstance(tenant, str) or not TENANT_RE.fullmatch(tenant):
        raise ValueError(f"locataire invalide : {tenant!r}")
    prefix = secret_name(tenant, "")
    names = sorted(i.name for i in provider.list() if i.name.startswith(prefix))
    if apply:
        for name in names:
            provider.delete(name)
    return names


__all__ = ["erase_byok_tenant", "provider_from_config"]
