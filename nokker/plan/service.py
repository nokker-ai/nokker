"""Plan — pilotage de projet (lot X18, docs/LOKKAL.md §7.18).

- **Cartes hiérarchiques** : objectif (`goal`) → lots (`milestone`) → tâches (`task`), 6 niveaux au plus, sans cycle ;
  une carte est rattachée à un projet d'équipe (X17) ou au compte (`project: null`) ; une fille a toujours le projet de
  sa mère. Supprimer une carte remonte ses filles d'un cran (rien n'est perdu en silence).
- **Le contexte descend** : le but, les contraintes et les décisions de chaque ancêtre, de la racine à la carte, forment
  un bloc injecté dans le message système quand une session (fil rattaché à la carte) ou un agent (listé dans
  `agents` de la carte) travaille dessus — `block_for_thread`, appelé par le Context Engine.
- **L'avancement remonte** : seule une feuille déclare son statut ; celui d'une carte qui a des filles est calculé
  (toutes faites → `done` ; une bloquée → `blocked` ; une commencée ou faite → `in_progress` ; sinon `todo`), avec le
  compte des feuilles faites (`progress`). Un client ne pose jamais le statut d'une carte mère (400).
- **Découper une carte** : l'outil `plan_propose_split` du modèle POSE une proposition (bulle `plan_split` dans le fil,
  ou `GET /v1/plan/proposals`) ; seul un humain l'accepte (les filles sont créées à son nom) ou la refuse.
- **Brief du matin** : un agent Crew planifié (X14, `30 7 * * 1-5` Europe/Zurich par défaut) dont la mission appelle
  `plan_brief` (cartes, incidents ouverts) et, si le connecteur d'agenda est posé, `calendar_events` ; sa réponse est
  rangée dans une bulle `brief` de son fil, et une notification part à son propriétaire.

Événements, diffusés à toute identité vérifiée du locataire (comme `agent.*`) : `plan.card.updated {card}` (la carte
et chacun de ses ancêtres, dont l'avancement a pu changer), `plan.card.deleted {id, lifted}`,
`plan.proposal.updated {proposal}`."""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.plan.store import BasePlan
from nokker.plan.types import (
    KINDS, MAX_CARDS, MAX_CHILDREN_PROPOSED, MAX_DEPTH, STATUSES, Card, PlanError, PlanNotFound, Proposal, iso,
    new_card_id, new_proposal_id, proposal_to_json,
)
from nokker.tenancy.context import current_tenant_or_none

logger = logging.getLogger("nokker.plan")

KIND_LABELS = {"goal": "Objectif", "milestone": "Lot", "task": "Tâche"}
MAX_BLOCK_CHARS = 6000
BRIEF_CRON = "30 7 * * 1-5"
BRIEF_TZ = "Europe/Zurich"
BRIEF_NAME = "Brief du matin"
CALENDAR_TOOL = "calendar_events"
BRIEF_TOOL = "plan_brief"
_EDITABLE = ("title", "kind", "intent", "constraints", "decisions", "status", "agents", "position")


@dataclass
class _Index:
    by_id: dict[str, Card]
    children: dict[str | None, list[Card]]

    def kids(self, card_id: str | None) -> list[Card]:
        return self.children.get(card_id, [])

    def chain(self, card_id: str) -> list[Card]:
        """Ancêtres puis la carte, de la racine à elle."""
        out: list[Card] = []
        c = self.by_id.get(card_id)
        while c is not None and len(out) <= MAX_DEPTH + 1:
            out.append(c)
            c = self.by_id.get(c.parent_id) if c.parent_id else None
        return list(reversed(out))

    def depth(self, card_id: str) -> int:
        return len(self.chain(card_id)) - 1

    def height(self, card_id: str) -> int:
        kids = self.kids(card_id)
        return 0 if not kids else 1 + max(self.height(k.id) for k in kids)

    def subtree(self, card_id: str) -> list[Card]:
        out, todo = [], [card_id]
        while todo:
            cid = todo.pop()
            for k in self.kids(cid):
                out.append(k)
                todo.append(k.id)
        return out


def _rollup(idx: _Index, card_id: str, memo: dict[str, dict[str, int]]) -> dict[str, int]:
    if card_id in memo:
        return memo[card_id]
    kids = idx.kids(card_id)
    if not kids:
        st = idx.by_id[card_id].status
        out = {"done": int(st == "done"), "total": 1, "blocked": int(st == "blocked"),
               "in_progress": int(st == "in_progress")}
    else:
        out = {"done": 0, "total": 0, "blocked": 0, "in_progress": 0}
        for k in kids:
            for key, v in _rollup(idx, k.id, memo).items():
                out[key] += v
    memo[card_id] = out
    return out


def effective_status(idx: _Index, card: Card, memo: dict[str, dict[str, int]]) -> str:
    if not idx.kids(card.id):
        return card.status
    r = _rollup(idx, card.id, memo)
    if r["done"] == r["total"]:
        return "done"
    if r["blocked"]:
        return "blocked"
    if r["in_progress"] or r["done"]:
        return "in_progress"
    return "todo"


def _strings(value: Any, name: str, *, each: int, most: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise PlanError(f"{name} : liste de textes attendue")
    out = [" ".join(x.split())[:each] for x in value if x.strip()]
    if len(out) > most:
        raise PlanError(f"{name} : {most} au plus")
    return out


@dataclass
class Plan:
    store: BasePlan
    agents: Any = None  # AgentsHub (agents des cartes, brief du matin)
    hub: Any = None  # BubbleHub (bulles `plan_split` et `brief`, fils rattachés) ; None = routes seulement
    history: Callable[[str], list[Any]] | None = None  # fil → tours (réponse complète du brief) : kernel.store.history
    operate: Callable[[], Any] | None = None  # → Operate (incidents du brief) ; None ou None rendu = incidents éteints
    has_tool: Callable[[str], bool] | None = None  # outil présent dans le registre du locataire courant (agenda)
    # Projet d'équipe (X17) : (identité, pj_…) → pj_… vérifié (membre au moins), ou PlanError ; None = drapeau `team`
    # éteint : le champ `project` est ignoré et n'apparaît jamais rempli.
    check_project: Callable[[str, str], str] | None = None
    publish: Callable[[str, dict], Any] | None = None
    scrub: Callable[[str], str] | None = None
    # Livraison du brief hors de l'app (Telegram, e-mail : nokker/plan/delivery.py::BriefDelivery) ; None = dans l'app
    # seulement (bulle + notification).
    delivery: Any = None
    clock: Callable[[], float] = time.time
    cache_ttl_s: float = 30.0
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _version: int = field(default=0, init=False, repr=False)
    _blocks: dict[tuple[str, str], tuple[int, float, str]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        listeners = getattr(self.agents, "run_listeners", None)
        if listeners is not None:
            listeners.append(self.on_run)

    # ─────────── utilitaires ───────────

    def _clean(self, text: str) -> str:
        return self.scrub(text) if self.scrub is not None and text else text

    def _index(self) -> _Index:
        cards = self.store.all()
        by_id = {c.id: c for c in cards}
        children: dict[str | None, list[Card]] = {}
        for c in cards:
            parent = c.parent_id if c.parent_id in by_id else None
            children.setdefault(parent, []).append(c)
        for lst in children.values():
            lst.sort(key=lambda c: (c.position, c.created_at, c.id))
        return _Index(by_id=by_id, children=children)

    def _require(self, idx: _Index, card_id: str) -> Card:
        c = idx.by_id.get(card_id)
        if c is None:
            raise PlanNotFound(f"carte inconnue : {card_id}")
        return c

    def card_json(self, idx: _Index, c: Card, memo: dict[str, dict[str, int]] | None = None) -> dict[str, Any]:
        memo = {} if memo is None else memo
        r = _rollup(idx, c.id, memo)
        kids = idx.kids(c.id)
        return {"id": c.id, "parent_id": c.parent_id, "project": c.project, "kind": c.kind, "title": c.title,
                "intent": c.intent, "constraints": list(c.constraints), "decisions": list(c.decisions),
                "status": effective_status(idx, c, memo), "computed": bool(kids),
                "progress": {**r, "ratio": round(r["done"] / r["total"], 4) if r["total"] else 0.0},
                "children": [k.id for k in kids], "depth": idx.depth(c.id), "agents": list(c.agents),
                "threads": list(c.threads), "position": c.position, "created_by": c.created_by,
                "created_at": iso(c.created_at), "updated_at": iso(c.updated_at)}

    def _changed(self, card_ids: list[str]) -> None:
        """Après une écriture : invalide le cache des blocs, publie la carte et ses ancêtres (avancement)."""
        self._version += 1
        if self.publish is None:
            return
        idx = self._index()
        memo: dict[str, dict[str, int]] = {}
        seen: set[str] = set()
        for cid in card_ids:
            for c in reversed(idx.chain(cid)):
                if c.id in seen:
                    continue
                seen.add(c.id)
                try:
                    self.publish("plan.card.updated", {"card": self.card_json(idx, c, memo)})
                except Exception:  # noqa: BLE001
                    logger.exception("publication de plan.card.updated impossible")

    def _emit_proposal(self, p: Proposal) -> None:
        if self.publish is not None:
            try:
                self.publish("plan.proposal.updated", {"proposal": proposal_to_json(p)})
            except Exception:  # noqa: BLE001
                logger.exception("publication de plan.proposal.updated impossible")

    def _fields(self, body: dict[str, Any], *, partial: bool) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if "title" in body or not partial:
            title = " ".join(str(body.get("title") or "").split())
            if not title:
                raise PlanError("title : requis")
            out["title"] = self._clean(title[:200])
        if "kind" in body and body["kind"] is not None:
            if body["kind"] not in KINDS:
                raise PlanError(f"kind : {' | '.join(KINDS)}")
            out["kind"] = body["kind"]
        if "intent" in body:
            out["intent"] = self._clean(str(body.get("intent") or "").strip()[:4000])
        if "constraints" in body:
            out["constraints"] = [self._clean(x) for x in _strings(body["constraints"], "constraints", each=500,
                                                                   most=30)]
        if "decisions" in body:
            out["decisions"] = [self._clean(x) for x in _strings(body["decisions"], "decisions", each=500, most=50)]
        if "status" in body:
            if body["status"] not in STATUSES:
                raise PlanError(f"status : {' | '.join(STATUSES)}")
            out["status"] = body["status"]
        if "agents" in body:
            ids = _strings(body["agents"], "agents", each=40, most=10)
            for aid in ids:
                if self.agents is None or self.agents.store.get(aid) is None:
                    raise PlanError(f"agents : agent inconnu {aid}")
            out["agents"] = list(dict.fromkeys(ids))
        if "position" in body:
            try:
                out["position"] = int(body["position"])
            except (TypeError, ValueError):
                raise PlanError("position : entier attendu") from None
        return out

    def _project(self, body: dict[str, Any], by: str) -> str | None:
        if self.check_project is None or not body.get("project"):
            return None
        return self.check_project(by, str(body["project"]))

    # ─────────── lecture ───────────

    def list_cards(self, *, project: str | None = None, roots: bool = False) -> list[dict[str, Any]]:
        idx = self._index()
        memo: dict[str, dict[str, int]] = {}
        cards = idx.kids(None) if roots else sorted(idx.by_id.values(), key=lambda c: (idx.depth(c.id), c.position,
                                                                                       c.created_at))
        if project is not None:
            cards = [c for c in cards if (c.project or "") == project]
        return [self.card_json(idx, c, memo) for c in cards]

    def get_card(self, card_id: str) -> dict[str, Any]:
        idx = self._index()
        c = self._require(idx, card_id)
        memo: dict[str, dict[str, int]] = {}
        out = self.card_json(idx, c, memo)
        out["ancestors"] = [{"id": a.id, "kind": a.kind, "title": a.title} for a in idx.chain(card_id)[:-1]]
        out["children_cards"] = [self.card_json(idx, k, memo) for k in idx.kids(card_id)]
        return out

    # ─────────── écriture ───────────

    def create(self, body: dict[str, Any], by: str) -> dict[str, Any]:
        with self._lock:
            f = self._fields(body, partial=False)
            idx = self._index()
            if len(idx.by_id) >= MAX_CARDS:
                raise PlanError(f"au plus {MAX_CARDS} cartes", status=409, code="too_many")
            parent_id = body.get("parent_id") or None
            project = self._project(body, by)
            depth = 0
            if parent_id:
                parent = self._require(idx, str(parent_id))
                depth = idx.depth(parent.id) + 1
                if depth >= MAX_DEPTH:
                    raise PlanError(f"au plus {MAX_DEPTH} niveaux")
                if body.get("project") and self.check_project is not None and project != parent.project:
                    raise PlanError("project : une carte fille a le projet de sa mère")
                project = parent.project
            now = self.clock()
            kind = f.pop("kind", None) or ("goal" if depth == 0 else "milestone" if depth == 1 else "task")
            c = Card(id=new_card_id(), kind=kind, created_by=by, created_at=now, updated_at=now,
                     parent_id=parent_id, project=project, **f)
            self.store.put(c)
            self._changed([c.id])
            idx = self._index()
            return self.card_json(idx, idx.by_id[c.id])

    def patch(self, card_id: str, body: dict[str, Any], by: str) -> dict[str, Any]:
        with self._lock:
            idx = self._index()
            c = self._require(idx, card_id)
            f = self._fields(body, partial=True)
            if "status" in f and idx.kids(card_id):
                raise PlanError("status : calculé depuis les cartes filles (seule une feuille le déclare)",
                                code="computed")
            touched = [card_id]
            if "project" in body and self.check_project is not None and (c.parent_id or body.get("parent_id")) \
                    and (body.get("project") or None) != c.project:
                raise PlanError("project : une carte fille a le projet de sa mère (déplacer la carte)")
            if "parent_id" in body:
                new_parent = body.get("parent_id") or None
                if new_parent != c.parent_id:
                    touched.extend(self._move(idx, c, new_parent))
            if "project" in body and not c.parent_id and self.check_project is not None:
                project = self._project(body, by)
                c.project = project
                for k in idx.subtree(c.id):
                    k.project = project
                    self.store.put(k)
            for k, v in f.items():
                setattr(c, k, v)
            c.updated_at = self.clock()
            self.store.put(c)
            self._changed(touched)
            idx = self._index()
            return self.card_json(idx, idx.by_id[c.id])

    def _move(self, idx: _Index, c: Card, new_parent: str | None) -> list[str]:
        old = c.parent_id
        if new_parent is not None:
            parent = self._require(idx, str(new_parent))
            if parent.id == c.id or parent.id in {k.id for k in idx.subtree(c.id)}:
                raise PlanError("parent_id : une carte ne descend pas sous elle-même")
            if idx.depth(parent.id) + 1 + idx.height(c.id) >= MAX_DEPTH:
                raise PlanError(f"au plus {MAX_DEPTH} niveaux")
            project = parent.project
        else:
            project = c.project
        c.parent_id = new_parent
        if project != c.project:
            c.project = project
            for k in idx.subtree(c.id):
                k.project = project
                self.store.put(k)
        return [x for x in (old,) if x]

    def delete(self, card_id: str, by: str) -> dict[str, Any]:
        with self._lock:
            idx = self._index()
            c = self._require(idx, card_id)
            lifted = []
            for k in idx.kids(card_id):
                k.parent_id = c.parent_id
                k.updated_at = self.clock()
                self.store.put(k)
                lifted.append(k.id)
            self.store.delete(card_id)
            for p in self.store.proposals(card_id):
                if p.state == "proposed":
                    p.state, p.decided_at, p.decided_by = "stale", self.clock(), by
                    self.store.put_proposal(p)
                    self._close_proposal_bubble(p)
                    self._emit_proposal(p)
            if self.publish is not None:
                self.publish("plan.card.deleted", {"id": card_id, "lifted": lifted})
            self._changed([*lifted, *([c.parent_id] if c.parent_id else [])])
            return {"deleted": True, "lifted": lifted}

    # ─────────── contexte qui descend ───────────

    def block_of(self, idx: _Index, card_id: str) -> str:
        chain = idx.chain(card_id)

        def render(short: bool) -> str:
            lines = ["[Contexte du plan — consignes héritées des cartes parentes]"]
            for depth, c in enumerate(chain):
                pad = "  " * depth
                mine = c.id == card_id
                lines.append(f"{pad}- {KIND_LABELS.get(c.kind, c.kind)} « {c.title} » ({c.id})"
                             f"{' — la carte sur laquelle tu travailles' if mine else ''}")
                intent = c.intent if (mine or not short) else c.intent[:300]
                if intent:
                    lines.append(f"{pad}  but : {intent}")
                lines.extend(f"{pad}  contrainte : {x}" for x in c.constraints)
                lines.extend(f"{pad}  décision : {x}" for x in c.decisions)
            lines.append("Les consignes d'une carte valent pour toutes ses filles : ne les contourne pas sans le "
                         "demander. Pour découper la carte, propose-le (plan_propose_split), ne crée rien.")
            lines.append("[/Contexte du plan]")
            return "\n".join(lines)

        text = render(False)
        if len(text) > MAX_BLOCK_CHARS:
            text = render(True)
        if len(text) > MAX_BLOCK_CHARS:
            text = text[:MAX_BLOCK_CHARS - 40] + "\n[…]\n[/Contexte du plan]"
        return text

    def context(self, card_id: str) -> dict[str, Any]:
        idx = self._index()
        self._require(idx, card_id)
        return {"card_id": card_id, "chain": [{"id": c.id, "kind": c.kind, "title": c.title}
                                              for c in idx.chain(card_id)], "block": self.block_of(idx, card_id)}

    def card_for_thread(self, idx: _Index, thread_id: str) -> Card | None:
        attached = [c for c in idx.by_id.values() if thread_id in c.threads]
        if attached:
            return max(attached, key=lambda c: c.updated_at)
        store = getattr(self.agents, "store", None)
        run = store.run_by_thread(thread_id) if store is not None else None
        if run is None:
            return None
        mine = [c for c in idx.by_id.values() if run.agent_id in c.agents]
        return max(mine, key=lambda c: c.updated_at) if mine else None

    def block_for_thread(self, thread_id: str) -> str:
        """Bloc du plan pour un fil (session rattachée, ou passage d'un agent listé sur une carte) ; "" sinon. Gardé
        `cache_ttl_s` et invalidé par toute écriture du plan : le Context Engine l'appelle à chaque appel modèle."""
        tenant = current_tenant_or_none() or ""
        key = (tenant, thread_id)
        now = time.monotonic()
        hit = self._blocks.get(key)
        if hit is not None and hit[0] == self._version and now - hit[1] < self.cache_ttl_s:
            return hit[2]
        idx = self._index()
        c = self.card_for_thread(idx, thread_id)
        text = self.block_of(idx, c.id) if c is not None else ""
        if len(self._blocks) > 2000:
            self._blocks.clear()
        self._blocks[key] = (self._version, now, text)
        return text

    def attach(self, card_id: str, thread_id: str, by: str) -> dict[str, Any]:
        with self._lock:
            if self.hub is not None:
                from nokker.threads.types import ThreadForbidden, ThreadNotFound

                try:
                    self.hub.require_member(thread_id, by)
                except ThreadNotFound:
                    raise PlanNotFound(f"fil inconnu : {thread_id}") from None
                except ThreadForbidden:
                    raise PlanError("fil d'un autre : seul un membre du fil le rattache", status=403,
                                    code="forbidden") from None
            idx = self._index()
            c = self._require(idx, card_id)
            touched = [card_id]
            for other in idx.by_id.values():  # un fil travaille sur UNE carte à la fois
                if other.id != card_id and thread_id in other.threads:
                    other.threads = [t for t in other.threads if t != thread_id]
                    self.store.put(other)
                    touched.append(other.id)
            if thread_id not in c.threads:
                c.threads = [*c.threads, thread_id][-50:]
            c.updated_at = self.clock()
            self.store.put(c)
            self._changed(touched)
            idx = self._index()
            return self.card_json(idx, idx.by_id[c.id])

    def detach(self, card_id: str, thread_id: str, by: str) -> dict[str, Any]:
        with self._lock:
            idx = self._index()
            c = self._require(idx, card_id)
            c.threads = [t for t in c.threads if t != thread_id]
            c.updated_at = self.clock()
            self.store.put(c)
            self._changed([card_id])
            idx = self._index()
            return self.card_json(idx, idx.by_id[c.id])

    # ─────────── propositions de découpe ───────────

    def propose_split(self, card_id: str, children: Any, reason: str, by: str,
                      thread_id: str | None = None) -> Proposal:
        """Une PROPOSITION : aucune carte n'est créée ici ; un humain accepte ou refuse."""
        idx = self._index()
        card = self._require(idx, card_id)
        if idx.depth(card_id) + 1 >= MAX_DEPTH:
            raise PlanError(f"au plus {MAX_DEPTH} niveaux : cette carte ne se découpe plus")
        if not isinstance(children, list) or not children:
            raise PlanError("children : liste non vide de {title, kind?, intent?, constraints?}")
        if len(children) > MAX_CHILDREN_PROPOSED:
            raise PlanError(f"children : {MAX_CHILDREN_PROPOSED} au plus")
        clean = []
        for ch in children:
            if not isinstance(ch, dict):
                raise PlanError("children : objets {title, kind?, intent?, constraints?} attendus")
            f = self._fields({k: ch.get(k) for k in ("title", "kind", "intent", "constraints") if k in ch},
                             partial=False)
            clean.append({"title": f["title"], "kind": f.get("kind"), "intent": f.get("intent", ""),
                          "constraints": f.get("constraints", [])})
        p = Proposal(id=new_proposal_id(), card_id=card.id, proposed_by=by, created_at=self.clock(), children=clean,
                     reason=self._clean(" ".join(str(reason or "").split())[:1000]), thread_id=thread_id)
        if self.hub is not None and thread_id and self.hub.threads.get(thread_id) is not None:
            b = self.hub.create_kernel_bubble(
                thread_id, kind="plan_split", title=f"Découper « {card.title} »"[:200],
                state={"proposal_id": p.id, "card_id": card.id, "card_title": card.title, "children": clean,
                       "reason": p.reason, "state": p.state},
                actions=[{"id": "accept", "label": "Créer ces cartes", "primary": True},
                         {"id": "refuse", "label": "Non"}])
            p.bubble_id = b.id
        self.store.put_proposal(p)
        self._emit_proposal(p)
        return p

    def proposals(self, card_id: str | None = None, state: str | None = None) -> list[Proposal]:
        items = [p for p in self.store.proposals(card_id) if state is None or p.state == state]
        return sorted(items, key=lambda p: p.created_at, reverse=True)

    def _close_proposal_bubble(self, p: Proposal) -> None:
        if self.hub is None or not p.bubble_id:
            return
        try:
            b = self.hub.bubbles.get(p.bubble_id)
            if b is not None and b.closed_at is None:
                self.hub.update_from_kernel(p.bubble_id, state={**(b.state or {}), "state": p.state,
                                                                "created": list(p.created)},
                                            status="done" if p.state == "accepted" else "failed")
                self.hub._close(p.bubble_id)
        except Exception:  # noqa: BLE001 — la bulle suit, la décision est déjà rangée
            logger.exception("bulle de la proposition %s non mise à jour", p.id)

    def decide(self, proposal_id: str, decision: str, by: str) -> dict[str, Any]:
        if decision not in ("accept", "refuse"):
            raise PlanError("décision : accept | refuse")
        with self._lock:
            p = self.store.get_proposal(proposal_id)
            if p is None:
                raise PlanNotFound(f"proposition inconnue : {proposal_id}")
            if p.state != "proposed":
                raise PlanError(f"proposition déjà {p.state}", status=409, code="decided")
            idx = self._index()
            card = idx.by_id.get(p.card_id)
            created: list[dict[str, Any]] = []
            if card is None:
                p.state = "stale"
            elif decision == "refuse":
                p.state = "refused"
            else:
                for i, ch in enumerate(p.children):
                    out = self.create({"parent_id": card.id, "title": ch["title"], "kind": ch.get("kind"),
                                       "intent": ch.get("intent", ""), "constraints": ch.get("constraints", []),
                                       "position": i}, by)
                    created.append(out)
                p.created = [c["id"] for c in created]
                p.state = "accepted"
            p.decided_by, p.decided_at = by, self.clock()
            self.store.put_proposal(p)
            self._close_proposal_bubble(p)
            self._emit_proposal(p)
            if p.state == "stale":
                raise PlanError("la carte à découper n'existe plus", status=409, code="stale")
            return {"proposal": proposal_to_json(p), "cards": created}

    # ─────────── brief du matin ───────────

    def brief_data(self, sender: str | None = None) -> dict[str, Any]:
        """Ce que l'outil `plan_brief` rend : cartes en cours, bloquées, prochaines ; incidents vivants ; agenda."""
        idx = self._index()
        memo: dict[str, dict[str, int]] = {}

        def short(c: Card) -> dict[str, Any]:
            r = _rollup(idx, c.id, memo)
            return {"id": c.id, "kind": c.kind, "title": c.title, "status": effective_status(idx, c, memo),
                    "progress": f"{r['done']}/{r['total']}",
                    "path": " › ".join(a.title for a in idx.chain(c.id)[:-1]) or None}

        leaves = [c for c in idx.by_id.values() if not idx.kids(c.id)]
        leaves.sort(key=lambda c: (idx.depth(c.id), c.position, c.created_at))
        out: dict[str, Any] = {
            "date": iso(self.clock()),
            "goals": [short(c) for c in idx.kids(None)][:20],
            "in_progress": [short(c) for c in leaves if c.status == "in_progress"][:20],
            "blocked": [short(c) for c in leaves if c.status == "blocked"][:20],
            "next": [short(c) for c in leaves if c.status == "todo"][:10],
        }
        op = self.operate() if self.operate is not None else None
        if op is None:
            out["incidents"] = None
        else:
            out["incidents"] = [{"id": i.id, "title": i.title, "name": i.name, "severity": i.severity,
                                 "state": i.state, "since": iso(i.first_seen_at), "occurrences": i.occurrences}
                                for i in op.open_incidents()][:20]
        has_cal = bool(self.has_tool is not None and self.has_tool(CALENDAR_TOOL))
        out["agenda"] = ("appelle calendar_events pour la journée" if has_cal
                         else "aucun connecteur d'agenda sur ce compte : ne pas inventer d'agenda")
        return out

    def get_brief(self, owner: str) -> Any:
        aid = self.store.brief_agent(owner)
        a = self.agents.store.get(aid) if aid and self.agents is not None else None
        return a

    def ensure_brief(self, body: dict[str, Any], owner: str) -> tuple[Any, bool]:
        """L'agent du brief de `owner` (créé et armé si absent ; son horaire changé si `cron`/`tz` sont donnés).
        Le POST de l'humain est la décision : l'agent ne lève aucune écriture, il s'arme sans carte."""
        if self.agents is None or getattr(self.agents, "engine", None) is None:
            raise PlanError("moteur Crew absent sur cette instance : pas de brief planifié", status=503,
                            code="engine_unavailable")
        trigger = {"kind": "schedule", "cron": str(body.get("cron") or BRIEF_CRON),
                   "tz": str(body.get("tz") or BRIEF_TZ)}
        existing = self.get_brief(owner)
        if existing is not None:
            if "cron" in body or "tz" in body:
                self.agents.patch_agent(existing.id, {"trigger": trigger}, owner)
                existing, _ = self.agents.arm(existing.id, owner)
            return existing, False
        tools = [BRIEF_TOOL]
        has_cal = bool(self.has_tool is not None and self.has_tool(CALENDAR_TOOL))
        if has_cal:
            tools.append(CALENDAR_TOOL)
        a = self.agents.create_from_body({
            "name": BRIEF_NAME, "role": "brief du matin", "model": str(body.get("model") or ""),
            "instructions": brief_instructions(has_cal), "trigger": trigger, "tools": tools,
            "budget": {"max_calls": 8}}, owner)
        self.store.set_brief(owner, a.id, self.clock())
        a, _ = self.agents.arm(a.id, owner)
        return a, True

    def delete_brief(self, owner: str) -> bool:
        a = self.get_brief(owner)
        if a is not None:
            self.agents.delete_agent(a.id, owner)
        found = self.store.brief_agent(owner) is not None
        self.store.set_brief(owner, None, self.clock())
        return found

    def on_run(self, run: Any) -> None:
        """Fin d'un passage du brief : sa réponse dans une bulle `brief` de son fil, et une notification."""
        if run.status != "succeeded" or current_tenant_or_none() is None:
            return
        try:
            owner = self.store.brief_owner(run.agent_id)
            if owner is None or self.hub is None or not run.thread_id:
                return
            text = ""
            if self.history is not None:
                turns = self.history(run.thread_id)
                text = (turns[-1].reply or "") if turns else ""
            text = self._clean((text or run.summary or "").strip())[:8000]
            title = f"Brief du {iso(run.started_at or self.clock())[:10]}"
            b = self.hub.create_kernel_bubble(run.thread_id, kind="brief", title=title, status="done",
                                              state={"text": text, "run_id": run.id, "agent_id": run.agent_id,
                                                     "date": iso(run.started_at or self.clock())})
            self.hub.notify_user(owner, title=title, text=text, from_thread=run.thread_id)
            logger.info("brief %s rangé dans la bulle %s", run.id, b.id)
        except Exception:  # noqa: BLE001 — un brief non rangé ne casse jamais le passage
            logger.exception("brief : passage %s non rangé", getattr(run, "id", "?"))
            return
        if self.delivery is not None:
            try:
                self.delivery.deliver(owner, title, text)  # texte déjà caviardé (`_clean`), recaviardé là-bas
            except Exception:  # noqa: BLE001 — une livraison ratée ne casse jamais le brief
                logger.exception("brief : passage %s non livré hors de l'app", getattr(run, "id", "?"))


def brief_instructions(has_calendar: bool) -> str:
    agenda = ("Appelle ensuite calendar_events pour les événements d'aujourd'hui (lecture seulement)."
              if has_calendar else "Il n'y a pas de connecteur d'agenda : n'invente aucun rendez-vous.")
    return ("Prépare le brief du matin de ton propriétaire. Appelle d'abord plan_brief : il rend les objectifs et leur "
            f"avancement, les tâches en cours, bloquées et les prochaines, et les incidents ouverts. {agenda} "
            "Rédige ensuite un brief court en français : 1) ce qui brûle (incidents, tâches bloquées), 2) ce qui est "
            "en cours, 3) l'agenda du jour, 4) les deux ou trois prochaines tâches utiles. Ne modifie rien, ne crée "
            "rien : c'est une lecture.")


__all__ = ["BRIEF_CRON", "BRIEF_NAME", "BRIEF_TOOL", "BRIEF_TZ", "CALENDAR_TOOL", "Plan", "brief_instructions",
           "effective_status"]
