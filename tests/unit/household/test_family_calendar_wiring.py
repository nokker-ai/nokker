"""Calendrier familial câblé dans le VRAI serveur (config de la bêta + `household_paper` + `family_calendar`), vrai
Handler, vrai noyau : le PDF synthétique du club importé par la route des Papiers, un tour scripté qui propose puis
écrit → UNE carte `family:calendar`, rien dans l'agenda avant ; le brouillon s'édite par la route du compte et reste
invisible au membre et au voisin ; outils absents du locataire interne. Drapeau éteint : 404, aucun outil."""
from __future__ import annotations

import base64
from pathlib import Path

from tests.unit.household.test_paper_service import NOW
from tests.unit.household.test_paper_wiring import _rig
from tests.unit.tenancy.test_isolation_suite import FakeBot, _session  # noqa: F401

PDF = (Path(__file__).parent / "fixtures" / "famcal" / "planning-club.pdf").read_bytes()


def _close(w):
    from nokker.household import http as household_http
    from nokker.household.famcal import http as famcal_http
    from nokker.household.paper import http as paper_http
    from nokker.team import http as team_http

    w.httpd.shutdown()
    for mod in (household_http, team_http, paper_http, famcal_http):
        mod.configure(None)


def test_family_calendar_wired_in_the_real_server(monkeypatch):
    from nokker.api.server import configure_family_calendar
    from nokker.household.famcal import http as famcal_http
    from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

    w = _rig(monkeypatch, paper=True)
    try:
        cfg = type("C", (), {"family_calendar": True, "pg_dsn": lambda self: ""})()
        configure_family_calendar(cfg, w.kernel, w.tenancy)
        svc = famcal_http.service()
        svc.new_id, svc.clock = (lambda: "fc_00000000000000aa"), (lambda: NOW)
        with tenant_scope(w.p.tenant_id):
            assert w.kernel.tools.spec("family_calendar_commit") is not None
        with tenant_scope(INTERNAL_TENANT):
            assert w.kernel.tools.spec("family_calendar_commit") is None
        st, out = w.call("POST", "/v1/paper/documents", {"filename": "planning-club.pdf",
                                                          "content_base64": base64.b64encode(PDF).decode()}, who="P")
        assert st == 201
        doc_id = out["document"]["id"]
        events = [{"title": "Tournoi", "date": "2026-11-14", "start": "09:00", "location": "Onex",
                   "source": "Samedi 14 novembre : tournoi a Onex, 09h00", "year_source": "Saison 2026-2027"},
                  {"title": "Match amical", "date": "2026-12-05", "source": "Match amical : 05/12, 10h00",
                   "year_source": "Saison 2026-2027"}]
        st, _ = w.chat("mets le planning du club dans l'agenda", "P", [
            {"tool": "family_calendar_propose", "args": {"document_id": doc_id, "events": events}},
            {"tool": "family_calendar_commit", "args": {"draft_id": "fc_00000000000000aa", "revision": 1}}])
        assert st == 200
        with tenant_scope(w.p.tenant_id):
            [card] = [c for c in w.approvals.pending() if c.tool_name == "family_calendar_commit"]
        assert card.rule == "family:calendar" and card.strength == "standard"
        assert card.reason.startswith("NON écrit : ligne 2, Match amical (2026-12-05) — format J/M ou M/J ambigu.")
        assert "1. 2026-11-14 09:00 Tournoi @ Onex" in card.reason
        st, d = w.call("GET", "/v1/family-calendar/drafts/fc_00000000000000aa", who="P")
        assert st == 200 and d["draft"]["origin"] == "« planning-club.pdf »"
        st, d = w.call("PATCH", "/v1/family-calendar/drafts/fc_00000000000000aa/lines/2", {"action": "reject"},
                       who="P")
        assert st == 200 and d["draft"]["revision"] == 2
        for who in ("M", "O"):
            assert w.call("GET", "/v1/family-calendar/drafts/fc_00000000000000aa", who=who)[0] == 404
            assert w.call("GET", "/v1/family-calendar/drafts", who=who) == (200, {"drafts": []})
    finally:
        _close(w)


def test_flag_off_routes_404_and_no_tools(monkeypatch):
    from nokker.api.server import configure_family_calendar
    from nokker.config import NokkerConfig
    from nokker.household.famcal import http as famcal_http
    from nokker.tenancy.context import tenant_scope

    w = _rig(monkeypatch, paper=True)
    try:
        gates = w.kernel.gates
        configure_family_calendar(NokkerConfig(), w.kernel, w.tenancy)
        assert not famcal_http.enabled() and w.kernel.gates is gates
        assert w.call("GET", "/v1/family-calendar/drafts", who="P")[0] == 404
        with tenant_scope(w.p.tenant_id):
            assert w.kernel.tools.spec("family_calendar_propose") is None
            assert w.kernel.tools.spec("paper_calendar_add") is not None  # Papiers inchangés
    finally:
        _close(w)


def test_both_flags_leave_one_way_to_the_calendar(monkeypatch):
    """Papiers + calendrier familial allumés : `paper_calendar_add` sort du catalogue (spec, index, appel direct) ;
    la date isolée d'un courrier passe par `family_calendar_*` ; les autres outils des Papiers restent."""
    from nokker.api.server import configure_family_calendar
    from nokker.household.famcal import http as famcal_http
    from nokker.household.paper.tools import PAPER_TOOLS, PaperTools
    from nokker.tenancy.context import tenant_scope
    from nokker.tools.select import compact_index

    w = _rig(monkeypatch, paper=True)
    try:
        cfg = type("C", (), {"family_calendar": True, "pg_dsn": lambda self: ""})()
        configure_family_calendar(cfg, w.kernel, w.tenancy)
        svc = famcal_http.service()
        svc.clock = lambda: NOW
        with tenant_scope(w.p.tenant_id):
            assert w.kernel.tools.spec("paper_calendar_add") is None
            for name in set(PAPER_TOOLS) - {"paper_calendar_add"}:
                assert w.kernel.tools.spec(name) is not None
            assert w.kernel.tools.spec("family_calendar_propose") is not None
        [paper] = [s for s in w.kernel.tools.tester_sources if isinstance(s, PaperTools)]
        names = [s.name for s in paper.specs()]
        assert "paper_calendar_add" not in names
        assert "- Papiers : paper_list, paper_read, paper_file, paper_bill_due, paper_payment_prepare, paper_share, " \
               "paper_tax_report" in compact_index(names)
        from nokker.core.ports import ToolContext

        r = paper.call("paper_calendar_add", {"events": []}, ToolContext(thread_id="t", turn_index=0, channel="api",
                                                                          approval_id="ap_x"))
        assert r.is_error and r.content == "outil inconnu : paper_calendar_add"
        # Une date isolée d'un courrier importé : par le calendrier familial.
        from nokker.household.paper.pdftext import make_pdf

        st, out = w.call("POST", "/v1/paper/documents", {"filename": "convocation.pdf", "content_base64":
                         base64.b64encode(make_pdf(["Convocation", "Rendez-vous le 20 novembre 2026 a 14h00"])).decode()},
                         who="P")
        assert st == 201
        with tenant_scope(w.p.tenant_id):
            d = svc.propose({"document_id": out["document"]["id"], "events": [
                {"title": "Rendez-vous", "date": "2026-11-20", "start": "14:00",
                 "source": "Rendez-vous le 20 novembre 2026 a 14h00"}]})
            assert [x.writable for x in svc.lines(d)] == [True] and d.origin == "« convocation.pdf »"
    finally:
        _close(w)
