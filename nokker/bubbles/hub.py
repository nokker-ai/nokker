"""`BubbleHub` — orchestration des fils et des bulles (docs/BUBBLES.md §1-§4, §6).

Un seul endroit pour :
  - les DROITS (membre du fil = lecture + écriture ; `shared_with.members` ou membre d'un fil de
    `shared_with.threads` = lecture seule ; bulle `notify` sans fil = ses destinataires) ;
  - le transfert Déplacer/Copier et l'injection de contexte au prochain tour du fil d'arrivée ;
  - les sessions qui se parlent (`send_message`, `ask` en tâche de fond, bulle `notify` fermée à la première
    action ou au bout de `notify_ttl_s`) ;
  - les événements `/v1/events` (`turn.*`, `tool.*`, `thread.updated`, `bubble.*`, `session.message`),
    chacun avec son AUDIENCE (`_audience` : utilisateurs qui ont le droit de le recevoir ; le serveur
    filtre et retire ce champ avant l'envoi) ;
  - l'observateur du noyau (`core.ports.TurnObserver`) : contexte reçu, événements du tour, producteurs.

Les routes HTTP (`nokker/api/server.py`) et les outils du modèle (`nokker/bubbles/tools.py`) appellent les
mêmes méthodes : une règle de droit ne peut pas diverger entre les deux chemins."""
from __future__ import annotations

import contextvars
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.agents.hub import AgentsHub
from nokker.agents.types import agent_to_json
from nokker.bubbles.base import BaseBubbles
from nokker.bubbles.context import render_for_context, render_sent, render_session_message
from nokker.bubbles.producers import Producers
from nokker.bubbles.types import (
    KINDS, MODES, Bubble, BubbleError, BubbleForbidden, BubbleNotFound, bubble_to_json,
)
from nokker.core.ports import ToolSpec, call_effect
from nokker.core.turn import ToolCall, ToolResult, Turn, TurnInput
from nokker.ledger.prices import load as load_prices
from nokker.ledger.turn_cost import is_subagent_thread, root_thread, row_from_call, turn_cost
from nokker.tenancy.context import (
    INTERNAL_TENANT, current_tenant_or_none, scoped_thread_id, tenant_of_thread, tenant_scope,
)
from nokker.threads.base import BaseThreads, new_thread_id
from nokker.threads.types import Thread, ThreadError, ThreadForbidden, ThreadNotFound, thread_to_json

logger = logging.getLogger("nokker.bubbles.hub")

NOTIFY_TTL_S = 600.0
SESSION_CHANNEL = "session"
ARGS_PREVIEW_CHARS = 200
TOOL_ARG_CHARS = 2000  # une valeur d'argument dans tool.call (§7.4.2), caviardée puis bornée
TOOL_PREVIEW_CHARS = 1200  # début du résultat dans tool.result


def _spawn_thread(fn: Callable[[], None]) -> None:
    # Le contexte (locataire courant, docs/TENANCY.md §2) suit la tâche de fond : tour `ask`, aperçu de `bubble_show`.
    ctx = contextvars.copy_context()
    threading.Thread(target=ctx.run, args=(fn,), daemon=True, name="nokker-session-ask").start()


def _tenant_ok(thread_id: str | None) -> bool:
    """Le fil appartient-il au locataire courant ? Hors de tout `tenant_scope` (tests du hub seul) : pas de filtre,
    la frontière est ailleurs. Un objet d'un autre locataire est traité comme inexistant (404, jamais 403)."""
    tid = current_tenant_or_none()
    return tid is None or (bool(thread_id) and tenant_of_thread(str(thread_id)) == tid)


def bubble_tenant(b: Bubble) -> str:
    """Locataire d'une bulle : celui de son fil ; une `notify` sans fil, celui du fil destinataire."""
    tid = b.thread_id or (b.state or {}).get("to_thread") or (b.state or {}).get("from_thread")
    return tenant_of_thread(str(tid)) if tid else INTERNAL_TENANT


SEARCH_MERGED_MAX = 15


def _merge_search(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Fusionne une 2e recherche du même tour : requêtes listées, résultats dédoublonnés par URL (aperçus déjà
    posés conservés), moteurs unis, durée cumulée."""
    state = dict(old)
    queries = list(old.get("queries") or [old.get("query")])
    if new.get("query") and new["query"] not in queries:
        queries.append(new["query"])
    state["queries"] = [q for q in queries if q]
    seen = {r.get("url") for r in old.get("results") or []}
    results = list(old.get("results") or [])
    for r in new.get("results") or []:
        if r.get("url") not in seen:
            results.append(r)
            seen.add(r.get("url"))
    state["results"] = results[:SEARCH_MERGED_MAX]
    state["engines"] = sorted(set(old.get("engines") or []) | set(new.get("engines") or []))
    if old.get("took_ms") is not None or new.get("took_ms") is not None:
        state["took_ms"] = (old.get("took_ms") or 0) + (new.get("took_ms") or 0)
    return state


def _turn_id(turn: Turn) -> str:
    return f"{turn.thread_id}:{turn.index}"


@dataclass
class BubbleHub:
    threads: BaseThreads
    bubbles: BaseBubbles
    publish: Callable[[str, dict], Any] | None = None  # EventBus.publish
    scrub: Callable[[str], str] | None = None  # DeterministicInvariants.scrub_output
    known_users: frozenset[str] = frozenset()
    # Multi-locataire : locataire d'un compte `us_…` (`nokker.tenancy.resolve.account_tenant_fn`, posé par `serve`).
    # Chez un testeur, seuls les comptes de SON locataire sont des membres ou destinataires possibles.
    user_tenant: Callable[[str | None], str | None] | None = None
    producers: Producers = field(default_factory=Producers)
    kernel: Any = None  # posé après construction du noyau (dépendance circulaire : outils ↔ hub)
    # Crew minimal (docs/BUBBLES.md §9.4) : posé après construction (même raison que `kernel` ci-dessus,
    # `nokker/api/server.py::serve`) — `None` = activation d'un `agent_draft` refusée (400).
    agents_hub: AgentsHub | None = None
    # Never clear (docs/NEVER-CLEAR.md §3) : `nokker.context.manager.ContextManager` ; posé par `build_kernel`.
    # `None` = les bulles `episode` ne sont pas créées et leur transfert ne copie pas de transcription.
    context_manager: Any = None
    spawn: Callable[[Callable[[], None]], None] = _spawn_thread
    notify_ttl_s: float = NOTIFY_TTL_S
    clock: Callable[[], float] = time.time
    _locks: dict[str, threading.RLock] = field(default_factory=dict)
    _locks_guard: threading.Lock = field(default_factory=threading.Lock)
    _tool_t0: dict[tuple[str, str], float] = field(default_factory=dict)
    # Une seule bulle `search` par tour (canari 18.10 : 3 web_search d'un même tour = 3 bulles violettes) :
    # les recherches suivantes du tour fusionnent leurs résultats dans la première.
    _turn_search: dict[str, str] = field(default_factory=dict)
    # Bulles `artifact` touchées pendant le tour : `working` pendant le tour, `done` à `turn_done` (§10.2).
    _turn_artifacts: dict[str, set[str]] = field(default_factory=dict)
    pages: Any = None  # nokker.pages.store.PageStore du noyau local (None ailleurs)
    # Coût par tour (X2, docs/LOKKAL.md §7.8) : grille de prix de `turn.cost` ; None = celle du ledger du noyau, sinon
    # la grille versionnée. Les appels des sous-agents d'un fil s'accumulent ici jusqu'à la fin du tour qui les a lancés.
    prices: Any = None
    _subagent_calls: dict[str, list] = field(default_factory=dict)  # fil de session -> [UsageRow]
    _cost_guard: threading.Lock = field(default_factory=threading.Lock)

    # ─────────────────────────────── utilitaires ───────────────────────────────

    def thread_lock(self, thread_id: str) -> threading.RLock:
        """Un tour à la fois par fil (le store attribue l'index du tour au début du tour)."""
        with self._locks_guard:
            lock = self._locks.get(thread_id)
            if lock is None:
                lock = self._locks[thread_id] = threading.RLock()
            return lock

    def _scrub(self, text: str) -> str:
        return self.scrub(text) if self.scrub is not None else text

    def _emit(self, event_type: str, data: dict[str, Any], audience: set[str]) -> None:
        if self.publish is None:
            return
        payload = dict(data)
        payload["_audience"] = sorted(a for a in audience if a)
        try:
            self.publish(event_type, payload)
        except Exception:  # noqa: BLE001
            logger.exception("publication de %s impossible", event_type)

    def thread_audience(self, thread_id: str | None) -> set[str]:
        if not thread_id:
            return set()
        t = self.threads.get(thread_id)
        return t.audience() if t is not None else set()

    def bubble_audience(self, b: Bubble) -> set[str]:
        out = self.thread_audience(b.thread_id) | set(b.shared_members)
        for tid in b.shared_threads:
            out |= self.thread_audience(tid)
        return out

    def _bubble_count(self, thread_id: str) -> int:
        return len(self.bubbles.list_thread(thread_id))

    def thread_json(self, t: Thread) -> dict[str, Any]:
        return thread_to_json(t, self._bubble_count(t.id))

    def _emit_thread(self, t: Thread) -> None:
        self._emit("thread.updated", {"thread": self.thread_json(t)}, t.audience())

    def _emit_bubble(self, event_type: str, b: Bubble, extra_audience: set[str] | None = None) -> None:
        self._emit(event_type, {"bubble": bubble_to_json(b)}, self.bubble_audience(b) | (extra_audience or set()))

    # ─────────────────────────────── droits ───────────────────────────────

    def is_known_user(self, user: str) -> bool:
        """Membre ou destinataire possible dans le locataire courant : jamais un utilisateur d'un autre locataire."""
        tid = current_tenant_or_none()
        if tid is None or tid == INTERNAL_TENANT:
            return user in self.known_users and not user.startswith("us_")
        return user.startswith("us_") and self.user_tenant is not None and self.user_tenant(user) == tid

    def require_member(self, thread_id: str, user: str | None) -> Thread:
        t = self.threads.get(thread_id) if _tenant_ok(thread_id) else None
        if t is None:
            raise ThreadNotFound(thread_id)
        if not t.is_member(user):
            raise ThreadForbidden(thread_id)
        return t

    def can_write_bubble(self, user: str | None, b: Bubble) -> bool:
        if b.thread_id is None:  # notify sans fil : ses destinataires seulement
            return bool(user) and user in b.shared_members
        t = self.threads.get(b.thread_id)
        return t is not None and t.is_member(user)

    def can_read_bubble(self, user: str | None, b: Bubble) -> bool:
        if not user:
            return False
        tid = current_tenant_or_none()
        if tid is not None and bubble_tenant(b) != tid:
            return False
        if self.can_write_bubble(user, b) or user in b.shared_members:
            return True
        return any(user in self.thread_audience(tid) for tid in b.shared_threads)

    def _readable(self, bubble_id: str, user: str | None) -> Bubble:
        self.sweep_expired()
        b = self.bubbles.require(bubble_id)
        if not self.can_read_bubble(user, b):
            raise BubbleNotFound(bubble_id)  # ne révèle pas l'existence d'une bulle illisible
        return b

    def _writable(self, bubble_id: str, user: str | None) -> Bubble:
        b = self._readable(bubble_id, user)
        if not self.can_write_bubble(user, b):
            raise BubbleForbidden("lecture seule : bulle partagée")
        return b

    # ─────────────────────────────── fils ───────────────────────────────

    def list_threads(self, user: str | None) -> list[Thread]:
        return [t for t in self.threads.list_for(user) if _tenant_ok(t.id)]

    def create_thread(self, title: str, user: str | None) -> Thread:
        if not user:
            raise ThreadError("identité requise")
        tid = current_tenant_or_none()
        t = self.threads.create(title, user, thread_id=scoped_thread_id(tid, new_thread_id()) if tid else None)
        self._emit_thread(t)
        return t

    def patch_thread(self, thread_id: str, user: str | None, *, title: str | None = None,
                     members: list[str] | None = None) -> Thread:
        t = self.require_member(thread_id, user)
        if members is not None:
            if user != t.owner:
                raise ThreadForbidden("seul le propriétaire change les membres")
            unknown = [m for m in members if not self.is_known_user(m)]
            if unknown:
                raise ThreadError(f"membre(s) inconnu(s) : {', '.join(unknown)}")
        old_audience = t.audience()
        t = self.threads.update(thread_id, title=title, members=members)
        self._emit("thread.updated", {"thread": self.thread_json(t)}, old_audience | t.audience())
        return t

    def delete_thread(self, thread_id: str, user: str | None) -> Thread:
        """Suppression d'une session : propriétaire seulement. Ses bulles ouvertes sont fermées (les copies
        partagées ailleurs restent lisibles), le fil disparaît des listes, l'historique des tours est conservé."""
        t = self.require_member(thread_id, user)
        if user != t.owner:
            raise ThreadForbidden("seul le propriétaire supprime la session")
        audience = t.audience()
        for b in self.bubbles.list_thread(thread_id):
            self._close(b.id)
        self.threads.delete(thread_id)
        # Images jointes (X7) : les octets partent avec le fil (ceux qu'un autre fil du locataire référence restent).
        images = getattr(self.kernel, "images", None) if self.kernel is not None else None
        if images is not None:
            try:
                images.erase_thread(thread_id)
            except Exception:  # noqa: BLE001 — le fil est supprimé ; le reste est journalisé pour l'opérateur
                logger.exception("images du fil %s non effacées", thread_id)
        self._emit("thread.deleted", {"thread_id": thread_id}, audience)
        return t

    def ensure_thread_for_turn(self, thread_id: str, sender: str | None, first_text: str) -> Thread:
        """`X-Session-Id` inconnu → fil créé à la volée (titre = 48 premiers caractères). Fil connu :
        `sender` doit en être membre (403 sinon) — sauf `sender` None (serveur ouvert, dev). Fil d'un autre
        locataire : 404."""
        if not _tenant_ok(thread_id):
            raise ThreadNotFound(thread_id)
        t, created = self.threads.ensure(thread_id, sender, first_text)
        if created:
            self._emit_thread(t)
            return t
        if sender is not None and t.owner is not None and not t.is_member(sender):
            raise ThreadForbidden(thread_id)
        return t

    def messages(self, thread_id: str, *, before: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Derniers tours (user/assistant) ; `before` = index de tour exclu, `limit` = nombre de TOURS."""
        if self.kernel is None:
            return []
        turns = [t for t in self.kernel.store.history(thread_id) if before is None or t.index < before]
        turns = turns[-max(1, min(200, limit)):]
        out: list[dict[str, Any]] = []
        for t in turns:
            user = {"index": t.index, "role": "user", "content": t.input.text, "sender": t.sender,
                    "channel": t.channel}
            if t.images:  # X7 : références ; octets par GET /v1/threads/{id}/images/{sha256}
                user["images"] = [i.to_dict() for i in t.images]
            out.append(user)
            out.append({"index": t.index, "role": "assistant", "content": t.reply})
        return out

    # ─────────────────────────────── bulles ───────────────────────────────

    def list_bubbles(self, thread_id: str, user: str | None, include_closed: bool = False
                     ) -> list[tuple[Bubble, bool]]:
        self.require_member(thread_id, user)
        self.sweep_expired()
        own = [(b, False) for b in self.bubbles.list_thread(thread_id, include_closed)]
        shared = [(b, True) for b in self.bubbles.list_shared_with_thread(thread_id, include_closed)
                  if b.thread_id != thread_id and _tenant_ok(b.thread_id)]
        return own + shared

    def get_bubble(self, bubble_id: str, user: str | None) -> tuple[Bubble, bool]:
        b = self._readable(bubble_id, user)
        return b, not self.can_write_bubble(user, b)

    def list_notifications(self, user: str | None) -> list[Bubble]:
        """Bulles `notify` OUVERTES adressées à `user` (sans fil, §4) — `GET /v1/notifications`, pour
        qu'un rechargement de page ne les perde pas."""
        if not user:
            return []
        self.sweep_expired()
        tid = current_tenant_or_none()
        return [b for b in self.bubbles.list_open_notify()
                if user in b.shared_members and (tid is None or bubble_tenant(b) == tid)]

    def create_bubble(self, thread_id: str, user: str | None, *, kind: str, title: str = "",
                      state: dict[str, Any] | None = None, status: str = "idle",
                      actions: list[dict[str, Any]] | None = None) -> Bubble:
        self.require_member(thread_id, user)
        if kind not in KINDS or kind in ("notify", "episode", "artifact"):
            raise BubbleError(f"kind non créable par un client : {kind}")
        if state is not None and not isinstance(state, dict):
            raise BubbleError("state doit être un objet")
        b = self.bubbles.create(thread_id=thread_id, kind=kind, title=title, state=state, status=status,
                                actions=actions, created_by=user or "nina")
        self._emit_bubble("bubble.created", b)
        return b

    def create_episode_bubble(self, ep: Any) -> str | None:
        """Bulle `episode` à la clôture d'un épisode (callback de `ContextManager.on_episode_closed`) : la
        représentation VISIBLE de ce qui sort de la fenêtre. `Recharger` l'épingle dans le contexte, `Archiver`
        la ferme ; déplacée/copiée vers un autre fil, elle emporte la mémoire (cf. `transfer`)."""
        turns = (ep.last_turn - ep.first_turn + 1) if ep.last_turn is not None else 0
        b = self.bubbles.create(
            thread_id=ep.thread_id, kind="episode", title=ep.title, status="done", created_by="nina",
            state={"episode_id": ep.id, "title": ep.title, "summary": ep.summary, "turns": turns,
                   "from_turn": ep.first_turn, "to_turn": ep.last_turn, "tokens_raw": ep.raw_tokens,
                   "closed_reason": ep.closed_reason, "day": ep.day, "keywords": list(ep.keywords)},
            actions=[{"id": "reload", "label": "Recharger dans le contexte", "primary": True},
                     {"id": "archive", "label": "Archiver"}])
        self._emit_bubble("bubble.created", b)
        return b.id

    def patch_bubble(self, bubble_id: str, user: str | None, *, title: str | None = None,
                     state: dict[str, Any] | None = None, status: str | None = None,
                     if_version: int | None = None) -> Bubble:
        self._writable(bubble_id, user)
        b = self.bubbles.update(bubble_id, title=title, state=state, status=status, if_version=if_version)
        self._emit_bubble("bubble.updated", b)
        return b

    def update_from_kernel(self, bubble_id: str, *, title: str | None = None, state: dict[str, Any] | None = None,
                           status: str | None = None, if_version: int | None = None) -> Bubble:
        """Mise à jour par le noyau lui-même (aperçus §5, producteurs) — pas de contrôle d'identité."""
        b = self.bubbles.update(bubble_id, title=title, state=state, status=status, if_version=if_version)
        self._emit_bubble("bubble.updated", b)
        return b

    def close_bubble(self, bubble_id: str, user: str | None) -> Bubble:
        self._writable(bubble_id, user)
        return self._close(bubble_id)

    def _close(self, bubble_id: str) -> Bubble:
        b = self.bubbles.require(bubble_id)
        if b.closed_at is not None:
            return b
        b = self.bubbles.update(bubble_id, close=True)
        self._emit("bubble.closed", {"bubble_id": b.id, "thread_id": b.thread_id}, self.bubble_audience(b))
        return b

    def transfer(self, bubble_id: str, user: str | None, to_thread: str, mode: str) -> dict[str, Any]:
        if mode not in MODES:
            raise BubbleError("mode ∈ move | copy")
        src = self._writable(bubble_id, user)
        if src.thread_id is None:
            raise BubbleError("une notification sans fil ne se transfère pas")
        dest = self.require_member(to_thread, user)
        if dest.id == src.thread_id:
            raise BubbleError("même fil")
        from_t = self.threads.require(src.thread_id)
        origin = {"thread_id": src.thread_id, "bubble_id": src.id, "mode": mode}
        old_audience = self.bubble_audience(src)
        if mode == "move":
            out = self.bubbles.update(src.id, thread_id=dest.id, origin=origin)
            if src.kind == "artifact" and self.pages is not None:  # la page suit le fil d'arrivée (§10.5)
                self.pages.update(str((src.state or {}).get("page_id") or ""), thread_id=dest.id)
        else:
            out = self.bubbles.clone(src, thread_id=dest.id, origin=origin)
        # Contexte : au PROCHAIN tour du fil d'arrivée (et du fil de départ en move).
        block = render_for_context(out, from_t.title, self._scrub)
        if src.kind == "episode" and self.context_manager is not None:
            # La mémoire voyage avec la bulle : transcription copiée (caviardée) dans le RÉSERVOIR du fil d'arrivée,
            # relisible par `recall_context` là-bas seulement (aucune lecture inter-fils).
            item = self.context_manager.export_episode(str((src.state or {}).get("episode_id") or ""), dest.id)
            if item is not None:
                block += f' — transcription : recall_context("{item.id}")'
        self.threads.push_context(dest.id, block)
        if mode == "move":
            self.threads.push_context(from_t.id, render_sent(out, dest.title))
        data = {"bubble_id": src.id, "new_bubble_id": out.id, "from_thread": from_t.id, "to_thread": dest.id,
                "mode": mode}
        audience = from_t.audience() | dest.audience()
        self._emit("bubble.transferred", data, audience)
        self._emit_bubble("bubble.created" if mode == "copy" else "bubble.updated", out, old_audience)
        self._emit_thread(self.threads.update(dest.id, touch=False))
        self._emit_thread(self.threads.update(from_t.id, touch=False))
        return {**data, "bubble": out}

    def share(self, bubble_id: str, user: str | None, *, members: list[str] | None = None,
              threads: list[str] | None = None) -> Bubble:
        b = self._writable(bubble_id, user)
        if b.thread_id is None:
            raise BubbleError("une notification ne se partage pas")
        if members is not None:
            unknown = [m for m in members if not self.is_known_user(m)]
            if unknown:
                raise BubbleError(f"membre(s) inconnu(s) : {', '.join(unknown)}")
        if threads is not None:
            for tid in threads:
                self.require_member(tid, user)  # on ne pousse pas une bulle dans un fil qu'on ne voit pas
        old_audience = self.bubble_audience(b)
        b = self.bubbles.update(bubble_id, shared_members=members, shared_threads=threads)
        self._emit_bubble("bubble.updated", b, old_audience)
        return b

    def action(self, bubble_id: str, user: str | None, action_id: str) -> dict[str, Any]:
        b = self._writable(bubble_id, user)
        if b.kind == "notify":
            # Fermée à la première action (§4) ; `goto` est implicite, le client navigue lui-même.
            known = {"goto", *(a.get("id") for a in (b.state.get("actions") or []) if isinstance(a, dict))}
            if action_id not in known:
                raise BubbleNotFound(f"action inconnue : {action_id}")
            self._close(b.id)
            return {"closed": True, "goto": b.state.get("from_thread") if action_id == "goto" else None}
        if b.kind == "episode":
            if self.context_manager is None:
                raise BubbleError("mémoire de session non configurée")
            ep_id = str((b.state or {}).get("episode_id") or "")
            if action_id == "reload":
                if b.thread_id is None or not self.context_manager.pin(b.thread_id, ep_id):
                    raise BubbleError("épisode introuvable dans ce fil")
                return {"pinned": True, "episode_id": ep_id}
            if action_id == "archive":
                if b.thread_id is not None:
                    self.context_manager.unpin(b.thread_id, ep_id)
                self._close(b.id)
                return {"closed": True, "episode_id": ep_id}
            raise BubbleNotFound(f"action inconnue : {action_id}")
        if b.kind == "agent_draft":
            # Seule action : `activate` (§9.4) — crée l'agent (Crew), ferme la bulle, publie
            # `agent.created`. Jamais en direct ailleurs : pas d'approbation requise ici, un
            # `agent_draft` armé ne fait rien tout seul (P2 = exécution réelle, hors scope).
            if action_id != "activate":
                raise BubbleNotFound(f"action inconnue : {action_id}")
            if self.agents_hub is None:
                raise BubbleError("crew (agents) non configuré")
            s = b.state or {}
            agent = self.agents_hub.create_agent(
                name=str(s.get("name") or b.title or ""), role=str(s.get("role") or ""),
                model=str(s.get("model") or ""), tools=[str(t) for t in (s.get("tools") or [])],
                created_by=user or b.created_by)
            self._close(b.id)
            return {"activated": True, "agent": agent_to_json(agent)}
        action = next((a for a in b.actions if isinstance(a, dict) and a.get("id") == action_id), None)
        if action is None:
            raise BubbleNotFound(f"action inconnue : {action_id}")
        # Jamais en direct : l'action devient un tour du fil, soumis aux portes et aux approbations.
        text = f"[Action « {action.get('label') or action_id} » demandée sur la bulle « {b.title} » ({b.id})]"
        self._run_background_turn(b.thread_id, text, user)
        return {"accepted": True, "thread_id": b.thread_id}

    # ─────────────────────────────── sessions qui se parlent ───────────────────────────────

    def send_message(self, from_thread: str, to_thread: str, text: str, *, ask: bool = False,
                     sender: str | None, allow_ask: bool = True) -> dict[str, Any]:
        src = self.require_member(from_thread, sender)
        dest = self.require_member(to_thread, sender)
        if src.id == dest.id:
            raise ThreadError("un fil ne s'écrit pas à lui-même")
        text = str(text or "").strip()
        if not text:
            raise ThreadError("texte vide")
        ask = ask and allow_ask
        block = render_session_message(src.title, text, self._scrub)
        # `ask` : le bloc est l'ENTRÉE du tour lancé dans `dest` — pas aussi dans sa boîte (doublon).
        self._deliver(src, dest, block, text, from_inbox=ask)
        if ask:
            self._run_background_turn(dest.id, block, sender, reply_to=src.id)
        return {"delivered": True, "ask": ask}

    def _deliver(self, src: Thread, dest: Thread, block: str, text: str, *, from_inbox: bool = False) -> None:
        if not from_inbox:
            self.threads.push_context(dest.id, block)
        clean = self._scrub(text)[:2000]
        self._emit("session.message", {"from_thread": src.id, "to_thread": dest.id, "text": clean},
                   src.audience() | dest.audience())
        self._notify(dest, src, clean)

    def _notify(self, recipient_thread: Thread, from_thread: Thread, text: str) -> Bubble:
        recipients = sorted(recipient_thread.audience())
        b = self.bubbles.create(thread_id=None, kind="notify", title=from_thread.title, status="done",
                                state={"from_thread": from_thread.id, "from_title": from_thread.title,
                                       "to_thread": recipient_thread.id, "text": text[:280], "actions": []},
                                created_by="nina", shared_members=recipients)
        self._emit_bubble("bubble.created", b)
        return b

    def _run_background_turn(self, thread_id: str, text: str, sender: str | None, *,
                             reply_to: str | None = None) -> None:
        """Tour en tâche de fond dans `thread_id` (canal `session`) ; si `reply_to`, la réponse revient à ce
        fil comme un message de session. Un tour lancé ainsi ne peut pas relancer `ask` (pas de ping-pong)."""
        kernel = self.kernel
        if kernel is None:
            raise ThreadError("noyau non branché")

        def run() -> None:
            try:
                with self.thread_lock(thread_id):
                    turn = kernel.run_turn(TurnInput(thread_id=thread_id, user_text=text, channel=SESSION_CHANNEL,
                                                     sender=sender))
                if reply_to:
                    dest = self.threads.get(reply_to)
                    src = self.threads.get(thread_id)
                    if dest is not None and src is not None and turn.reply:
                        block = render_session_message(src.title, turn.reply, self._scrub, reply=True)
                        self._deliver(src, dest, block, turn.reply)
            except Exception:  # noqa: BLE001
                logger.exception("tour de fond dans %s en échec", thread_id)

        self.spawn(run)

    def sweep_expired(self) -> int:
        """Ferme les bulles `notify` ouvertes depuis plus de `notify_ttl_s` (§4 : 10 min). Dans une requête : celles
        du locataire courant. Balayeur de fond (hors scope) : un passage par locataire connu, chacun dans son
        `tenant_scope`, donc sous sa propre RLS (docs/TENANCY.md §4) ; backend mémoire : un passage."""
        tenants = getattr(self.bubbles, "tenants", None)
        if current_tenant_or_none() is not None or tenants is None:
            return self._sweep_expired()
        n = 0
        for tid in tenants():
            try:
                with tenant_scope(tid):
                    n += self._sweep_expired()
            except Exception:  # noqa: BLE001 — un locataire en échec n'arrête pas le balayage des autres
                logger.exception("balayage des notifications en échec pour %s", tid)
        return n

    def _sweep_expired(self) -> int:
        n = 0
        cutoff = self.clock() - self.notify_ttl_s
        for b in self.bubbles.list_open_notify():
            if b.created_at <= cutoff:
                self._close(b.id)
                n += 1
        return n

    # ─────────────────────────────── outils du modèle ───────────────────────────────

    def tool_bubble_update(self, thread_id: str, bubble_id: str, *, title: str | None = None,
                           state: dict[str, Any] | None = None, status: str | None = None) -> Bubble:
        b = self.bubbles.require(bubble_id)
        if b.thread_id != thread_id:
            raise BubbleForbidden("cette bulle n'appartient pas au fil courant")
        return self.update_from_kernel(bubble_id, title=title, state=state, status=status)

    # ─────────────────────────────── observateur du noyau ───────────────────────────────

    def context_for(self, thread_id: str) -> list[str]:
        return self.threads.drain_context(thread_id)

    def turn_started(self, turn: Turn) -> None:
        t = self.threads.get(turn.thread_id)
        if t is None:
            t, _ = self.threads.ensure(turn.thread_id, turn.sender, turn.input.text)
        audience = t.audience()
        self._emit("turn.started", {"thread_id": turn.thread_id, "turn_id": _turn_id(turn)}, audience)
        self._emit_thread(self.threads.update(turn.thread_id, working=True))

    def attachment_skipped(self, turn: Turn, count: int, reason: str) -> None:
        """Images jointes non lues par le modèle (X7, docs/LOKKAL.md §7.6) : `reason` = "no_vision"."""
        self._emit("attachment.skipped", {"thread_id": turn.thread_id, "turn_id": _turn_id(turn), "count": count,
                                          "reason": reason}, self.thread_audience(turn.thread_id))

    def tool_call(self, turn: Turn, call: ToolCall) -> None:
        self._tool_t0[(turn.thread_id, call.id)] = time.monotonic()
        preview = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)[:ARGS_PREVIEW_CHARS]
        # §7.4.2 (Nokker Lokkal) : l'effet de CET appel et les arguments, caviardés et bornés, pour que le client
        # terminal montre ce qui part ; `args_preview` reste pour les clients existants.
        spec = None
        try:
            spec = self.kernel.tools.spec(call.name) if self.kernel is not None else None
        except Exception:  # noqa: BLE001 — l'événement part sans l'effet
            spec = None
        effect = call_effect(spec, call.arguments).value
        args = {k: (self._scrub(v)[:TOOL_ARG_CHARS] if isinstance(v, str) else v) for k, v in call.arguments.items()}
        self._emit("tool.call", {"thread_id": turn.thread_id, "turn_id": _turn_id(turn), "call_id": call.id,
                                 "name": call.name, "args_preview": self._scrub(preview), "effect": effect,
                                 "args": args},
                   self.thread_audience(turn.thread_id))

    def tool_result(self, turn: Turn, call: ToolCall, result: ToolResult, ms: int) -> None:
        self._tool_t0.pop((turn.thread_id, call.id), None)
        self._emit("tool.result", {"thread_id": turn.thread_id, "turn_id": _turn_id(turn), "call_id": call.id,
                                   "name": call.name, "ok": not result.is_error, "ms": ms,
                                   "is_error": result.is_error,
                                   "preview": self._scrub(result.content or "")[:TOOL_PREVIEW_CHARS]},
                   self.thread_audience(turn.thread_id))

    def direct_result(self, thread_id: str, approval_id: str, name: str, content: str, is_error: bool,
                      owner: str | None = None) -> None:
        """Résultat d'un appel direct du propriétaire décidé sur une carte (POST /v1/local/tools/call, §7.4.8) : le
        contenu entier (le client terminal l'affiche tel quel), caviardé comme le reste. Un fil sans tour encore n'a pas
        d'audience au hub : l'événement va alors au propriétaire qui a décidé (sinon personne ne le reçoit)."""
        self._emit("tool.result", {"thread_id": thread_id, "approval_id": approval_id, "name": name,
                                   "ok": not is_error, "is_error": is_error, "content": self._scrub(content or ""),
                                   "preview": self._scrub(content or "")[:TOOL_PREVIEW_CHARS]},
                   self.thread_audience(thread_id) or ({owner} if owner else set()))

    def tool_executed(self, turn: Turn, call: ToolCall, spec: ToolSpec | None, result: ToolResult) -> str | None:
        t0 = self._tool_t0.get((turn.thread_id, call.id))
        took_ms = int((time.monotonic() - t0) * 1000) if t0 is not None else None
        produced = self.producers.produce(call, spec, result, took_ms)
        if produced is None:
            return None
        if self.threads.get(turn.thread_id) is None:
            self.threads.ensure(turn.thread_id, turn.sender, turn.input.text)
        if produced.kind == "artifact":  # une bulle par page et par fil (docs/BUBBLES.md §10.2)
            page_id = produced.state.get("page_id")
            same = next((b for b in self.bubbles.list_thread(turn.thread_id)
                         if b.kind == "artifact" and b.closed_at is None and (b.origin or {}).get("mode") != "copy"
                         and (b.state or {}).get("page_id") == page_id), None)
            self._turn_artifacts.setdefault(_turn_id(turn), set())
            if same is not None:
                b = self.update_from_kernel(same.id, state=produced.state, status="working")
                self._turn_artifacts[_turn_id(turn)].add(b.id)
                return f"\n[bulle {b.id} (artifact) mise à jour : version {produced.state.get('version')}]"
        prev_id = self._turn_search.get(_turn_id(turn)) if produced.kind == "search" else None
        prev = self.bubbles.get(prev_id) if prev_id else None
        if prev is not None and prev.closed_at is None:
            b = self.update_from_kernel(prev.id, state=_merge_search(prev.state, produced.state))
            try:
                self.producers.after_create(b)
            except Exception:  # noqa: BLE001
                logger.exception("request_previews a levé")
            return f"\n[résultats ajoutés à la bulle {b.id} (search) de ce tour]"
        b = self.bubbles.create(thread_id=turn.thread_id, kind=produced.kind, title=produced.title,
                                state=produced.state, status=produced.status, actions=produced.actions,
                                created_by="nina")
        self._emit_bubble("bubble.created", b)
        if b.kind == "search":
            self._turn_search[_turn_id(turn)] = b.id
        if b.kind == "artifact":
            self._turn_artifacts.setdefault(_turn_id(turn), set()).add(b.id)
        try:
            self.producers.after_create(b)
        except Exception:  # noqa: BLE001 — un aperçu en panne ne casse pas le tour
            logger.exception("request_previews a levé")
        return f"\n[bulle {b.id} ({b.kind}) affichée dans le fil — bubble_update pour la modifier]"

    def turn_done(self, turn: Turn) -> None:
        audience = self.thread_audience(turn.thread_id)
        tid = _turn_id(turn)
        self._turn_search.pop(tid, None)
        for bid in self._turn_artifacts.pop(tid, set()):  # la page a fini de bouger pour ce tour
            try:
                self.update_from_kernel(bid, status="done")
            except Exception:  # noqa: BLE001 — bulle fermée ou retirée entre-temps
                logger.debug("bulle artifact %s non close", bid, exc_info=True)
        if turn.reply:
            self._emit("turn.delta", {"thread_id": turn.thread_id, "turn_id": tid, "text": turn.reply}, audience)
        prompt = sum(c.usage.prompt for c in turn.calls)
        completion = sum(c.usage.completion for c in turn.calls)
        chf = [c.chf for c in turn.calls if c.chf is not None]
        data: dict[str, Any] = {"thread_id": turn.thread_id, "turn_id": tid,
                                "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                                          "total_tokens": prompt + completion}}
        if turn.calls:
            data["tier"] = turn.calls[-1].tier.value
        if chf:
            data["cost_chf"] = round(sum(chf), 6)
        # Le coût part AVANT `turn.done` : un client qui cesse d'écouter à `turn.done` (mode non interactif) l'a déjà.
        self._emit_turn_cost(turn, tid, audience)
        self._emit("turn.done", data, audience)
        if self.threads.get(turn.thread_id) is not None:
            self._emit_thread(self.threads.update(turn.thread_id, working=False, last_snippet=turn.reply or ""))

    def _price_catalog(self) -> Any:
        if self.prices is None:
            self.prices = getattr(getattr(self.kernel, "ledger", None), "prices", None) or load_prices()
        return self.prices

    def _emit_turn_cost(self, turn: Turn, tid: str, audience: set[str]) -> None:
        """`turn.cost` (X2), juste avant `turn.done`. Un tour de sous-agent ne publie rien : ses appels comptent dans le tour
        du fil de session qui l'a lancé (un tour humain = un `turn.cost`, sous-agents compris)."""
        try:
            if is_subagent_thread(turn.thread_id):
                with self._cost_guard:
                    self._subagent_calls.setdefault(root_thread(turn.thread_id), []).extend(
                        row_from_call(c) for c in turn.calls)
                return
            with self._cost_guard:
                children = self._subagent_calls.pop(turn.thread_id, [])
            body = turn_cost(turn.calls, self._price_catalog(), subagents=children)
        except Exception:  # noqa: BLE001 — un coût manqué ne casse jamais la fin du tour
            logger.exception("turn.cost non calculé")
            return
        self._emit("turn.cost", {"thread_id": turn.thread_id, "turn_id": tid, **body}, audience)


__all__ = ["NOTIFY_TTL_S", "SESSION_CHANNEL", "BubbleHub"]
