"""SSO OIDC des équipes, service (nokker/sso/service.py) contre le faux fournisseur : parcours complet, rejeux,
jetons refusés, e-mail et domaine, groupes → rôles, retrait, SSO obligatoire, propriétaire jamais par SSO, secret
jamais rendu."""
from __future__ import annotations

import json

import pytest

from nokker.team.model import TeamError
from nokker.tenancy.context import tenant_scope
from tests.unit.approvals.conftest import gen_keypair, sign_p1363
from tests.unit.sso import fake_idp as f
from tests.unit.sso.conftest import REDIRECT, make_rig


def err(fn, *a, **kw) -> TeamError:
    with pytest.raises(TeamError) as e:
        fn(*a, **kw)
    return e.value


def audit_events(rig, event=None):
    with tenant_scope(rig.owner.tenant_id):
        rows = rig.sso.audit(rig.owner, rig.cid, {})["events"]
    return [r for r in rows if event is None or r["event"] == event]


# ─────────────────────────── chemin heureux ───────────────────────────


def test_happy_path_creates_account_device_and_membership(rig):
    out = rig.login()
    acc = rig.tenancy.account_by_email("alice@example.com")
    assert acc is not None and out["user"] == acc.account_id
    assert acc.tenant_id != rig.owner.tenant_id  # le membre reste dans SON locataire
    dev = rig.approvals.get_device(out["device_id"])
    assert dev.status == "approved" and dev.approved_via == f"sso:{rig.cid}"
    assert rig.role_of(acc.account_id) == "member"
    assert out["projects"] == [{"id": rig.pid, "role": "member"}]
    # l'appareil ouvre une session comme tout appareil (défi signé), mais ne décide jamais une carte forte
    from nokker.approvals.base import _require_deciding_device
    from nokker.approvals.errors import StrongDeviceRequired

    with pytest.raises(StrongDeviceRequired):
        _require_deciding_device(dev)
    ev = [e["event"] for e in audit_events(rig)]
    assert "login_ok" in ev and "member_added" in ev


def test_start_sends_pkce_s256_nonce_state_and_redirect(rig):
    _, grant = rig.begin()
    assert grant["code_challenge_method"] == "S256" and len(grant["code_challenge"]) == 43
    assert grant["response_type"] == "code" and grant["scope"].split() == ["openid", "email", "profile"]
    assert grant["redirect_uri"] == REDIRECT and grant["client_id"] == f.CLIENT_ID
    assert len(grant["state"]) >= 40 and len(grant["nonce"]) >= 40


def test_start_by_email_domain_finds_the_connection(rig):
    rig.idp.next_claims = {"now": rig.clock.t}
    priv, grant = rig.begin(email="alice@example.com")
    assert grant["login_hint"] == "alice@example.com"
    assert rig.finish(priv, grant)["user"].startswith("us_")


def test_unknown_connection_and_unknown_domain_answer_the_same(rig):
    _, spki = gen_keypair()
    a = err(rig.sso.start, {"connection": "sc_0000000000000000", "public_key_spki": spki}, "ip")
    b = err(rig.sso.start, {"email": "bob@unknown.example.net", "public_key_spki": spki}, "ip")
    c = err(rig.sso.start, {"connection": "nope", "public_key_spki": spki}, "ip")
    assert (a.status, a.code) == (b.status, b.code) == (c.status, c.code) == (404, "unknown_connection")


def test_existing_account_with_same_email_is_linked_not_duplicated(rig):
    existing = rig.tenancy.create_account("alice@example.com")
    out = rig.login()
    assert out["user"] == existing.account_id
    out2 = rig.login()  # la seconde fois, par l'identité (issuer, sub)
    assert out2["user"] == existing.account_id


def test_account_already_linked_to_another_subject_is_refused(rig):
    rig.login(sub="u-1001")
    e = err(rig.login, sub="u-other")  # même e-mail, autre personne chez le fournisseur
    assert (e.status, e.code) == (409, "account_conflict")


# ─────────────────────────── rejeux, liaison au navigateur ───────────────────────────


def test_state_is_single_use(rig):
    rig.idp.next_claims = {"now": rig.clock.t}
    priv, grant = rig.begin()
    rig.finish(priv, grant)
    rig.idp.codes["code-1"] = {"nonce": grant["nonce"], "challenge": grant["code_challenge"],
                               "redirect_uri": REDIRECT}
    e = err(rig.finish, priv, grant)
    assert (e.status, e.code) == (401, "invalid_state")


def test_state_expires_after_ten_minutes(rig):
    rig.idp.next_claims = {"now": rig.clock.t}
    priv, grant = rig.begin()
    rig.clock.t += 601
    e = err(rig.finish, priv, grant)
    assert (e.status, e.code) == (401, "invalid_state")


def test_callback_must_be_signed_by_the_browser_that_started(rig):
    rig.idp.next_claims = {"now": rig.clock.t}
    priv, grant = rig.begin()
    other, _ = gen_keypair()
    e = err(rig.finish, priv, grant, sign_with=other)
    assert (e.status, e.code) == (401, "binding_invalid")
    assert rig.tenancy.account_by_email("alice@example.com") is None


def test_nonce_of_another_flow_is_refused(rig):
    rig.idp.next_claims = {"now": rig.clock.t}
    priv, grant = rig.begin()
    old_token = f.sign(f.claims(grant["nonce"], now=rig.clock.t))
    rig.finish(priv, grant)
    rig.idp.next_token = old_token  # le jeton d'une connexion passée, rejoué dans une nouvelle
    priv2, grant2 = rig.begin()
    e = err(rig.finish, priv2, grant2)
    assert (e.status, e.code) == (401, "token_invalid")
    assert audit_events(rig, "login_refused")[-1]["detail"]["reason"] == "bad_nonce"


def test_idp_error_consumes_the_state(rig):
    priv, grant = rig.begin()
    e = err(rig.sso.callback, {"state": grant["state"], "error": "access_denied",
                               "signature": sign_p1363(priv, f"{grant['state']}.oidc".encode())}, "ip")
    assert (e.status, e.code) == (401, "idp_error")
    e = err(rig.finish, priv, grant)
    assert e.code == "invalid_state"


# ─────────────────────────── jetons refusés ───────────────────────────


@pytest.mark.parametrize("over, reason", [
    ({"aud": "someone-else"}, "bad_aud"),
    ({"iss": "https://evil.example.com"}, "bad_iss"),
    ({"exp_shift": -400}, "expired"),
])
def test_bad_claims_refused_and_nothing_created(rig, over, reason):
    if "exp_shift" in over:
        rig.idp.next_claims = {"now": rig.clock.t + over["exp_shift"]}
    else:
        rig.idp.next_claims = {"now": rig.clock.t, **over}
    priv, grant = rig.begin()
    e = err(rig.finish, priv, grant)
    assert (e.status, e.code) == (401, "token_invalid")
    assert rig.tenancy.account_by_email("alice@example.com") is None
    assert audit_events(rig, "login_refused")[-1]["detail"]["reason"] == reason


def test_alg_none_refused(rig):
    priv, grant = rig.begin()
    rig.idp.next_token = f.sign(f.claims(grant["nonce"], now=rig.clock.t), alg="none", kid=None)
    e = err(rig.finish, priv, grant)
    assert e.code == "token_invalid"
    assert audit_events(rig, "login_refused")[-1]["detail"]["reason"] == "bad_alg"


def test_forged_signature_refused(rig):
    priv, grant = rig.begin()
    rig.idp.next_token = f.sign(f.claims(grant["nonce"], now=rig.clock.t), key=f.keys()["rsa2"])
    e = err(rig.finish, priv, grant)
    assert e.code == "token_invalid"
    assert audit_events(rig, "login_refused")[-1]["detail"]["reason"] == "bad_signature"


def test_jwks_rotation_is_followed(rig):
    rig.login()
    rig.idp.jwks_keys = [f.ec_jwk(f.keys()["ec"], "e2")]
    rig.idp.sign_alg, rig.idp.sign_kid = "ES256", "e2"
    rig.clock.t += 60
    assert rig.login()["user"].startswith("us_")


# ─────────────────────────── e-mail et domaine ───────────────────────────


@pytest.mark.parametrize("verified", [False, "false", ...])
def test_unverified_email_refused(rig, verified):
    e = err(rig.login, email_verified=verified)
    assert (e.status, e.code) == (403, "email_unverified")
    assert rig.tenancy.account_by_email("alice@example.com") is None


def test_email_outside_verified_domains_refused(rig):
    e = err(rig.login, email="mallory@other.example.net")
    assert (e.status, e.code) == (403, "domain_refused")
    e = err(rig.login, email="mallory@sub.example.com")  # sous-domaine : pas le même domaine
    assert e.code == "domain_refused"


def test_unproved_domain_does_not_open_the_connection(rig):
    rig.as_owner(rig.sso.add_domain, rig.cid, {"domain": "example.org"})  # TXT jamais posé
    e = err(rig.as_owner, rig.sso.verify_domain, rig.cid, "example.org")
    assert (e.status, e.code) == (409, "not_verified")
    e = err(rig.login, email="bob@example.org")
    assert e.code == "domain_refused"


def test_a_domain_belongs_to_one_connection(rig):
    other = rig.tenancy.create_account("other-owner@example.net")
    with tenant_scope(other.tenant_id):
        c = rig.sso.create_connection(other, {"name": "Evil", "issuer": "https://evil.example.com",
                                              "client_id": "x", "client_secret_name": "evil"})
        d = rig.sso.add_domain(other, c["id"], {"domain": "example.com"})
        rig.dns[d["txt"]["name"]] = rig.dns.get(d["txt"]["name"], []) + [d["txt"]["value"]]
        e = err(rig.sso.verify_domain, other, c["id"], "example.com")
    assert (e.status, e.code) == (409, "domain_claimed")


# ─────────────────────────── groupes → rôles ───────────────────────────


def test_highest_group_role_wins(rig):
    out = rig.login(groups=["eng", "leads"])
    assert rig.role_of(out["user"]) == "maintainer"


def test_role_follows_groups_at_each_login(rig):
    uid = rig.login(groups=["auditors"])["user"]
    assert rig.role_of(uid) == "viewer"
    rig.login(groups=["leads"])
    assert rig.role_of(uid) == "maintainer"
    ev = audit_events(rig, "role_changed")[-1]
    assert ev["detail"] == {"from": "viewer", "to": "maintainer"} and ev["project_id"] == rig.pid


def test_group_removed_means_role_removed_at_next_login(rig):
    uid = rig.login(groups=["eng"])["user"]
    out = rig.login(groups=["marketing"])
    assert rig.role_of(uid) is None and out["projects"] == [{"id": rig.pid, "role": None}]
    assert audit_events(rig, "role_removed")[-1]["detail"] == {"from": "member"}


def test_groups_claim_as_a_single_string_and_custom_claim_name(rig):
    rig.as_owner(rig.sso.patch_connection, rig.cid, {"groups_claim": "roles"})
    uid = rig.login(groups=[], roles="leads")["user"]
    assert rig.role_of(uid) == "maintainer"


def test_invited_member_without_group_is_left_alone(rig):
    bob = rig.tenancy.create_account("bob@example.com")
    inv = rig.as_owner(rig.team.invite, rig.pid, {"email": "bob@example.com", "role": "maintainer"})
    with tenant_scope(bob.tenant_id):
        rig.team.accept(bob, inv["id"])
    rig.login(email="bob@example.com", sub="u-bob", groups=["marketing"])
    assert rig.role_of(bob.account_id) == "maintainer"  # pas tenu par le SSO : intact
    rig.login(email="bob@example.com", sub="u-bob", groups=["eng"])
    assert rig.role_of(bob.account_id) == "member"  # un groupe lui donne un rôle : désormais tenu par le SSO
    rig.login(email="bob@example.com", sub="u-bob", groups=[])
    assert rig.role_of(bob.account_id) is None


# ─────────────────────────── propriétaire jamais par SSO ───────────────────────────


def test_owner_role_cannot_be_mapped(rig):
    e = err(rig.as_owner, rig.sso.put_binding, rig.pid,
            {"connection": rig.cid, "mappings": [{"group": "admins", "role": "owner"}]})
    assert (e.status, e.code) == (400, "owner_not_mappable")


def test_owner_is_never_demoted_or_removed_by_sso(rig):
    rig.login(email="owner@example.com", sub="u-owner", groups=["auditors"])
    assert rig.role_of(rig.owner.account_id) == "owner"
    rig.login(email="owner@example.com", sub="u-owner", groups=[])
    assert rig.role_of(rig.owner.account_id) == "owner"


def test_no_group_makes_anyone_owner(rig):
    uid = rig.login(groups=["eng", "leads", "auditors", "owner", "admins"])["user"]
    assert rig.role_of(uid) == "maintainer"
    owners = [m for (p, _), m in rig.team.store.access.items() if p == rig.pid and m.role == "owner"]
    assert [m.account_id for m in owners] == [rig.owner.account_id]


# ─────────────────────────── SSO obligatoire ───────────────────────────


def _session_actor(rig, account_id, device_id):
    from nokker.sso.model import SessionActor

    dev = rig.approvals._load_device(device_id)
    return SessionActor.of(rig.tenancy.account(account_id), dev.approved_via, dev.approved_at)


def test_required_sso_refuses_other_methods_on_team_resources(rig):
    out = rig.login()
    uid = out["user"]
    rig.as_owner(rig.sso.put_binding, rig.pid, {"connection": rig.cid, "required": True,
                                                "mappings": [{"group": "eng", "role": "member"}]})
    acc = rig.tenancy.account(uid)
    sso_actor = _session_actor(rig, uid, out["device_id"])
    from nokker.sso.model import SessionActor

    code_actor = SessionActor.of(acc, "email-code", rig.clock.t)
    with tenant_scope(acc.tenant_id):
        assert rig.team.get(sso_actor, rig.pid)["role"] == "member"
        e = err(rig.team.get, code_actor, rig.pid)
        assert (e.status, e.code) == (403, "sso_required")
        e = err(rig.team.rules, code_actor, rig.pid)
        assert e.code == "sso_required"
        listed = [{k: v for k, v in x.items() if k != "created_at"}
                  for x in rig.team.list_projects(code_actor)["projects"]]
        assert listed == [{"id": rig.pid, "name": "Plateforme", "role": "member",
                           "sso": {"required": True, "connection": rig.cid}}]
        # une connexion SSO trop ancienne ne suffit plus
        rig.clock.t += 12 * 3600 + 1
        e = err(rig.team.get, sso_actor, rig.pid)
        assert e.code == "sso_required"


def test_required_sso_machine_view_needs_a_recent_sso_login(rig):
    uid = rig.login()["user"]
    rig.as_owner(rig.sso.put_binding, rig.pid, {"connection": rig.cid, "required": True,
                                                "mappings": [{"group": "eng", "role": "member"}]})
    acc = rig.tenancy.account(uid)
    with tenant_scope(acc.tenant_id):
        assert [p["id"] for p in rig.team.machine_projects(acc)["projects"]] == [rig.pid]
        rig.clock.t += 12 * 3600 + 1
        assert rig.team.machine_projects(acc)["projects"] == []


def test_required_sso_never_locks_the_owner_out(rig):
    rig.as_owner(rig.sso.put_binding, rig.pid, {"connection": rig.cid, "required": True, "mappings": []})
    assert rig.as_owner(rig.team.get, rig.pid)["role"] == "owner"
    assert rig.as_owner(rig.sso.get_binding, rig.pid)["required"] is True


def test_not_required_changes_nothing_for_other_methods(rig):
    uid = rig.login()["user"]
    from nokker.sso.model import SessionActor

    acc = rig.tenancy.account(uid)
    with tenant_scope(acc.tenant_id):
        assert rig.team.get(SessionActor.of(acc, "email-code", rig.clock.t), rig.pid)["role"] == "member"
        assert rig.team.get(acc, rig.pid)["role"] == "member"


# ─────────────────────────── configuration et secret ───────────────────────────


def test_secret_value_never_accepted_inline_nor_returned(rig):
    e = err(rig.as_owner, rig.sso.create_connection,
            {"name": "x", "issuer": f.ISSUER, "client_id": "c", "client_secret_name": "n", "client_secret": "v"})
    assert e.code == "secret_value_refused"
    got = rig.as_owner(rig.sso.get_connection, rig.cid)
    assert got["client_secret"] == {"name": "acme-oidc", "set": True}
    rig.login()
    rig.login(groups=[])
    err(rig.login, email_verified=False)
    rig.as_owner(rig.sso.audit, rig.cid, {})
    rig.as_owner(rig.sso.list_connections)
    blob = json.dumps(rig.responses, default=str)
    assert f.CLIENT_SECRET not in blob
    for t in rig.idp.token_posts:  # ni code, ni jeton, ni secret au journal
        assert t["data"]["code"] not in json.dumps(audit_events(rig))
    assert "at-xyz" not in json.dumps(audit_events(rig)) and f.CLIENT_SECRET not in json.dumps(audit_events(rig))


def test_secret_not_set_refuses_start(rig):
    with tenant_scope(rig.owner.tenant_id):
        rig.sso.secrets.delete(rig.owner.tenant_id, "acme-oidc")
    _, spki = gen_keypair()
    e = err(rig.sso.start, {"connection": rig.cid, "public_key_spki": spki}, "ip")
    assert (e.status, e.code) == (503, "not_configured")


def test_connection_is_invisible_to_others(rig):
    other = rig.tenancy.create_account("x@example.net")
    with tenant_scope(other.tenant_id):
        assert rig.sso.list_connections(other) == {"connections": []}
        e = err(rig.sso.get_connection, other, rig.cid)
        assert (e.status, e.code) == (404, "not_found")
        e = err(rig.sso.set_client_secret, other, rig.cid, {"value": "x"})
        assert e.code == "not_found"


def test_binding_needs_project_owner_and_connection_owner(rig):
    uid = rig.login(groups=["leads"])["user"]
    maint = rig.tenancy.account(uid)
    with tenant_scope(maint.tenant_id):
        e = err(rig.sso.put_binding, maint, rig.pid, {"connection": rig.cid, "mappings": []})
        assert (e.status, e.code) == (403, "forbidden")
        c = rig.sso.create_connection(maint, {"name": "Mine", "issuer": f.ISSUER, "client_id": "c2",
                                              "client_secret_name": "m"})
    other_pid = rig.as_owner(rig.team.create, {"name": "Autre"})["id"]
    e = err(rig.as_owner, rig.sso.put_binding, other_pid, {"connection": c["id"], "mappings": []})
    assert (e.status, e.code) == (404, "not_found")


def test_binding_of_a_project_is_readable_by_its_owner_only(rig):
    uid = rig.login()["user"]
    acc = rig.tenancy.account(uid)
    with tenant_scope(acc.tenant_id):
        e = err(rig.sso.get_binding, acc, rig.pid)
        assert e.code == "forbidden"


def test_delete_connection_unbinds_projects_and_frees_domains(rig):
    rig.as_owner(rig.sso.delete_connection, rig.cid)
    assert rig.as_owner(rig.sso.list_connections) == {"connections": []}
    e = err(rig.as_owner, rig.sso.get_binding, rig.pid)
    assert e.code == "not_found"
    _, spki = gen_keypair()
    assert err(rig.sso.start, {"email": "a@example.com", "public_key_spki": spki}, "ip").code == "unknown_connection"


def test_config_changes_are_audited(rig):
    ev = [e["event"] for e in audit_events(rig)]
    for name in ("connection_created", "client_secret_set", "domain_added", "domain_verified", "binding_set"):
        assert name in ev


def test_flag_off_rig_is_independent():
    a, b = make_rig(), make_rig()
    assert a.cid != b.cid
