"""Garde-fous de la boucle (docs/SELF-IMPROVE.md §5), tous déterministes, appliqués AVANT d'évaluer une variante :

- `secrets`        : aucune valeur ni motif de secret dans le texte de la variante (caviardage du noyau,
                     `qa/secrets.redact` avec le coffre de la config) ; une poignée `[secret:…]` compte aussi ;
- `bancs`          : aucune suite de 8 mots en commun avec le texte d'un banc (`Corpus`) qui ne soit pas déjà dans le
                     texte de départ — une variante ne recopie jamais un banc, donc aucun banc ne remonte par elle dans
                     les traces, puis dans l'entraînement (exigence de la relecture du format : recouvrement nul) ;
- `locataires`     : aucun identifiant de compte, de locataire, de fil pseudonymisé ni adresse e-mail qui ne soit pas
                     déjà dans le texte de départ ;
- `skills`         : un mode d'emploi passe par le scanner des skills (`dangerous` ou scanner absent → refus, comme
                     `learning`, échec fermé).

Puis, APRÈS évaluation, `safety_regressions` : toute métrique de sûreté d'un banc (secrets, fuite entre locataires,
injections, GO…) plus basse que celle de la base élimine la variante, quel que soit son gain.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import yaml

NGRAM = 8
_WORD = re.compile(r"[0-9a-zà-ÿœæ]+", re.IGNORECASE)
_TENANT_RE = re.compile(r"\b(?:us_[0-9a-z]{6,}|tn_[0-9a-z]{6,}|t-[0-9a-f]{16})\b|[\w.+-]+@[\w-]+\.[\w.-]+")
_HANDLE_RE = re.compile(r"\[secret:[^\]\[]+\]")


def words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text or "")]


def ngrams(text: str, n: int = NGRAM) -> set[tuple[str, ...]]:
    w = words(text)
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


class Corpus:
    """Textes des bancs (demandes, réponses attendues, contenus scriptés) en n-grammes de 8 mots."""

    def __init__(self, texts: Iterable[str] = (), n: int = NGRAM) -> None:
        self.n = n
        self.grams: set[tuple[str, ...]] = set()
        self.texts = 0
        for t in texts:
            self.add(t)

    def add(self, text: str) -> None:
        g = ngrams(text, self.n)
        if g:
            self.grams |= g
            self.texts += 1

    def overlaps(self, text: str) -> bool:
        return bool(ngrams(text, self.n) & self.grams)

    def new_overlap(self, variant: str, base: str) -> int:
        """N-grammes du banc présents dans la variante et absents du texte de départ."""
        return len((ngrams(variant, self.n) & self.grams) - ngrams(base, self.n))

    @classmethod
    def from_files(cls, paths: Iterable[str | Path]) -> Corpus:
        """Toutes les chaînes des YAML/JSON de bancs donnés (dossiers parcourus), sauf les catalogues d'outils
        (`tools*.json` : ce sont les textes de DÉPART des outils, pas un banc)."""
        c = cls()
        for p in map(Path, paths):
            files = [p] if p.is_file() else sorted([*p.rglob("*.yaml"), *p.rglob("*.yml"), *p.rglob("*.json")])
            for f in files:
                if f.name.startswith("tools") or "results" in f.parts:
                    continue
                try:
                    data = yaml.safe_load(f.read_text(encoding="utf-8"))
                except (yaml.YAMLError, UnicodeDecodeError):
                    continue
                for s in _strings(data):
                    c.add(s)
        return c


def _strings(node: Any) -> Iterable[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def secret_names(text: str, known: Any = None) -> list[str]:
    """Noms des secrets trouvés (jamais les valeurs). Vide = propre."""
    from nokker.qa.secrets import redact

    names = list(redact(text, known, weak=True).names)
    if _HANDLE_RE.search(text):
        names.append("poignée de secret")
    return names


def tenant_markers(variant: str, base: str) -> list[str]:
    before = set(_TENANT_RE.findall(base or ""))
    return sorted({m for m in _TENANT_RE.findall(variant or "") if m not in before})


def check_variant(variant_text: str, base_text: str, *, corpus: Corpus | None, known: Any = None,
                  skill_scan: Callable[[str], str | None] | None = None) -> str | None:
    """Motif de rejet, ou None si la variante peut être évaluée. `skill_scan(texte)` rend un motif ou None."""
    found = secret_names(variant_text, known)
    if found:
        return f"secret dans la variante ({len(found)})"
    if corpus is not None:
        n = corpus.new_overlap(variant_text, base_text)
        if n:
            return f"recoupe un banc ({n} suite(s) de {corpus.n} mots)"
    leaked = tenant_markers(variant_text, base_text)
    if leaked:
        return f"identifiant de compte ou adresse ({len(leaked)})"
    if skill_scan is not None:
        why = skill_scan(variant_text)
        if why:
            return why
    return None


def default_skill_scan() -> Callable[[str], str | None]:
    """Le scanner des skills du noyau (même brique que `skill_install` et l'apprentissage) ; absent → refus."""
    def scan(content: str) -> str | None:
        from nokker.learning.skills import LearnedSkills, LearningError

        try:
            verdict, _ = LearnedSkills(store=None).scan("variante", content)
        except LearningError as e:
            return f"scanner des skills : {e.code}"
        return "scanner des skills : dangerous" if verdict == "dangerous" else None

    return scan


def safety_regressions(base: dict[str, float], child: dict[str, float]) -> list[str]:
    """Métriques de sûreté de la base que la variante fait baisser (ou fait disparaître : un banc de sûreté qui n'a pas
    tourné ne vaut pas un vert)."""
    out = []
    for k, v in base.items():
        got = child.get(k)
        if got is None or got < v - 1e-9:
            out.append(f"{k} {v:.3f} → {'absent' if got is None else f'{got:.3f}'}")
    return out


__all__ = [
    "NGRAM",
    "Corpus",
    "check_variant",
    "default_skill_scan",
    "ngrams",
    "safety_regressions",
    "secret_names",
    "tenant_markers",
    "words",
]
