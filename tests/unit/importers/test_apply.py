"""Écriture d'un import : skills copiés (description courte), NOKKER.md créé sans écrasement, permissions restrictives
ajoutées avec sauvegarde ; le reste n'est jamais laissé « prévu »."""
import json
from pathlib import Path

from nokker.importers import claude_code, cursor
from nokker.importers.apply import apply
from nokker.importers.base import split_frontmatter


def _w(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_apply_claude_code(tmp_path):
    home = tmp_path / "home"
    c = home / ".claude"
    _w(c / "skills" / "pdf" / "SKILL.md", "---\nname: pdf\ndescription: Lire, remplir et fusionner des PDF, extraire les tableaux et le texte des scans\n---\n# PDF\nCorps.\n")
    _w(c / "skills" / "pdf" / "references" / "api.md", "ref")
    _w(c / "settings.json", json.dumps({"permissions": {"deny": ["Bash(rm -rf:*)"], "allow": ["Bash(ls:*)"]}, "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}}))
    _w(c / "CLAUDE.md", "# Style\nCourt.\n")
    settings = tmp_path / "nk" / "settings.json"
    _w(settings, json.dumps({"permissions": {"deny": ["terminal(sudo*)"]}, "mode": "default"}))
    r = claude_code.scan(home, nokker_home=tmp_path / "nkhome")
    apply(r, user_settings=settings, backup_dir=tmp_path / "bak")

    dst = tmp_path / "nkhome" / "skills" / "claude-code" / "pdf"
    meta, body = split_frontmatter((dst / "SKILL.md").read_text())
    assert len(meta["description"]) <= 60 and meta["name"] == "pdf"
    assert body.startswith("> Lire, remplir") and "Corps." in body and (dst / "references" / "api.md").is_file()
    got = json.loads(settings.read_text())
    assert got["permissions"]["deny"] == ["terminal(sudo*)", "terminal(rm -rf*)"] and got["mode"] == "default"
    assert "allow" not in got["permissions"]  # un allow n'est jamais repris en silence
    assert json.loads((tmp_path / "bak" / "settings.json.avant-import").read_text())["permissions"]["deny"] == ["terminal(sudo*)"]
    assert not [i for i in r.items if i.status == "planned"]
    # hooks de l'utilisateur : jamais écrits ; sans mémoire branchée, la note est sautée avec la raison
    assert {i.kind: i.status for i in r.items if i.kind in ("hooks", "user-note")} == {"hooks": "skipped", "user-note": "skipped"}


def test_apply_never_overwrites(tmp_path):
    home = tmp_path / "home"
    _w(home / ".claude" / "skills" / "pdf" / "SKILL.md", "---\nname: pdf\ndescription: PDF\n---\n")
    r = claude_code.scan(home, nokker_home=tmp_path / "nkhome")
    _w(tmp_path / "nkhome" / "skills" / "claude-code" / "pdf" / "SKILL.md", "à moi")  # apparu après le scan
    apply(r, user_settings=tmp_path / "s.json", backup_dir=tmp_path / "bak")
    assert next(i for i in r.items if i.kind == "skill").status == "conflict"
    assert (tmp_path / "nkhome" / "skills" / "claude-code" / "pdf" / "SKILL.md").read_text() == "à moi"


def test_apply_cursor_rules(tmp_path):
    proj = tmp_path / "repo"
    _w(proj / ".cursorrules", "Strict.")
    r = cursor.scan(tmp_path / "home", proj)
    apply(r, user_settings=tmp_path / "s.json", backup_dir=tmp_path / "bak")
    assert (proj / "NOKKER.md").read_text().startswith("# Consignes reprises de Cursor") and "Strict." in (proj / "NOKKER.md").read_text()
    assert {i.status for i in r.items if i.kind == "rules"} == {"imported"}
    # un second scan voit le NOKKER.md : conflit, pas de réécriture
    r2 = cursor.scan(tmp_path / "home", proj)
    assert {i.status for i in r2.items if i.kind == "rules"} == {"conflict"}


def test_symlink_skill_refused(tmp_path):
    home = tmp_path / "home"
    sk = home / ".claude" / "skills" / "evil"
    _w(sk / "SKILL.md", "---\nname: evil\ndescription: x\n---\n")
    (sk / "leak").symlink_to("/etc")
    r = claude_code.scan(home, nokker_home=tmp_path / "nkhome")
    apply(r, user_settings=tmp_path / "s.json", backup_dir=tmp_path / "bak")
    assert next(i for i in r.items if i.kind == "skill").status == "error"
    assert not (tmp_path / "nkhome" / "skills" / "claude-code" / "evil").exists()


def test_apply_subagent_hooks_mcp_notes(tmp_path):
    from nokker.context.project_memory import USER_PROJECT, ProjectMemory
    from nokker.tools.hooks import parse_hooks
    from nokker.tools.mcp_config import load_user_servers

    home, proj = tmp_path / "home", tmp_path / "repo"
    _w(home / ".claude" / "agents" / "reviewer.md", "---\nname: reviewer\ndescription: Relit le code\ntools: Read, Grep\n---\nTu relis le diff.\n")
    _w(home / ".claude" / "CLAUDE.md", "Réponds en français.\n\n# Git\nJamais de force-push.\n")
    _w(home / ".claude.json", json.dumps({"mcpServers": {"Docs": {"type": "http", "url": "https://mcp.example.org/mcp", "headers": {"Authorization": "Bearer S3CR3T-VALUE"}},
                                                          "Wiki": {"url": "https://wiki.example.org/mcp"}}}))
    _w(proj / ".claude" / "settings.json", json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "./lint.sh"}]}]}}))
    _w(proj / ".nokker" / "settings.json", json.dumps({"permissions": {"deny": ["terminal(sudo*)"]}}))
    nk = tmp_path / "nk"
    mem = ProjectMemory(root=nk / "project-memory")
    r = claude_code.scan(home, proj, nokker_home=nk)
    asked = []
    apply(r, user_settings=nk / "settings.json", backup_dir=tmp_path / "bak", mcp_path=nk / "mcp-servers.yaml",
          confirm=lambda it: asked.append(it.detail["entry"]["name"]) or it.detail["entry"]["name"] == "docs", memory=mem)

    # Q1 : sous-agent → skill agents/reviewer
    meta, body = split_frontmatter((nk / "skills" / "agents" / "reviewer" / "SKILL.md").read_text())
    assert meta == {"name": "reviewer", "description": "Relit le code"} and "Tu relis le diff." in body and "Read, Grep" in body
    # Q2 : hooks du dépôt dans son .nokker/settings.json (soumis à confiance), réglages existants gardés
    ps = json.loads((proj / ".nokker" / "settings.json").read_text())
    assert ps["permissions"] == {"deny": ["terminal(sudo*)"]} and parse_hooks(ps["hooks"], "t")
    assert next(i for i in r.items if i.kind == "hooks").status == "imported"
    # Q3 : chaque serveur demandé ; seul le confirmé est ajouté, sans aucune valeur de secret
    assert asked == ["docs", "wiki"]
    servers = load_user_servers(nk / "mcp-servers.yaml")
    assert [s["name"] for s in servers] == ["docs"] and servers[0]["token"] == "secret:MCP_DOCS_TOKEN"
    assert "S3CR3T" not in (nk / "mcp-servers.yaml").read_text()
    assert {i.detail["entry"]["name"]: i.status for i in r.items if i.kind == "mcp"} == {"docs": "imported", "wiki": "skipped"}
    # Q4 : une note user par section, origine import:, idempotent
    learned = mem.learned(USER_PROJECT)
    assert sorted(n.title for n in learned) == ["Consignes générales", "Git"]
    note_file = next((nk / "project-memory" / USER_PROJECT).glob("*.md")).read_text()
    assert 'scope: "user"' in note_file and 'derived_from: "user"' in note_file and 'origin: "import:' in note_file
    r2 = claude_code.scan(home, None, nokker_home=nk)
    apply(r2, user_settings=nk / "settings.json", backup_dir=tmp_path / "bak2", memory=mem)
    assert len(mem.learned(USER_PROJECT)) == 2


def test_mcp_without_terminal_adds_nothing(tmp_path):
    home = tmp_path / "home"
    _w(home / ".claude.json", json.dumps({"mcpServers": {"wiki": {"url": "https://wiki.example.org/mcp"}}}))
    r = claude_code.scan(home, nokker_home=tmp_path / "nk")
    apply(r, user_settings=tmp_path / "s.json", backup_dir=tmp_path / "bak", mcp_path=tmp_path / "nk" / "mcp-servers.yaml", confirm=None)
    assert not (tmp_path / "nk" / "mcp-servers.yaml").exists()
    assert next(i for i in r.items if i.kind == "mcp").status == "skipped"
