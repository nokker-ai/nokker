"""Effacement d'un locataire dans l'index sémantique de session (`context_corthexis`, never clear sur CortHeXis) : les
notes des épisodes (`<root_dir>/s-<hash du fil>/ep-….md`) et, si `CORTHEXIS_DATABASE_URL` est posé, leurs lignes dans
la base CortHeXis. À blanc, on compte ; validé, il ne reste rien des fils du locataire et tout des autres. Les sorties
ne portent ni l'adresse ni l'identifiant du compte, ni le DSN. La base vraie (pgvector) est couverte par
tests/integration/test_erase.py."""
from __future__ import annotations

import os

import pytest

from nokker.context.corthexis_index import CorthexisSemanticIndex, project_of
from nokker.context.types import Episode

TA, TB = "tn_aaaaaaaaaaaa", "tn_bbbbbbbbbbbb"
ACC, MAIL = "us_aaaaaaaaaaaa", "personne@recette.test"


def _index(root, thread: str, n: int = 2) -> None:
    """Écrit les notes comme le noyau (`index_episode`), sans lancer l'indexeur de fond (pas de CortHeXis ici)."""
    idx = CorthexisSemanticIndex(root)
    idx._enqueue = lambda project: None
    for i in range(n):
        ep = Episode(id=f"ep_{thread[-4:]}{i:04d}", thread_id=thread, seq=i, first_turn=i, last_turn=i,
                     title=f"épisode {i}", summary=f"{MAIL} a parlé de son dossier {i}", closed_at=1.0,
                     day="2026-10-25")
        idx.index_episode(ep, f"## Messages de l'utilisateur\n\n[T{i}] je suis {MAIL}")


@pytest.fixture
def layout(tmp_path):
    root = tmp_path / "session-memory"
    a = [f"{TA}:th_web", f"{TA}:th_web/sa-1", f"{TA}:tg_42"]
    b = [f"{TB}:th_web"]
    for t in a + b:
        _index(root, t)
    return root, a, b


def test_dry_run_counts_and_changes_nothing(layout, monkeypatch):
    from nokker.context.corthexis_erase import FILES_KEY, erase_session_memory

    monkeypatch.delenv("CORTHEXIS_DATABASE_URL", raising=False)
    root, a, b = layout
    counts = erase_session_memory({"enabled": True, "root_dir": str(root)}, a, apply=False)
    assert counts == {FILES_KEY: 6}
    assert all((root / project_of(t)).is_dir() for t in a + b)


def test_apply_erases_the_tenant_threads_and_keeps_the_others(layout, monkeypatch):
    from nokker.context.corthexis_erase import FILES_KEY, erase_session_memory

    monkeypatch.delenv("CORTHEXIS_DATABASE_URL", raising=False)
    root, a, b = layout
    counts = erase_session_memory({"enabled": True, "root_dir": str(root)}, a + [f"{TA}:jamais-indexé"], apply=True)
    assert counts == {FILES_KEY: 6}
    assert not any((root / project_of(t)).exists() for t in a)
    assert [p.name for p in root.iterdir()] == [project_of(b[0])]
    assert len(list((root / project_of(b[0])).glob("ep-*.md"))) == 2
    # Rejoué : rien à effacer, aucune erreur (une tâche planifiée peut repasser).
    assert erase_session_memory({"root_dir": str(root)}, a, apply=True) == {FILES_KEY: 0}


def test_a_symlink_is_removed_never_followed(tmp_path, monkeypatch):
    from nokker.context.corthexis_erase import erase_session_memory

    monkeypatch.delenv("CORTHEXIS_DATABASE_URL", raising=False)
    root = tmp_path / "session-memory"
    root.mkdir()
    outside = tmp_path / "ailleurs"
    outside.mkdir()
    (outside / "ep-garde.md").write_text("à garder")
    thread = f"{TA}:th_web"
    os.symlink(outside, root / project_of(thread))
    erase_session_memory({"root_dir": str(root)}, [thread], apply=True)
    assert not (root / project_of(thread)).exists() and (outside / "ep-garde.md").read_text() == "à garder"


def test_no_block_no_work(tmp_path):
    from nokker.context.corthexis_erase import erase_session_memory

    assert erase_session_memory(None, [f"{TA}:th_web"], apply=True) == {}
    assert erase_session_memory({}, [f"{TA}:th_web"], apply=True) == {}


def test_unreachable_database_refuses_and_touches_no_file(layout, monkeypatch):
    """Base CortHeXis injoignable : erreur, AUCUN fichier effacé (l'appelant annule l'effacement de la base du noyau :
    tout se rejoue), et le message ne porte ni le DSN (mot de passe), ni le compte, ni son adresse."""
    from nokker.context.corthexis_erase import SessionMemoryEraseError, erase_session_memory

    root, a, b = layout
    dsn = "postgresql://cx:s3cr3t-mot-de-passe@127.0.0.1:1/cx?connect_timeout=1"
    monkeypatch.setenv("NOKKER_TEST_CX_ERASE", dsn)
    with pytest.raises(SessionMemoryEraseError) as e:
        erase_session_memory({"root_dir": str(root), "database_url_env": "NOKKER_TEST_CX_ERASE"}, a, apply=True)
    msg = str(e.value)
    assert "s3cr3t" not in msg and "127.0.0.1" not in msg and ACC not in msg and MAIL not in msg
    assert all((root / project_of(t)).is_dir() for t in a)


def test_erase_account_wires_the_session_index(monkeypatch):
    """`nokker erase` passe le bloc `context_corthexis` de la configuration à l'effacement (comme les images et les
    sessions de code) : `erase_scope` est ce que tout chemin d'effacement (CLI, file des suppressions) doit passer."""
    from nokker.admin.erase import erase_scope
    from nokker.config import NokkerConfig

    cfg = NokkerConfig()
    assert erase_scope(cfg)["session_memory"] is None
    cfg.context_corthexis = {"enabled": True, "root_dir": "/var/lib/nokker/session-memory"}
    scope = erase_scope(cfg)
    assert scope["session_memory"] == {"enabled": True, "root_dir": "/var/lib/nokker/session-memory"}
    assert set(scope) == {"images_root", "code", "session_memory"}


def test_cli_erase_passes_the_scope(monkeypatch, tmp_path, capsys):
    from nokker import cli
    from nokker.admin import erase as erase_mod

    cfg_path = tmp_path / "core-config.yaml"
    cfg_path.write_text("context_corthexis: {enabled: true, root_dir: /var/lib/nokker/session-memory}\n")
    seen = {}

    def fake_erase(dsn, **kw):
        seen.update(kw)
        return erase_mod.Report(account_id=ACC, tenant_id=TA, email=MAIL, devices=[])

    monkeypatch.setattr(erase_mod, "erase_account", fake_erase)
    monkeypatch.setattr(cli, "_admin_dsn", lambda args: "postgresql://x")
    assert cli.main(["--config", str(cfg_path), "erase", "--account", ACC]) == 0
    assert seen["session_memory"] == {"enabled": True, "root_dir": "/var/lib/nokker/session-memory"}
