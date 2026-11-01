"""Drapeau `execute_code_enabled` (docs/LOKKAL.md §7.31) : éteint, l'outil n'est nulle part dans la surface (ni schéma,
ni index, appel → « outil inconnu ») ; allumé, il rejoint le jeu des comptes à côté des outils de code, jamais
l'interne."""
from __future__ import annotations

import pytest

from nokker.api.server import build_kernel, configure_code_sessions
from nokker.code_sessions import http as code_http
from nokker.config import NokkerConfig
from nokker.core.ports import ToolContext
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from tests.unit.code_sessions.conftest import code_block

TENANT = "tn_aaaaaaaaaaaa"


@pytest.fixture(autouse=True)
def _reset():
    yield
    code_http.configure(None)


def _surface(kernel):
    return {s.name for s in kernel.tools.core()} | {n for n, _ in kernel.tools.index()}


def test_flag_off_by_default_tool_absent_from_the_surface(cluster):
    assert NokkerConfig().execute_code_enabled is False
    cfg = NokkerConfig(code=code_block(cluster.workspace_root))
    kernel = build_kernel(cfg)
    svc = configure_code_sessions(cfg, kernel)
    try:
        for tenant in (TENANT, INTERNAL_TENANT):
            with tenant_scope(tenant):
                assert kernel.tools.spec("execute_code") is None and "execute_code" not in _surface(kernel)
        with tenant_scope(TENANT):
            r = kernel.tools.call("execute_code", {"code": "1"},
                                  ToolContext(thread_id="t", turn_index=0, channel="api", sender="us_a"))
            assert r.is_error and "outil inconnu : execute_code" in r.content
    finally:
        svc.close_all()


def test_flag_on_offers_it_to_accounts_only(cluster):
    cfg = NokkerConfig(code=code_block(cluster.workspace_root), execute_code_enabled=True,
                       execute_code={"tools": ["read_file", "search_files"], "max_calls": 5})
    kernel = build_kernel(cfg)
    svc = configure_code_sessions(cfg, kernel)
    try:
        with tenant_scope(TENANT):
            spec = kernel.tools.spec("execute_code")
            assert spec is not None and not spec.core and "execute_code" in _surface(kernel)
            assert "Outils appelables : read_file, search_files ; au plus 5 appels." in \
                spec.parameters["properties"]["code"]["description"]
            # hors session de code : le bloc fixe, l'erreur dit pourquoi, rien n'est lancé
            r = kernel.tools.call("execute_code", {"code": "1"},
                                  ToolContext(thread_id="t", turn_index=0, channel="api", sender="us_a",
                                              kernel=kernel))
            assert r.content.startswith("[execute_code]") and "pas de session de code sur ce fil" in r.content
        with tenant_scope(INTERNAL_TENANT):
            assert kernel.tools.spec("execute_code") is None
    finally:
        svc.close_all()
