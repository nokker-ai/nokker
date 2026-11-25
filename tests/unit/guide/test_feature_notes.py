"""Notes de fonctions (`feature_notes`, défaut éteint) : format standard généré depuis le catalogue, filtres drapeaux et
mineur, bloc absent quand rien ne passe, collection non modifiable, textes nouveaux listés."""
from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest

from nokker.config import NokkerConfig
from nokker.guide import notes
from nokker.guide.catalog import CATALOG, CLOUD, LOKKAL, WHO

SHA = "0b995db"
ON = SimpleNamespace(bubbles_enabled=True, payments=True, show_me=True, agents_enabled=True, web_search_enabled=True)


def _f(fid):
    return next(f for f in CATALOG if f.id == fid)


def test_flag_off_by_default():
    assert NokkerConfig().feature_notes is False


def test_note_exact_template():
    f = _f("payments")
    got = notes.render_note(f, SHA).splitlines()
    assert got[0] == f"[Fonction] payments · version {SHA}"  # drapeaux et cible : filtrés, plus rendus
    assert [ln.split(" : ", 1)[0] for ln in got[1:]] == ["Quoi", "Quand", "Qui fait", "Comment", "Jamais",
                                                          "La personne voit", "Exemple"]
    assert got[1] == f"Quoi : {f.purpose_fr}"
    assert got[3] == "Qui fait : voix, carte de la personne"
    assert got[-1] == f"Exemple : « {f.example_fr} »"


def test_empty_lines_not_rendered_and_never_invented():
    f = next(f for f in CATALOG if not f.who)
    text = notes.render_note(f, SHA)
    for label in ("Quand", "Qui fait", "Comment", "Jamais", "La personne voit"):
        assert f"\n{label} : " not in text
    assert f"Quoi : {f.purpose_fr}" in text


def test_generated_notes_identical_to_catalog():
    gen = notes.generate(SHA)
    assert list(gen) == [f.id for f in CATALOG]
    assert all(gen[f.id] == notes.render_note(f, SHA) for f in CATALOG)
    assert notes.generate(SHA) == gen  # déterministe
    assert all(f"version {SHA}" in t for t in gen.values())


def test_collection_cannot_be_written():
    gen = notes.generate(SHA)
    with pytest.raises(TypeError):
        gen["payments"] = "autre"  # type: ignore[index]
    with pytest.raises(TypeError):
        del gen["payments"]  # type: ignore[attr-defined]


def test_who_values_are_canonical():
    for f in CATALOG:
        assert set(f.who) <= set(WHO), f.id


def test_filter_by_flag():
    assert notes.block(["payments"], SimpleNamespace(), target=CLOUD, minor=False, sha=SHA) == ""
    assert "[Fonction] payments" in notes.block(["payments"], ON, target=CLOUD, minor=False, sha=SHA)


def test_filter_by_target():
    assert notes.block(["payments"], ON, target=LOKKAL, minor=False, sha=SHA) == ""  # cloud seulement


def test_filter_minor():
    assert notes.block(["payments"], ON, target=CLOUD, minor=True, sha=SHA) == ""  # réservée aux adultes
    assert "[Fonction] bubbles" in notes.block(["payments", "bubbles"], ON, target=CLOUD, minor=True, sha=SHA)


def test_no_block_when_nothing_matches():
    assert notes.block([], ON, target=CLOUD, minor=False, sha=SHA) == ""
    assert notes.block(["inconnue"], ON, target=CLOUD, minor=False, sha=SHA) == ""


def test_block_frame_max_and_budget():
    ids = ["bubbles", "payments", "show_me", "crew", "bubbles"]
    out = notes.block(ids, ON, target=CLOUD, minor=False, sha=SHA)
    assert out.startswith("[Fonctions]\n[Fonction] bubbles") and out.endswith("\n[/Fonctions]")
    assert out.count("[Fonction] ") == notes.MAX_NOTES
    small = notes.block(ids, ON, target=CLOUD, minor=False, sha=SHA, budget=len(notes.render_note(_f("bubbles"), SHA))
                        + 30)
    assert small.count("[Fonction] ") == 1  # une note qui ne tient pas est omise, jamais coupée
    assert len(notes.block(ids, ON, target=CLOUD, minor=False, sha=SHA)) <= notes.BUDGET_CHARS


def test_note_for_feature_get():
    assert notes.note_for("payments", ON, target=CLOUD, minor=False, sha=SHA) == notes.render_note(_f("payments"), SHA)
    assert notes.note_for("payments", SimpleNamespace(), target=CLOUD, minor=False, sha=SHA) is None


def test_all_texts_listed():
    for t in (notes.NOTE_HEADER, notes.BLOCK_OPEN, notes.BLOCK_CLOSE, notes.FEATURE_GET_DESC):
        assert t in notes.ALL_NOTE_TEXTS


def test_described_features_use_real_tools():
    """Les outils cités dans « Comment » existent (noms relevés dans les sources)."""
    import pathlib
    import re

    src = "\n".join(p.read_text() for p in pathlib.Path("nokker").rglob("*.py") if p.name != "catalog.py")
    for f in CATALOG:
        for tool in re.findall(r"\b([a-z]+_[a-z_]+)\(", f.tools_how):
            assert f'"{tool}"' in src, (f.id, tool)


def test_undescribed_listed():
    left = notes.undescribed()
    assert "payments" not in left and len(left) == len(CATALOG) - 10


GOLDEN = pathlib.Path(__file__).with_name("feature_notes_golden.txt")


def test_rendered_notes_frozen():
    """Notes relues et validées (03.11) : tout changement de texte passe par une nouvelle relecture."""
    got = "\n\n".join(notes.generate("SHA")[f.id] for f in CATALOG if f.who) + "\n"
    assert got == GOLDEN.read_text(encoding="utf-8")


def test_frame_texts_frozen():
    assert notes.NOTE_HEADER == "[Fonction] {id} · version {sha}"
    assert (notes.BLOCK_OPEN, notes.BLOCK_CLOSE) == ("[Fonctions]", "[/Fonctions]")
    assert notes.FEATURE_GET_DESC == ("Relit la note d'une fonction de Nokker (quoi, quand, qui fait, comment, "
                                      "jamais) par son identifiant, tel qu'il apparaît dans un bloc [Fonctions]. "
                                      "Lecture seule.")
    assert notes.FEATURE_GET_UNKNOWN == "fonction inconnue ou indisponible pour ce compte : {id}"


# ── phase 2 : choix, injection, feature_get ──

class FakeEmbedder:
    """Vecteur = présence de quelques mots clés : assez pour vérifier le chemin des embeddings."""

    KEYS = ("payer", "bulle", "montre", "page", "agenda", "mail", "code", "commande")

    def __init__(self, fail_docs=False, fail_query=False):
        self.fail_docs, self.fail_query, self.docs = fail_docs, fail_query, 0

    def _v(self, text, pad):
        t = text.lower()
        return [1.0 if k in t else 0.0 for k in self.KEYS] + pad

    def embed_docs(self, texts):
        self.docs += 1
        if self.fail_docs:
            raise RuntimeError("embed hors ligne")
        return [self._v(t, [0.01, 0.0]) for t in texts]

    def embed_query(self, text):
        if self.fail_query:
            raise RuntimeError("embed hors ligne")
        return self._v(text, [0.0, 0.01])


def test_lexical_choice_without_embedder():
    fn = notes.FeatureNotes(config=ON, sha=SHA)
    assert fn.warm() is False
    out = fn.block_for("Commande les cartouches d'encre habituelles, je paierai.", minor=False)
    assert "[Fonction] payments" in out
    assert fn.block_for("Bonjour", minor=False) == ""  # rien de pertinent : aucun bloc
    assert fn.block_for("", minor=False) == ""


def test_embedding_choice_and_threshold():
    emb = FakeEmbedder()
    fn = notes.FeatureNotes(config=ON, sha=SHA, embedder=emb, min_cosine=0.5)
    assert fn.warm() is True and emb.docs == 1
    ranked = fn.ranked("Commande et paie avec la carte à payer")
    assert ranked and ranked[0] == "payments"
    assert fn.ranked("zzz") == []  # sous le seuil


def test_embedding_failures_fall_back_to_lexical():
    fn = notes.FeatureNotes(config=ON, sha=SHA, embedder=FakeEmbedder(fail_docs=True))
    assert fn.warm() is False
    assert "payments" in fn.ranked("Commande les cartouches d'encre habituelles")
    fq = notes.FeatureNotes(config=ON, sha=SHA, embedder=FakeEmbedder(fail_query=True))
    assert fq.warm() is True
    assert "payments" in fq.ranked("Commande les cartouches d'encre habituelles")


def test_selector_filters_minor_and_flags():
    fn = notes.FeatureNotes(config=ON, sha=SHA)
    msg = "Commande les cartouches d'encre habituelles"
    assert "payments" in fn.ranked(msg)
    assert "[Fonction] payments" not in fn.block_for(msg, minor=True)
    off = notes.FeatureNotes(config=SimpleNamespace(), sha=SHA)
    assert "[Fonction] payments" not in off.block_for(msg, minor=False)


def test_notes_never_written_to_corthexis():
    """Les notes vivent en mémoire : le sélecteur n'a aucune écriture, seulement des lectures d'embed."""
    emb = FakeEmbedder()
    fn = notes.FeatureNotes(config=ON, sha=SHA, embedder=emb)
    fn.warm()
    with pytest.raises(TypeError):
        fn.notes["payments"] = "x"  # type: ignore[index]
    assert not [m for m in dir(fn) if m.startswith(("write", "save", "store", "put"))]


def _engine(**kw):
    from datetime import datetime

    from nokker.config import PRODUCT_PREFIX_PATH
    from nokker.context.engine import ZURICH, ContextEngineV0
    from nokker.tools.registry import Registry

    return ContextEngineV0(tools=Registry(), prefix_path=str(PRODUCT_PREFIX_PATH),
                           now_fn=lambda: datetime(2026, 11, 3, 9, 0, tzinfo=ZURICH), **kw)


def _head(engine, text="Commande les cartouches d'encre habituelles"):
    from nokker.core.ports import Recall
    from nokker.core.turn import InputCheck, Tier, Turn

    turn = Turn(thread_id="t", index=0, input=InputCheck(text=text), injected=["[Bulle] reçue"])
    out = engine.assemble(turn, [], [Recall(note="n1", text="souvenir", score=0.9)], [], Tier.SHIP)
    return out.messages[-1]["content"]


def test_engine_flag_off_head_unchanged():
    base = _head(_engine())
    assert "[Fonctions]" not in base
    assert _head(_engine(feature_notes=None)) == base


def test_engine_block_after_recall_before_received_context():
    fn = notes.FeatureNotes(config=ON, sha=SHA)
    head = _head(_engine(feature_notes=fn))
    i_recall, i_notes, i_recv = head.index("[/Rappel]"), head.index("[Fonctions]"), head.index("[Bulle] reçue")
    assert i_recall < i_notes < i_recv
    assert head.replace(fn.block_for("Commande les cartouches d'encre habituelles", minor=False) + "\n\n", "") \
        == _head(_engine())


def test_engine_v1_block_placement(tmp_path):
    from nokker.core.ports import Recall
    from nokker.core.turn import InputCheck, Tier, Turn

    from tests.unit.context.harness import make_session

    eng = make_session(tmp_path=tmp_path).kernel.context
    eng.feature_notes = notes.FeatureNotes(config=ON, sha=SHA)
    turn = Turn(thread_id="th_test", index=0, input=InputCheck(text="Commande les cartouches d'encre habituelles"),
                injected=["[Bulle] reçue"])
    head = eng.assemble(turn, [], [Recall(note="n1", text="souvenir", score=0.9)], [], Tier.SHIP).messages[-1]["content"]
    assert head.index("[/Rappel]") < head.index("[Fonctions]") < head.index("[Bulle] reçue")


def test_engine_minor_profile_filters():
    from nokker.config import PRODUCT_PREFIX_PATH
    from nokker.tenancy.context import tenant_scope

    fn = notes.FeatureNotes(config=ON, sha=SHA)
    eng = _engine(feature_notes=fn, young_profile=lambda tid: "teen", product_prefix_path=str(PRODUCT_PREFIX_PATH))
    with tenant_scope("tn_teen"):
        assert "[Fonction] payments" not in _head(eng)


def test_engine_selector_failure_never_breaks_turn():
    class Boom:
        def block_for(self, *a, **k):
            raise RuntimeError("panne")

    assert _head(_engine(feature_notes=Boom())) == _head(_engine())


def test_feature_get_tool():
    from nokker.core.ports import ToolContext
    from nokker.core.turn import Effect

    src = notes.FeatureGetSource(notes=notes.FeatureNotes(config=ON, sha=SHA))
    (spec,) = src.specs()
    assert spec.name == "feature_get" and spec.effect is Effect.READ and spec.description == notes.FEATURE_GET_DESC
    ctx = ToolContext(thread_id="t", turn_index=0, channel="web")
    ok = src.call("feature_get", {"id": "payments"}, ctx)
    assert not ok.is_error and ok.content == notes.render_note(_f("payments"), SHA)
    bad = src.call("feature_get", {"id": "nope"}, ctx)
    assert bad.is_error and bad.content == "fonction inconnue ou indisponible pour ce compte : nope"
    minor = notes.FeatureGetSource(notes=src.notes, minor=lambda: True)
    assert minor.call("feature_get", {"id": "payments"}, ctx).is_error


def test_configure_off_adds_nothing():
    from nokker.api.server import configure_feature_notes

    kernel = SimpleNamespace(context=SimpleNamespace(feature_notes=None), tools=SimpleNamespace(tester_sources=[]))
    assert configure_feature_notes(NokkerConfig(), kernel) is None
    assert kernel.context.feature_notes is None and kernel.tools.tester_sources == []


def test_configure_on_wires_block_and_tool(monkeypatch):
    from nokker.api.server import configure_feature_notes

    monkeypatch.delenv("CORTHEXIS_EMBED_URLS", raising=False)
    refreshed = []
    kernel = SimpleNamespace(context=SimpleNamespace(feature_notes=None),
                             tools=SimpleNamespace(tester_sources=[], refresh=lambda: refreshed.append(1)))
    fn = configure_feature_notes(NokkerConfig(feature_notes=True), kernel)
    assert kernel.context.feature_notes is fn and fn.embedder is None
    assert [type(s).__name__ for s in kernel.tools.tester_sources] == ["FeatureGetSource"] and refreshed
