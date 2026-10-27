"""`python -m nokker household minors allow|revoke|list` (docs/LOKKAL.md §7.29.4) : à blanc par défaut, audit en ajout
seul, aucune sortie avec l'adresse ni l'id du compte, un retrait suspend sans rien supprimer."""
from __future__ import annotations

import json

import pytest

from nokker.household import grants as gr
from nokker.household.model import Settings, consent_text
from nokker.household.service import Household
from nokker.household.store import MemoryHouseholdStore
from nokker.team.model import TeamError
from nokker.team.service import Team
from nokker.tenancy.context import tenant_scope
from nokker.tenancy.store import MemoryTenancy


@pytest.fixture
def op(monkeypatch):
    tenancy = MemoryTenancy()
    pay, kid, other = (tenancy.create_account(f"{n}@example.com") for n in ("pay", "kid", "other"))
    store = MemoryHouseholdStore()
    hh = Household(team=Team(store=store, tenancy=tenancy), settings=Settings(minors=True, minors_mode="allowlist"))

    def lookup(email, account_id):
        for a in (pay, kid, other):
            if a.email == email or a.account_id == account_id:
                return a.account_id, a.tenant_id
        return None

    service = gr.MinorGrants(store=store, lookup=lookup, clock=lambda: 1000.0)
    monkeypatch.setattr(gr, "open_operator", lambda config: service)

    def run(*argv):
        from nokker.cli import main

        return main(["household", "minors", *argv])

    return run, hh, store, (pay, kid, other)


def attest():
    return {"version": consent_text("fr").sha256, "lang": "fr", "legal_guardian": True}


def test_allow_is_dry_by_default_then_applies_with_an_audit_line(op, capsys):
    run, hh, store, (pay, kid, _) = op
    assert run("allow", "--email", "pay@example.com") == 0
    out = capsys.readouterr().out
    assert "BLANC" in out and store.grant_rows == {} and store.grant_audit_rows == []
    assert run("allow", "--email", "pay@example.com", "--apply") == 0
    out = capsys.readouterr().out
    audit = json.loads(next(ln for ln in out.splitlines() if ln.startswith("{")))
    code = store.grant_rows[pay.account_id]["code"]
    assert audit == {"event": "household_minors_allow", "code": code, "at": "1970-01-01T00:16:40Z"}
    assert code.startswith("mg-") and f"AUTORISÉ — foyer {code}" in out
    assert [r["action"] for r in store.grant_audit_rows] == ["allow"]
    # idempotent : rien de plus à l'audit
    assert run("allow", "--account", pay.account_id, "--apply") == 0
    assert "RIEN À FAIRE" in capsys.readouterr().out and len(store.grant_audit_rows) == 1
    # le payeur autorisé peut maintenant inviter un mineur
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
        assert hh.invite(pay, {"email": kid.email, "profile": "teen", "attestation": attest()})["profile"] == "teen"


def test_no_output_ever_carries_the_address_or_the_account_id(op, capsys):
    run, _, _, (pay, *_) = op
    run("allow", "--email", "pay@example.com")
    run("allow", "--email", "pay@example.com", "--apply")
    run("list")
    run("list", "--email", "pay@example.com")
    run("revoke", "--account", pay.account_id)
    run("revoke", "--account", pay.account_id, "--apply")
    run("allow", "--email", "nobody@example.com")
    cap = capsys.readouterr()
    for text in (cap.out, cap.err):
        assert "pay@example.com" not in text and pay.account_id not in text and "nobody@example.com" not in text
        assert pay.tenant_id not in text


def test_selector_is_required_and_exclusive_and_unknown_account_is_refused(op, capsys):
    run, *_ = op
    for argv in (["allow"], ["revoke", "--apply"], ["allow", "--email", "a@example.com", "--account", "us_x"]):
        with pytest.raises(SystemExit) as e:
            run(*argv)
        assert e.value.code == 2
    capsys.readouterr()
    assert run("allow", "--email", "nobody@example.com", "--apply") == 1
    assert "aucun compte" in capsys.readouterr().err


def test_revoke_suspends_declared_minors_without_deleting_anything(op, capsys):
    run, hh, store, (pay, kid, _) = op
    run("allow", "--email", "pay@example.com", "--apply")
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {})
        iv = hh.invite(pay, {"email": kid.email, "profile": "child", "attestation": attest()})
    with tenant_scope(kid.tenant_id):
        hh.accept(kid, iv["id"])
    assert hh.minor_state(kid.account_id).active is True
    capsys.readouterr()
    assert run("revoke", "--email", "pay@example.com") == 0  # à blanc
    out = capsys.readouterr().out
    assert "BLANC" in out and "1 profil" in out
    assert store.grant_rows[pay.account_id]["revoked_at"] is None
    assert hh.minor_state(kid.account_id).active is True
    assert run("revoke", "--email", "pay@example.com", "--apply") == 0
    out = capsys.readouterr().out
    audit = json.loads(next(ln for ln in out.splitlines() if ln.startswith("{")))
    assert audit["event"] == "household_minors_revoke" and audit["suspended"] == 1
    # suspendu, rien de supprimé : profil, adhésion, attestation sont gardés
    st = hh.minor_state(kid.account_id)
    assert st is not None and st.active is False
    assert store.minor(kid.account_id) is not None and len(store.consent_rows) == 1
    assert {r.status for r in store.hh_rows.values() if r.minor} == {"suspended"}
    assert [r["action"] for r in store.grant_audit_rows] == ["allow", "revoke"]
    assert store.grant_audit_rows[-1]["suspended"] == 1
    # un second retrait ne fait rien ; renouveler sans autorisation est refusé
    assert run("revoke", "--email", "pay@example.com", "--apply") == 0
    assert "RIEN À FAIRE" in capsys.readouterr().out and len(store.grant_audit_rows) == 2
    with tenant_scope(pay.tenant_id), pytest.raises(TeamError) as e:
        hh.renew_consent(pay, kid.account_id, {"attestation": attest()})
    assert e.value.code == "minors_not_enabled"


def test_allow_again_after_revoke_restores_the_grant_but_not_the_suspended_profiles(op, capsys):
    run, hh, store, (pay, kid, _) = op
    run("allow", "--email", "pay@example.com", "--apply")
    code = store.grant_rows[pay.account_id]["code"]
    run("revoke", "--email", "pay@example.com", "--apply")
    capsys.readouterr()
    assert run("allow", "--email", "pay@example.com", "--apply") == 0
    assert "de nouveau" in capsys.readouterr().out
    assert store.grant_rows[pay.account_id]["code"] == code  # même code
    assert hh.minors_open(pay.account_id) is True
    assert [r["action"] for r in store.grant_audit_rows] == ["allow", "revoke", "allow"]


def test_list_shows_code_state_dates_and_counts_only(op, capsys):
    run, hh, store, (pay, kid, other) = op
    run("allow", "--email", "pay@example.com", "--apply")
    run("allow", "--email", "other@example.com", "--apply")
    run("revoke", "--email", "other@example.com", "--apply")
    capsys.readouterr()
    assert run("list") == 0
    cap = capsys.readouterr()
    lines = cap.out.strip().splitlines()
    assert lines[0].split("\t") == ["foyer", "état", "autorisé le", "retiré le", "profils mineurs"]
    states = sorted(ln.split("\t")[1] for ln in lines[1:])
    assert states == ["autorisée", "retirée"] and "[2 foyer(s)]" in cap.err
    assert run("list", "--email", "pay@example.com") == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 2
