"""Points de retour reliés à leur tour (nokker/tools/checkpoints.py, docs/LOKKAL.md §7.4.7) : chaque point pris par le
noyau porte `thread_id`/`turn_index`/`tool`, gardés dans l'état local ; événement `checkpoint.created` ; restaurer un
fichier CRÉÉ après le point le supprime (même outil, même carte), sans jamais perdre la seule copie.

Partie 1 sans l'amont (faux gestionnaire) ; partie 2 avec le vrai gestionnaire (image de dev)."""
from __future__ import annotations

import json

from nokker.core.ports import ToolContext
from nokker.core.turn import Effect
from nokker.tools.checkpoints import CheckpointToolSource, CheckpointingToolSource, TurnLinks, checkpoint_sources
from tests.unit.tools.test_checkpoints import FakeInner, _j, needs_upstream


def ctx(turn=0, tid="th_1"):
    return ToolContext(thread_id=tid, turn_index=turn, channel="api")


class TipManager:
    """Faux gestionnaire : chaque prise qui « réussit » crée un point h<n> en tête."""

    def __init__(self, take=True):
        self.take, self.points, self.log = take, [], []

    def new_turn(self):
        self.log.append("new_turn")

    def ensure_checkpoint(self, root, reason):
        if not self.take:
            return False
        self.points.insert(0, {"hash": f"{len(self.points) + 1:040x}", "short_hash": "x", "timestamp": "t",
                               "reason": reason})
        return True

    def tip(self, root):
        return self.points[0]["hash"] if self.points else None

    def list_checkpoints(self, root):
        return list(self.points)

    def record_agent_write(self, path):
        pass


def test_a_point_taken_before_a_write_is_linked_to_its_turn_and_announced(tmp_path):
    events = []
    links = TurnLinks(tmp_path / "state" / "checkpoints" / "turn-links.json")
    m = TipManager()
    w = CheckpointingToolSource(FakeInner([]), lambda t: tmp_path, manager=m, links=links,
                                emit=lambda n, p: events.append((n, p)))
    w.call("write_file", {"path": "a.py"}, ctx(3))
    h = m.tip(tmp_path)
    assert links.get(h) == {"thread_id": "th_1", "turn_index": 3, "tool": "write_file"}
    assert events == [("checkpoint.created", {"thread_id": "th_1", "turn_index": 3, "hash": h, "tool": "write_file"})]
    m.take = False  # même tour : pas de nouveau point, pas d'événement
    w.call("patch", {"path": "a.py"}, ctx(3))
    assert len(events) == 1
    w.call("read_file", {"path": "a.py"}, ctx(4))  # lecture : jamais de point
    assert len(events) == 1


def test_links_survive_a_restart_and_are_bounded(tmp_path):
    path = tmp_path / "turn-links.json"
    a = TurnLinks(path, cap=3)
    for i in range(5):
        a.add(f"{i:040x}", "th_1", i, "write_file")
    b = TurnLinks(path, cap=3)  # nouveau processus
    assert b.get(f"{4:040x}") == {"thread_id": "th_1", "turn_index": 4, "tool": "write_file"}
    assert b.get(f"{0:040x}") is None and len(json.loads(path.read_text())) == 3
    assert b.get("") is None and b.get("zz") is None
    path.write_text("{pas du json")
    assert TurnLinks(path).get(f"{4:040x}") is None  # fichier abîmé : aucun lien, jamais d'exception


def test_list_carries_the_turn_of_each_point(tmp_path):
    links = TurnLinks(tmp_path / "l.json")
    m = TipManager()
    wrapper = CheckpointingToolSource(FakeInner([]), lambda t: tmp_path, manager=m, links=links)
    tools = CheckpointToolSource(lambda t: tmp_path, manager=m, links=links)
    wrapper.call("write_file", {"path": "a"}, ctx(0))
    m.points.insert(0, {"hash": "f" * 40, "short_hash": "f", "timestamp": "t", "reason": "auto"})  # point d'ailleurs
    listing = json.loads(tools.call("checkpoint_list", {}, ctx()).content)
    assert listing[0]["hash"] == "f" * 40 and "turn_index" not in listing[0]  # point sans lien connu : rien d'inventé
    assert (listing[1]["thread_id"], listing[1]["turn_index"], listing[1]["tool"]) == ("th_1", 0, "write_file")


def test_explicit_create_is_linked_and_announced(tmp_path):
    class Real(TipManager):
        _checkpointed_dirs: set = set()

    events = []
    links = TurnLinks(tmp_path / "l.json")
    m = Real()
    tools = CheckpointToolSource(lambda t: tmp_path, manager=m, links=links, emit=lambda n, p: events.append((n, p)))
    out = json.loads(tools.call("checkpoint_create", {"reason": "avant refonte"}, ctx(7)).content)
    assert out["created"] and out["turn_index"] == 7 and out["thread_id"] == "th_1"
    assert events[0][1] == {"thread_id": "th_1", "turn_index": 7, "hash": out["hash"], "tool": "checkpoint_create"}


def test_restore_keeps_its_strong_effect():
    specs = {s.name: s for s in CheckpointToolSource(lambda t: None, manager=object()).specs()}
    assert specs["checkpoint_restore"].effect is Effect.DESTRUCTIVE  # même carte qu'avant, forte


def test_restore_of_a_created_file_uses_the_managers_path(tmp_path):
    class Created(TipManager):
        def __init__(self):
            super().__init__()
            self.restored = []

        def restore(self, root, h, file, safe=True):
            return {"success": False, "error": "pathspec did not match"}

        def restore_created(self, root, h, file):
            self.restored.append((h, file))
            self.points.insert(0, {"hash": "e" * 40, "short_hash": "e", "timestamp": "t", "reason": "pre"})
            return {"success": True, "restored_to": h[:8], "file": file, "deleted": True}

    events = []
    links = TurnLinks(tmp_path / "l.json")
    m = Created()
    tools = CheckpointToolSource(lambda t: tmp_path, manager=m, links=links, emit=lambda n, p: events.append((n, p)))
    r = json.loads(tools.call("checkpoint_restore", {"hash": "a" * 40, "file": "neuf.py"}, ctx(2)).content)
    assert r["deleted"] and m.restored == [("a" * 40, "neuf.py")]
    # le point « avant retour » pris par la restauration est relié lui aussi
    assert links.get("e" * 40) == {"thread_id": "th_1", "turn_index": 2, "tool": "checkpoint_restore"}
    assert events[-1][1]["tool"] == "checkpoint_restore"


# ─────────────────────────────── avec l'amont ───────────────────────────────


def _real(tmp_path, monkeypatch, events):
    home = tmp_path / "nokker-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    work = tmp_path / "proj"
    work.mkdir()
    (work / "a.txt").write_text("un\n")
    wrapper, tools = checkpoint_sources(FakeInner([]), lambda tid: work, home=home,
                                        emit=lambda n, p: events.append((n, p)))
    return work, wrapper, tools


@needs_upstream
def test_real_points_are_linked_and_listed_with_their_turn(tmp_path, monkeypatch):
    events = []
    work, wrapper, tools = _real(tmp_path, monkeypatch, events)
    wrapper.call("write_file", {"path": "a.txt"}, ctx(0))
    (work / "a.txt").write_text("deux\n")
    wrapper.call("write_file", {"path": "a.txt"}, ctx(1))
    listing = _j(tools.call("checkpoint_list", {}, ctx()))
    assert [(e["turn_index"], e["tool"]) for e in listing] == [(1, "write_file"), (0, "write_file")]
    assert [p["hash"] for n, p in events if n == "checkpoint.created"] == [e["hash"] for e in reversed(listing)]
    # un redémarrage (nouvelles sources, même état) garde les liens
    _, tools2 = checkpoint_sources(FakeInner([]), lambda tid: work, home=tmp_path / "nokker-home")
    assert [e["turn_index"] for e in _j(tools2.call("checkpoint_list", {}, ctx()))] == [1, 0]


@needs_upstream
def test_real_restoring_a_created_file_deletes_it_and_can_be_undone(tmp_path, monkeypatch):
    events = []
    work, wrapper, tools = _real(tmp_path, monkeypatch, events)
    wrapper.call("write_file", {"path": "neuf.py"}, ctx(0))  # photo AVANT l'écriture : neuf.py n'existe pas
    (work / "neuf.py").write_text("print('agent')\n")
    wrapper.manager.record_agent_write(str(work / "neuf.py"))
    point = _j(tools.call("checkpoint_list", {}, ctx()))[0]["hash"]
    r = _j(tools.call("checkpoint_restore", {"hash": point, "file": "neuf.py"}, ctx(1)))
    assert r["success"] and r["deleted"] is True and not (work / "neuf.py").exists()
    assert (work / "a.txt").read_text() == "un\n"  # rien d'autre n'a bougé
    # défaire le retour : le point « avant retour » contient le fichier
    pre = _j(tools.call("checkpoint_list", {}, ctx()))[0]
    assert pre["tool"] == "checkpoint_restore" and pre["hash"] == r["snapshot"]
    _j(tools.call("checkpoint_restore", {"hash": pre["hash"], "file": "neuf.py"}, ctx(1)))
    assert (work / "neuf.py").read_text() == "print('agent')\n"
    # un fichier déjà absent : succès sans rien supprimer
    (work / "neuf.py").unlink()
    r = _j(tools.call("checkpoint_restore", {"hash": point, "file": "neuf.py"}, ctx(2)))
    assert r["success"] and r["deleted"] is False


@needs_upstream
def test_real_a_file_the_points_never_capture_is_not_deleted(tmp_path, monkeypatch):
    events = []
    work, wrapper, tools = _real(tmp_path, monkeypatch, events)
    wrapper.call("write_file", {"path": "x"}, ctx(0))
    point = _j(tools.call("checkpoint_list", {}, ctx()))[0]["hash"]
    (work / "build").mkdir()
    (work / "build" / "out.txt").write_text("généré\n")  # build/ est exclu des points de retour
    (work / ".gitignore").write_text("secret.env\n")
    (work / "secret.env").write_text("K=1\n")  # ignoré par le dépôt : jamais photographié
    for f in ("build/out.txt", "secret.env"):
        r = tools.call("checkpoint_restore", {"hash": point, "file": f}, ctx(1))
        assert r.is_error and "non capturé" in r.content, (f, r.content)
    assert (work / "build" / "out.txt").exists() and (work / "secret.env").exists()
    # hors du dossier, ou un dossier : refusé
    assert tools.call("checkpoint_restore", {"hash": point, "file": "../a.txt"}, ctx(1)).is_error
    assert tools.call("checkpoint_restore", {"hash": point, "file": "build"}, ctx(1)).is_error
    assert (work / "build" / "out.txt").exists()


@needs_upstream
def test_real_local_session_publishes_checkpoint_created(tmp_path, monkeypatch):
    from tests.unit.test_local_api import _api

    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "state"))
    api = _api(tmp_path)
    api.state_dir = tmp_path / "state"
    events = []
    api.emit = lambda n, p: events.append((n, p))
    app = tmp_path / "projets" / "app"
    (app / "a.txt").write_text("x\n")
    tid = api.handle("POST", "/v1/local/sessions", f"Bearer {api.token}", {"cwd": str(app)})[1]["thread_id"]
    wrapper = api.tool_sources()[0]
    r = wrapper.call("write_file", {"path": "b.txt", "content": "y\n"}, ctx(4, tid))
    assert not r.is_error, r.content
    created = [p for n, p in events if n == "checkpoint.created"]
    assert len(created) == 1 and created[0]["thread_id"] == tid and created[0]["turn_index"] == 4
    assert created[0]["tool"] == "write_file" and len(created[0]["hash"]) == 40
    assert (tmp_path / "state" / "checkpoints" / "turn-links.json").is_file()
