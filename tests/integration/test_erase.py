"""Effacement d'un compte et retrait d'un appareil (nokker/admin/erase.py) sur un vrai Postgres, avec le rôle du pod
(`nokker_app`, sous RLS). Ignoré sans `NOKKER_TEST_PG_DSN` (voir test_rls.py, dont on reprend la base jetable).

Prouve : chaque table qui porte `tenant_id`, `account_id` ou `"user"` reçoit une ligne de A et une de B ; à blanc, le
compte rendu compte les lignes de A et rien ne bouge ; validé, il ne reste rien de A et tout de B ; le locataire
interne et une confirmation fausse sont refusés ; le retrait d'un appareil ne touche que lui."""
from __future__ import annotations

import uuid

import pytest

from tests.integration.test_rls import A, B, pg, requires_pg  # noqa: F401  (pg : fixture)

ACC = {A: "us_aaaaaaaaaaaa", B: "us_bbbbbbbbbbbb"}
CHECKED = {"data_consents": {"scope": "training", "source": "signup"}, "connector_credentials": {"kind": "mail"}}


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
