"""NAS : état des disques, des volumes et des sauvegardes ; relancer une sauvegarde DÉCLARÉE.

**TrueNAS** (25.04 et suivants, 26 : seule API restante) — API officielle documentée, JSON-RPC 2.0 sur WebSocket
`wss://<nas>/api/current` (https://api.truenas.com/, référence v25.10) : `auth.login_with_api_key [clé]`,
`pool.query` (`name, status, healthy, warning, status_detail, topology`), `alert.list`, `cloudsync.query` (`id,
description, enabled, job: {state, time_finished}`), `replication.query` (`id, name, state: {state, datetime}`),
`cloudsync.sync [id]`, `replication.run [id]`.

**Synology DSM** — seule l'authentification (`SYNO.API.Auth`) et la découverte (`SYNO.API.Info`) sont documentées
publiquement par Synology (« DSM Login Web API Guide »). L'état du stockage (`SYNO.Storage.CGI.Storage load_info`) et
les tâches Hyper Backup (`SYNO.Backup.Task list|status|backup`) sont des interfaces INTERNES de DSM, non documentées :
elles ne sont appelées que si `dsm_internal_api: true` est posé explicitement dans `home.yaml`, après vérification de
leur présence par `SYNO.API.Info`. Sans ce réglage, la veille Synology dit qu'elle ne peut pas lire l'état (aucune
supposition). Compte DSM dédié, sans droit d'administration hors de ce qu'exige la lecture.
"""
from __future__ import annotations

import json
import re
import socket
import ssl
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from nokker.homebridge.watch.net import LanHttp, UpstreamError, WatchEnv

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
GOOD_DISK = {"normal", "initialized", "not_initialized"}
GOOD_VOLUME = {"normal", "background"}
OK_JOB = {"SUCCESS", "FINISHED"}


def _text(v: Any, limit: int = 80) -> str:
    s = "" if v is None else str(v)
    return " ".join("".join(c if c.isprintable() else " " for c in s).split())[:limit]


def _epoch(v: Any) -> float | None:
    if isinstance(v, dict) and "$date" in v:  # JSON-RPC TrueNAS : {"$date": millisecondes}
        try:
            return float(v["$date"]) / 1000.0
        except (TypeError, ValueError):
            return None
    if isinstance(v, (int, float)) and v > 0:
        return float(v) / 1000.0 if v > 1e12 else float(v)
    return None


def _declared(raw: dict[str, Any], key: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, ref in (raw.get(key) or {}).items():
        if not _NAME.match(str(name)):
            raise ValueError(f"{key} : nom invalide ({str(name)[:48]!r})")
        out[str(name)] = ref
    return out


# ─────────────────────────────── Synology ───────────────────────────────

@dataclass
class Synology:
    config: WatchEnv
    url: str
    user: str
    password_ref: str
    name: str = "synology"
    internal_api: bool = False
    backups: dict[str, Any] = field(default_factory=dict)  # nom déclaré → task_id Hyper Backup
    http: LanHttp | None = None
    _sid: str | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @classmethod
    def from_config(cls, config: WatchEnv, raw: dict[str, Any]) -> Synology:
        url = str(raw.get("url") or "").rstrip("/")
        if not url or not raw.get("user") or not raw.get("password"):
            raise ValueError("watch.synology : url, user et password (secret:NOM) obligatoires")
        config.check_url(url)
        verify = raw.get("ca_file") or bool(raw.get("verify_tls", True))
        return cls(config=config, url=url, user=str(raw["user"]), password_ref=str(raw["password"]),
                   name=_text(raw.get("name") or "synology", 40), internal_api=bool(raw.get("dsm_internal_api", False)),
                   backups=_declared(raw, "backups"), http=LanHttp(config, verify=verify))

    def _call(self, api: str, method: str, version: int, **params: Any) -> Any:
        data = {"api": api, "method": method, "version": str(version), **{k: str(v) for k, v in params.items()}}
        if api != "SYNO.API.Info":
            data["_sid"] = self._login()
        out = self.http.json("POST", f"{self.url}/webapi/entry.cgi", data=data)
        if not isinstance(out, dict) or not out.get("success"):
            code = (out.get("error") or {}).get("code") if isinstance(out, dict) else None
            if code in (105, 106, 107, 119) and api != "SYNO.API.Auth":
                self._sid = None
            raise UpstreamError(f"DSM a refusé {api}.{method} (code {code})", "dsm")
        return out.get("data") or {}

    def _login(self) -> str:
        with self._lock:
            if self._sid is None:
                data = {"api": "SYNO.API.Auth", "method": "login", "version": "6", "account": self.user,
                        "passwd": self.config.secret(self.password_ref), "format": "sid", "session": "nokker-watch"}
                out = self.http.json("POST", f"{self.url}/webapi/entry.cgi", data=data)
                sid = ((out or {}).get("data") or {}).get("sid") if isinstance(out, dict) and out.get("success") \
                    else None
                if not sid:
                    raise UpstreamError("connexion à DSM refusée", "auth")
                self._sid = str(sid)
            return self._sid

    def _has(self, *apis: str) -> bool:
        info = self._call("SYNO.API.Info", "query", 1, query=",".join(apis))
        return all(a in info for a in apis)

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": "synology", "name": self.name, "disks": [], "volumes": [], "backups": [],
                               "warnings": []}
        if not self.internal_api:
            out["warnings"].append("état non lu : DSM n'a pas d'API publique documentée pour le stockage et Hyper "
                                   "Backup (réglage dsm_internal_api de home.yaml)")
            return out
        if not self._has("SYNO.Storage.CGI.Storage", "SYNO.Backup.Task"):
            out["warnings"].append("interfaces de stockage ou de sauvegarde absentes de ce DSM")
            return out
        st = self._call("SYNO.Storage.CGI.Storage", "load_info", 1)
        for d in st.get("disks") or []:
            status = _text(d.get("status") or d.get("overview_status"), 24).lower()
            smart = _text(d.get("smart_status"), 24).lower()
            healthy = status in GOOD_DISK and smart in ("", "normal")
            out["disks"].append({"name": _text(d.get("name") or d.get("id"), 40), "status": status or "inconnu",
                                 "smart": smart or None, "healthy": healthy,
                                 "temp_c": d.get("temp") if isinstance(d.get("temp"), (int, float)) else None})
        for v in st.get("volumes") or []:
            status = _text(v.get("status"), 24).lower()
            size = v.get("size") or {}
            try:
                used_pct = round(100.0 * float(size.get("used")) / float(size.get("total")), 1)
            except (TypeError, ValueError, ZeroDivisionError):
                used_pct = None
            out["volumes"].append({"name": _text(v.get("vol_path") or v.get("id"), 40), "status": status or "inconnu",
                                   "healthy": status in GOOD_VOLUME, "used_pct": used_pct})
        tasks = self._call("SYNO.Backup.Task", "list", 1).get("task_list") or []
        for t in tasks:
            tid = t.get("task_id")
            try:
                s = self._call("SYNO.Backup.Task", "status", 1, task_id=tid)
            except UpstreamError:
                s = {}
            result = _text(s.get("last_bkp_result"), 24).lower()
            last_end = _epoch(s.get("last_bkp_end_time") or s.get("last_bkp_time"))
            last_ok = _epoch(s.get("last_bkp_success_time")) or (last_end if result in ("done", "success") else None)
            name = next((n for n, ref in self.backups.items() if str(ref) == str(tid)), None)
            out["backups"].append({"name": _text(t.get("name"), 60), "key": name, "state": _text(s.get("state"), 24),
                                   "last_run": last_end, "last_result": result or None, "last_success": last_ok})
        return out

    def run_backup(self, name: str) -> dict[str, Any]:
        tid = self.backups.get(name)
        if tid is None:
            raise UpstreamError(f"sauvegarde non déclarée : {name[:48]}", "unknown_target")
        if not self.internal_api:
            raise UpstreamError("relance impossible sans dsm_internal_api (API Hyper Backup non documentée)",
                                "unsupported")
        self._call("SYNO.Backup.Task", "backup", 1, task_id=tid)
        return {"started": True}


# ─────────────────────────────── TrueNAS ───────────────────────────────

RpcFn = Callable[[str, list[Any]], Any]


def websocket_rpc(config: WatchEnv, url: str, key_ref: str, verify: Any) -> RpcFn:
    """Un appel = une connexion : login par clé d'API, puis la méthode (JSON-RPC 2.0, `wss://…/api/current`)."""
    def call(method: str, params: list[Any]) -> Any:
        from websockets.sync.client import connect

        ip = config.check_url(url)
        parts = urlsplit(url)
        kw: dict[str, Any] = {}
        if url.startswith("wss://"):
            ctx = ssl.create_default_context(cafile=verify if isinstance(verify, str) else None)
            if verify is False:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            kw = {"ssl": ctx, "server_hostname": parts.hostname}
        port = parts.port or (443 if url.startswith("wss://") else 80)
        try:  # TCP sur l'IP vérifiée (pas de second tour DNS) ; certificat et SNI au nom de l'hôte
            sock = socket.create_connection((ip, port), timeout=10)
        except OSError as e:
            raise UpstreamError(f"TrueNAS injoignable ({type(e).__name__})", "unreachable") from None
        try:
            with connect(url, sock=sock, open_timeout=10, close_timeout=2, max_size=4 * 1024 * 1024, **kw) as ws:
                def rpc(i: int, m: str, p: list[Any]) -> Any:
                    ws.send(json.dumps({"jsonrpc": "2.0", "id": i, "method": m, "params": p}))
                    while True:
                        msg = json.loads(ws.recv(timeout=15))
                        if msg.get("id") == i:
                            if "error" in msg:
                                raise UpstreamError(f"TrueNAS a refusé {m}", "rpc")
                            return msg.get("result")

                if rpc(1, "auth.login_with_api_key", [config.secret(key_ref)]) is not True:
                    raise UpstreamError("clé d'API TrueNAS refusée", "auth")
                return rpc(2, method, params)
        except UpstreamError:
            raise
        except Exception as e:  # noqa: BLE001 — réseau, TLS, protocole : jamais le détail (pas de secret)
            raise UpstreamError(f"TrueNAS injoignable ({type(e).__name__})", "unreachable") from None
        finally:
            sock.close()
    return call


@dataclass
class TrueNAS:
    rpc: RpcFn
    name: str = "truenas"
    backups: dict[str, str] = field(default_factory=dict)  # nom déclaré → "cloudsync:<id>" | "replication:<id>"

    @classmethod
    def from_config(cls, config: WatchEnv, raw: dict[str, Any], rpc: RpcFn | None = None) -> TrueNAS:
        url = str(raw.get("url") or "")
        if not url or not raw.get("api_key"):
            raise ValueError("watch.truenas : url (wss://…/api/current) et api_key (secret:NOM) obligatoires")
        config.check_url(url)
        backups = {}
        for name, ref in _declared(raw, "backups").items():
            kind, _, num = str(ref).partition(":")
            if kind not in ("cloudsync", "replication") or not num.isdigit():
                raise ValueError(f"watch.truenas.backups.{name} : « cloudsync:<id> » ou « replication:<id> »")
            backups[name] = f"{kind}:{num}"
        verify = raw.get("ca_file") or bool(raw.get("verify_tls", True))
        return cls(rpc=rpc or websocket_rpc(config, url, str(raw["api_key"]), verify),
                   name=_text(raw.get("name") or "truenas", 40), backups=backups)

    def _key_of(self, kind: str, tid: Any) -> str | None:
        return next((n for n, ref in self.backups.items() if ref == f"{kind}:{tid}"), None)

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": "truenas", "name": self.name, "disks": [], "volumes": [], "backups": [],
                               "warnings": []}
        for p in self.rpc("pool.query", []) or []:
            out["volumes"].append({"name": _text(p.get("name"), 40), "status": _text(p.get("status"), 24),
                                   "healthy": bool(p.get("healthy")) and not p.get("warning"), "used_pct": None})
            for group in ((p.get("topology") or {}).values()):
                for vdev in group or []:
                    for d in (vdev.get("children") or [vdev]):
                        if d.get("type") == "DISK" or d.get("disk"):
                            st = _text(d.get("status"), 24)
                            errs = sum(int((d.get("stats") or {}).get(k) or 0)
                                       for k in ("read_errors", "write_errors", "checksum_errors"))
                            out["disks"].append({"name": _text(d.get("disk") or d.get("name"), 40), "status": st,
                                                 "smart": None, "healthy": st == "ONLINE" and errs == 0,
                                                 "temp_c": None})
        for a in self.rpc("alert.list", []) or []:
            if a.get("dismissed") or str(a.get("level")) not in ("WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY"):
                continue
            out["warnings"].append(_text(a.get("formatted") or a.get("klass"), 200))
        for t in self.rpc("cloudsync.query", []) or []:
            job = t.get("job") or {}
            state = _text(job.get("state"), 16)
            end = _epoch(job.get("time_finished"))
            out["backups"].append({"name": _text(t.get("description") or f"cloudsync {t.get('id')}", 60),
                                   "key": self._key_of("cloudsync", t.get("id")), "state": state, "last_run": end,
                                   "last_result": state.lower() or None,
                                   "last_success": end if state in OK_JOB else None})
        for t in self.rpc("replication.query", []) or []:
            st = t.get("state") or {}
            state = _text(st.get("state"), 16)
            end = _epoch(st.get("datetime"))
            out["backups"].append({"name": _text(t.get("name") or f"replication {t.get('id')}", 60),
                                   "key": self._key_of("replication", t.get("id")), "state": state, "last_run": end,
                                   "last_result": state.lower() or None,
                                   "last_success": end if state in OK_JOB else None})
        out["warnings"] = out["warnings"][:10]
        return out

    def run_backup(self, name: str) -> dict[str, Any]:
        ref = self.backups.get(name)
        if ref is None:
            raise UpstreamError(f"sauvegarde non déclarée : {name[:48]}", "unknown_target")
        kind, _, num = ref.partition(":")
        self.rpc("cloudsync.sync" if kind == "cloudsync" else "replication.run", [int(num)])
        return {"started": True}


__all__ = ["Synology", "TrueNAS", "websocket_rpc"]
