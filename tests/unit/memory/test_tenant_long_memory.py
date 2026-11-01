"""Mémoire longue multi-locataire (docs/MEMORY-TENANCY.md) : service, rappel par tour, quotas, foyer, mineurs,
quarantaine, effacement, routes — magasin en mémoire (mêmes règles que les politiques de la migration 049 ; la base
réelle est prouvée par tests/integration/test_long_memory_rls.py)."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from nokker.core.ports import Recall
from nokker.memory.tenant_long import (
    HOUSEHOLD_PREFIX,
    HashEmbedder,
    LongMemory,
    LongMemoryError,
    LongMemoryTools,
    Membership,
    MemoryLongStore,
    Settings,
    merge_quotas,
)
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

TA, TB, TP, TM, TK, TO = (f"tn_{c * 12}" for c in "abpmko")
A, B, P, M, K, O = "us_aaaa", "us_bbbb", "us_payeur", "us_membre", "us_ado", "us_voisin"
TENANT = {A: TA, B: TB, P: TP, M: TM, K: TK, O: TO}
HID = "pj_foyer000001"


@dataclass
class Acc:
    account_id: str
    tenant_id: str
    status: str = "active"


class World:
    def __init__(self) -> None:
        self.members: dict[str, Membership] = {
            P: Membership(HID, TP, P, "adult"), M: Membership(HID, TP, P, "adult"), K: Membership(HID, TP, P, "teen")}
        self.store = MemoryLongStore(self.membership)
        self.svc = LongMemory(store=self.store, membership=self.membership, embedder=HashEmbedder(),
                              account_for_tenant=self.account_for_tenant, review=True)

    def membership(self, account_id: str) -> Membership | None:
        return self.members.get(account_id)

    def account_for_tenant(self, tid: str) -> Acc | None:
        acc = next((a for a, t in TENANT.items() if t == tid), None)
        return Acc(acc, tid) if acc else None

    def write(self, acc: str, **kw):
        with tenant_scope(TENANT[acc]):
            return self.svc.write(acc, **kw)

    def recall(self, acc: str, query: str, k: int = 5) -> list[Recall]:
        with tenant_scope(TENANT[acc]):
            return self.svc.recall(acc, query, k)


@pytest.fixture
def w() -> World:
    return World()


SECRET = "Mon code de casier au club de tennis est 4471, côté vestiaire nord"


def test_profile_memory_is_per_account_even_for_an_identical_text(w):
    w.write(A, scope="user", kind="fact", title="Casier tennis", text=SECRET)
    got = w.recall(A, "quel est mon code de casier au tennis ?")
    assert [r.text for r in got] == [f"Casier tennis — {SECRET}"]
    assert not got[0].note.startswith(HOUSEHOLD_PREFIX)
    # B pose EXACTEMENT la même question, puis écrit EXACTEMENT le même texte : jamais la note de A
    assert w.recall(B, SECRET) == []
    w.write(B, scope="user", kind="fact", title="Casier tennis", text=SECRET)
    got_b = w.recall(B, SECRET)
    assert len(got_b) == 1
    with tenant_scope(TB):
        assert {n["name"] for n in w.svc.listing(B)["notes"]} == {got_b[0].note}
    assert all(n.account_id == B for n in w.store.notes.values() if n.tenant_id == TB)


def test_household_note_is_common_to_active_members_only(w):
    out = w.write(P, scope="household", kind="fact", title="Dentiste", text="Le dentiste des enfants est le mardi 16h")
    assert "note" in out
    for acc in (P, M, K):
        got = w.recall(acc, "quand est le dentiste ?")
        assert [r.text for r in got] == ["[Mémoire du foyer] Dentiste — Le dentiste des enfants est le mardi 16h"], acc
        assert got[0].note.startswith(HOUSEHOLD_PREFIX)
    assert w.recall(O, "quand est le dentiste ?") == []
    # départ du foyer : plus rien
    del w.members[M]
    assert w.recall(M, "quand est le dentiste ?") == []


def test_adults_only_note_never_reaches_a_minor(w):
    w.write(P, scope="household", kind="fact", title="Cadeau", text="Le cadeau surprise est caché dans le garage",
            audience="adults")
    assert w.recall(M, "où est caché le cadeau surprise ?")
    assert w.recall(K, "où est caché le cadeau surprise ?") == []
    with tenant_scope(TK):
        assert w.svc.listing(K)["notes"] == []
        assert w.svc.get_note(K, "x") is None


def test_audience_is_set_by_the_payer_only(w):
    name = w.write(P, scope="household", kind="fact", title="Code wifi", text="Le code wifi est girafe-42")["note"]["name"]
    with tenant_scope(TM), pytest.raises(LongMemoryError) as e:
        w.svc.set_audience(M, name, "adults")
    assert e.value.status == 403
    with tenant_scope(TP):
        assert w.svc.set_audience(P, name, "adults")["audience"] == "adults"
    assert w.recall(K, "code wifi") == []


def test_member_proposal_waits_for_the_payer(w):
    out = w.write(M, scope="household", kind="rule", title="Courses", text="Les courses se font le samedi matin")
    assert out["pending"]["status"] == "pending"
    assert w.recall(P, "quand faire les courses ?") == []  # rien n'entre avant l'approbation
    pid = out["pending"]["id"]
    assert w.store.pending[pid]["account_id"] == P and w.store.pending[pid]["tenant_id"] == TP

    class Note:
        id, source, scope, kind, project = pid, "household", "household", "rule", HID
        title, text, decided_by = "Courses", "Les courses se font le samedi matin", None

    with tenant_scope(TP):
        name = w.svc.commit_pending(Note())
    got = w.recall(K, "quand faire les courses ?")
    assert got and got[0].note == HOUSEHOLD_PREFIX + name
    stored = next(n for n in w.store.notes.values() if n.name == name)
    assert stored.author_account == M and stored.account_id == P and stored.tenant_id == TP
    # un membre n'approuve pas (il n'est pas le payeur)
    with tenant_scope(TM), pytest.raises(LongMemoryError):
        w.svc.commit_pending(Note())


def test_member_proposal_refused_without_review(w):
    w.svc.review = False
    with pytest.raises(LongMemoryError) as e:
        w.write(M, scope="household", kind="fact", title="x", text="une proposition")
    assert e.value.code == "review_off"


def test_non_member_cannot_write_to_a_household(w):
    with pytest.raises(LongMemoryError) as e:
        w.write(O, scope="household", kind="fact", title="x", text="intrusion")
    assert e.value.status == 404


def test_author_may_withdraw_a_contribution_others_may_not(w):
    w.svc.review = True
    w.members[M] = Membership(HID, TP, P, "adult")
    with tenant_scope(TP):
        n = w.svc._put(w.svc.actor(P), "household", w.members[P], "fact", "Clés", "Les clés sont chez la voisine",
                       "all", M, "pending:x")
    with tenant_scope(TK), pytest.raises(LongMemoryError) as e:
        w.svc.delete(K, n.name)
    assert e.value.status == 403
    with tenant_scope(TM):
        assert w.svc.delete(M, n.name)
    assert w.recall(P, "où sont les clés ?") == []


def test_erasing_a_member_removes_their_household_contributions(w):
    with tenant_scope(TP):
        w.svc._put(w.svc.actor(P), "household", w.members[P], "fact", "Clés", "Les clés sont chez la voisine", "all",
                   M, "pending:x")
    w.write(M, scope="user", kind="fact", title="Perso", text="Je préfère le thé au café")
    assert w.store.erase_account(M, TM) == 2
    assert w.recall(P, "où sont les clés ?") == []


def test_household_deleted_drops_its_notes(w):
    w.write(P, scope="household", kind="fact", title="Dentiste", text="Le dentiste est le mardi")
    w.store.drop_household(HID, TP)
    assert w.recall(M, "dentiste mardi") == []


def test_lexical_recall_without_embedder(w):
    w.svc.embedder = None
    w.write(A, scope="user", kind="fact", title="Allergie", text="Je suis allergique aux arachides")
    got = w.recall(A, "allergique arachides")
    assert got and got[0].text.startswith("Allergie — ")
    assert w.recall(B, "allergique arachides") == []


def test_off_topic_question_recalls_nothing(w):
    w.write(A, scope="user", kind="fact", title="Allergie", text="Je suis allergique aux arachides")
    assert w.recall(A, "quel temps fera-t-il demain à Genève ?") == []


def test_duplicate_is_not_rewritten(w):
    first = w.write(A, scope="user", kind="fact", title="Thé", text="Je bois du thé vert")
    again = w.write(A, scope="user", kind="fact", title="Thé (bis)", text="je bois du  thé vert")
    assert again["duplicate"] and again["note"]["name"] == first["note"]["name"]


def test_validation_and_internal_tenant(w):
    with pytest.raises(LongMemoryError):
        w.write(A, scope="user", kind="fact", title="", text="x")
    with pytest.raises(LongMemoryError):
        w.write(A, scope="user", kind="fact", title="t", text="x", audience="adults")
    with tenant_scope(INTERNAL_TENANT), pytest.raises(LongMemoryError):
        w.svc.write(A, scope="user", title="t", text="x")


def test_quotas_per_scope_then_free_places():
    u = [Recall(note=f"u{i}", text="", score=0.9 - i / 100) for i in range(5)]
    h = [Recall(note=f"foyer:h{i}", text="", score=0.5 - i / 100) for i in range(5)]
    got = [r.note for r in merge_quotas({"user": u, "household": h}, {"user": 3, "household": 2}, 5)]
    assert got == ["u0", "u1", "u2", "foyer:h0", "foyer:h1"]
    got = [r.note for r in merge_quotas({"user": u, "household": []}, {"user": 3, "household": 2}, 5)]
    assert got == ["u0", "u1", "u2", "u3", "u4"]  # places du foyer libres : à la portée user
    dup = [Recall(note="foyer:x", text="a", score=0.4), Recall(note="foyer:x", text="b", score=0.7)]
    assert [r.text for r in merge_quotas({"household": dup}, {"household": 2}, 5)] == ["b"]


def test_app_household_notes_are_merged_in_the_household_quota(w):
    class Legacy:
        def recall_for_account(self, account_id, query, k):
            return [Recall(note="foyer:app-note", text="[Mémoire du foyer] App — note de l'app", score=0.8)]

    w.svc.household_recall = Legacy()
    w.write(A, scope="user", kind="fact", title="Allergie", text="Je suis allergique aux arachides")
    got = w.recall(A, "allergique arachides")
    assert {r.note for r in got} >= {"foyer:app-note"}


def test_tenant_memory_port_routes_testers_and_leaves_internal_alone(w):
    from nokker.tenancy.resources import NullMemory, TenantMemory

    class Internal(NullMemory):
        pass

    internal = Internal()
    tm = TenantMemory(internal, tenant_memory=w.svc.for_tenant)
    w.write(A, scope="user", kind="fact", title="Allergie", text="Je suis allergique aux arachides")
    with tenant_scope(TA):
        assert tm.recall_for("tn_aaaaaaaaaaaa:th", "allergique arachides", 5)
    with tenant_scope(TB):
        assert tm.recall_for("tn_bbbbbbbbbbbb:th", "allergique arachides", 5) == []
    with tenant_scope(INTERNAL_TENANT):
        assert tm.for_tenant() is internal


def test_propose_writes_profile_facts(w):
    with tenant_scope(TA):
        w.svc.for_tenant(TA).propose("tn_aaaaaaaaaaaa:th", ["Je travaille de nuit le jeudi"])
    assert w.recall(A, "je travaille de nuit le jeudi ?")


def test_tools_reuse_the_existing_texts_and_stay_off_by_default(w):
    from nokker.memory.corthexis import _tool_specs

    tools = LongMemoryTools(w.svc)
    assert [(s.name, s.description, s.parameters) for s in tools.specs()] == \
        [(s.name, s.description, s.parameters) for s in _tool_specs("x")]
    assert not tools.available_for(TA)
    w.svc.settings = Settings(tools=True)
    assert tools.available_for(TA)
    w.write(A, scope="user", kind="fact", title="Allergie", text="Je suis allergique aux arachides")
    w.write(P, scope="household", kind="fact", title="Cadeau", text="Cadeau caché au garage", audience="adults")
    from nokker.core.ports import ToolContext

    with tenant_scope(TA):
        res = tools.call("memory_search", {"query": "allergique arachides"}, ToolContext("t", 0, "web"))
        assert "Allergie" in res.content and "Cadeau" not in res.content
    with tenant_scope(TK):
        res = tools.call("memory_search", {"query": "cadeau garage"}, ToolContext("t", 0, "web"))
        assert "Cadeau" not in res.content


def test_settings_from_config():
    s = Settings.from_config({"user_k": 4, "tools": True, "inconnue": 1})
    assert s.user_k == 4 and s.tools is True


def test_routes(w):
    from nokker.memory import long_http

    long_http.configure(None)
    assert long_http.dispatch("GET", "/v1/memory/long", A, lambda: None) is None  # éteint : pas une route
    long_http.configure(w.svc)
    try:
        with tenant_scope(TA):
            st, body = long_http.dispatch("POST", "/v1/memory/long/notes", A,
                                          lambda: {"scope": "user", "title": "Thé", "text": "Je bois du thé vert"})
            assert st == 201
            name = body["note"]["name"]
            st, body = long_http.dispatch("GET", "/v1/memory/long", A, lambda: None)
            assert st == 200 and [n["name"] for n in body["notes"]] == [name] and body["household"] is None
            assert "tenant_id" not in body["notes"][0]
            st, body = long_http.dispatch("GET", "/v1/memory/long/export", A, lambda: None)
            assert st == 200 and [n["name"] for n in body["user"]] == [name]
            st, body = long_http.dispatch("GET", "/v1/memory/long?scope=team", A, lambda: None)
            assert st == 400
        with tenant_scope(TB):
            st, body = long_http.dispatch("DELETE", f"/v1/memory/long/notes/{name}", B, lambda: None)
            assert st == 404  # la note de A n'existe pas pour B
        with tenant_scope(TM):
            st, body = long_http.dispatch("POST", "/v1/memory/long/notes", M,
                                          lambda: {"scope": "household", "title": "Courses", "text": "le samedi"})
            assert st == 202 and body["pending"]["status"] == "pending"
        with tenant_scope(TA):
            st, _ = long_http.dispatch("DELETE", f"/v1/memory/long/notes/{name}", A, lambda: None)
            assert st == 200
    finally:
        long_http.configure(None)


def test_flag_is_off_by_default():
    from nokker.config import NokkerConfig

    assert NokkerConfig().tenant_long_memory is False
