"""L'invitation de `[Mémoire de la session]` nomme `recall_context` : elle n'est rendue que si l'outil est dans le catalogue
du fil (`deny_tools`, source d'outils absente). Sans lui, la balise et le contenu restent, sans la ligne d'invitation.
La valeur est prise au figement du bloc : elle n'entre pas dans la clé, le préfixe ne bouge pas entre deux clôtures."""
from __future__ import annotations

from nokker.approvals.rule_labels import LANGS, lang_scope
from nokker.context.types import ContextBudgets
from nokker.core import model_texts as mt
from nokker.tools.registry import Registry

from .harness import make_session

FILLER = "détail technique " * 40
TAG = "[Mémoire de la session]"
ESCAPE = {"fr": "dis-le simplement", "en": "simply say so", "de": "sage es einfach", "it": "dillo semplicemente"}


class _Denying(Registry):
    """Registre dont `deny_tools` retire `recall_context` du catalogue (comme `ThreadRegistry.core`)."""
    hidden: frozenset = frozenset()

    def core(self):
        return [s for s in super().core() if s.name not in self.hidden]


def _closed(tmp_path, **kw):
    s = make_session(ContextBudgets(current_budget=400, episodes_budget=500, **kw), tmp_path=tmp_path)
    for i in range(12):
        s.say(f"sujet {i} : {FILLER}")
    assert s.manager.episodes.list(s.thread, closed_only=True)
    return s


def _memory_text(s) -> str:
    s.say("et maintenant ?")
    texts = [m["content"] for m in s.provider.seen[-1] if isinstance(m.get("content"), str) and TAG in m["content"]]
    assert texts, "bloc mémoire absent de la requête"
    return texts[0]


def test_hint_has_the_exit_ramp_in_four_languages():
    for lang in LANGS:
        assert ESCAPE[lang] in mt.SESSION_MEMORY_HINT[lang], lang
        assert mt.SESSION_MEMORY_HINT[lang].endswith(")"), lang


def test_available_recall_keeps_the_invitation(tmp_path):
    s = _closed(tmp_path)
    block = _memory_text(s)
    assert "recall_context" in block and block.split("\n", 1)[0] == TAG


def test_denied_recall_drops_the_invitation_but_keeps_the_tag(tmp_path):
    s = _closed(tmp_path)
    reg = _Denying(sources=list(s.registry.sources))
    reg.hidden = frozenset({"recall_context"})
    s.kernel.context.tools = reg
    s.kernel.tools = reg
    s.manager._mem_snap.clear()  # nouveau figement : la valeur est prise à ce moment
    block = _memory_text(s)
    assert block.split("\n", 1)[0] == TAG and "ep_" in block
    assert "recall_context" not in block.split("ep_", 1)[0]
    for lang in LANGS:
        assert mt.SESSION_MEMORY_HINT[lang] not in block


def test_no_recall_source_drops_the_invitation(tmp_path):
    s = make_session(ContextBudgets(current_budget=400, episodes_budget=500), tmp_path=tmp_path, with_recall=False)
    for i in range(12):
        s.say(f"sujet {i} : {FILLER}")
    block = _memory_text(s)
    assert block.split("\n", 1)[0] == TAG and "recall_context" not in block.split("ep_", 1)[0]


def test_legacy_header_also_follows_availability(tmp_path):
    s = _closed(tmp_path, direct_recall_hint=False)
    assert "recall_context(query)" in s.manager.memory_block(s.thread)
    block = s.manager.memory_block(s.thread, recall_available=False)
    assert block.startswith(TAG + "\n") and "AVANT de répondre" not in block


def test_frozen_block_is_stable_and_takes_the_value_at_freeze(tmp_path):
    s = _closed(tmp_path)
    first = s.manager.frozen_memory_block(s.thread)
    assert s.manager.frozen_memory_block(s.thread) == first
    assert s.manager.frozen_memory_block(s.thread, recall_available=False) == first  # figé : la valeur du figement
    for lang in LANGS:
        with lang_scope(lang):
            assert s.manager.memory_block(s.thread) == s.manager.memory_block(s.thread)
