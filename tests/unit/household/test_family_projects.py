"""Projets du foyer (docs/FAMILY-PROJECTS.md) : brouillon et révision, carte sur une révision dépassée, ACL des
profils mineurs, aucun envoi hors du foyer sans carte, coche locale, retard et replanification, réglage des noms en
retard, départ d'un membre. Foyer SYNTHÉTIQUE : Nina (payeuse), Marc (adulte), Léa (enfant), Oscar (hors foyer)."""
from __future__ import annotations

import json
from types import SimpleNamespace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Decision, Effect, ToolCall, ToolResult
from nokker.household.famcal.service import FamilyCalendar
from nokker.household.famcal.store import MemoryFamcalStore
from nokker.household.projects.gates import RULE, FamilyProjectGates
from nokker.household.projects.review import Person
from nokker.household.projects.service import FamilyProjects, Home, ProjectError
from nokker.household.projects.store import MemoryProjectStore
from nokker.household.projects.tools import (
    CALENDAR_TOOL,
    CARD_TOOL,
    MINOR_TOOLS,
    PROJECT_TOOLS,
    SOURCE,
    FamilyProjectTools,
    minor_tools,
)
from nokker.tenancy.context import tenant_scope
from tests.unit.household.helpers import _Inner

ZH = ZoneInfo("Europe/Zurich")
NOW = datetime(2026, 11, 7, 10, 0, tzinfo=ZH).timestamp()
DAY = 86400.0
TP, TA, TC, TO = "tn_payer000000", "tn_marc0000000", "tn_lea00000000", "tn_oscar000000"
ACC = {TP: "us_nina0000000", TA: "us_marc0000000", TC: "us_lea00000000", TO: "us_oscar000000"}
PEOPLE = (Person(ACC[TP], "Nina"), Person(ACC[TA], "Marc"), Person(ACC[TC], "Léa", "child"))
HID = "tp_household00"


class Rig:
    def __init__(self, calendar: Any = None) -> None:
        self.now = NOW
        self.ids = 0
        self.store = MemoryProjectStore()

        def home_of(acc: str) -> Home | None:
            me = next((p for p in PEOPLE if p.account_id == acc), None)
            return Home(HID, TP, me, PEOPLE) if me else None

        def new_id(prefix: str) -> str:
            self.ids += 1
            return f"{prefix}{self.ids:016d}"

        self.svc = FamilyProjects(store=self.store, account_of=lambda tid: ACC.get(tid), home_of=home_of,
                                  calendar=calendar, clock=lambda: self.now, new_id=new_id)
        self.tools = FamilyProjectTools(service=self.svc)
        self.gates = FamilyProjectGates(inner=_Inner(), service=self.svc)

    def as_(self, tid: str):
        return tenant_scope(tid)

    def call(self, tid: str, name: str, args: dict[str, Any], approval: str | None = None) -> ToolResult:
        with tenant_scope(tid):
            return self.tools.call(name, args, ToolContext(thread_id="t", turn_index=0, channel="api",
                                                           approval_id=approval))

    def gate(self, tid: str, args: dict[str, Any]):
        spec = next(s for s in self.tools.specs() if s.name == CARD_TOOL)
        turn = SimpleNamespace(sender=ACC[tid])
        with tenant_scope(tid):
            return self.gates.action(ToolCall(id="c1", name=CARD_TOOL, arguments=args), spec, turn)


BIRTHDAY = {
    "title": "Anniversaire de Léa", "goal": "Fête des 9 ans à la maison", "deadline": "2026-11-21",
    "deadline_source": "samedi 21 novembre", "kind": "anniversaire", "members": ["Marc", "Léa"],
    "budget": {"amount": 250}, "lines": [
        {"kind": "milestone", "title": "Invitations envoyées", "due": "2026-11-12"},
        {"title": "Écrire les cartes d'invitation", "assignee": "Léa", "due": "2026-11-11",
         "due_source": "mercredi 11 novembre", "milestone": "Invitations envoyées"},
        {"title": "Commander le gâteau", "assignee": "Marc", "due": "2026-11-18"},
        {"title": "Réserver la salle", "assignee": "oscar@example.org", "due": "2026-11-10"},
        {"title": "Acheter les ballons", "assignee": "Tante Rose", "due": "2026-11-15"},
        {"title": "Ranger le salon", "assignee": "Nina", "due": "2026-11-02"},
        {"title": "Décorer", "assignee": "Nina", "due": "2026-11-25"},
        {"title": "Préparer le jeu", "assignee": "Nina", "due": "2026-12-05", "due_source": "05/12"},
    ]}


def _propose(r: Rig, plan: dict[str, Any] | None = None) -> dict[str, Any]:
    res = r.call(TP, "family_project_propose", plan or BIRTHDAY)
    assert not res.is_error, res.content
    head = json.loads(res.content.split("\n")[[i for i, x in enumerate(res.content.split("\n"))
                                                 if x.startswith("{")][0]])
    return head


def test_propose_reviews_every_line_and_writes_nothing():
    r = Rig()
    head = _propose(r)
    assert head["revision"] == 1 and head["to_write"] == 3 and head["to_check"] == 5
    d = r.store._drafts[TP][head["draft_id"]]
    lines = {x["title"]: x for x in d.lines}
    assert lines["Réserver la salle"]["problems"] == [
        "une adresse n'est pas un membre du foyer : rien ne part hors du foyer"]
    assert "n'est pas un membre actif du foyer" in lines["Acheter les ballons"]["problems"][0]
    assert lines["Ranger le salon"]["problems"] == ["date déjà passée"]
    assert lines["Décorer"]["problems"] == ["après l'échéance du projet (2026-11-21)"]
    assert "format J/M ou M/J ambigu" in lines["Préparer le jeu"]["problems"]
    assert lines["Commander le gâteau"]["notes"] == ["date calculée par l'assistant, à relire"]
    assert lines["Écrire les cartes d'invitation"]["assignee"] == ACC[TC]
    with r.as_(TP):
        assert r.store.projects(HID) == [] and r.store.tasks(HID) == []
    res = r.call(TP, "family_project_propose", BIRTHDAY)
    assert res.content.startswith("NON écrit : ligne 4, Réserver la salle — une adresse")
    assert "<untrusted-data kind=\"family-project\"" in res.content


def test_written_date_is_checked_against_the_users_words():
    r = Rig()
    plan = {**BIRTHDAY, "lines": [{"title": "Gâteau", "assignee": "Marc", "due": "2026-11-19",
                                   "due_source": "mercredi 18 novembre"}]}
    head = _propose(r, plan)
    d = r.store._drafts[TP][head["draft_id"]]
    assert d.lines[0]["problems"] == ["la date ne correspond à aucune date écrite dans la demande",
                                      "jour de la semaine incohérent avec la date"]
    assert head["next"].startswith("rien à écrire en l'état")


def test_edit_bumps_revision_and_a_card_on_a_stale_revision_writes_nothing():
    r = Rig()
    head = _propose(r)
    did = head["draft_id"]
    res = r.call(TP, "family_project_edit", {"draft_id": did, "action": "reject", "line": [4, 5, 6, 7, 8]})
    assert not res.is_error and '"revision": 2' in res.content
    # La carte de la révision 1 n'est plus posée (la porte laisse l'outil expliquer) ; approuvée quand même, rien.
    [v] = r.gate(TP, {"draft_id": did, "revision": 1})
    assert v.decision is Decision.ALLOW and v.rule == f"{RULE}:invalid" and "révision 2 (pas 1)" in v.reason
    res = r.call(TP, CARD_TOOL, {"draft_id": did, "revision": 1}, approval="ap_old")
    assert res.is_error and "révision 2 (pas 1)" in res.content
    with r.as_(TP):
        assert r.store.projects(HID) == []
    # Édition d'une ligne : l'assigné corrigé est relu contre le foyer.
    res = r.call(TP, "family_project_edit", {"draft_id": did, "action": "restore", "line": [5]})
    res = r.call(TP, "family_project_edit", {"draft_id": did, "action": "update", "line": [5],
                                             "fields": {"assignee": "Marc"}})
    assert '"revision": 4' in res.content and '"to_check": 0' in res.content
    with pytest.raises(ProjectError) as e, r.as_(TP):
        r.svc.edit(did, [5], "update", {})
    assert e.value.status == 400


def test_one_card_bound_to_the_revision_then_one_write():
    r = Rig()
    head = _propose(r)
    did = head["draft_id"]
    r.call(TP, "family_project_edit", {"draft_id": did, "action": "reject", "line": [6, 7, 8]})
    [v] = r.gate(TP, {"draft_id": did, "revision": 2})
    assert v.decision is Decision.HITL and v.rule == RULE and v.strength == "standard"
    assert v.reason.startswith("NON écrit : ligne 4, Réserver la salle")
    assert "nouveau projet du foyer « Anniversaire de Léa » (révision 2) : échéance 2026-11-21, tout le foyer" in v.reason
    assert "budget indicatif 250.00 CHF (aucun paiement)" in v.reason and "rien n'est envoyé hors du foyer" in v.reason
    assert "3. tâche : Commander le gâteau → Marc pour le 2026-11-18" in v.reason
    # Sans carte approuvée : rien.
    res = r.call(TP, CARD_TOOL, {"draft_id": did, "revision": 2})
    assert "rien n'est écrit sans carte approuvée" in res.content
    with r.as_(TP):
        assert r.store.projects(HID) == []
    res = r.call(TP, CARD_TOOL, {"draft_id": did, "revision": 2}, approval="ap_1")
    assert not res.is_error
    out = json.loads(res.content.split("\n")[-1])
    assert out["tasks"] == 2 and out["milestones"] == 1
    with r.as_(TP):
        [p] = r.store.projects(HID)
        assert p.members == [ACC[TP], ACC[TA], ACC[TC]] and p.budget == {"amount": "250.00", "currency": "CHF"}
        assert sorted(t.title for t in r.store.tasks(HID)) == ["Commander le gâteau", "Écrire les cartes d'invitation"]
    # Rejouer la même carte : déjà écrit, rien de plus.
    res = r.call(TP, CARD_TOOL, {"draft_id": did, "revision": 2}, approval="ap_1")
    assert res.is_error and "déjà écrit" in res.content
    with r.as_(TP):
        assert len(r.store.tasks(HID)) == 2


def _commit(r: Rig, plan: dict[str, Any], who: str = TP) -> str:
    res = r.call(who, "family_project_propose", plan)
    head = json.loads(next(x for x in res.content.split("\n") if x.startswith("{")))
    res = r.call(who, CARD_TOOL, {"draft_id": head["draft_id"], "revision": head["revision"]}, approval="ap")
    assert not res.is_error, res.content
    return json.loads(res.content.split("\n")[-1])["project_id"]


SCHOOL = {"title": "Rentrée de janvier", "deadline": "2026-12-20", "members": ["Léa", "Marc"], "lines": [
    {"title": "Couvrir les cahiers", "assignee": "Léa", "due": "2026-11-14"},
    {"title": "Acheter le sac", "assignee": "Marc", "due": "2026-11-09"}]}
TAXES = {"title": "Impôts 2026", "deadline": "2026-12-15", "visibility": "parents", "members": ["Marc"],
         "budget": {"amount": "120"}, "lines": [{"title": "Rassembler les relevés", "assignee": "Marc",
                                                 "due": "2026-11-30"}]}


def test_child_sees_and_checks_only_her_tasks_never_a_parents_project():
    r = Rig()
    school = _commit(r, SCHOOL)
    taxes = _commit(r, TAXES)
    with r.as_(TC):
        view = r.svc.projects()
        assert [p["id"] for p in view["projects"]] == [school]
        assert [t["title"] for t in view["projects"][0]["tasks"]] == ["Couvrir les cahiers"]
        assert view["projects"][0]["progress"] == {"done": 0, "total": 1, "overdue": 0}
        with pytest.raises(ProjectError) as e:
            r.svc.project(taxes)
        assert e.value.status == 404
        with pytest.raises(ProjectError) as e:
            r.svc.member_tasks("Marc")
        assert e.value.status == 403
        assert [t["title"] for t in r.svc.member_tasks()["tasks"]] == ["Couvrir les cahiers"]
        marc_task = None
    with r.as_(TP):
        marc_task = next(t.task_id for t in r.store.tasks(HID) if t.title == "Acheter le sac")
        tax_task = next(t.task_id for t in r.store.tasks(HID) if t.project_id == taxes)
        lea_task = next(t.task_id for t in r.store.tasks(HID) if t.assignee == ACC[TC])
    for tid in (marc_task, tax_task):
        res = r.call(TC, "family_project_task_done", {"task_id": tid})
        assert res.is_error and res.content == "tâche introuvable"
    assert '"state": "done"' in r.call(TC, "family_project_task_done", {"task_id": lea_task}).content
    for fn in (lambda: r.svc.propose(SCHOOL), lambda: r.svc.nudge(lea_task), lambda: r.svc.replan(school),
               lambda: r.svc.put_settings({"overdue_by_member": True}), lambda: r.svc.drafts()):
        with pytest.raises(ProjectError) as e, r.as_(TC):
            fn()
        assert e.value.status == 403
    # Outsider : aucun foyer.
    with pytest.raises(ProjectError) as e, r.as_(TO):
        r.svc.projects()
    assert e.value.status == 404


def test_a_minor_is_never_put_in_a_parents_project():
    r = Rig()
    plan = {**TAXES, "members": ["Léa"], "lines": [{"title": "Trier", "assignee": "Léa", "due": "2026-11-30"}]}
    head = _propose(r, plan)
    assert head["header_problems"] == ["Léa est un profil mineur : pas dans un projet réservé aux parents"]
    d = r.store._drafts[TP][head["draft_id"]]
    assert "profil mineur : pas de tâche dans un projet réservé aux parents" in d.lines[0]["problems"]
    [v] = r.gate(TP, {"draft_id": head["draft_id"], "revision": 1})
    assert v.decision is Decision.ALLOW and v.rule == f"{RULE}:nothing"


def test_minor_source_has_only_her_two_tools():
    r = Rig()
    src = minor_tools(r.svc, None)
    assert [s.name for s in src.specs()] == list(MINOR_TOOLS)
    with tenant_scope(TC):
        res = src.call("family_project_propose", BIRTHDAY, ToolContext(thread_id="t", turn_index=0, channel="api"))
    assert res.is_error and res.content == "outil inconnu : family_project_propose"
    names = [s.name for s in r.tools.specs()]
    assert CALENDAR_TOOL not in names and set(names) == set(PROJECT_TOOLS) - {CALENDAR_TOOL}


def test_nothing_leaves_the_household_without_a_card():
    """Aucun outil n'a d'effet externe ; la relance ne vise que l'assigné, dans Nokker ; l'agenda passe par la carte
    du calendrier familial (brouillon seulement ici)."""
    written: list[Any] = []

    class Agenda:
        def specs(self):
            return [ToolSpec(name="calendar_events", description="", parameters={}, effect=Effect.READ),
                    ToolSpec(name="calendar_create", description="", parameters={})]

        def call(self, name, args, ctx):
            if name == "calendar_create":
                written.append(args)
            return ToolResult(call_id="", name=name, content=json.dumps({"events": []}))

    cal = FamilyCalendar(store=MemoryFamcalStore(), account_of=lambda tid: ACC.get(tid),
                         calendars_for=lambda tid: [Agenda()], clock=lambda: NOW)
    r = Rig(calendar=cal)
    assert {s.effect for s in r.tools.specs()} <= {Effect.READ, Effect.WRITE}
    assert all(s.source == SOURCE for s in r.tools.specs())
    pid = _commit(r, SCHOOL)
    with r.as_(TP):
        lea = next(t for t in r.store.tasks(HID) if t.assignee == ACC[TC])
    res = r.call(TP, "family_project_nudge", {"task_id": lea.task_id, "message": "N'oublie pas tes cahiers",
                                              "to": "oscar@example.org"})
    assert not res.is_error and '"to": "Léa"' in res.content and "dans Nokker" in res.content
    with r.as_(TP):
        t = r.store.task(HID, lea.task_id)
    assert t.nudge_text == "N'oublie pas tes cahiers" and t.nudged_by == ACC[TP]
    res = r.call(TP, "family_project_nudge", {"task_id": lea.task_id})
    assert res.is_error and "moins d'un jour" in res.content
    with r.as_(TC):
        [mine] = r.svc.member_tasks()["tasks"]
    assert mine["nudge"]["text"] == "N'oublie pas tes cahiers" and mine["nudge"]["by"] == "Nina"
    # Agenda : un brouillon du calendrier familial, rien d'écrit avant SA carte.
    res = r.call(TP, CALENDAR_TOOL, {"project_id": pid})
    assert not res.is_error and "family_calendar_commit" in res.content
    assert written == []
    [d] = list(cal.store._drafts[TP].values())
    assert d.origin == "projet du foyer « Rentrée de janvier »" and len(d.lines) == 2
    assert [x.writable for x in cal.lines(d)] == [True, True]


def test_checking_a_task_is_a_local_write():
    r = Rig()
    _commit(r, SCHOOL)
    with r.as_(TP):
        marc = next(t for t in r.store.tasks(HID) if t.assignee == ACC[TA])
    res = r.call(TA, "family_project_task_done", {"task_id": marc.task_id})
    assert '"state": "done"' in res.content and "écrit dans le foyer seulement" in res.content
    with r.as_(TP):
        t = r.store.task(HID, marc.task_id)
    assert t.status == "done" and t.done_by == ACC[TA] and t.done_at == NOW
    r.call(TA, "family_project_task_done", {"task_id": marc.task_id, "done": False})
    with r.as_(TP):
        assert r.store.task(HID, marc.task_id).status == "todo"
    # Le brouillon et la tâche ne vivent pas dans le locataire de Marc (foyer seulement).
    with r.as_(TA):
        assert r.store.tasks(HID) == []


def test_overdue_is_detected_hidden_in_common_views_and_replanned_through_a_new_card():
    r = Rig()
    pid = _commit(r, SCHOOL)
    r.now = NOW + 3 * DAY  # 10.11 : « Acheter le sac » (09.11) est en retard
    with r.as_(TP):
        sac = next(t for t in r.store.tasks(HID) if t.assignee == ACC[TA])
        cahiers = next(t for t in r.store.tasks(HID) if t.assignee == ACC[TC])
        week = r.svc.weekly()
    [late] = week["overdue"]
    assert late["state"] == "overdue" and late["assignee"] is None and not week["overdue_names_shown"]
    assert [t["title"] for t in week["due_this_week"]] == ["Couvrir les cahiers"]
    assert week["projects"][0]["progress"] == {"done": 0, "total": 2, "overdue": 1}
    with r.as_(TP):
        common = r.svc.projects()["projects"][0]["tasks"]
        assert next(t for t in common if t["id"] == sac.task_id)["assignee"] is None
        assert next(t for t in common if t["id"] == cahiers.task_id)["assignee"]["name"] == "Léa"
    with r.as_(TA):  # l'assigné voit son propre nom
        assert next(t for t in r.svc.projects()["projects"][0]["tasks"]
                    if t["id"] == sac.task_id)["assignee"]["name"] == "Marc"
    with r.as_(TP):
        r.svc.put_settings({"overdue_by_member": True})
        assert r.svc.weekly()["overdue"][0]["assignee"]["name"] == "Marc"
        r.svc.put_settings({"overdue_by_member": False})
    res = r.call(TP, "family_project_list", {})
    assert "family_project_replan" in res.content
    # Replanification : un nouveau brouillon, rien de modifié avant la carte.
    res = r.call(TP, "family_project_replan", {"project_id": pid})
    head = json.loads(next(x for x in res.content.split("\n") if x.startswith("{")))
    assert head["kind"] == "replan" and head["to_write"] == 1 and head["unchanged"] == 1
    with r.as_(TP):
        assert r.store.task(HID, sac.task_id).due == "2026-11-09"
    [v] = r.gate(TP, {"draft_id": head["draft_id"], "revision": 1})
    assert v.decision is Decision.HITL and "replanification du projet « Rentrée de janvier »" in v.reason
    assert "tâche : Acheter le sac → Marc pour le 2026-11-13 (avant : 2026-11-09)" in v.reason
    # Une tâche en retard laissée à sa date est signalée, jamais réécrite.
    r.call(TP, "family_project_edit", {"draft_id": head["draft_id"], "action": "update", "line": [1],
                                       "fields": {"due": "2026-11-09"}})
    d = r.store._drafts[TP][head["draft_id"]]
    assert d.lines[0]["problems"] == ["en retard (échéance 2026-11-09 passée) : nouvelle date à poser"]
    r.call(TP, "family_project_edit", {"draft_id": head["draft_id"], "action": "update", "line": [1],
                                       "fields": {"due": "2026-11-13"}})
    # Marc coche entre la carte et l'approbation : la replanification ne la rouvre pas.
    r.call(TA, "family_project_task_done", {"task_id": sac.task_id})
    res = r.call(TP, CARD_TOOL, {"draft_id": head["draft_id"], "revision": 3}, approval="ap_r")
    out = json.loads(res.content.split("\n")[-1])
    assert out["skipped_done_meanwhile"] == 1 and out["updated"] == 0
    with r.as_(TP):
        t = r.store.task(HID, sac.task_id)
    assert t.status == "done" and t.due == "2026-11-09"


def test_replan_moves_the_late_task_once_approved():
    r = Rig()
    pid = _commit(r, SCHOOL)
    r.now = NOW + 3 * DAY
    res = r.call(TP, "family_project_replan", {"project_id": pid, "shift_days": 5})
    head = json.loads(next(x for x in res.content.split("\n") if x.startswith("{")))
    r.call(TP, CARD_TOOL, {"draft_id": head["draft_id"], "revision": 1}, approval="ap")
    with r.as_(TP):
        sac = next(t for t in r.store.tasks(HID) if t.assignee == ACC[TA])
    assert sac.due == "2026-11-15" and r.svc.state(sac, r.svc.today()) == "todo"


def test_member_gone_unassigns_her_tasks():
    r = Rig()
    _commit(r, SCHOOL)
    assert r.svc.member_gone(HID, TP, ACC[TC]) == 1
    with r.as_(TP):
        t = next(t for t in r.store.tasks(HID) if t.title == "Couvrir les cahiers")
        [p] = r.store.projects(HID)
    assert t.assignee == "" and ACC[TC] not in p.members


def test_texts_seen_by_the_model_are_frozen():
    """Gel de la relecture des textes (docs/FAMILY-PROJECTS.md §7) : toute retouche d'un texte vu par le modèle (schémas, consignes,
    `_LINE`, `_HEADER`, chaînes de `review.py`, motif de la carte) change l'empreinte et doit repasser par la relecture des textes."""
    from nokker.household.projects.tools import texts_sha256

    assert texts_sha256() == FROZEN


FROZEN = "a2a747113d375cce54c563c2c04c9c416030776c96da2f4cec1591d5afeecbff"
