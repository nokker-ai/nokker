"""Mesure du 1er appel d'une reprise IMPORTÉE (266 entrées, ~104 Ko, 13 gros résultats d'outils, fixture synthétique) contre le
faux amont Anthropic qui compte le cache : fenêtre « messages » estimée (même estimateur des deux côtés) et cache des 3 premières
questions. `python -m tests.unit.context.import_scenario [--budgets '{…}']` depuis la racine ; fonctionne sur l'ancien code."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from nokker.providers.anthropic import AnthropicProvider

from ..providers.anthropic_upstream import MODEL, FakeAnthropic, _tokens, _units, message
from .cache_scenario import HOST_PROMPT_BYTES, budgets_from
from .harness import make_session
from .import_fixture import import_into
from .token_fixtures import prose_fr

QUESTIONS = ["Dans quel fichier se trouve le modèle de référence, celui avec les renforts ?",
             "Quel est le pas de temps retenu pour la simulation ?",
             "Rappelle-moi la masse du vérin."]


def run(budgets, tmp_path: Path) -> dict:
    with FakeAnthropic(count_cache=True) as up:
        provider = AnthropicProvider(base_url=up.url, api_key="k", model=MODEL, retry_backoff_s=0.0, cache="prefix",
                                     cache_ttl="1h")
        s = make_session(budgets, tmp_path=tmp_path, tool_size=100, provider=provider,
                         prefix_text=prose_fr(HOST_PROMPT_BYTES))
        s.kernel.tool_loading = "all"
        import_into(s)
        for q in QUESTIONS:
            up.reply(message([{"type": "text", "text": "Voici la réponse."}]))
            assert s.say(q).error is None
        first = up.requests[0]["body"]
        units, _ = _units(first)
        window = sum(_tokens(u) for u in units if u[0] == "M")
        tool_results = sum(1 for m in first["messages"] for b in (m["content"] if isinstance(m["content"], list) else [])
                           if b.get("type") == "tool_result" and "déplacé hors fenêtre" not in json.dumps(b)
                           and "tokens au total" not in json.dumps(b))
        return {"window_messages_tokens": window, "tool_results_in_clear": tool_results, "calls": up.cache_log}


if __name__ == "__main__":
    over = json.loads(sys.argv[sys.argv.index("--budgets") + 1]) if "--budgets" in sys.argv else {}
    with tempfile.TemporaryDirectory() as d:
        r = run(budgets_from(over), Path(d))
    print(json.dumps({k: v for k, v in r.items() if k != "calls"}))
    for c in r["calls"]:
        print(c)
