"""Contrôle déterministe des dates et heures du relecteur automatique (`nokker/approvals/dates.py`)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from nokker.approvals.auto_review import AutoReviewer
from nokker.approvals.dates import card_values, request_values, unmatched
from nokker.core.ports import Approval

FRI = datetime(2026, 10, 16, 10, 0, tzinfo=ZoneInfo("Europe/Zurich"))  # vendredi


@pytest.mark.parametrize("request_text, args, ok", [
    ("Le 24 à 15h, rendez-vous chez l'assureur. Mets au planning.", {"start": "2026-10-24T15:00:00"}, True),
    ("Le 24 à 15h, rendez-vous chez l'assureur.", {"start": "2026-10-23T15:00:00"}, False),   # mauvais jour
    ("Le 24 à 15h, rendez-vous chez l'assureur.", {"start": "2026-10-24T13:00:00"}, False),   # mauvaise heure
    ("Rappelle-moi demain à 9h30 d'appeler alice", {"start": "2026-10-17T09:30"}, True),
    ("Rappelle-moi après-demain à 9h", {"start": "2026-10-18T09:00"}, True),
    ("Rappelle-moi après-demain à 9h", {"start": "2026-10-17T09:00"}, False),
    ("Réunion jeudi à 14h", {"start": "2026-10-22T14:00:00"}, True),
    ("Tâche pour vendredi", {"due": "2026-10-16"}, False),          # vendredi un vendredi : ambigu
    ("Tâche pour vendredi", {"due": "2026-10-23"}, False),
    ("Tâche pour vendredi prochain", {"due": "2026-10-23"}, True),
    ("Payer la TVA le 30 novembre à 9h", {"start": "2026-11-30T09:00:00"}, True),
    ("Payer la TVA le 30.11.2026 à 09:00", {"start": "2026-11-30T09:00:00"}, True),
    ("Payer la TVA le 30/11", {"date": "30.11.2026"}, True),
    ("Corrige la faute dans src/app.py", {"path": "src/app.py", "new_string": "return"}, True),  # rien à vérifier
    ("lance les tests", {"command": "pytest -q"}, True),
    ("Ajoute l'événement", {"start": "2026-10-20T10:00"}, False),   # date inventée
    ("Contrôle mardi prochain à 15h", {"start": "2026-10-20T15:00:00+02:00", "end": "2026-10-20T16:00:00+02:00"}, True),
    ("Contrôle mardi prochain à 15h", {"start": "2026-10-20T15:00", "end": "2026-10-21T16:00"}, False),  # fin un autre jour
    ("Échéance le 23", {"due_date": "2026-10-23T18:00:00Z"}, False),  # heure inventée (le décalage n'en est pas une)
    ("Échéance le 23 à 18h", {"due_date": "2026-10-23T18:00:00Z"}, True),
])
def test_dates_must_come_from_the_request(request_text, args, ok):
    assert (unmatched(request_text, args, FRI) == []) is ok


def test_values_normalized():
    assert card_values({"a": "2026-10-24T15:00:00", "b": ["24.10.2026"], "c": "15h30"}) == (
        {"2026-10-24"}, {"15:00", "15:30"})
    d, t = request_values("aujourd'hui à 8h puis demain 17:15", FRI)
    assert d == {"2026-10-16", "2026-10-17"} and t == {"08:00", "17:15"}


class Transport:
    def __init__(self):
        self.bodies = []

    def post(self, body):
        self.bodies.append(body)
        return {"choices": [{"message": {"content": "DÉCISION: APPROUVER\nRAISON: demandé."}}]}


def test_mismatch_escalates_without_model_call():
    rev = AutoReviewer(transport=Transport(), now=lambda: FRI)
    card = Approval(id="a", thread_id="t", turn_index=0, channel="api", tool_name="calendar_create",
                    arguments={"start": "2026-10-23T15:00:00"}, args_sha256="", strength="standard",
                    rule="permissions:default")
    res = rev.review_text("Le 24 à 15h, rendez-vous.", card, [])
    assert res.decision == "escalate" and res.by_rule and "2026-10-23" in res.reason
    assert rev.transport.bodies == []
    card.arguments = {"start": "2026-10-24T15:00:00"}
    assert rev.review_text("Le 24 à 15h, rendez-vous.", card, []).decision == "approve"


def test_addresses_must_be_named_or_the_accounts_own():
    from nokker.approvals.dates import unmatched_addresses

    assert unmatched_addresses("ajoute au planning", {"user": "bob@example.org"}, ("alice@example.org",)) == [
        "bob@example.org"]
    assert unmatched_addresses("ajoute au planning", {"user": "alice@example.org"}, ("alice@example.org",)) == []
    assert unmatched_addresses("écris à bob@example.org", {"to": ["bob@example.org"]}) == []
