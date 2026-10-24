from concurrent.futures import ThreadPoolExecutor

import pytest

from nokker.tenancy.context import (
    INTERNAL_TENANT, NoTenant, current_tenant, scoped_thread_id, submit_in_context, tenant_of_thread, tenant_scope,
)


def test_no_scope_raises_instead_of_falling_back_to_internal():
    with pytest.raises(NoTenant):
        current_tenant()


def test_scope_sets_and_restores():
    with tenant_scope("tn_aaaaaaaaaaaa"):
        assert current_tenant() == "tn_aaaaaaaaaaaa"
        with tenant_scope("tn_bbbbbbbbbbbb"):
            assert current_tenant() == "tn_bbbbbbbbbbbb"
        assert current_tenant() == "tn_aaaaaaaaaaaa"
    with pytest.raises(NoTenant):
        current_tenant()


def test_invalid_tenant_is_refused():
    with pytest.raises(ValueError):
        with tenant_scope("alice"):
            pass


def test_pool_gets_the_tenant_only_through_submit_in_context():
    pool = ThreadPoolExecutor(1)
    with tenant_scope("tn_aaaaaaaaaaaa"):
        assert submit_in_context(pool, current_tenant).result() == "tn_aaaaaaaaaaaa"
        with pytest.raises(NoTenant):
            pool.submit(current_tenant).result()


def test_thread_ids_are_namespaced_for_testers_only():
    assert scoped_thread_id(INTERNAL_TENANT, "gw-canary") == "gw-canary"
    a = scoped_thread_id("tn_aaaaaaaaaaaa", "s1")
    b = scoped_thread_id("tn_bbbbbbbbbbbb", "s1")
    assert a != b and tenant_of_thread(a) == "tn_aaaaaaaaaaaa" and tenant_of_thread("gw-canary") == INTERNAL_TENANT
    assert scoped_thread_id("tn_aaaaaaaaaaaa", a) == a
