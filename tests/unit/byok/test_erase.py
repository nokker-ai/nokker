"""`nokker erase` efface aussi les clés BYOK du locataire au coffre du noyau (`byok.<tn_…>.*`) : à blanc, les noms
sont listés et rien n'est supprimé ; avec --apply, ils sont supprimés. Jamais une valeur dans le compte rendu ; les
clés d'un autre locataire ne bougent pas."""
from __future__ import annotations

from nokker.admin import erase as erase_mod
from nokker.byok.erase import provider_from_config
from nokker.byok.service import ByokSecrets
from nokker.config import NokkerConfig

TA, TB = "tn_aaaaaaaaaaaa", "tn_bbbbbbbbbbbb"
KEY_A1, KEY_A2, KEY_B = "sk-" + "A1-0123456789abcdef", "sk-" + "A2-0123456789abcdef", "sk-" + "B-0123456789abcdef"


def test_erase_lists_names_dry_run_then_deletes_with_apply(monkeypatch, tmp_path):
    monkeypatch.delenv("NOKKER_SECRETS_PROVIDER", raising=False)
    raw = {"enabled": True, "secrets": {"provider": "file"}, "state_dir": str(tmp_path / "byok")}
    vault = ByokSecrets(provider_from_config(raw))
    vault.set(TA, "perso", KEY_A1)
    vault.set(TA, "travail", KEY_A2)
    vault.set(TB, "perso", KEY_B)

    def fake_db(dsn, *, apply, **_kw):
        return erase_mod.Report(account_id="us_aaaaaaaaaaaa", tenant_id=TA, email="a@example.org", devices=[],
                                applied=apply)

    monkeypatch.setattr(erase_mod, "_erase_db", fake_db)
    scope = erase_mod.erase_scope(NokkerConfig(cloud_byok=raw))
    assert scope["cloud_byok"] == raw

    dry = erase_mod.erase_account("postgresql://invalid", account_id="us_aaaaaaaaaaaa", **scope)
    names = [f"byok.{TA}.perso", f"byok.{TA}.travail"]
    assert dry.vault_names == names and dry.deleted["byok: clés du coffre"] == 2
    text = dry.render()
    assert all(n in text for n in names) and "à supprimer" in text
    assert vault.get(TA, "perso") == KEY_A1 and vault.get(TA, "travail") == KEY_A2  # à blanc : rien ne part

    done = erase_mod.erase_account("postgresql://invalid", account_id="us_aaaaaaaaaaaa", apply=True,
                                   confirm="us_aaaaaaaaaaaa", **scope)
    assert done.vault_names == names and "supprimées" in done.render()
    assert vault.get(TA, "perso") is None and vault.get(TA, "travail") is None
    assert vault.get(TB, "perso") == KEY_B  # l'autre locataire ne bouge pas
    for report in (dry, done):
        out = report.render()
        assert KEY_A1 not in out and KEY_A2 not in out and KEY_B not in out


def test_switched_off_touches_nothing():
    assert erase_mod.erase_scope(NokkerConfig())["cloud_byok"] is None
    from nokker.byok.erase import erase_byok_tenant

    assert erase_byok_tenant(None, TA, apply=True) == []
    assert erase_byok_tenant({"enabled": False}, TA, apply=True) == []
