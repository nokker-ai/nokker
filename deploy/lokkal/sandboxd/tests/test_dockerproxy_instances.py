"""Deux instances de noyau, deux proxys, UN SEUL (faux) démon : chacune ne voit, n'atteint et ne supprime que ses bacs."""
import asyncio
import json
import os
import re
import sys
import tempfile
from urllib.parse import quote

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import dockerproxy as dp

A, B = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"


class FakeDaemon:
    """Le strict nécessaire de l'API docker : create/inspect/liste/start/stop/kill/wait/rm/logs/exec, en mémoire."""

    def __init__(self, path):
        self.path, self.ctrs, self.execs, self.calls, self.n = path, {}, {}, [], 0

    async def serve(self):
        self.server = await asyncio.start_unix_server(self.on_client, self.path)

    async def on_client(self, r, w):
        head = await dp.read_head(r)
        line, headers = head
        method, target, _ = line.split(" ", 2)
        body = await dp.read_body(r, headers)
        path, _, query = target.partition("?")
        path = dp._VER.sub("", path, count=1)
        self.calls.append((method, path, query))
        status, out = self.dispatch(method, path, query, json.loads(body) if body else None)
        data = json.dumps(out).encode()
        w.write(f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n"
                f"Connection: close\r\n\r\n".encode() + data)
        await w.drain()
        w.close()

    def dispatch(self, method, path, query, body):
        if (method, path) == ("POST", "/containers/create"):
            self.n += 1
            cid = f"c{self.n:02d}"
            self.ctrs[cid] = {"Id": cid, "Config": {"Labels": (body or {}).get("Labels") or {}}, "State": {"Running": True}}
            return 201, {"Id": cid}
        if (method, path) == ("GET", "/containers/json"):
            from urllib.parse import parse_qs
            labs = json.loads(parse_qs(query).get("filters", ["{}"])[0]).get("label", [])
            def ok(c):
                lb = c["Config"]["Labels"]
                return all((k in lb) if "=" not in k else lb.get(k.split("=", 1)[0]) == k.split("=", 1)[1] for k in labs)
            return 200, [c for c in self.ctrs.values() if ok(c)]
        m = re.fullmatch(r"/containers/([^/]+)(?:/(\w+))?", path)
        if m and m.group(1) != "json":
            cid, act = m.groups()
            c = self.ctrs.get(cid)
            if c is None:
                return 404, {"message": "No such container"}
            if method == "DELETE":
                del self.ctrs[cid]
                return 204, None
            if act == "exec":
                eid = f"e{len(self.execs) + 1:02d}"
                self.execs[eid] = cid
                return 201, {"Id": eid}
            return 200, c if act in (None, "json") else {}
        m = re.fullmatch(r"/exec/([^/]+)/(\w+)", path)
        if m:
            return (200, {"ExitCode": 0}) if m.group(1) in self.execs else (404, {"message": "No such exec"})
        return 404, {"message": "?"}


async def http(port, method, path, body=None):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    data = json.dumps(body).encode() if body is not None else b""
    w.write((f"{method} {path} HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n"
             f"Connection: close\r\n\r\n").encode() + data)
    await w.drain()
    raw = await r.read()
    w.close()
    head, _, payload = raw.partition(b"\r\n\r\n")
    status = int(head.split()[1])
    return status, (json.loads(payload) if payload.strip() else None)


def sandbox_body(instance, name="fil-1", **labels):
    labs = {"nokker.sandbox": name, **({"nokker.instance": instance} if instance else {}), **labels}
    return {"Image": "ghcr.io/nokker-ai/sandbox:1", "Cmd": ["sleep", "infinity"], "Labels": labs,
            "HostConfig": {"SecurityOpt": ["no-new-privileges"], "CapDrop": ["ALL"], "NetworkMode": "none",
                           "Binds": ["/srv/ws/a:/workspace"]}}


def with_world(fn):
    """fn(daemon, port_a, port_b) dans une boucle : un démon, deux proxys (un par instance), leurs exec à eux."""
    async def run():
        with tempfile.TemporaryDirectory() as d:
            sock = os.path.join(d, "docker.sock")
            daemon = FakeDaemon(sock)
            await daemon.serve()
            ports, servers = [], []
            for inst in (A, B):
                cfg = dp.Config(upstream=sock, workspaces=["/srv/ws"], instance=inst,
                                probe_images={"ghcr.io/nokker-ai/sandbox"})
                execs: dict = {}
                async def on_client(rd, wr, cfg=cfg, execs=execs):
                    await dp.Proxy(cfg, execs, rd).handle(rd, wr)
                srv = await asyncio.start_server(on_client, "127.0.0.1", 0)
                servers.append(srv)
                ports.append(srv.sockets[0].getsockname()[1])
            try:
                await fn(daemon, *ports)
            finally:
                for s in servers:
                    s.close()
                daemon.server.close()
    asyncio.run(run())


def test_chaque_instance_agit_sur_ses_bacs():
    async def fn(daemon, pa, pb):
        st, a = await http(pa, "POST", "/containers/create", sandbox_body(A))
        assert st == 201
        for method, path in [("GET", "/json"), ("POST", "/start"), ("POST", "/stop"), ("POST", "/kill"),
                             ("POST", "/wait"), ("POST", "/resize"), ("GET", "/logs")]:
            st, _ = await http(pa, method, f"/containers/{a['Id']}{path}")
            assert st == 200, path
        st, e = await http(pa, "POST", f"/containers/{a['Id']}/exec", {"Cmd": ["id"]})
        assert st == 201
        assert (await http(pa, "POST", f"/exec/{e['Id']}/start", {}))[0] == 200
        assert (await http(pa, "DELETE", f"/containers/{a['Id']}"))[0] == 204
    with_world(fn)


def test_B_ne_touche_pas_au_bac_de_A_et_inversement():
    async def fn(daemon, pa, pb):
        _, a = await http(pa, "POST", "/containers/create", sandbox_body(A))
        _, b = await http(pb, "POST", "/containers/create", sandbox_body(B))
        # cet exec de A existe : B ne peut pas le piloter non plus (ses exec sont ceux créés par SON proxy)
        _, ea = await http(pa, "POST", f"/containers/{a['Id']}/exec", {"Cmd": ["id"]})
        for port, mine, other in ((pb, b, a), (pa, a, b)):
            oid = other["Id"]
            for method, path in [("GET", ""), ("GET", "/json"), ("POST", "/start"), ("POST", "/stop"),
                                 ("POST", "/kill"), ("POST", "/wait"), ("POST", "/resize"), ("GET", "/logs"),
                                 ("POST", "/attach")]:
                st, _ = await http(port, method, f"/containers/{oid}{path}" if path else f"/containers/{oid}/json")
                assert st == 403, (method, path, st)
            assert (await http(port, "POST", f"/containers/{oid}/exec", {"Cmd": ["id"]}))[0] == 403
            assert (await http(port, "DELETE", f"/containers/{oid}"))[0] == 403
        assert (await http(pb, "POST", f"/exec/{ea['Id']}/start", {}))[0] == 403
        assert (await http(pb, "GET", f"/exec/{ea['Id']}/json"))[0] == 403
        # rien n'a été supprimé ni exécuté chez le voisin
        assert set(daemon.ctrs) == {a["Id"], b["Id"]}
        assert not any(c[0] == "DELETE" for c in daemon.calls)
        assert sum(1 for c in daemon.calls if c[1].endswith("/exec")) == 1     # seul l'exec de A à A
    with_world(fn)


def test_conteneur_sans_etiquette_ou_sans_instance_refuse():
    async def fn(daemon, pa, pb):
        daemon.ctrs["nu"] = {"Id": "nu", "Config": {"Labels": {}}, "State": {}}
        daemon.ctrs["sansinst"] = {"Id": "sansinst", "Config": {"Labels": {"nokker.sandbox": "x"}}, "State": {}}
        daemon.ctrs["autre"] = {"Id": "autre", "Config": {"Labels": {"nokker.instance": A}}, "State": {}}  # pas de .sandbox
        for cid in ("nu", "sansinst", "autre"):
            assert (await http(pa, "GET", f"/containers/{cid}/json"))[0] == 403
            assert (await http(pa, "POST", f"/containers/{cid}/exec", {"Cmd": ["id"]}))[0] == 403
            assert (await http(pa, "DELETE", f"/containers/{cid}"))[0] == 403
        assert not any(c[0] == "DELETE" for c in daemon.calls)
    with_world(fn)


def test_inexistant_et_etranger_meme_reponse():
    async def fn(daemon, pa, pb):
        _, b = await http(pb, "POST", "/containers/create", sandbox_body(B))
        s1 = await http(pa, "GET", f"/containers/{b['Id']}/json")
        s2 = await http(pa, "GET", "/containers/nexistepas/json")
        assert s1[0] == s2[0] == 403
        assert s1[1]["message"].replace(b["Id"], "X") == s2[1]["message"].replace("nexistepas", "X")
    with_world(fn)


@pytest.mark.parametrize("inst", [None, B, "", "AAAAAAAAAAAAAAAA", A + "x"])
def test_creation_sans_son_etiquette_refusee(inst):
    async def fn(daemon, pa, pb):
        st, out = await http(pa, "POST", "/containers/create", sandbox_body(inst))
        assert st == 403 and "nokker.instance" in out["message"]
        assert not daemon.ctrs
    with_world(fn)


def test_creation_sans_aucune_etiquette_de_bac_refusee():
    async def fn(daemon, pa, pb):
        b = sandbox_body(A)
        b["Labels"] = {"nokker.instance": A}
        st, _ = await http(pa, "POST", "/containers/create", b)
        assert st == 403 and not daemon.ctrs
    with_world(fn)


def test_liste_limitee_a_l_instance():
    async def fn(daemon, pa, pb):
        _, a = await http(pa, "POST", "/containers/create", sandbox_body(A))
        _, b = await http(pb, "POST", "/containers/create", sandbox_body(B))
        daemon.ctrs["nu"] = {"Id": "nu", "Config": {"Labels": {}}, "State": {}}
        assert [c["Id"] for c in (await http(pa, "GET", "/containers/json?all=1"))[1]] == [a["Id"]]
        assert [c["Id"] for c in (await http(pb, "GET", "/containers/json?all=1"))[1]] == [b["Id"]]
        # le client demande l'étiquette du voisin : ET avec la sienne, liste vide
        q = "all=1&filters=" + quote(json.dumps({"label": [f"nokker.instance={B}"]}))
        assert (await http(pa, "GET", "/containers/json?" + q))[1] == []
    with_world(fn)


def test_sonde_prend_l_instance_du_proxy_et_ses_bacs_lui_sont_propres():
    with open(os.path.join(os.path.dirname(__file__), "fixtures", "docker_run_sonde_cgroup.json")) as f:
        probe = json.load(f)

    async def fn(daemon, pa, pb):
        probe["Labels"] = {"nokker.instance": B}     # une étiquette d'une autre instance posée par le client : écrasée
        st, out = await http(pa, "POST", "/containers/create", probe)
        assert st == 201
        assert daemon.ctrs[out["Id"]]["Config"]["Labels"] == {"nokker.sandbox": "probe", "nokker.instance": A}
        assert (await http(pb, "GET", f"/containers/{out['Id']}/json"))[0] == 403
        assert (await http(pa, "GET", f"/containers/{out['Id']}/json"))[0] == 200
    with_world(fn)


def test_instance_obligatoire_au_demarrage():
    for bad in ("", "court", "a b c d e f", "x" * 129):
        with pytest.raises(SystemExit):
            dp.Config.from_env({"NOKKER_HOST_WORKSPACE": "/a", "NOKKER_INSTANCE": bad})
    with pytest.raises(SystemExit):
        dp.Config.from_env({"NOKKER_HOST_WORKSPACE": "/a"})
    assert dp.Config.from_env({"NOKKER_HOST_WORKSPACE": "/a", "NOKKER_INSTANCE": A}).instance == A
