"""Texte de `delegate` en conversation (delegate_tenants, sans session de code) : sans git ni worktree (relecture, Run6)."""
from nokker.tools.delegate import CONV_DESCRIPTION, DelegateToolSource

EXPECTED = ("Confie un travail à un sous-agent et attends son compte rendu : tier ship pour une exécution technique "
            "bornée (code, build, opérations), tier deep pour une conception ou un diagnostic difficile. Tu restes la "
            "voix : transmets le compte rendu fidèlement, sans rien ajouter.")


def _spec(**kw):
    return DelegateToolSource(lambda *a: None, kw.pop("code", None), **kw).specs()[0]


def test_conversation_avec_paliers_texte_sans_git():
    spec = _spec(tier=True)
    assert CONV_DESCRIPTION == EXPECTED and spec.description == EXPECTED
    assert "worktree" not in spec.description and "git" not in spec.description
    assert "isolate" not in spec.parameters["properties"] and "tier" in spec.parameters["properties"]


def test_sans_paliers_ou_session_de_code_texte_inchange():
    for spec in (_spec(tier=False), _spec(tier=True, code=object())):
        assert "worktree git (branche nokker/sa-<id>)" in spec.description
        assert "isolate" in spec.parameters["properties"]
