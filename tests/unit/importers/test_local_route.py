"""Routes locales d'import (écran « Tu viens de X ? ») : jeton local seulement, refus par la passerelle, dossier sous
une racine déclarée, MCP jamais ajouté par la route."""
import json
from pathlib import Path

from nokker.local.api import LocalApi


def _w(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _api(tmp: Path, monkeypatch) -> tuple[LocalApi, Path, str]:
    root = tmp / "work"
    proj = root / "repo"
    _w(proj / ".cursorrules", "Strict.")
    home = tmp / "home"
    _w(home / ".claude" / "skills" / "pdf" / "SKILL.md", "---\nname: pdf\ndescription: PDF\n---\n")
    _w(home / ".claude.json", json.dumps({"mcpServers": {"wiki": {"url": "https://wiki.example.org/mcp"}}}))
    monkeypatch.setenv("NOKKER_IMPORT_HOME", str(home))
    api = LocalApi.from_config({"token_path": str(tmp / "tok"), "state_dir": str(tmp / "state"),
                                "code": {"roots": [str(root)], "backend": "local"}}, port=8742)
    return api, proj, f"Bearer {api.token}"


def test_scan_then_apply(tmp_path, monkeypatch):
    api, proj, auth = _api(tmp_path, monkeypatch)
    st, rep = api.handle("POST", "/v1/local/import/scan", auth, {"source": "claude-code"})
    assert st == 200 and rep["counts"]["planned"] == 2  # skill + serveur MCP
    assert not (tmp_path / "state" / "skills").exists()  # à blanc : rien d'écrit
    st, rep = api.handle("POST", "/v1/local/import/apply", auth, {"source": "claude-code"})
    assert st == 200 and (tmp_path / "state" / "skills" / "claude-code" / "pdf" / "SKILL.md").is_file()
    mcp = next(i for i in rep["items"] if i["kind"] == "mcp")
    assert mcp["status"] == "skipped" and "carte forte" in mcp["reason"]
    assert not (tmp_path / "state" / "mcp-servers.yaml").exists()
    st, rep = api.handle("POST", "/v1/local/import/apply", auth, {"source": "cursor", "project": str(proj)})
    assert st == 200 and (proj / "NOKKER.md").is_file()


def test_guards(tmp_path, monkeypatch):
    api, proj, auth = _api(tmp_path, monkeypatch)
    assert api.handle("POST", "/v1/local/import/scan", "Bearer faux", {"source": "cursor"})[0] == 401
    assert api.handle("POST", "/v1/local/import/scan", auth, {"source": "chatgpt"})[0] == 400
    assert api.handle("POST", "/v1/local/import/scan", auth, {"source": "cursor", "project": str(tmp_path / "home")})[0] == 403
    # par la passerelle : refusé avant tout, même avec un jeton relayé valide
    api.relayed_owner = lambda authorization: {"sub": "us_1"}
    assert api.handle("POST", "/v1/local/import/apply", "Bearer relais", {"source": "cursor"}, relayed=True)[0] == 403


def test_latest_host_report(tmp_path, monkeypatch):
    api, proj, auth = _api(tmp_path, monkeypatch)
    assert api.handle("GET", "/v1/local/import/report", auth, {})[0] == 404
    for d, n in (("claude-code-20261101-090000", 1), ("claude-code-20261101-101500", 2), ("hermes-20261101-120000", 3)):
        _w(tmp_path / "state" / "imports" / d / "report.json", json.dumps({"origin": d.split("-2")[0], "counts": {"planned": n}, "items": []}))
    st, rep = api.handle("GET", "/v1/local/import/report?source=claude-code", auth, {})
    assert st == 200 and rep["counts"] == {"planned": 2} and rep["dir"] == "claude-code-20261101-101500"
    assert api.handle("GET", "/v1/local/import/report", auth, {})[1]["dir"] == "hermes-20261101-120000"
    assert api.handle("GET", "/v1/local/import/report?source=../x", auth, {})[0] == 400
    api.relayed_owner = lambda authorization: {"sub": "us_1"}
    assert api.handle("GET", "/v1/local/import/report", "Bearer relais", {}, relayed=True)[0] == 403
