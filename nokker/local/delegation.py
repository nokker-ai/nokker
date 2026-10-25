"""Sessions déléguées par nokker.ai sur cette machine (lot X21, docs/LOKKAL.md §6.7 et §7.16), côté Lokkal.

Une session web de nokker.ai qui a besoin d'une ressource `local` passe le relais à cette machine, APRÈS acceptation
de l'utilisateur : le noyau de nokker.ai arrive par la passerelle avec un **jeton de délégation** (portée
`lokkal:session`, la machine nommée, la session web et la demande `handoff` nommées). Ce module tient :

- **le cloisonnement de la passerelle** (`allows`) : un jeton de délégation n'ouvre QUE trois routes —
  `POST /v1/local/sessions` (ouvrir la session de la demande), `POST /v1/chat/completions` et
  `POST /v1/threads/{id}/cancel` sur un fil que CETTE délégation a ouvert (même compte, même session web). Tout le
  reste (fils, mémoire, coffre, réglages, pages, processus, approbations) est refusé (403) : nokker.ai ne lit rien de
  la machine au-delà de la réponse que la session Lokkal choisit de rendre ;
- **le retour des résultats** (`wrap`) : chaque tour fini d'un fil délégué (réponse synchrone, ou suite du tour après
  une carte décidée sur la machine ou au téléphone, X10) est envoyé à `POST /v1/gateway/handoffs/{id}/result`
  (jeton de machine) : `{turn_index, reply, pending_approvals}`, la réponse seule (caviardée par les invariants du
  noyau), jamais les appels d'outils ni leurs sorties. nokker.ai ne range chaque tour qu'une fois.

La session déléguée suit les permissions et les cartes LOCALES (§L1) comme toute session : le jeton de délégation ne
décide aucune carte. Registre en mémoire : après un redémarrage, les suites des fils délégués ne repartent plus vers
le web (la demande y reste « en attente d'une carte ») ; l'utilisateur la voit sur la machine.
"""
from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("nokker.local.delegation")

SCOPE = "lokkal:session"
_RE_CANCEL = re.compile(r"^/v1/threads/([^/]+)/cancel$")
_RE_HANDOFF = re.compile(r"^ho_[0-9a-f]{16}$")


@dataclass
class DelegatedSessions:
    base_url: str = "https://app.nokker.ai"
    token: Callable[[], str | None] = field(default=lambda: None)  # jeton de machine (coffre local)
    client: Any = None  # httpx.Client injectable (tests)
    spawn: Callable[[Callable[[], None]], None] = field(
        default=lambda fn: threading.Thread(target=fn, name="nokker-delegated", daemon=True).start())
    _threads: dict[str, dict[str, str]] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    # ── registre ──

    def register(self, thread_id: str, claims: dict[str, Any]) -> None:
        handoff = str(claims.get("handoff") or "")
        with self._lock:
            self._threads[thread_id] = {"account": str(claims.get("sub") or ""),
                                        "web_session": str(claims.get("web_session") or ""),
                                        "handoff": handoff if _RE_HANDOFF.match(handoff) else ""}

    def is_delegated(self, thread_id: str) -> bool:
        with self._lock:
            return thread_id in self._threads

    def _owns(self, thread_id: str, claims: dict[str, Any]) -> bool:
        with self._lock:
            d = self._threads.get(thread_id)
        return d is not None and d["account"] == str(claims.get("sub") or "") \
            and d["web_session"] == str(claims.get("web_session") or "")

    # ── cloisonnement de la passerelle ──

    def allows(self, method: str, path: str, session_id: str | None, claims: dict[str, Any]) -> bool:
        """Une requête relayée portant un jeton de délégation : seulement les routes de la session déléguée ; un jeton
        qui nomme une opération des agents (`crew`, X15) : la route de cette opération seulement, jamais une session."""
        if "crew" in claims:
            from nokker.local.crew import allows as crew_allows

            return crew_allows(method, path, claims.get("crew"))
        if method == "POST" and path == "/v1/local/sessions":
            return True
        if method == "POST" and path == "/v1/chat/completions":
            return bool(session_id) and self._owns(str(session_id), claims)
        m = _RE_CANCEL.match(path)
        return method == "POST" and m is not None and self._owns(m.group(1), claims)

    # ── retour des résultats ──

    def wrap(self, run_turn: Callable[..., Any]) -> Callable[..., Any]:
        def reported(inp: Any, *args: Any, **kwargs: Any) -> Any:
            turn = run_turn(inp, *args, **kwargs)
            try:
                tid = getattr(inp, "thread_id", "")
                with self._lock:
                    d = self._threads.get(tid)
                if d and d["handoff"] and turn is not None:
                    body = {"turn_index": int(getattr(turn, "index", 0)), "reply": str(getattr(turn, "reply", "") or ""),
                            "pending_approvals": len(getattr(turn, "pending_approvals", None) or [])}
                    self.spawn(lambda: self.report(d["handoff"], body))
            except Exception:  # noqa: BLE001 — le tour est fini, son retour au web ne le casse pas
                logger.debug("relais : retour du tour non envoyé", exc_info=True)
            return turn

        return reported

    def report(self, handoff_id: str, body: dict[str, Any]) -> bool:
        try:
            token = self.token()
        except Exception:  # noqa: BLE001
            token = None
        if not token:
            return False
        try:
            http = self.client or httpx.Client(timeout=15.0)
            r = http.post(f"{self.base_url.rstrip('/')}/v1/gateway/handoffs/{handoff_id}/result", json=body,
                          headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            logger.info("relais %s : résultat non remis (%s)", handoff_id, type(e).__name__)
            return False
        return r.status_code == 200


def turn_extension(turn: Any) -> dict[str, Any]:
    """Champ `nokker` de la réponse de chat d'un fil délégué : le tour et ses cartes en attente (nokker.ai sait si la
    réponse est finale ou attend une décision)."""
    return {"turn_index": int(getattr(turn, "index", 0)),
            "pending_approvals": len(getattr(turn, "pending_approvals", None) or [])}


__all__ = ["SCOPE", "DelegatedSessions", "turn_extension"]
