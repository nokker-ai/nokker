"""Pack Emploi, lot L2 : les recettes Workday et SuccessFactors jouées dans le VRAI Chrome de sites-chrome contre les
portails simulés servis en HTTP (`nokker.jobs.apply.fakeats_http`), comparées au même `jobsd` sur le portail en mémoire :
relevé à blanc (mêmes champs, mêmes questions), rejeu à l'approbation (mêmes valeurs, mêmes fichiers reçus par le
portail), page de confirmation capturée ; SuccessFactors : compte créé avec un mot de passe du coffre et code lu dans la
boîte. Aucun portail réel."""
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from nokker.jobs.apply.desk import JobsDesk, Offer, Piece
from nokker.jobs.apply.fakeats import SUCCESSFACTORS, WORKDAY, FakeAtsBrowser, FakeAtsServer
from nokker.jobs.apply.model import FilePart
from nokker.jobs.apply.portals import PORTALS
from nokker.sites.build import _mcp_driver_factory
from nokker.sites.governor import Governor
from tests.unit.sites.helpers import make_stack

R = []


def ok(name, cond, detail=""):
    R.append(bool(cond))
    print(("VERT " if cond else "ROUGE"), "|", name, "|", str(detail)[:400])
    sys.stdout.flush()


PDF = b"%PDF-1.4\n1 0 obj << /Type /Page >> endobj\n%%EOF"
CV = Piece(FilePart("cv", "x|cv:1", "Camille_Martin_CV_Acme_SA.pdf", __import__("hashlib").sha256(PDF).hexdigest(),
                    len(PDF), 1), PDF)
SHEET = {"first_name": "Camille", "last_name": "Martin", "email": "camille@example.org", "phone": "+41 79 000 00 00",
         "street": "Rue du Lac 1", "postcode": "1204", "city": "Genève", "country": "Suisse", "work_auth_ch": "Oui",
         "salary": "110000"}
ANSWERS = {"combien d'années d'expérience avec kubernetes ?": "2"}


def state(host):
    with urllib.request.urlopen(f"http://{host}/__state", timeout=10) as r:  # noqa: S310 — réseau du test
        return json.load(r)


def make_desk(host, real, servers):
    stack = make_stack(tmp=Path(tempfile.mkdtemp()))
    ats = "workday" if "wd" in host else "successfactors"
    recipes = {ats: PORTALS[ats].with_hosts(host.split(":")[0])}
    if real:
        factory = _mcp_driver_factory()
    else:
        def factory(conn, profile):
            return FakeAtsBrowser(servers[host], profile)

    def mail_code(conn, sender, since):
        out = state(host)["outbox"] if real else servers[host].outbox
        return next((m["code"] for m in reversed(out) if m.get("code")), None)

    desk = JobsDesk(sites=stack.service, master=b"m" * 32, state_dir=Path(tempfile.mkdtemp()), recipes=recipes,
                    driver_factory=factory, mail_code=mail_code,
                    governor_factory=lambda r: Governor(limits={}, sleep=lambda s: None, busy_wait_s=5),
                    sleep=lambda s: time.sleep(min(s, 1.0)), handoff_wait_s=60, handoff_poll_s=2)
    desk.sheets.put("us_camille", "em_test000001", fields=SHEET, answers=ANSWERS)
    return desk, stack


def plan_view(plan):
    return [(f.page, f.id, f.label, f.kind, f.required, f.value, f.source) for f in plan.fields]


def run_workday():
    host = os.environ.get("WD", "fakewd:8080")
    mem = FakeAtsServer(skin=dict(WORKDAY, host=host))
    servers = {host: mem}
    offer = Offer("em_test000001|offer:42", 42, f"http://{host}/job/R1", "Acme SA", "Ingénieure Python", "Genève",
                  "workday")
    results = {}
    for real in (False, True):
        desk, _ = make_desk(host, real, servers)
        t0 = time.time()
        res = desk.scan(account="us_camille", profile="em_test000001", offer=offer, pieces={"cv": CV}, answers={},
                        username="camille@example.org")
        results[real] = (res, desk, time.time() - t0)
    (fres, fdesk, _), (rres, rdesk, rt) = results[False], results[True]
    ok("Workday : relevé à blanc, statut", fres.status == rres.status == "ready", f"{fres.status} / {rres.status}")
    ok("Workday : mêmes champs, valeurs et sources que le simulé", plan_view(fres.plan) == plan_view(rres.plan),
       f"chrome {plan_view(rres.plan)}\n simulé {plan_view(fres.plan)}")
    ok("Workday : rien d'envoyé par le relevé", not state(host)["submissions"], f"{rt:.0f} s")
    t0 = time.time()
    out = rdesk.submit(rres.plan, {"cv": CV})
    st = state(host)
    ok("Workday : envoi rejoué, page « Félicitations » capturée", out.status == "sent" and out.proof.get("sha256"),
       f"{out.status} {out.reference} {time.time() - t0:.0f} s")
    sub = st["submissions"][-1] if st["submissions"] else {}
    fdesk.submit(fres.plan, {"cv": CV})
    msub = mem.submissions[-1].__dict__
    ok("Workday : le portail a reçu exactement les valeurs du plan (comme le simulé)",
       sub.get("values") == msub["values"] and sub.get("checks") == msub["checks"],
       f"diff valeurs {set(sub.get('values', {}).items()) ^ set(msub['values'].items())} "
       f"cases {set(sub.get('checks', {}).items()) ^ set(msub['checks'].items())}")
    ok("Workday : le CV reçu est le fichier de la carte (nom, empreinte)",
       sub.get("files", {}).get("Téléverser le CV", [None, None])[:2] == [CV.part.name, CV.part.sha256],
       sub.get("files"))


def run_successfactors():
    host = os.environ.get("SF", "fakesf:8080")
    mem = FakeAtsServer(skin=dict(SUCCESSFACTORS, host=host))
    servers = {host: mem}
    offer = Offer("em_test000001|offer:43", 43, f"http://{host}/job/R1", "Acme SA", "Ingénieure Python", "Genève",
                  "successfactors")
    results = {}
    for real in (False, True):
        desk, stack = make_desk(host, real, servers)
        res = desk.scan(account="us_camille", profile="em_test000001", offer=offer, pieces={"cv": CV}, answers={},
                        username="camille@example.org")
        ok(f"SuccessFactors ({'chrome' if real else 'simulé'}) : compte à créer d'abord (carte forte)",
           res.status == "account" and "account" in res.plan.strong, res.status)
        made = desk.create_account(res.plan, offer, {"cv": CV}, {})
        [conn] = desk.store.of("us_camille")
        pw = stack.vault.values.get(f"sites.{conn.id}.password", "")
        results[real] = (made, desk, pw)
    (fm, fdesk, _), (rm, rdesk, pw) = results[False], results[True]
    st = state(host)
    ok("SuccessFactors : compte créé chez le portail, mot de passe du coffre conforme (8-18)",
       "camille@example.org" in st["accounts"] and 8 <= len(pw) <= 18, len(pw))
    ok("SuccessFactors : relevé après création, mêmes champs que le simulé",
       fm.status == rm.status == "ready" and plan_view(fm.plan) == plan_view(rm.plan),
       f"chrome {rm.status} {plan_view(rm.plan)}\n simulé {fm.status} {plan_view(fm.plan)}")
    try:
        out = rdesk.submit(rm.plan, {"cv": CV})
    except Exception as e:  # noqa: BLE001
        ok("SuccessFactors : envoi", False, f"{getattr(e, 'code', e)} {getattr(e, 'extra', '')}")
        return
    st = state(host)
    sub = st["submissions"][-1] if st["submissions"] else {}
    fdesk.submit(fm.plan, {"cv": CV})
    msub = mem.submissions[-1].__dict__
    ok("SuccessFactors : « Merci d'avoir postulé » capturée, accusé envoyé par le portail",
       out.status == "sent" and any(m.get("ref") for m in st["outbox"]), out.status)
    ok("SuccessFactors : valeurs et pièce reçues comme le simulé (CV re-téléversé avant l'envoi)",
       sub.get("values") == msub["values"] and sub.get("files", {}).get("CV", [None, None])[:2]
       == [CV.part.name, CV.part.sha256], f"chrome {sub}\n simulé {msub}")


run_workday()
run_successfactors()
print(f"{sum(R)}/{len(R)}")
sys.exit(0 if all(R) else 1)
