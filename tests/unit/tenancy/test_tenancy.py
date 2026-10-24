import pytest

from nokker.tenancy import IdentityTaken, MemoryTenancy


def test_account_creates_its_own_tenant_and_email_identity():
    t = MemoryTenancy()
    a = t.create_account(" Lea@Example.org ", "en")
    b = t.create_account("sam@example.ch")
    assert a.tenant_id != b.tenant_id and a.account_id.startswith("us_") and a.tenant_id.startswith("tn_")
    assert a.email == "lea@example.org" and a.lang == "en"
    assert t.account_by_email("LEA@example.org") == a


def test_same_email_twice_is_refused():
    t = MemoryTenancy()
    t.create_account("lea@example.org")
    with pytest.raises(IdentityTaken):
        t.create_account("lea@example.org")


def test_an_identity_never_points_to_two_accounts():
    t = MemoryTenancy()
    a, b = t.create_account("a@x.ch"), t.create_account("b@x.ch")
    t.bind_identity(a.account_id, "telegram", "111")
    t.bind_identity(a.account_id, "telegram", "111")  # idempotent
    with pytest.raises(IdentityTaken):
        t.bind_identity(b.account_id, "telegram", "111")
    assert t.account_for_identity("telegram", "111") == a
    assert t.account_for_identity("telegram", "222") is None


def test_unknown_kind_is_refused():
    t = MemoryTenancy()
    a = t.create_account("a@x.ch")
    with pytest.raises(ValueError):
        t.bind_identity(a.account_id, "sms", "+41")
