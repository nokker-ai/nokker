"""Import depuis Cursor : règles réunies dans NOKKER.md (jamais d'écrasement), MCP, rien sauté en silence."""
import json
from pathlib import Path

from nokker.importers.base import UNACCOUNTED
from nokker.importers.cursor import rules_markdown, scan
from nokker.importers.cursor import scan as cursor_scan


def _w(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _repo(tmp: Path) -> Path:
    proj = tmp / "repo"
    _w(proj / ".cursorrules", "Toujours du TypeScript strict.")
    _w(proj / ".cursor" / "rules" / "api.mdc", "---\ndescription: Règles de l'API\nglobs: src/api/**\nalwaysApply: false\n---\nValider chaque entrée.\n")
    _w(proj / ".cursor" / "rules" / "vide.mdc", "---\ndescription: rien\n---\n")
    _w(proj / ".cursor" / "mcp.json", json.dumps({"mcpServers": {"gh": {"url": "https://mcp.example.org/gh"}, "local": {"command": "node", "args": ["s.js"]}}}))
    _w(proj / ".cursor" / "notes.txt", "?")
    return proj


def test_rules_markdown(tmp_path):
    text, used, empty = rules_markdown(_repo(tmp_path))
    assert [p.name for p in empty] == ["vide.mdc"]
    assert [p.name for p in used] == [".cursorrules", "api.mdc"]
    assert "## Règles de l'API" in text and "`src/api/**`" in text and "Valider chaque entrée." in text
    assert "TypeScript strict" in text


def test_scan_project(tmp_path):
    proj = _repo(tmp_path)
    r = scan(tmp_path / "home", proj)
    rules = [i for i in r.items if i.kind == "rules"]
    assert {i.status for i in rules} == {"planned", "skipped"} and rules[-1].detail["content"].startswith("# Consignes reprises de Cursor")
    mcp = {i.source.rsplit(".", 1)[1]: i.status for i in r.items if i.kind == "mcp"}
    assert mcp == {"gh": "planned", "local": "unsupported"}
    # notes.txt n'est lu par aucune étape : une ligne, rien de silencieux
    un = sorted(Path(i.source).name for i in r.items if i.kind == UNACCOUNTED)
    assert un == ["notes.txt"]


def test_existing_nokker_md_is_never_overwritten(tmp_path):
    proj = _repo(tmp_path)
    _w(proj / "NOKKER.md", "# à moi\n")
    r = scan(tmp_path / "home", proj)
    assert {i.status for i in r.items if i.kind == "rules"} == {"conflict", "skipped"}
    assert not any("content" in i.detail for i in r.items)


def test_project_out_keeps_repo_read_only(tmp_path):
    from nokker.importers.apply import apply

    proj = _repo(tmp_path)
    out = tmp_path / "import-out"
    out.mkdir()
    r = cursor_scan(tmp_path / "home", proj, project_out=out)
    apply(r, user_settings=tmp_path / "s.json", backup_dir=tmp_path / "bak")
    assert (out / "NOKKER.md").is_file() and not (proj / "NOKKER.md").exists()
    _w(proj / "NOKKER.md", "à moi")  # un NOKKER.md du dépôt reste un conflit, même avec project_out
    out2 = tmp_path / "o2"
    out2.mkdir()
    r2 = cursor_scan(tmp_path / "home", proj, project_out=out2)
    assert {i.status for i in r2.items if i.kind == "rules" and i.reason != "règle vide"} == {"conflict"}
