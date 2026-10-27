"""Effacement d'un compte et retrait d'un appareil (nokker/admin/erase.py) sur un vrai Postgres, avec le rôle du pod
(`nokker_app`, sous RLS). Ignoré sans `NOKKER_TEST_PG_DSN` (voir test_rls.py, dont on reprend la base jetable).

Prouve : chaque table qui porte `tenant_id`, `account_id` ou `"user"` reçoit une ligne de A et une de B ; à blanc, le
compte rendu compte les lignes de A et rien ne bouge ; validé, il ne reste rien de A et tout de B ; le locataire
interne et une confirmation fausse sont refusés ; le retrait d'un appareil ne touche que lui ; l'index sémantique de
session (`context_corthexis` : notes sous `root_dir` et base CortHeXis) part avec le compte, ou rien ne part."""
from __future__ import annotations

import os
import uuid

import pytest

from tests.integration.test_rls import A, B, pg, requires_pg  # noqa: F401  (pg : fixture)

ACC = {A: "us_aaaaaaaaaaaa", B: "us_bbbbbbbbbbbb"}
CHECKED = {"data_consents": {"scope": "training", "source": "signup"}, "connector_credentials": {"kind": "mail"},
           # foyer (039) : une attestation est toujours attestée, pour un profil mineur
           "parental_consents": {"attested": True, "profile": "teen"}, "account_minors": {"profile": "teen"},
           # foyers autorisés aux profils mineurs (043) : l'audit n'accepte que allow | revoke
           "household_minor_grant_audit": {"action": "allow"},
           # modération des pages publiques (040) : une page désignée par son page_id ou son empreinte ; un motif
           "lokkal_public_pages": {"page_id": "pg_0123456789abcdef"},
           "lokkal_page_moderation": {"action": "unpublish", "reason": "signalement"}}


def _dummy_row(conn, table: str) -> tuple[list[str], list]:
    """Comme test_rls._dummy_row, pour tous les types des tables rattachées (dates, octets, tableaux…)."""
    import datetime
    import secrets

    from psycopg.types.json import Jsonb

    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = %s AND is_nullable = 'NO' AND column_default IS NULL ORDER BY ordinal_position",
        (table,)).fetchall()
    vals = {"text": lambda: uuid.uuid4().hex, "character varying": lambda: uuid.uuid4().hex[:12],
            "integer": lambda: secrets.randbelow(10**6), "smallint": lambda: secrets.randbelow(100),
            "bigint": lambda: secrets.randbelow(10**9), "double precision": lambda: 1.0, "real": lambda: 1.0,
            "numeric": lambda: 1, "boolean": lambda: False, "jsonb": lambda: Jsonb({}), "json": lambda: Jsonb({}),
            "uuid": lambda: uuid.uuid4(), "bytea": lambda: secrets.token_bytes(8),
            "date": lambda: datetime.date(2026, 10, 1 + secrets.randbelow(28)),
            "timestamp with time zone": lambda: datetime.datetime.now(datetime.timezone.utc),
            "timestamp without time zone": lambda: datetime.datetime.now(), "ARRAY": lambda: []}
    return [c for c, _ in rows], [vals[t]() for _, t in rows]


def _fks(conn) -> dict[str, list[tuple[str, str, str]]]:
    """table → [(colonne, table parente, colonne parente)] pour les clés étrangères à une colonne."""
    out: dict[str, list[tuple[str, str, str]]] = {}
    for child, col, parent, pcol in conn.execute(
            "SELECT c.conrelid::regclass::text, a.attname, c.confrelid::regclass::text, pa.attname FROM pg_constraint c "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
            "JOIN pg_attribute pa ON pa.attrelid = c.confrelid AND pa.attnum = c.confkey[1] "
            "WHERE c.contype = 'f' AND array_length(c.conkey, 1) = 1").fetchall():
        out.setdefault(child.strip('"'), []).append((col, parent.strip('"'), pcol))
    return out


def _seed(su: str) -> list[str]:
    """Une ligne par locataire (A, B) dans chaque table rattachée, clés étrangères résolues ; rend les tables semées."""
    import psycopg

    from nokker.admin.erase import _children_first, _columns

    with psycopg.connect(su, autocommit=True) as conn:
        cols = _columns(conn)
        scoped = [t for t, cs in cols.items() if cs & {"tenant_id", "account_id", "user"} and t not in ("tenants",)]
        fks = _fks(conn)
        order = list(reversed(_children_first(conn, scoped)))  # parents d'abord
        if "accounts" in order:
            order.remove("accounts")
            order.insert(0, "accounts")
        seeded: dict[tuple[str, str], dict[str, object]] = {}
        for tenant in (A, B):
            for t in order:
                names, vals = _dummy_row(conn, t)
                row = dict(zip(names, vals))
                for col, parent, pcol in fks.get(t, []):
                    if (parent, tenant) in seeded:
                        row[col] = seeded[(parent, tenant)][pcol]
                if "tenant_id" in cols[t]:
                    row["tenant_id"] = tenant
                if "account_id" in cols[t]:
                    row["account_id"] = ACC[tenant]
                if "user" in cols[t]:
                    row["user"] = ACC[tenant]
                row.update(CHECKED.get(t, {}))
                if t == "accounts":
                    row.update(id=ACC[tenant], email=f"{tenant}@recette.test")
                if t == "account_identities":
                    row.update(kind="device", value=f"dev-{tenant}")
                if t == "devices":
                    row.update(id=f"dev-{tenant}", code=tenant[-6:])
                if t == "sessions":
                    row["device_id"] = f"dev-{tenant}"
                q = ", ".join(f'"{c}"' for c in row)
                full = conn.execute(f'INSERT INTO "{t}" ({q}) VALUES ({", ".join(["%s"] * len(row))}) RETURNING *',
                                    list(row.values()))
                desc = [d.name for d in full.description]
                seeded[(t, tenant)] = dict(zip(desc, full.fetchone()))
            conn.execute("INSERT INTO used_nonces (nonce, scope_id, exp) VALUES (%s, %s, 0)",
                         (uuid.uuid4().hex, f"login:dev-{tenant}"))
        return order


def _count(su: str, table: str, tenant: str) -> int:
    import psycopg

    from nokker.admin.erase import _columns

    with psycopg.connect(su) as conn:
        cs = _columns(conn)[table]
        col, val = (("tenant_id", tenant) if "tenant_id" in cs else ("account_id", ACC[tenant]) if "account_id" in cs
                    else ("user", ACC[tenant]) if "user" in cs else ("id", ACC[tenant]))
        return conn.execute(f'SELECT count(*) FROM "{table}" WHERE "{col}" = %s', (val,)).fetchone()[0]


@pytest.fixture(scope="module")
def seeded(pg):  # noqa: F811
    return _seed(pg["su"])


@requires_pg
def test_dry_run_counts_and_changes_nothing(pg, seeded):  # noqa: F811
    from nokker.admin.erase import erase_account

    r = erase_account(pg["app"], email=f"{A}@recette.test")
    assert not r.applied and r.tenant_id == A and r.account_id == ACC[A]
    for t in seeded:
        assert r.deleted.get(t) == 1, (t, r.deleted.get(t))
        assert _count(pg["su"], t, A) == 1
    assert r.deleted["tenants"] == 1 and r.deleted["used_nonces"] == 1
    assert r.unscoped == []  # toute table nouvelle sans rattachement apparaîtrait ici


@requires_pg
def test_refusals(pg, seeded):  # noqa: F811
    import psycopg

    from nokker.admin.erase import EraseError, erase_account
    from nokker.tenancy.context import INTERNAL_TENANT

    with pytest.raises(EraseError, match="--confirm"):
        erase_account(pg["app"], account_id=ACC[A], apply=True, confirm=ACC[B])
    with pytest.raises(EraseError, match="aucun compte"):
        erase_account(pg["app"], email="personne@recette.test")
    with psycopg.connect(pg["su"], autocommit=True) as conn:
        conn.execute("INSERT INTO tenants (id) VALUES (%s) ON CONFLICT DO NOTHING", (INTERNAL_TENANT,))
        cols, vals = _dummy_row(conn, "accounts")
        row = {**dict(zip(cols, vals)), "id": "us_interne0000", "tenant_id": INTERNAL_TENANT, "email": "op@recette.test"}
        conn.execute(f'INSERT INTO accounts ({", ".join(row)}) VALUES ({", ".join(["%s"] * len(row))})', list(row.values()))
    with pytest.raises(EraseError, match="interne"):
        erase_account(pg["app"], account_id="us_interne0000")


@requires_pg
def test_remove_device_touches_only_it(pg, seeded):  # noqa: F811
    import psycopg

    from nokker.admin.erase import remove_device

    msg = remove_device(pg["app"], B[-6:])
    assert msg.startswith("À BLANC") and "1 session(s)" in msg
    with psycopg.connect(pg["su"]) as conn:
        assert conn.execute("SELECT count(*) FROM devices WHERE id = %s", (f"dev-{B}",)).fetchone()[0] == 1
    msg = remove_device(pg["app"], B[-6:], apply=True)
    assert msg.startswith("RETIRÉ") and "il reste 0 appareil" in msg
    with psycopg.connect(pg["su"]) as conn:
        for q in ("SELECT count(*) FROM devices WHERE id = %s", "SELECT count(*) FROM sessions WHERE device_id = %s",
                  "SELECT count(*) FROM account_identities WHERE value = %s"):
            assert conn.execute(q, (f"dev-{B}",)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM used_nonces WHERE scope_id = %s", (f"login:dev-{B}",)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM accounts WHERE id = %s", (ACC[B],)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM devices WHERE id = %s", (f"dev-{A}",)).fetchone()[0] == 1


@requires_pg
def test_apply_erases_a_keeps_b(pg, seeded):  # noqa: F811
    from nokker.admin.erase import erase_account

    r = erase_account(pg["app"], account_id=ACC[A], apply=True, confirm=ACC[A])
    assert r.applied and "journal des effacements (coffre de l'opérateur)" in r.render() and A in r.journal_line()
    for t in seeded:
        assert _count(pg["su"], t, A) == 0, t
        if t not in ("devices", "sessions", "account_identities"):  # celles de B : retirées par le test précédent
            assert _count(pg["su"], t, B) == 1, t
    import psycopg

    with psycopg.connect(pg["su"]) as conn:
        assert conn.execute("SELECT count(*) FROM tenants WHERE id = %s", (A,)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM tenants WHERE id = %s", (B,)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM used_nonces WHERE scope_id = %s", (f"login:dev-{A}",)).fetchone()[0] == 0


@requires_pg
def test_turns_stay_append_only_outside_erasure(pg, seeded):  # noqa: F811
    import psycopg

    with psycopg.connect(pg["app"]) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (B,))
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("DELETE FROM turns")
    with psycopg.connect(pg["app"]) as conn:  # le réglage d'effacement n'ouvre jamais l'UPDATE, ni un autre locataire
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (B,))
        conn.execute("SELECT set_config('nokker.erasing_tenant', %s, true)", (B,))
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("UPDATE turns SET turn = '{}'")
    with psycopg.connect(pg["app"]) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (B,))
        conn.execute("SELECT set_config('nokker.erasing_tenant', %s, true)", (A,))
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("DELETE FROM turns")


@requires_pg
def test_deletion_granted_by_the_person_suspends_then_erase_pending_clears_it(pg):  # noqa: F811
    """Suppression par la personne (nokker/account/delete.py) sur Postgres : compte et locataire suspendus, ligne
    dans `account_deletions` ; `nokker erase --pending` l'efface, la ligne de la file partant avec le compte."""
    import psycopg

    from nokker.admin.erase import erase_account, pending_deletions
    from nokker.tenancy.store import PgTenancy

    tenancy = PgTenancy(pg["app"])
    acc = tenancy.create_account("supprime-moi@recette.test")
    tenancy.suspend_for_deletion(acc.account_id, "ap-1")
    tenancy.suspend_for_deletion(acc.account_id, "ap-2")  # rejouée : une seule ligne, la première demande
    assert tenancy.account(acc.account_id).status == "suspended"
    with psycopg.connect(pg["su"]) as conn:
        assert conn.execute("SELECT status FROM tenants WHERE id = %s", (acc.tenant_id,)).fetchone()[0] == "suspended"
        assert conn.execute("SELECT approval_id FROM account_deletions WHERE account_id = %s",
                            (acc.account_id,)).fetchone()[0] == "ap-1"
    assert acc.account_id in pending_deletions(pg["app"])
    r = erase_account(pg["app"], account_id=acc.account_id, apply=True, confirm=acc.account_id)
    assert r.applied and r.deleted.get("account_deletions") == 1
    assert acc.account_id not in pending_deletions(pg["app"]) and tenancy.account(acc.account_id) is None


def test_active_subscription_blocks_erasure(pg, seeded):  # noqa: F811
    import psycopg

    from nokker.admin.erase import EraseError, erase_account

    with psycopg.connect(pg["su"], autocommit=True) as conn:
        conn.execute("UPDATE subscriptions SET status = 'active' WHERE tenant_id = %s", (B,))
    with pytest.raises(EraseError, match="abonnement actif"):
        erase_account(pg["app"], account_id=ACC[B])
    with psycopg.connect(pg["su"], autocommit=True) as conn:
        conn.execute("UPDATE subscriptions SET status = 'canceled' WHERE tenant_id = %s", (B,))
    assert erase_account(pg["app"], account_id=ACC[B]).deleted.get("subscriptions") == 1  # à blanc : compté


# ── index sémantique de session (never clear sur CortHeXis, `context_corthexis`) ──
# Un locataire C (son compte, deux fils, des épisodes) et un locataire D (un fil) : leurs épisodes sont écrits comme par
# le noyau (`CorthexisSemanticIndex.index_episode`) sous `root_dir` et, avec `NOKKER_TEST_CX_DSN` (Postgres avec
# l'extension `vector`) et le paquet `corthexis`, indexés par le VRAI indexeur de CortHeXis (embeddings factices).
# L'effacement de C retire ses notes des deux stockages, garde celles de D, et ne dit ni l'adresse ni le compte.
C, D = "tn_cccccccccccc", "tn_dddddddddddd"
ACC_C, MAIL_C = "us_cccccccccccc", f"{C}@recette.test"
CX_DSN = os.environ.get("NOKKER_TEST_CX_DSN", "")


class _HashEmbed:
    """Embeddings déterministes (8 dimensions) : l'indexeur de CortHeXis tourne sans serveur d'embeddings."""

    def identity(self) -> str:
        return "test-hash-8"

    def embed_docs(self, texts: list[str]) -> list[list[float]]:
        import hashlib

        return [[1.0 + b for b in hashlib.sha256(t.encode()).digest()[:8]] for t in texts]

    def embed_query(self, text: str, timeout: float = 30.0) -> list[float]:
        return self.embed_docs([text])[0]


def _cx_available() -> bool:
    if not CX_DSN:
        return False
    try:
        import corthexis.indexer  # noqa: F401
        import corthexis.service  # noqa: F401
    except ImportError:
        return False
    return True


def _cx_counts(projects: list[str]) -> dict[str, int]:
    import psycopg

    with psycopg.connect(CX_DSN) as conn:
        out = {t: conn.execute(f"SELECT count(*) FROM {t} WHERE project = ANY(%s)", (projects,)).fetchone()[0]
               for t in ("notes", "note_versions")}
        out["chunks"] = conn.execute("SELECT count(*) FROM chunks c JOIN notes n ON n.id = c.note_id "
                                     "WHERE n.project = ANY(%s)", (projects,)).fetchone()[0]
        return out


def _cx_consistent() -> None:
    """Après effacement, l'index reste cohérent : compteurs de morceaux et nombre de notes de l'IDF."""
    import psycopg

    with psycopg.connect(CX_DSN) as conn:
        for gid, count in conn.execute("SELECT id, chunk_count FROM index_generations").fetchall():
            assert conn.execute("SELECT count(*) FROM chunks WHERE generation_id = %s", (gid,)).fetchone()[0] == count
        notes = conn.execute("SELECT count(*) FROM notes").fetchone()[0]
        df = conn.execute("SELECT df FROM lex_df WHERE token = ''").fetchone()
        assert (df[0] if df else 0) == notes


@pytest.fixture
def session_index(pg, tmp_path, monkeypatch):  # noqa: F811
    import psycopg

    from nokker.context.corthexis_index import CorthexisSemanticIndex, project_of
    from nokker.context.types import Episode

    threads = {C: [f"{C}:th_{uuid.uuid4().hex[:8]}", f"{C}:tg_{uuid.uuid4().hex[:8]}"],
               D: [f"{D}:th_{uuid.uuid4().hex[:8]}"]}
    with psycopg.connect(pg["su"], autocommit=True) as conn:
        conn.execute("INSERT INTO tenants (id) VALUES (%s), (%s) ON CONFLICT DO NOTHING", (C, D))
        cols, vals = _dummy_row(conn, "accounts")
        row = {**dict(zip(cols, vals)), "id": ACC_C, "tenant_id": C, "email": MAIL_C}
        conn.execute(f'INSERT INTO accounts ({", ".join(row)}) VALUES ({", ".join(["%s"] * len(row))}) '
                     "ON CONFLICT DO NOTHING", list(row.values()))
        for tenant, ths in threads.items():
            for i, th in enumerate(ths):
                conn.execute("INSERT INTO threads (thread_id, tenant_id) VALUES (%s, %s)", (th, tenant))
                conn.execute("INSERT INTO turns (thread_id, turn_index, turn, tenant_id) VALUES (%s, 0, '{}', %s)",
                             (th, tenant))
                if i == 0:  # le second fil de C n'a plus d'épisode en base : ses notes restent à effacer
                    conn.execute("INSERT INTO nokker_episodes (id, thread_id, seq, first_turn, tenant_id) "
                                 "VALUES (%s, %s, 0, 0, %s)", (f"ep_{uuid.uuid4().hex[:12]}", th, tenant))
    root = tmp_path / "session-memory"
    cx = _cx_available()
    if cx:
        from corthexis import service as cx_service

        monkeypatch.setenv("CORTHEXIS_DATABASE_URL", CX_DSN)
        cx_service.reset()
        monkeypatch.setattr(cx_service, "embedder", lambda: _HashEmbed())
    else:
        monkeypatch.delenv("CORTHEXIS_DATABASE_URL", raising=False)
    idx = CorthexisSemanticIndex(root, database_url=CX_DSN if cx else "")
    idx._enqueue = lambda project: None  # indexation à la main, ci-dessous, sans fil de fond
    for ths in threads.values():
        for th in ths:
            for n in range(2):
                ep = Episode(id=f"ep_{uuid.uuid4().hex[:12]}", thread_id=th, seq=n, first_turn=n, last_turn=n,
                             title=f"Dossier {n}", summary=f"{MAIL_C} parle du dossier {n}", closed_at=1.0,
                             day="2026-10-25")
                idx.index_episode(ep, f"## Messages de l'utilisateur\n\n[T{n}] écris-moi à {MAIL_C}, dossier {n}")
            if cx:
                idx._index_project(project_of(th))
    yield {"root": root, "threads": threads, "cx": cx,
           "projects": {t: [project_of(th) for th in ths] for t, ths in threads.items()}}
    if cx:
        from corthexis import service as cx_service

        cx_service.reset()


@requires_pg
def test_session_index_erased_with_the_account(pg, session_index, monkeypatch):  # noqa: F811
    import psycopg

    from nokker.admin.erase import erase_account
    from nokker.context.corthexis_erase import FILES_KEY

    root, projects, cx = session_index["root"], session_index["projects"], session_index["cx"]
    settings = {"enabled": True, "root_dir": str(root)}
    if cx:
        settings["database_url_env"] = "NOKKER_TEST_CX_DSN"
        before = _cx_counts(projects[C])
        assert before["notes"] == 4 and before["chunks"] >= 4 and before["note_versions"] >= 4
        assert _cx_counts(projects[D])["notes"] == 2

    dry = erase_account(pg["app"], account_id=ACC_C, session_memory=settings)
    assert not dry.applied and dry.deleted[FILES_KEY] == 4
    assert all((root / p).is_dir() for p in projects[C])
    if cx:
        assert dry.deleted["corthexis: notes"] == 4 and _cx_counts(projects[C]) == before

    done = erase_account(pg["app"], account_id=ACC_C, apply=True, confirm=ACC_C, session_memory=settings)
    assert done.applied and done.deleted[FILES_KEY] == 4
    assert not any((root / p).exists() for p in projects[C])
    assert all(len(list((root / p).glob("ep-*.md"))) == 2 for p in projects[D])
    if cx:
        assert _cx_counts(projects[C]) == {"notes": 0, "note_versions": 0, "chunks": 0}
        assert _cx_counts(projects[D])["notes"] == 2
        _cx_consistent()
    # Ce que l'effacement écrit (compte rendu par table, journal des effacements) : ni l'adresse ni le compte.
    for out in (" ".join(done.deleted), done.journal_line()):
        assert ACC_C not in out and MAIL_C not in out
    with psycopg.connect(pg["su"]) as conn:
        assert conn.execute("SELECT count(*) FROM accounts WHERE id = %s", (ACC_C,)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM threads WHERE tenant_id = %s", (D,)).fetchone()[0] == 1


@requires_pg
def test_session_index_unreachable_cancels_the_whole_erasure(pg, session_index, monkeypatch):  # noqa: F811
    """Base CortHeXis injoignable : rien n'est effacé, ni la base du noyau (rejouable : les fils du locataire servent
    à retrouver ses notes), ni les fichiers ; le refus ne dit ni le DSN, ni le compte, ni l'adresse."""
    import psycopg

    from nokker.admin.erase import EraseError, erase_account

    root, projects = session_index["root"], session_index["projects"]
    monkeypatch.setenv("NOKKER_TEST_CX_DOWN", "postgresql://cx:s3cr3t@127.0.0.1:1/cx?connect_timeout=1")
    settings = {"enabled": True, "root_dir": str(root), "database_url_env": "NOKKER_TEST_CX_DOWN"}
    with pytest.raises(EraseError) as e:
        erase_account(pg["app"], account_id=ACC_C, apply=True, confirm=ACC_C, session_memory=settings)
    assert "s3cr3t" not in str(e.value) and ACC_C not in str(e.value) and MAIL_C not in str(e.value)
    assert all((root / p).is_dir() for p in projects[C])
    with psycopg.connect(pg["su"]) as conn:
        assert conn.execute("SELECT count(*) FROM accounts WHERE id = %s", (ACC_C,)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM turns WHERE tenant_id = %s", (C,)).fetchone()[0] == 2


@requires_pg
def test_erase_pending_clears_the_queue_with_the_full_scope_under_a_lock(pg, session_index, tmp_path):  # noqa: F811
    """La file des suppressions accordées (`nokker erase --pending`) efface le compte avec TOUT ce qui vit hors base
    (`erase_scope`) : images jointes, sessions de code, index de session. Un second passage pendant que le premier
    tient le verrou consultatif ne fait rien ; un passage de plus sur une file vide ne fait rien non plus."""
    import psycopg

    from nokker.admin.erase import PENDING_LOCK_KEY, erase_pending, pending_deletions
    from nokker.images.store import ImageStore
    from nokker.images.validate import RawImage
    from nokker.tenancy.store import PgTenancy

    root, projects, threads = session_index["root"], session_index["projects"], session_index["threads"]
    images = tmp_path / "images"
    ImageStore(images).put(threads[C][0], RawImage(data=b"\x89PNG\r\n\x1a\n" + b"0" * 16, media_type="image/png"))
    ImageStore(images).put(threads[D][0], RawImage(data=b"\x89PNG\r\n\x1a\n" + b"1" * 16, media_type="image/png"))
    workspaces = tmp_path / "workspaces"
    (workspaces / C / ACC_C / "cs_0123456789ab").mkdir(parents=True)
    scope = {"images_root": str(images), "code": {"workspace_root": str(workspaces)},
             "session_memory": {"enabled": True, "root_dir": str(root)}}
    if session_index["cx"]:
        scope["session_memory"]["database_url_env"] = "NOKKER_TEST_CX_DSN"
    queued = len(pending_deletions(pg["app"]))  # la base du module peut en porter d'autres tests
    PgTenancy(pg["app"]).suspend_for_deletion(ACC_C, "ap-pending")
    assert ACC_C in pending_deletions(pg["app"])

    with psycopg.connect(pg["app"], autocommit=True) as other:  # un passage en cours ailleurs
        other.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (PENDING_LOCK_KEY,))
        busy = erase_pending(pg["app"], apply=True, scope=scope)
        assert busy.locked and busy.erased == 0 and ACC_C in pending_deletions(pg["app"])
        other.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (PENDING_LOCK_KEY,))

    dry = erase_pending(pg["app"], apply=False, scope=scope)
    assert not dry.locked and dry.erased == queued + 1 and ACC_C in pending_deletions(pg["app"])
    assert all((root / p).is_dir() for p in projects[C]) and (workspaces / C).is_dir()

    run = erase_pending(pg["app"], apply=True, scope=scope)
    assert (run.erased, run.refused, run.gone) == (queued + 1, 0, 0) and ACC_C not in pending_deletions(pg["app"])
    assert not any((root / p).exists() for p in projects[C]) and all((root / p).is_dir() for p in projects[D])
    assert not (images / C).exists() and (images / D).is_dir() and not (workspaces / C).exists()
    if session_index["cx"]:
        assert _cx_counts(projects[C])["notes"] == 0 and _cx_counts(projects[D])["notes"] == 2
    line = run.journal_line()
    assert ACC_C not in line and MAIL_C not in line and C not in line
    with psycopg.connect(pg["su"]) as conn:
        assert conn.execute("SELECT count(*) FROM accounts WHERE id = %s", (ACC_C,)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM threads WHERE tenant_id = %s", (C,)).fetchone()[0] == 0

    again = erase_pending(pg["app"], apply=True, scope=scope)  # idempotent : file vide, rien à faire
    assert (again.erased, again.refused, again.gone, again.locked) == (0, 0, 0, False)


@requires_pg
def test_erase_pending_journal_is_written_before_the_commit(pg, tmp_path):  # noqa: F811
    """`--journal` sur Postgres : la ligne `{tenant_id, erased_at}` est écrite (et fsync) AVANT la validation ; une
    écriture impossible (/dev/full : disque plein) annule l'effacement de ce compte, qui reste dans la file et en base."""
    import json
    import os
    import stat

    import psycopg

    from nokker.admin.erase import ErasureJournal, erase_pending, pending_deletions
    from nokker.tenancy.store import PgTenancy

    tenancy = PgTenancy(pg["app"])
    acc = tenancy.create_account("journal-moi@recette.test")
    tenancy.suspend_for_deletion(acc.account_id, "ap-journal")

    if os.path.exists("/dev/full"):  # fd direct : `open` refuse le dossier /dev, non inscriptible hors root (CI)
        with ErasureJournal(os.open("/dev/full", os.O_WRONLY | os.O_APPEND)) as full:
            run = erase_pending(pg["app"], apply=True, journal=full)
        assert run.refused >= 1 and acc.account_id in pending_deletions(pg["app"])
        with psycopg.connect(pg["su"]) as conn:
            assert conn.execute("SELECT count(*) FROM accounts WHERE id = %s", (acc.account_id,)).fetchone()[0] == 1

    path = tmp_path / "beta-erasures.jsonl"
    with ErasureJournal.open(str(path)) as journal:
        run = erase_pending(pg["app"], apply=True, journal=journal)
    assert acc.account_id not in pending_deletions(pg["app"]) and tenancy.account(acc.account_id) is None
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert {"tenant_id": acc.tenant_id} in [{"tenant_id": x["tenant_id"]} for x in lines]
    assert all(set(x) == {"tenant_id", "erased_at"} for x in lines) and len(lines) == run.erased
    assert acc.account_id not in path.read_text() and "journal-moi" not in path.read_text()
