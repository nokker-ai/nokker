"""Câblage dans le noyau (`api/server.py::configure_code_sessions`) : drapeau éteint = rien ne change ; allumé = outils
de code dans le jeu des comptes (jamais l'interne), portes des sessions devant celles du noyau, ménage des bacs à sable
de CETTE instance au démarrage (une fois), erreur claire au démarrage pour un bloc refusé."""
from __future__ import annotations

import logging

import pytest

from nokker.api.server import build_kernel, configure_code_sessions
from nokker.code_sessions import http as code_http
from nokker.code_sessions.config import CodeConfigError
from nokker.config import NokkerConfig
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from tests.unit.code_sessions.conftest import code_block


@pytest.fixture(autouse=True)
def _reset():
    yield
    code_http.configure(None)


def test_off_changes_nothing():
    kernel = build_kernel(NokkerConfig())
    before_gates, before = kernel.gates, list(kernel.tools.tester_sources)
    assert configure_code_sessions(NokkerConfig(), kernel) is None
    assert kernel.gates is before_gates and kernel.tools.tester_sources == before and code_http.service() is None
    with tenant_scope("tn_aaaaaaaaaaaa"):
        assert kernel.tools.spec("write_file") is None


def test_on_wires_tools_gates_and_reaps_once(cluster, caplog):
    cfg = NokkerConfig(code=code_block(cluster.workspace_root))
    kernel = build_kernel(cfg)
    inner = kernel.gates
    with caplog.at_level(logging.INFO):
        svc = configure_code_sessions(cfg, kernel)
    try:
        assert code_http.service() is svc and cluster.reaped == ["nokker-test"]
        assert any("2 bac(s) à sable" in r.getMessage() for r in caplog.records)
        assert kernel.gates is not inner and kernel.gates.inner is inner
        with tenant_scope("tn_aaaaaaaaaaaa"):
            assert kernel.tools.spec("write_file") is not None and kernel.tools.spec("checkpoint_list") is not None
        with tenant_scope(INTERNAL_TENANT):
            assert kernel.tools.spec("write_file") is None  # jamais dans le registre interne
    finally:
        svc.close_all()


def test_on_wires_skills_subagents_and_child_kernels_keep_the_outer_gates(cluster):
    """Skills du dépôt et sous-agents dans le jeu des comptes ; un enfant est une copie du noyau PRIS AU MOMENT où il
    part : les portes posées plus tard à l'extérieur (foyer : `MinorGates`) l'entourent aussi ; ses cartes se décident
    par le propriétaire de la session (`approvals.thread_owner`) et s'exécutent avec SON noyau (`subagent_kernel`)."""
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.tools.registry import Registry

    cfg = NokkerConfig(code=code_block(cluster.workspace_root))
    approvals = InMemoryApprovals()
    kernel = build_kernel(cfg, approvals=approvals)
    svc = configure_code_sessions(cfg, kernel)
    try:
        with tenant_scope("tn_aaaaaaaaaaaa"):
            assert kernel.tools.spec("delegate") is not None and kernel.tools.spec("delegate_cancel") is not None
            assert kernel.tools.spec("skill_list").source == "builtin:code-skills"
        with tenant_scope(INTERNAL_TENANT):
            assert kernel.tools.spec("delegate") is None
        assert approvals.thread_owner == svc.owner_of_thread
        outer = type("Outer", (), {"__init__": lambda self, inner: setattr(self, "inner", inner)})(kernel.gates)
        kernel.gates = outer  # posé après les sessions (configure_household)
        child = svc.kernel_factory(Registry(sources=[]), "tn_aaaaaaaaaaaa:cs_0123456789abcdef/sa-1", None)
        assert child.gates is outer and child.approvals is approvals and child.max_calls == kernel.max_calls
        assert child is not kernel and kernel.tools is not child.tools
    finally:
        svc.close_all()


def test_refused_blocks_fail_at_startup(tmp_path):
    kernel = build_kernel(NokkerConfig())
    with pytest.raises(CodeConfigError, match="local"):
        configure_code_sessions(NokkerConfig(code={"enabled": True, "backend": "local",
                                                   "workspace_root": str(tmp_path)}), kernel)
    with pytest.raises(CodeConfigError, match="Lokkal"):
        configure_code_sessions(NokkerConfig(code=code_block(str(tmp_path), backend="docker", kubernetes=None),
                                             local={"token_path": str(tmp_path / "t")}), kernel)


def test_config_file_key(tmp_path):
    from nokker.config import load_config

    p = tmp_path / "c.yaml"
    p.write_text("code:\n  enabled: true\n  backend: docker\n  workspace_root: /workspaces\n", encoding="utf-8")
    assert load_config(str(p)).code == {"enabled": True, "backend": "docker", "workspace_root": "/workspaces"}
