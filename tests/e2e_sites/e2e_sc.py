"""e2e sites-chrome : vrai Chrome, pilote McpBrowserDriver + MaskedDriver, site de connexion simulé (réseau de test)."""
import json
import os
import sys
import time
import http.client
from types import SimpleNamespace
from nokker.sites.build import _mcp_driver_factory
from nokker.sites.driver import MaskedDriver
R = []
def ok(name, cond, detail=""):
    R.append(bool(cond))
    print(("VERT " if cond else "ROUGE"), "|", name, "|", str(detail)[:220])
    sys.stdout.flush()
TOK = os.environ["NOKKER_SITES_BROWSER_TOKEN"]
SC = "sc"
def api(method, path, body=None, port=8119, auth=True, headers=None):
    c = http.client.HTTPConnection(SC, port, timeout=60)
    h = dict(headers or {})
    if auth:
        h["Authorization"] = f"Bearer {TOK}"
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read()
A, B = SimpleNamespace(id="st_aaaaaaaaaa"), SimpleNamespace(id="st_bbbbbbbbbb")
fac = _mcp_driver_factory()
PW = "Mdp-de-test-7Qz!"

# 1. connexion avec le mot de passe masqué
t0 = time.time()
d = MaskedDriver(fac(A, None))
d.goto("http://fakesite:8080/login")
ok("premier geste (Chrome à froid)", True, f"{time.time()-t0:.1f} s")
nodes = d.nodes()
ok("snapshot parsé (rôles et noms)", any(n.role == "textbox" for n in nodes) and any(n.role == "button" for n in nodes),
   [(n.role, n.name, n.attrs.get('type','')) for n in nodes][:8])
user = next(n for n in nodes if n.role == "textbox" and "Identifiant" in n.name)
pw = next((n for n in nodes if n.role == "textbox" and n.attrs.get("type") == "password"), None) \
     or next(n for n in nodes if n.role == "textbox" and "Mot de passe" in n.name)
ok("champ mot de passe reconnu comme tel", pw.attrs.get("type") == "password", (pw.name, pw.attrs))
d.type(user.ref, "alice")
fill = getattr(d, "fill_secret", None)
if fill:
    fill(pw.ref, PW)
else:
    d.mark_secret(pw.ref)
    d.type(pw.ref, PW)
shot = d.screenshot()
ok("capture pendant la connexion : jamais le mot de passe", PW.encode() not in shot, f"{len(shot)} octets")
snap = "\n".join(f"{n.role} {n.name} {n.value}" for n in d.nodes())
ok("snapshot après saisie : mot de passe absent", PW not in snap)
btn = next(n for n in d.nodes() if n.role == "button")
d.click(btn.ref)
time.sleep(1.5)
d.goto("http://fakesite:8080/feed/")
txt = d.text()
ok("connecté : fil lu", "Bonjour alice" in txt, txt[:120])
prof = d.export_profile()
ok("export du profil (tar.gz)", prof[:2] == b"\x1f\x8b" and len(prof) > 1000, f"{len(prof)} octets")
d.close()

# 2. écran de passation (ticket, cookie, noVNC, websockify) pendant que l'enfant vit
st, _, b = api("POST", f"/s/{A.id}/screen-ticket")
tk = json.loads(b) if st == 200 else {}
ok("ticket d'écran", st == 200 and "ticket" in tk, st)
st, h, _ = api("GET", f"/screen/{A.id}/?ticket={tk.get('ticket','x')}", port=8120, auth=False)
cookie = (h.get("Set-Cookie") or "").split(";")[0]
ok("ticket → 303 + cookie propre au connecteur", st == 303 and cookie.startswith(f"nk_screen_{A.id}="), (st, h.get("Location", "")[:90]))
st, _, b = api("GET", f"/screen/{A.id}/vnc.html", port=8120, auth=False, headers={"Cookie": cookie})
ok("noVNC servi avec le cookie", st == 200 and b"noVNC" in b, st)
loc = h.get("Location", "")
ok("le flux porte le ticket (passerelle : pas de cookie)", f"websockify%3Fticket%3D{tk.get('ticket')}" in loc, loc[:160])
st, _, _ = api("GET", f"/screen/{A.id}/vnc.html", port=8120, auth=False)
ok("fichiers de noVNC publics (génériques)", st == 200, st)
WSH = {"Connection": "Upgrade", "Upgrade": "websocket", "Sec-WebSocket-Version": "13",
       "Sec-WebSocket-Key": "dGhlIHNh" + "bXBsZSBub25jZQ==", "Sec-WebSocket-Protocol": "binary"}
def ws(path, **h):
    c = http.client.HTTPConnection(SC, 8120, timeout=10)
    c.request("GET", path, headers={**WSH, **h})
    return c, c.getresponse().status
c0, s0 = ws(f"/screen/{A.id}/websockify")
ok("flux refusé sans ticket ni cookie", s0 == 403, s0)
c0.close()
c0, s0 = ws(f"/screen/{B.id}/websockify", Cookie=cookie.replace(A.id, B.id))
ok("cookie de A refusé pour B", s0 == 403, s0)
c0.close()
c1, s1 = ws(f"/screen/{A.id}/websockify?ticket={tk.get('ticket')}")
ok("flux ouvert avec le ticket dans l'URL", s1 == 101, s1)
c2, s2 = ws(f"/screen/{A.id}/websockify", Cookie=cookie)
ok("un seul flux à la fois par ticket", s2 == 403, s2)
c2.close()
c1.close()
time.sleep(0.5)
c3, s3 = ws(f"/screen/{A.id}/websockify", Cookie=cookie)
ok("flux rouvert (cookie) une fois le premier fermé", s3 == 101, s3)
c3.close()
st, _, _ = api("GET", f"/screen/{A.id}/?ticket={tk.get('ticket','x')}x", port=8120, auth=False)
ok("ticket inconnu → 410", st == 410, st)

# 3. profil : enfant arrêté, nouveau pilote sur l'archive → toujours connecté
api("POST", f"/s/{A.id}/stop")
time.sleep(9)
d2 = MaskedDriver(fac(A, prof))
d2.goto("http://fakesite:8080/feed/")
ok("profil réimporté : session gardée", "Bonjour alice" in d2.text() and d2.url().endswith("/feed/"), d2.url())
# 4. sortie : plages privées refusées (proxy de l'image), site simulé seul autorisé
for url in ("http://192.168.1.1/", "http://169.254.169.254/latest/meta-data/", "http://fakesite:8081/"):
    d2.goto(url)
    t = d2.text()
    ok(f"sortie refusée : {url}", "Bonjour" not in t and ("refus" in t.lower() or "interne" in t.lower() or "err" in t.lower() or len(t) < 400), t[:140])
d2.close()

# 5. deux connecteurs : écrans distincts
dB = MaskedDriver(fac(B, None))
dB.goto("http://fakesite:8080/login")
ok("second site : son Chrome répond (connexion, port CDP propre)", any(n.role == "textbox" for n in dB.nodes()), dB.url())
st, _, b = api("GET", "/state")
ch = json.loads(b)["children"] if st == 200 else []
ok("deux sites actifs : écrans distincts", len({c["display"] for c in ch}) == len(ch) >= 2, ch)
dB.close()
st, _, _ = api("POST", "/s/st_aaaaaaaaaa/mcp", auth=False)
ok("MCP sans jeton → 401", st == 401, st)
print(f"RÉSUMÉ : {sum(R)} / {len(R)} verts")
