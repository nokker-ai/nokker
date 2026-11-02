"""Le Guide (docs/GUIDE.md) : sur-agent qui apprend à la personne ce que Nokker sait faire.

- `view` : statut de chaque fonction POUR CE COMPTE (codes), compteurs d'usage, tour d'accueil (3 fonctions au plus
  selon le profil), fonction suivante proposée — sans appel modèle.
- `ask` : une question libre (« qu'est-ce que tu sais faire ? », « comment j'active X ? »), un appel modèle sur le
  palier le moins cher, avec le catalogue rendu pour ce compte ; la réponse cite des identifiants, le noyau y joint
  les codes (écran, élément à montrer, commande).
- `observe` : observateur du bus d'événements, compte un usage par code de fonction (outil ou événement), sans
  contenu.

Le Guide n'active RIEN : aucun outil, aucune écriture hors de ses compteurs. Un réglage du compte se change à l'écran
(lien profond) ou par une carte existante.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.guide.catalog import (
    CATALOG,
    CLOUD,
    HOUSEHOLD,
    LOKKAL,
    SOLO,
    Feature,
    by_id,
    flag_on,
    render_for_model,
)
from nokker.guide.store import INTRO_KEY
from nokker.guide.texts import GUIDE_SYSTEM, NONE_FR, PROFILE_FR, SEE_MARKER

logger = logging.getLogger("nokker.guide")

AVAILABLE = ("on", "setup")
INTRO_SIZE = 3
MAX_SEE = 3
HISTORY_MESSAGES = 6
HISTORY_CHARS = 1000
QUESTION_CHARS = 1000


@dataclass(frozen=True)
class AccountFacts:
    """Ce que le noyau sait du compte, en codes : membre d'un foyer, profil mineur, Telegram relié."""

    household: bool = False
    minor: bool = False
    telegram: bool = False


class GuideRefused(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code = status, code


@dataclass
class GuideService:
    config: Any
    usage: Any
    facts_of: Callable[[str | None], AccountFacts] = lambda sender: AccountFacts()
    transport: Any = None  # `post(body) -> dict` (HttpJudgeTransport) ; None : `ask` répond 503
    model_name: str = ""
    max_tokens: int = 400
    max_asks_per_day: int = 40
    scrub_in: Callable[[str], str] = lambda s: s
    scrub_out: Callable[[str], str] = lambda s: s
    # (tenant, model, usage dict, latence) → appel compté au ledger ; None : rien.
    on_call: Callable[[str, str, dict, float], None] | None = None
    clock: Callable[[], float] = time.time
    _asks: dict[tuple[str, str], int] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _signals: dict[str, tuple[str, ...]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        sig: dict[str, list[str]] = {}
        for f in CATALOG:
            for s in f.signals:
                sig.setdefault(s, []).append(f.id)
        self._signals = {k: tuple(v) for k, v in sig.items()}

    # ── statuts ──

    @property
    def target(self) -> str:
        return LOKKAL if getattr(self.config, "local", None) is not None else CLOUD

    @property
    def show_me(self) -> bool:
        return bool(getattr(self.config, "show_me", False))

    def profile(self, facts: AccountFacts) -> str:
        if self.target == LOKKAL:
            return LOKKAL
        return HOUSEHOLD if facts.household else SOLO

    def status(self, f: Feature, facts: AccountFacts) -> str:
        """`on` (utilisable), `setup` (allumé, un geste du compte manque), `off` (pas sur cette installation),
        `not_for_you` (refusé à ce profil, ex. mineur)."""
        if self.target not in f.targets or not all(flag_on(self.config, fl) for fl in f.flags):
            return "off"
        if f.adult_only and facts.minor:
            return "not_for_you"
        if any(not getattr(facts, n, False) for n in f.needs):
            return "setup"
        return "on"

    def statuses(self, facts: AccountFacts) -> list[tuple[Feature, str]]:
        return [(f, self.status(f, facts)) for f in CATALOG]

    def codes(self, f: Feature, status: str) -> dict[str, Any]:
        """Ce que le client reçoit d'une fonction : des codes, jamais une phrase (le web traduit d'après `id`)."""
        out: dict[str, Any] = {"id": f.id, "status": status}
        if status in AVAILABLE:
            if f.screen:
                out["screen"] = f.screen
            if f.ui and self.show_me:
                out["ui"] = f.ui
            if f.command:
                out["command"] = f.command
        return out

    # ── vue ──

    def _unused(self, statuses: list[tuple[Feature, str]], counts: dict[str, int], profile: str) -> list[Feature]:
        avail = [f for f, s in statuses if s in AVAILABLE and not counts.get(f.id)]
        return sorted(avail, key=lambda f: (profile not in f.profiles, f.intro_rank))

    def view(self, sender: str | None) -> dict[str, Any]:
        facts = self.facts_of(sender)
        statuses = self.statuses(facts)
        counts = self.usage.counts()
        profile = self.profile(facts)
        unused = self._unused(statuses, counts, profile)
        picks = [f.id for f in unused if profile in f.profiles][:INTRO_SIZE]
        return {
            "profile": profile,
            "show_me": self.show_me,
            "features": [{**self.codes(f, s), "used": int(counts.get(f.id, 0))} for f, s in statuses],
            "intro": {"seen": bool(counts.get(INTRO_KEY)), "picks": picks},
            "next": unused[0].id if unused else None,
            "catalog_tokens": self.catalog_tokens(statuses),
        }

    def mark_intro_seen(self) -> None:
        self.usage.bump(INTRO_KEY)

    def catalog_tokens(self, statuses: list[tuple[Feature, str]]) -> int:
        """Estimation (4 caractères par jeton) du bloc envoyé au modèle pour ce compte : le budget annoncé."""
        return (len(GUIDE_SYSTEM) + len(render_for_model(statuses)) + 200) // 4

    # ── usage (observateur du bus) ──

    def observe(self, event_type: str, data: dict[str, Any]) -> None:
        name = str(data.get("name") or "") if event_type == "tool.call" else event_type
        ids = self._signals.get(name)
        if not ids:
            return
        tenant = data.get("_tenant")
        if not tenant:
            return
        from nokker.tenancy.context import tenant_scope

        try:
            with tenant_scope(str(tenant)):
                for fid in ids:
                    self.usage.bump(fid)
        except Exception:
            logger.warning("guide : usage non compté", exc_info=True)

    # ── question libre ──

    def system_prompt(self, facts: AccountFacts, statuses: list[tuple[Feature, str]], counts: dict[str, int]) -> str:
        profile = self.profile(facts)
        unused = [f.id for f in self._unused(statuses, counts, profile)][:8]
        return GUIDE_SYSTEM.format(profile=PROFILE_FR[profile], unused=", ".join(unused) or NONE_FR,
                                   catalog=render_for_model(statuses))

    def _take_ask(self, tenant: str) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime(self.clock()))
        with self._lock:
            n = self._asks.get((tenant, day), 0)
            if n >= self.max_asks_per_day:
                raise GuideRefused(429, "guide_quota", "trop de questions au Guide aujourd'hui")
            self._asks = {k: v for k, v in self._asks.items() if k[1] == day}
            self._asks[(tenant, day)] = n + 1

    def ask(self, sender: str | None, tenant: str, text: str, history: Any = None) -> dict[str, Any]:
        question = self.scrub_in(str(text or "").strip()[:QUESTION_CHARS])
        if not question:
            raise GuideRefused(400, "empty", "question vide")
        if self.transport is None:
            raise GuideRefused(503, "guide_model_missing", "aucun modèle pour le Guide")
        self._take_ask(tenant)
        facts = self.facts_of(sender)
        statuses = self.statuses(facts)
        counts = self.usage.counts()
        messages: list[dict[str, str]] = [{"role": "system", "content": self.system_prompt(facts, statuses, counts)}]
        for m in (history if isinstance(history, list) else [])[-HISTORY_MESSAGES:]:
            if isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
                messages.append({"role": m["role"], "content": self.scrub_in(m["content"][:HISTORY_CHARS])})
        messages.append({"role": "user", "content": question})
        t0 = time.monotonic()
        data = self.transport.post({"messages": messages, "max_tokens": self.max_tokens, "temperature": 0.2})
        latency = time.monotonic() - t0
        if self.on_call is not None:
            try:
                self.on_call(tenant, str(data.get("model") or self.model_name), data.get("usage") or {}, latency)
            except Exception:
                logger.warning("guide : appel non compté au ledger", exc_info=True)
        raw = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        reply, ids = split_see(raw)
        known = by_id()
        status_of = {f.id: s for f, s in statuses}
        features = [self.codes(known[i], status_of[i]) for i in ids if i in known][:MAX_SEE]
        point = next(({"ui": c["ui"], "feature": c["id"]} for c in features if c.get("ui")), None)
        return {"reply": self.scrub_out(reply), "features": features, "point": point}


def split_see(raw: str) -> tuple[str, list[str]]:
    """Retire la ligne « VOIR: a, b » (la dernière qui commence ainsi) et rend les identifiants cités, dans l'ordre."""
    lines = raw.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].strip().strip("*_` ")
        if line.upper().startswith(SEE_MARKER):
            rest = line[len(SEE_MARKER):]
            ids = [x.strip().strip("*_`«» .").lower() for x in rest.replace(";", ",").split(",")]
            ids = list(dict.fromkeys(x for x in ids if x))
            return "\n".join(lines[:i] + lines[i + 1:]).strip(), ids
    return raw.strip(), []


def prefix_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = ["AVAILABLE", "AccountFacts", "GuideRefused", "GuideService", "split_see"]
