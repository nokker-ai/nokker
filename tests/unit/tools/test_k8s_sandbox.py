"""Bac à sable Kubernetes (lot X19) : manifeste, ménage, cycle de vie, sur un faux kubectl (aucun cluster)."""
from __future__ import annotations

import json

import pytest

from nokker.tools import k8s_sandbox as ks

CFG = {"image": "ghcr.io/nokker-ai/sandbox@sha256:" + "a" * 64, "namespace": "nk", "pvc": "nokker-workspaces"}
LABELS = {"nokker.tenant": "tn_acme", "nokker.session": "cs_1"}


class FakeKubectl(ks.Kubectl):
    """Enregistre les appels ; `exec_argv` exécute la commande EN LOCAL (le pod est simulé par l'hôte de test)."""

    def __init__(self, fail: dict[str, int] | None = None, pods: list[str] | None = None) -> None:
        super().__init__(exe="kubectl", namespace="nk")
        self.calls: list[tuple[list[str], str | None]] = []
        self.fail = fail or {}
        self.pods = pods or []

    def run(self, args, stdin=None, timeout=60):
        self.calls.append((list(args), stdin))
        verb = args[0]
        if verb in self.fail:
            return self.fail[verb], "", "refusé par le faux"
        if verb == "get":
            return 0, "".join(f"pod/{p}\n" for p in self.pods), ""
        return 0, "", ""

    def exec_argv(self, pod, argv, stdin):
        self.calls.append((["exec", pod, *argv], None))
        return list(argv)

    def verbs(self) -> list[str]:
        return [a[0] for a, _ in self.calls]

    def manifest(self) -> dict:
        return json.loads(next(s for a, s in self.calls if a[:1] == ["apply"]))


# ─────────────────────────── manifeste ───────────────────────────

def manifest(**over):
    cfg = ks.K8sSandboxConfig.from_dict({**CFG, **over})
    return ks.pod_manifest(cfg, "nokker-sbx-x", "tn_acme/alice/cs_1", LABELS, "core-0")


def test_pod_restricted_sans_jeton_ni_hote():
    m = manifest()
    spec, c = m["spec"], m["spec"]["containers"][0]
    assert spec["automountServiceAccountToken"] is False
    assert spec["serviceAccountName"] == "nokker-sandbox"
    assert spec["enableServiceLinks"] is False
    assert not spec["hostNetwork"] and not spec["hostPID"] and not spec["hostIPC"]
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    sc = c["securityContext"]
    assert sc["readOnlyRootFilesystem"] is True and sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"] == {"drop": ["ALL"]} and sc["privileged"] is False
    assert all("hostPath" not in v for v in spec["volumes"])
    assert {v["mountPath"] for v in c["volumeMounts"]} == {"/workspace", "/tmp"}  # seuls lieux écrits


def test_pod_limites_et_filet():
    m = manifest(resources={"cpu": "1", "memory": "2Gi"}, ephemeral_storage="1Gi", max_lifetime_s=3600)
    res = m["spec"]["containers"][0]["resources"]
    assert res["limits"] == {"cpu": "1", "memory": "2Gi", "ephemeral-storage": "1Gi"}
    assert res["requests"] == {"cpu": "250m", "memory": "512Mi"}
    assert m["spec"]["activeDeadlineSeconds"] == 3600


def test_pod_espace_de_travail_en_subpath():
    m = manifest()
    ws = next(v for v in m["spec"]["containers"][0]["volumeMounts"] if v["name"] == "workspace")
    assert ws == {"name": "workspace", "mountPath": "/workspace", "subPath": "tn_acme/alice/cs_1"}
    assert m["spec"]["volumes"][0] == {"name": "workspace", "persistentVolumeClaim": {"claimName": "nokker-workspaces"}}


@pytest.mark.parametrize("bad", ["", "/", "../x", "a/../b", "a//b", "./a"])
def test_subpath_refuse_les_sorties(bad):
    cfg = ks.K8sSandboxConfig.from_dict(CFG)
    with pytest.raises(ValueError):
        ks.pod_manifest(cfg, "p", bad, LABELS, "core-0")


def test_openshift_sans_uid_ni_fsgroup():
    sc = manifest(run_as_user=None, fs_group=None)["spec"]["securityContext"]
    assert "runAsUser" not in sc and "fsGroup" not in sc and "runAsGroup" not in sc
    assert sc["runAsNonRoot"] is True


def test_reseau_deny_par_defaut_sans_proxy():
    m = manifest()
    env = {e["name"] for e in m["spec"]["containers"][0]["env"]}
    assert m["metadata"]["labels"]["nokker.network"] == "deny"
    assert not env & {"HTTP_PROXY", "HTTPS_PROXY"}


def test_reseau_egress_par_le_proxy():
    m = manifest(network="egress", egress_proxy="http://nokker-egress:3128")
    env = {e["name"]: e["value"] for e in m["spec"]["containers"][0]["env"]}
    assert m["metadata"]["labels"]["nokker.network"] == "egress"
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == "http://nokker-egress:3128"


def test_config_refuse_egress_sans_proxy_et_image_absente():
    with pytest.raises(ValueError):
        ks.K8sSandboxConfig.from_dict({**CFG, "network": "egress"})
    with pytest.raises(ValueError):
        ks.K8sSandboxConfig.from_dict({"namespace": "nk"})
    with pytest.raises(ValueError):
        ks.K8sSandboxConfig.from_dict({**CFG, "network": "open"})


def test_labels_assainis_et_par_session():
    m = ks.pod_manifest(ks.K8sSandboxConfig.from_dict(CFG), "p", "a/b", {"nokker.session": "x" * 80 + "/é"}, "c:0")
    labels = m["metadata"]["labels"]
    assert labels["app.kubernetes.io/name"] == "nokker-sandbox"
    assert labels["nokker.instance"] == "c-0"
    assert len(labels["nokker.session"]) <= 63
    assert all(ch.isalnum() or ch in "-_." for v in labels.values() for ch in v.replace("/", ""))


def test_labels_fixes_non_ecrasables():
    """La NetworkPolicy du chart isole les pods `app.kubernetes.io/name=nokker-sandbox` (DNS et proxy de sortie seuls) :
    un label passé par l'appelant ne peut ni l'enlever ni changer l'instance ou le réseau déclarés."""
    m = ks.pod_manifest(ks.K8sSandboxConfig.from_dict({**CFG, "network": "egress", "egress_proxy": "http://p:3128"}),
                        "p", "a/b", {"app.kubernetes.io/name": "autre", "app.kubernetes.io/managed-by": "x",
                                     "nokker.network": "deny", "nokker.instance": "autre", "nokker.tenant": "t"}, "c:0")
    labels = m["metadata"]["labels"]
    assert labels["app.kubernetes.io/name"] == "nokker-sandbox" and labels["app.kubernetes.io/managed-by"] == "nokker"
    assert labels["nokker.network"] == "egress" and labels["nokker.instance"] == "c-0" and labels["nokker.tenant"] == "t"


def test_git_fait_confiance_au_montage():
    """Le dépôt est cloné par le noyau (autre uid) : safe.directory sur le montage, sinon git refuse tout."""
    env = {e["name"]: e["value"] for e in manifest()["spec"]["containers"][0]["env"]}
    assert (env["GIT_CONFIG_COUNT"], env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == ("1", "safe.directory",
                                                                                         "/workspace")


def test_colocation_rwo():
    m = manifest(colocate_with_core=True, core_pod_labels={"app.kubernetes.io/component": "core"})
    term = m["spec"]["affinity"]["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][0]
    assert term["topologyKey"] == "kubernetes.io/hostname"
    assert "affinity" not in manifest()["spec"]


def test_aucun_secret_dans_le_manifeste():
    cfg = ks.K8sSandboxConfig.from_dict(CFG)
    m = ks.pod_manifest(cfg, "p", "a/b", LABELS, "core-0", env={"GIT_AUTHOR_NAME": "x"})
    env = m["spec"]["containers"][0]["env"]
    # seulement ce que l'appelant donne, plus HOME et le safe.directory de git
    assert {e["name"] for e in env} == {"HOME", "GIT_AUTHOR_NAME", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
                                        "GIT_CONFIG_VALUE_0"}
    assert all(set(e) == {"name", "value"} for e in env)  # aucune référence à un Secret (valueFrom, secretKeyRef)
    assert not any(v.get("secret") for v in m["spec"]["volumes"])


# ─────────────────────────── ménage au démarrage ───────────────────────────

def test_reap_ne_vise_que_cette_instance():
    k = FakeKubectl(pods=["nokker-sbx-1", "nokker-sbx-2"])
    assert ks.reap_k8s_sandboxes("core-0", k) == 2
    get = next(a for a, _ in k.calls if a[0] == "get")
    assert "app.kubernetes.io/name=nokker-sandbox,nokker.instance=core-0" in get
    delete = next(a for a, _ in k.calls if a[0] == "delete")
    assert delete[1:3] == ["pod/nokker-sbx-1", "pod/nokker-sbx-2"]


def test_reap_sans_instance_ni_kubectl():
    assert ks.reap_k8s_sandboxes("", FakeKubectl(pods=["x"])) == 0
    assert ks.reap_k8s_sandboxes("core-0", FakeKubectl(fail={"get": 1})) == 0


# ─────────────────────────── cycle de vie (amont requis) ───────────────────────────

@pytest.fixture
def upstream():
    pytest.importorskip("tools.environments.base")


def open_env(k, **cfg):
    return ks.KubernetesEnvironment({**CFG, **cfg}, "/tmp", "tn_acme/alice/cs_1",
                                    {**LABELS, "nokker.instance": "core-0"}, 30, kubectl=k)


def test_cree_le_pod_attend_pret_et_execute(upstream, tmp_path):
    k = FakeKubectl()
    env = open_env(k)
    try:
        assert k.verbs()[:2] == ["apply", "wait"]
        assert k.manifest()["metadata"]["labels"]["nokker.instance"] == "core-0"
        r = env.execute("echo bonjour", cwd=str(tmp_path))
        assert r["returncode"] == 0 and "bonjour" in r["output"]
        assert any(a[0] == "exec" and a[1] == env.pod for a, _ in k.calls)
    finally:
        env.cleanup()
    assert k.calls[-1][0][:3] == ["delete", "pod", env.pod]


def test_pod_pas_pret_est_supprime(upstream):
    k = FakeKubectl(fail={"wait": 1})
    with pytest.raises(ks.K8sSandboxError):
        open_env(k)
    assert k.verbs() == ["apply", "wait", "delete"]


def test_creation_refusee(upstream):
    with pytest.raises(ks.K8sSandboxError, match="refusée"):
        open_env(FakeKubectl(fail={"apply": 1}))


def test_inactivite_libere_puis_recree(upstream, tmp_path):
    k = FakeKubectl()
    env = open_env(k, idle_ttl_s=10)
    try:
        assert not env.idle_expired(env._last_used + 5)
        assert env.idle_expired(env._last_used + 11)
        env.release()
        assert k.verbs()[-1] == "delete" and not env._live
        r = env.execute("echo encore", cwd=str(tmp_path))
        assert r["returncode"] == 0 and r.get("environment_recreated") is True
        assert k.verbs().count("apply") == 2
    finally:
        env.cleanup()


def test_registre_balaye_les_inactifs(upstream):
    k = FakeKubectl()
    env = open_env(k, idle_ttl_s=10)
    try:
        assert ks.REGISTRY.sweep(now=env._last_used + 60) >= 1
        assert not env._live
    finally:
        env.cleanup()
    assert env not in ks.REGISTRY._envs


def test_instance_requise(upstream):
    with pytest.raises(ValueError):
        ks.KubernetesEnvironment(CFG, "/tmp", "a/b", LABELS, 30, kubectl=FakeKubectl())


def test_ferme_refuse_d_executer(upstream):
    env = open_env(FakeKubectl())
    env.cleanup()
    with pytest.raises(ks.K8sSandboxError):
        env._before_execute()
