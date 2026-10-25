"""Coffre des identifiants de forge (`code.secrets`, même forme que `local.secrets` et `sso_settings.secrets`) : portée
par locataire imposée par le noyau, fournisseurs de nokker/secrets/ réutilisés, une seule session OpenBao."""
from __future__ import annotations

import pytest

from nokker.code_sessions.secrets import TenantSecrets
from nokker.secrets.base import SecretsError

TA, TB = "tn_aaaaaaaaaaaa", "tn_bbbbbbbbbbbb"


def test_file_provider_one_file_per_tenant(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("NOKKER_CODE_TEST_KEY", Fernet.generate_key().decode())
    ts = TenantSecrets.from_config({"provider": "file", "file": {"key_env": "NOKKER_CODE_TEST_KEY"}}, tmp_path)
    ts.for_tenant(TA).set("forge-bot", "tok-A")
    assert ts.for_tenant(TA).get("forge-bot") == "tok-A"
    assert ts.for_tenant(TB).get("forge-bot") is None and ts.for_tenant(TB).list() == []
    assert (tmp_path / "tenants" / TA / "vault.bin").is_file() and not (tmp_path / "tenants" / TB / "vault.bin").exists()
    assert ts.for_tenant(TA) is ts.for_tenant(TA)
    with pytest.raises(SecretsError):
        ts.for_tenant("../tn_x")


def test_openbao_paths_are_under_the_tenant_prefix(tmp_path, monkeypatch):
    calls = []

    class Session:
        address = "https://bao.example.org"

        def call(self, method, path, body=None, params=None, what=""):
            calls.append((method, path))
            return {"data": {"data": {"value": "v"}}} if method == "GET" else {}

    sessions = []

    def fake_session(cfg):
        sessions.append(cfg)
        return Session()

    monkeypatch.setattr("nokker.secrets.openbao.session_from_config", fake_session)
    monkeypatch.delenv("NOKKER_SECRETS_PROVIDER", raising=False)
    ts = TenantSecrets.from_config({"provider": "openbao", "openbao": {"address": "https://bao.example.org",
                                                                      "mount": "nokker", "prefix": "corp"}}, tmp_path)
    ts.for_tenant(TA).get("forge-bot")
    ts.for_tenant(TB).set("forge-bot", "x")
    assert calls == [("GET", f"nokker/data/corp/tenants/{TA}/forge/forge-bot"),
                     ("POST", f"nokker/data/corp/tenants/{TB}/forge/forge-bot")]
    assert len(sessions) == 1  # une seule session OpenBao pour tous les locataires


def test_env_provider_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("NOKKER_SECRETS_PROVIDER", "bogus")
    with pytest.raises(SecretsError, match="bogus"):
        TenantSecrets.from_config({"provider": "file"}, tmp_path)
