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
PING_S = 25.0
_HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "content-length", "set-cookie"}


@dataclass
class Tunnel:
    url: str
    machine_token: str
    local_base: str  # ex. http://127.0.0.1:8742 (l'API du noyau)
    web_base: str | None = None  # ex. http://web:8080 (fichiers de l'interface, hors /v1)
    on_up: Callable[[], None] | None = None
    on_down: Callable[[], None] | None = None
    state: str = "down"
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
        bodies: dict[int, list[bytes]] = {}
        pending: dict[int, dict[str, Any]] = {}

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
                        task = self._tasks.pop(int(msg["id"]), None)
                        if task is not None:
                            task.cancel()
                    elif kind in ("req", "body"):
                        rid = int(msg["id"])
                        if kind == "req":
                            pending[rid] = msg
                            bodies[rid] = [base64.b64decode(msg.get("body") or "")]
                        else:
                            bodies.setdefault(rid, []).append(base64.b64decode(msg.get("data") or ""))
                        if not msg.get("more"):
                            req = pending.pop(rid, None)
                            if req is None:
                                continue
                            if len(self._tasks) >= MAX_IN_FLIGHT:
                                await ws.send(json.dumps(_res(rid, 503, [], "tunnel saturé".encode())))
                                continue
                            body = b"".join(bodies.pop(rid, []))
                            self._tasks[rid] = asyncio.create_task(self._forward(ws, client, rid, req, body))
        finally:
            ping.cancel()
            for t in self._tasks.values():
                t.cancel()
            self._tasks.clear()

    async def _forward(self, ws: Any, client: httpx.AsyncClient, rid: int, req: dict[str, Any], body: bytes) -> None:
        path = str(req.get("path") or "/")
        base = self.local_base if path.startswith("/v1/") or path == "/healthz" or not self.web_base else self.web_base
        headers = [(k, v) for k, v in req.get("headers") or [] if k.lower() not in _HOP]
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
