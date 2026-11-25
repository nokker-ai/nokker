"""Historique importé SYNTHÉTIQUE de la forme mesurée au banc de reprise (120 tours, 266 entrées, ~104 Ko, 13 gros résultats
d'outils JSON à UUID, 7 dans la première moitié, 6 dans la seconde) : aucune donnée réelle."""
from __future__ import annotations

import json
import random
import uuid

from nokker.core.history_import import Entry, build_turns

from . import token_fixtures as fx

TOOL_TURNS = (6, 13, 22, 31, 40, 52, 66, 78, 85, 92, 99, 108, 115)  # 13 tours avec un gros résultat d'outil
FACT_TURN = 31  # un nom de fichier planté dans le résultat et dans la réponse de ce tour


def entries(seed: int = 11, tool_bytes: int = 4600) -> list[Entry]:
    rng = random.Random(seed)
    out: list[Entry] = []
    for i in range(120):
        out.append(Entry(role="user", content=f"T{i} : " + fx.prose_fr(rng.randint(60, 150), seed=100 + i)))
        if i in TOOL_TURNS:
            cid = f"call_{i}"
            args = {"path": f"fichier_{i}.mbd"}
            out.append(Entry(role="assistant", content="Je lis le fichier.",
                             tool_calls=[{"id": cid, "name": "read_file", "arguments": args}]))
            body = fx.tool_result_json(tool_bytes + rng.randint(-1500, 2500), seed=200 + i)
            if i == FACT_TURN:
                body = json.dumps({"path": "mon_modele_v3.mbd", "id": str(uuid.UUID(int=i))}) + "\n" + body
            out.append(Entry(role="tool", content=body, name="read_file", tool_call_id=cid))
            out.append(Entry(role="assistant", content=("Le modèle est dans mon_modele_v3.mbd. " if i == FACT_TURN else "")
                             + fx.prose_fr(rng.randint(80, 200), seed=300 + i)))
        else:
            out.append(Entry(role="assistant", content=fx.prose_fr(rng.randint(100, 260), seed=400 + i)))
    return out


def import_into(session, thread: str | None = None, **kw) -> int:
    """Range l'historique dans le store de la session comme `import_history` ; renvoie le nombre d'entrées."""
    thread = thread or session.thread
    es = entries(**kw)
    for t in build_turns(session.kernel, thread, es):
        session.store.append(t)
    return len(es)


def window_tokens_of(messages: list[dict]) -> int:
    """Jetons estimés des messages hors `system` (ce que mesure « messages » au Router)."""
    from nokker.context.tokens import estimate_tokens

    n = 0
    for m in messages:
        if m.get("role") == "system":
            continue
        n += estimate_tokens(str(m.get("content") or ""))
        for tc in m.get("tool_calls") or []:
            n += estimate_tokens(json.dumps(tc, ensure_ascii=False), "json")
    return n
