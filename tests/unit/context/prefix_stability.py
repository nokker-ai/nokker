"""MESURE (pas de changement d'ordre) : longueur, en jetons, du début de requête STRICTEMENT identique d'un appel au suivant
dans un même fil, et ce qui le casse. vLLM ne met en cache que par blocs alignés (~1 600 jetons Stockholm, 1 664 Genève) :
un préfixe commun qui dépasse 1 600 puis 3 200 jetons gagne 1 puis 2 blocs.

Requête mesurée = `tools` (schémas) puis `messages`, sérialisés dans l'ordre où le gabarit de chat les rend (outils
d'abord, puis le système, puis les tours). Jetons = `estimate_tokens` (même estimateur que le noyau).

    PYTHONPATH=. python -m tests.unit.context.prefix_stability [--turns N] [--prefix-chars N]

Scénario : un fil de N tours (un tour sur trois appelle un outil), un événement de clôture d'épisode au tour `close_at`,
un préfixe hôte de `prefix_chars` caractères (~7 k jetons comme la mesure du 18.11)."""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from nokker.context.tokens import estimate_tokens
from nokker.context.types import ContextBudgets

from .harness import BIG_MARK, make_session


def common_prefix_chars(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def router_order(req: dict) -> str:
    """Ordre de rendu d'un gabarit de chat de la famille du Router : le système, PUIS les outils, puis les tours (le gabarit
    Qwen rend les schémas dans le bloc système, après son texte). Anthropic : `tools` avant `system` (ordre ci-dessus)."""
    msgs = req["messages"]
    return (json.dumps(msgs[:1], ensure_ascii=False) + json.dumps(req["tools"], ensure_ascii=False)
            + json.dumps(msgs[1:], ensure_ascii=False))


def tools_append_only(prev: dict, cur: dict) -> bool:
    """Le bloc d'outils de `cur` prolonge celui de `prev` (même début, rien retiré ni réordonné)."""
    a, b = prev["tools"], cur["tools"]
    return b[:len(a)] == a


def first_divergence(prev: dict, cur: dict) -> str:
    """Quel bloc de la requête porte la première différence."""
    if json.dumps(prev["tools"], ensure_ascii=False) != json.dumps(cur["tools"], ensure_ascii=False):
        return "tools"
    for i, (a, b) in enumerate(zip(prev["messages"], cur["messages"], strict=False)):
        if a != b:
            role = a.get("role")
            return f"messages[{i}] ({role})" + (" = système" if i == 0 else "")
    return "fin de la requête précédente (ajout seul)"


def run(turns: int = 12, prefix_chars: int = 20000, close_at: int | None = 8, budgets: ContextBudgets | None = None,
        tool_turns: tuple[int, ...] | None = None):
    with tempfile.TemporaryDirectory() as d:
        s = make_session(budgets or ContextBudgets(), tmp_path=Path(d), tool_size=1500, prefix_text="Règle. " * (prefix_chars // 7))
        reqs: list[dict] = []
        orig = s.provider.complete

        def spy(messages, tools, **kw):
            reqs.append({"messages": [dict(m) for m in messages], "tools": json.loads(json.dumps(tools))})
            return orig(messages, tools, **kw)

        s.provider.complete = spy
        rows = []
        for t in range(1, turns + 1):
            if close_at and t == close_at:
                s.manager.new_episode(s.thread, "manual")
            uses_tool = (t in tool_turns) if tool_turns is not None else t % 3 == 0
            s.say(f"Tour {t} : question {t}{' ' + BIG_MARK if uses_tool else ''}")
            while len(rows) + 1 < len(reqs):
                i = len(rows) + 1
                cur, prev = reqs[i], reqs[i - 1]
                a = json.dumps(prev["tools"], ensure_ascii=False) + json.dumps(prev["messages"], ensure_ascii=False)
                b = json.dumps(cur["tools"], ensure_ascii=False) + json.dumps(cur["messages"], ensure_ascii=False)
                k = common_prefix_chars(a, b)
                rows.append({"call": i, "turn": t, "common_tokens": estimate_tokens(a[:k]),
                             "request_tokens": estimate_tokens(b), "breaks": first_divergence(prev, cur),
                             "router_common_tokens": estimate_tokens(router_order(prev)[:common_prefix_chars(
                                 router_order(prev), router_order(cur))]),
                             "tools_append_only": tools_append_only(prev, cur)})
        return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=12)
    ap.add_argument("--prefix-chars", type=int, default=20000)
    ap.add_argument("--close-at", type=int, default=8)
    a = ap.parse_args()
    print(f"{'appel':>5} {'tour':>4} {'préfixe commun':>15} {'requête':>8}  >1600 >3200  casse à")
    for r in run(a.turns, a.prefix_chars, a.close_at):
        print(f"{r['call']:>5} {r['turn']:>4} {r['common_tokens']:>15} {r['request_tokens']:>8}  "
              f"{'oui' if r['common_tokens'] > 1600 else 'non':>5} {'oui' if r['common_tokens'] > 3200 else 'non':>5}  {r['breaks']}")


if __name__ == "__main__":
    main()
