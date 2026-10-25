"""Juge allumé dans Lokkal (X8, docs/LOKKAL.md §7.17) : relecture du diff avant « terminé », avis sur les cartes fortes.

Deux rôles, chacun derrière son drapeau (`judge_review`, `judge_advice`, éteints par défaut dans le code), sur le
modèle du juge (`judge:` de la config ; absent : le palier Ship, un seul modèle) :

  - **relecture** (`DiffReviewer`) : quand un tour a écrit dans le dossier de travail (`write_file`, `patch` réussis),
    le juge relit le diff du tour avant que la réponse ne parte. Il répond `OK` ou `À CORRIGER` suivi d'au plus cinq
    remarques. « À corriger » renvoie le tour à l'auteur (Deep) avec les remarques, `rounds` fois au plus par tour
    (défaut 1) ; ensuite la réponse part telle quelle. Le juge voit le CONTENU réel (le diff et la sortie des
    dernières commandes), jamais un compte rendu de l'auteur (leçon d'un banc
    interne). Il conseille, il ne bloque pas : un juge qui tombe ne relit pas, le tour
    finit normalement ;
  - **avis** (`CardAdvisor`) : sur une carte d'approbation FORTE (`Verdict.strength == "strong"`), un avis court et un
    niveau de risque (`low` | `medium` | `high`), format attendu par X10 (carte au téléphone). Rangé dans
    `Approval.advice` sous la forme `[risk:<niveau>] <texte>` (`format_advice` / `parse_advice`), sans migration :
    la colonne `advice` est du texte ; l'API rend `advice` (le texte) et `advice_risk` (le niveau) séparés.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from nokker.core.turn import Turn

logger = logging.getLogger("nokker.qa.review")

WRITE_TOOLS = ("write_file", "patch")
RISKS = ("low", "medium", "high")
_RISK_FR = {"faible": "low", "bas": "low", "low": "low", "moyen": "medium", "modéré": "medium", "medium": "medium",
            "élevé": "high", "eleve": "high", "fort": "high", "high": "high", "critique": "high"}
RISK_LABEL_FR = {"low": "faible", "medium": "moyen", "high": "élevé"}


# ─────────────────────────────── diff du tour ───────────────────────────────

def _result_ok(content: str) -> bool:
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        return not (content or "").startswith(("REFUSÉ", "EN ATTENTE", "ESCALADÉ", "argument manquant"))
    return not (isinstance(data, dict) and data.get("error"))


def turn_diff(turn: Turn, max_chars: int = 24000) -> str:
    """Le diff des écritures RÉUSSIES de ce tour, reconstruit depuis les appels (le juge relit ce que l'auteur a
    réellement envoyé à l'outil, pas son résumé) : `patch` mode replace → bloc -/+, mode patch → le patch V4A,
    `write_file` → le fichier écrit (tronqué). Vide : rien n'a été écrit."""
    results = {m.tool_call_id: m.content for m in turn.messages if m.role == "tool"}
    parts: list[str] = []
    for m in turn.messages:
        for tc in m.tool_calls:
            if tc.name not in WRITE_TOOLS or not _result_ok(results.get(tc.id, "")):
                continue
            a = tc.arguments or {}
            if tc.name == "write_file":
                body = str(a.get("content") or "")
                parts.append(f"=== write_file {a.get('path')} ({len(body)} car.)\n" +
                             "\n".join("+" + line for line in body.splitlines()))
            elif str(a.get("mode") or "replace") == "patch":
                parts.append(f"=== patch V4A\n{a.get('patch') or ''}")
            else:
                old = "\n".join("-" + line for line in str(a.get("old_string") or "").splitlines())
                new = "\n".join("+" + line for line in str(a.get("new_string") or "").splitlines())
                parts.append(f"=== patch {a.get('path')}" + (" (toutes les occurrences)" if a.get("replace_all")
                                                            else "") + f"\n{old}\n{new}")
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n[… diff tronqué : {len(text) - max_chars} caractères de plus]"
    return text


def last_commands(turn: Turn, n: int = 2, max_chars: int = 3000) -> str:
    """Sortie des `n` dernières commandes du tour (les tests de l'auteur), bornée : le juge voit ce qu'elles ont dit."""
    calls = {tc.id: tc for m in turn.messages for tc in m.tool_calls if tc.name == "terminal"}
    outs = [(calls[m.tool_call_id], m.content) for m in turn.messages
            if m.role == "tool" and m.tool_call_id in calls]
    lines = []
    for tc, content in outs[-n:]:
        lines.append(f"$ {(tc.arguments or {}).get('command', '')}\n{(content or '')[-max_chars // max(n, 1):]}")
    return "\n".join(lines)


REVIEW_PROMPT = """Tu relis le travail d'un agent de code avant qu'il annonce « terminé ». Tu ne réécris pas le code.

Demande de l'utilisateur :
{request}

Diff du tour (ce que l'agent a écrit) :
{diff}

Dernières commandes lancées par l'agent et leur sortie :
{commands}

Le diff fait-il ce qui est demandé, sans erreur évidente (bug, cas oublié, test supprimé ou affaibli, fichier hors
sujet, code laissé à moitié) ? Réponds EXACTEMENT ainsi :
- première ligne : OK ou À CORRIGER
- si À CORRIGER : au plus cinq lignes « - fichier : problème précis », sans réécrire le code.
Ne demande pas de correction de style. Dans le doute sur un point mineur, réponds OK."""


@dataclass
class ReviewResult:
    ok: bool
    remarks: str
    model: str
    latency_s: float
    raw: str = ""
    usage: dict[str, Any] = field(default_factory=dict)  # `usage` de l'amont (jetons du juge, comptés au tour)


def parse_review(text: str) -> tuple[bool | None, str]:
    """(ok, remarques) ; ok=None si la première ligne n'est ni OK ni À CORRIGER (le tour finit sans relecture)."""
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    if not lines:
        return None, ""
    head = lines[0].upper().replace("**", "").strip(" .:*#")
    if head.startswith(("À CORRIGER", "A CORRIGER")):
        return False, "\n".join(lines[1:6])[:1200]
    if head.startswith("OK"):
        return True, ""
    return None, ""


@dataclass
class DiffReviewer:
    transport: Any  # qa.judge.JudgeTransport
    model_name: str = "judge"
    rounds: int = 1  # renvois à l'auteur par tour, au plus
    tier: Any = None  # palier sous lequel ses jetons sont comptés (celui dont le modèle sert de juge)
    max_tokens: int = 400  # remarques courtes ; plus grand pour un juge qui raisonne d'abord (gpt-oss)
    max_diff_chars: int = 24000

    def review(self, turn: Turn) -> ReviewResult | None:
        """None : rien à relire (aucune écriture réussie), ou juge indisponible / réponse illisible."""
        diff = turn_diff(turn, self.max_diff_chars)
        if not diff:
            return None
        prompt = REVIEW_PROMPT.format(request=(turn.input.text or "")[:4000], diff=diff,
                                      commands=last_commands(turn) or "(aucune)")
        t0 = time.monotonic()
        try:
            data = self.transport.post({"messages": [{"role": "user", "content": prompt}],
                                        "max_tokens": self.max_tokens, "temperature": 0})
        except Exception as e:  # noqa: BLE001 — un juge qui tombe ne relit pas, le tour finit
            logger.warning("relecture : juge indisponible (%s)", e)
            return None
        text = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        ok, remarks = parse_review(text)
        if ok is None:
            logger.warning("relecture : réponse illisible du juge (%r)", text[:120])
            return None
        return ReviewResult(ok=ok, remarks=remarks, model=data.get("model") or self.model_name,
                            latency_s=time.monotonic() - t0, raw=text[:2000], usage=data.get("usage") or {})


REVIEW_NOTE = ("[relecture] Le relecteur a relu ton diff avant « terminé » et signale :\n{remarks}\n"
               "Corrige ce qui est fondé (puis relance les tests), ou dis en une ligne pourquoi ce n'en est pas un. "
               "Ne recommence pas le travail.")

# Réponse qui ne peut plus être corrigée (conclusion forcée, dernier appel) : les remarques suivent la réponse, pour
# l'utilisateur (lot fix-batch-1). Jamais de renvoi à l'auteur à ce stade.
FINAL_REVIEW_NOTE = ("\n\n---\n**Relecture du diff : à vérifier, non corrigé** ({why}). Le relecteur signale :\n"
                     "{remarks}")
NOT_REVIEWED_NOTE = ("\n\n---\n**Le diff de ce tour n'a pas été relu** (relecteur indisponible) : vérifie-le avant "
                     "de t'y fier.")


# ─────────────────────────────── avis sur les cartes fortes ───────────────────────────────

ADVICE_PROMPT = """Une action d'un agent attend l'approbation de son utilisateur (carte forte : effet difficile à défaire).

Demande de l'utilisateur dans ce tour :
{request}

Action suspendue : {tool}
Arguments : {arguments}
Motif de la carte : {reason}

Réponds EXACTEMENT sur deux lignes :
RISQUE: faible, moyen ou élevé
AVIS: une ou deux phrases pour l'utilisateur (ce que fait l'action, le risque principal, comment revenir en arrière)"""


def format_advice(risk: str | None, text: str) -> str:
    """Rangement dans `Approval.advice` (TEXT) : `[risk:high] texte`. Sans niveau lisible : le texte seul."""
    text = (text or "").strip()
    return f"[risk:{risk}] {text}" if risk in RISKS else text


def parse_advice(advice: str | None) -> tuple[str | None, str | None]:
    """(niveau, texte) d'un `Approval.advice` ; un avis d'avant X8 (texte libre) rend (None, texte)."""
    if not advice:
        return None, advice
    m = re.match(r"\[risk:(low|medium|high)\]\s*(.*)$", advice, re.DOTALL)
    return (m.group(1), m.group(2).strip()) if m else (None, advice)


def parse_advice_reply(text: str) -> tuple[str | None, str]:
    risk = None
    avis = ""
    for line in (text or "").splitlines():
        s = line.strip().replace("**", "")
        low = s.lower()
        if low.startswith("risque") and ":" in s:
            word = s.split(":", 1)[1].strip().strip(".").lower().split()
            risk = _RISK_FR.get(word[0]) if word else None
        elif low.startswith("avis") and ":" in s:
            avis = s.split(":", 1)[1].strip()
    if not avis:  # réponse hors format : le texte sans la ligne de risque
        avis = " ".join(ln.strip() for ln in (text or "").splitlines() if not ln.lower().strip().startswith("risque"))
    return risk, avis[:400]


@dataclass
class CardAdvisor:
    transport: Any  # qa.judge.JudgeTransport
    model_name: str = "judge"
    max_tokens: int = 160
    strengths: tuple[str, ...] = ("strong",)  # cartes qui reçoivent un avis
    tier: Any = None  # palier sous lequel ses jetons sont comptés
    last_latency_s: float = field(default=0.0, init=False)
    last_usage: dict[str, Any] = field(default_factory=dict, init=False)  # dernier appel (jetons comptés au tour)
    last_model: str = field(default="", init=False)

    def advise(self, turn: Turn, tool_name: str, arguments: dict[str, Any], reason: str,
               strength: str | None) -> str | None:
        if (strength or "standard") not in self.strengths:
            return None
        args = json.dumps(arguments or {}, ensure_ascii=False)[:1500]
        prompt = ADVICE_PROMPT.format(request=(turn.input.text or "")[:2000], tool=tool_name, arguments=args,
                                      reason=(reason or "")[:300])
        t0 = time.monotonic()
        self.last_usage, self.last_model = {}, ""
        try:
            data = self.transport.post({"messages": [{"role": "user", "content": prompt}],
                                        "max_tokens": self.max_tokens, "temperature": 0})
        except Exception as e:  # noqa: BLE001 — une carte sans avis reste une carte
            logger.warning("avis de carte : juge indisponible (%s)", e)
            return None
        finally:
            self.last_latency_s = time.monotonic() - t0
        self.last_usage, self.last_model = data.get("usage") or {}, data.get("model") or self.model_name
        text = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        if not text:
            return None
        risk, avis = parse_advice_reply(text)
        return format_advice(risk, avis) if avis else None


__all__ = [
    "ADVICE_PROMPT",
    "FINAL_REVIEW_NOTE",
    "NOT_REVIEWED_NOTE",
    "REVIEW_NOTE",
    "REVIEW_PROMPT",
    "RISKS",
    "RISK_LABEL_FR",
    "CardAdvisor",
    "DiffReviewer",
    "ReviewResult",
    "format_advice",
    "last_commands",
    "parse_advice",
    "parse_advice_reply",
    "parse_review",
    "turn_diff",
]
