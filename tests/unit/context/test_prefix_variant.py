"""Variante de préfixe PAR SESSION (`prefix_variant`, `local.prefix_variants`, docs/HOST-EMBED.md §22) : liste blanche déclarée côté config (jamais un chemin
reçu), défaut inchangé à l'octet, empreinte propre et stable par variante, mémoire par fil (sous-agents compris), erreurs claires. Préfixes de test en tmp_path."""
from __future__ import annotations

import logging

import pytest

from nokker.context import prefix_mode
from nokker.context.engine import ContextEngineV0
from nokker.local.api import LocalApi
from nokker.tools.registry import Registry
from nokker.tools.scope import thread_scope


@pytest.fixture(autouse=True)
def _clean():
    for t in ("th_v", "th_v/sa-1", "th_w"):
        prefix_mode.variant_registry().clear(t)
        prefix_mode.registry().clear(t)
    yield
    for t in ("th_v", "th_v/sa-1", "th_w"):
        prefix_mode.variant_registry().clear(t)
        prefix_mode.registry().clear(t)


def _file(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _engine(tmp_path, variants=None):
    base = _file(tmp_path, "base.md", "PREFIXE-DE-BASE\n")
    kw = {} if variants is None else {"prefix_variants": {n: _file(tmp_path, f"{n}.md", t) for n, t in variants.items()}}
    return ContextEngineV0(tools=Registry(sources=[]), prefix_path=base, **kw)


def test_default_is_unchanged_to_the_byte_with_or_without_declared_variants(tmp_path):
    plain = _engine(tmp_path)
    declared = _engine(tmp_path, {"court": "PREFIXE-COURT\n"})
    with thread_scope("th_v"):
        assert (plain._prefix_text, plain._prefix_sha256) == (declared._prefix_text, declared._prefix_sha256)
    assert plain._prefix_text == "PREFIXE-DE-BASE\n"


def test_session_variant_selects_its_prefix_with_its_own_stable_sha(tmp_path):
    eng = _engine(tmp_path, {"court": "PREFIXE-COURT\n", "formel": "PREFIXE-FORMEL\n"})
    with thread_scope("th_w"):
        base_sha = eng._prefix_sha256
    prefix_mode.variant_registry().set("th_v", "court")
    prefix_mode.variant_registry().set("th_w", "formel")
    with thread_scope("th_v"):
        assert eng._prefix_text == "PREFIXE-COURT\n"
        court = {eng._prefix_sha256 for _ in range(3)}
    with thread_scope("th_w"):
        assert eng._prefix_text == "PREFIXE-FORMEL\n"
        formel = eng._prefix_sha256
    assert len(court) == 1 and court != {base_sha} and formel != base_sha and {formel} != court
    prefix_mode.variant_registry().clear("th_v")  # une ouverture sans le champ : retour au défaut, même empreinte qu'avant
    with thread_scope("th_v"):
        assert eng._prefix_text == "PREFIXE-DE-BASE\n" and eng._prefix_sha256 == base_sha


def test_sub_agent_inherits_and_other_threads_are_untouched(tmp_path):
    eng = _engine(tmp_path, {"court": "PREFIXE-COURT\n"})
    prefix_mode.variant_registry().set("th_v", "court")
    with thread_scope("th_v/sa-1"):
        assert eng._prefix_text == "PREFIXE-COURT\n"
    with thread_scope("th_w"):
        assert eng._prefix_text == "PREFIXE-DE-BASE\n"
    assert eng._prefix_text == "PREFIXE-DE-BASE\n"  # hors fil


def test_prefix_host_wins_and_an_undeclared_name_falls_back(tmp_path):
    eng = _engine(tmp_path, {"court": "PREFIXE-COURT\n"})
    prefix_mode.variant_registry().set("th_v", "court")
    prefix_mode.registry().set("th_v", "host")  # (l'API refuse la paire ; le moteur reste sûr)
    with thread_scope("th_v"):
        assert "PREFIXE-COURT" not in eng._prefix_text
    prefix_mode.registry().clear("th_v")
    prefix_mode.variant_registry().set("th_v", "fantome")
    with thread_scope("th_v"):
        assert eng._prefix_text == "PREFIXE-DE-BASE\n"


def test_unreadable_variant_is_a_startup_error_without_content(tmp_path, caplog):
    base = _file(tmp_path, "base.md", "B\n")
    with pytest.raises(ValueError, match="prefix_variants"):
        ContextEngineV0(tools=Registry(sources=[]), prefix_path=base, prefix_variants={"court": str(tmp_path / "absent.md")})
    ok = _file(tmp_path, "c.md", "CONTENU-SECRET")
    with caplog.at_level(logging.INFO):
        ContextEngineV0(tools=Registry(sources=[]), prefix_path=base, prefix_variants={"court": ok})
    assert "court -> " in caplog.text and "CONTENU-SECRET" not in caplog.text


@pytest.mark.parametrize("bad", ["Court", "", "a b", "../x", "é", "x" * 33, "-a"])
def test_declared_names_are_a_strict_whitelist_syntax(bad):
    with pytest.raises(ValueError):
        prefix_mode.declared_variants({bad: "/p.md"})
    with pytest.raises(ValueError):
        prefix_mode.declared_variants({"ok": ""})
    with pytest.raises(ValueError):
        prefix_mode.declared_variants(["court"])
    assert prefix_mode.declared_variants(None) == {} and prefix_mode.declared_variants({"a-b_1": "/p.md"}) == {"a-b_1": "/p.md"}


# ───────────────────────── option de session ─────────────────────────

def _api(tmp_path, variants=None):
    root = tmp_path / "projets"
    (root / "app").mkdir(parents=True, exist_ok=True)
    local = {"token_path": str(tmp_path / "tok"), "code": {"roots": [str(root)], "backend": "local"}}
    if variants is not None:
        local["prefix_variants"] = variants
    return LocalApi.from_config(local, port=8742)


def _open(api, tmp_path, **extra):
    return api.handle("POST", "/v1/local/sessions", f"Bearer {api.token}", {"cwd": str(tmp_path / "projets" / "app"), **extra})


def test_session_option_is_remembered_cleared_and_echoed(tmp_path):
    api = _api(tmp_path, {"court": "/x/court.md"})
    status, body = _open(api, tmp_path, thread_id="th_v", prefix_variant="court")
    assert status == 201 and body["prefix_variant"] == "court" and prefix_mode.variant_registry().get("th_v") == "court"
    status, body = _open(api, tmp_path, thread_id="th_v")  # réouverture sans le champ
    assert status == 201 and "prefix_variant" not in body and prefix_mode.variant_registry().get("th_v") == ""
    _open(api, tmp_path, thread_id="th_v", prefix_variant="court")
    assert api.handle("DELETE", "/v1/local/sessions/th_v", f"Bearer {api.token}", {})[0] == 200
    assert prefix_mode.variant_registry().get("th_v") == ""


def test_unknown_name_never_a_path_and_wrong_type(tmp_path):
    api = _api(tmp_path, {"court": "/x/court.md", "formel": "/x/formel.md"})
    for bad in ("inconnu", "/x/court.md", "../court", "Court"):
        status, body = _open(api, tmp_path, prefix_variant=bad)
        assert status == 400 and body["error"]["code"] == "unknown_prefix_variant" and body["error"]["valid"] == ["court", "formel"]
    for bad in (1, True, [], {}):
        status, body = _open(api, tmp_path, prefix_variant=bad)
        assert status == 400 and body["error"]["code"] == "bad_prefix_variant"
    assert api.code.sessions() == {}


def test_without_declared_variants_the_field_is_refused_with_an_empty_list(tmp_path):
    api = _api(tmp_path)
    status, body = _open(api, tmp_path, prefix_variant="court")
    assert status == 400 and body["error"]["code"] == "unknown_prefix_variant" and body["error"]["valid"] == []


def test_conflict_with_prefix_host(tmp_path):
    api = _api(tmp_path, {"court": "/x/court.md"})
    status, body = _open(api, tmp_path, prefix="host", prefix_variant="court")
    assert status == 400 and body["error"]["code"] == "prefix_variant_conflict" and api.code.sessions() == {}


def test_relayed_call_ignores_prefix_variant(tmp_path, caplog):
    api = _api(tmp_path, {"court": "/x/court.md"})
    api.relayed_owner = lambda auth: "owner"  # type: ignore[method-assign]
    api.delegation_claims = lambda auth: None  # type: ignore[method-assign]
    caplog.set_level(logging.INFO)
    status, body = api.handle("POST", "/v1/local/sessions", "Bearer x",
                              {"cwd": str(tmp_path / "projets" / "app"), "thread_id": "th_w", "prefix_variant": "court"}, relayed=True)
    assert status == 201 and "prefix_variant" not in body and prefix_mode.variant_registry().get("th_w") == ""
    assert "prefix_variant ignoré" in caplog.text
