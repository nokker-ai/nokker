"""§13.3 étape 1 : la recette LinkedIn (libellés FR) jouée dans le VRAI Chrome de sites-chrome contre fakesocial en HTTP,
comparée geste par geste à la même recette sur le FakeBrowser en mémoire (même contenu)."""
import os
import re as _re
import sys
import time
import http.client
from dataclasses import replace
from types import SimpleNamespace
from nokker.sites.build import _mcp_driver_factory
from nokker.sites.driver import MaskedDriver
from nokker.sites.fakesocial import LINKEDIN_FR, FakeServer, FakeBrowser, Account
from nokker.sites.recipes import LINKEDIN_RECIPE
from nokker.sites.recipes import base as rb
R = []
def ok(name, cond, detail=""):
    R.append(bool(cond))
    print(("VERT " if cond else "ROUGE"), "|", name, "|", str(detail)[:260])
    sys.stdout.flush()
HOST = "fakesocial:8080"
MFA = os.environ.get("MFA", "none")
PW = "Mdp-Test-91xQ"
class HttpRecipe(rb.SocialRecipe):
    @property
    def base(self):
        return f"http://{self.hosts[0]}"
labels = dict(LINKEDIN_FR, login_url=f"http://{HOST}/login")
real_r = HttpRecipe(**{**LINKEDIN_RECIPE.__dict__, "hosts": (HOST,), "labels": labels})
fake_r = replace(LINKEDIN_RECIPE, hosts=(HOST,), labels=dict(LINKEDIN_FR, login_url=f"https://{HOST}/login"))
srv = FakeServer(skin=dict(LINKEDIN_FR, host=HOST)).seed()
srv.accounts["alex@example.org"] = Account("alex@example.org", PW, "Alex Martin", mfa=MFA)
fake = FakeBrowser(srv)

conn = SimpleNamespace(id="st_recipe0001")
real = MaskedDriver(_mcp_driver_factory()(conn, None))

def login(r, nav, label):
    nav.goto(r.login_url)
    steps = []
    for _ in range(6):
        st = r.detect(nav.nodes(), nav.url())
        steps.append(st)

        if st == rb.LOGGED_IN:
            break
        if st == rb.ASK_USERNAME:
            r.type_username(nav, "alex@example.org")
        elif st == rb.ASK_PASSWORD:
            r.fill_password(nav, lambda ref: nav.fill_secret(ref, PW) if hasattr(nav, "fill_secret") else nav.type(ref, PW))
        elif st == rb.ASK_CODE:
            r.fill_code(nav, lambda ref: nav.fill_secret(ref, "482913") if hasattr(nav, "fill_secret") else nav.type(ref, "482913"))
        else:
            break
        time.sleep(0.3)
    return steps
fs = login(fake_r, fake, "simulé")
rs = login(real_r, real, "chrome")
ok(f"connexion (mfa={MFA}) : mêmes étapes que le simulé", fs == rs and rs[-1] == rb.LOGGED_IN, f"chrome {rs} / simulé {fs}")
ok("nom du compte lu", real_r.account_name(real.nodes()) == "Alex Martin", real_r.account_name(real.nodes()))
ok("mot de passe jamais dans le snapshot", PW not in real.snapshot())

_INV = _re.compile("[​-‏⁠﻿]")
def norm(items, meta=True):
    return [(i.ref_kind, (i.url or "").replace("https://", "").replace("http://", ""),
             {k: _INV.sub("", v) for k, v in i.fields.items()}, i.meta if meta else {}) for i in items]
def same(name, fr, rr, meta=True):
    a, b = norm(fr, meta), norm(rr, meta)
    ok(name, a == b and len(a) > 0, b if a == b else f"chrome {b}\n        simulé {a}")
same("notifications", fake_r.list(fake, "notifications", 20), real_r.list(real, "notifications", 20))
fm, rm = fake_r.list(fake, "messages", 20), real_r.list(real, "messages", 20)
same("messagerie (fils, dernier message, date, non lu)", fm, rm)
if rm:
    same("fil lu (contenu piégé)", fake_r.read(fake, "msg", fm[1].url)[1], real_r.read(real, "msg", rm[1].url)[1])
same("profil (titre, degré)", fake_r.read(fake, "in", fake_r.base + "/in/sam-martin/")[1], real_r.read(real, "in", real_r.base + "/in/sam-martin/")[1])
same("recherche personnes (nom, titre, lien)", fake_r.search(fake, "people", "martin", 10), real_r.search(real, "people", "martin", 10), meta=False)
fp, rp = fake_r.search(fake, "posts", "produit", 10), real_r.search(real, "posts", "produit", 10)
same("recherche posts", fp, rp)
if rp:
    same("post et commentaires", fake_r.read(fake, "post", fp[0].url)[1], real_r.read(real, "post", rp[0].url)[1])
prof = real.export_profile()
real.close()
c = http.client.HTTPConnection("sc", 8119, timeout=30)
c.request("POST", f"/s/{conn.id}/stop", headers={"Authorization": f"Bearer {os.environ['NOKKER_SITES_BROWSER_TOKEN']}"})
c.getresponse().read()
time.sleep(9)
again = MaskedDriver(_mcp_driver_factory()(conn, prof))
again.goto(real_r.base + "/feed/")
ok("profil réimporté : toujours connecté (cookie du site dans le profil)", real_r.detect(again.nodes(), again.url()) == rb.LOGGED_IN, again.url())
again.close()
print(f"RÉSUMÉ : {sum(R)} / {len(R)} verts")
