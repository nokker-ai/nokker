"""Portails simulés servis en HTTP (e2e du lot L2) : rendu accessible, formulaire multipart (valeurs, cases, fichiers)."""
from __future__ import annotations

import hashlib

from nokker.jobs.apply.fakeats import SUCCESSFACTORS, WORKDAY, FakeAtsServer
from nokker.jobs.apply.fakeats_http import FakeAtsHttp, parse_form

HOST = "fakewd:8080"


def _jar(headers: dict, jar: dict) -> dict:
    for c in headers.get("Set-Cookie", []):
        k, _, v = c.split(";")[0].partition("=")
        jar[k] = v
    return jar


def _refs(app: FakeAtsHttp, jar: dict) -> dict[str, str]:
    _tab, fb = app.browser(jar)
    return {n.name: n.ref for n in fb.nodes()}


def test_workday_guest_form_posts_values_files_and_checks_then_submits():
    srv = FakeAtsServer(skin=dict(WORKDAY, host=HOST))
    app, jar = FakeAtsHttp(srv), {}
    st, h, body = app.handle("GET", "/job/R1", jar, {})
    _jar(h, jar)
    assert st == 200 and "Ingénieure Python" in body and "Postuler" in body
    st, h, _ = app.handle("POST", "/__act", jar, {"__click": _refs(app, jar)["Postuler"]})
    assert h["Location"] == "/start"
    app.handle("POST", "/__act", jar, {"__click": _refs(app, jar)["Postuler en tant qu'invité"]})
    st, _h, body = app.handle("GET", "/apply/R1", jar, {})
    assert '<select name="f_' in body and "Prénom *" in body
    r = _refs(app, jar)
    values = {"Prénom *": "Camille", "Nom de famille *": "Martin", "Adresse e-mail *": "c@example.org",
              "Téléphone *": "1", "Adresse *": "Rue 1", "NPA *": "1204", "Ville *": "Genève", "Pays *": "Suisse"}
    form = {f"f_{r[k]}": v for k, v in values.items()}
    app.handle("POST", "/__act", jar, {**form, "__click": r["Enregistrer et continuer"]})
    r = _refs(app, jar)
    pdf = b"%PDF-1.4 test"
    app.handle("POST", "/__act", jar, {f"u_{r['Téléverser le CV *']}": ("cv.pdf", pdf),
                                       "__click": r["Enregistrer et continuer"]})
    _tab, fb = app.browser(jar)
    assert fb.files["Téléverser le CV"] == ("cv.pdf", hashlib.sha256(pdf).hexdigest(), len(pdf)) and fb.step == 2
    body = app.handle("GET", "/apply/R1", jar, {})[2]
    assert "Kubernetes" in body


def test_disabled_privacy_box_until_read_and_multipart_parser():
    srv = FakeAtsServer(skin=dict(SUCCESSFACTORS, host="fakesf:8080"))
    app, jar = FakeAtsHttp(srv), {}
    _jar(app.handle("GET", "/job/R1", jar, {})[1], jar)
    app.handle("POST", "/__act", jar, {"__click": _refs(app, jar)["Postuler maintenant"]})
    app.handle("POST", "/__act", jar, {"__click": _refs(app, jar)["Créer un compte"]})
    body = app.handle("GET", "/account/create", jar, {})[2]
    assert 'type="checkbox"' in body and " disabled>" in body
    app.handle("GET", "/privacy", jar, {})
    body = app.handle("GET", "/account/create", jar, {})[2]
    assert " disabled>" not in body
    raw = (b'--b\r\nContent-Disposition: form-data; name="f_e1"\r\n\r\nx\r\n'
           b'--b\r\nContent-Disposition: form-data; name="u_e2"; filename="a.pdf"\r\nContent-Type: application/pdf'
           b'\r\n\r\n%PDF\r\n--b--\r\n')
    assert parse_form("multipart/form-data; boundary=b", raw) == {"f_e1": "x", "u_e2": ("a.pdf", b"%PDF")}
