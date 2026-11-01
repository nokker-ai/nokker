"""`nokker import` : à blanc par défaut (rien d'écrit, rapport rangé), --apply écrit."""
from nokker.cli import main


def test_import_cli(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    sk = home / ".claude" / "skills" / "pdf" / "SKILL.md"
    sk.parent.mkdir(parents=True)
    sk.write_text("---\nname: pdf\ndescription: PDF\n---\n", encoding="utf-8")
    nk = tmp_path / "nk"
    monkeypatch.setenv("NOKKER_HOME", str(nk))
    assert main(["import", "claude-code", "--home", str(home)]) == 0
    out = capsys.readouterr().out
    assert "planned 1" in out and "--apply" in out
    assert not (nk / "skills").exists()
    assert len(list((nk / "imports").glob("claude-code-*/summary.md"))) == 1
    assert main(["import", "claude-code", "--home", str(home), "--apply"]) == 0
    assert (nk / "skills" / "claude-code" / "pdf" / "SKILL.md").is_file()
    assert "imported 1" in capsys.readouterr().out


def test_state_dir_sets_kernel_settings(tmp_path, monkeypatch, capsys):
    import json

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"deny": ["Bash(rm -rf:*)"]}}), encoding="utf-8")
    monkeypatch.setenv("NOKKER_HOME", str(tmp_path / "ailleurs"))
    state = tmp_path / "opt-data"
    assert main(["import", "claude-code", "--home", str(home), "--state-dir", str(state), "--apply"]) == 0
    assert json.loads((state / "settings.json").read_text())["permissions"]["deny"] == ["terminal(rm -rf*)"]
    assert not (tmp_path / "ailleurs" / "settings.json").exists()
