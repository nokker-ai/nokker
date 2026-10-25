"""Effacement d'un locataire, hors base, pour les sessions de code (`nokker erase`) : espaces de travail, points de
retour, confiances des hooks et identifiants de forge du coffre — à blanc, on compte ; validé, il ne reste rien du
locataire et tout des autres."""
from __future__ import annotations

import os

import pytest

from nokker.code_sessions.erase import erase_code_tenant

TA, TB = "tn_aaaaaaaaaaaa", "tn_bbbbbbbbbbbb"


@pytest.fixture
def layout(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("NOKKER_CODE_ERASE_KEY", Fernet.generate_key().decode())
    root = tmp_path / "ws"
    raw = {"enabled": True, "backend": "kubernetes", "workspace_root": str(root),
           "secrets": {"provider": "file", "file": {"key_env": "NOKKER_CODE_ERASE_KEY"}}}
    from nokker.code_sessions.secrets import TenantSecrets

    state = root / ".nokker-state"
    vault = TenantSecrets.from_config(raw["secrets"], state / "secrets")
    for tn, us in ((TA, "us_aaaaaaaaaaa1"), (TB, "us_bbbbbbbbbbb1")):
        ws = root / tn / us / "cs_0123456789abcdef"
        ws.mkdir(parents=True)
        (ws / "f.txt").write_text(tn)
        ck = state / tn / us / "checkpoints" / "cs_0123456789abcdef" / "checkpoints" / "store"
        ck.mkdir(parents=True)
        (ck / "HEAD").write_text("ref")
        (state / tn / us / "settings.json").write_text("{}")
        vault.for_tenant(tn).set("forge-bot", f"tok-{tn}")
        vault.for_tenant(tn).set("other", "x")
    monkeypatch.delenv("NOKKER_SECRETS_PROVIDER", raising=False)
    return raw, root, state, vault


def test_dry_run_counts_and_keeps_everything(layout):
    raw, root, state, vault = layout
    counts = erase_code_tenant(raw, TA, apply=False)
    assert counts == {"code: espaces de travail": 1, "code: points de retour": 1,
                      "code: identifiants de forge": 2}
    assert (root / TA).is_dir() and (state / TA).is_dir() and vault.for_tenant(TA).get("forge-bot") == f"tok-{TA}"


def test_apply_erases_a_and_keeps_b(layout):
    raw, root, state, vault = layout
    counts = erase_code_tenant(raw, TA, apply=True)
    assert counts["code: espaces de travail"] == 1 and counts["code: identifiants de forge"] == 2
    assert not (root / TA).exists() and not (state / TA).exists()
    from nokker.code_sessions.secrets import TenantSecrets

    fresh = TenantSecrets.from_config(raw["secrets"], state / "secrets")
    assert fresh.for_tenant(TA).list() == []
    assert (root / TB).is_dir() and (state / TB).is_dir() and fresh.for_tenant(TB).get("forge-bot") == f"tok-{TB}"
    assert erase_code_tenant(raw, TA, apply=True) == {"code: espaces de travail": 0, "code: points de retour": 0,
                                                      "code: identifiants de forge": 0}


def test_without_a_code_block_nothing_happens(tmp_path):
    assert erase_code_tenant(None, TA, apply=True) == {}
    assert erase_code_tenant({"enabled": True}, TA, apply=True) == {}


def test_bad_tenant_or_symlinked_tenant_dir(layout, tmp_path):
    raw, root, state, vault = layout
    with pytest.raises(ValueError):
        erase_code_tenant(raw, "../x", apply=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("1")
    os.symlink(outside, root / "tn_cccccccccccc")
    counts = erase_code_tenant(raw, "tn_cccccccccccc", apply=True)
    assert (outside / "keep").is_file() and not os.path.lexists(root / "tn_cccccccccccc")
    assert counts["code: espaces de travail"] == 0
