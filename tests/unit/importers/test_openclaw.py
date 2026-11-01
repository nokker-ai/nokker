"""Import depuis OpenClaw : résolution de l'espace de travail comme le Migrator, persona et mémoire en notes user,
skills, MCP http, canaux et modèles au rapport, rien de sensible lu, rien sauté en silence."""
import json
from pathlib import Path

from nokker.context.project_memory import USER_PROJECT, ProjectMemory
from nokker.importers import openclaw
from nokker.importers.apply import apply
from nokker.importers.base import UNACCOUNTED


def _w(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _oc(tmp: Path) -> Path:
    home = tmp / "home"
    oc = home / ".openclaw"
    ws = oc / "workspace-main"  # pas de workspace/ : repli comme le Migrator
    _w(ws / "SOUL.md", "Calme et direct.\n")
    _w(ws / "USER.md", "# Alice\nIngénieure à Genève.\n")
    _w(ws / "IDENTITY.md", "nom")
    _w(ws / "memory" / "2026-10-30.md", "Rendez-vous banque reporté.\n")
    _w(ws / "skills" / "meteo" / "SKILL.md", "---\nname: meteo\ndescription: Météo du jour\n---\n")
    _w(oc / "skills" / "meteo" / "SKILL.md", "---\nname: meteo\ndescription: doublon\n---\n")
    _w(oc / "openclaw.json", json.dumps({
        "agents": {"defaults": {"model": "anthropic/claude", "workspace": "~/.openclaw/workspace-main"}},
        "models": {"providers": {"anthropic": {"api": "anthropic-messages", "apiKey": "sk-ant-NE-DOIT-PAS-SORTIR"}}},
        "mcp": {"servers": {"docs": {"url": "https://mcp.example.org/mcp", "headers": {"X-Key": "SECRET-HDR"}},
                            "fs": {"command": "npx"}, "off": {"url": "https://x.example.org", "enabled": False}}},
        "channels": {"telegram": {"accounts": {"default": {"botToken": "123:TOKEN-SECRET"}}}},
        "env": {"vars": {"OPENAI_API_KEY": "sk-NE-PAS"}},
        "gateway": {"auth": {"token": "GW-SECRET"}},
    }))
    _w(oc / ".env", "ELEVEN_KEY=abc\n")
    _w(oc / "exec-approvals.json", json.dumps({"agents": {"main": {"allowlist": [{"pattern": "git status*"}]}}}))
    _w(oc / "credentials" / "telegram-default-allowFrom.json", "{}")
    _w(oc / "agents" / "main" / "agent" / "auth-profiles.json", '{"profiles": {"a": {"key": "K-SECRET"}}}')
    _w(oc / "agents" / "main" / "sessions" / "s.jsonl", "{}")
    _w(oc / "cron" / "jobs.json", "{}")
    _w(oc / "extensions" / "x" / "index.js", "")
    return home


def test_scan_openclaw(tmp_path):
    home = _oc(tmp_path)
    r = openclaw.scan(home, nokker_home=tmp_path / "nk")
    titles = [i.detail["title"] for i in r.items if i.kind == "user-note"]
    assert titles == ["Personnalité (OpenClaw)", "Profil : Alice", "Mémoire du 2026-10-30"]
    skills = [i for i in r.items if i.kind == "skill"]
    assert [(i.status, Path(i.source).parent.parent.name) for i in skills] == [("planned", "workspace-main"), ("skipped", ".openclaw")]
    assert {i.source.rsplit(".", 1)[1]: i.status for i in r.items if i.kind == "mcp"} == {"docs": "planned", "fs": "unsupported"}
    docs = next(i for i in r.items if i.kind == "mcp" and i.status == "planned")
    assert docs.detail["entry"]["headers"] == {"X-Key": "secret:MCP_DOCS_X_KEY"}
    assert next(i for i in r.items if i.kind == "channel").status == "skipped"
    assert next(i for i in r.items if i.kind == "model").detail["model"]["providers"] == {"anthropic": {"api": "anthropic-messages"}}
    assert sorted(i.source.rsplit("#", 1)[1] for i in r.items if i.kind == "secret-name") == ["ELEVEN_KEY", "OPENAI_API_KEY"]
    assert next(i for i in r.items if i.kind == "permission").detail["rules"] == ["terminal(git status*)"]
    assert next(i for i in r.items if i.kind == "cron").status == "unsupported"
    sens = sorted(Path(i.source).name for i in r.items if i.kind == "sensitive")
    assert sens == ["auth-profiles.json", "credentials"]
    assert [Path(i.source).name for i in r.items if i.kind == UNACCOUNTED] == ["extensions"]
    dump = json.dumps(r.to_json())
    for secret in ("NE-DOIT-PAS-SORTIR", "TOKEN-SECRET", "SECRET-HDR", "sk-NE-PAS", "GW-SECRET", "K-SECRET"):
        assert secret not in dump


def test_apply_openclaw(tmp_path):
    home = _oc(tmp_path)
    nk = tmp_path / "nk"
    mem = ProjectMemory(root=nk / "project-memory")
    r = openclaw.scan(home, nokker_home=nk)
    apply(r, user_settings=nk / "settings.json", backup_dir=tmp_path / "bak", memory=mem)
    assert (nk / "skills" / "openclaw" / "meteo" / "SKILL.md").is_file()
    assert len(mem.learned(USER_PROJECT)) == 3
    assert not [i for i in r.items if i.status == "planned"]


def test_bad_config_falls_back_and_missing_home(tmp_path):
    oc = tmp_path / "home" / ".openclaw"
    _w(oc / "openclaw.json", "{ commentaire json5 }")
    _w(oc / "clawdbot.json", json.dumps({"ui": {}}))
    r = openclaw.scan(tmp_path / "home")
    assert any(i.kind == "setting" and i.source.endswith("clawdbot.json#ui") for i in r.items)
    assert any(i.kind == "settings" and i.source.endswith("openclaw.json") for i in r.items)
    assert openclaw.scan(tmp_path / "vide").items == []
