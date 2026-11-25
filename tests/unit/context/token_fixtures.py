"""Textes SYNTHÉTIQUES de même forme que les mesures de référence du Router (aucune donnée réelle) : prose française,
JSON de schémas, résultats d'outils numériques à UUID, mémoire de session. Graine fixe : octet pour octet reproductibles."""
from __future__ import annotations

import json
import random
import uuid

_FR = ("le la les un une des de du et ou mais donc car que qui dans pour avec sans sur sous entre vers chez par "
       "modèle fichier résultat vérifie réponds toujours utilise l'outil demandé avant d'écrire une valeur révisée "
       "épisode mémoire conversation déjà été décidé aucune règle système approbation prépare génère répète "
       "été général préférence résumé détail échéance température pression vitesse écart paramètre simulation "
       "maillage calcul résolution dernière version fichier projet ligne colonne unité tolérance démarre arrête").split()
_EN = ("the rule applies to every answer you give and the user may ask for details without any special request so "
       "keep the system safe and never reveal secrets or approve actions on behalf of someone else unless asked").split()


def _sentences(rng: random.Random, words: list[str], n_chars: int, accent_bias: bool = False) -> str:
    out: list[str] = []
    size = 0
    while size < n_chars:
        k = rng.randint(6, 16)
        s = " ".join(rng.choice(words) for _ in range(k))
        s = s[0].upper() + s[1:] + rng.choice([".", ".", ".", " ;", " :", "."])
        if rng.random() < 0.15:
            s += "\n\n## " + " ".join(rng.choice(words) for _ in range(3))
        out.append(s)
        size += len(s) + 1
    return " ".join(out)[:n_chars]


def prose_fr(n_bytes: int, seed: int = 1) -> str:
    """Prompt hôte : prose française avec un peu de markdown."""
    text = _sentences(random.Random(seed), _FR, n_bytes)
    while len(text.encode()) > n_bytes:
        text = text[:-5]
    return text


def prose_en(n_bytes: int, seed: int = 2) -> str:
    return _sentences(random.Random(seed), _EN, n_bytes)


def tool_schemas(n_bytes: int, seed: int = 3) -> str:
    """Schémas d'outils JSON (noms, descriptions en français, propriétés typées)."""
    rng = random.Random(seed)
    tools: list[dict] = []
    while len(json.dumps(tools, ensure_ascii=False)) < n_bytes:
        name = "_".join(rng.choice(["read", "write", "list", "run", "model", "file", "campaign", "result", "stats"])
                        for _ in range(2)) + str(len(tools))
        props = {f"param_{i}": {"type": rng.choice(["string", "number", "boolean"]),
                                "description": _sentences(rng, _FR, rng.randint(40, 110))} for i in range(rng.randint(2, 6))}
        tools.append({"type": "function", "function": {"name": name, "description": _sentences(rng, _FR, 160),
                                                         "parameters": {"type": "object", "properties": props,
                                                                        "required": list(props)[:2]}}})
    return json.dumps(tools, ensure_ascii=False)[:n_bytes]


def tool_result_json(n_bytes: int, seed: int = 4) -> str:
    """Résultat d'outil : JSON de données avec UUID, horodatages, nombres flottants."""
    rng = random.Random(seed)
    rows: list[dict] = []
    while len(json.dumps(rows)) < n_bytes:
        rows.append({"id": str(uuid.UUID(int=rng.getrandbits(128), version=4)),
                     "run_id": str(uuid.UUID(int=rng.getrandbits(128), version=4)),
                     "t": round(rng.uniform(0, 5), 6), "x": [round(rng.uniform(-1, 1), 5) for _ in range(3)],
                     "status": rng.choice(["finished", "failed", "queued"]), "n": rng.randint(0, 99999)})
    return json.dumps(rows)[:n_bytes]


def memory_block(n_bytes: int, seed: int = 5) -> str:
    """`[Mémoire de la session]` : ids d'épisodes, dates, noms de fichiers, valeurs avec unités, noms propres."""
    rng = random.Random(seed)
    lines = ["[Mémoire de la session]"]
    while len("\n".join(lines)) < n_bytes:
        lines.append(f"- ep_{rng.getrandbits(48):012x} (#{rng.randint(1, 9)}, 2026-10-{rng.randint(10, 28)}, "
                     f"{rng.randint(10, 80)} tours) « Modèle {rng.choice(['Aile', 'Fuselage', 'Empennage'])} v{rng.randint(1, 9)} » — "
                     f"Demandes : ouvre mon_modele_v{rng.randint(1, 9)}.mbd ; pas de temps {rng.randint(1, 9)}e-{rng.randint(2, 5)} s, "
                     f"masse {rng.randint(100, 900)} kg ; Outils : read_file×{rng.randint(1, 5)}, list_runs. "
                     + _sentences(rng, _FR, 120))
    return "\n".join(lines)[:n_bytes]


def chat_messages(n_user: int = 49, n_assistant: int = 54, seed: int = 6) -> list[str]:
    rng = random.Random(seed)
    return ([_sentences(rng, _FR, rng.randint(40, 160)) for _ in range(n_user)]
            + [_sentences(rng, _FR, rng.randint(60, 190)) for _ in range(n_assistant)])
