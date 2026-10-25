"""Serveurs MCP ajoutés depuis une session (lot X16, nokker/tools/mcp_config.py) : identifiants PAR NOM, adresse
contrôlée, fichier 0600 lu au démarrage, la config principale l'emporte, effets des outils."""
from __future__ import annotations

import json
import stat

import pytest

from nokker.core.ports import ToolContext
from nokker.core.turn import Effect
from nokker.tools.mcp_config import (
    MCP_CONFIG_TOOLS,
    McpConfigError,
    McpConfigToolSource,
    check_entry,
    load_user_servers,
    merge_servers,
)

CTX = ToolContext(thread_id="t-mcp", turn_index=0, channel="api", sender="alice")


@pytest.fixture
def src(tmp_path):
    return McpConfigToolSource(path=tmp_path / "mcp-servers.yaml", known_secrets=lambda: ["TRACKER_TOKEN"],
                               configured=lambda: ["memory"])


def call(s, tool, **a):
    r = s.call(tool, a, CTX)
    return r, (json.loads(r.content) if not r.is_error else r.content)


def test_effects():
    specs = {s.name: s.effect for s in McpConfigToolSource(path=None).specs()}  # type: ignore[arg-type]
    assert tuple(specs) == MCP_CONFIG_TOOLS
    assert specs == {"mcp_server_list": Effect.READ, "mcp_server_add": Effect.DESTRUCTIVE,
                     "mcp_server_remove": Effect.DESTRUCTIVE}


def test_add_by_secret_name_list_and_remove(src, tmp_path):
    r, out = call(src, "mcp_server_add", name="tracker", url="https://mcp.example.com/mcp",
                  token="secret:TRACKER_TOKEN", headers={"X-Team": "ops", "X-Api-Key": "secret:TRACKER_KEY"},
                  purpose="issue tracker")
    assert not r.is_error and out["secrets"] == ["TRACKER_TOKEN", "TRACKER_KEY"]
    assert out["missing_secrets"] == ["TRACKER_KEY"] and "nokker secret set TRACKER_KEY" in out["next"]
    f = tmp_path / "mcp-servers.yaml"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert "secret:TRACKER_TOKEN" in f.read_text(encoding="utf-8")
    _, listed = call(src, "mcp_server_list")
    assert listed["servers"][0]["missing_secrets"] == ["TRACKER_KEY"] and listed["configured"] == ["memory"]
    r, _ = call(src, "mcp_server_add", name="tracker", url="https://other.example.com/mcp")
    assert r.is_error and "déjà" in r.content
    r, _ = call(src, "mcp_server_add", name="memory", url="https://other.example.com/mcp")
    assert r.is_error  # nom de config.yaml
    r, out = call(src, "mcp_server_remove", name="tracker")
    assert out == {"removed": "tracker", "active": "au prochain redémarrage du noyau"}
    r, _ = call(src, "mcp_server_remove", name="memory")
    assert r.is_error


@pytest.mark.parametrize("raw,needle", [
    ({"name": "x", "url": "https://h.example.com/mcp", "token": "sk-live-0123456789abcdef"}, "token refusé"),
    ({"name": "x", "url": "https://h.example.com/mcp", "token": "plainvalue"}, "token refusé"),
    ({"name": "x", "url": "https://h.example.com/mcp", "headers": {"X-Api-Key": "a1b2c3d4e5f6a7b8c9d0e1f2a3"}},
     "secret:NOM"),
    ({"name": "x", "url": "https://h.example.com/mcp", "headers": {"Authorization": "secret:K"}}, "Authorization"),
    ({"name": "x", "url": "http://mcp.example.com/mcp"}, "adresse refusée"),
    ({"name": "x", "url": "https://user:pw@h.example.com/mcp"}, "identifiant"),
    ({"name": "x", "url": "https://h.example.com/mcp?key=a1b2c3d4e5f6a7b8c9d0e1f2a3b4"}, "clé"),
    ({"name": "x", "url": "file:///etc/passwd"}, "adresse refusée"),
    ({"name": "Bad Name", "url": "https://h.example.com/mcp"}, "nom refusé"),
])
def test_values_and_bad_addresses_are_refused(raw, needle):
    with pytest.raises(McpConfigError, match=needle):
        check_entry(raw)


def test_local_addresses_allowed():
    for url in ("http://127.0.0.1:8765/mcp", "http://localhost:8765/mcp", "http://notes:8765/mcp"):
        assert check_entry({"name": "n", "url": url})["url"] == url


def test_refused_value_is_never_written(src, tmp_path):
    r, _ = call(src, "mcp_server_add", name="x", url="https://h.example.com/mcp", token="sk-abcdefabcdefabcdef")
    assert r.is_error and not (tmp_path / "mcp-servers.yaml").exists()


def test_load_skips_bad_entries_and_config_wins(tmp_path):
    f = tmp_path / "mcp-servers.yaml"
    f.write_text("servers:\n  - {name: ok, url: 'https://a.example.com/mcp', token: 'secret:A'}\n"
                 "  - {name: bad, url: 'https://b.example.com/mcp', token: 'literal-value'}\n"
                 "  - {name: memory, url: 'https://c.example.com/mcp'}\n", encoding="utf-8")
    added = load_user_servers(f)
    assert [s["name"] for s in added] == ["ok", "memory"]
    merged = merge_servers([{"name": "memory", "url": "https://configured.example.com/mcp"}], added)
    assert [(s["name"], s["url"]) for s in merged] == [("memory", "https://configured.example.com/mcp"),
                                                       ("ok", "https://a.example.com/mcp")]
    assert merged[1]["token"] == "secret:A"
    assert load_user_servers(tmp_path / "absent.yaml") == []
    (tmp_path / "broken.yaml").write_text(": : :", encoding="utf-8")
    assert load_user_servers(tmp_path / "broken.yaml") == []


def test_missing_secrets_unknown_when_vault_unavailable(tmp_path):
    def sealed():
        raise RuntimeError("coffre scellé")

    s = McpConfigToolSource(path=tmp_path / "m.yaml", known_secrets=sealed)
    _, out = call(s, "mcp_server_add", name="t", url="https://h.example.com/mcp", token="secret:T")
    assert "missing_secrets" not in out
