"""Règles du proxy docker de Lokkal : ce que le bac à sable de Hermes envoie passe, le reste est refusé."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import dockerproxy as dp  # noqa: E402

CFG = dp.Config(workspaces=["/home/jean/projet", "/srv/code"], skills=["/home/jean/.nokker/skills"], probe_images={"hello-world",
                "ghcr.io/nokker-ai/sandbox"})


def sandbox(**over):
    """Corps d'un create tel que l'envoie `docker run -d` de DockerEnvironment (b53dd80)."""
    body = {
        "Image": "ghcr.io/nokker-ai/sandbox:1", "Cmd": ["sleep", "infinity"], "WorkingDir": "/workspace",
        "Labels": {"nokker.sandbox": "fil-12"}, "Env": ["FOO=1", "HTTP_PROXY=http://evil:1"],
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


def test_sonde_cgroup():
    out = dp.check_create({"Image": "ghcr.io/nokker-ai/sandbox:1", "Cmd": ["true"],
                           "HostConfig": {"AutoRemove": True, "NanoCpus": 500_000_000, "Memory": 64 << 20,
                                          "PidsLimit": 32}}, CFG)
    assert out["Labels"]["nokker.sandbox"] == "probe"
    assert out["HostConfig"]["NetworkMode"] == "none"
    assert out["HostConfig"]["SecurityOpt"] == ["no-new-privileges"]


def test_sonde_disque():
    out = dp.check_create({"Image": "hello-world", "HostConfig": {"StorageOpt": {"size": "1m"}}}, CFG)
    assert out["Labels"]["nokker.sandbox"] == "probe"


@pytest.mark.parametrize("body", [
    {"Image": "alpine", "Cmd": ["true"]},                                         # image inconnue
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
    q = dp.filter_list_query("all=1&filters=" + json.dumps({"status": ["exited"]}))
    f = json.loads(dict(x.split("=", 1) for x in q.split("&"))["filters"].replace("%22", '"').replace("%3A", ":")
                   .replace("%5B", "[").replace("%5D", "]").replace("%7B", "{").replace("%7D", "}")
                   .replace("+", " ").replace("%2C", ","))
    assert "nokker.sandbox" in f["label"] and f["status"] == ["exited"]


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
    c = dp.Config.from_env({"NOKKER_HOST_WORKSPACE": "/a/b:/c"})
    assert c.workspaces == ["/a/b", "/c"]


def test_workspace_obligatoire():
    with pytest.raises(SystemExit):
        dp.Config.from_env({"NOKKER_HOST_WORKSPACE": "/"})
