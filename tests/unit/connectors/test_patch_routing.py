"""PATCH /v1/connectors/{id} (renommer, défaut, niveau d'un partage) atteint bien le service des connecteurs par le
VRAI serveur (do_PATCH). Rév. 37 : la route n'était branchée que pour GET/POST/DELETE → 404 générique en prod
(relevé QA du 05.11). Les tests du service appelaient `handle` directement et ne voyaient pas le trou."""
from __future__ import annotations

from tests.unit.household.test_paper_wiring import _rig


def test_patch_connector_reaches_the_connectors_service(monkeypatch):
    from nokker.connectors import http as connectors_http

    seen = []

    def fake_handle(handler, method, path, account):
        if not path.startswith("/v1/connectors"):
            return False
        seen.append((method, path))
        handler._json(200, {"ok": True})
        return True

    monkeypatch.setattr(connectors_http, "handle", fake_handle)
    w = _rig(monkeypatch, paper=False)
    try:
        st, out = w.call("PATCH", "/v1/connectors/mb_test0000ab", {"label": "pro"}, who="P")
        assert (st, out) == (200, {"ok": True})
        assert ("PATCH", "/v1/connectors/mb_test0000ab") in seen
        st, _ = w.call("PATCH", "/v1/connectors/mb_test0000ab/grants/acc_x", {"access": "read"}, who="P")
        assert st == 200 and ("PATCH", "/v1/connectors/mb_test0000ab/grants/acc_x") in seen
    finally:
        w.httpd.shutdown()
