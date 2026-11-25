"""Plafond de dépense par session (docs/NOKKAN-HOST-API.md §2, drapeau `host_session_budget`, éteint par défaut).

Le produit hôte pose un plafond sur un fil de session, à l'ouverture ou en cours (`PUT /v1/local/sessions/{fil}/budget`).
Le plafond se décompte sur le LEDGER, aucun second compteur : `turn_cost.ledger_totals(fil)` relit les appels modèle du
fil, sous-agents compris (`<fil>/sa-…`), comme `GET /v1/local/cost`.

**Unités** (ce que le ledger compte réellement) :
- `max_total_tokens` : jetons d'entrée + de sortie de tous les appels du fil (`total_tokens`, toujours connu) ;
- `max_cost_chf` ou `max_cost_usd` (l'un OU l'autre) : coût des appels, d'après `usage.cost` du Router quand il est
  donné, sinon d'après le catalogue de prix. Le ledger compte en CHF ; un plafond en USD est comparé au coût converti
  au taux du serveur (`billing_usd_chf`, `turn_cost.usd_chf`). Jamais un prix ni un taux inventé : si un appel du fil
  n'a pas de prix (modèle hors catalogue, ex. un modèle local) ou une devise sans taux, un plafond de COÛT est
  invérifiable et le tour est refusé par prudence (`session_budget_unverifiable`). Pour un modèle sans prix, plafonner
  en jetons.

**Refus** : vérifié AVANT le tour (`Kernel.run_turn`), comme le budget d'un projet : aucun appel modèle, tour gardé
dans le fil avec son verdict `session_budget:<motif>`, et un échec au code stable pour l'hôte (`turn.done.error`,
`nokker.error`) qui dit de ne pas réessayer. Un tour commencé sous le plafond va à son terme ; le suivant est refusé.
Un fil de sous-agent est jugé sur le plafond du fil de sa session.

Les plafonds sont gardés sur disque quand un chemin est donné (`<state_dir>/session-budgets.json`, 0600) : un
redémarrage ne les lève pas.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nokker.core.turn import Decision, Gate, Verdict
from nokker.ledger import turn_cost

logger = logging.getLogger("nokker.ledger.session_budget")

# Codes stables de l'échec (`turn.done.error.code`, `nokker.error.code`) : pour l'hôte, jamais vus par le modèle.
EXHAUSTED = "session_budget_exhausted"
UNVERIFIABLE = "session_budget_unverifiable"
UNAVAILABLE = "session_budget_unavailable"

# ─────────────── textes gardés dans le fil (gelés : docs/nokkan-host-api-textes.md) ───────────────
# Même mécanisme de langue que les refus de budget de projet (`nokker/team/budget.py::REPLY`) : UN texte, le français
# puis l'anglais, séparés par une ligne vide ; `{…_en}` pour ce qui se dit autrement en anglais (comme `{label_en}`).
REPLY_EXHAUSTED = ("Plafond de dépense de cette session atteint ({spent} sur {cap}). Rien n'a été envoyé au modèle ; "
                   "la conversation est gardée.\n\n"
                   "Spending cap of this session reached ({spent_en} out of {cap_en}). Nothing was sent to the model; "
                   "your conversation is kept.")
REPLY_UNVERIFIABLE = ("Plafond de dépense de cette session invérifiable pour l'instant. Par prudence, rien n'a été "
                      "envoyé au modèle ; la conversation est gardée.\n\n"
                      "The spending cap of this session cannot be checked right now. To be safe, nothing was sent to "
                      "the model; your conversation is kept.")
# ──────────────────────────────────────────────────────────────────────────────────────────────

LIMIT_KEYS = ("max_cost_chf", "max_cost_usd", "max_total_tokens")
MAX_TOKENS_CAP = 10**12
MAX_COST_CAP = 10**9


class BudgetError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class Refusal:
    verdict: Verdict
    reply: str
    failure: dict[str, Any]


def parse_limits(body: Any) -> dict[str, Any]:
    """Plafonds d'un corps de requête → forme gardée `{max_cost, currency, max_total_tokens}` (None = pas de plafond de
    ce genre). `null` ou clé absente : pas de plafond ; tout à null : la session n'a plus de plafond. Lève
    `BudgetError` (400) sur une clé inconnue, une valeur non numérique, négative, ou deux devises à la fois."""
    if not isinstance(body, dict):
        raise BudgetError("bad_budget", "budget : objet attendu")
    unknown = sorted(k for k in body if k not in LIMIT_KEYS)
    if unknown:
        raise BudgetError("bad_budget", f"budget : clé inconnue {unknown[0]!r} (parmi {', '.join(LIMIT_KEYS)})")

    def number(key: str, top: float, whole: bool) -> float | int | None:
        v = body.get(key)
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 or v > top \
                or (whole and v != int(v)):
            raise BudgetError("bad_budget", f"{key} : nombre {'entier ' if whole else ''}positif attendu")
        return int(v) if whole else float(v)

    chf, usd = number("max_cost_chf", MAX_COST_CAP, False), number("max_cost_usd", MAX_COST_CAP, False)
    if chf is not None and usd is not None:
        raise BudgetError("bad_budget", "max_cost_chf et max_cost_usd : un seul plafond de coût")
    return {"max_cost": chf if chf is not None else usd,
            "currency": "USD" if usd is not None else ("CHF" if chf is not None else None),
            "max_total_tokens": number("max_total_tokens", MAX_TOKENS_CAP, True)}


def _public(limits: dict[str, Any] | None) -> dict[str, Any]:
    lim = limits or {}
    cur = lim.get("currency")
    return {"max_cost_chf": lim.get("max_cost") if cur == "CHF" else None,
            "max_cost_usd": lim.get("max_cost") if cur == "USD" else None,
            "max_total_tokens": lim.get("max_total_tokens")}


def _money(amount: float, currency: str) -> str:
    return f"{amount:.2f} {currency}"


@dataclass
class SessionBudgets:
    ledger: Any  # ledger avec `fetch_calls(thread_id=…, include_subagents=True)` (InMemoryLedger, PostgresLedger)
    prices: Any = None  # PriceCatalog ; None = celui du ledger
    path: Path | None = None  # fichier des plafonds ; None = mémoire du processus seulement
    usd_chf: Callable[[], float] = turn_cost.usd_chf
    _limits: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _broken: bool = field(default=False, init=False, repr=False)  # fichier des plafonds illisible au chargement

    def __post_init__(self) -> None:
        if self.prices is None:
            self.prices = getattr(self.ledger, "prices", None)
        if self.path is not None:
            self._load()

    # ── plafonds ──
    def _load(self) -> None:
        try:
            raw = json.loads(Path(self.path).read_text(encoding="utf-8"))  # type: ignore[arg-type]
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            # Fichier illisible : on ne lève pas les plafonds en silence. Tout fil est refusé tant qu'il n'est pas
            # réparé ou reposé (`check` : `session_budget_unavailable`).
            logger.error("plafonds de session illisibles (%s) : tours refusés jusqu'à réparation", self.path)
            self._broken = True
            return
        if isinstance(raw, dict):
            self._limits = {str(k): v for k, v in raw.items() if isinstance(v, dict)}

    def _save_locked(self) -> None:
        if self.path is None:
            return
        p = Path(self.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self._limits, f, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, p)

    def set(self, thread_id: str, body: Any) -> dict[str, Any]:
        """Pose (ou retire, tout à null) le plafond du fil. Rend l'état (`status`)."""
        limits = parse_limits(body)
        root = turn_cost.root_thread(thread_id)
        with self._lock:
            if limits["max_cost"] is None and limits["max_total_tokens"] is None:
                self._limits.pop(root, None)
            else:
                self._limits[root] = limits
            self._save_locked()
            self._broken = False  # un fichier réécrit en entier est de nouveau lisible
        return self.status(root)

    def limits_of(self, thread_id: str) -> dict[str, Any] | None:
        with self._lock:
            got = self._limits.get(turn_cost.root_thread(thread_id))
            return dict(got) if got else None

    # ── dépense ──
    def _spent(self, root: str, currency: str | None) -> dict[str, Any]:
        totals = turn_cost.ledger_totals(self.ledger, self.prices, thread_id=root)
        costs: dict[str, float] = dict(totals.get("cost") or {})
        unpriced = list(totals.get("unpriced_models") or [])
        partial = any("partial" in str(m.get("price_source") or "") for m in totals.get("models") or [])
        cost: float | None = None
        if currency is not None:
            rate = float(self.usd_chf())
            total, known = 0.0, True
            for cur, amount in costs.items():
                if cur == currency:
                    total += amount
                elif {cur, currency} == {"USD", "CHF"} and rate > 0:
                    total += amount * rate if currency == "CHF" else amount / rate
                else:
                    known = False
            cost = round(total, turn_cost.AMOUNT_DIGITS) if known else None
        return {"calls": totals["calls"], "turns": totals["turns"], "total_tokens": totals["total_tokens"],
                "input_tokens": totals["input_tokens"], "output_tokens": totals["output_tokens"],
                "cost_chf": totals["cost_chf"], "cost": cost, "currency": currency,
                "unpriced_models": unpriced,
                "cost_verifiable": cost is not None and not unpriced and not partial}

    def _judge(self, limits: dict[str, Any], spent: dict[str, Any]) -> str | None:
        """Motif du refus (`tokens`, `cost`, `unverifiable`), ou None si le fil est sous ses plafonds."""
        cap_tokens, cap_cost = limits.get("max_total_tokens"), limits.get("max_cost")
        if cap_tokens is not None and spent["total_tokens"] >= cap_tokens:
            return "tokens"
        if cap_cost is not None:
            if spent["cost"] is not None and spent["cost"] >= cap_cost:
                return "cost"  # déjà au plafond sur la partie connue : inutile d'attendre un prix manquant
            if spent["calls"] and not spent["cost_verifiable"]:
                return "unverifiable"
        return None

    def status(self, thread_id: str) -> dict[str, Any]:
        """État pour l'hôte : plafonds, dépense relue au ledger, `exhausted` et son motif."""
        root = turn_cost.root_thread(thread_id)
        limits = self.limits_of(root)
        spent = self._spent(root, (limits or {}).get("currency"))
        reason = self._judge(limits, spent) if limits else None
        return {"thread_id": root, "limits": _public(limits),
                "spent": {"total_tokens": spent["total_tokens"], "input_tokens": spent["input_tokens"],
                          "output_tokens": spent["output_tokens"], "calls": spent["calls"], "turns": spent["turns"],
                          # Coût : None quand il n'est pas connu en entier (un appel sans prix), jamais un zéro inventé
                          # (essai réel du 11.11 sur un modèle local hors catalogue : 0.0 affiché à tort).
                          "cost_chf": spent["cost_chf"] if not spent["unpriced_models"] else None,
                          "cost_usd": spent["cost"] if spent["currency"] == "USD" and spent["cost_verifiable"]
                          else None,
                          "unpriced_models": spent["unpriced_models"]},
                "exhausted": reason is not None,
                "reason": {None: None, "tokens": EXHAUSTED, "cost": EXHAUSTED, "unverifiable": UNVERIFIABLE}[reason]}

    # ── porte d'avant-tour ──
    def check(self, thread_id: str) -> Refusal | None:
        """Appelé par `Kernel.run_turn` avant tout appel modèle. None : le tour part (pas de plafond, ou dessous)."""
        root = turn_cost.root_thread(thread_id)
        limits = self.limits_of(root)
        if limits is None and not self._broken:
            return None
        try:
            if self._broken:
                raise RuntimeError("fichier des plafonds illisible")
            spent = self._spent(root, limits.get("currency"))
            reason = self._judge(limits, spent)
        except Exception:  # noqa: BLE001 — ledger illisible : on ne dépense pas à l'aveugle
            logger.warning("plafond de session invérifiable (fil %s)", root, exc_info=True)
            return self._refuse(root, "unavailable", REPLY_UNVERIFIABLE, UNAVAILABLE, retry=True)
        if reason is None:
            return None
        if reason == "unverifiable":
            return self._refuse(root, reason, REPLY_UNVERIFIABLE, UNVERIFIABLE, retry=False)
        if reason == "tokens":
            used, cap = spent["total_tokens"], limits["max_total_tokens"]
            text = REPLY_EXHAUSTED.format(spent=f"{used} jetons", cap=f"{cap} jetons",
                                          spent_en=f"{used} tokens", cap_en=f"{cap} tokens")
        else:
            used, cap = _money(spent["cost"], limits["currency"]), _money(limits["max_cost"], limits["currency"])
            text = REPLY_EXHAUSTED.format(spent=used, cap=cap, spent_en=used, cap_en=cap)
        return self._refuse(root, reason, text, EXHAUSTED, retry=False, limit=reason)

    @staticmethod
    def _refuse(root: str, reason: str, reply: str, code: str, *, retry: bool, limit: str | None = None) -> Refusal:
        verdict = Verdict(gate=Gate.SWITCH, decision=Decision.DENY, layer=0, rule=f"session_budget:{reason}",
                          reason=f"plafond de la session {root} ({reason}) : tour refusé avant l'appel modèle",
                          subject="reply")
        failure: dict[str, Any] = {"code": code, "retry": retry,
                                   "message": "plafond de dépense de la session : aucun appel modèle"}
        if limit is not None:
            failure["limit"] = limit  # `tokens` | `cost`
        return Refusal(verdict=verdict, reply=reply, failure=failure)


__all__ = ["BudgetError", "EXHAUSTED", "LIMIT_KEYS", "Refusal", "SessionBudgets", "UNAVAILABLE", "UNVERIFIABLE",
           "parse_limits"]
