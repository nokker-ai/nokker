"""`memory_note` côté nokker.ai (nokker/memory/long_note.py) : la note proposée par le modèle attend dans la quarantaine,
n'entre dans AUCUN rappel avant l'approbation de la personne, s'écrit alors dans SA mémoire longue (jamais celle d'un
autre compte) ; citation vérifiée contre les mots de la personne du tour ; secrets caviardés ; sous-agent, mineur et
message qui n'est pas de la personne refusés ; refusée, la note ne s'écrit jamais. Magasins en mémoire."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nokker.context.project_memory import PENDING_RESULT
from nokker.core.ports import ToolContext
from nokker.core.turn import Effect
from nokker.learning.pending import PendingError, PendingMemory
from nokker.learning.store import MemoryLearningStore
from nokker.memory import long_note as LN
from nokker.tenancy.context import tenant_scope
from tests.unit.memory.test_tenant_long_memory import A, B, TA, TB, TENANT, World

FAKE_KEY = "AKIA" + "Z7Q3VNXW" + "5LPR2K4M"  # faux identifiant, coupé en trois pour le contrôle des secrets
SAID = "Retiens que je préfère les réponses courtes et sans emoji, merci"
QUOTE = "je préfère les réponses courtes"


class Env:
    def __init__(self, allowed=None):
        self.w = World()
        self.store = MemoryLearningStore()
        self.threads = {"th_a": A, "th_b": B}
        self.pending = PendingMemory(store=self.store, account_of_thread=lambda tid: self.threads.get(tid))
        self.pending.commit_note = LN.commit_note_for(self.w.svc, self.pending)
        self.tool = LN.LongNoteTool(self.w.svc, self.pending, scrub=lambda t: t.replace(FAKE_KEY, "[secret]"),
                                    allowed=allowed)

    def held(self, acc=A):
        with tenant_scope(TENANT[acc]):
            return self.store.list_pending(acc)

    def decide(self, nid, approve, acc=A):
        with tenant_scope(TENANT[acc]):
            return self.pending.decide(acc, nid, approve, acc)

    def status(self, nid, acc=A):
        with tenant_scope(TENANT[acc]):
            return self.store.get_pending(acc, nid).status

    def call(self, args, *, tid="th_a", said=SAID, origin="", thread=None):
        turn = SimpleNamespace(input=SimpleNamespace(text=said, origin=origin))
        ctx = ToolContext(thread_id=thread or tid, turn_index=3, channel="web", sender=self.threads.get(tid), turn=turn)
        with tenant_scope(TENANT[self.threads[tid]]):
            return self.tool.call("memory_note", args, ctx)


def args(**kw):
    base = {"kind": "feedback", "title": "Réponses courtes", "text": "Préfère des réponses courtes, sans emoji.",
            "quote": QUOTE}
    base.update(kw)
    return base


@pytest.fixture
def env():
    return Env()


def test_spec_is_a_draft_tool_with_the_frozen_texts_only(env):
    (spec,) = env.tool.specs()
    assert spec.name == "memory_note" and spec.effect is Effect.READ  # la validation humaine (quarantaine) EST la carte and spec.core is False
    assert spec.description == LN.DESCRIPTION and set(spec.parameters["required"]) == {"kind", "title", "text", "quote"}
    assert all(isinstance(t, str) and t for t in LN.ALL_MODEL_TEXTS)
    assert PENDING_RESULT in LN.ALL_MODEL_TEXTS  # le résultat d'attente est celui de Lokkal, mot pour mot


def test_a_note_waits_in_quarantine_and_is_recalled_nowhere_until_approved(env):
    r = env.call(args())
    assert not r.is_error and r.content == PENDING_RESULT.format(id=r.content.split(" : ")[1].split(" ")[0])
    held = env.held()
    assert len(held) == 1 and held[0].source == "memory_note" and held[0].scope == "user" and held[0].kind == "feedback"
    assert held[0].derived_from == "user" and held[0].thread_id == "th_a" and held[0].turn_index == 3
    assert env.w.recall(A, "réponses courtes sans emoji") == []
    with tenant_scope(TA):
        assert env.w.svc.listing(A, None)["notes"] == []


def test_approval_writes_into_the_account_memory_only(env):
    env.call(args())
    nid = env.held()[0].id
    done = env.decide(nid, True)
    assert done.status == "approved" and done.result
    with tenant_scope(TA):
        notes = env.w.svc.listing(A, "user")["notes"]
    assert [n["title"] for n in notes] == ["Réponses courtes"] and notes[0]["name"] == done.result
    assert env.w.recall(A, "réponses courtes sans emoji") and env.w.recall(B, "réponses courtes sans emoji") == []
    with tenant_scope(TB):
        assert env.w.svc.listing(B, "user")["notes"] == []
    with tenant_scope(TA):
        n = env.w.store.find(env.w.svc.actor(A), done.result, None)
    assert n.provenance == "assistant" and n.origin == LN.ORIGIN and n.scope == "user"


def test_a_rejected_note_is_never_written(env):
    env.call(args())
    nid = env.held()[0].id
    assert env.decide(nid, False).status == "rejected"
    with tenant_scope(TA):
        assert env.w.svc.listing(A, None)["notes"] == []


def test_the_quote_must_be_the_persons_words_of_this_turn(env):
    cite = f"{LN.ERR_QUOTE} — {LN.NOT_WRITTEN}"  # B5 : le refus dit que rien n'a été noté
    for bad in ({"quote": "préfère"}, {"quote": ""}, {"quote": "des mots que personne n'a dits ici"}):
        r = env.call(args(**bad))
        assert r.is_error and r.content == cite
    assert env.call(args(), said="Autre chose sans rapport du tout").content == cite
    assert env.call(args(), origin="approval").content == cite  # suite d'une carte : pas la personne
    assert env.call(args(), thread="th_a/sa-1").content == cite  # sous-agent
    r = env.call(args(), said="  RETIENS que JE   préfère les réponses COURTES  ")  # normalisation
    assert not r.is_error
    assert len(env.held()) == 1


def test_form_errors_use_the_lokkal_wording(env):
    assert env.call(args(kind="projet")).content == f"{LN.ERR_KIND} — {LN.NOT_WRITTEN}" and env.call(args(kind="projet")).is_error
    assert env.call(args(title="")).content == f"{LN.ERR_SIZE} — {LN.NOT_WRITTEN}"
    assert env.call(args(text="x" * 1501)).content == f"{LN.ERR_SIZE} — {LN.NOT_WRITTEN}"
    assert env.call(args(title="t" * 121)).content == f"{LN.ERR_SIZE} — {LN.NOT_WRITTEN}"


def test_secrets_are_scrubbed_and_a_duplicate_waits_once(env):
    env.call(args(text="Ma clé " + FAKE_KEY + " ne se note pas, réponses courtes."))
    held = env.held()[0]
    assert "AKIA" not in held.text and "[secret]" in held.text
    env.call(args(text="Ma clé " + FAKE_KEY + " ne se note pas, réponses courtes."))
    assert len(env.held()) == 1  # même note : pas de doublon


def test_unknown_tenant_or_minor_never_sees_the_tool():
    env = Env(allowed=lambda tid: tid != TB)
    assert env.tool.available_for(TA) is True and env.tool.available_for(TB) is False
    assert env.tool.available_for("tn_inconnu00000") is False
    r = env.call(args(), tid="th_b")
    assert r.is_error and r.content == "outil inconnu : memory_note"
    assert env.held(B) == []


def test_commit_failure_leaves_the_note_pending_with_the_reason(env):
    env.call(args())
    nid = env.held()[0].id
    env.threads["th_a"] = None  # compte du fil introuvable à l'approbation
    with pytest.raises(PendingError) as e:
        env.decide(nid, True)
    assert e.value.status == 403 and env.status(nid) == "pending"


def _wire(settings, pending_on=True):
    from nokker.api.server import configure_long_memory
    from nokker.config import NokkerConfig
    from nokker.household import http as household_http
    from nokker.learning import http as learning_http
    from nokker.learning.http import Learning
    from nokker.memory import long_http

    household_http.configure(None)
    lrn = Learning(store=MemoryLearningStore())
    if pending_on:
        lrn.pending = PendingMemory(store=lrn.store, account_of_thread=lambda tid: A)
    learning_http.configure(lrn)
    sources: list = []
    kernel = SimpleNamespace(tools=SimpleNamespace(tester_sources=sources, refresh=lambda: None),
                             memory=SimpleNamespace(), invariants=SimpleNamespace(scrub_output=lambda t: t))
    tenancy = SimpleNamespace(account_for_tenant=lambda tid: SimpleNamespace(account_id=A) if tid == TA else None)
    cfg = NokkerConfig(tenant_long_memory=True, tenant_long_memory_settings=settings, memory_pending=pending_on)
    try:
        configure_long_memory(cfg, kernel, tenancy)
        return kernel, lrn, sources
    finally:
        long_http.configure(None)
        learning_http.configure(None)


def test_wiring_is_off_by_default_and_needs_the_quarantine():
    _k, lrn, sources = _wire({})
    assert sources == [] and lrn.pending.commit_note is None  # éteint : ni outil ni texte
    _k, _lrn, sources = _wire({"note_tool": True}, pending_on=False)
    assert sources == []  # sans memory_pending : éteint, avec un avertissement


def test_wiring_registers_the_tool_and_the_cloud_commit():
    _k, lrn, sources = _wire({"note_tool": True})
    assert [type(s).__name__ for s in sources] == ["LongNoteTool"] and lrn.pending.commit_note is not None
    assert sources[0].available_for(TA) is True and sources[0].available_for("tn_inconnu00000") is False
    assert [s.name for s in sources[0].specs()] == ["memory_note"]


# Gel : relecture des textes du modèle, infra/lokkal-maj/soul/memory_note.tool.json, sha256 du fichier
# dbffcd16…2232e (commit 8ea9c63). `FROZEN` = empreinte des constantes de nokker/memory/long_note.py ; ne se change
# qu'avec une nouvelle relecture. T6 (preference, « déjà noté ») puis B5 (refus explicites) : regelé par la relecture des textes
# le 15.11.2026.
FROZEN = "a029a50a9ec3f4239573b1c1a2d04c43bcde40a75c45acaf13dff3f962ba9b86"


def test_model_visible_texts_are_frozen():
    assert LN.texts_sha256() == FROZEN


def test_minimum_quote_counts_characters_not_bytes(env):
    for short in ("préféré", "été été"):  # 7 et 6 caractères (9 et 8 octets) : refusés, même si les octets dépassent 8
        assert env.call(args(quote=short), said=f"{short} ok").content == f"{LN.ERR_QUOTE} — {LN.NOT_WRITTEN}"
    ok = env.call(args(quote="préféré!"), said="Je trouve ça préféré! vraiment")  # 8 caractères, 10 octets
    assert not ok.is_error and ok.content.startswith("en attente de validation")
