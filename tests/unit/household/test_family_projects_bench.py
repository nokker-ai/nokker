"""Banc des projets du foyer (`bench/family_projects/live.py`) : les scènes se montent sans modèle (aucun appel réseau),
le projet de départ a bien une tâche en retard et une tâche de l'enfant."""
from __future__ import annotations

from bench.family_projects.live import ACC, HID, SCENES, TC, TP, build
from nokker.tenancy.context import tenant_scope


def test_scenes_mount_without_a_model():
    assert [s[0] for s in SCENES] == ["creation", "edition", "relance", "retard", "enfant", "externe"]
    svc, ids = build()
    assert set(ids) == {"project", "Couvrir les cahiers", "Acheter le sac"}
    with tenant_scope(TP):
        states = {t["title"]: t["state"] for t in svc.member_tasks()["tasks"]}
        assert svc.store.task(HID, ids["Acheter le sac"]).assignee == ACC["tn_marc0000000"]
    assert states == {"Acheter le sac": "overdue", "Couvrir les cahiers": "todo"}
    with tenant_scope(TC):
        assert [t["title"] for t in svc.member_tasks()["tasks"]] == ["Couvrir les cahiers"]
