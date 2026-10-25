"""Bloc `code` des sessions de code multi-utilisateur (docs/LOKKAL.md §7.28) : éteint par défaut, backend local refusé
hors Lokkal, contrat kubernetes validé en type seulement et transmis tel quel, module absent = erreur au démarrage."""
from __future__ import annotations

import sys

import pytest

from nokker.code_sessions.config import CodeConfig, CodeConfigError, k8s_labels, sanitize_label
from tests.unit.code_sessions.conftest import code_block


def test_absent_or_disabled_is_off(tmp_path):
    assert CodeConfig.from_config(None, lokkal=False) is None
    assert CodeConfig.from_config({}, lokkal=False) is None
    assert CodeConfig.from_config({"enabled": False, "backend": "local"}, lokkal=False) is None


def test_local_backend_is_refused_outside_lokkal(tmp_path):
    with pytest.raises(CodeConfigError, match="local"):
        CodeConfig.from_config(code_block(str(tmp_path), backend="local"), lokkal=False)
    with pytest.raises(CodeConfigError, match="backend"):
        CodeConfig.from_config(code_block(str(tmp_path), backend="ssh"), lokkal=False)


def test_not_inside_lokkal(tmp_path, cluster):
    with pytest.raises(CodeConfigError, match="Lokkal"):
        CodeConfig.from_config(code_block(cluster.workspace_root), lokkal=True)


@pytest.mark.parametrize("over,match", [
    ({"layout": "user/session"}, "layout"),
    ({"workspace_root": "relative/path"}, "workspace_root"),
    ({"max_sessions_per_user": 0}, "max_sessions_per_user"),
    ({"idle_ttl_s": "long"}, "idle_ttl_s"),
    ({"kubernetes": {"exec_timeout_s": "x"}}, "exec_timeout_s"),
    ({"kubernetes": {"network": "open"}}, "network"),
    ({"kubernetes": {"tolerations": {"a": 1}}}, "tolerations"),
    ({"kubernetes": {"colocate_with_core": "yes"}}, "colocate_with_core"),
    ({"forge": {"allowed_hosts": "git.example.org"}}, "allowed_hosts"),
])
def test_bad_values_fail_at_startup(cluster, over, match):
    with pytest.raises(CodeConfigError, match=match):
        CodeConfig.from_config(code_block(cluster.workspace_root, **over), lokkal=False)


def test_missing_workspace_root_fails(tmp_path, cluster):
    with pytest.raises(CodeConfigError, match="workspace_root"):
        CodeConfig.from_config(code_block(str(tmp_path / "absent")), lokkal=False)


def test_kubernetes_block_is_passed_as_is(cluster):
    k8s = {"namespace": "", "image": "sandbox:1", "image_pull_secrets": ["pull"], "pvc": "ws",
           "workspace_mount": "/workspace", "service_account": "sb", "resources": {"cpu": "1", "memory": "1Gi"},
           "resources_requests": {"cpu": "100m"}, "ephemeral_storage": "2Gi", "tmp_size": "1Gi", "exec_timeout_s": 60,
           "start_timeout_s": 90, "max_lifetime_s": 86400, "network": "egress", "egress_proxy": "http://egress:3128",
           "run_as_user": 10000, "fs_group": 10000, "node_selector": {"pool": "sb"}, "tolerations": [{"key": "sb"}],
           "colocate_with_core": True}
    cfg = CodeConfig.from_config(code_block(cluster.workspace_root, kubernetes=dict(k8s)), lokkal=False)
    assert cfg.kubernetes == k8s and cfg.mount == "/workspace"
    assert cfg.env_factory is sys.modules["nokker.tools.k8s_sandbox"].KubernetesEnvironment


def test_kubernetes_module_absent_is_a_clear_startup_error(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "nokker.tools.k8s_sandbox", None)  # import → ImportError
    root = tmp_path / "ws"
    root.mkdir()
    with pytest.raises(CodeConfigError, match="k8s_sandbox"):
        CodeConfig.from_config(code_block(str(root)), lokkal=False)


def test_docker_backend_defaults(tmp_path):
    cfg = CodeConfig.from_config(code_block(str(tmp_path), backend="docker", kubernetes=None,
                                            docker={"network_name": "nokker-sandbox"}), lokkal=False)
    assert cfg.backend == "docker" and cfg.env_factory is None and cfg.docker["network_name"] == "nokker-sandbox"
    assert cfg.modes == ("default", "accept_edits", "plan") and cfg.default_mode == "accept_edits"


def test_instance_is_stable(tmp_path, monkeypatch):
    monkeypatch.setenv("NOKKER_INSTANCE", "release-a")
    cfg = CodeConfig.from_config(code_block(str(tmp_path), backend="docker", kubernetes=None, instance=None),
                                 lokkal=False)
    assert cfg.instance == "release-a"
    monkeypatch.delenv("NOKKER_INSTANCE")
    monkeypatch.setenv("HOSTNAME", "nokker-core-0")
    cfg = CodeConfig.from_config(code_block(str(tmp_path), backend="docker", kubernetes=None, instance=None),
                                 lokkal=False)
    assert cfg.instance == "nokker-core-0"


@pytest.mark.parametrize("raw,clean", [
    ("tn_abcdef012345", "tn-abcdef012345"), ("cs_0123456789abcdef", "cs-0123456789abcdef"),
    ("Nokker.Core_0", "nokker.core-0"), ("_x_", "x"), ("a" * 80, "a" * 63), ("--", "x"),
])
def test_label_values_are_kubernetes_safe(raw, clean):
    assert sanitize_label(raw) == clean


def test_labels_of_a_session():
    assert k8s_labels("Core-1", "tn_aaaaaaaaaaaa", "cs_0123456789abcdef") == {
        "app.kubernetes.io/name": "nokker-sandbox", "nokker.instance": "core-1", "nokker.tenant": "tn-aaaaaaaaaaaa",
        "nokker.session": "cs-0123456789abcdef"}


def test_state_dir_never_inside_a_session_tree(tmp_path):
    (tmp_path / "tn_aaaaaaaaaaaa").mkdir()
    for bad in (str(tmp_path / "tn_aaaaaaaaaaaa" / "state"), "relative/state"):
        with pytest.raises(CodeConfigError, match="state_dir"):
            CodeConfig.from_config(code_block(str(tmp_path), backend="docker", kubernetes=None, state_dir=bad),
                                   lokkal=False)
    ok = CodeConfig.from_config(code_block(str(tmp_path), backend="docker", kubernetes=None), lokkal=False)
    assert ok.state_dir == str(tmp_path / ".nokker-state")
