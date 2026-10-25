"""Client du tunnel de la passerelle `nokker.app` (docs/LOKKAL.md §7.5, format exact : docs/GATEWAY.md §3, brosim).

Le noyau local ouvre UNE connexion sortante WebSocket vers le relais (`wss://relay.nokker.app/v1/tunnel`, sous-protocole
`nokker-tunnel.v1`, `Authorization: Bearer <jeton de machine>`, suit `HTTPS_PROXY`), et rejoue chaque requête relayée
sur le noyau local (`local_base`) ou sur l'interface web (`web_base`, hors `/v1`) :
  relais → machine : {"t":"req","id","method","path","headers":[[k,v]…],"body":"<b64>","more"} (+ {"t":"body"…})
  machine → relais : {"t":"res","id","status","headers","body","more"} (+ une {"t":"body"…} par événement SSE)
  les deux : {"t":"cancel","id"} ; {"t":"ping"} / {"t":"pong"} toutes les 25 s.
Reconnexion à attente croissante (1 s → 60 s, avec aléa) ; `4001` = un tunnel plus récent a pris la place : on
s'arrête. Le Host `<nom>.nokker.app` posé par le relais n'est accepté par le noyau que tant que le tunnel est ouvert
(`on_up` / `on_down`).
"""
from __future__ import annotations

import asyncio
import base64
import json
import secrets
import logging
import random
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("nokker.local.tunnel")

SUBPROTOCOL = "nokker-tunnel.v1"
MAX_FRAME = 1024 * 1024
CHUNK = 256 * 1024  # corps découpé en trames de 256 Kio (base64 < 1 Mio)
MAX_IN_FLIGHT = 64
MAX_BODY = 50 * 1024 * 1024  # même plafond que le relais (413 au-delà) : un relais fautif ne remplit pas la RAM
PING_S = 25.0
_HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "content-length", "set-cookie"}


class _Inbound:
    """Corps des requêtes en cours d'arrivée, trame par trame. `feed` rend `(req, corps)` quand la requête est
    complète, `"too_big"` la première fois qu'elle dépasse `limit` (le reste de ses trames est ensuite jeté), `None`
    sinon. `cancel` oublie tout ce qui concerne la requête : rien ne reste en mémoire si le relais abandonne."""

    def __init__(self, limit: int = MAX_BODY) -> None:
        self.limit = limit
        self.pending: dict[int, dict[str, Any]] = {}
        self.chunks: dict[int, list[bytes]] = {}
        self.sizes: dict[int, int] = {}
        self.refused: set[int] = set()

    def feed(self, msg: dict[str, Any]) -> Any:
        rid = int(msg["id"])
        more = bool(msg.get("more"))
        if msg.get("t") == "req":
            self.cancel(rid)
            self.pending[rid] = msg
            self.chunks[rid], self.sizes[rid] = [], 0
            data = base64.b64decode(msg.get("body") or "")
        elif rid in self.refused:
            if not more:
                self.refused.discard(rid)
            return None
        elif rid not in self.pending:
            return None  # trame d'une requête inconnue ou déjà annulée
        else:
            data = base64.b64decode(msg.get("data") or "")
        self.sizes[rid] += len(data)
        if self.sizes[rid] > self.limit:
            self.cancel(rid)
            if more:
                self.refused.add(rid)
            return "too_big"
        self.chunks[rid].append(data)
        if more:
            return None
        req, body = self.pending.pop(rid), b"".join(self.chunks.pop(rid))
        self.sizes.pop(rid, None)
        return req, body

    def cancel(self, rid: int) -> None:
        self.pending.pop(rid, None)
        self.chunks.pop(rid, None)
        self.sizes.pop(rid, None)
        self.refused.discard(rid)


@dataclass
class Tunnel:
    url: str
    machine_token: str
    local_base: str  # ex. http://127.0.0.1:8742 (l'API du noyau)
    web_base: str | None = None  # ex. http://web:8080 (fichiers de l'interface, hors /v1)
    on_up: Callable[[], None] | None = None
    on_down: Callable[[], None] | None = None
    state: str = "down"
    # Preuve qu'une requête arrive par CE tunnel (et non d'un processus local qui imiterait les en-têtes du relais) :
    # ajoutée à chaque requête rejouée, vérifiée par le serveur avant de croire `X-Nokker-Account` (pages, §10.3).
    secret: str = field(default_factory=lambda: secrets.token_hex(16), repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _tasks: dict[int, asyncio.Task] = field(default_factory=dict, init=False, repr=False)

    # ── cycle de vie ──
    def start(self) -> None:
        self._thread = threading.Thread(target=lambda: asyncio.run(self._run()), name="nokker-tunnel", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _set(self, state: str) -> None:
        if state == self.state:
            return
        self.state = state
        cb = self.on_up if state == "up" else self.on_down
        try:
            if cb is not None:
                cb()
        except Exception:  # noqa: BLE001 — un rappel en panne ne coupe pas le tunnel
            logger.exception("rappel du tunnel en échec")

    async def _run(self) -> None:
        import websockets

        delay = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                        self.url, subprotocols=[SUBPROTOCOL], max_size=MAX_FRAME,
                        additional_headers={"Authorization": f"Bearer {self.machine_token}"},
                        ping_interval=None, proxy=True) as ws:
                    self._set("up")
                    delay = 1.0
                    await self._serve(ws)
            except Exception as e:  # noqa: BLE001 — réseau, relais redémarré : on retente
                code = getattr(getattr(e, "rcvd", None), "code", None)
                if code == 4001:
                    logger.info("tunnel remplacé par un plus récent (4001) : arrêt")
                    self._set("down")
                    return
                logger.info("tunnel coupé (%s) : nouvel essai dans %.0f s", type(e).__name__, delay)
            self._set("down")
            await asyncio.sleep(delay + random.uniform(0, delay / 2))
            delay = min(delay * 2, 60.0)

    async def _serve(self, ws: Any) -> None:
        inbound = _Inbound()

        async def pinger() -> None:
            while True:
                await asyncio.sleep(PING_S)
                await ws.send(json.dumps({"t": "ping"}))

        ping = asyncio.create_task(pinger())
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=3600.0)) as client:
                async for raw in ws:
                    if self._stop.is_set():
                        return
                    msg = json.loads(raw)
                    kind = msg.get("t")
                    if kind == "ping":
                        await ws.send(json.dumps({"t": "pong"}))
                    elif kind == "cancel":
                        rid = int(msg["id"])
                        task = self._tasks.pop(rid, None)
                        if task is not None:
                            task.cancel()
                        inbound.cancel(rid)  # annulée pendant l'arrivée de son corps
                    elif kind in ("req", "body"):
                        rid = int(msg["id"])
                        got = inbound.feed(msg)
                        if got == "too_big":
                            await ws.send(json.dumps(_res(rid, 413, [["content-type", "application/json"]], json.dumps(
                                {"error": {"message": "corps de requête trop gros (50 Mio au plus)"}}).encode())))
                        elif got is not None:
                            req, body = got
                            if len(self._tasks) >= MAX_IN_FLIGHT:
                                await ws.send(json.dumps(_res(rid, 503, [], "tunnel saturé".encode())))
                                continue
                            self._tasks[rid] = asyncio.create_task(self._forward(ws, client, rid, req, body))
        finally:
            ping.cancel()
            for t in self._tasks.values():
                t.cancel()
            self._tasks.clear()

    async def _forward(self, ws: Any, client: httpx.AsyncClient, rid: int, req: dict[str, Any], body: bytes) -> None:
        path = str(req.get("path") or "/")
        base = self.local_base if path.startswith("/v1/") or path == "/healthz" or not self.web_base else self.web_base
        headers = [(k, v) for k, v in req.get("headers") or [] if k.lower() not in _HOP and k.lower() != "x-nokker-tunnel"]
        headers.append(("X-Nokker-Tunnel", self.secret))
        try:
            async with client.stream(str(req.get("method") or "GET"), base.rstrip("/") + path, headers=headers,
                                     content=body or None) as r:
                out = [[k, v] for k, v in r.headers.items() if k.lower() not in _HOP]
                if r.headers.get("content-type", "").startswith("text/event-stream"):
                    await ws.send(json.dumps(_res(rid, r.status_code, out, b"", more=True)))
                    async for chunk in r.aiter_raw():
                        await ws.send(json.dumps({"t": "body", "id": rid, "data": _b64(chunk), "more": True}))
                    await ws.send(json.dumps({"t": "body", "id": rid, "data": "", "more": False}))
                    return
                data = await r.aread()
                head, rest = data[:CHUNK], data[CHUNK:]
                await ws.send(json.dumps(_res(rid, r.status_code, out, head, more=bool(rest))))
                while rest:
                    part, rest = rest[:CHUNK], rest[CHUNK:]
                    await ws.send(json.dumps({"t": "body", "id": rid, "data": _b64(part), "more": bool(rest)}))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — le relais reçoit une réponse, jamais un silence
            logger.warning("requête relayée %s en échec : %s", path, e)
            await ws.send(json.dumps(_res(rid, 502, [["content-type", "application/json"]],
                                          json.dumps({"error": {"message": "noyau local injoignable"}}).encode())))
        finally:
            self._tasks.pop(rid, None)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _res(rid: int, status: int, headers: list, body: bytes, more: bool = False) -> dict[str, Any]:
    return {"t": "res", "id": rid, "status": status, "headers": headers, "body": _b64(body), "more": more}


__all__ = ["SUBPROTOCOL", "Tunnel"]
