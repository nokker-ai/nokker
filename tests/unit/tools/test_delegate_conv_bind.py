"""`delegate` en conversation (`delegate_tenants`) : la source construite par `build_kernel` est liée au registre du
locataire courant ; un appel ne rend jamais « registre du parent non lié »."""
from __future__ import annotations

import dataclasses

from nokker.api.server import build_kernel
from nokker.config import NokkerConfig
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from nokker.tools.delegate import DelegateToolSource


def test_delegate_conv_lie_au_registre_du_locataire_courant():
    config = dataclasses.replace(NokkerConfig(), delegate_tenants=[INTERNAL_TENANT], delegate_tier=True,
                                 approvals_enabled=False, judge_enabled=False)
    kernel = build_kernel(config)
    src = next(s for s in kernel.tools.internal.sources if isinstance(s, DelegateToolSource))
    with tenant_scope(INTERNAL_TENANT):
        reg = src._parent_registry()  # noqa: SLF001
        assert reg is not None and reg is kernel.tools.internal
        assert reg.spec("delegate") is not None
