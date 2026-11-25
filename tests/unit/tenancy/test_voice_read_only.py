"""Drapeau `voice_read_only` : la VOIX (profondeur 0) lit seulement parmi les outils des sessions de code ; ses enfants
de `delegate` (`/sa-`) gardent tout le jeu. Fil ordinaire et fil de session, drapeau allumé et éteint, et articulation
avec `code_tools_session_only`. Aucun texte vu par le modèle n'est ajouté : on ne fait que retirer des schémas."""
from __future__ import annotations

import pytest

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import ToolResult
from nokker.tenancy.context import tenant_scope
from nokker.tenancy.resources import VOICE_HIDDEN, TenantRegistry
from nokker.tools.delegate import CONV_DESCRIPTION, DelegateToolSource, _Child
from nokker.tools.registry import Registry
from nokker.tools.scope import thread_scope

TN = "tn_voice000001"
SESSION = f"{TN}:s-0123456789abcdef"
PLAIN = f"{TN}:th_plain"
CODE = ("read_file", "search_files", "write_file", "patch", "terminal", "todo", "execute_code",
        "checkpoint_create", "checkpoint_list", "checkpoint_diff", "checkpoint_restore")


class _Code:
    code_session_only = True

    def specs(self):
        return [ToolSpec(name=n, description=n, parameters={}, core=True) for n in CODE]

    def call(self, name, arguments, ctx):
        return ToolResult(call_id="", name=name, content=f"code:{name}")


def _reg(voice, session_only=False):
    conv = DelegateToolSource(lambda *a: None, None, tier=True)
    conv.conversation_only = True
    sessions = DelegateToolSource(lambda *a: None, object(), background=True)
    sessions.code_session_only = True
    reg = TenantRegistry(internal=Registry(sources=[]), tester_sources=[conv, sessions, _Code()])
    reg.has_session = lambda th: th.split("/sa-", 1)[0] == SESSION
    reg.voice_read_only, reg.session_only = voice, session_only
    return reg


def _names(reg):
    return {s.name for s in reg.core()} | {n for n, _ in reg.index()}


def _ctx(th):
    return ToolContext(thread_id=th, turn_index=0, channel="web", sender="us_x")


@pytest.mark.parametrize("thread", [SESSION, PLAIN])
def test_voice_reads_only_but_keeps_the_worktree_delegate_in_session(thread):
    reg = _reg(True)
    with tenant_scope(TN), thread_scope(thread):
        names = _names(reg)
        assert {"read_file", "search_files"} <= names and not names & VOICE_HIDDEN, names & VOICE_HIDDEN
        assert reg.call("write_file", {}, _ctx(thread)).is_error  # schéma absent : outil inconnu
        assert reg.call("read_file", {}, _ctx(thread)).content == "code:read_file"
        desc = reg.spec("delegate").description
        assert ("worktree" in desc) if thread == SESSION else desc == CONV_DESCRIPTION


@pytest.mark.parametrize("thread", [SESSION + "/sa-abc123", PLAIN + "/sa-abc123"])
def test_children_keep_the_whole_set(thread):
    reg = _reg(True)
    with tenant_scope(TN), thread_scope(thread):
        assert set(CODE) <= _names(reg)
        assert reg.call("terminal", {}, _ctx(thread)).content == "code:terminal"


def test_flag_off_the_voice_keeps_everything():
    reg = _reg(False)
    with tenant_scope(TN), thread_scope(SESSION):
        assert set(CODE) <= _names(reg)


def test_with_code_tools_session_only_none_outside_reads_in_session_children_write():
    reg = _reg(True, session_only=True)
    with tenant_scope(TN):
        with thread_scope(PLAIN):
            assert not _names(reg) & set(CODE)  # hors session : aucun outil de code
        with thread_scope(SESSION):
            names = _names(reg)
            assert {"read_file", "search_files"} <= names and not names & VOICE_HIDDEN
        with thread_scope(SESSION + "/sa-abc123"):
            assert set(CODE) <= _names(reg)


def test_a_delegate_child_built_from_the_voice_registry_gets_the_whole_set():
    """`_child_registry` part de `child_sources` (jeu complet) du registre de la voix, pas de son jeu filtré."""
    reg = _reg(True)
    conv = DelegateToolSource(lambda *a: None, None, tier=True)
    with tenant_scope(TN), thread_scope(PLAIN):
        voice = reg.current()
        assert not {s.name for s in voice._all()} & VOICE_HIDDEN and hasattr(voice, "child_sources")
        ch = _Child(index=0, id="c1", thread_id=PLAIN + "/sa-c1", task={"goal": "x"}, schema=None)
        ch.registry = voice
        child = conv._child_registry(ch, None, 0)
    names = {s.name for s in child._all()}
    assert VOICE_HIDDEN - {"checkpoint_restore", "forge_push"} <= names  # l'enfant écrit et exécute (pas de forge ici)
    assert not {n for n in names if n.startswith("checkpoint_")}  # DENIED_FOR_CHILD : jamais de point de retour


def test_voice_and_child_have_distinct_prefix_keys():
    reg = _reg(True)
    with tenant_scope(TN):
        with thread_scope(SESSION):
            a = reg.cache_key()
        with thread_scope(SESSION + "/sa-x"):
            b = reg.cache_key()
    assert a != b and a.endswith("|voice")



def test_the_voice_modifies_nothing_restore_hidden_reading_checkpoints_and_skills_kept():
    """Décision du 03.11 : checkpoint_restore caché à la voix ; un retour à un point passe par la personne (rollback
    de la carte) — un enfant de delegate n'a JAMAIS checkpoint_* (DENIED_FOR_CHILD). checkpoint_create/list/diff restent
    à la voix (aucun fichier touché). Les skills de SESSION n'ont que skill_list et skill_view : rien d'autre à cacher."""
    from nokker.code_sessions.skills import TOOLS as SESSION_SKILLS

    assert "checkpoint_restore" in VOICE_HIDDEN and set(SESSION_SKILLS) == {"skill_list", "skill_view"}
    reg = _reg(True)
    with tenant_scope(TN):
        with thread_scope(SESSION):
            voice = _names(reg)
            assert reg.call("checkpoint_restore", {}, _ctx(SESSION)).is_error
        with thread_scope(SESSION + "/sa-abc123"):
            child = _names(reg)
    assert "checkpoint_restore" not in voice and {"checkpoint_create", "checkpoint_list", "checkpoint_diff"} <= voice
    assert "checkpoint_restore" in child



def test_forge_push_is_never_offered_to_the_voice_but_to_session_children():
    """Décision du 03.11 : forge_push (source marquée code_session_only) caché à la voix ; offert à l'enfant /sa- d'un
    fil de session ; absent d'un fil ordinaire (code_tools_session_only)."""

    class _Forge:
        code_session_only = True

        def specs(self):
            return [ToolSpec(name="forge_push", description="push", parameters={})]

        def call(self, name, arguments, ctx):
            return ToolResult(call_id="", name=name, content="pushed")

    reg = _reg(True, session_only=True)
    reg.tester_sources.append(_Forge())
    with tenant_scope(TN):
        with thread_scope(SESSION):
            assert "forge_push" not in _names(reg) and reg.call("forge_push", {}, _ctx(SESSION)).is_error
        with thread_scope(SESSION + "/sa-abc123"):
            assert "forge_push" in _names(reg)
        with thread_scope(PLAIN):
            assert "forge_push" not in _names(reg)
