"""Palier « nina » : le modèle maison, À PART de Ship et Deep, éteint par défaut (docs/LOKKAL.md §7.17 point 13).

Deux modes, choisis par `route.nina.mode` (`off` par défaut) :

  - **`shadow` (ombre)** : sur une fraction réglable (`shadow_fraction`) des tours du locataire INTERNE seulement (jamais
    un testeur), Nina reçoit, en tâche de fond, exactement les messages et les schémas d'outils du PREMIER appel du tour
    (après le filet de secrets du fil) et répond sans être montrée. Sa réponse et celle du palier réel sont écrites au
    format de l'export des traces (docs/TRACES.md §3), une ligne par tour, `meta.tier = "nina-ombre"`,
    `call_index = SHADOW_CALL_INDEX`. Aucun outil n'est exécuté pour Nina ; son erreur ou sa lenteur n'a aucun effet
    sur le tour réel (lancée APRÈS la réponse réelle, dans un pool borné : pool plein → tour non doublé) ;
  - **`route`** : le palier est servi par la politique (`PolicyV1.nina_kinds`) pour les types de tour confiés à Nina
    (`route/classify.py::turn_kind`) ; code, raisonnement long et rédaction restent à Ship/Deep.

Contraintes de service (modèle 70B, cache KV FP8, contexte 32 k, poids fusionné complet) : le palier porte
`max_context_tokens` (32 768) ; un appel qui ne tient pas (prompt estimé + réponse) n'est pas envoyé à Nina — en mode
`route`, le noyau le bascule sur Ship (`core/loop.py::_fit_context`) ; en ombre, le tour n'est pas doublé.
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from nokker.tenancy.context import INTERNAL_TENANT, current_tenant_or_none, submit_in_context, tenant_of_thread

logger = logging.getLogger("nokker.route.nina")

SHADOW_TIER = "nina-ombre"
# Index d'appel des lignes d'ombre dans `trace_calls` : hors de la plage des appels réels (≤ quelques dizaines par
# tour), pour ne jamais entrer en conflit avec la ligne de l'appel 0 (clé tenant, fil, tour, appel).
SHADOW_CALL_INDEX = 1000
NINA_MODES = ("off", "shadow", "route")
DEFAULT_MAX_CONTEXT = 32768


def estimate_prompt_tokens(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> int:
    """Estimation prudente (`len // 3`, plus haute que `len // 4` du contexte) : un dépassement doit basculer avant que
    l'amont ne refuse."""
    text = json.dumps(messages, ensure_ascii=False) + json.dumps(tools or [], ensure_ascii=False)
    return len(text) // 3


def fits(provider: Any, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
         max_tokens: int = 0) -> tuple[bool, int, int]:
    """(tient, jetons estimés, limite) pour un provider qui déclare `max_context_tokens` (0/absent = pas de limite)."""
    limit = int(getattr(provider, "max_context_tokens", 0) or 0)
    if limit <= 0:
        return True, 0, 0
    need = estimate_prompt_tokens(messages, tools) + int(max_tokens or getattr(provider, "max_tokens_default", 0) or 0)
    return need <= limit, need, limit


def _msg_dict(msg: Any) -> dict[str, Any]:
    d = msg if isinstance(msg, dict) else msg.to_openai()
    return json.loads(json.dumps(d, ensure_ascii=False, default=str))


@dataclass
class NinaShadow:
    """Voir l'en-tête du module. `sink` : un `TraceSink` (traces/recorder.py) ; `seal` : le caviardage du fil."""

    provider: Any
    sink: Any
    fraction: float = 0.0
    max_tokens: int = 1024
    seal: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None
    kernel_sha: str = ""
    target: str = "unknown"
    max_in_flight: int = 2
    rng: random.Random = field(default_factory=random.Random, repr=False)
    _pool: ThreadPoolExecutor | None = field(default=None, repr=False)
    _slots: threading.Semaphore | None = field(default=None, repr=False)
    stats: dict[str, int] = field(default_factory=lambda: {"offered": 0, "sent": 0, "skipped_busy": 0,
                                                           "skipped_context": 0, "errors": 0}, repr=False)

    def __post_init__(self) -> None:
        self.fraction = max(0.0, min(1.0, float(self.fraction)))
        self._pool = ThreadPoolExecutor(max_workers=max(1, self.max_in_flight), thread_name_prefix="nina-ombre")
        self._slots = threading.Semaphore(max(1, self.max_in_flight))

    @staticmethod
    def internal(turn: Any) -> bool:
        """Locataire interne seulement : le fil ET le locataire courant (jamais un testeur, même par erreur de fil)."""
        tenant = current_tenant_or_none()
        return tenant in (None, INTERNAL_TENANT) and tenant_of_thread(turn.thread_id) == INTERNAL_TENANT

    def maybe_run(self, turn: Any, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
                  real: Any, real_tier: str, real_model: str) -> bool:
        """Appelé après la réponse RÉELLE du premier appel d'un tour. Vrai si Nina a été lancée. Ne lève jamais."""
        try:
            if self.fraction <= 0 or not self.internal(turn) or self.rng.random() >= self.fraction:
                return False
            self.stats["offered"] += 1
            ok, need, limit = fits(self.provider, messages, tools, self.max_tokens)
            if not ok:
                self.stats["skipped_context"] += 1
                logger.info("nina ombre : contexte ~%d jetons > %d, tour non doublé", need, limit)
                return False
            if not self._slots.acquire(blocking=False):
                self.stats["skipped_busy"] += 1
                return False
            # Copies : rien de ce que tient la boucle n'est partagé avec la tâche de fond.
            msgs = json.loads(json.dumps(messages, ensure_ascii=False, default=str))
            tls = json.loads(json.dumps(tools or [], ensure_ascii=False, default=str))
            real_msg = _msg_dict(real.message)
            meta = {"thread_id": turn.thread_id, "turn_index": turn.index, "account_id": turn.sender,
                    "channel": getattr(turn, "channel", ""), "real": {
                        "tier": real_tier, "model": real_model, "latency_s": round(float(real.latency_s or 0.0), 3),
                        "tokens": {"prompt": real.usage.prompt, "completion": real.usage.completion},
                        "finish_reason": getattr(real, "finish_reason", None)}}
            submit_in_context(self._pool, self._run, msgs, tls, real_msg, meta)
            self.stats["sent"] += 1
            return True
        except Exception:  # noqa: BLE001 — l'ombre ne casse jamais un tour
            logger.warning("nina ombre : lancement en échec", exc_info=True)
            return False

    def _run(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], real_msg: dict[str, Any],
             meta: dict[str, Any]) -> None:
        t0 = time.monotonic()
        completion: list[dict[str, Any]] = []
        error, usage, model, finish = None, None, getattr(self.provider, "model", ""), None
        try:
            comp = self.provider.complete(messages, tools, reasoning=False, session_key=f"nina-ombre:{meta['thread_id']}",
                                          max_tokens=self.max_tokens)
            completion = [_msg_dict(comp.message)]
            usage, model, finish = comp.usage, comp.model or model, comp.finish_reason
        except Exception as e:  # noqa: BLE001 — erreur de Nina : journalisée dans la ligne, jamais remontée
            self.stats["errors"] += 1
            error = f"{type(e).__name__}: {e}"[:300]
        latency = round(time.monotonic() - t0, 3)
        try:
            prompt, real_completion = messages, [real_msg]
            if self.seal is not None:
                prompt, completion, real_completion = self.seal(prompt), self.seal(completion), self.seal(real_completion)
            row = {
                "tenant_id": current_tenant_or_none(), "thread_id": meta["thread_id"],
                "turn_index": meta["turn_index"], "call_index": SHADOW_CALL_INDEX, "account_id": meta["account_id"],
                "kernel_sha": self.kernel_sha, "target": self.target,
                "recorded_at": datetime.now(timezone.utc).isoformat(), "prompt": prompt, "tools": tools,
                "completion": completion,
                "meta": {
                    "tenant_type": "internal", "channel": meta["channel"], "tier": SHADOW_TIER, "model": model,
                    "reasoning": False, "latency_s": latency,
                    "tokens": {"prompt": usage.prompt if usage else None,
                               "cached": (usage.cached if usage is not None and usage.cached_reported else None),
                               "completion": usage.completion if usage else None},
                    "route_reason": "ombre : appel 0 du tour doublé", "finish_reason": finish,
                    "gates_and_invariants": [], "approvals": [], "escalation": {"call": False, "turn": False},
                    "secret_seals": {"count": 0, "names": []}, "judge_verdict_v3": None, "turn_error": error,
                    "last_call": True,
                    "shadow": {"of_call": 0, "error": error, "real": {**meta["real"], "completion": real_completion}},
                },
            }
            self.sink.write([row])
        except Exception:  # noqa: BLE001
            logger.warning("nina ombre : écriture de la trace en échec", exc_info=True)
        finally:
            self._slots.release()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)


def build_shadow(config: Any, provider: Any, invariants: Any = None, sink: Any = None) -> NinaShadow | None:
    """`route.nina.mode: shadow` + un locataire interne + un magasin de traces → l'ombre ; sinon None (journalisé)."""
    nina = ((getattr(config, "route", None) or {}).get("nina") or {})
    if str(nina.get("mode", "off")) != "shadow" or provider is None:
        return None
    if not getattr(config, "internal_tenant", True):
        logger.info("nina ombre : pas de locataire interne sur cette instance, ombre éteinte")
        return None
    if sink is None:
        dsn = config.pg_dsn() if hasattr(config, "pg_dsn") else ""
        if not dsn:
            logger.warning("nina ombre : aucun DSN Postgres pour trace_calls, ombre éteinte")
            return None
        from nokker.traces.recorder import PgTraceSink

        sink = PgTraceSink(dsn)
    from nokker.traces.recorder import kernel_sha

    seal = getattr(invariants, "redact_context", None)
    target = (getattr(config, "traces_target", "") or "").strip() or "unknown"
    return NinaShadow(provider=provider, sink=sink, fraction=float(nina.get("shadow_fraction", 0.0)),
                      max_tokens=int(nina.get("max_tokens", 1024)), seal=seal if callable(seal) else None,
                      kernel_sha=kernel_sha(), target=target, max_in_flight=int(nina.get("max_in_flight", 2)))


__all__ = ["DEFAULT_MAX_CONTEXT", "NINA_MODES", "SHADOW_CALL_INDEX", "SHADOW_TIER", "NinaShadow", "build_shadow",
           "estimate_prompt_tokens", "fits"]
