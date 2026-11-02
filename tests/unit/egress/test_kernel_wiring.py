"""Câblage noyau du drapeau `code.egress_secrets` / `local.code.egress_secrets` :

- drapeau éteint (bloc absent ou `enabled: false`) : config inchangée, aucun appel au proxy, bac à sable construit
  exactement comme avant (pas d'argument `env` au module kubernetes, `env={"HOME": "/tmp"}` en docker), une seule note
  vue par le modèle (le texte d'origine) ;
- drapeau allumé : chaque session reçoit SON jeton et les noms présents dans le coffre de SON locataire, jamais une
  valeur ; un locataire sans le secret n'a ni accord ni note ; la fermeture révoque.
"""
from __future__ import annotations

import sys
import types

import pytest

from nokker.code_sessions.config import CodeConfig, CodeConfigError
from nokker.code_sessions.secrets import TenantSecrets
from nokker.code_sessions.service import CodeSessions
from nokker.code_sessions.store import MemoryCodeStore
from nokker.core.ports import ToolContext
from nokker.egress.kernel import EgressSecrets, EgressSecretsConfig, SandboxSetup, note_text
from nokker.egress.policy import parse_rules
from nokker.tenancy.context import tenant_scope
from tests.unit.code_sessions.conftest import MemorySecrets, cluster, code_block, fake_upstream  # noqa: F401
from tests.unit.egress.conftest import egress_block

TA, TB = "tn_aaaaaaaaaaaa", "tn_bbbbbbbbbbbb"
ANA, CAM = "us_aaaaaaaaaaa1", "us_bbbbbbbbbbb1"
VALUE = "ghp_tenantA_0123456789abcdef"
K8S_EGRESS = {"namespace": "", "image": "sandbox:1", "pvc": "workspaces", "resources": {"cpu": "1", "memory": "1Gi"},
              "exec_timeout_s": 120, "network": "egress", "egress_proxy": "http://nokker-egress:3128"}


def _svc(cfg, vaults, notes):
    secrets = TenantSecrets(lambda tn: vaults.setdefault(tn, MemorySecrets()))
    return CodeSessions(cfg=cfg, store=MemoryCodeStore(), secrets=secrets, cloner=lambda *a, **k: None,
                        note=lambda thread, text: notes.append((thread, text)))


def _touch(svc, tenant, user, thread):
    with tenant_scope(tenant):
        r = svc.tools().call("write_file", {"path": "a.txt", "content": "x"},
                             ToolContext(thread_id=thread, turn_index=1, channel="api", sender=user))
    assert not r.is_error, r.content


# ── drapeau éteint ──

def test_config_eteinte_par_defaut(cluster):  # noqa: F811
    assert CodeConfig.from_config(code_block(cluster.workspace_root), lokkal=False).egress_secrets is None
    off = code_block(cluster.workspace_root, egress_secrets={"enabled": False, "secrets": {"X": {}}})
    assert CodeConfig.from_config(off, lokkal=False).egress_secrets is None
    assert EgressSecretsConfig.from_config(None) is None


def test_drapeau_eteint_bac_a_sable_et_notes_identiques(cluster, fake_upstream):  # noqa: F811
    """Le faux module kubernetes n'accepte PAS d'argument `env` (signature d'origine) : il casserait si le drapeau
    éteint en passait un."""
    cfg = CodeConfig.from_config(code_block(cluster.workspace_root), lokkal=False)
    notes: list = []
    vaults = {TA: MemorySecrets()}
    vaults[TA].values["GH_TOKEN"] = VALUE
    svc = _svc(cfg, vaults, notes)
    assert svc.egress is None
    with tenant_scope(TA):
        a = svc.create(ANA, {})
    _touch(svc, TA, ANA, f"{TA}:{a['id']}")
    pod = cluster.pods[-1]
    assert pod.cfg == {**cfg.kubernetes, "idle_ttl_s": 1800}
    assert len(notes) == 1 and notes[0][1].startswith("[session de code] dossier de travail /workspace")
    svc.close_all()


def test_drapeau_eteint_docker_env_d_origine(tmp_path, fake_upstream, monkeypatch):  # noqa: F811
    from nokker.tools.code import CodeToolSource

    seen: dict = {}
    mod = types.ModuleType("tools.environments.docker")

    class DockerEnvironment:
        def __init__(self, **kw):
            seen.update(kw)
            self.cwd = kw["cwd"]

    mod.DockerEnvironment = DockerEnvironment
    mod._readonly_skill_mount_args = list
    mod._BASE_SECURITY_ARGS = []
    monkeypatch.setitem(sys.modules, "tools.environments.docker", mod)
    CodeToolSource(root=str(tmp_path), backend="docker").workspace("t1")
    assert seen["env"] == {"HOME": "/tmp"}
    seen.clear()
    CodeToolSource(root=str(tmp_path), backend="docker", sandbox_env={"HTTPS_PROXY": "http://n:t@e:3128"}).workspace("t2")
    assert seen["env"] == {"HOME": "/tmp", "HTTPS_PROXY": "http://n:t@e:3128"}


def test_lokkal_drapeau_eteint(tmp_path):
    from nokker.local.api import SessionCodeTools, _egress

    assert _egress({}) is None and _egress({"egress_secrets": {"enabled": False}}) is None
    t = SessionCodeTools(backend="local")
    t.open("th", str(tmp_path), str(tmp_path))
    assert t.for_thread("th").sandbox_env == {}


def test_lokkal_drapeau_allume_environnement_de_la_session(tmp_path):
    from nokker.local.api import SessionCodeTools, _egress_id

    calls = []

    class FakeEgress:
        def open_session(self, session, tenant, provider, host_dir, sandbox_dir):
            calls.append((session, tenant, provider, host_dir, sandbox_dir))
            return SandboxSetup(proxy_url="http://nokker:t@egress:3128", env={"HTTPS_PROXY": "http://nokker:t@egress:3128"},
                                names=("GH_TOKEN",))

        def close_session(self, session):
            calls.append(("close", session))

    t = SessionCodeTools(backend="docker", egress=FakeEgress(), egress_provider="vault-user")
    t.open("th", str(tmp_path), str(tmp_path))
    assert t.for_thread("th").sandbox_env == {"HTTPS_PROXY": "http://nokker:t@egress:3128"}
    assert calls[0] == (_egress_id("th"), "local", "vault-user", str(tmp_path), "/workspace")
    t.close()
    assert calls[-1] == ("close", _egress_id("th"))


# ── drapeau allumé ──

def test_config_invalide_arrete_le_noyau(cluster, tmp_path):  # noqa: F811
    bad = code_block(cluster.workspace_root, egress_secrets={"enabled": True, "proxy_url": "http://e:3128",
                                                              "control_url": "http://e:3129", "ca_cert": "/ca.crt",
                                                              "secrets": {"X": {"hosts": ["a.example"]}}})
    with pytest.raises(CodeConfigError, match="au moins un emplacement"):
        CodeConfig.from_config(bad, lokkal=False)
    deny = code_block(cluster.workspace_root, egress_secrets={"enabled": True, "proxy_url": "http://e:3128",
                                                               "control_url": "http://e:3129", "ca_cert": "/ca.crt"})
    with pytest.raises(CodeConfigError, match="network=egress"):
        CodeConfig.from_config(deny, lokkal=False)


@pytest.fixture
def cluster_env(cluster, monkeypatch):  # noqa: F811
    """Le faux module kubernetes, avec l'argument `env` du vrai (`KubernetesEnvironment(..., env=)`)."""
    base = cluster.factory()

    class WithEnv(base):
        def __init__(self, cfg, cwd, sub_path, labels, timeout, env=None):
            super().__init__(cfg, cwd, sub_path, labels, timeout)
            self.env = env

    mod = cluster.module()
    mod.KubernetesEnvironment = WithEnv
    monkeypatch.setitem(sys.modules, "nokker.tools.k8s_sandbox", mod)
    return cluster


def test_drapeau_allume_deux_locataires(cluster_env, fake_upstream, proxy, monkeypatch):  # noqa: F811
    monkeypatch.setenv("NOKKER_EGRESS_CONTROL_KEY", "k" * 40)
    block = code_block(cluster_env.workspace_root, kubernetes=K8S_EGRESS, egress_secrets=egress_block(proxy))
    cfg = CodeConfig.from_config(block, lokkal=False)
    assert cfg.egress_secrets is not None
    notes: list = []
    vaults = {TA: MemorySecrets(), TB: MemorySecrets()}
    vaults[TA].values["GH_TOKEN"] = VALUE
    vaults[TB].values["OTHER"] = "bob-other-value-1234"  # non déclaré : jamais accordé
    svc = _svc(cfg, vaults, notes)
    svc.egress = EgressSecrets(cfg.egress_secrets)
    with tenant_scope(TA):
        a = svc.create(ANA, {})
    with tenant_scope(TB):
        b = svc.create(CAM, {})
    _touch(svc, TA, ANA, f"{TA}:{a['id']}")
    pod_a = cluster_env.pods[-1]
    _touch(svc, TB, CAM, f"{TB}:{b['id']}")
    pod_b = cluster_env.pods[-1]
    # A : proxy avec jeton, certificat de l'AC, noms ; jamais la valeur
    assert pod_a.cfg["egress_proxy"].startswith("http://nokker:") and pod_a.cfg["egress_proxy"].endswith(
        f"@127.0.0.1:{proxy.port}")
    assert pod_a.env["NOKKER_SECRET_REFS"] == "GH_TOKEN"
    assert pod_a.env["SSL_CERT_FILE"] == "/workspace/.nokker/egress-ca.pem"
    assert VALUE not in repr(pod_a.cfg) + repr(pod_a.env)
    # B : rien de déclaré dans son coffre → environnement et proxy d'origine
    assert pod_b.cfg == {**cfg.kubernetes, "idle_ttl_s": 1800} and pod_b.env is None
    # le proxy ne connaît que la session de A, avec la valeur de A
    assert len(proxy.proxy.sessions) == 1
    # notes vues par le modèle : la note des secrets seulement pour A
    ta, tb = f"{TA}:{a['id']}", f"{TB}:{b['id']}"
    texts = {t: [x for th, x in notes if th == t] for t in (ta, tb)}
    assert texts[ta][1] == note_text(list(parse_rules(egress_block(proxy)["secrets"])))
    assert len(texts[ta]) == 2 and len(texts[tb]) == 1
    # fermeture : jeton révoqué
    with tenant_scope(TA):
        svc.close(ANA, a["id"])
    assert len(proxy.proxy.sessions) == 0
    svc.close_all()


def test_texte_de_la_note():
    rules = list(parse_rules({"GH_TOKEN": {"hosts": ["github.com", "api.github.com"], "headers": ["authorization"]},
                              "SEARCH_KEY": {"hosts": ["api.search.example"], "query": ["key"],
                                             "fields": ["auth.key"]}}))
    assert note_text(rules) == (
        "[session de code] secrets par référence : écris `secret:NOM` là où irait la valeur ; le proxy de sortie la "
        "pose au départ de la requête, seulement vers les hôtes et aux emplacements déclarés (sinon refus 403), et la "
        "remet en référence dans les réponses. La valeur n'entre jamais dans le bac à sable.\n"
        "- GH_TOKEN : github.com, api.github.com ; en-têtes authorization\n"
        "- SEARCH_KEY : api.search.example ; paramètres d'URL key ; champs du corps auth.key")
