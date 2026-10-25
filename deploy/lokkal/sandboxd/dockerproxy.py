"""Proxy filtrant du socket docker pour le noyau Lokkal (compose Lokkal, service `dockerproxy`).

Le noyau lance un bac à sable par fil (`nokker/tools/code.py`, DockerEnvironment de la bibliothèque amont, CLI docker). Lui donner
le socket brut, c'est lui donner l'hôte : `docker run --privileged -v /:/host`. Ce proxy est la seule voie du noyau
vers le démon (DOCKER_HOST=tcp://dockerproxy:2375, sur un réseau interne que seuls eux deux partagent) et n'accepte
que ce dont le bac à sable a besoin :

- sondes : _ping, version, info ; image inspect et pull ;
- containers ÉTIQUETÉS `nokker.sandbox` seulement : create (vérifié, voir `check_create`), start, attach, wait,
  inspect, exec, stop, kill, rm, liste (filtrée sur l'étiquette) ;
- exec : seulement ceux créés par ce proxy, jamais privilégiés.

Tout le reste répond 403. Un create est refusé s'il demande : privileged, une capacité hors {DAC_OVERRIDE, CHOWN,
FOWNER}, un espace de noms de l'hôte, un périphérique, un montage hors du dossier de travail (rw) et des dossiers de
skills (ro), un port publié, un autre réseau que `nokker-sandbox` ou `none`, ou s'il oublie no-new-privileges.
Le proxy ajoute lui-même HTTP(S)_PROXY vers le proxy de sortie (`egress`) : le réseau `nokker-sandbox` est interne,
c'est la seule sortie, et elle refuse les plages privées.

Les deux sondes de la bibliothèque amont (limites cgroup, quota disque) créent un container sans étiquette : acceptées si elles n'ont
ni montage ni commande autre que `true`, sur une image connue ; le proxy leur pose l'étiquette et le réseau `none`.

Bibliothèque standard seulement. Configuration par l'environnement (voir `Config.from_env`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import posixpath
import re
import sys
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, urlencode, urlsplit

log = logging.getLogger("dockerproxy")

LABEL = "nokker.sandbox"
ALLOWED_CAPS = {"DAC_OVERRIDE", "CHOWN", "FOWNER"}
NNP = {"no-new-privileges", "no-new-privileges:true", "no-new-privileges=true"}
MAX_BODY = 1 << 20  # un create fait quelques Ko ; au-delà, refus
_VER = re.compile(r"^/v\d+\.\d+(?=/)")
_ID = r"([A-Za-z0-9][A-Za-z0-9_.-]{0,127})"


@dataclass
class Config:
    upstream: str = "/var/run/docker.sock"
    listen_host: str = "0.0.0.0"
    listen_port: int = 2375
    workspaces: list[str] = field(default_factory=list)  # NOKKER_HOST_WORKSPACE (« : ») : dossiers vus par le démon
    skills: list[str] = field(default_factory=list)  # dossiers de skills montables en lecture seule
    network: str = "nokker-sandbox"
    egress_proxy: str = "http://egress:3128"
    probe_images: set[str] = field(default_factory=lambda: {"hello-world"})

    @classmethod
    def from_env(cls, env=os.environ) -> "Config":
        c = cls(
            upstream=env.get("DOCKER_UPSTREAM", cls.upstream),
            listen_host=env.get("LISTEN_HOST", cls.listen_host),
            listen_port=int(env.get("LISTEN_PORT", cls.listen_port)),
            workspaces=[_norm(p) for p in env.get("NOKKER_HOST_WORKSPACE", "").split(":") if p.strip()],
            skills=[_norm(p) for p in env.get("NOKKER_HOST_SKILLS", "").split(":") if p.strip()],
            network=env.get("NOKKER_SANDBOX_NETWORK", cls.network),
            egress_proxy=env.get("NOKKER_SANDBOX_EGRESS", cls.egress_proxy),
        )
        c.probe_images |= {i.strip() for i in env.get("NOKKER_SANDBOX_IMAGES", "").split(",") if i.strip()}
        if not c.workspaces or "/" in c.workspaces:
            raise SystemExit("NOKKER_HOST_WORKSPACE absent ou « / » : refus de démarrer")
        return c


class Refused(Exception):
    pass


def _norm(p: str) -> str:
    p = p.strip()
    if not p:
        return ""
    if not p.startswith("/") or "\x00" in p or any(part == ".." for part in p.split("/")):
        raise Refused(f"chemin refusé : {p!r}")
    return posixpath.normpath(p)


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _image_name(ref: str) -> str:
    """`docker.io/library/hello-world:latest` → `hello-world` ; garde le dépôt pour les autres."""
    ref = ref.split("@", 1)[0]
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        ref = ref[: len(ref) - len(last)] + last.split(":", 1)[0]
    for p in ("docker.io/library/", "library/", "docker.io/"):
        if ref.startswith(p):
            ref = ref[len(p):]
    return ref


# --------------------------------------------------------------------------- règles du create
def _check_bind(src: str, dst: str, ro: bool, cfg: Config) -> None:
    s = _norm(src)
    if any(_under(s, w) for w in cfg.workspaces):
        return
    if any(_under(s, k) for k in cfg.skills):
        if not ro:
            raise Refused(f"skill monté en écriture : {src}")
        return
    raise Refused(f"montage hors du dossier de travail : {src} → {dst}")


def check_create(body: dict, cfg: Config) -> dict:
    """Valide (et complète) le corps d'un POST /containers/create. Renvoie le corps à transmettre."""
    hc = body.setdefault("HostConfig", {}) or {}
    body["HostConfig"] = hc
    labels = body.get("Labels") or {}
    cmd = body.get("Cmd")
    probe = LABEL not in labels
    if probe:
        # Sondes de la bibliothèque amont : aucun montage, `true` ou la commande par défaut de l'image, image connue.
        if hc.get("Binds") or hc.get("Mounts") or body.get("Volumes"):
            raise Refused("container sans étiquette nokker.sandbox avec montage")
        if cmd not in (None, [], ["true"]) or body.get("Entrypoint") not in (None, [], ""):
            raise Refused("container sans étiquette nokker.sandbox : commande non autorisée")
        if _image_name(body.get("Image", "")) not in {_image_name(i) for i in cfg.probe_images}:
            raise Refused(f"container sans étiquette nokker.sandbox : image {body.get('Image')!r} non autorisée")
        body["Labels"] = {**labels, LABEL: "probe"}
        hc["NetworkMode"] = "none"
        body.pop("NetworkingConfig", None)

    if hc.get("Privileged"):
        raise Refused("privileged")
    caps = {c.upper().removeprefix("CAP_") for c in (hc.get("CapAdd") or [])}
    if caps - ALLOWED_CAPS:
        raise Refused(f"capacités refusées : {sorted(caps - ALLOWED_CAPS)}")
    sec = hc.get("SecurityOpt") or []
    if any(o not in NNP for o in sec):
        raise Refused(f"security-opt refusée : {[o for o in sec if o not in NNP]}")
    if not probe and not any(o in NNP for o in sec):
        raise Refused("no-new-privileges obligatoire")
    if probe and not sec:
        hc["SecurityOpt"] = ["no-new-privileges"]
    for k in ("PidMode", "IpcMode", "UTSMode", "UsernsMode", "CgroupnsMode"):
        v = (hc.get(k) or "").lower()
        if v == "host" or v.startswith("container:"):
            raise Refused(f"{k}={v}")
    for k in ("Devices", "DeviceRequests", "DeviceCgroupRules", "VolumesFrom", "Links", "ExtraHosts",
              "Sysctls", "CgroupParent", "PortBindings", "VolumeDriver"):
        if hc.get(k):
            raise Refused(f"{k} refusé")
    if hc.get("PublishAllPorts") or hc.get("OomKillDisable"):
        raise Refused("PublishAllPorts / OomKillDisable refusés")
    if (hc.get("Runtime") or "runc") not in ("runc", "runsc"):
        raise Refused(f"runtime {hc.get('Runtime')!r} refusé")

    for b in hc.get("Binds") or []:
        parts = b.split(":")
        if len(parts) < 2:
            raise Refused(f"bind sans destination : {b}")   # volume anonyme : passer par Volumes
        opts = parts[2].split(",") if len(parts) > 2 else []
        if not parts[0].startswith("/"):
            raise Refused(f"volume nommé refusé : {parts[0]}")
        _check_bind(parts[0], parts[1], "ro" in opts, cfg)
    for m in hc.get("Mounts") or []:
        t = m.get("Type", "volume")
        if t == "tmpfs":
            continue
        if t != "bind":
            raise Refused(f"montage de type {t} refusé")
        _check_bind(m.get("Source", ""), m.get("Target", ""), bool(m.get("ReadOnly")), cfg)

    if not probe:
        net = hc.get("NetworkMode") or "default"
        if net not in (cfg.network, "none"):
            raise Refused(f"réseau {net!r} refusé (seulement {cfg.network} ou none)")
        eps = ((body.get("NetworkingConfig") or {}).get("EndpointsConfig") or {})
        if set(eps) - {cfg.network, "none"}:
            raise Refused(f"réseaux refusés : {sorted(set(eps) - {cfg.network, 'none'})}")
        if net == cfg.network:
            env = [e for e in (body.get("Env") or []) if e.split("=", 1)[0].lower() not in
                   ("http_proxy", "https_proxy", "no_proxy", "all_proxy")]
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                env.append(f"{k}={cfg.egress_proxy}")
            env += ["NO_PROXY=localhost,127.0.0.1", "no_proxy=localhost,127.0.0.1"]
            body["Env"] = env
    return body


def check_exec(body: dict) -> dict:
    if body.get("Privileged"):
        raise Refused("exec privilégié")
    return body


def filter_list_query(query: str) -> str:
    """GET /containers/json : on ne laisse voir que les bacs à sable."""
    q = parse_qs(query, keep_blank_values=True)
    try:
        filters = json.loads(q.get("filters", ["{}"])[0] or "{}")
    except ValueError:
        raise Refused("filtres illisibles")
    lab = filters.get("label") or []
    if isinstance(lab, dict):  # ancien format {"k=v": true}
        lab = [k for k, v in lab.items() if v]
    if LABEL not in lab and not any(x.startswith(LABEL + "=") for x in lab):
        lab = [*lab, LABEL]
    filters["label"] = lab
    q["filters"] = [json.dumps(filters)]
    return urlencode({k: v[-1] for k, v in q.items()})


# --------------------------------------------------------------------------- routes
Route = tuple[str, re.Pattern, str]
ROUTES: list[Route] = [(m, re.compile("^" + p + "$"), name) for m, p, name in [
    ("GET", r"/_ping", "pass"), ("HEAD", r"/_ping", "pass"), ("GET", r"/version", "pass"), ("GET", r"/info", "pass"),
    ("GET", r"/images/(.+)/json", "pass"), ("POST", r"/images/create", "pull"),
    ("GET", r"/distribution/(.+)/json", "pass"),
    ("POST", r"/containers/create", "create"),
    ("GET", r"/containers/json", "list"),
    ("GET", rf"/containers/{_ID}/json", "ctr"), ("POST", rf"/containers/{_ID}/start", "ctr"),
    ("POST", rf"/containers/{_ID}/attach", "ctr"), ("POST", rf"/containers/{_ID}/wait", "ctr"),
    ("POST", rf"/containers/{_ID}/stop", "ctr"), ("POST", rf"/containers/{_ID}/kill", "ctr"),
    ("POST", rf"/containers/{_ID}/resize", "ctr"), ("DELETE", rf"/containers/{_ID}", "ctr"),
    ("GET", rf"/containers/{_ID}/logs", "ctr"),
    ("POST", rf"/containers/{_ID}/exec", "exec_create"),
    ("POST", rf"/exec/{_ID}/start", "exec"), ("GET", rf"/exec/{_ID}/json", "exec"),
    ("POST", rf"/exec/{_ID}/resize", "exec"),
]]


def route(method: str, path: str) -> tuple[str, str | None]:
    path = _VER.sub("", path, count=1)
    for m, rx, name in ROUTES:
        if m == method and (mm := rx.match(path)):
            return name, (mm.group(1) if mm.groups() else None)
    raise Refused(f"{method} {path} non autorisé")


# --------------------------------------------------------------------------- HTTP minimal
async def read_head(reader: asyncio.StreamReader) -> tuple[str, list[tuple[str, str]]] | None:
    try:
        raw = await reader.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError:
        return None
    except asyncio.LimitOverrunError:
        raise Refused("en-têtes trop longs")
    lines = raw.decode("latin-1").split("\r\n")
    headers = []
    for ln in lines[1:]:
        if ln:
            k, _, v = ln.partition(":")
            headers.append((k.strip(), v.strip()))
    return lines[0], headers


def hget(headers, name, default=None):
    name = name.lower()
    for k, v in headers:
        if k.lower() == name:
            return v
    return default


async def read_body(reader, headers) -> bytes:
    if (hget(headers, "transfer-encoding") or "").lower() == "chunked":
        out = bytearray()
        while True:
            size = int((await reader.readuntil(b"\r\n")).split(b";")[0], 16)
            if size == 0:
                await reader.readuntil(b"\r\n")
                return bytes(out)
            out += await reader.readexactly(size)
            await reader.readexactly(2)
            if len(out) > MAX_BODY:
                raise Refused("corps trop long")
    n = int(hget(headers, "content-length", "0") or 0)
    if n > MAX_BODY:
        raise Refused("corps trop long")
    return await reader.readexactly(n) if n else b""


def build_request(method, target, headers, body: bytes) -> bytes:
    drop = {"content-length", "transfer-encoding", "connection", "host", "proxy-connection", "keep-alive"}
    h = [(k, v) for k, v in headers if k.lower() not in drop]
    upgrade = hget(headers, "upgrade")
    h.append(("Host", "docker"))
    h.append(("Connection", "Upgrade" if upgrade else "close"))
    if body or method in ("POST", "PUT"):
        h.append(("Content-Length", str(len(body))))
    head = f"{method} {target} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h) + "\r\n"
    return head.encode("latin-1") + body


async def deny(writer, status: int, msg: str) -> None:
    payload = json.dumps({"message": f"nokker dockerproxy : {msg}"}).encode()
    reason = {403: "Forbidden", 400: "Bad Request", 502: "Bad Gateway"}.get(status, "Error")
    writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
                 f"Connection: close\r\n\r\n".encode() + payload)
    try:
        await writer.drain()
    finally:
        writer.close()


async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while chunk := await src.read(65536):
            dst.write(chunk)
            await dst.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            if dst.can_write_eof():
                dst.write_eof()
        except (OSError, RuntimeError):
            pass


class Proxy:
    def __init__(self, cfg: Config, execs: dict[str, None], client_reader: asyncio.StreamReader | None = None):
        self.cfg = cfg
        self.execs = execs                 # exec créés par ce proxy sur un bac à sable (partagé, ordre d'insertion)
        self._client_reader = client_reader

    async def upstream(self, raw: bytes):
        r, w = await asyncio.open_unix_connection(self.cfg.upstream, limit=1 << 20)
        w.write(raw)
        await w.drain()
        return r, w

    async def call_json(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict | list | None]:
        data = json.dumps(body).encode() if body is not None else b""
        h = [("Content-Type", "application/json")] if body is not None else []
        r, w = await self.upstream(build_request(method, path, h, data))
        try:
            status_line, headers = await read_head(r)
            status = int(status_line.split()[1])
            payload = await read_body(r, headers) if hget(headers, "content-length") or \
                hget(headers, "transfer-encoding") else await r.read()
            return status, (json.loads(payload) if payload.strip() else None)
        finally:
            w.close()

    async def is_sandbox(self, ref: str) -> bool:
        status, info = await self.call_json("GET", f"/containers/{quote(ref, safe='')}/json")
        return status == 200 and LABEL in ((info or {}).get("Config", {}).get("Labels") or {})

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await read_head(reader)
            if head is None:
                writer.close()
                return
            request_line, headers = head
            try:
                method, target, _ = request_line.split(" ", 2)
            except ValueError:
                return await deny(writer, 400, "requête illisible")
            parts = urlsplit(target)
            body = await read_body(reader, headers)
            kind, ref = route(method, parts.path)
            query = parts.query
            if kind == "create":
                body = json.dumps(check_create(json.loads(body or b"{}"), self.cfg)).encode()
                headers = [(k, v) for k, v in headers if k.lower() != "content-type"] + \
                          [("Content-Type", "application/json")]
            elif kind == "list":
                query = filter_list_query(query)
            elif kind == "ctr":
                if not await self.is_sandbox(ref):
                    raise Refused(f"container {ref} hors bac à sable")
            elif kind == "exec_create":
                if not await self.is_sandbox(ref):
                    raise Refused(f"container {ref} hors bac à sable")
                body = json.dumps(check_exec(json.loads(body or b"{}"))).encode()
            elif kind == "exec":
                if ref not in self.execs:
                    raise Refused(f"exec {ref} inconnu")
            elif kind == "pull":
                q = parse_qs(query)
                if q.get("fromSrc"):
                    raise Refused("import d'image refusé")
            new_target = parts.path + (f"?{query}" if query else "")
            log.info("%s %s", method, new_target)
            await self.forward(method, new_target, headers, body, writer, kind)
        except Refused as e:
            log.warning("refus : %s", e)
            await deny(writer, 403, str(e))
        except (ValueError, json.JSONDecodeError) as e:
            await deny(writer, 400, f"corps illisible : {e}")
        except (ConnectionError, FileNotFoundError) as e:
            await deny(writer, 502, f"démon docker injoignable : {e}")
        except asyncio.IncompleteReadError:
            writer.close()

    async def forward(self, method, target, headers, body, writer, kind) -> None:
        ur, uw = await self.upstream(build_request(method, target, headers, body))
        try:
            status_line, rheaders = await read_head(ur)
            status = int(status_line.split()[1])
            if kind == "exec_create" and status == 201:
                payload = await read_body(ur, rheaders)
                exec_id = json.loads(payload).get("Id")
                if exec_id:
                    self.execs[exec_id] = None
                    while len(self.execs) > 10000:   # borne mémoire : les plus vieux exec ne servent plus
                        del self.execs[next(iter(self.execs))]
                out = [(k, v) for k, v in rheaders if k.lower() not in ("content-length", "transfer-encoding",
                                                                         "connection")]
                writer.write((status_line + "\r\n" + "".join(f"{k}: {v}\r\n" for k, v in out) +
                              f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n").encode("latin-1")
                             + payload)
                await writer.drain()
                return
            # Réponse relayée telle quelle (flux compris), puis fermeture ; 101 = flux bidirectionnel (attach, exec).
            out = [(k, v) for k, v in rheaders if k.lower() != "connection"]
            conn = "Upgrade" if status == 101 else "close"
            writer.write((status_line + "\r\n" + "".join(f"{k}: {v}\r\n" for k, v in out) +
                          f"Connection: {conn}\r\n\r\n").encode("latin-1"))
            await writer.drain()
            hijack = status == 101 or (status == 200 and kind in ("exec", "ctr") and
                                       (hget(rheaders, "content-type") or "").startswith(
                                           "application/vnd.docker.raw-stream"))
            if hijack:
                await asyncio.gather(pipe(ur, writer), pipe(self._client_reader, uw))
            else:
                await pipe(ur, writer)
        finally:
            uw.close()
            writer.close()



async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), stream=sys.stdout,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = Config.from_env()

    execs: dict[str, None] = {}

    async def on_client(reader, writer):
        await Proxy(cfg, execs, reader).handle(reader, writer)

    server = await asyncio.start_server(on_client, cfg.listen_host, cfg.listen_port, limit=1 << 16)
    log.info("écoute %s:%s → %s ; dossiers %s ; skills %s ; réseau %s", cfg.listen_host, cfg.listen_port,
             cfg.upstream, cfg.workspaces, cfg.skills, cfg.network)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
