"""Détection d'Ollama (nokker/local/setup.py) : candidats par défaut, ou ceux du profil compose `local-llm`
(NOKKER_OLLAMA_URLS)."""
from __future__ import annotations

import httpx

from nokker.local import setup


def test_default_candidates_without_env(monkeypatch):
    monkeypatch.delenv(setup.OLLAMA_URLS_ENV, raising=False)
    assert setup.ollama_candidates() == setup.OLLAMA_CANDIDATES


def test_env_candidates_replace_the_defaults(monkeypatch):
    monkeypatch.setenv(setup.OLLAMA_URLS_ENV, " http://ollama:11434/ , ,http://autre:11434")
    assert setup.ollama_candidates() == ("http://ollama:11434", "http://autre:11434")


def test_detection_reads_the_env_at_call_time(monkeypatch):
    seen = []

    def fake_get(url, timeout):
        seen.append(url)
        if url.startswith("http://ollama:11434"):
            return httpx.Response(200, json={"models": [{"name": "petit:1b"}]})
        raise httpx.ConnectError("absent")

    monkeypatch.setattr(setup.httpx, "get", fake_get)
    monkeypatch.setenv(setup.OLLAMA_URLS_ENV, "http://ollama:11434")
    found = setup.detect_local_models()
    assert seen == ["http://ollama:11434/api/tags"]
    assert found and "petit:1b" in str(found)
