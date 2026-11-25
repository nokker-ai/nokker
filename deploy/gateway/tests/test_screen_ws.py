"""Écran de passation par la passerelle (docs/CONNECTED-SITES.md §4.3) : le seul WebSocket qui traverse le tunnel."""
import asyncio
import base64
import json
import os
import sys

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay as R  # noqa: E402

MACHINES = {"tok-jean": R.Machine("m1", "jean", "t_jean", True)}
GOOD = "T" * 32
PATH = "/sites/screen/st_aaaaaaaaaa/websockify"


async def check_machine(tok):
    return MACHINES.get(tok)


async def check_token(tok):
    return None


@pytest.fixture
async def env():
    relay = R.Relay(R.Settings(cookie_secret=b"x" * 32), check_machine, check_token)
    client = TestClient(TestServer(R.make_app(relay)))
    await client.start_server()
    yield relay, client
    await client.close()


async def machine(client, frames):
    """Faux Lokkal : `wsopen` accepté pour le bon ticket (écho inversé des octets), refusé sinon ; note tout."""
    ws = await client.ws_connect("/v1/tunnel", headers={"Host": "relay.nokker.app", "Authorization": "Bearer tok-jean"},
                                 protocols=("nokker-tunnel.v1",))

    async def loop():
        async for msg in ws:
            f = json.loads(msg.data)
            frames.append(f)
            if f["t"] == "wsopen":
                ok = f["path"].endswith("?ticket=" + GOOD)
                await ws.send_str(json.dumps({"t": "wsok" if ok else "wsclose", "id": f["id"]}))
            elif f["t"] == "ws":
                data = base64.b64decode(f["data"])[::-1]
                await ws.send_str(json.dumps({"t": "ws", "id": f["id"], "data": base64.b64encode(data).decode()}))
            elif f["t"] == "req":
                await ws.send_str(json.dumps({"t": "res", "id": f["id"], "status": 200, "headers": [],
                                              "body": base64.b64encode(b"page").decode(), "more": False}))
    task = asyncio.create_task(loop())
    await asyncio.sleep(0.05)
    return ws, task


def H(**kw):
    return {"Host": "jean.nokker.app", **kw}


async def test_screen_stream_goes_through_the_tunnel(env):
    _, c = env
    frames = []
    _ws, task = await machine(c, frames)
    br = await c.ws_connect(f"{PATH}?ticket={GOOD}", headers=H(), protocols=("binary",))
    await br.send_bytes(b"RFB 003.008\n")
    got = await br.receive(timeout=3)
    assert got.data == b"\n800.300 BFR"
    opened = next(f for f in frames if f["t"] == "wsopen")
    assert opened["path"] == f"{PATH}?ticket={GOOD}" and opened["protocols"] == ["binary"]
    assert "headers" not in opened  # ni cookie, ni jeton, ni compte ne partent vers la machine
    await br.close()
    await asyncio.sleep(0.1)
    assert any(f["t"] == "wsclose" and f["id"] == opened["id"] for f in frames)  # la machine ferme son flux local
    task.cancel()


async def test_ticket_refused_by_the_machine_is_403(env):
    _, c = env
    frames = []
    _ws, task = await machine(c, frames)
    with pytest.raises(aiohttp.WSServerHandshakeError) as e:
        await c.ws_connect(f"{PATH}?ticket={'B' * 32}", headers=H())
    assert e.value.status == 403
    task.cancel()


@pytest.mark.parametrize("path", [
    PATH,                                                   # sans ticket
    f"{PATH}?ticket=court",                                 # ticket mal formé
    f"{PATH}?ticket={GOOD}&x=1",                            # autre paramètre
    f"/sites/screen/st_AAAAAAAAAA/websockify?ticket={GOOD}",  # id hors forme
    f"/sites/screen/st_aaaaaaaaaa/vnc.html?ticket={GOOD}",  # autre chemin de l'écran
    f"/v1/sites/screen/st_aaaaaaaaaa/websockify?ticket={GOOD}",
    f"/sites/screen/st_aaaaaaaaaa/websockify/../x?ticket={GOOD}",
])
async def test_neighbouring_paths_never_open_a_websocket(env, path):
    _, c = env
    frames = []
    _ws, task = await machine(c, frames)
    with pytest.raises(aiohttp.WSServerHandshakeError):
        await c.ws_connect(path, headers=H())
    assert not any(f["t"] == "wsopen" for f in frames)
    task.cancel()


async def test_at_most_two_screens_per_machine(env):
    _, c = env
    frames = []
    _ws, task = await machine(c, frames)
    a = await c.ws_connect(f"{PATH}?ticket={GOOD}", headers=H())
    b = await c.ws_connect(f"{PATH}?ticket={GOOD}", headers=H())
    with pytest.raises(aiohttp.WSServerHandshakeError) as e:
        await c.ws_connect(f"{PATH}?ticket={GOOD}", headers=H())
    assert e.value.status == 503
    await a.close()
    await b.close()
    task.cancel()


async def test_offline_machine_is_502(env):
    _, c = env
    with pytest.raises(aiohttp.WSServerHandshakeError) as e:
        await c.ws_connect(f"{PATH}?ticket={GOOD}", headers=H())
    assert e.value.status == 502
