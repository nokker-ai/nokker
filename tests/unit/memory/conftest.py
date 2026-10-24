"""Fixtures du test d'intégration `DirectCorthexisMemory` (A-2) : Postgres + embed CPU JETABLES.

Même convention que `tests/unit/ledger/conftest.py` (F6) : DSN/URL pris dans l'environnement si
posé, sinon le conteneur jetable par défaut de ce chantier (rapport A-2) ; injoignable -> SKIP,
jamais d'échec. AUCUN de ces défauts ne pointe vers un service de prod.
"""
from __future__ import annotations

import os
import uuid

import pytest

DEFAULT_TEST_DSN = "postgresql://corthexis:corthexis@127.0.0.1:25533/corthexis_test"  # conteneur jetable local
DEFAULT_TEST_EMBED_URL = "http://127.0.0.1:28009"


def _pg_reachable(dsn: str) -> bool:
    psycopg = pytest.importorskip("psycopg", reason="psycopg[binary] non installé dans cet environnement")
    try:
        conn = psycopg.connect(dsn, connect_timeout=2)
        conn.close()
        return True
    except Exception:  # noqa: BLE001 — n'importe quelle panne de connexion -> skip, pas d'échec
        return False


def _embed_reachable(url: str) -> bool:
    httpx = pytest.importorskip("httpx", reason="httpx non installé dans cet environnement")
    try:
        r = httpx.get(f"{url}/health", timeout=2)
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
def corthexis_dsn() -> str:
    dsn = os.environ.get("NOKKER_TEST_CORTHEXIS_DSN", DEFAULT_TEST_DSN)
    if not _pg_reachable(dsn):
        pytest.skip(f"Postgres de test non joignable ({dsn}) — lancer le conteneur jetable du rapport A-2")
    return dsn


@pytest.fixture
def corthexis_embed_url() -> str:
    url = os.environ.get("NOKKER_TEST_CORTHEXIS_EMBED_URL", DEFAULT_TEST_EMBED_URL)
    if not _embed_reachable(url):
        pytest.skip(f"serveur d'embedding de test non joignable ({url}) — lancer le conteneur jetable du rapport A-2")
    return url


@pytest.fixture
def corthexis_notes_dir(tmp_path, corthexis_dsn, corthexis_embed_url):
    """Un petit corpus de notes jetable (PAS une copie de la mémoire de prod — voir
    `test_corthexis_direct_integration.py` pour la mesure A1/A2 sur la vraie copie, hors pytest)."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    (notes_dir / "hote21_gpu_experiment_safety.md").write_text(
        "---\n"
        "description: Avant toute expérience GPU sur HOTE21, vérifier le matériel réel "
        "(clinfo -l) avant tout lancement — le matériel de HOTE21 bouge vite, ne jamais lancer à l'aveugle.\n"
        "---\n\n"
        "# hote21-gpu-experiment-safety\n\n"
        "Toujours vérifier le matériel réel de HOTE21 (`clinfo -l`) avant de lancer un test de charge GPU : "
        "le matériel bouge vite (cartes ajoutées/retirées), et un lancement à l'aveugle peut saturer une "
        "carte déjà utilisée par un autre service de prod.\n",
        encoding="utf-8",
    )
    (notes_dir / "resend_deliverability_pattern.md").write_text(
        "---\n"
        "description: Pattern obligatoire payload Resend pour la délivrabilité.\n"
        "---\n\n"
        "# resend-deliverability-pattern\n\n"
        "Toujours poser `text` ET `html`, plus `List-Unsubscribe` et `List-Unsubscribe-Post`, sur tout "
        "envoi Resend — sinon le mail part en spam chez les gros fournisseurs.\n",
        encoding="utf-8",
    )
    return notes_dir


@pytest.fixture
def indexed_corthexis_memory(corthexis_notes_dir, corthexis_dsn, corthexis_embed_url):
    """`DirectCorthexisMemory` sur une base PROPRE (schéma dédié, jamais réutilisé entre tests) + le
    petit corpus de `corthexis_notes_dir` déjà indexé."""
    import os as _os

    pytest.importorskip("corthexis", reason="paquet corthexis absent (installé --no-deps en dev, pas en CI)")
    from corthexis import embed as cx_embed
    from corthexis import service as cx_service
    from corthexis.indexer import IndexConfig, IndexRunner
    from corthexis.store import Store

    schema_db = f"corthexis_test_{uuid.uuid4().hex[:8]}"
    # une base par test (pas un schéma) : la plus simple isolation avec le même conteneur jetable.
    import psycopg
    admin_dsn = corthexis_dsn.rsplit("/", 1)[0] + "/corthexis_test"
    with psycopg.connect(admin_dsn, autocommit=True) as con:
        con.execute(f'CREATE DATABASE "{schema_db}"')
    dsn = corthexis_dsn.rsplit("/", 1)[0] + f"/{schema_db}"

    _os.environ["CORTHEXIS_DATABASE_URL"] = dsn
    _os.environ["CORTHEXIS_EMBED_URLS"] = corthexis_embed_url
    _os.environ["CORTHEXIS_MEMORY_PROFILE"] = "leger"
    _os.environ.pop("CORTHEXIS_RERANK_URL", None)
    cx_service.reset()
    cx_embed.reset()

    cfg = IndexConfig.from_env(memory_dir=corthexis_notes_dir, normalize=False, write_index=False)
    runner = IndexRunner(lambda: Store(dsn), cx_embed.get, cfg)
    rep = runner.run_once()
    assert rep is not None, f"indexation échouée : {runner.last_error}"

    mem = _direct_memory()
    yield mem
    mem.close()
    with psycopg.connect(admin_dsn, autocommit=True) as con:
        con.execute(f'DROP DATABASE IF EXISTS "{schema_db}" WITH (FORCE)')


def _direct_memory():
    from nokker.memory.corthexis import DirectCorthexisMemory
    return DirectCorthexisMemory()
