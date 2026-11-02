"""Lecture des échecs dans les lots de `nokker traces export` (docs/TRACES.md, docs/SELF-IMPROVE.md §3).

Ce que la réflexion a le droit de lire :

- les lignes du locataire INTERNE seulement (`meta.tenant_type == "internal"`) : un calque d'outil ou de routeur vaut
  pour toute l'instance, il ne doit rien porter d'un compte testeur (cloisonnement, docs/TENANCY.md) ;
- jamais une ligne de banc (`meta.bench`, fil `bench-…` / `improve-…`) ni une ligne dont la demande recoupe le texte
  d'un banc (8 mots de suite en commun, `guards.Corpus`) : aucun banc ne nourrit une variante, donc aucun banc ne
  remonte par elle dans l'entraînement.

Un tour en échec : signal de l'utilisateur au tour suivant (`no`, `correction`, `relance`), erreur du tour, ou appel à
un outil inconnu. Attribution par cible (`failures_for`) :

- `tools` : (1) outil inconnu `X` → l'outil réel le plus proche de `X` ; (2) tour en échec suivi, dans le même fil, d'un
  tour réussi qui appelle un outil `T` absent du tour raté → `T` (sa description n'a pas suffi à le trouver) ;
  (3) appel de `T` dont le résultat est une erreur → `T` ;
- `skills` : skill lu (`skill_view`) dans un tour raté, ou lu au tour qui rattrape → ce skill ;
- `router` : tous les tours en échec, avec palier, motif de routage et échecs d'outil (une seule unité `route`) ;
- `blocks` : tous les tours en échec (un bloc ne s'attribue pas à un tour).
"""
from __future__ import annotations

import difflib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FAIL_SIGNALS = ("no", "correction", "relance")
UNKNOWN_PREFIX = "outil inconnu"


@dataclass
class TurnRecord:
    thread: str
    turn: int
    user_text: str
    tools: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)  # (outil, début du résultat)
    unknown: list[str] = field(default_factory=list)
    signal: str | None = None
    turn_error: str | None = None
    tiers: list[str] = field(default_factory=list)
    route_reasons: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.signal in FAIL_SIGNALS or bool(self.turn_error) or bool(self.unknown)

    def outcome(self) -> str:
        if self.unknown:
            return f"outil inconnu appelé : {', '.join(self.unknown)}"
        if self.turn_error:
            return f"erreur du tour : {self.turn_error[:160]}"
        return {"no": "l'utilisateur a dit non au tour suivant", "correction": "l'utilisateur a corrigé au tour suivant",
                "relance": "l'utilisateur a relancé la même demande"}.get(self.signal or "", "réussi")


@dataclass
class Example:
    """Un échec attribué à une unité, tel qu'il part dans la réflexion (textes courts, déjà caviardés à l'export)."""

    user_text: str
    what: str
    tools: list[str]
    detail: str = ""

    def render(self) -> str:
        tools = ", ".join(self.tools) or "aucun"
        line = f"- Demande : « {self.user_text[:300]} » — outils appelés : {tools} — {self.what}"
        return line + (f" ({self.detail[:200]})" if self.detail else "")


def iter_lines(paths: Iterable[str | Path]) -> Iterator[dict[str, Any]]:
    """Lignes de lots (`traces-AAAAMMJJ-n/`), de dossiers qui en contiennent, ou de fichiers `calls*.jsonl`."""
    for p in map(Path, paths):
        files = sorted(p.rglob("calls*.jsonl")) if p.is_dir() else [p]
        for f in files:
            with open(f, encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if raw:
                        yield json.loads(raw)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return ""


def _user_text(prompt: list[dict[str, Any]]) -> str:
    for m in reversed(prompt or []):
        if m.get("role") == "user":
            return _text(m.get("content")).strip()
    return ""


def turns_from_lines(lines: Iterable[dict[str, Any]], corpus: Any = None,
                     testers_with_consent: bool = False) -> tuple[list[TurnRecord], Counter]:
    """Regroupe les lignes par tour. `corpus` : `guards.Corpus` des bancs (lignes qui les recoupent écartées).
    `testers_with_consent` (réglage `self_improve_settings.testers_with_consent`, défaut faux) : garde aussi les lignes
    de testeurs qui ont consenti à l'entraînement (`meta.training_ok`), déjà pseudonymisées et passées au filtre PII par
    l'export ; sinon le locataire interne seul."""
    counts: Counter = Counter()
    by_turn: dict[tuple[str, int], TurnRecord] = {}
    for ln in lines:
        meta = ln.get("meta") or {}
        thread = str(meta.get("thread") or "")
        if meta.get("bench") or thread.rsplit(":", 1)[-1].startswith(("bench-", "improve-")):
            counts["excluded_bench"] += 1
            continue
        if meta.get("tenant_type") != "internal" and not (testers_with_consent and meta.get("training_ok") is True):
            counts["excluded_tester"] += 1
            continue
        key = (thread, int(meta.get("turn") or 0))
        rec = by_turn.get(key)
        if rec is None:
            text = _user_text(ln.get("prompt") or [])
            if corpus is not None and corpus.overlaps(text):
                counts["excluded_bench_overlap"] += 1
                by_turn[key] = TurnRecord(thread=thread, turn=key[1], user_text="", signal="__skip__")
                continue
            rec = by_turn[key] = TurnRecord(thread=thread, turn=key[1], user_text=text)
        if rec.signal == "__skip__":
            continue
        counts["lines"] += 1
        sig = (meta.get("user_signal") or {}).get("kind") if isinstance(meta.get("user_signal"), dict) else None
        rec.signal = sig if sig is not None else rec.signal
        extra = meta.get("extra") or {}
        rec.turn_error = rec.turn_error or extra.get("turn_error")
        if meta.get("tier"):
            rec.tiers.append(str(meta["tier"]))
        if extra.get("route_reason"):
            rec.route_reasons.append(str(extra["route_reason"]))
        names: dict[str, str] = {}
        for m in ln.get("completion") or []:
            if m.get("role") == "assistant":
                for tc in m.get("tool_calls") or []:
                    name = str((tc.get("function") or {}).get("name") or "")
                    if name:
                        names[str(tc.get("id") or "")] = name
                        rec.tools.append(name)
                    if name == "skill_view":
                        try:
                            args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
                        except ValueError:
                            args = {}
                        if isinstance(args, dict) and args.get("name"):
                            rec.tools.append(f"skill_view:{args['name']}")
            elif m.get("role") == "tool":
                body = _text(m.get("content")).strip()
                name = str(m.get("name") or names.get(str(m.get("tool_call_id") or ""), ""))
                low = body.lower()
                if low.startswith(UNKNOWN_PREFIX):
                    rec.unknown.append(name)
                elif low.startswith(("erreur", "error", "refusé", "échec")):
                    rec.errors.append((name, body[:200]))
    turns = [t for t in by_turn.values() if t.signal != "__skip__"]
    turns.sort(key=lambda t: (t.thread, t.turn))
    counts["turns"] = len(turns)
    counts["failed_turns"] = sum(1 for t in turns if t.failed)
    return turns, counts


def failures_for(target: str, turns: list[TurnRecord], units: Iterable[str]) -> dict[str, list[Example]]:
    keys = list(units)
    out: dict[str, list[Example]] = defaultdict(list)
    by_thread: dict[str, list[TurnRecord]] = defaultdict(list)
    for t in turns:
        by_thread[t.thread].append(t)
    if target in ("router", "blocks"):
        for t in turns:
            if t.failed:
                detail = ""
                if target == "router":
                    detail = f"paliers {'/'.join(t.tiers) or '?'} ; routage : {'; '.join(t.route_reasons)[:160]} ; " \
                             f"échecs d'outil {len(t.errors)}"
                for k in keys:
                    out[k].append(Example(t.user_text, t.outcome(), t.tools, detail))
        return dict(out)
    family = "skill_view" if target == "skills" else None
    for seq in by_thread.values():
        for i, t in enumerate(seq):
            if not t.failed:
                continue
            nxt = seq[i + 1] if i + 1 < len(seq) and seq[i + 1].turn == t.turn + 1 else None
            if target == "tools":
                for bad in t.unknown:
                    close = difflib.get_close_matches(bad, keys, n=1, cutoff=0.5)
                    if close:
                        out[close[0]].append(Example(t.user_text, f"outil inconnu « {bad} » appelé à sa place",
                                                     t.tools))
                for name, body in t.errors:
                    if name in keys:
                        out[name].append(Example(t.user_text, "appel en erreur", t.tools, body))
                if nxt is not None and not nxt.failed:
                    for name in dict.fromkeys(nxt.tools):
                        if name in keys and name not in t.tools:
                            out[name].append(Example(t.user_text, f"{t.outcome()} ; trouvé seulement au tour "
                                                                  f"suivant", t.tools))
            elif family is not None:
                for name in keys:
                    used_here = name in _skills_read(t)
                    rescued = nxt is not None and not nxt.failed and name in _skills_read(nxt)
                    if used_here or rescued:
                        what = t.outcome() + (" ; skill lu pendant l'échec" if used_here else
                                              " ; skill lu seulement au tour suivant")
                        out[name].append(Example(t.user_text, what, t.tools))
    return dict(out)


def split_turns(turns: list[TurnRecord]) -> tuple[list[TurnRecord], list[TurnRecord]]:
    """Deux moitiés disjointes PAR FIL (empreinte du fil) : réflexion (`D_feedback` de GEPA) et évaluation
    (`D_pareto`). Un même fil n'est jamais des deux côtés : la variante n'est pas notée sur les demandes qu'elle a lues."""
    import hashlib

    refl, held = [], []
    for t in turns:
        (held if hashlib.sha256(t.thread.encode()).digest()[0] % 2 else refl).append(t)
    return refl, held


def tasks_from_turns(target: str, turns: list[TurnRecord], units: Iterable[str]) -> list[dict[str, Any]]:
    """Tâches de choix tirées des traces (banc `trace-choice`) : demande → outil (ou skill) qui l'a servie. Tour
    réussi à un seul outil (ou skill lu) ; tour raté rattrapé au tour suivant par un outil absent du tour raté."""
    keys = set(units)
    out: list[dict[str, Any]] = []
    by_thread: dict[str, list[TurnRecord]] = defaultdict(list)
    for t in turns:
        by_thread[t.thread].append(t)
    for seq in by_thread.values():
        for i, t in enumerate(seq):
            used = sorted(_skills_read(t)) if target == "skills" else list(dict.fromkeys(
                n for n in t.tools if ":" not in n))
            if not t.user_text:
                continue
            if not t.failed and len(used) == 1 and used[0] in keys:
                out.append({"id": f"tr-{len(out):03d}", "text": t.user_text[:500], "expect": used[0]})
                continue
            nxt = seq[i + 1] if i + 1 < len(seq) and seq[i + 1].turn == t.turn + 1 else None
            if t.failed and nxt is not None and not nxt.failed:
                later = sorted(_skills_read(nxt)) if target == "skills" else [
                    n for n in dict.fromkeys(nxt.tools) if ":" not in n and n not in t.tools]
                if len(later) == 1 and later[0] in keys:
                    out.append({"id": f"tr-{len(out):03d}", "text": t.user_text[:500], "expect": later[0]})
    return out


def _skills_read(t: TurnRecord) -> set[str]:
    # `turns_from_lines` ajoute `skill_view:<nom>` pour chaque lecture de skill (argument `name` de l'appel).
    return {n.split(":", 1)[1] for n in t.tools if n.startswith("skill_view:")}


__all__ = [
    "FAIL_SIGNALS",
    "Example",
    "TurnRecord",
    "failures_for",
    "iter_lines",
    "split_turns",
    "tasks_from_turns",
    "turns_from_lines",
]
