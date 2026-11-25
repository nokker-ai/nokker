"""Prénom du membre (migration 072) sur un vrai Postgres, connexion `nokker_app`. Ignoré sans `NOKKER_TEST_PG_DSN`.
Saisi à l'invitation au foyer (`team_invites.first_name`), recopié dans le compte de l'invité à l'acceptation
(`accounts.first_name`, écrit par la clé du compte), relu par le foyer des connecteurs ; modifiable puis effaçable."""
from __future__ import annotations

import pytest

from tests.integration.test_rls import pg, requires_pg  # noqa: F401 — fixtures (base neuve, rôles, migrations)

pytestmark = pytest.mark.integration


@requires_pg
def test_pg_first_name_from_invitation_to_profile_and_068_name(pg):  # noqa: F811
    from nokker.connectors.household import HouseholdLink
    from nokker.household.model import Settings
    from nokker.household.service import Household
    from nokker.household.store import PgHouseholdStore
    from nokker.team.service import Team
    from nokker.tenancy.context import tenant_scope
    from nokker.tenancy.names import owner_name
    from nokker.tenancy.store import PgTenancy

    tenancy = PgTenancy(pg["app"])
    pay = tenancy.create_account("fn-payeur@example.org")
    alice = tenancy.create_account("fn-alice@example.org")
    assert pay.first_name == "" and alice.first_name == ""  # colonne additive : vide par défaut
    hh = Household(team=Team(store=PgHouseholdStore(pg["app"]), tenancy=tenancy), settings=Settings())
    with tenant_scope(pay.tenant_id):
        hh.create(pay, {"name": "Maison"})
        iv = hh.invite(pay, {"email": alice.email, "first_name": "Alice"})
        assert iv["first_name"] == "Alice"
        assert hh.invitations(pay)["invitations"][0]["first_name"] == "Alice"  # relu en base
    with tenant_scope(alice.tenant_id):
        hh.accept(alice, iv["id"])
    assert tenancy.account(alice.account_id).first_name == "Alice"
    link = HouseholdLink(lambda: hh, tenancy)
    with tenant_scope(pay.tenant_id):
        assert owner_name(link, alice.account_id) == "Alice"
        assert {m["name"] for m in hh.view(pay)["members"]} == {"fn-payeur", "Alice"}
    assert tenancy.set_first_name(alice.account_id, "Ali").first_name == "Ali"
    assert tenancy.set_first_name(alice.account_id, "").first_name == ""
    with tenant_scope(pay.tenant_id):
        assert owner_name(link, alice.account_id) == "fn-alice"  # repli : partie locale de l'adresse
    assert tenancy.set_first_name("us_inconnu0000", "X") is None
