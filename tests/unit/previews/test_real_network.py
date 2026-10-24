"""Un seul test réseau RÉEL (comme `tests/unit/tools/test_web.py::test_web_search_real_engines_
return_something`) : frappe un vrai site public pour vérifier que le pipeline complet (SSRF,
redirections, OpenGraph, Trafilatura) marche aussi hors mock. Marqué `integration`, SAUTÉ par
défaut (`pyproject.toml` : non exécuté par CI) — lancer avec `NOKKER_TEST_PREVIEWS=1`."""
from __future__ import annotations

import os

import pytest

from nokker.previews.service import PreviewService


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("NOKKER_TEST_PREVIEWS") != "1",
    reason="appel réseau réel vers un site public ; export NOKKER_TEST_PREVIEWS=1 pour le lancer",
)
def test_preview_real_kubernetes_docs_page(tmp_path):
    svc = PreviewService(cache_dir=str(tmp_path))
    result = svc.preview("https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/")
    print(f"[integration] aperçu réel : {result}")
    assert result["title"]
    assert result["kind"] in ("article", "other")
    assert result["final_url"].startswith("https://kubernetes.io/")
