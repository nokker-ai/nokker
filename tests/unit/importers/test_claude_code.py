"""Import depuis Claude Code : rapport à blanc complet (rien sauté en silence), traductions, secrets jamais copiés."""
import json
from pathlib import Path

from nokker.importers.base import UNACCOUNTED, Report, reconcile, short_description, split_frontmatter
from nokker.importers.claude_code import scan, translate_matcher, translate_tool
from nokker.tools.hooks import parse_hooks


def _w(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _home(tmp: Path) -> Path:
    home = tmp / "home"
    c = home / ".claude"
    _w(c / "CLAUDE.md", "# Style\nRéponds court.\n")
    _w(c / "skills" / "pdf" / "SKILL.md", "---\nname: pdf\ndescription: Lire, remplir et fusionner des PDF, extraire les tableaux et le texte des scans\n---\n# PDF\n")
    _w(c / "skills" / "vide" / "SKILL.md", "---\nname: vide\n---\ncorps\n")
    _w(c / "agents" / "reviewer.md", "---\nname: reviewer\n---\nTu relis.\n")
    _w(c / "projects" / "x" / "hist.jsonl", "{}\n")
    _w(c / "plugins" / "p.json", "{}")
    _w(c / "settings.json", json.dumps({
        "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "./check.sh", "timeout": 5}]}],
            "Notification": [{"hooks": [{"type": "command", "command": "notify"}]}],
        },
        "permissions": {"allow": ["Bash(npm test:*)"], "deny": ["Bash(rm -rf:*)", "WebFetch"]},
        "env": {"OPENAI_API_KEY": "sk-NE-DOIT-PAS-SORTIR"},
        "model": "opus",
    }))
    _w(home / ".claude.json", json.dumps({"mcpServers": {
        "docs": {"type": "http", "url": "https://mcp.example.org/mcp", "headers": {"Authorization": "Bearer XYZ-SECRET"}},
        "fs": {"command": "npx", "args": ["fs-server"]},
    }}))
    return home


def test_scan_user_setup(tmp_path):
    r = scan(_home(tmp_path))
    by = {(i.kind, Path(i.source.split("#")[0]).name if "#" not in i.source else i.source.rsplit("#", 1)[1]): i for i in r.items}
    notes = [i for i in r.items if i.kind == "user-note"]
    assert [(i.status, i.detail["title"], i.detail["text"]) for i in notes] == [("planned", "Style", "Réponds court.")]
    pdf = next(i for i in r.items if i.kind == "skill" and i.source.endswith("pdf"))
    assert pdf.status == "planned" and pdf.target.endswith("skills/claude-code/pdf")
    assert len(pdf.detail["description"]) <= 60 and pdf.detail["long_description"].startswith("Lire, remplir")
    assert next(i for i in r.items if i.kind == "skill" and i.source.endswith("vide")).status == "skipped"
    sa = next(i for i in r.items if i.kind == "subagent")
    assert sa.status == "planned" and sa.target.endswith("skills/agents/reviewer") and sa.detail["prompt"] == "Tu relis."
    assert "sous-agent nommé" in sa.reason
    assert by[("runtime", "projects")].status == "skipped"
    # hooks utilisateur : proposés, jamais écrits en silence ; Notification sans équivalent
    hooks = next(i for i in r.items if i.kind == "hooks")
    assert hooks.status == "skipped" and hooks.detail["block"] == {"pre_tool": [{"match": "terminal", "command": "./check.sh", "timeout_s": 5}]}
    parse_hooks(hooks.detail["block"], "import")  # le bloc proposé est valide pour Nokker
    assert any(i.kind == "hook" and i.source.endswith("hooks.Notification") and i.status == "unsupported" for i in r.items)
    # permissions : deny repris, allow à la main, outil inconnu signalé
    perms = [i for i in r.items if i.kind == "permission"]
    assert {(i.status, i.source.rsplit(".", 1)[1]) for i in perms} == {("skipped", "allow"), ("planned", "deny"), ("unsupported", "deny")}
    # MCP : http proposé (noms d'en-têtes seulement), stdio sans équivalent
    mcp = {i.source.rsplit(".", 1)[1]: i for i in r.items if i.kind == "mcp"}
    assert mcp["docs"].status == "planned"
    assert mcp["docs"].detail["entry"] == {"name": "docs", "url": "https://mcp.example.org/mcp", "token": "secret:MCP_DOCS_TOKEN",
                                           "purpose": "importé (docs)"}
    assert mcp["docs"].detail["secret_names"] == ["MCP_DOCS_TOKEN"]
    assert mcp["fs"].status == "unsupported"
    assert any(i.kind == "setting" and i.source.endswith("#model") for i in r.items)
    # rien n'est sauté en silence : plugins/ n'est lu par aucune étape → une ligne
    un = [i for i in r.items if i.kind == UNACCOUNTED]
    assert [Path(i.source).name for i in un] == ["plugins"]
    # aucune valeur de secret dans le rapport
    dump = json.dumps(r.to_json())
    assert "NE-DOIT-PAS-SORTIR" not in dump and "XYZ-SECRET" not in dump
    assert "OPENAI_API_KEY" in dump


def test_scan_project(tmp_path):
    home = tmp_path / "home"
    proj = tmp_path / "repo"
    _w(proj / "CLAUDE.md", "# Projet\n")
    _w(proj / ".claude" / "skills" / "deploy" / "SKILL.md", "---\nname: deploy\ndescription: Déployer\n---\n")
    _w(proj / ".claude" / "settings.json", json.dumps({"hooks": {"PostToolUse": [{"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "fmt"}]}]}}))
    _w(proj / ".mcp.json", json.dumps({"mcpServers": {"api": {"url": "http://127.0.0.1:9000/mcp"}}}))
    _w(proj / ".nokker" / "skills" / "claude-code" / "deploy" / "SKILL.md", "déjà là")
    r = scan(home, proj)
    assert next(i for i in r.items if i.kind == "project-instructions").status == "native"
    assert next(i for i in r.items if i.kind == "skill").status == "conflict"
    hooks = next(i for i in r.items if i.kind == "hooks")
    assert hooks.status == "planned" and [e["match"] for e in hooks.detail["block"]["post_tool"]] == ["write_file", "patch"]
    assert hooks.target == str(proj / ".nokker" / "settings.json")
    parse_hooks(hooks.detail["block"], "import")
    assert next(i for i in r.items if i.kind == "mcp").status == "planned"
    assert not [i for i in r.items if i.kind == UNACCOUNTED]


def test_translations():
    assert translate_tool("Bash(npm test:*)") == ["terminal(npm test*)"]
    assert translate_tool("Edit") == ["write_file", "patch"]
    assert translate_tool("WebFetch") is None
    assert translate_matcher("") == ["*"] and translate_matcher("Write|Edit") == ["write_file", "patch"]
    assert translate_matcher("Bash|Task") is None


def test_base_helpers(tmp_path):
    assert split_frontmatter("---\nname: a\ndescription: b c\n---\ncorps") == ({"name": "a", "description": "b c"}, "corps")
    assert split_frontmatter("pas de frontmatter") == ({}, "pas de frontmatter")
    s, full = short_description("un " * 40)
    assert len(s) <= 60 and s.endswith("…") and full
    r = Report("x")
    _w(tmp_path / "a" / "b.txt", "x")
    r.add("k", tmp_path / "a", None, "skipped")
    _w(tmp_path / "c.txt", "x")
    reconcile(r, [tmp_path])
    assert [Path(i.source).name for i in r.items if i.kind == UNACCOUNTED] == ["c.txt"]
    r.write(tmp_path / "out")
    assert json.loads((tmp_path / "out" / "report.json").read_text())["counts"]["unsupported"] == 1
    assert "# Import depuis x" in (tmp_path / "out" / "summary.md").read_text()
