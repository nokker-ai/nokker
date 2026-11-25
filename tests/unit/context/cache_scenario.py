"""Scénario de mesure du cache : un fil de N tours (un tour sur trois appelle un gros outil) contre le faux amont Anthropic
qui compte `cache_read` / `cache_write` d'après la stabilité du préfixe (`CacheModel`). Sert aux tests et à la mesure
AVANT/APRÈS (`python -m tests.unit.context.cache_scenario [--budgets '{"memory_as_message": false}']`, depuis la racine).

Fonctionne sur l'ancien code comme sur le nouveau (les champs de `ContextBudgets` inconnus sont ignorés) : pour la mesure
« avant », ce fichier, `anthropic_upstream.py` et `nokker/context/tokens.py` sont copiés dans un arbre d'avant le lot."""
from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import fields
from pathlib import Path

from nokker.context.types import ContextBudgets
from nokker.providers.anthropic import AnthropicProvider

from ..providers.anthropic_upstream import MODEL, FakeAnthropic, message
from .harness import BIG_MARK, make_session
from .token_fixtures import prose_fr

TOOL_EVERY = 3


def budgets_from(overrides: dict | None) -> ContextBudgets:
    known = {f.name for f in fields(ContextBudgets)}
    return ContextBudgets(**{k: v for k, v in (overrides or {}).items() if k in known})


HOST_PROMPT_BYTES = 21_000  # ≈ 7 k jetons : le prompt hôte de la mesure de référence


def run(budgets: ContextBudgets, tmp_path: Path, n_turns: int = 40, tool_size: int = 2600,
        close_after: int | None = None, bodies: list | None = None, tool_every: int = TOOL_EVERY,
        calls_per_turn: int = 1, distinct: bool = False, tok_scale: float = 1.0,
        user_extra: int = 0) -> list[dict]:
    """Une ligne par appel amont : tour, appel, total, lu, écrit, frais, et les événements survenus depuis l'appel d'avant
    (« shed » = compaction_version qui monte, « close » = un épisode de plus clos)."""
    rows: list[dict] = []
    with FakeAnthropic(count_cache=True) as up:
        provider = AnthropicProvider(base_url=up.url, api_key="k", model=MODEL, retry_backoff_s=0.0, cache="prefix",
                                     cache_ttl="1h")
        s = make_session(budgets, tmp_path=tmp_path, tool_size=tool_size, provider=provider,
                         prefix_text=prose_fr(HOST_PROMPT_BYTES))
        s.kernel.tool_loading = "all"
        seen_calls = 0
        prev_version, prev_closed = 0, 0
        for i in range(n_turns):
            uses_tool = i % tool_every == 0
            if uses_tool:  # `calls_per_turn` appels d'outil à la suite (tours « gros » : plusieurs résultats volumineux)
                for c in range(calls_per_turn):
                    up.reply(message([{"type": "tool_use", "id": f"toolu_{i}_{c}", "name": "big_tool",
                                      "input": {"n": i * 10 + c + 1} if distinct else {}}], stop_reason="tool_use"))
            up.reply(message([{"type": "text", "text": f"Fait n°{i}."}]))
            more = " " + prose_fr(user_extra) if user_extra else ""  # messages utilisateur longs : résumés (mémoire) plus gros
            turn = s.say(f"Montre-moi les journaux du serveur numéro {i}.{more} {BIG_MARK if uses_tool else ''}".strip())
            assert turn.error is None, (i, turn.error)
            if close_after is not None and i == close_after:  # clôture manuelle (/new, API) : même événement des deux côtés
                s.manager.new_episode(s.thread, "manual")
            st = s.manager.state(s.thread)
            closed = len(s.manager.episodes.list(s.thread, closed_only=True))
            events = []
            if closed > prev_closed:
                events.append("close")
            elif st.compaction_version > prev_version:
                events.append("shed")
            prev_version, prev_closed = st.compaction_version, closed
            for k, row in enumerate(up.cache_log[seen_calls:]):
                # `tok_scale` : jetons FACTURÉS / jetons estimés du noyau (le tokenizer réel fait plus de jetons que
                # `estimate_tokens` sur du texte chiffré). Le noyau ne change pas ; seul le décompte du cache est mis à l'échelle.
                rows.append({"turn": i, "call": row["call"], "total": round(row["total"] * tok_scale),
                             "read": round(row["cache_read"] * tok_scale), "write": round(row["cache_write"] * tok_scale),
                             "fresh": round(row["fresh"] * tok_scale),
                             "event": ",".join(events) if k == len(up.cache_log[seen_calls:]) - 1 else ""})
            seen_calls = len(up.cache_log)
        if bodies is not None:
            bodies.extend(r["body"] for r in up.requests)
    return rows


def summarize(rows: list[dict]) -> dict:
    return {"calls": len(rows), "read": sum(r["read"] for r in rows), "write": sum(r["write"] for r in rows),
            "fresh": sum(r["fresh"] for r in rows),
            "cost_usd": round((sum(r["write"] for r in rows) * 4 + sum(r["read"] for r in rows) * 0.10
                               + sum(r["fresh"] for r in rows) * 2) / 1e6, 4)}


def closures(rows: list[dict]) -> list[dict]:
    """Une entrée par clôture : le tour où elle survient, l'écart de tours avec la précédente, et ce que l'appel SUIVANT
    (le premier de la nouvelle fenêtre) lit et écrit chez l'amont."""
    out: list[dict] = []
    prev_turn = -1
    for k, r in enumerate(rows):
        if "close" in r["event"] and k + 1 < len(rows):
            nxt = rows[k + 1]
            out.append({"turn": r["turn"], "gap": r["turn"] - prev_turn, "read": nxt["read"], "write": nxt["write"],
                        "total": nxt["total"]})
            prev_turn = r["turn"]
    return out


def table(rows: list[dict]) -> str:
    out = ["tour appel   total    lu  écrit  frais  événement"]
    for r in rows:
        out.append(f"{r['turn']:>4} {r['call']:>5} {r['total']:>7} {r['read']:>5} {r['write']:>6} {r['fresh']:>6}  {r['event']}")
    return "\n".join(out)


if __name__ == "__main__":
    over = json.loads(sys.argv[sys.argv.index("--budgets") + 1]) if "--budgets" in sys.argv else {}
    n = int(sys.argv[sys.argv.index("--turns") + 1]) if "--turns" in sys.argv else 40
    close = int(sys.argv[sys.argv.index("--close-after") + 1]) if "--close-after" in sys.argv else None
    size = int(sys.argv[sys.argv.index("--tool-size") + 1]) if "--tool-size" in sys.argv else 2600
    every = int(sys.argv[sys.argv.index("--tool-every") + 1]) if "--tool-every" in sys.argv else TOOL_EVERY
    extra = int(sys.argv[sys.argv.index("--user-extra") + 1]) if "--user-extra" in sys.argv else 0
    scale = float(sys.argv[sys.argv.index("--tok-scale") + 1]) if "--tok-scale" in sys.argv else 1.0
    distinct = "--distinct" in sys.argv  # lectures toutes différentes (sinon identiques : une seule est vraiment admise)
    cpt = int(sys.argv[sys.argv.index("--calls-per-turn") + 1]) if "--calls-per-turn" in sys.argv else 1
    with tempfile.TemporaryDirectory() as d:
        rows = run(budgets_from(over), Path(d), n_turns=n, tool_size=size, close_after=close,
                   tool_every=every, calls_per_turn=cpt, distinct=distinct, tok_scale=scale, user_extra=extra)
    if "--rows" in sys.argv:
        print(table(rows))
    if "--closures" in sys.argv:
        for c in closures(rows):
            print(c)
    print(json.dumps({**summarize(rows), "closes": len(closures(rows))}))
