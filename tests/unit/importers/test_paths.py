"""Chemins lus par source (montés seuls, en lecture seule, par la commande de l'hôte) : relatifs, jamais un secret
connu, et réellement suffisants pour les lecteurs."""
import json

from nokker.cli import main
from nokker.importers.paths import READ_PATHS, listing


def test_listing_contract(capsys):
    for source, spec in READ_PATHS.items():
        out = listing(source)
        paths = [e["path"] for e in out["paths"] + out["project_paths"]]
        assert all(not p.startswith("/") and ".." not in p.split("/") for p in paths)
        assert {e["kind"] for e in out["paths"] + out["project_paths"]} <= {"file", "dir"}
        for never in spec["never"]:  # aucun chemin listé n'est un secret connu, ni ne le contient
            assert all(not (p == never or never.startswith(p + "/")) for p in paths), (source, never)
        assert not any(w in p for p in paths for w in ("credentials", "auth-profiles", "identity", ".env"))
    assert {"path": "NOKKER.md", "kind": "file"} in listing("cursor")["project_paths"]
    assert main(["import", "claude-code", "--list-paths"]) == 0
    line = capsys.readouterr().out.strip().splitlines()[-1]
    got = json.loads(line)
    assert got["source"] == "claude-code" and {"path": ".claude/skills", "kind": "dir"} in got["paths"]


def test_unknown_source_exits_2(capsys):
    try:
        main(["import", "chatgpt", "--list-paths"])
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("source inconnue acceptée")


def test_every_reason_has_a_stable_code(tmp_path):
    """Le web traduit par `code` : aucune raison produite sur les jeux de test ne tombe dans « other »."""
    from nokker.context.project_memory import ProjectMemory
    from nokker.importers import claude_code, cursor, hermes, openclaw
    from nokker.importers.apply import apply
    from tests.unit.importers.test_claude_code import _home
    from tests.unit.importers.test_cursor import _repo
    from tests.unit.importers.test_hermes import _hermes
    from tests.unit.importers.test_openclaw import _oc

    reports = [claude_code.scan(_home(tmp_path / "a"), nokker_home=tmp_path / "nk"), cursor.scan(tmp_path / "b", _repo(tmp_path / "b"))]
    _hermes(tmp_path / "c")
    reports.append(hermes.scan(tmp_path / "c" / "home", nokker_home=tmp_path / "nk"))
    reports.append(openclaw.scan(_oc(tmp_path / "d"), nokker_home=tmp_path / "nk"))
    after = openclaw.scan(_oc(tmp_path / "e"), nokker_home=tmp_path / "nk2")
    apply(after, user_settings=tmp_path / "s.json", backup_dir=tmp_path / "bak", memory=ProjectMemory(root=tmp_path / "pm"))
    reports.append(after)
    for r in reports:
        for item in r.to_json()["items"]:
            assert item["code"] != "other", (item["kind"], item["reason"])
            assert bool(item["code"]) == bool(item["reason"])
