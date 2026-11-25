"""Après `memory forget`, le décompte des notes baisse tout de suite : la liste, le noyau injecté (`core_block`) et
le rappel ne comptent jamais une note oubliée (rangée dans `.forgotten`)."""
import json

from nokker.context.project_memory import ProjectMemoryTools
from tests.unit.context.test_project_memory import ctx, make_memory, make_repo


def _notes(tools, turn):
    r = tools.call("memory", {"action": "list"}, ctx(turn=turn))
    assert not r.is_error, r.content
    return json.loads(r.content)["notes"]


def test_forget_then_count_is_immediate(tmp_path):
    repo = make_repo(tmp_path)
    pm, _ = make_memory(tmp_path)
    pm.open_session("th_1", repo, tmp_path)
    tools = ProjectMemoryTools(pm)
    pm.core_block("th_1", 1, "règles")
    for i, word in enumerate(["alpha", "bravo", "charlie", "delta"]):
        r = tools.call("memory_note", {"kind": "rule", "title": f"R{i} {word}", "text": f"toujours le mot {word} ici {i * 7}",
                                       "derived_from": "user"}, ctx(turn=1))
        assert not r.is_error, r.content
    assert len(_notes(tools, 1)) == 4
    core = pm.core_block("th_1", 2, "suite")
    victim = _notes(tools, 2)[0]["name"]
    assert json.loads(tools.call("memory", {"action": "forget", "name": victim}, ctx(turn=2)).content) == {"forgotten": victim}
    assert len(_notes(tools, 2)) == 3 and victim not in [n["name"] for n in _notes(tools, 2)]
    assert len(pm.learned(pm._session("th_1").project)) == 3
    assert core.count("- [") == 4  # le noyau du tour est figé : celui-ci a été calculé avant l'oubli
    after = pm.core_block("th_1", 3, "suite")  # tour suivant : recalculé, 3 notes
    assert after.count("- [") == 3 and victim not in after
