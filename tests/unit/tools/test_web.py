"""`nokker/tools/web.py` — recherche web sans clé (P1, 18.10.2026).

Tous les tests unitaires ci-dessous simulent les moteurs (aucune requête réseau) : on remplace
`web._ENGINE_SEARCHERS`/`web._ENGINE_EXTRACTORS` par des fonctions de test via `monkeypatch`.
Un seul test (`test_web_search_real_engines_return_something`) frappe les vrais moteurs publics ;
il est marqué `integration` et sauté par défaut (voir `pyproject.toml` : non exécuté par CI), sauf
`NOKKER_TEST_WEB=1`.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from nokker.core.ports import ToolContext
import nokker.tools.web as web
from nokker.tools.web import WebEngineError, WebToolSource, merge_results, normalize_url, pii_violation

CTX = ToolContext(thread_id="t1", turn_index=0, channel="api")
_REAL_DIRECT = web._direct_extract  # gardé avant le patch autouse ci-dessous


@pytest.fixture(autouse=True)
def _no_direct_fetch(monkeypatch):
    """La lecture directe (`_direct_extract`) ferait du vrai réseau : coupée par défaut, chaque test qui la vise
    la remplace lui-même."""
    def refuse(url, timeout_s):
        raise WebEngineError("lecture directe coupée en test")

    monkeypatch.setattr(web, "_direct_extract", refuse)


# ─────────────────────────────── normalisation d'URL ───────────────────────────────


def test_normalize_url_ignores_scheme_www_slash_and_utm():
    a = normalize_url("https://www.example.com/path/?utm_source=x&utm_medium=y")
    b = normalize_url("http://example.com/path")
    assert a == b


def test_normalize_url_keeps_non_tracking_query_params_and_sorts_them():
    a = normalize_url("https://example.com/path?b=2&a=1")
    b = normalize_url("https://example.com/path?a=1&b=2")
    assert a == b
    assert "a=1" in a and "b=2" in a


# ─────────────────────────────── fusion / dédoublonnage / consensus ───────────────────────────────


def test_merge_results_dedups_by_normalized_url_and_ranks_consensus_first():
    engine_rows = {
        "exa": [
            {"url": "https://example.com/a?utm_source=x", "title": "A", "snippet": "sa"},
            {"url": "https://example.com/b", "title": "B", "snippet": "sb"},
        ],
        "parallel": [
            {"url": "https://www.example.com/a/", "title": "A2", "snippet": ""},
            {"url": "https://example.com/c", "title": "C", "snippet": "sc"},
        ],
        "keenable": [
            {"url": "https://example.com/a", "title": "", "snippet": ""},
        ],
    }
    merged = merge_results(engine_rows, limit=5)
    assert len(merged) == 3  # a/b/c : 3 URLs distinctes malgré les 4 variantes de /a
    top = merged[0]
    assert set(top["engines"]) == {"exa", "parallel", "keenable"}
    assert top["rank"] == 1
    assert top["domain"] == "example.com"
    assert top["favicon_url"] == "https://example.com/favicon.ico"
    assert top["title"] == "A"  # 1er titre non vide rencontré
    rest_urls = {m["url"] for m in merged[1:]}
    assert rest_urls == {"https://example.com/b", "https://example.com/c"}


def test_merge_results_respects_limit():
    engine_rows = {"exa": [{"url": f"https://example.com/{i}", "title": str(i), "snippet": ""} for i in range(5)]}
    merged = merge_results(engine_rows, limit=2)
    assert len(merged) == 2
    assert [m["rank"] for m in merged] == [1, 2]


def test_merge_results_empty_when_no_engine_rows():
    assert merge_results({}, limit=5) == []


# ─────────────────────────────── garde PII ───────────────────────────────


def test_pii_guard_blocks_email():
    assert pii_violation("contacte-moi: alice@example.com") == "adresse e-mail"


def test_pii_guard_blocks_swiss_phone_number():
    assert pii_violation("appelle le +41 79 123 45 67 avant midi") == "numéro de téléphone"


def test_pii_guard_blocks_swiss_iban():
    assert pii_violation("virement vers CH93 0076 2011 6238 5295 7 stp") == "IBAN"


def test_pii_guard_blocks_swiss_avs_number():
    assert pii_violation("numéro AVS 756.1234.5678.90 à vérifier") == "numéro AVS"


def test_pii_guard_blocks_configured_term():
    assert pii_violation("projet Bruno confidentiel", blocked_terms=["Bruno"]) == "terme bloqué : Bruno"


def test_pii_guard_allows_clean_query():
    assert pii_violation("météo à Genève demain", blocked_terms=["Bruno"]) is None


def test_web_search_call_refuses_query_with_pii():
    src = WebToolSource(engines=["exa"])
    try:
        result = src.call("web_search", {"query": "écris à alice@example.com pour ça"}, CTX)
    finally:
        src.close()
    assert result.is_error
    assert "requête refusée" in result.content
    assert "adresse e-mail" in result.content


# ─────────────────────────────── disjoncteur ───────────────────────────────


def test_circuit_breaker_opens_after_threshold_and_cools_down():
    cb = web._CircuitBreaker(fail_threshold=3, cooldown_s=60.0)
    t0 = 1_000.0
    assert not cb.is_open("exa", now=t0)
    cb.record_failure("exa", now=t0)
    cb.record_failure("exa", now=t0 + 1)
    assert not cb.is_open("exa", now=t0 + 1)  # 2 échecs seulement : pas encore ouvert
    cb.record_failure("exa", now=t0 + 2)
    assert cb.is_open("exa", now=t0 + 2)  # 3e échec : ouvert
    assert cb.is_open("exa", now=t0 + 2 + 59)
    assert not cb.is_open("exa", now=t0 + 2 + 60)  # cooldown écoulé : referme


def test_circuit_breaker_success_resets_failure_count():
    cb = web._CircuitBreaker(fail_threshold=3, cooldown_s=60.0)
    cb.record_failure("exa", now=0)
    cb.record_failure("exa", now=1)
    cb.record_success("exa")
    cb.record_failure("exa", now=2)
    assert not cb.is_open("exa", now=2)  # le succès a remis le compteur à zéro


# ─────────────────────────────── cache LRU + TTL ───────────────────────────────


def test_query_cache_ttl_expires_entry():
    cache = web._QueryCache(maxsize=10, ttl_s=10.0)
    cache.set("k", "v", now=0.0)
    assert cache.get("k", now=5.0) == "v"
    assert cache.get("k", now=10.1) is None


def test_query_cache_evicts_least_recently_used():
    cache = web._QueryCache(maxsize=2, ttl_s=100.0)
    cache.set("a", "va", now=0.0)
    cache.set("b", "vb", now=0.0)
    assert cache.get("a", now=0.0) == "va"  # "a" redevient le plus récent
    cache.set("c", "vc", now=0.0)  # dépasse maxsize=2 → évince "b" (jamais relue)
    assert cache.get("b", now=0.0) is None
    assert cache.get("a", now=0.0) == "va"
    assert cache.get("c", now=0.0) == "vc"


def test_query_cache_key_normalizes_whitespace_and_case():
    assert web._QueryCache.key("  Python   Async  ", 8) == web._QueryCache.key("python async", 8)


# ─────────────────────────────── web_search (orchestration simulée) ───────────────────────────────


def test_web_search_formats_numbered_text_and_embeds_json_payload(monkeypatch):
    def fake_exa(client, query, limit, timeout_s):
        return [{"url": "https://example.com/a", "title": "A", "snippet": "sa"}]

    def fake_parallel(client, query, limit, timeout_s):
        return [{"url": "https://example.com/a", "title": "A", "snippet": "sa"}]

    monkeypatch.setitem(web._ENGINE_SEARCHERS, "exa", fake_exa)
    monkeypatch.setitem(web._ENGINE_SEARCHERS, "parallel", fake_parallel)
    src = WebToolSource(engines=["exa", "parallel"], budget_s=1.0)
    try:
        result = src.call("web_search", {"query": "requête technique sans PII", "limit": 5}, CTX)
    finally:
        src.close()
    assert not result.is_error
    assert "1. A" in result.content
    marker_start, marker_end = "<!-- nokker:web_search:data -->\n", "\n<!-- /nokker:web_search:data -->"
    assert marker_start in result.content
    json_text = result.content.split(marker_start, 1)[1].split(marker_end, 1)[0]
    payload = json.loads(json_text)
    assert payload["results"][0]["url"] == "https://example.com/a"
    assert set(payload["results"][0]["engines"]) == {"exa", "parallel"}
    assert sorted(payload["engines_queried"]) == ["exa", "parallel"]


def test_web_search_uses_cache_on_normalized_repeat_query(monkeypatch):
    calls = {"n": 0}

    def fake_exa(client, query, limit, timeout_s):
        calls["n"] += 1
        return [{"url": "https://example.com/a", "title": "A", "snippet": "sa"}]

    monkeypatch.setitem(web._ENGINE_SEARCHERS, "exa", fake_exa)
    src = WebToolSource(engines=["exa"], budget_s=1.0)
    try:
        src.call("web_search", {"query": "requête stable", "limit": 5}, CTX)
        src.call("web_search", {"query": "  REQUÊTE   stable  ", "limit": 5}, CTX)
    finally:
        src.close()
    assert calls["n"] == 1


def test_web_search_respects_global_budget_with_a_slow_engine(monkeypatch):
    def fast(client, query, limit, timeout_s):
        return [{"url": "https://fast.example/x", "title": "Fast", "snippet": ""}]

    def slow(client, query, limit, timeout_s):
        time.sleep(0.3)
        return [{"url": "https://slow.example/x", "title": "Slow", "snippet": ""}]

    monkeypatch.setitem(web._ENGINE_SEARCHERS, "exa", fast)
    monkeypatch.setitem(web._ENGINE_SEARCHERS, "parallel", slow)
    src = WebToolSource(engines=["exa", "parallel"], budget_s=0.05)
    try:
        start = time.monotonic()
        result = src.call("web_search", {"query": "requête avec un moteur lent", "limit": 5}, CTX)
        elapsed = time.monotonic() - start
    finally:
        src.close()
    assert elapsed < 0.2  # bien sous les 0,3 s du moteur lent : le budget a été respecté
    assert "fast.example" in result.content
    assert "slow.example" not in result.content


def test_web_search_opens_breaker_after_repeated_engine_failures(monkeypatch):
    def failing(client, query, limit, timeout_s):
        raise WebEngineError("HTTP 503: en panne")

    def ok(client, query, limit, timeout_s):
        return [{"url": "https://good.example/x", "title": "G", "snippet": ""}]

    monkeypatch.setitem(web._ENGINE_SEARCHERS, "exa", failing)
    monkeypatch.setitem(web._ENGINE_SEARCHERS, "parallel", ok)
    src = WebToolSource(engines=["exa", "parallel"], budget_s=1.0)
    try:
        for i in range(3):  # requêtes distinctes : jamais de cache ici
            src.call("web_search", {"query": f"requête numéro {i}"}, CTX)
        assert src.breaker.is_open("exa")
        # 4e requête : exa en pause, mais parallel répond quand même -> pas d'échec visible
        result = src.call("web_search", {"query": "requête numéro 3"}, CTX)
    finally:
        src.close()
    assert not result.is_error
    assert "good.example" in result.content


def test_web_search_rejects_unknown_tool_name():
    src = WebToolSource(engines=["exa"])
    try:
        result = src.call("not_a_tool", {}, CTX)
    finally:
        src.close()
    assert result.is_error
    assert "outil inconnu" in result.content


# ─────────────────────────────── web_fetch (orchestration simulée) ───────────────────────────────


def test_web_fetch_returns_first_successful_engine(monkeypatch):
    def exa_fail(client, url, timeout_s):
        raise WebEngineError("HTTP 500")

    def parallel_ok(client, url, timeout_s):
        return {"title": "Page", "content": "contenu complet de la page"}

    monkeypatch.setitem(web._ENGINE_EXTRACTORS, "exa", exa_fail)
    monkeypatch.setitem(web._ENGINE_EXTRACTORS, "parallel", parallel_ok)
    src = WebToolSource(engines=["exa", "parallel"], budget_s=1.0)
    try:
        result = src.call("web_fetch", {"urls": ["https://example.com/a"]}, CTX)
    finally:
        src.close()
    assert not result.is_error
    assert "contenu complet de la page" in result.content


def test_web_fetch_reports_error_when_every_engine_fails(monkeypatch):
    def fail(client, url, timeout_s):
        raise WebEngineError("HTTP 503")

    monkeypatch.setitem(web._ENGINE_EXTRACTORS, "exa", fail)
    src = WebToolSource(engines=["exa"], budget_s=1.0)
    try:
        result = src.call("web_fetch", {"urls": ["https://example.com/a"]}, CTX)
    finally:
        src.close()
    assert result.is_error
    assert "échec" in result.content


def test_web_fetch_rejects_empty_urls():
    src = WebToolSource(engines=["exa"])
    try:
        result = src.call("web_fetch", {"urls": []}, CTX)
    finally:
        src.close()
    assert result.is_error


# ─────────────────────────────── intégration réelle (sautée par défaut) ───────────────────────────────


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("NOKKER_TEST_WEB") != "1",
    reason="appel réseau réel vers les moteurs publics ; export NOKKER_TEST_WEB=1 pour le lancer",
)
def test_web_search_real_engines_return_something():
    src = WebToolSource()
    try:
        start = time.monotonic()
        result = src.call("web_search", {"query": "kubernetes pod readiness probe", "limit": 5}, CTX)
        elapsed = time.monotonic() - start
    finally:
        src.close()
    print(f"[integration] web_search réel en {elapsed:.3f}s : {result.content[:400]}")
    marker_start, marker_end = "<!-- nokker:web_search:data -->\n", "\n<!-- /nokker:web_search:data -->"
    payload = json.loads(result.content.split(marker_start, 1)[1].split(marker_end, 1)[0])
    assert payload["engines_queried"], "aucun moteur n'a répondu dans le budget — vérifier les 4 endpoints"


def test_web_fetch_accepts_a_json_encoded_list_of_urls(monkeypatch):
    seen = []

    def fake_extract(client, url, timeout_s):
        seen.append(url)
        return {"title": url, "content": "texte"}

    monkeypatch.setitem(web._ENGINE_EXTRACTORS, "exa", fake_extract)
    src = WebToolSource(engines=["exa"])
    try:
        res = src.call("web_fetch", {"urls": '["https://a.example/1", "https://b.example/2"]'}, CTX)
    finally:
        src.close()
    assert sorted(seen) == ["https://a.example/1", "https://b.example/2"] and not res.is_error


def test_web_fetch_takes_urls_from_any_argument_name(monkeypatch):
    seen = []

    def fake_extract(client, url, timeout_s):
        seen.append(url)
        return {"title": url, "content": "texte"}

    monkeypatch.setitem(web._ENGINE_EXTRACTORS, "exa", fake_extract)
    src = WebToolSource(engines=["exa"])
    try:
        res = src.call("web_fetch", {"query": "https://meteo.example/geneve?day=2026-10-20"}, CTX)
    finally:
        src.close()
    assert seen == ["https://meteo.example/geneve?day=2026-10-20"] and not res.is_error


def test_web_fetch_reads_the_page_directly_before_any_engine(monkeypatch):
    """20.10 : lecture directe (HTTP gardé + trafilatura) d'abord ; les moteurs externes seulement en repli."""
    engines = []
    monkeypatch.setattr(web, "_direct_extract", lambda url, t: {"title": "Page", "content": "texte " * 100})
    monkeypatch.setitem(web._ENGINE_EXTRACTORS, "exa", lambda c, u, t: engines.append(u) or {"title": "", "content": ""})
    src = WebToolSource(engines=["exa"])
    try:
        res = src.call("web_fetch", {"urls": ["https://a.example/1"]}, CTX)
    finally:
        src.close()
    assert not res.is_error and engines == [] and '"engine": "direct"' in res.content


def test_web_fetch_falls_back_to_engines_when_direct_read_fails(monkeypatch):
    monkeypatch.setitem(web._ENGINE_EXTRACTORS, "exa", lambda c, u, t: {"title": "Via exa", "content": "texte"})
    src = WebToolSource(engines=["exa"])
    try:
        res = src.call("web_fetch", {"urls": ["https://js-app.example/"]}, CTX)
    finally:
        src.close()
    assert not res.is_error and '"engine": "exa"' in res.content


def test_direct_extract_rejects_short_pages_and_keeps_long_ones(monkeypatch):
    pytest.importorskip("trafilatura")  # dépendance du projet, absente de certains venv de dev
    from nokker.previews import fetch as pf
    real = _REAL_DIRECT

    long_html = "<html><head><title>T</title></head><body><article>" + "<p>Paragraphe de contenu réel. </p>" * 60 + "</article></body></html>"

    def fake_get(url, **kw):
        body = (long_html if "long" in url else "<html><body><div id=app></div></body></html>").encode()
        return pf.FetchResult(requested_url=url, final_url=url, status_code=200, content_type="text/html", body=body,
                              redirects=0)

    monkeypatch.setattr(pf, "guarded_get", fake_get)
    doc = real("https://long.example/", 3.0)
    assert doc["title"] == "T" and "Paragraphe de contenu" in doc["content"]
    with pytest.raises(WebEngineError):
        real("https://short.example/", 3.0)
