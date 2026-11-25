"""tests/e2e_sites/chain.sh. Chaîne complète de l'écran par la passerelle : navigateur → relais (vrai code) → tunnel (vrai client) → web Lokkal
(nginx, lokkal.conf) → sites-chrome (enfant actif) → websockify → x11vnc. Doit rendre la bannière RFB."""
import asyncio
import json
import os
import sys
import http.client
sys.path.insert(0, "/src/deploy/gateway")
import aiohttp
from aiohttp import web
import relay as R
from nokker.local.tunnel import Tunnel
from nokker.tools.browser_pool import _McpCaller
RES = []
def ok(n, c, d=""):
    RES.append(bool(c))
    print(("VERT " if c else "ROUGE"), "|", n, "|", str(d)[:200])
    sys.stdout.flush()
TOK = os.environ["NOKKER_SITES_BROWSER_TOKEN"]
CID = "st_chainchain"
M = R.Machine("m1", "jean", "t_jean", True)
async def check_machine(t):
    return M if t == "mt-jean" else None
async def check_token(t):
    return None

async def main():
    relay = R.Relay(R.Settings(cookie_secret=b"x" * 32), check_machine, check_token)
    runner = web.AppRunner(R.make_app(relay))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 80).start()
    t = Tunnel(url="ws://relay.nokker.app/v1/tunnel", machine_token="mt-jean", local_base="http://127.0.0.1:9",
               web_base="http://web:8080")
    t.start()
    for _ in range(50):
        if t.state == "up":
            break
        await asyncio.sleep(0.1)
    ok("tunnel ouvert (vrai client → vrai relais)", t.state == "up", t.state)
    # enfant actif sur sites-chrome (comme une passation en cours), puis ticket comme le noyau
    c = _McpCaller(f"http://sc:8119/s/{CID}/mcp", {"Authorization": f"Bearer {TOK}"})
    await asyncio.to_thread(c.call, "browser_navigate", {"url": "about:blank", "snapshot": False})
    h = http.client.HTTPConnection("sc", 8119, timeout=20)
    h.request("POST", f"/s/{CID}/screen-ticket", headers={"Authorization": f"Bearer {TOK}"})
    tk = json.loads(h.getresponse().read())
    path = tk["path"]
    ok("ticket du noyau (chemin relatif)", path.startswith(f"/sites/screen/{CID}/?ticket="), path[:60])
    H = {"Host": "jean.nokker.app"}
    async with aiohttp.ClientSession() as s:
        async with s.get("http://127.0.0.1" + path, headers=H, allow_redirects=False) as r:
            loc = r.headers.get("Location", "")
            ok("page d'écran par la passerelle → 303 vers noVNC", r.status == 303 and "vnc.html" in loc, (r.status, loc[:120]))
            ok("aucun cookie ne revient par le relais", "Set-Cookie" not in r.headers, list(r.headers))
        async with s.get(f"http://127.0.0.1/sites/screen/{CID}/vnc.html", headers=H) as r:
            ok("noVNC servi par la passerelle", r.status == 200 and "noVNC" in await r.text(), r.status)
        ticket = path.split("ticket=")[1]
        ws = await s.ws_connect(f"http://127.0.0.1/sites/screen/{CID}/websockify?ticket={ticket}", headers=H, protocols=("binary",))
        m = await ws.receive(timeout=15)
        ok("flux VNC de bout en bout : bannière RFB", m.type == aiohttp.WSMsgType.BINARY and m.data.startswith(b"RFB 00"), m.data[:12] if m.data else m.type)
        try:
            ws2 = await s.ws_connect(f"http://127.0.0.1/sites/screen/{CID}/websockify?ticket={ticket}", headers=H)
            ok("second flux avec le même ticket refusé", False, "accepté")
            await ws2.close()
        except aiohttp.WSServerHandshakeError as e:
            ok("second flux avec le même ticket refusé", e.status == 403, e.status)
        await ws.close()
        await asyncio.sleep(1)
        try:
            ws3 = await s.ws_connect(f"http://127.0.0.1/sites/screen/{CID}/websockify?ticket={'Z' * 32}", headers=H)
            ok("ticket inconnu refusé", False, "accepté")
            await ws3.close()
        except aiohttp.WSServerHandshakeError as e:
            ok("ticket inconnu refusé", e.status == 403, e.status)
        ws4 = await s.ws_connect(f"http://127.0.0.1/sites/screen/{CID}/websockify?ticket={ticket}", headers=H, protocols=("binary",))
        m4 = await ws4.receive(timeout=15)
        ok("flux rouvert après fermeture (reconnexion noVNC)", m4.data[:6] == b"RFB 00" if m4.data else False, m4.type)
        await ws4.close()
    t.stop()
    print(f"RÉSUMÉ : {sum(RES)} / {len(RES)} verts")
asyncio.run(main())
