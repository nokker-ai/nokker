"""Import depuis Codex : consignes globales en notes user, skills (actuel, ancien, dépôt), prompts repris en skills,
mémoire générée montrée sans être reprise, config.toml (MCP http en références secret:NOM, stdio sans équivalent,
modèle et bac à sable au rapport), identifiants jamais lus, rien sauté en silence."""
import json
from pathlib import Path

from nokker.context.project_memory import USER_PROJECT, ProjectMemory
from nokker.importers import codex
from nokker.importers.apply import apply
from nokker.importers.base import UNACCOUNTED, split_frontmatter


def _w(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _codex(tmp: Path) -> Path:
    home = tmp / "home"
    c = home / ".codex"
    _w(c / "AGENTS.md", "Réponds en français.\n\n# Tests\nToujours lancer pytest.\n")
    _w(c / "auth.json", '{"OPENAI_API_KEY": "sk-NE-DOIT-PAS-SORTIR", "tokens": {"access_token": "AT-SECRET"}}')
    _w(c / "history.jsonl", "{}\n")
    _w(c / "sessions" / "2026" / "11" / "01" / "rollout.jsonl", "{}\n")
    _w(c / "prompts" / "review.md", "---\ndescription: Relire le diff courant\n---\nRelis le diff et liste les risques.\n")
    _w(c / "prompts" / "vide.md", "")
    _w(c / "skills" / "legacy" / "SKILL.md", "---\nname: legacy\ndescription: Ancien emplacement\n---\n")
    _w(c / "skills" / ".system" / "bundled" / "SKILL.md", "---\nname: bundled\ndescription: livré\n---\n")
    _w(home / ".agents" / "skills" / "pdf" / "SKILL.md", "---\nname: pdf\ndescription: Lire les PDF\n---\n")
    _w(home / ".agents" / "skills" / "legacy" / "SKILL.md", "---\nname: legacy\ndescription: doublon\n---\n")
    _w(c / "memories_v2" / "memory_summary.md", "L'utilisateur travaille sur Nokker.\n")
    _w(c / "memories_v2" / "MEMORY.md", "- préfère pytest\n")
    _w(c / "memories_v2" / "raw_memories.md", "brut\n")
    _w(c / "memories_v2" / "rollout_summaries" / "a.md", "résumé\n")
    _w(c / "memories_v2" / "extensions" / "ad_hoc" / "notes" / "2026-11-01T10-00-00-x.md", "note du modèle\n")
    _w(c / "memories" / "memory_summary.md", "v1\n")
    _w(c / "config.toml", '''
model = "gpt-5.5"
model_provider = "openai"
approval_policy = "on-request"
sandbox_mode = "workspace-write"
[sandbox_workspace_write]
network_access = false
[shell_environment_policy]
set = { CI = "1", GITHUB_TOKEN = "ghp_NE_PAS_SORTIR" }
[mcp_servers.docs]
url = "https://mcp.example.org/mcp"
bearer_token_env_var = "DOCS_TOKEN"
env_http_headers = { "X-Team" = "TEAM_ID" }
[mcp_servers.raw]
url = "https://raw.example.org/mcp"
http_headers = { "Authorization" = "Bearer SECRET-EN-CLAIR" }
[mcp_servers.fs]
command = "npx"
args = ["fs-server"]
env = { FS_KEY = "VALEUR-SECRETE" }
[mcp_servers.off]
url = "https://off.example.org/mcp"
enabled = false
[tui]
theme = "dark"
''')
    _w(c / "plugins" / "x.json", "{}")
    return home


def test_scan_codex(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = _codex(tmp_path)
    r = codex.scan(home, nokker_home=tmp_path / "nk")
    assert [i.detail["title"] for i in r.items if i.kind == "user-note"] == ["Consignes (Codex)", "Consignes (Codex) : Tests"]
    skills = {(Path(i.source).name, Path(i.source).parent.parent.name): i.status for i in r.items if i.kind == "skill"}
    assert skills == {("pdf", ".agents"): "planned", ("legacy", ".agents"): "planned", ("legacy", ".codex"): "skipped"}
    assert any(i.kind == "runtime" and i.source.endswith(".system") for i in r.items)
    prompts = {Path(i.source).name: i for i in r.items if i.kind == "prompt"}
    assert prompts["review.md"].status == "planned" and prompts["review.md"].detail["prompt"].startswith("Relis le diff")
    assert prompts["vide.md"].status == "skipped"
    mem = [i for i in r.items if i.kind == "codex-memory"]
    assert {(i.detail["version"], Path(i.source).name) for i in mem} == {("v2", "memory_summary.md"), ("v2", "MEMORY.md"), ("v2", "2026-11-01T10-00-00-x.md"), ("v1", "memory_summary.md")}
    assert {i.status for i in mem} == {"skipped"}
    mcp = {i.source.rsplit(".", 1)[1]: i for i in r.items if i.kind == "mcp"}
    assert mcp["docs"].status == "planned" and mcp["docs"].detail["entry"]["token"] == "secret:DOCS_TOKEN"
    assert mcp["docs"].detail["entry"]["headers"] == {"X-Team": "secret:TEAM_ID"}
    assert mcp["raw"].detail["entry"]["token"] == "secret:MCP_RAW_AUTHORIZATION"
    assert mcp["fs"].status == "unsupported" and mcp["off"].status == "skipped"
    assert next(i for i in r.items if i.kind == "model").detail["model"]["model"] == "gpt-5.5"
    assert {i.source.rsplit("#", 1)[1] for i in r.items if i.kind == "sandbox"} == {"approval_policy", "sandbox_mode", "sandbox_workspace_write"}
    names = sorted(i.source.rsplit(".", 1)[1] for i in r.items if i.kind == "secret-name")
    assert names == ["CI", "FS_KEY", "GITHUB_TOKEN"]
    assert any(i.kind == "sensitive" and i.source.endswith("auth.json") for i in r.items)
    assert any(i.kind == "setting" and i.source.endswith("#tui") for i in r.items)
    assert [Path(i.source).name for i in r.items if i.kind == UNACCOUNTED] == ["plugins"]
    dump = json.dumps(r.to_json(), ensure_ascii=False)
    for secret in ("NE-DOIT-PAS-SORTIR", "AT-SECRET", "ghp_NE_PAS_SORTIR", "SECRET-EN-CLAIR", "VALEUR-SECRETE"):
        assert secret not in dump
    assert all(i["code"] not in ("other",) for i in r.to_json()["items"])


def test_apply_codex(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = _codex(tmp_path)
    nk = tmp_path / "nk"
    mem = ProjectMemory(root=nk / "project-memory")
    r = codex.scan(home, nokker_home=nk)
    apply(r, user_settings=nk / "settings.json", backup_dir=tmp_path / "bak", memory=mem)
    meta, body = split_frontmatter((nk / "skills" / "codex" / "review" / "SKILL.md").read_text())
    assert meta == {"name": "review", "description": "Relire le diff courant"} and "Relis le diff" in body
    assert (nk / "skills" / "codex" / "pdf" / "SKILL.md").is_file()
    assert sorted(n.title for n in mem.learned(USER_PROJECT)) == ["Consignes (Codex)", "Consignes (Codex) : Tests"]
    assert not (nk / "project-memory" / USER_PROJECT / "memory_summary.md").exists()
    assert not [i for i in r.items if i.status == "planned"]


def test_project_and_codex_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    proj = tmp_path / "repo"
    _w(proj / "AGENTS.md", "# Projet\n")
    _w(proj / "AGENTS.override.md", "# Prio\n")
    _w(proj / ".agents" / "skills" / "deploy" / "SKILL.md", "---\nname: deploy\ndescription: Déployer\n---\n")
    _w(proj / ".codex" / "config.toml", 'sandbox_mode = "read-only"\n')
    alt = _w(tmp_path / "ailleurs" / "AGENTS.md", "Bonjour.\n").parent
    monkeypatch.setenv("CODEX_HOME", str(alt))
    r = codex.scan(home, proj, nokker_home=tmp_path / "nk")
    assert next(i for i in r.items if i.source.endswith("AGENTS.md") and i.kind == "project-instructions").status == "native"
    assert next(i for i in r.items if i.source.endswith("AGENTS.override.md") and i.kind == "project-instructions").status == "unsupported"
    sk = next(i for i in r.items if i.kind == "skill")
    assert sk.status == "planned" and sk.target.endswith(".nokker/skills/codex/deploy")
    assert any(i.kind == "sandbox" for i in r.items)
    assert [i.detail["title"] for i in r.items if i.kind == "user-note"] == ["Consignes (Codex)"]  # $CODEX_HOME respecté
