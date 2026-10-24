"""Le VRAI client de tunnel du noyau (nokker/local/tunnel.py) contre ce relais : interface, API filtrée, gros corps,
SSE au fil de l'eau, requêtes en parallèle. Joué quand le paquet nokker et websockets sont importables."""
import asyncio
import os
import sys
import time

import aiohttp
import pytest
from aiohttp import web

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
import relay as R  # noqa: E402

tunnel_mod = pytest.importorskip("nokker.local.tunnel")
pytest.importorskip("websockets")


async def _site(app, port):
    rn = web.AppRunner(app)
    await rn.setup()
    await web.TCPSite(rn, "127.0.0.1", port).start()
    return rn


async def test_bout_en_bout(unused_tcp_port_factory):
    p_relay, p_api, p_web = (unused_tcp_port_factory() for _ in range(3))
    seen = []

    async def api(req):
        seen.append({k.lower(): v for k, v in req.headers.items()})
        if req.path == "/v1/stream":
            r = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await r.prepare(req)
            for i in range(3):
                await r.write(f"data: {i}\n\n".encode())
                await asyncio.sleep(0.2)
            return r
        return web.json_response({"path": req.path, "len": len(await req.read())}, headers={"Set-Cookie": "x=1"})

    a = web.Application(client_max_size=60 << 20)
    a.router.add_route("*", "/{t:.*}", api)
    w = web.Application()
    w.router.add_route("*", "/{t:.*}", lambda r: web.Response(text="<html>interface</html>", content_type="text/html"))

    async def cm(tok):
        return R.Machine("m1", "jean", "t_jean", True) if tok == "mt" else None

    async def ct(tok):
        return {"tenant": "t_jean"} if tok == "a.b.c" else None

    rl = R.Relay(R.Settings(cookie_secret=b"x" * 32, relay_host="127.0.0.1"), cm, ct)
    runners = [await _site(a, p_api), await _site(w, p_web), await _site(R.make_app(rl), p_relay)]
    t = tunnel_mod.Tunnel(url=f"ws://127.0.0.1:{p_relay}/v1/tunnel", machine_token="mt",
                          local_base=f"http://127.0.0.1:{p_api}", web_base=f"http://127.0.0.1:{p_web}")
    t.start()
    try:
        for _ in range(50):
            if "jean" in rl.reg.by_name:
                break
            await asyncio.sleep(0.1)
        assert "jean" in rl.reg.by_name
        base = f"http://127.0.0.1:{p_relay}"
        H = {"Host": "jean.nokker.app"}
        A = {**H, "Authorization": "Bearer a.b.c"}
        async with aiohttp.ClientSession() as s:
            async with s.get(base + "/", headers=H) as r:
                assert r.status == 200 and "interface" in await r.text()
            async with s.get(base + "/v1/threads", headers=H) as r:
                assert r.status == 401
            async with s.get(base + "/v1/threads", headers={**A, "Cookie": "s=1"}) as r:
                assert r.status == 200 and "set-cookie" not in {k.lower() for k in r.headers}
            assert seen[-1]["host"] == "jean.nokker.app" and seen[-1]["x-nokker-relay"] == "1"
            assert seen[-1]["x-nokker-account"] == "t_jean" and "cookie" not in seen[-1]
            data = os.urandom(3 * 1024 * 1024 + 7)
            async with s.post(base + "/v1/up", headers=A, data=data) as r:
                assert (await r.json())["len"] == len(data)
            t0, times = time.monotonic(), []
            async with s.get(base + "/v1/stream", headers=A) as r:
                async for _ in r.content.iter_any():
                    times.append(time.monotonic() - t0)
            assert len(times) == 3 and times[-1] - times[0] > 0.3      # au fil de l'eau, pas en bloc
            res = await asyncio.gather(*[s.get(base + f"/v1/p{i}", headers=A) for i in range(40)])
            assert {r.status for r in res} == {200}
    finally:
        t.stop()
        for rn in runners:
            await rn.cleanup()
