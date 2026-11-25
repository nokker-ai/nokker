"""Proxy filtrant du socket docker pour le noyau Lokkal (compose Lokkal, service `dockerproxy`).

Le noyau lance un bac à sable par fil (`nokker/tools/code.py`, DockerEnvironment de la bibliothèque amont, CLI docker). Lui donner
le socket brut, c'est lui donner l'hôte : `docker run --privileged -v /:/host`. Ce proxy est la seule voie du noyau
vers le démon (DOCKER_HOST=tcp://dockerproxy:2375, sur un réseau interne que seuls eux deux partagent) et n'accepte
que ce dont le bac à sable a besoin :

- sondes : _ping, version, info ; image inspect et pull ;
- containers ÉTIQUETÉS `nokker.sandbox` ET `nokker.instance=<NOKKER_INSTANCE>` seulement : create (vérifié, voir
  `check_create`), start, attach, wait, inspect, exec, stop, kill, rm, logs, resize, liste (filtrée sur les deux
  étiquettes) ;
- exec : seulement ceux créés par ce proxy, jamais privilégiés.

Volumes nommés : refusés, sauf sous une forme unique et seulement si le service a `NOKKER_HOST_VOLUMES=1` (hôte qui
embarque le noyau dans un conteneur, `local.code.host_volumes`) : `Mounts` de type volume, source `nokker-<instance>-<nom>`
(le préfixe de CETTE instance), `VolumeOptions` réduit à un `Subpath` relatif non vide sans `..`, cible absolue ; ni
`Binds` (« nom:/cible » n'a pas de sous-dossier), ni pilote, ni étiquette de volume, ni volume entier.

Instance : le proxy ne sert QU'UNE instance de noyau, celle de `NOKKER_INSTANCE` (posée par l'installeur dans `.env`,
la même valeur que `<état>/instance-id` que lit le noyau). Un create sans l'étiquette `nokker.instance` de SON instance
est refusé ; un container d'une autre instance, ou sans l'étiquette, est refusé (403, même message qu'il existe ou
non) pour toutes les actions. Deux noyaux sur un même démon ont chacun leur proxy et ne se voient pas.

Tout le reste répond 403. Un create est refusé s'il demande : privileged, une capacité hors {DAC_OVERRIDE, CHOWN,
FOWNER}, un espace de noms de l'hôte, un périphérique, un montage hors du dossier de travail (rw) et des dossiers de
skills (ro), un port publié, un autre réseau que `nokker-sandbox` ou `none`, ou s'il oublie no-new-privileges.
Le proxy ajoute lui-même HTTP(S)_PROXY vers le proxy de sortie (`egress`) : le réseau `nokker-sandbox` est interne,
c'est la seule sortie, et elle refuse les plages privées.

Les deux sondes de la bibliothèque amont (limites cgroup, quota disque) créent un container sans étiquette. Acceptées
seulement sous leur forme EXACTE, sur une image connue, sans montage ni port ni entrypoint ni variable : la sonde cgroup
est `docker run --rm --cpus 0.5 --memory 64m --pids-limit 32 <image> sleep 0` (commande `["sleep","0"]`, AutoRemove,
plafonds au plus ceux de la sonde ; `sleep 5`, `sleep` quelconque, `true` : refusés), la sonde disque `docker create
--storage-opt size=1m <image>` (aucune commande). Le proxy leur pose l'étiquette et le réseau `none`.

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
INSTANCE_LABEL = "nokker.instance"
_INSTANCE_FORM = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{5,127}")   # au moins 6 caractères, comme `MIN_INSTANCE_LEN` du noyau
CGROUP_PROBE_CMD = ["sleep", "0"]                      # tools/environments/docker.py::_cgroup_limits_available (amont)
CGROUP_PROBE_MAX = {"NanoCpus": 500_000_000, "Memory": 64 << 20, "PidsLimit": 32}  # --cpus 0.5 --memory 64m --pids-limit 32
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
    host_volumes: bool = False         # NOKKER_HOST_VOLUMES=1 : le noyau monte un volume nommé de son instance (sous-dossier)
    instance: str = ""                 # NOKKER_INSTANCE : étiquette `nokker.instance` des seuls bacs que ce proxy sert

    @classmethod
    def from_env(cls, env=os.environ) -> "Config":
        c = cls(
            upstream=env.get("DOCKER_UPSTREAM", cls.upstream),
            listen_host=env.get("LISTEN_HOST", cls.listen_host),
            listen_port=int(env.get("LISTEN_PORT", cls.listen_port)),
            workspaces=[_norm(p) for p in env.get("NOKKER_HOST_WORKSPACE", "").split(":") if p.strip()],
            skills=[_norm(p) for p in env.get("NOKKER_HOST_SKILLS", "").split(":") if p.strip()],
            network=env.get("NOKKER_SANDBOX_NETWORK", cls.network),
            instance=env.get("NOKKER_INSTANCE", "").strip(),
            host_volumes=env.get("NOKKER_HOST_VOLUMES", "").strip().lower() in ("1", "true", "yes"),
            egress_proxy=env.get("NOKKER_SANDBOX_EGRESS", cls.egress_proxy),
        )
        c.probe_images |= {i.strip() for i in env.get("NOKKER_SANDBOX_IMAGES", "").split(",") if i.strip()}
        if not c.workspaces or "/" in c.workspaces:
            raise SystemExit("NOKKER_HOST_WORKSPACE absent ou « / » : refus de démarrer")
        if not _INSTANCE_FORM.fullmatch(c.instance):
            raise SystemExit("NOKKER_INSTANCE absent ou invalide (6 à 128 caractères [A-Za-z0-9_.-]) : refus de démarrer")
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


def _check_cgroup_probe(body: dict, hc: dict) -> None:
    """La sonde cgroup de l'amont, et rien de plus large : `sleep 0` éphémère (AutoRemove), plafonds posés (sinon elle ne
    sonde rien) et au plus ceux de la sonde, ni variable, ni utilisateur, ni dossier de travail, ni port."""
    if hc.get("AutoRemove") is not True:
        raise Refused("sonde cgroup : AutoRemove attendu")
    for k, top in CGROUP_PROBE_MAX.items():
        v = hc.get(k)
        if not isinstance(v, int) or isinstance(v, bool) or not 0 < v <= top:
            raise Refused(f"sonde cgroup : {k} hors de la sonde de l'amont")
    if body.get("Env") or body.get("User") or body.get("WorkingDir") or body.get("ExposedPorts") or body.get("Tty"):
        raise Refused("sonde cgroup : paramètres hors de la sonde de l'amont")


_VOLUME_REST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}")
_SUBPATH = re.compile(r"[A-Za-z0-9_./@+=-]{1,512}")           # même alphabet que `volume-subpath` côté noyau
_VOLUME_MOUNT_KEYS = {"Type", "Source", "Target", "ReadOnly", "VolumeOptions"}


def _check_volume(m: dict, cfg: Config) -> None:
    """Le seul volume nommé admis : celui de l'instance, restreint à un sous-dossier (`--mount type=volume,source=
    nokker-<instance>-<nom>,target=<cible>,volume-subpath=<dossier>`), et seulement si l'hôte l'a demandé."""
    if not cfg.host_volumes:
        raise Refused("volume nommé refusé (NOKKER_HOST_VOLUMES absent)")
    if set(m) - _VOLUME_MOUNT_KEYS:
        raise Refused(f"montage de volume : champs refusés {sorted(set(m) - _VOLUME_MOUNT_KEYS)}")
    src, dst = m.get("Source", ""), m.get("Target", "")
    prefix = f"nokker-{cfg.instance}-"
    if not (isinstance(src, str) and src.startswith(prefix) and _VOLUME_REST.fullmatch(src[len(prefix):])):
        raise Refused(f"volume {src!r} refusé : seul un volume {prefix}<nom> de cette instance")
    if not (isinstance(dst, str) and dst.startswith("/") and dst != "/" and "\x00" not in dst and
            ".." not in dst.split("/")):
        raise Refused(f"cible de volume refusée : {dst!r}")
    vo = m.get("VolumeOptions")
    sub = vo.get("Subpath") if isinstance(vo, dict) and set(vo) == {"Subpath"} else None
    if not (isinstance(sub, str) and _SUBPATH.fullmatch(sub) and not sub.startswith("/")
            and not any(p in ("", "..", ".") for p in sub.split("/"))):
        raise Refused("volume sans sous-dossier valide (Subpath relatif, sans « .. ») : le volume entier est refusé")


def check_create(body: dict, cfg: Config) -> dict:
    """Valide (et complète) le corps d'un POST /containers/create. Renvoie le corps à transmettre."""
    hc = body.setdefault("HostConfig", {}) or {}
    body["HostConfig"] = hc
    labels = body.get("Labels") or {}
    if not isinstance(labels, dict):
        raise Refused("Labels illisibles")
    cmd = body.get("Cmd")
    probe = LABEL not in labels
    if probe:
        # Sondes de la bibliothèque amont : aucun montage, `true` ou la commande par défaut de l'image, image connue.
        if hc.get("Binds") or hc.get("Mounts") or body.get("Volumes"):
            raise Refused("container sans étiquette nokker.sandbox avec montage")
        if body.get("Entrypoint") not in (None, [], ""):
            raise Refused("container sans étiquette nokker.sandbox : entrypoint non autorisé")
        if cmd in (None, []):
            pass                                  # sonde disque : la commande par défaut de l'image
        elif cmd == CGROUP_PROBE_CMD:
            _check_cgroup_probe(body, hc)
        else:
            raise Refused("container sans étiquette nokker.sandbox : commande non autorisée")
        if _image_name(body.get("Image", "")) not in {_image_name(i) for i in cfg.probe_images}:
            raise Refused(f"container sans étiquette nokker.sandbox : image {body.get('Image')!r} non autorisée")
        labels = body["Labels"] = {**labels, LABEL: "probe", INSTANCE_LABEL: cfg.instance}
        hc["NetworkMode"] = "none"
        body.pop("NetworkingConfig", None)

    if labels.get(INSTANCE_LABEL) != cfg.instance or not cfg.instance:
        raise Refused(f"étiquette {INSTANCE_LABEL} absente ou d'une autre instance : création refusée")
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
        if t == "volume":
            _check_volume(m, cfg)
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
            given = {e.split("=", 1)[0]: e.split("=", 1)[1] for e in (body.get("Env") or []) if "=" in e}
            env = [e for e in (body.get("Env") or []) if e.split("=", 1)[0].lower() not in
                   ("http_proxy", "https_proxy", "no_proxy", "all_proxy", "nokker_ssh_proxy")]
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                # Le noyau peut poser l'identité du bac à sable (`nokker:<jeton>@`, injection de secrets par egress,
                # docs/EGRESS-SECRETS.md) : gardée SEULEMENT si l'URL vise exactement le même proxy ; sinon écrasée.
                v = given.get(k, "")
                env.append(f"{k}={v if v and same_proxy(v, cfg.egress_proxy) else cfg.egress_proxy}")
            env += ["NO_PROXY=localhost,127.0.0.1", "no_proxy=localhost,127.0.0.1"]
            # Client ssh des outils `ssh_*` (nokker/tools/ssh.py) : ProxyCommand CONNECT vers egress (hôte:port), qui
            # n'ouvre que les hôtes déclarés dans hosts.yaml. Toujours la valeur d'egress, jamais celle du noyau.
            e = urlsplit(cfg.egress_proxy)
            if e.hostname and e.port:
                env.append(f"NOKKER_SSH_PROXY={e.hostname}:{e.port}")
            body["Env"] = env
    return body


def same_proxy(url: str, egress: str) -> bool:
    """`url` = `egress` à l'identité près : même schéma, hôte et port, utilisateur `nokker`, ni chemin ni requête."""
    try:
        u, e = urlsplit(url), urlsplit(egress)
        same = (u.scheme, u.hostname, u.port) == (e.scheme, e.hostname, e.port)
    except ValueError:
        return False
    return (same and u.username == "nokker" and bool(u.password) and u.path in ("", "/")
            and not u.query and not u.fragment and "\n" not in url and " " not in url)


def check_exec(body: dict) -> dict:
    if body.get("Privileged"):
        raise Refused("exec privilégié")
    return body


def filter_list_query(query: str, instance: str) -> str:
    """GET /containers/json : on ne laisse voir que les bacs à sable de CETTE instance. Les filtres de label se
    cumulent (ET) : un filtre `nokker.instance=<autre>` du client ne peut donc que vider la liste."""
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
    if f"{INSTANCE_LABEL}={instance}" not in lab:
        lab = [*lab, f"{INSTANCE_LABEL}={instance}"]
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
        """Vrai seulement pour un bac étiqueté `nokker.sandbox` ET `nokker.instance=<celle de ce proxy>`."""
        status, info = await self.call_json("GET", f"/containers/{quote(ref, safe='')}/json")
        labels = ((info or {}).get("Config", {}).get("Labels") or {}) if status == 200 else {}
        return LABEL in labels and bool(self.cfg.instance) and labels.get(INSTANCE_LABEL) == self.cfg.instance

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
                query = filter_list_query(query, self.cfg.instance)
            elif kind == "ctr":
                if not await self.is_sandbox(ref):
                    raise Refused(f"container {ref} hors des bacs à sable de cette instance")
            elif kind == "exec_create":
                if not await self.is_sandbox(ref):
                    raise Refused(f"container {ref} hors des bacs à sable de cette instance")
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
    log.info("écoute %s:%s → %s ; dossiers %s ; skills %s ; réseau %s ; instance %s", cfg.listen_host, cfg.listen_port,
             cfg.upstream, cfg.workspaces, cfg.skills, cfg.network, cfg.instance)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
