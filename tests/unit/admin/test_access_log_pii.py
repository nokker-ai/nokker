"""S2 (backlog bêta) : le journal d'accès de la console admin ne porte pas la query string."""
from __future__ import annotations

import logging
from urllib import error as urllib_error
from urllib import request as urllib_request

from nokker.admin.server import serve_admin
from nokker.admin.store import MemoryAdminStore
from tests.unit.admin.test_admin import verifier


def test_query_absente_du_journal_admin(caplog):
    httpd = serve_admin(MemoryAdminStore(), verifier(), "127.0.0.1", 0)
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/api/me?since=tn_abc123&x=th_def"
        with caplog.at_level(logging.INFO):
            try:
                urllib_request.urlopen(url, timeout=5)
            except urllib_error.HTTPError:
                pass  # 401/403 sans jeton : la ligne de requête est journalisée quand même
    finally:
        httpd.shutdown()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "/api/me?…" in text
    assert "tn_abc123" not in text and "th_def" not in text and "since" not in text
