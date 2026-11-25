"""`delegate_tenants: ["*"]` : delegate en conversation offert à tous les locataires (testeurs aux ids aléatoires)."""
from nokker.tools.delegate import DelegateToolSource


def test_tenants_none_means_all():
    src = DelegateToolSource(lambda *a: None, None, tier=True, tenants=None)
    assert src.tenants is None
    assert all(src.available_for(t) for t in ("tn_x7f", "internal", "tn_abc"))


def test_explicit_list_still_restricts():
    src = DelegateToolSource(lambda *a: None, None, tier=True, tenants=frozenset({"tn_a"}))
    assert src.available_for("tn_a") and not src.available_for("tn_b")
