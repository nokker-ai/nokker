"""Point d'accroche `backend == "kubernetes"` de `CodeToolSource` : l'environnement vient d'une fabrique injectable,
appelée avec la signature convenue (`cfg, cwd, sub_path, labels, timeout`) ; lectures dans le dossier monté côté noyau
(comme en docker), écritures et commandes par l'environnement ; sans fabrique, refus net."""
from __future__ import annotations

import os

import pytest

from nokker.core.ports import ToolContext
from nokker.tools.code import CodeToolSource

CTX = ToolContext(thread_id="tn_aaaaaaaaaaaa:cs_0123456789abcdef", turn_index=0, channel="api")


def test_kubernetes_needs_a_factory(tmp_path):
    with pytest.raises(ValueError, match="kubernetes"):
        CodeToolSource(root=str(tmp_path), backend="kubernetes")


def test_factory_gets_the_agreed_signature(tmp_path, cluster, fake_upstream):
    rel = "tn_aaaaaaaaaaaa/us_aaaaaaaaaaa1/cs_0123456789abcdef"
    root = os.path.join(cluster.workspace_root, rel)
    os.makedirs(root)
    labels = {"app.kubernetes.io/name": "nokker-sandbox", "nokker.instance": "i", "nokker.tenant": "tn-a",
              "nokker.session": "cs-0"}
    cfg = {"image": "sandbox:1", "workspace_mount": "/work", "exec_timeout_s": 60}
    src = CodeToolSource(root=root, backend="kubernetes", env_factory=cluster.factory(), k8s=cfg, sub_path=rel,
                         labels=labels)
    assert src.mount == "/work"
    r = src.call("write_file", {"path": "a.txt", "content": "hello"}, CTX)
    assert not r.is_error, r.content
    pod = cluster.pods[-1]
    assert (pod.cfg, pod.cwd, pod.sub_path, pod.labels, pod.timeout) == (cfg, "/work", rel, labels, 120)
    assert open(os.path.join(root, "a.txt")).read() == "hello"  # écrit par l'environnement, vu sur le volume
    read = src.call("read_file", {"path": "/work/a.txt"}, CTX)
    assert "hello" in read.content and root not in read.content  # lu côté noyau, chemins du pod seulement
    assert src.call("terminal", {"command": "make test"}, CTX).content.count("ran:make test") == 1
    assert src.run_hook(CTX.thread_id, "./hook.sh", "{}", 5)[0] == 0 and pod.commands[-1] == "./hook.sh"
    src.close()
    assert pod.closed
