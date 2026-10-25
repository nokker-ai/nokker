"""Export des données d'un compte (nokker/account/export.py) : limite d'une demande par 24 h, expiration du fichier,
contenu sans identifiant de locataire ni fil d'un autre locataire."""
from __future__ import annotations

import gzip
import json
from types import SimpleNamespace

import pytest

from nokker.account.export import EVERY_S, TTL_S, AccountExports, ExportLimit, MemoryExports

ACC = SimpleNamespace(account_id="us_a", email="a@example.org", lang="fr", created_at=0.0, status="active")


def _rows(after, limit):
    rows = [("2026-10-24T10:00:00+00:00", "tn_a:web1", 0, {"input": {"text": "bonjour"}, "reply": "salut",
                                                            "channel": "api", "messages": []}),
            ("2026-10-24T10:01:00+00:00", "tn_b:web1", 0, {"input": {"text": "pas à moi"}, "reply": "x"})]
    return [r for r in rows if r[:3] > after][:limit]


def test_one_export_per_day_and_the_file_expires():
    now = [1000.0]
    ex = AccountExports(store=MemoryExports(), turns_after=_rows, clock=lambda: now[0])
    meta = ex.request(ACC, "tn_a")
    with pytest.raises(ExportLimit):
        ex.request(ACC, "tn_a")
    doc = json.loads(gzip.decompress(ex.get(meta.id, "us_a")[1]))
    assert doc["conversations"] == [{"thread": "web1", "turns": [
        {"index": 0, "at": "2026-10-24T10:00:00+00:00", "channel": "api", "message": "bonjour", "reply": "salut",
         "tool_messages": []}]}]
    assert ex.get(meta.id, "us_b") is None
    now[0] += TTL_S
    assert meta.public(now[0])["status"] == "expired" and meta.public(now[0])["download"] is None
    now[0] = 1000.0 + EVERY_S
    assert ex.request(ACC, "tn_a").id != meta.id
