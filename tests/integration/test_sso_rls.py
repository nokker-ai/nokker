"""SSO OIDC des équipes en base (migration 032) : vrai Postgres jetable, service connecté en `nokker_app` (ni
propriétaire, ni BYPASSRLS), faux fournisseur OIDC en mémoire. Ignoré sans `NOKKER_TEST_PG_DSN`.

Prouve : le parcours complet tient sous RLS (le membre reste dans SON locataire, l'adhésion est écrite dans celui du
projet) ; un autre locataire ne lit rien des connexions, domaines, liens, adhésions tenues, journal ; un membre lit le
lien de SON projet sans pouvoir l'écrire ; la base refuse un `owner` qui ne soit pas le créateur et une correspondance
vers `owner` ; un `state` ne se consomme qu'une fois ; l'effacement d'un compte emporte ses lignes SSO."""
from __future__ import annotations

import pytest

from tests.integration.test_rls import pg, pod_env, requires_pg  # noqa: F401 — fixtures (base neuve, rôles, migrations)

pytestmark = pytest.mark.integration


def _raw(dsn, tenant, sql, params=(), *, account=None):
    import psycopg

    with psycopg.connect(dsn) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
        if account is not None:
            conn.execute("SELECT set_config('app.account_id', %s, false), set_config('app.account_email', %s, false)",
                         (account.account_id, account.email))
        cur = conn.execute(sql, params)
        rows = cur.fetchall() if cur.description else cur.rowcount
        conn.commit()
        return rows


def _refused(dsn, tenant, sql, params=(), *, account=None):
    import psycopg

    try:
        n = _raw(dsn, tenant, sql, params, account=account)
    except psycopg.Error:
        return True
    return n == 0


@pytest.fixture(scope="module")
def world(pg):  # noqa: F811
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.sso.idp import Idp
    from nokker.sso.secrets import MemorySsoSecrets
    from nokker.sso.service import Sso
    from nokker.sso.store import PgSsoStore
    from nokker.team.service import Team
    from nokker.team.store import PgTeamStore
    from nokker.tenancy.context import tenant_scope
    from nokker.tenancy.resolve import account_tenant_fn
    from nokker.tenancy.store import PgTenancy
    from tests.unit.sso import fake_idp as f

    tenancy = PgTenancy(pg["app"])
    owner = tenancy.create_account("owner@example.com")
    outsider = tenancy.create_account("out@example.net")
    approvals = InMemoryApprovals(account_tenant=account_tenant_fn(tenancy))
    team = Team(store=PgTeamStore(pg["app"], migrate_now=False), tenancy=tenancy)
    idp = f.FakeIdp()
    dns: dict[str, list[str]] = {}
    sso = Sso(store=PgSsoStore(pg["app"], migrate_now=False), team=team, tenancy=tenancy, approvals=approvals,
              secrets=MemorySsoSecrets(), idp=Idp(http=idp), redirect_uri="https://app.example.com/sso/callback",
              resolve_txt=lambda n: dns.get(n, []))
    team.sso_gate = sso.gate_reason
    with tenant_scope(owner.tenant_id):
        pid = team.create(owner, {"name": "Plateforme"})["id"]
        cid = sso.create_connection(owner, {"name": "Acme", "issuer": f.ISSUER, "client_id": f.CLIENT_ID,
                                            "client_secret_name": "acme"})["id"]
        sso.set_client_secret(owner, cid, {"value": f.CLIENT_SECRET})
        d = sso.add_domain(owner, cid, {"domain": "example.com"})
        dns[d["txt"]["name"]] = [d["txt"]["value"]]
        sso.verify_domain(owner, cid, "example.com")
        sso.put_binding(owner, pid, {"connection": cid, "required": True,
                                     "mappings": [{"group": "eng", "role": "member"}]})
    return {"pg": pg, "sso": sso, "team": team, "tenancy": tenancy, "idp": idp, "owner": owner,
            "outsider": outsider, "pid": pid, "cid": cid}


def _login(w, email="alice@example.com", sub="u-1001", groups=("eng",)):
    from tests.unit.approvals.conftest import gen_keypair, sign_p1363

    priv, spki = gen_keypair()
    out = w["sso"].start({"connection": w["cid"], "public_key_spki": spki}, "ip")
    grant = w["idp"].authorize(out["authorize_url"])
    w["idp"].next_claims = {"email": email, "sub": sub, "groups": list(groups)}
    body = {"state": grant["state"], "code": grant["code"],
            "signature": sign_p1363(priv, f"{grant['state']}.oidc".encode())}
    return w["sso"].callback(body, "ip"), body


def _role(w, account_id):
    rows = _raw(w["pg"]["app"], w["owner"].tenant_id,
                "SELECT role FROM team_access WHERE project_id = %s AND account_id = %s", (w["pid"], account_id))
    return rows[0][0] if rows else None


@requires_pg
def test_full_flow_under_rls_and_single_use_state(world):
    out, body = _login(world)
    acc = world["tenancy"].account(out["user"])
    assert acc.tenant_id != world["owner"].tenant_id
    assert _role(world, acc.account_id) == "member"
    # le membre ne voit le projet que par l'équipe, sans entrer dans le locataire du propriétaire
    rows = _raw(world["pg"]["app"], acc.tenant_id, "SELECT tenant_id FROM sso_managed")
    assert rows == []  # sans compte posé : rien ; avec : seulement ses propres lignes
    rows = _raw(world["pg"]["app"], acc.tenant_id, "SELECT account_id FROM sso_managed", account=acc)
    assert rows == [(acc.account_id,)]
    from nokker.team.model import TeamError

    with pytest.raises(TeamError) as e:
        world["sso"].callback(body, "ip")
    assert e.value.code == "invalid_state"
    n = _raw(world["pg"]["app"], world["owner"].tenant_id, "SELECT count(*) FROM sso_flows")[0][0]
    assert n == 0


@requires_pg
def test_group_removed_removes_role_in_db(world):
    uid = _login(world, email="bob@example.com", sub="u-bob")[0]["user"]
    assert _role(world, uid) == "member"
    _login(world, email="bob@example.com", sub="u-bob", groups=())
    assert _role(world, uid) is None
    events = [r[0] for r in _raw(world["pg"]["app"], world["owner"].tenant_id,
                                 "SELECT event FROM sso_audit WHERE actor = %s ORDER BY id", (uid,))]
    assert events[-2:] == ["role_removed", "login_ok"]


@requires_pg
def test_other_tenant_reads_nothing(world):
    out = world["outsider"]
    for t in ("sso_connections", "sso_domains", "sso_bindings", "sso_managed", "sso_audit"):
        assert _raw(world["pg"]["app"], out.tenant_id, f"SELECT count(*) FROM {t}", account=out)[0][0] == 0, t
    assert _refused(world["pg"]["app"], out.tenant_id,
                    "UPDATE sso_connections SET client_id = 'x'", account=out)
    assert _refused(world["pg"]["app"], out.tenant_id,
                    "INSERT INTO sso_bindings (project_id, tenant_id, connection_id, updated_at) "
                    "VALUES (%s, %s, %s, 1)", (world["pid"], world["owner"].tenant_id, world["cid"]), account=out)


@requires_pg
def test_member_reads_binding_of_his_project_and_writes_nothing(world):
    uid = _login(world, email="carol@example.com", sub="u-carol")[0]["user"]
    acc = world["tenancy"].account(uid)
    app = world["pg"]["app"]
    assert _raw(app, acc.tenant_id, "SELECT project_id FROM sso_bindings", account=acc) == [(world["pid"],)]
    assert _raw(app, acc.tenant_id, "SELECT count(*) FROM sso_connections", account=acc)[0][0] == 0
    assert _raw(app, acc.tenant_id, "SELECT count(*) FROM sso_audit", account=acc)[0][0] == 0
    assert _refused(app, acc.tenant_id, "UPDATE sso_bindings SET required = false", account=acc)
    assert _refused(app, acc.tenant_id, "DELETE FROM sso_bindings", account=acc)
    # le gate du service lit le lien par la même politique : SSO obligatoire, connexion récente → permis
    from nokker.tenancy.context import tenant_scope

    with tenant_scope(acc.tenant_id):
        assert [p["id"] for p in world["team"].machine_projects(acc)["projects"]] == [world["pid"]]


@requires_pg
def test_db_refuses_an_owner_that_is_not_the_creator(world):
    uid = _login(world, email="dave@example.com", sub="u-dave")[0]["user"]
    app, tn = world["pg"]["app"], world["owner"].tenant_id
    assert _refused(app, tn, "UPDATE team_access SET role = 'owner' WHERE project_id = %s AND account_id = %s",
                    (world["pid"], uid))
    assert _refused(app, tn, "INSERT INTO team_members (project_id, tenant_id, account_id, role, added_at) "
                             "VALUES (%s, %s, 'us_intruder0001', 'owner', 1)", (world["pid"], tn))
    assert _refused(app, tn, "UPDATE sso_bindings SET mappings = '[{\"group\": \"g\", \"role\": \"owner\"}]'::jsonb")
    assert _role(world, world["owner"].account_id) == "owner"


@requires_pg
def test_erase_takes_sso_rows_of_the_account(world, pod_env):  # noqa: F811
    from nokker.admin.erase import erase_account

    uid = _login(world, email="erin@example.com", sub="u-erin")[0]["user"]
    rep = erase_account(world["pg"]["app"], account_id=uid, apply=True, confirm=uid)
    assert rep.unscoped == []
    app, tn = world["pg"]["app"], world["owner"].tenant_id
    assert _raw(app, tn, "SELECT count(*) FROM sso_managed WHERE account_id = %s", (uid,))[0][0] == 0
    import psycopg

    with psycopg.connect(app) as conn:
        assert conn.execute("SELECT count(*) FROM sso_identities WHERE account_id = %s", (uid,)).fetchone()[0] == 0
