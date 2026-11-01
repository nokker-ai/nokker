"""Import depuis Hermes : mémoires en notes user, skills (même format), MCP, noms d'env, tâches cron en brouillons
d'agents valides pour le noyau ; rien sauté en silence, aucun secret copié."""
import json
from pathlib import Path

from nokker.agents.spec import norm_fields
from nokker.context.project_memory import USER_PROJECT, ProjectMemory
from nokker.importers import hermes
from nokker.importers.apply import apply
from nokker.importers.base import UNACCOUNTED


def _w(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _hermes(tmp: Path) -> Path:
    h = tmp / "home" / ".hermes"
    _w(h / "SOUL.md", "Tu es posé et précis.\n")
    _w(h / "memories" / "USER.md", "Alice vit à Genève.\n§\nPréfère le français.\n")
    _w(h / "memories" / "MEMORY.md", "")
    _w(h / "skills" / "ops" / "backup" / "SKILL.md", "---\nname: backup\ndescription: Sauvegarder les bases chaque nuit\n---\nCorps\n")
    _w(h / "skills" / ".bundled_manifest", "x")
    _w(h / "config.yaml", "model:\n  default: qwen3\n  provider: custom\n  base_url: https://router.example.org/v1\n"
                          "mcp_servers:\n  wiki:\n    url: https://wiki.example.org/mcp\n  fs:\n    command: npx\n"
                          "display:\n  theme: dark\n")
    _w(h / ".env", "OPENROUTER_API_KEY=sk-NE-DOIT-PAS-SORTIR\nexport TELEGRAM_TOKEN=abc\n")
    _w(h / "cron" / "jobs.json", json.dumps({"jobs": [
        {"id": "j1", "name": "Point du matin", "prompt": "Résume mes mails", "schedule": {"kind": "cron", "expr": "0 8 * * 1-5"}},
        {"id": "j2", "name": "toutes les 30 min", "prompt": "x", "schedule": "every 30m"},
        {"id": "j3", "name": "script", "script": "backup.sh", "schedule": "0 2 * * *"},
    ]}))
    _w(h / "sessions" / "s1.json", "{}")
    _w(h / "plugins" / "p" / "plugin.yaml", "x")
    return h


def test_scan_hermes(tmp_path):
    h = _hermes(tmp_path)
    r = hermes.scan(tmp_path / "home", nokker_home=tmp_path / "nk")
    notes = [i.detail["title"] for i in r.items if i.kind == "user-note"]
    assert notes == ["Personnalité (Hermes)", "Profil : Alice vit à Genève.", "Profil : Préfère le français."]
    sk = next(i for i in r.items if i.kind == "skill")
    assert sk.status == "planned" and sk.target.endswith("skills/ops/backup")
    assert {i.source.rsplit(".", 1)[1]: i.status for i in r.items if i.kind == "mcp"} == {"wiki": "planned", "fs": "unsupported"}
    assert next(i for i in r.items if i.kind == "model").detail["model"]["default"] == "qwen3"
    assert any(i.kind == "setting" and i.source.endswith("#display") for i in r.items)
    assert sorted(i.source.rsplit("#", 1)[1] for i in r.items if i.kind == "secret-name") == ["OPENROUTER_API_KEY", "TELEGRAM_TOKEN"]
    cron = {i.source.rsplit("#", 1)[1]: i for i in r.items if i.kind == "cron"}
    assert cron["j1"].status == "planned" and cron["j2"].status == "unsupported" and cron["j3"].status == "unsupported"
    body = norm_fields(cron["j1"].detail["draft"], partial=False)  # le brouillon passe la validation du noyau
    assert body["trigger"] == {"kind": "schedule", "cron": "0 8 * * 1-5", "tz": "UTC"} and body["instructions"] == "Résume mes mails"
    assert [Path(i.source).name for i in r.items if i.kind == UNACCOUNTED] == ["plugins"]
    assert "NE-DOIT-PAS-SORTIR" not in json.dumps(r.to_json())
    assert h.is_dir()


def test_apply_hermes(tmp_path):
    _hermes(tmp_path)
    nk = tmp_path / "nk"
    mem = ProjectMemory(root=nk / "project-memory")
    created = []
    r = hermes.scan(tmp_path / "home", nokker_home=nk)
    apply(r, user_settings=nk / "settings.json", backup_dir=tmp_path / "bak", mcp_path=nk / "mcp-servers.yaml",
          confirm=lambda it: True, memory=mem, create_agent=created.append)
    assert (nk / "skills" / "ops" / "backup" / "SKILL.md").is_file()
    assert len(mem.learned(USER_PROJECT)) == 3
    assert [d["name"] for d in created] == ["point-du-matin"]
    assert next(i for i in r.items if i.kind == "cron" and i.status == "imported").reason.startswith("agent créé en pause")
    assert not [i for i in r.items if i.status == "planned"]


def test_no_kernel_keeps_draft_in_report(tmp_path):
    _hermes(tmp_path)
    r = hermes.scan(tmp_path / "home", nokker_home=tmp_path / "nk")
    apply(r, user_settings=tmp_path / "s.json", backup_dir=tmp_path / "bak")
    j1 = next(i for i in r.items if i.kind == "cron" and i.source.endswith("#j1"))
    assert j1.status == "skipped" and "draft" in j1.detail


def test_missing_hermes_home(tmp_path):
    assert hermes.scan(tmp_path).items == []
