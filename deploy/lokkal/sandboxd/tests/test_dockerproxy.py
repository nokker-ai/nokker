"""Règles du proxy docker de Lokkal : ce que le bac à sable de la bibliothèque amont envoie passe, le reste est refusé."""
import json
import os
import sys
from urllib.parse import parse_qs

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import dockerproxy as dp  # noqa: E402

INST = "0123456789abcdef"
CFG = dp.Config(workspaces=["/home/jean/projet", "/srv/code"], skills=["/home/jean/.nokker/skills"], probe_images={"hello-world",
                "ghcr.io/nokker-ai/sandbox"}, instance=INST)


def sandbox(**over):
    """Corps d'un create tel que l'envoie `docker run -d` de DockerEnvironment (b53dd80)."""
    body = {
        "Image": "ghcr.io/nokker-ai/sandbox:1", "Cmd": ["sleep", "infinity"], "WorkingDir": "/workspace",
        "Labels": {"nokker.sandbox": "fil-12", "nokker.instance": INST}, "Env": ["FOO=1", "HTTP_PROXY=http://evil:1"],
        "HostConfig": {
            "Init": True, "CapDrop": ["ALL"], "CapAdd": ["DAC_OVERRIDE", "CHOWN", "FOWNER"],
            "SecurityOpt": ["no-new-privileges"], "PidsLimit": 256, "NanoCpus": 2_000_000_000, "Memory": 1 << 31,
            "Tmpfs": {"/tmp": "", "/run": ""}, "Binds": ["/home/jean/projet:/workspace"],
            "NetworkMode": "nokker-sandbox",
        },
    }
    for k, v in over.items():
        if k.startswith("hc_"):
            body["HostConfig"][k[3:]] = v
        else:
            body[k] = v
    return body


def test_sandbox_ok_et_proxy_pose():
    out = dp.check_create(sandbox(), CFG)
    env = out["Env"]
    assert "FOO=1" in env and "HTTP_PROXY=http://egress:3128" in env
    assert not any("evil" in e for e in env)


def test_reseau_none_sans_proxy():
    out = dp.check_create(sandbox(hc_NetworkMode="none"), CFG)
    assert "HTTP_PROXY=http://egress:3128" not in out["Env"]


def test_skill_ro_ok_rw_refuse():
    dp.check_create(sandbox(hc_Binds=["/home/jean/projet:/workspace", "/home/jean/.nokker/skills/a:/skills/a:ro"]), CFG)
    with pytest.raises(dp.Refused):
        dp.check_create(sandbox(hc_Binds=["/home/jean/.nokker/skills/a:/skills/a"]), CFG)


@pytest.mark.parametrize("over", [
    {"hc_Privileged": True},
    {"hc_CapAdd": ["SYS_ADMIN"]},
    {"hc_CapAdd": ["CAP_NET_ADMIN"]},
    {"hc_SecurityOpt": []},
    {"hc_SecurityOpt": ["no-new-privileges", "seccomp=unconfined"]},
    {"hc_PidMode": "host"}, {"hc_IpcMode": "host"}, {"hc_UsernsMode": "host"}, {"hc_UTSMode": "host"},
    {"hc_PidMode": "container:abc"},
    {"hc_NetworkMode": "host"}, {"hc_NetworkMode": "bridge"}, {"hc_NetworkMode": "default"},
    {"hc_NetworkMode": "container:abc"},
    {"hc_Devices": [{"PathOnHost": "/dev/kvm"}]},
    {"hc_DeviceRequests": [{"Count": -1}]},
    {"hc_Binds": ["/:/host"]},
    {"hc_Binds": ["/var/run/docker.sock:/var/run/docker.sock"]},
    {"hc_Binds": ["/home/jean/projet/../.ssh:/x"]},
    {"hc_Binds": ["/home/jean/projet2:/x"]},
    {"hc_Binds": ["monvolume:/x"]},
    {"hc_Mounts": [{"Type": "bind", "Source": "/etc", "Target": "/e"}]},
    {"hc_Mounts": [{"Type": "volume", "Source": "v", "Target": "/v"}]},
    {"hc_Mounts": [{"Type": "volume", "Source": f"nokker-{INST}-ws", "Target": "/workspace",
                    "VolumeOptions": {"Subpath": "s/1"}}]},                  # volume de l'instance, hôte sans host_volumes
    {"hc_PortBindings": {"80/tcp": [{"HostPort": "80"}]}},
    {"hc_ExtraHosts": ["host.docker.internal:host-gateway"]},
    {"hc_Runtime": "nvidia"},
    {"hc_VolumesFrom": ["autre"]},
    {"NetworkingConfig": {"EndpointsConfig": {"nokker-lokkal_internal": {}}}},
])
def test_refus(over):
    with pytest.raises(dp.Refused):
        dp.check_create(sandbox(**over), CFG)


def test_sous_dossier_du_workspace_ok():
    dp.check_create(sandbox(hc_Binds=["/home/jean/projet/src:/workspace"]), CFG)


def _sonde_reelle():
    """Corps du create que le CLI docker envoie pour la sonde cgroup de l'amont (relevé sur docker 29.4 :
    `docker run --rm --cpus 0.5 --memory 64m --pids-limit 32 <image> sleep 0`)."""
    with open(os.path.join(os.path.dirname(__file__), "fixtures", "docker_run_sonde_cgroup.json")) as f:
        return json.load(f)


def test_sonde_cgroup_reelle_acceptee():
    out = dp.check_create(_sonde_reelle(), CFG)
    assert out["Cmd"] == ["sleep", "0"]
    assert out["Labels"]["nokker.sandbox"] == "probe"
    assert out["HostConfig"]["NetworkMode"] == "none"
    assert out["HostConfig"]["SecurityOpt"] == ["no-new-privileges"]
    assert out["HostConfig"]["Memory"] == 64 << 20 and out["HostConfig"]["NanoCpus"] == 500_000_000


def _variante(**over):
    b = _sonde_reelle()
    for k, v in over.items():
        if k.startswith("hc_"):
            b["HostConfig"][k[3:]] = v
        else:
            b[k] = v
    return b


@pytest.mark.parametrize("over", [
    {"Cmd": ["sleep", "5"]}, {"Cmd": ["sleep", "infinity"]}, {"Cmd": ["sleep", "0", "1"]}, {"Cmd": ["sleep"]},
    {"Cmd": ["true"]}, {"Cmd": ["sh", "-c", "sleep 0"]}, {"Cmd": ["sleep 0"]},
    {"hc_Binds": ["/srv/code:/w"]},                                                    # montage
    {"hc_Mounts": [{"Type": "bind", "Source": "/srv/code", "Target": "/w"}]},
    {"Volumes": {"/data": {}}},
    {"Entrypoint": ["sleep"]}, {"Entrypoint": "/bin/sh"},
    {"Image": "alpine:3.20"},                                                          # image hors liste
    {"Image": "ghcr.io/nokker-ai/evil:1"},
    {"hc_AutoRemove": False},
    {"hc_Memory": 0}, {"hc_NanoCpus": 0}, {"hc_PidsLimit": 0},                            # sans plafond : ne sonde rien
    {"hc_Memory": 4 << 30}, {"hc_NanoCpus": 8_000_000_000}, {"hc_PidsLimit": 4096},       # plus large que la sonde
    {"hc_Privileged": True}, {"hc_CapAdd": ["SYS_ADMIN"]}, {"hc_PortBindings": {"80/tcp": [{"HostPort": "80"}]}},
    {"hc_PidMode": "host"}, {"hc_Devices": [{"PathOnHost": "/dev/kvm"}]},
    {"Env": ["A=1"]}, {"User": "root"}, {"WorkingDir": "/"}, {"ExposedPorts": {"80/tcp": {}}}, {"Tty": True},
    {"hc_NetworkMode": "host"},
])
def test_sonde_cgroup_variantes_refusees(over):
    b = _variante(**over)
    if over.get("hc_NetworkMode") == "host":      # écrasé en `none` par le proxy : la sonde n'ouvre jamais un réseau
        assert dp.check_create(b, CFG)["HostConfig"]["NetworkMode"] == "none"
        return
    with pytest.raises(dp.Refused):
        dp.check_create(b, CFG)


def test_sonde_disque():
    out = dp.check_create({"Image": "hello-world", "HostConfig": {"StorageOpt": {"size": "1m"}}}, CFG)
    assert out["Labels"] == {"nokker.sandbox": "probe", "nokker.instance": INST}


@pytest.mark.parametrize("body", [
    {"Image": "alpine", "Cmd": ["true"]},                                         # image inconnue
    {"Image": "hello-world", "Cmd": ["true"]},                                    # `true` n'est plus la sonde
    {"Image": "hello-world", "Cmd": ["sh", "-c", "id"]},                          # commande
    {"Image": "hello-world", "HostConfig": {"Binds": ["/home/jean/projet:/w"]}},   # montage
    {"Image": "hello-world", "HostConfig": {"Privileged": True}},
    {"Image": "hello-world", "Entrypoint": ["/bin/sh"]},
])
def test_sans_etiquette_refuse(body):
    with pytest.raises(dp.Refused):
        dp.check_create(body, CFG)


def test_exec_privilegie_refuse():
    with pytest.raises(dp.Refused):
        dp.check_exec({"Cmd": ["id"], "Privileged": True})
    dp.check_exec({"Cmd": ["id"], "AttachStdout": True})


def test_liste_filtree():
    q = dp.filter_list_query("all=1&filters=" + json.dumps({"status": ["exited"]}), INST)
    f = json.loads(parse_qs(q)["filters"][0])
    assert "nokker.sandbox" in f["label"] and f"nokker.instance={INST}" in f["label"] and f["status"] == ["exited"]


@pytest.mark.parametrize("method,path,kind", [
    ("GET", "/v1.47/_ping", "pass"), ("POST", "/v1.47/containers/create", "create"),
    ("POST", "/v1.47/containers/abc/exec", "exec_create"), ("POST", "/v1.47/exec/123/start", "exec"),
    ("DELETE", "/v1.47/containers/hermes-1a2b3c4d", "ctr"), ("GET", "/containers/json", "list"),
])
def test_routes_ok(method, path, kind):
    assert dp.route(method, path)[0] == kind


@pytest.mark.parametrize("method,path", [
    ("POST", "/v1.47/build"), ("POST", "/v1.47/containers/abc/archive"), ("PUT", "/v1.47/containers/abc/archive"),
    ("POST", "/v1.47/containers/abc/update"), ("POST", "/v1.47/networks/create"), ("POST", "/v1.47/volumes/create"),
    ("POST", "/v1.47/swarm/init"), ("POST", "/v1.47/plugins/pull"), ("GET", "/v1.47/events"),
    ("POST", "/v1.47/containers/abc/commit"), ("POST", "/v1.47/commit"), ("POST", "/v1.47/images/load"),
    ("POST", "/v1.47/containers/../x/start"), ("GET", "/v1.47/containers/abc/export"),
])
def test_routes_refusees(method, path):
    with pytest.raises(dp.Refused):
        dp.route(method, path)


def test_second_dossier_ok():
    dp.check_create(sandbox(hc_Binds=["/srv/code/app:/workspace"]), CFG)


def test_plusieurs_dossiers_env():
    c = dp.Config.from_env({"NOKKER_HOST_WORKSPACE": "/a/b:/c", "NOKKER_INSTANCE": INST})
    assert c.workspaces == ["/a/b", "/c"]


def test_workspace_obligatoire():
    with pytest.raises(SystemExit):
        dp.Config.from_env({"NOKKER_HOST_WORKSPACE": "/", "NOKKER_INSTANCE": INST})


def _env_of(env_in):
    body = sandbox()
    body["Env"] = env_in
    return dp.check_create(body, CFG)["Env"]


def test_identite_du_noyau_gardee_si_meme_proxy():
    env = _env_of(["HTTPS_PROXY=http://nokker:jeton123@egress:3128", "HTTP_PROXY=http://nokker:jeton123@egress:3128/"])
    assert "HTTPS_PROXY=http://nokker:jeton123@egress:3128" in env
    assert "HTTP_PROXY=http://nokker:jeton123@egress:3128/" in env
    assert "https_proxy=http://egress:3128" in env       # absente côté noyau : valeur d'egress


def test_autre_proxy_ou_identite_ecrases():
    for v in ("http://nokker:j@evil:3128", "http://nokker:j@egress:3129", "https://nokker:j@egress:3128",
              "http://root:j@egress:3128", "http://nokker@egress:3128", "http://nokker:j@egress:3128/x",
              "http://nokker:j@egress:3128?a=1", "http://egress:3128@evil:1"):
        env = _env_of([f"HTTPS_PROXY={v}"])
        assert "HTTPS_PROXY=http://egress:3128" in env, v
        assert f"HTTPS_PROXY={v}" not in env, v



def test_proxy_ssh_pose_par_le_proxy_seulement():
    """Client ssh des outils `ssh_*` : `NOKKER_SSH_PROXY` = hôte:port d'egress, jamais la valeur du noyau."""
    env = dp.check_create(sandbox(), CFG)["Env"]
    assert "NOKKER_SSH_PROXY=egress:3128" in env
    env = _env_of(["NOKKER_SSH_PROXY=evil:22", "nokker_ssh_proxy=evil:1"])
    assert [e for e in env if e.lower().startswith("nokker_ssh_proxy=")] == ["NOKKER_SSH_PROXY=egress:3128"]
    assert not any(e.startswith("NOKKER_SSH_PROXY") for e in dp.check_create(sandbox(hc_NetworkMode="none"),
                                                                              CFG)["Env"])


# ---- volumes nommés (host_volumes) : seulement `nokker-<instance>-<nom>` + sous-dossier, et seulement à la demande
VCFG = dp.Config(workspaces=["/srv/code"], instance=INST, host_volumes=True)
VNAME = f"nokker-{INST}-ws"


def vol(**over):
    """Ce que le CLI docker envoie pour `--mount type=volume,source=…,target=/workspace,volume-subpath=sess/1`
    (relevé sur docker 29.4)."""
    m = {"Type": "volume", "Source": VNAME, "Target": "/workspace", "VolumeOptions": {"Subpath": "sess/1"}}
    m.update(over)
    return sandbox(hc_Binds=None, hc_Mounts=[m])


def test_volume_de_l_instance_avec_sous_dossier_accepte():
    out = dp.check_create(vol(), VCFG)
    assert out["HostConfig"]["Mounts"][0]["VolumeOptions"] == {"Subpath": "sess/1"}
    dp.check_create(vol(ReadOnly=True), VCFG)
    dp.check_create(vol(VolumeOptions={"Subpath": "a_b.c-d/e@1+2=3"}), VCFG)


@pytest.mark.parametrize("over", [
    {"Source": "nokker-ffffffffffffffff-ws"},            # volume d'une autre instance
    {"Source": f"nokker-{INST}"},                         # le préfixe sans nom
    {"Source": f"nokker-{INST}-"},
    {"Source": f"nokker-{INST}x-ws"},                     # préfixe d'une instance plus longue
    {"Source": "ws"},                                     # nom nu
    {"Source": f"{VNAME}/../x"},
    {"Source": f"xnokker-{INST}-ws"},
    {"VolumeOptions": None}, {"VolumeOptions": {}},      # volume entier
    {"VolumeOptions": {"Subpath": ""}},
    {"VolumeOptions": {"Subpath": "../autre"}}, {"VolumeOptions": {"Subpath": "a/../../b"}},
    {"VolumeOptions": {"Subpath": "a/.."}}, {"VolumeOptions": {"Subpath": "/etc"}},
    {"VolumeOptions": {"Subpath": "a//b"}}, {"VolumeOptions": {"Subpath": "./a"}},
    {"VolumeOptions": {"Subpath": "a b"}}, {"VolumeOptions": {"Subpath": "a\nb"}},
    {"VolumeOptions": {"Subpath": "s", "NoCopy": True}},
    {"VolumeOptions": {"Subpath": "s", "DriverConfig": {"Name": "local", "Options": {"device": "/"}}}},
    {"VolumeOptions": {"Subpath": "s", "Labels": {"a": "b"}}},
    {"Target": "relatif"}, {"Target": "/"}, {"Target": "/a/../b"},
    {"BindOptions": {"Propagation": "rshared"}}, {"TmpfsOptions": {"SizeBytes": 1}},
])
def test_volume_refuse(over):
    with pytest.raises(dp.Refused):
        dp.check_create(vol(**over), VCFG)


def test_volume_en_bind_refuse_meme_avec_host_volumes():
    for b in (f"{VNAME}:/workspace", f"{VNAME}:/workspace:ro", "ws:/workspace"):
        with pytest.raises(dp.Refused):
            dp.check_create(sandbox(hc_Binds=[b]), VCFG)


def test_bind_mount_hors_workspace_toujours_refuse_avec_host_volumes():
    with pytest.raises(dp.Refused):
        dp.check_create(vol(Type="bind", Source="/etc", Target="/e"), VCFG)


def test_volume_refuse_sans_host_volumes():
    with pytest.raises(dp.Refused, match="NOKKER_HOST_VOLUMES"):
        dp.check_create(vol(), CFG)


def test_host_volumes_par_l_environnement():
    env = {"NOKKER_HOST_WORKSPACE": "/a", "NOKKER_INSTANCE": INST}
    assert dp.Config.from_env(env).host_volumes is False
    assert dp.Config.from_env({**env, "NOKKER_HOST_VOLUMES": "1"}).host_volumes is True
    assert dp.Config.from_env({**env, "NOKKER_HOST_VOLUMES": "0"}).host_volumes is False
