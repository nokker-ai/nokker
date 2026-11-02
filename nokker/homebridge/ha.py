"""Home Assistant vu de la passerelle du foyer (docs/HOME-ASSISTANT.md §2) — le SEUL client HA du dépôt.

Le noyau ne parle jamais à Home Assistant : il appelle les opérations nommées `ha.*` de la passerelle
(`nokker/homebridge/server.py::OPS`). Ici :

  - API WebSocket (`/api/websocket`) : états, registres (zones, appareils, entités), `call_service` ciblé par
    entity_id, abonnement `state_changed`. La connexion TCP passe par la liste blanche (`Lan.tcp` : adresse privée du
    foyer listée dans home.yaml, IP résolue une fois et épinglée) ;
  - REST (`/api/config/{automation|script}/config/<id>`, `/auth/token`) par `Lan.http` (même liste blanche).

Jetons (fichiers secrets de la passerelle, jamais un message ni un journal) : jeton longue durée d'un compte NON
administrateur recommandé (`token_file`), ou OAuth de HA (`refresh_token_file` + `client_id` : jeton d'accès court
redemandé à `/auth/token`, gardé en mémoire), et `admin_token_file` facultatif, pour l'écriture d'automatisations et
de scripts seulement.
"""
from __future__ import annotations

import collections
import contextlib
import fnmatch
import json
import logging
import queue
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit

logger = logging.getLogger("nokker.homebridge.ha")
EVENT_BUFFER = 1000
MAX_POLL_S = 30.0


class HaError(RuntimeError):
    """Erreur côté Home Assistant ; message construit ici, jamais recopié d'une exception qui citerait un en-tête."""

    def __init__(self, message: str, code: str = "ha_error") -> None:
        super().__init__(message)
        self.code = code


class Transport(Protocol):
    def send_json(self, msg: dict[str, Any]) -> None: ...

    def recv_json(self, timeout_s: float) -> dict[str, Any]: ...

    def close(self) -> None: ...


def ws_url(base: str) -> str:
    parts = urlsplit(base)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/") + "/api/websocket", "", ""))


class WebSocketTransport:
    """`websockets.sync.client` sur une socket ouverte par la liste blanche (`Lan.tcp`)."""

    def __init__(self, url: str, timeout_s: float, lan: Any) -> None:
        try:
            from websockets.sync.client import connect
        except ImportError as e:  # pragma: no cover — l'image l'embarque
            raise HaError("paquet websockets absent", "no_websockets") from e
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        sock = lan.tcp(parts.hostname or "", port)  # Refused / LanError si hors liste
        kw: dict[str, Any] = {}
        if parts.scheme == "wss":
            kw = {"ssl": ssl.create_default_context(), "server_hostname": parts.hostname}
        try:
            self._ws = connect(url, sock=sock, open_timeout=timeout_s, close_timeout=2, max_size=32 * 1024 * 1024,
                               **kw)
        except Exception as e:  # noqa: BLE001
            sock.close()
            raise HaError(f"Home Assistant injoignable ({type(e).__name__})", "unreachable") from None

    def send_json(self, msg: dict[str, Any]) -> None:
        self._ws.send(json.dumps(msg))

    def recv_json(self, timeout_s: float) -> dict[str, Any]:
        try:
            raw = self._ws.recv(timeout=timeout_s)
        except TimeoutError:
            raise HaError("Home Assistant ne répond pas (délai dépassé)", "timeout") from None
        except Exception as e:  # noqa: BLE001
            raise HaError(f"connexion Home Assistant perdue ({type(e).__name__})", "closed") from None
        return json.loads(raw)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._ws.close()


@dataclass
class TokenSource:
    """Jeton d'accès au moment de la connexion. `read()` : jeton longue durée ; `refresh()` + `client_id` : OAuth."""

    read: Callable[[], str] | None = None
    refresh: Callable[[], str] | None = None
    client_id: str = ""
    exchange: Callable[[dict[str, str]], dict[str, Any]] | None = None  # POST /auth/token (formulaire)
    clock: Callable[[], float] = time.monotonic
    _cached: tuple[str, float] | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __repr__(self) -> str:
        return f"TokenSource(oauth={self.refresh is not None})"

    def get(self) -> str:
        if self.refresh is None:
            try:
                value = self.read() if self.read is not None else ""
            except Exception:  # noqa: BLE001 — fichier absent : on ne recopie pas le chemin complet
                raise HaError("jeton Home Assistant illisible (home.yaml : token_file)", "no_token") from None
            if not value:
                raise HaError("jeton Home Assistant absent (home.yaml : token_file)", "no_token")
            return value
        with self._lock:
            if self._cached and self._cached[1] > self.clock():
                return self._cached[0]
            try:
                refresh = self.refresh()
            except Exception:  # noqa: BLE001
                raise HaError("jeton OAuth illisible (home.yaml : refresh_token_file)", "no_token") from None
            if self.exchange is None:
                raise HaError("OAuth sans client HTTP", "no_token")
            data = self.exchange({"grant_type": "refresh_token", "refresh_token": refresh,
                                  "client_id": self.client_id})
            token = str(data.get("access_token") or "")
            if not token:
                raise HaError("Home Assistant a refusé le jeton OAuth (à renouveler)", "auth_invalid")
            ttl = float(data.get("expires_in") or 1800)
            self._cached = (token, self.clock() + max(30.0, ttl - 60.0))
            return token


class HaSession:
    """Une connexion WebSocket authentifiée. Les événements d'un abonnement arrivent dans `events()`."""

    def __init__(self, transport: Transport, token: str, timeout_s: float) -> None:
        self.transport, self.timeout_s = transport, timeout_s
        self._next = 1
        self._pending: queue.Queue[dict[str, Any]] = queue.Queue()
        first = transport.recv_json(timeout_s)
        if first.get("type") != "auth_required":
            raise HaError("réponse inattendue de Home Assistant (auth_required attendu)", "protocol")
        transport.send_json({"type": "auth", "access_token": token})
        got = transport.recv_json(timeout_s)
        if got.get("type") != "auth_ok":
            raise HaError("Home Assistant a refusé le jeton (auth_invalid)", "auth_invalid")
        self.ha_version = str(got.get("ha_version") or "")

    def command(self, kind: str, **payload: Any) -> Any:
        msg_id = self._next
        self._next += 1
        self.transport.send_json({"id": msg_id, "type": kind, **payload})
        deadline = time.monotonic() + self.timeout_s
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise HaError(f"Home Assistant ne répond pas à {kind}", "timeout")
            msg = self.transport.recv_json(left)
            if msg.get("type") == "event":
                self._pending.put(msg)
                continue
            if msg.get("id") != msg_id or msg.get("type") != "result":
                continue
            if not msg.get("success"):
                err = msg.get("error") or {}
                raise HaError(f"Home Assistant : {err.get('code') or 'erreur'} — {str(err.get('message') or '')[:200]}",
                              "ha_" + str(err.get("code") or "error"))
            return msg.get("result")

    def states(self) -> list[dict[str, Any]]:
        return list(self.command("get_states") or [])

    def subscribe_state_changed(self) -> None:
        self.command("subscribe_events", event_type="state_changed")

    def events(self, timeout_s: float) -> Iterator[dict[str, Any]]:
        while True:
            try:
                yield self._pending.get_nowait()
                continue
            except queue.Empty:
                pass
            try:
                msg = self.transport.recv_json(timeout_s)
            except HaError as e:
                if e.code == "timeout":
                    return
                raise
            if msg.get("type") == "event":
                yield msg

    def close(self) -> None:
        self.transport.close()


@dataclass
class HomeAssistant:
    """Client unique de Home Assistant (WebSocket + REST), derrière la liste blanche de la passerelle. Même interface
    que l'ancien client REST pour l'ambiant : `states()`, `call_service(domain, service, data)`."""

    lan: Any  # nokker.homebridge.lan.Lan
    url: str
    token: TokenSource
    admin_token: TokenSource | None = None
    timeout_s: float = 10.0
    connect: Callable[[str, float], Transport] | None = None  # tests : faux Home Assistant

    def __repr__(self) -> str:
        return f"HomeAssistant(url={self.url!r})"

    def session(self) -> HaSession:
        self.lan.allow.check_url(self.url)
        token = self.token.get()
        connect = self.connect or (lambda u, t: WebSocketTransport(u, t, self.lan))
        transport = connect(ws_url(self.url), self.timeout_s)
        try:
            return HaSession(transport, token, self.timeout_s)
        except Exception:
            transport.close()
            raise

    def _with(self, fn: Callable[[HaSession], Any]) -> Any:
        s = self.session()
        try:
            return fn(s)
        finally:
            s.close()

    def states(self) -> list[dict[str, Any]]:
        return self._with(lambda s: s.states())

    def registry(self) -> dict[str, Any]:
        def get(s: HaSession) -> dict[str, Any]:
            return {"areas": list(s.command("config/area_registry/list") or []),
                    "devices": list(s.command("config/device_registry/list") or []),
                    "entities": list(s.command("config/entity_registry/list") or [])}

        return self._with(get)

    def call_service(self, domain: str, service: str, data: dict[str, Any] | None = None,
                     entity_ids: list[str] | None = None) -> Any:
        payload: dict[str, Any] = {"domain": domain, "service": service, "service_data": dict(data or {})}
        if entity_ids is not None:
            payload["target"] = {"entity_id": list(entity_ids)}
        return self._with(lambda s: s.command("call_service", **payload))

    def write_config(self, kind: str, config_id: str, config: dict[str, Any]) -> None:
        token = (self.admin_token or self.token).get()
        status, _ = self.lan.http("POST", f"{self.url}/api/config/{kind}/config/{config_id}",
                                  json.dumps(config).encode("utf-8"),
                                  {"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        if status in (401, 403):
            raise HaError("Home Assistant refuse l'écriture (compte administrateur requis : admin_token_file)",
                          "auth_invalid")
        if status != 200:
            raise HaError(f"Home Assistant a refusé la configuration (HTTP {status})", "ha_config")


def oauth_exchange(lan: Any, url: str) -> Callable[[dict[str, str]], dict[str, Any]]:
    def post(form: dict[str, str]) -> dict[str, Any]:
        status, body = lan.http("POST", f"{url}/auth/token", urlencode(form).encode("ascii"),
                                {"Content-Type": "application/x-www-form-urlencoded"})
        if status != 200:
            raise HaError(f"Home Assistant a refusé le jeton OAuth (HTTP {status})", "auth_invalid")
        return json.loads(body or b"{}")

    return post


def from_config(ha_raw: dict[str, Any], lan: Any, secret: Callable[[str], str],
                connect: Callable[[str, float], Transport] | None = None) -> HomeAssistant:
    """`home_assistant` de home.yaml → client. `secret(rel)` : `BridgeConfig.secret` (fichier sous le dossier)."""
    url = str(ha_raw["url"]).rstrip("/")
    lan.allow.check_url(url)
    refresh = ha_raw.get("refresh_token_file")
    token = TokenSource(read=lambda: secret(str(ha_raw.get("token_file") or "secrets/ha_token")),
                        refresh=(lambda: secret(str(refresh))) if refresh else None,
                        client_id=str(ha_raw.get("client_id") or ""), exchange=oauth_exchange(lan, url))
    admin = TokenSource(read=lambda: secret(str(ha_raw["admin_token_file"]))) \
        if ha_raw.get("admin_token_file") else None
    return HomeAssistant(lan=lan, url=url, token=token, admin_token=admin, connect=connect)


@dataclass
class HaOps:
    """Opérations `ha.*` de la passerelle, bornées par la liste blanche d'entités de home.yaml
    (`home_assistant.entities`, motifs glob ; vide = aucune)."""

    ha: HomeAssistant
    entities: tuple[str, ...] = ()
    _events: collections.deque = field(default_factory=lambda: collections.deque(maxlen=EVENT_BUFFER), init=False)
    _seq: int = field(default=0, init=False)
    _cond: threading.Condition = field(default_factory=threading.Condition, init=False)
    _sub: threading.Thread | None = field(default=None, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)

    def allowed(self, entity_id: str) -> bool:
        return any(fnmatch.fnmatchcase(entity_id, p) for p in self.entities)

    def states(self, _p: dict[str, Any]) -> dict[str, Any]:
        return {"states": [s for s in self.ha.states() if self.allowed(str(s.get("entity_id") or ""))]}

    def registry(self, _p: dict[str, Any]) -> dict[str, Any]:
        reg = self.ha.registry()
        return {"areas": [{"area_id": a.get("area_id"), "name": a.get("name")} for a in reg["areas"]],
                "devices": [{"id": d.get("id"), "area_id": d.get("area_id")} for d in reg["devices"]],
                "entities": [{k: e.get(k) for k in ("entity_id", "area_id", "device_id", "hidden_by", "disabled_by")}
                             for e in reg["entities"] if self.allowed(str(e.get("entity_id") or ""))]}

    def call(self, p: dict[str, Any]) -> dict[str, Any]:
        domain, service = str(p.get("domain") or ""), str(p.get("service") or "")
        ids, data = p.get("entity_ids"), p.get("data") or {}
        if not isinstance(ids, list) or not ids or not isinstance(data, dict):
            raise ValueError("entity_ids (liste non vide) et data (objet)")
        if {"entity_id", "area_id", "device_id", "floor_id", "label_id"} & set(data):
            raise PermissionError("cible dans data refusée")
        bad = [str(e) for e in ids if not self.allowed(str(e)) or str(e).split(".", 1)[0] != domain]
        if bad:
            raise PermissionError(f"entité hors liste ou d'un autre domaine : {', '.join(bad)[:200]}")
        self.ha.call_service(domain, service, data, [str(e) for e in ids])
        return {"ok": True}

    def write_config(self, p: dict[str, Any]) -> dict[str, Any]:
        kind, cid, cfg = str(p.get("type") or ""), str(p.get("id") or ""), p.get("config")
        if kind not in ("automation", "script") or not cid or not isinstance(cfg, dict):
            raise ValueError("type (automation|script), id et config (objet)")
        self.ha.write_config(kind, cid, cfg)
        return {"ok": True}

    def events(self, p: dict[str, Any]) -> dict[str, Any]:
        try:
            after, timeout = int(p.get("after", -1)), float(p.get("timeout", 25))
        except (TypeError, ValueError):
            raise ValueError("after et timeout numériques") from None
        self._ensure_subscriber()
        deadline = time.monotonic() + max(0.0, min(timeout, MAX_POLL_S))
        with self._cond:
            if after < 0:
                return {"cursor": self._seq, "events": []}
            while True:
                fresh = [e for e in self._events if e["seq"] > after]
                left = deadline - time.monotonic()
                if fresh or left <= 0:
                    return {"cursor": self._seq, "events": fresh}
                self._cond.wait(left)

    def _ensure_subscriber(self) -> None:
        if self._sub is None or not self._sub.is_alive():
            self._sub = threading.Thread(target=self._subscribe, name="home-bridge-ha-events", daemon=True)
            self._sub.start()

    def _subscribe(self) -> None:
        delay = 2.0
        while not self._stop.is_set():
            try:
                s = self.ha.session()
                try:
                    s.subscribe_state_changed()
                    delay = 2.0
                    while not self._stop.is_set():
                        for msg in s.events(30.0):
                            ev = msg.get("event") or {}
                            eid = str((ev.get("data") or {}).get("entity_id") or "")
                            if ev.get("event_type") != "state_changed" or not self.allowed(eid):
                                continue
                            with self._cond:
                                self._seq += 1
                                self._events.append({"seq": self._seq, "event": ev})
                                self._cond.notify_all()
                finally:
                    s.close()
            except Exception as e:  # noqa: BLE001 — reconnexion
                logger.info("ha.events : abonnement perdu (%s), reprise dans %.0f s", type(e).__name__, delay)
                self._stop.wait(delay)
                delay = min(60.0, delay * 2)

    def stop(self) -> None:
        self._stop.set()


__all__ = ["HaError", "HaOps", "HaSession", "HomeAssistant", "TokenSource", "Transport", "WebSocketTransport",
           "from_config", "oauth_exchange", "ws_url"]
