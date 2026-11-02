"""Consolidation nocturne (docs/CONSOLIDATION.md) en faux modèle, magasin en mémoire (mêmes règles que les politiques
de 049/053 ; la base réelle est prouvée par tests/integration/test_consolidation_rls.py).

Prouve : opérations (fusion, correction avec sa source, archivage, résumé d'épisodes, leçons) appliquées directement
et journalisées ; défaire (par opération, par nuit) rend l'état EXACT d'avant et est idempotent ; plafond par nuit ;
consignes, notes épinglées, mots de l'utilisateur et épisodes de la couche 3 intacts ; aucune fuite entre locataires
ni entre membres d'un foyer (payeur, adulte, ado, ex-membre) — ni dans ce qui part au modèle, ni dans le rapport ;
aucun secret dans le rapport ni dans le message du matin ; un seul passage à la fois ; déclencheur à l'heure locale."""
from __future__ import annotations

import copy
import json
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from nokker.memory.consolidation import http as chttp
from nokker.memory.consolidation import model as M
from nokker.memory.consolidation.engine import ConsolidationError, Consolidator, Settings
from nokker.memory.consolidation.scheduler import ConsolidationScheduler
from nokker.memory.consolidation.signals import KernelSignals
from nokker.memory.consolidation.stores import MemoryConsolidationStore
from nokker.memory.tenant_long import (
    HashEmbedder,
    LongMemory,
    LongNote,
    Membership,
    MemoryLongStore,
)
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

TP, TA, TK, TX, TB = (f"tn_{c * 12}" for c in "pakxb")
P, A, K, X, B = "us_payeur", "us_adulte", "us_ado", "us_ancien", "us_autre"
TENANT = {P: TP, A: TA, K: TK, X: TX, B: TB}
HID = "pj_foyer000001"
DAY = 86_400.0
NOW = datetime(2026, 11, 2, 2, 30, tzinfo=UTC).timestamp()  # 03:30 à Zurich
AKIA = "AKIA" + "Z7Q3VNXW" + "5LPR2K4M"


class FakeModel:
    """Faux modèle : rend ce que le test a prévu pour chaque type d'appel ; garde chaque message reçu."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.notes: Any = lambda listing: {}
        self.episodes: Any = lambda listing: {}
        self.lessons: Any = lambda listing: {}

    def __call__(self, messages: list[dict[str, Any]]) -> str:
        system, user = messages[0]["content"], messages[1]["content"]
        self.calls.append({"system": system, "user": user})
        if system == M.NOTES_SYSTEM:
            return json.dumps(self.notes(_index(user)), ensure_ascii=False)
        if system == M.EPISODES_SYSTEM:
            return json.dumps(self.episodes(user), ensure_ascii=False)
        if system == M.LESSONS_SYSTEM:
            return json.dumps(self.lessons(user), ensure_ascii=False)
        raise AssertionError("appel modèle inattendu")

    def seen(self) -> str:
        return "\n".join(c["user"] for c in self.calls)


def _index(user: str) -> dict[str, int]:
    """titre → numéro dans la liste des notes montrée au modèle."""
    out = {}
    for line in user.splitlines():
        m = re.match(r"^\[(\d+)\] \([^)]*\) (.+?) — ", line)
        if m:
            out[m.group(2)] = int(m.group(1))
    return out


@dataclass
class FakeSignals:
    episodes_by_thread: dict[str, list[Any]] = field(default_factory=dict)
    sits: list[dict[str, Any]] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)

    def old_episodes(self, account_id: str, cutoff: float):
        self.asked.append(account_id)
        for t, eps in self.episodes_by_thread.items():
            if t.startswith(TENANT[account_id] + ":"):
                yield t, [e for e in eps if e.closed_at < cutoff]

    def situations(self, account_id: str, since: float) -> list[dict[str, Any]]:
        return [s for s in self.sits if s["thread_id"].startswith(TENANT[account_id] + ":")]


class World:
    def __init__(self, settings: Settings | None = None) -> None:
        self.members: dict[str, Membership] = {
            P: Membership(HID, TP, P, "adult"), A: Membership(HID, TP, P, "adult"), K: Membership(HID, TP, P, "teen")}
        self.long_store = MemoryLongStore(self.members.get)
        self.long = LongMemory(store=self.long_store, membership=self.members.get, embedder=HashEmbedder())
        self.store = MemoryConsolidationStore(self.long_store)
        self.model = FakeModel()
        self.signals = FakeSignals()
        self.sent: list[tuple[str, str, str]] = []
        self.clock = [NOW]
        self.c = Consolidator(store=self.store, settings=settings or Settings(), complete=self.model,
                              model_name="faux-flash", signals=self.signals, embed=self.long._embed_doc,
                              notify=lambda acc, title, text: self.sent.append((acc, title, text)) or {"app": "sent"},
                              clock=lambda: self.clock[0])

    def put(self, acc: str, title: str, text: str, *, scope: str = "user", kind: str = "fact", origin: str = "app",
            age_days: float = 30, author: str | None = None, audience: str = "all") -> str:
        """Note écrite telle quelle (sans le dédoublonnage ni le caviardage de l'écriture, pour planter des cas)."""
        at = NOW - age_days * DAY
        owner = P if scope == "household" else acc
        name = f"{kind}-{re.sub('[^a-z0-9]+', '-', title.lower()).strip('-')}-{len(self.long_store.notes):04d}"
        vec = HashEmbedder().embed_docs([f"{title}\n{text}"])[0]
        self.long_store.notes[(TENANT[owner], owner, name)] = LongNote(
            name=name, scope=scope, kind=kind, title=title, text=text, account_id=owner, tenant_id=TENANT[owner],
            author_account=author or owner, household_id=HID if scope == "household" else None, audience=audience,
            origin=origin, created_at=at, updated_at=at, embed_model=HashEmbedder().identity(), embedding=vec)
        return name

    def run(self, acc: str, **kw: Any) -> dict[str, Any]:
        with tenant_scope(TENANT[acc]):
            return self.c.run(acc, **kw)

    def notes_of(self, acc: str) -> dict[str, LongNote]:
        return {n.name: n for (t, a, _), n in self.long_store.notes.items() if (t, a) == (TENANT[acc], acc)}

    def state(self) -> dict[Any, Any]:
        return {k: copy.deepcopy(vars(n)) for k, n in self.long_store.notes.items()}


@pytest.fixture
def w() -> World:
    return World()


def _ops(report: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [o for o in report["ops"] if o["kind"] == kind]


# ─────────────────────────────── opérations et journal ───────────────────────────────


def test_exact_duplicates_are_merged_without_the_model_and_undone_exactly(w):
    w.c.complete = None
    a = w.put(P, "Allergie", "Alice est allergique aux noix de cajou.", age_days=40)
    b = w.put(P, "Allergie (bis)", "Alice est allergique aux  noix de cajou.", origin="propose:tn_p:t1", age_days=3)
    before = w.state()
    rep = w.run(P)
    assert rep["run"]["status"] == "done" and rep["run"]["counts"]["merge"] == 1
    assert set(w.notes_of(P)) == {a}  # la note de l'utilisateur est gardée, le doublon archivé
    with tenant_scope(TP):
        undo = w.c.undo_run(P, rep["run"]["id"])
        assert undo["complete"] and len(undo["undone"]) == 1
        assert w.state() == before  # état EXACT d'avant, plongements et dates compris
        again = w.c.undo_run(P, rep["run"]["id"])  # idempotent
    assert again["undone"] == [] and again["already"] == undo["undone"] and w.state() == before
    _ = b


def test_model_merge_correction_and_stale(w):
    keep = w.put(P, "Club", "Bob joue au tennis au club de Carouge.", origin="propose:t:1", age_days=60)
    drop = w.put(P, "Tennis", "Bob joue au tennis le mardi au club de Carouge.", origin="propose:t:2", age_days=50)
    old = w.put(P, "Voiture", "La voiture est une Fiat grise.", origin="propose:t:3", age_days=90)
    new = w.put(P, "Voiture neuve", "Depuis octobre, la voiture est une Skoda bleue.", age_days=5)
    gone = w.put(P, "Rendez-vous", "Rendez-vous chez le dentiste le 3 mars 2026.", age_days=200)

    def plan(ix):
        return {"merges": [{"keep": ix["Club"], "drop": [ix["Tennis"]], "title": "Tennis",
                            "text": "Bob joue au tennis le mardi au club de Carouge.", "reason": "même fait"}],
                "corrections": [{"target": ix["Voiture"], "by": ix["Voiture neuve"],
                                 "text": "Voiture — La voiture est une Skoda bleue.", "reason": "voiture changée"}],
                "stale": [{"target": ix["Rendez-vous"], "reason": "rendez-vous passé"}]}

    w.model.notes = plan
    rep = w.run(P)
    notes = w.notes_of(P)
    assert notes[keep].text == "Bob joue au tennis le mardi au club de Carouge." and drop not in notes
    assert notes[old].text == "La voiture est une Skoda bleue."  # apprise par l'assistant : réécrite
    assert gone not in notes and new in notes
    corr = _ops(rep, "correct")[0]
    assert corr["evidence"]["by"] == new and "Skoda" in corr["evidence"]["by_text"]  # la source est gardée
    assert any(n["action"] == "annotate" and "Voiture neuve" in n["text"] for n in corr["notes"])
    assert rep["run"]["model"] == "faux-flash"
    with tenant_scope(TP):
        arch = w.store.tables["archive"][(TP, P)]
    assert {k[1] for k in arch} == {drop, gone}  # rien n'est supprimé pour de bon


def test_user_written_note_is_never_rewritten(w):
    mine = w.put(P, "Voiture", "La voiture est une Fiat grise.", origin="app", age_days=90)
    newer = w.put(P, "Voiture neuve", "La voiture est une Skoda bleue.", origin="propose:t:1", age_days=2)
    other = w.put(P, "Vélo", "Le vélo est rouge.", origin="app", age_days=40)
    agent = w.put(P, "Vélo (bis)", "Le vélo de Bob est rouge.", origin="propose:t:2", age_days=10)
    w.model.notes = lambda ix: {
        "corrections": [{"target": ix["Voiture"], "by": ix["Voiture neuve"], "text": "La voiture est une Skoda."}],
        "merges": [{"keep": ix["Vélo"], "drop": [ix["Vélo (bis)"]], "title": "Vélo", "text": "Le vélo de Bob est rouge."}]}
    rep = w.run(P)
    notes = w.notes_of(P)
    assert mine not in notes  # archivée (réversible), jamais réécrite
    assert notes[other].text == "Le vélo est rouge." and agent not in notes
    assert all(n["action"] != "update" for o in rep["ops"] for n in o["notes"] if n["name"] in (mine, other))
    assert newer in notes


def test_merge_always_keeps_the_note_written_by_the_person(w):
    mine = w.put(P, "Café", "Alice boit son café sans sucre.", origin="app", age_days=120)
    learned = w.put(P, "Café (bis)", "Alice prend son café sans sucre, le matin.", origin="propose:t:1", age_days=30)
    # le modèle garde la note apprise et jette celle de la personne : le noyau inverse, après sa réponse
    w.model.notes = lambda ix: {"merges": [{"keep": ix["Café (bis)"], "drop": [ix["Café"]], "title": "Café",
                                            "text": "Alice prend son café sans sucre, le matin."}]}
    before = copy.deepcopy(vars(w.notes_of(P)[mine]))
    rep = w.run(P)
    notes = w.notes_of(P)
    assert set(notes) == {mine} and vars(notes[mine]) == before  # gardée telle quelle, ses mots
    assert learned not in notes
    op = _ops(rep, "merge")[0]
    ann = [n for n in op["notes"] if n["action"] == "annotate"]
    assert ann and ann[0]["name"] == mine and "le matin" in ann[0]["text"]  # les faits des autres notes
    assert all(n["action"] != "update" for n in op["notes"])


def test_model_calls_are_counted_in_the_ledger(w):
    from nokker.core.turn import Usage

    rows = []

    class Ledger:
        def record(self, thread_id, turn_index, call):
            rows.append((thread_id, turn_index, call))

    class Comp:
        def __init__(self, text):
            self.message = type("Msg", (), {"content": text})()
            self.model, self.usage = "faux-flash", Usage(prompt=120, completion=30)

    w.c.ledger = Ledger()
    w.c.complete = lambda messages: Comp(w.model(messages))
    w.put(P, "A", "Un fait.", origin="propose:t:1")
    w.put(P, "B", "Un autre fait.", origin="propose:t:2")
    w.run(P)
    assert len(rows) == len(w.model.calls) == 1
    thread_id, _, call = rows[0]
    assert thread_id == f"{TP}:consolidation-{P}" and call.route_reason == "consolidation nocturne"
    assert call.usage.prompt == 120 and call.model == "faux-flash"


def test_invented_text_is_rejected(w):
    a = w.put(P, "Club", "Bob joue au tennis.", origin="propose:t:1")
    b = w.put(P, "Tennis", "Bob aime le tennis.", origin="propose:t:2")
    w.model.notes = lambda ix: {"merges": [{"keep": ix["Club"], "drop": [ix["Tennis"]], "title": "Tennis",
                                            "text": "Bob joue au tennis à Lausanne depuis 2019."}]}
    w.run(P)
    assert w.notes_of(P)[a].text == "Bob joue au tennis."  # fusion faite, texte inventé refusé
    assert b not in w.notes_of(P)


# ─────────────────────────────── garde-fous ───────────────────────────────


def test_rules_and_pinned_notes_are_untouched(w):
    rule = w.put(P, "Consigne", "Toujours répondre en français.", kind="rule", age_days=300)
    rule2 = w.put(P, "Consigne bis", "Toujours répondre en français.", kind="rule", age_days=10)
    pinned = w.put(P, "Épinglée", "Le code du portail change chaque mois.", age_days=400)
    dup = w.put(P, "Épinglée bis", "Le code du portail change chaque mois.", origin="propose:t:1", age_days=3)
    with tenant_scope(TP):
        w.c.pin(P, pinned, True)
    before = {n: copy.deepcopy(vars(w.notes_of(P)[n])) for n in (rule, rule2, pinned)}
    w.put(P, "Libre", "Une note ordinaire.", origin="propose:t:2", age_days=30)
    w.model.notes = lambda ix: {"stale": [{"target": ix["Consigne"]}, {"target": ix.get("Épinglée", 1)}],
                                "corrections": [{"target": ix["Consigne"], "by": ix["Consigne bis"], "text": "x"}],
                                "merges": [{"keep": ix["Consigne bis"], "drop": [ix["Consigne"]]}]}
    rep = w.run(P)
    for n, v in before.items():
        assert vars(w.notes_of(P)[n]) == v  # ni archivée, ni réécrite, ni annotée
    assert dup not in w.notes_of(P)  # le doublon d'une note épinglée part, l'épinglée reste telle quelle
    touched = {n["name"] for o in rep["ops"] for n in o["notes"]}
    assert touched == {dup}
    assert "protégée" in w.model.calls[0]["user"]


def test_cap_per_night(w):
    w.c.settings.max_ops = 2
    w.c.complete = None
    for i in range(5):
        w.put(P, f"Note {i}", f"Fait numéro {i} répété.", age_days=50)
        w.put(P, f"Note {i} bis", f"Fait numéro {i} répété.", origin="propose:t:1", age_days=5)
    rep = w.run(P)
    assert rep["run"]["counts"]["merge"] == 2 and rep["run"]["skipped"] == 3
    assert len(w.notes_of(P)) == 8


def test_undo_op_conflicts_when_the_user_changed_the_note(w):
    a = w.put(P, "Club", "Bob joue au tennis.", origin="propose:t:1")
    w.put(P, "Tennis", "Bob joue au tennis le mardi.", origin="propose:t:2")
    w.model.notes = lambda ix: {"merges": [{"keep": ix["Club"], "drop": [ix["Tennis"]], "title": "Tennis",
                                            "text": "Bob joue au tennis le mardi."}]}
    rep = w.run(P)
    op = rep["ops"][0]["id"]
    w.long_store.notes[(TP, P, a)].text = "Modifiée par la personne."
    w.long_store.notes[(TP, P, a)].updated_at = NOW + 10
    with tenant_scope(TP):
        with pytest.raises(ConsolidationError) as e:
            w.c.undo_op(P, op)
        assert e.value.code == "conflict"
        assert w.notes_of(P)[a].text == "Modifiée par la personne."  # rien d'écrit


def test_undo_op_then_run_is_exact_and_idempotent(w):
    w.put(P, "A", "Un fait.", age_days=40)
    w.put(P, "A bis", "Un fait.", origin="propose:t:1", age_days=4)
    w.put(P, "B", "Autre fait.", age_days=40)
    w.put(P, "B bis", "Autre fait.", origin="propose:t:1", age_days=4)
    w.c.complete = None
    before = w.state()
    rep = w.run(P)
    with tenant_scope(TP):
        first = rep["ops"][0]["id"]
        assert w.c.undo_op(P, first)["already"] is False
        assert w.c.undo_op(P, first)["already"] is True
        out = w.c.undo_run(P, rep["run"]["id"])
        assert out["already"] == [first] and out["complete"]
        assert w.c.report(P, rep["run"]["id"])["run"]["undone_at"] is not None
    assert w.state() == before


def test_one_consolidation_at_a_time_per_tenant(w):
    w.put(P, "A", "Un fait.")
    actor_lock = w.store._locks.setdefault(TP, threading.Lock())
    actor_lock.acquire()
    try:
        assert w.run(P)["status"] == "busy"
    finally:
        actor_lock.release()
    assert w.run(P)["run"]["status"] == "done"


def test_scheduled_night_runs_once(w):
    w.put(P, "A", "Un fait.")
    assert w.run(P, trigger="schedule", night="2026-11-02")["run"]["status"] == "done"
    assert w.run(P, trigger="schedule", night="2026-11-02")["status"] == "already"


def test_dry_run_writes_nothing(w):
    w.put(P, "A", "Un fait.", age_days=40)
    w.put(P, "A bis", "Un fait.", origin="propose:t:1")
    before = w.state()
    rep = w.run(P, dry_run=True)
    assert rep["run"]["dry_run"] and rep["run"]["counts"]["merge"] == 1
    assert w.state() == before
    with tenant_scope(TP):
        assert w.c.runs(P)["runs"] == []


# ─────────────────────────────── cloisonnement ───────────────────────────────


def _world_with_household() -> World:
    w = World()
    w.put(P, "Profil payeur", "Le payeur garde son agenda privé chez Alpha.", age_days=50)
    w.put(P, "Profil payeur bis", "Le payeur garde son agenda privé chez Alpha.", origin="propose:t:1")
    w.put(P, "Foyer", "Les poubelles sortent le lundi.", scope="household", author=A, origin="pending:mp_1")
    w.put(P, "Foyer bis", "Les poubelles sortent le lundi.", scope="household", origin="propose:t:2")
    w.put(P, "Adultes", "Le coffre de la cave ouvre avec la clé bleue.", scope="household", audience="adults")
    w.put(P, "Adultes bis", "Le coffre de la cave ouvre avec la clé bleue.", scope="household", audience="adults",
          origin="propose:t:3")
    w.put(A, "Profil adulte", "L'adulte prend le train de 7 h 12.", age_days=20)
    w.put(K, "Profil ado", "L'ado a piscine le jeudi.", age_days=20)
    w.put(X, "Profil ancien", "L'ancien membre aime le jazz.", age_days=20)
    w.put(B, "Profil autre", "Un autre client range ses factures dans Beta.", age_days=20)
    w.put(P, "Divers payeur", "Le payeur relit ses notes le dimanche.", age_days=15)
    w.put(P, "Autre payeur", "Le payeur préfère le thé.", age_days=12)
    for acc in (A, K, X, B):  # deux notes chacun : le modèle est appelé pour chaque compte
        w.put(acc, "Divers", "Une note sans importance.", age_days=15)
    w.model.notes = lambda ix: {}
    return w


SECRETS_OF = {P: ["agenda privé chez Alpha"], A: ["train de 7 h 12"], K: ["piscine le jeudi"],
              X: ["aime le jazz"], B: ["factures dans Beta"]}
HOUSEHOLD = ["poubelles sortent", "coffre de la cave"]


@pytest.mark.parametrize("acc", [P, A, K, X, B])
def test_no_leak_between_tenants_or_household_members(acc):
    w = _world_with_household()
    before = w.state()
    rep = w.run(acc)
    seen = w.model.seen() + json.dumps(rep, ensure_ascii=False)
    assert w.model.calls and all(t in seen for t in SECRETS_OF[acc])  # le compte voit les siennes
    for other, texts in SECRETS_OF.items():
        for t in texts:
            if other != acc:
                assert t not in seen, (acc, t)
    for t in HOUSEHOLD:
        if acc != P:
            assert t not in seen  # adulte, ado, ex-membre, autre locataire : jamais la mémoire du foyer
    # seules les notes du compte (et, pour le payeur, celles de son foyer) ont pu changer
    changed = {k for k in before if k not in w.long_store.notes or vars(w.long_store.notes[k]) != before[k]}
    assert all(k[:2] == (TENANT[acc], acc) for k in changed)
    if acc == P:
        assert len(changed) == 3  # un doublon de profil, deux du foyer (par audience)


def test_payer_partitions_are_never_mixed_in_one_model_call():
    w = _world_with_household()
    w.run(P)
    for call in w.model.calls:
        has_profile = "agenda privé" in call["user"]
        has_household = any(t in call["user"] for t in HOUSEHOLD)
        assert not (has_profile and has_household)
        assert not ("poubelles" in call["user"] and "coffre de la cave" in call["user"])  # tous / adultes seulement


def test_household_merge_never_crosses_audiences():
    w = World()
    w.put(P, "Coffre", "Le coffre ouvre avec la clé bleue.", scope="household", audience="adults")
    w.put(P, "Coffre bis", "Le coffre ouvre avec la clé bleue.", scope="household", origin="propose:t:1")
    w.c.complete = None
    rep = w.run(P)
    assert rep["run"]["counts"]["merge"] == 0


def test_internal_tenant_and_non_accounts_are_refused(w):
    with tenant_scope(INTERNAL_TENANT), pytest.raises(ConsolidationError):
        w.c.run(P)
    with tenant_scope(TP), pytest.raises(ConsolidationError):
        w.c.run("tg_123")


def test_routes_are_per_account():
    w = _world_with_household()
    chttp.configure(w.c)
    try:
        rep = w.run(P)
        rid, op = rep["run"]["id"], rep["ops"][0]["id"]
        with tenant_scope(TA):
            assert chttp.dispatch("GET", f"/v1/memory/consolidation/runs/{rid}", A, dict)[0] == 404
            assert chttp.dispatch("POST", f"/v1/memory/consolidation/ops/{op}/undo", A, dict)[0] == 404
            assert chttp.dispatch("GET", "/v1/memory/consolidation/runs", A, dict)[1] == {"runs": []}
        with tenant_scope(TP):
            st, body = chttp.dispatch("GET", f"/v1/memory/consolidation/runs/{rid}", P, dict)
            assert st == 200 and body["run"]["id"] == rid
            st, body = chttp.dispatch("POST", f"/v1/memory/consolidation/ops/{op}/undo", P, dict)
            assert st == 200 and body["undone"]
            st, body = chttp.dispatch("POST", f"/v1/memory/consolidation/runs/{rid}/undo", P, dict)
            assert st == 200 and body["complete"]
            st, body = chttp.dispatch("PUT", "/v1/memory/consolidation/settings", P,
                                      lambda: {"tz": "America/New_York", "cron": "0 4 * * *"})
            assert st == 200 and body["tz"] == "America/New_York"
            assert chttp.dispatch("PUT", "/v1/memory/consolidation/settings", P, lambda: {"tz": "Mars/Base"})[0] == 400
    finally:
        chttp.configure(None)
    assert chttp.dispatch("GET", "/v1/memory/consolidation/runs", P, dict) is None  # drapeau éteint : 404 du serveur


# ─────────────────────────────── secrets ───────────────────────────────


def test_secrets_never_reach_the_report_the_message_or_the_model(w):
    a = w.put(P, "Clé", f"Clé de déploiement {AKIA} pour le serveur.", origin="propose:t:1", age_days=40)
    w.put(P, "Clé bis", f"Clé de déploiement {AKIA} pour le serveur.", origin="propose:t:2")
    w.put(P, "Accès", f"La clé {AKIA} sert au déploiement.", origin="propose:t:3", age_days=60)
    w.put(P, "Accès neuf", "Le déploiement n'utilise plus de clé statique.", age_days=1)
    w.model.notes = lambda ix: {"corrections": [{"target": ix["Accès"], "by": ix["Accès neuf"],
                                                 "text": f"La clé {AKIA} ne sert plus.", "reason": f"remplacée {AKIA}"}]}
    rep = w.run(P)
    with tenant_scope(TP):
        full = w.c.report(P, rep["run"]["id"])
    blob = json.dumps([rep, full, w.sent], ensure_ascii=False)
    assert AKIA not in blob and AKIA not in w.model.seen()
    assert w.sent and w.sent[0][0] == P and "Mémoire consolidée" in w.sent[0][1]
    assert a in w.notes_of(P)


# ─────────────────────────────── épisodes et leçons ───────────────────────────────


@dataclass
class Ep:
    id: str
    title: str
    summary: str
    closed_at: float
    seq: int = 0
    day: str = "2026-09-01"


def test_old_episodes_are_summarized_once(w):
    t = f"{TP}:fil-1"
    w.signals.episodes_by_thread[t] = [Ep(f"ep_{i}", f"Épisode {i}", f"Choix du fournisseur Delta, étape {i}.",
                                          NOW - (40 - i) * DAY, i) for i in range(4)]
    w.model.episodes = lambda u: {"title": "Fournisseur", "text": "Le fournisseur retenu est Delta."}
    rep = w.run(P)
    s = _ops(rep, "summarize")
    assert len(s) == 1 and s[0]["evidence"]["thread_id"] == t and len(s[0]["evidence"]["episodes"]) == 4
    created = [n for n in w.notes_of(P).values() if n.origin == f"consolidation:episodes:{t}"]
    assert len(created) == 1 and created[0].text == "Le fournisseur retenu est Delta."
    w.clock[0] += DAY
    rep2 = w.run(P)
    assert not _ops(rep2, "summarize")  # déjà résumés
    with tenant_scope(TP):
        w.c.undo_op(P, s[0]["id"])
    assert not [n for n in w.notes_of(P).values() if n.origin.startswith("consolidation:episodes")]


def test_episode_signals_skip_pinned_and_layer3_episodes():
    @dataclass
    class E:
        id: str
        seq: int
        first_turn: int
        last_turn: int
        closed_at: float

    @dataclass
    class St:
        pinned_episodes: list[str]

    @dataclass
    class In:
        text: str

    @dataclass
    class T:
        index: int
        input: In

    class Eps:
        def list(self, thread, closed_only=True):
            return [E("ep_a", 0, 0, 1, 1.0), E("ep_b", 1, 2, 3, 2.0), E("ep_c", 2, 4, 5, 3.0), E("ep_d", 3, 6, 7, 4.0)]

        def get_state(self, thread):
            return St(["ep_c"])

    texts = ["bonjour", "parlons du budget", "Ah non, reviens au point-virgule.", "ok", "épinglé", "x",
             "réexporte total_ht pour ne rien casser", "y"]
    sig = KernelSignals(threads_of=lambda a: ["t1"], episodes=Eps(),
                        history_from=lambda t, i: [T(j, In(s)) for j, s in enumerate(texts) if j >= i],
                        recent_turns=lambda a, s: [])
    got = list(sig.old_episodes("us_x", 10.0))
    assert [e.id for e in got[0][1]] == ["ep_a"]  # revirement (b), épinglé (c), exigence (d) : jamais résumés


def test_lessons_from_corrections_and_refused_cards(w):
    t = f"{TP}:fil-2"
    w.signals.sits = [
        {"source": "correction", "key": f"c:{t}:7", "thread_id": t, "turn": 7, "before": "J'ai envoyé le devis.",
         "user": "Ah non, il ne fallait pas l'envoyer avant ma relecture.", "after": "Compris."},
        {"source": "refused", "key": "a:ap_1", "thread_id": t, "turn": 9, "tool": "mail_send",
         "reason": "envoi du devis au client"}]
    w.model.lessons = lambda u: {"lessons": [
        {"source": 1, "kind": "piege", "title": "Envoi sans relecture",
         "text": "Ne pas envoyer un devis avant la relecture de l'utilisateur."},
        {"source": 2, "kind": "strategie", "title": "Devis", "text": "Faire relire le devis avant tout envoi."},
        {"source": 2, "kind": "strategie", "title": "Doublon de source", "text": "Relire."}]}
    rep = w.run(P)
    les = _ops(rep, "lesson")
    assert len(les) == 2
    notes = [n for n in w.notes_of(P).values() if n.origin.startswith("consolidation:lesson:")]
    assert {n.title for n in notes} == {"Piège à éviter : Envoi sans relecture", "Stratégie : Devis"}
    assert {n.origin for n in notes} == {f"consolidation:lesson:{t}#T7", f"consolidation:lesson:{t}#T9"}
    assert all(n.kind == "feedback" for n in notes)
    w.clock[0] += DAY
    assert not _ops(w.run(P), "lesson")  # une situation ne sert qu'une fois


# ─────────────────────────────── planification ───────────────────────────────


def test_scheduler_runs_at_local_time_once_per_night(w):
    w.put(P, "A", "Un fait.", age_days=40)
    w.put(P, "A bis", "Un fait.", origin="propose:t:1")
    with tenant_scope(TP):
        w.c.put_settings(P, {"tz": "America/New_York", "cron": "30 3 * * *"})
    sched = ConsolidationScheduler(service=w.c, accounts=lambda: [(TP, P), (INTERNAL_TENANT, "us_x")],
                                   clock=lambda: w.clock[0])
    start = datetime(2026, 11, 2, 8, 0, tzinfo=UTC).timestamp()  # 03:00 à New York : pas encore
    w.clock[0] = start
    # la veille au soir déjà faite : on la marque pour ne tester que l'échéance du jour
    w.run(P, trigger="schedule", night="2026-11-01", tz="America/New_York")
    assert sched.tick(start) == []
    at = datetime(2026, 11, 2, 8, 31, tzinfo=UTC).timestamp()  # 03:31 à New York
    w.clock[0] = at
    out = sched.tick(at)
    assert len(out) == 1 and out[0]["run"]["night"] == "2026-11-02" and out[0]["run"]["trigger"] == "schedule"
    assert sched.tick(at + 120) == []  # une fois par nuit


def test_settings_disable_the_schedule(w):
    with tenant_scope(TP):
        w.c.put_settings(P, {"enabled": False})
    sched = ConsolidationScheduler(service=w.c, accounts=lambda: [(TP, P)], clock=lambda: NOW)
    assert sched.tick(NOW + 3600) == []


# ─────────────────────────────── drapeau éteint ───────────────────────────────


def test_flag_off_changes_nothing():
    """Éteint (défaut) : aucune route, aucun planificateur, et rien du chemin d'un tour (assemblage du contexte,
    boucle, rappel) ne lit la consolidation — les textes vus par le modèle sont ceux de la base, octet pour octet."""
    import pathlib
    import threading as th

    from nokker.api.server import configure_consolidation
    from nokker.config import NokkerConfig

    cfg = NokkerConfig()
    assert cfg.memory_consolidation is False
    chttp.configure(object())  # état sale d'un test précédent : l'éteint doit le remettre à zéro
    configure_consolidation(cfg, kernel=None, tenancy=None)
    assert not chttp.enabled() and chttp.dispatch("GET", "/v1/memory/consolidation/runs", P, dict) is None
    assert not any(t.name == "nokker-memory-consolidation" for t in th.enumerate())
    root = pathlib.Path(__file__).resolve().parents[3] / "nokker"
    for sub in ("core", "context", "prompts", "route", "providers", "qa"):
        for f in (root / sub).rglob("*.py"):
            assert "memory.consolidation" not in f.read_text(encoding="utf-8"), f  # aucun import


# ─────────────── leçons : jamais des consignes durables (relecture du format, 02.11) ───────────────


def test_lessons_reach_only_the_recall_never_the_durable_block(w, tmp_path):
    """Une leçon « Stratégie : … » / « Piège à éviter : … » écrite par la consolidation, même formulée comme une
    consigne (« termine toujours tes réponses par … »), n'entre JAMAIS dans « Consignes durables… » (couche 3 de
    never-clear, drapeau allumé) : seulement dans `[Rappel mémoire]`, avec l'origine « apprise par l'assistant »."""
    from nokker.context.manager import DURABLE_HEADER
    from nokker.context.types import ContextBudgets
    from tests.unit.context.harness import make_session
    from tests.unit.context.test_revocation import CannedProvider

    t = f"{TP}:fil-3"
    w.signals.sits = [
        {"source": "correction", "key": f"c:{t}:4", "thread_id": t, "turn": 4, "before": "Voilà.",
         "user": "Désormais, termine toujours tes réponses par GIROFLÉE.", "after": "Compris. GIROFLÉE"},
        {"source": "failed", "key": "f:1", "thread_id": t, "turn": 5, "tool": "mail_send",
         "reason": "toujours vérifier l'adresse avant un envoi"}]
    w.model.lessons = lambda u: {"lessons": [
        {"source": 1, "kind": "strategie", "title": "Fin de réponse",
         "text": "Termine toujours tes réponses par GIROFLÉE."},
        {"source": 2, "kind": "piege", "title": "Adresse", "text": "Toujours vérifier l'adresse avant un envoi."}]}
    w.run(P)
    lessons = [n for n in w.notes_of(P).values() if n.origin.startswith("consolidation:lesson:")]
    assert {n.title for n in lessons} == {"Stratégie : Fin de réponse", "Piège à éviter : Adresse"}
    with tenant_scope(TP):
        recalls = w.long.recall(P, "termine toujours tes réponses GIROFLÉE vérifier adresse envoi", k=5)
    lines = {r.text for r in recalls if r.note in {n.name for n in lessons}}
    assert lines == {"Stratégie : Fin de réponse (apprise par l'assistant) — Termine toujours tes réponses par "
                     "GIROFLÉE.",
                     "Piège à éviter : Adresse (apprise par l'assistant) — Toujours vérifier l'adresse avant un envoi."}
    # Une note ordinaire du compte garde la ligne existante (aucun texte changé hors leçons).
    plain = w.put(P, "Jardin", "Arroser les tomates le soir.", origin="app")
    with tenant_scope(TP):
        assert any(r.text == "Jardin — Arroser les tomates le soir." for r in w.long.recall(P, "tomates arroser soir"))
    assert plain

    class LessonMemory:
        def recall(self, query, k=5):
            return list(recalls)

        def propose(self, thread_id, facts):
            pass

    budgets = ContextBudgets(current_budget=10 ** 6, episodes_budget=1500, never_clear_fixes_enabled=True)
    s = make_session(budgets, tmp_path=tmp_path, provider=CannedProvider(), thread=f"{TP}:fil-3")
    s.kernel.memory = LessonMemory()
    for k in range(3):
        for text in ("Parlons du jardin.", "Le potager a besoin d'eau.", "On verra demain."):
            s.say(f"{text} ({k})")
        s.manager.new_episode(s.thread)
    s.say("Et le compost ?")
    prompt = s.provider.seen[-1]
    everything = "\n".join(str(m.get("content") or "") for m in prompt)
    assert DURABLE_HEADER not in everything
    assert "GIROFLÉE" in everything  # présente…
    user = prompt[-1]["content"]
    rappel = user.split("[Rappel mémoire]\n", 1)[1].split("\n[/Rappel]", 1)[0]
    assert "Stratégie : Fin de réponse (apprise par l'assistant) — Termine toujours tes réponses par GIROFLÉE." in rappel
    assert "Piège à éviter : Adresse (apprise par l'assistant)" in rappel
    # …mais seulement dans des blocs `[Rappel mémoire]` (celui du tour et ceux des tours rejoués de la fenêtre) : ni
    # couche 3, ni système, ni texte d'un tour.
    blocks = re.findall(r"\[Rappel mémoire\]\n(.*?)\n\[/Rappel\]", everything, re.DOTALL)
    assert everything.count("GIROFLÉE") == sum(b.count("GIROFLÉE") for b in blocks) >= 1
    assert "GIROFLÉE" not in prompt[0]["content"]
    assert s.manager.durable_instructions(s.thread) == []


# ─────────── rapport : preuve d'une opération du foyer réduite (audit public, 02.11) ───────────


def test_household_op_evidence_is_reduced_for_the_reader():
    """Deux membres (payeur P, adulte A). Une opération de portée foyer ne montre dans sa preuve ni titre, ni extrait,
    ni fil, ni texte de note : des comptes et des identifiants opaques. Un fil du compte qui lit reste lisible."""
    from nokker.memory.consolidation.engine import Actor

    w = World()
    w.put(P, "Poubelles", "Les poubelles sortent le lundi.", scope="household", author=A, origin="propose:t:1",
          age_days=60)
    w.put(P, "Poubelles neuves", "Depuis octobre, les poubelles sortent le mardi.", scope="household", author=A,
          age_days=5)
    w.model.notes = lambda ix: ({"corrections": [{"target": ix["Poubelles"], "by": ix["Poubelles neuves"],
                                                  "text": "Les poubelles sortent le mardi.", "reason": "jour changé"}]}
                                if "Poubelles" in ix else {})
    t = f"{TP}:fil-p"
    w.signals.sits = [{"source": "correction", "key": f"c:{t}:3", "thread_id": t, "turn": 3, "before": "Envoyé.",
                       "user": "Ne pas envoyer le devis avant ma relecture.", "after": "Compris."}]
    w.model.lessons = lambda u: {"lessons": [{"source": 1, "kind": "piege", "title": "Relecture",
                                              "text": "Ne pas envoyer le devis avant ma relecture."}]}
    rep = w.run(P)
    corr = _ops(rep, "correct")[0]
    assert corr["scope"] == "household"
    ev = corr["evidence"]
    assert ev["reduced"] is True and set(ev) <= {"counts", "ids", "reduced", "source", "lesson_kind"}
    assert all(i.startswith("ev_") for i in ev["ids"]) and len(ev["ids"]) == 2
    blob = json.dumps(ev, ensure_ascii=False)
    assert "mardi" not in blob and "Poubelles" not in blob and "poubelles" not in blob
    # la leçon vient d'un fil du payeur : il la lit en entier
    les = _ops(rep, "lesson")[0]
    assert les["evidence"]["thread_id"] == t and "relecture" in les["evidence"]["excerpt"]
    # A ne lit pas le rapport du payeur
    with tenant_scope(TA), pytest.raises(ConsolidationError):
        w.c.report(A, rep["run"]["id"])

    # Opération du foyer qui citerait un fil privé de A (titre, extrait) : rien de ce fil pour le payeur ; A, lui, lit
    # son propre fil.
    op = {"id": "co_x", "seq": 1, "kind": "lesson", "scope": "household", "reason": "r", "at": NOW, "changes": [],
          "evidence": {"key": "c:x", "source": "correction", "thread_id": f"{TA}:fil-prive", "turn": 2,
                       "excerpt": "Mon rendez-vous chez le médecin jeudi", "titles": ["Médecin jeudi"],
                       "lesson_kind": "piege"}}
    run = {"id": "cr_x", "night": "2026-11-02", "tz": "Europe/Zurich", "trigger": "manual", "status": "done",
           "dry_run": False, "started_at": NOW}
    for_p = w.c._view(run, [op], Actor(account_id=P, tenant_id=TP))["ops"][0]["evidence"]
    blob = json.dumps(for_p, ensure_ascii=False)
    assert "médecin" not in blob and "Médecin" not in blob and TA not in blob and "fil-prive" not in blob
    assert for_p["counts"] == {"titles": 1} and for_p["lesson_kind"] == "piege"
    for_a = w.c._view(run, [op], Actor(account_id=A, tenant_id=TA))["ops"][0]["evidence"]
    assert for_a["excerpt"].startswith("Mon rendez-vous") and for_a["thread_id"] == f"{TA}:fil-prive"
