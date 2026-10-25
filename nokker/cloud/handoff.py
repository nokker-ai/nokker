"""Délégation web → Lokkal par besoin (lot X21, docs/LOKKAL.md §6.7 et §7.16), côté nokker.ai.

Une session web qui a besoin d'une ressource `local` (un dépôt, un hôte ssh, un connecteur posé dans Lokkal, un
serveur MCP de la machine) ne la reçoit jamais. Elle PROPOSE de passer le relais :

  1. **proposition** (outil `lokkal_handoff {need: {kind, name}, task}`, effet READ : rien ne s'exécute) : la machine
     est choisie parmi celles du compte qui DÉCLARENT la ressource (inventaire par noms, `POST
     /v1/gateway/machine/resources`) ; s'il y en a plusieurs, la logique de placement de X15
     (`nokker/cloud/placement.py` : en ligne, RAM libre d'abord, puis CPU relatif, puis tours actifs) ; aucune en
     ligne : la plus récemment vue, et la proposition le dit (« la demande attendra ») ;
  2. **acceptation** par l'utilisateur, et par lui seul : action `accept` de la bulle `handoff`, ou
     `POST /v1/handoffs/{id}/accept` (session du compte). Le modèle n'a aucun outil pour accepter ;
  3. **exécution** : machine en ligne → session Lokkal ouverte par la passerelle avec un jeton de délégation (portée
     `lokkal:session`, la demande nommée), `POST /v1/local/sessions {resource}` puis `POST /v1/chat/completions` avec
     la tâche ; machine éteinte → `waiting`, reprise dès que sa charge revient (`machine_online`, appelé à la
     réception de la charge). La session Lokkal travaille avec SES permissions et SES cartes (téléphone, X10) ;
  4. **résultat** : la réponse finale de la session Lokkal, et rien d'autre (caviardée, bornée), revient en bulle
     dans le fil web et dans le contexte de son prochain tour. Une réponse qui attend une carte (`awaiting_approval`)
     est complétée par la machine quand la carte est décidée (`POST /v1/gateway/handoffs/{id}/result`).

Cloisonnement : tout est rangé sous le locataire (RLS, `lokkal_handoffs`, migration 025) et lu avec le compte ; une
demande d'un autre compte n'existe pas (404). Jamais d'exécution distante sans acceptation : `_run` ne part que d'un
état `accepted` ou `waiting`, que seule `decide(..., "accept")` pose.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from typing import Any

import httpx

from nokker.cloud import placement
from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult

logger = logging.getLogger("nokker.cloud.handoff")

TOOL = "lokkal_handoff"
FRESH_S = placement.FRESH_S  # charge plus vieille : machine éteinte, hors ligne ou ancienne version (LOKKAL §7.2)
PROPOSAL_TTL_S = 3600  # une proposition non acceptée expire au bout d'une heure
WAIT_TTL_S = 24 * 3600  # une demande acceptée attend sa machine 24 h au plus
# Passerelle injoignable alors que la machine donne signe de vie (revue du train du 2.11, défaut 3) : relances
# espacées (1, 2, 4, 8 min… après l'essai n), au plus RELAY_MAX_ATTEMPTS essais, puis `failed` ; jamais de relance
# automatique ensuite (l'utilisateur relance depuis le fil).
RELAY_MAX_ATTEMPTS = 5
RELAY_BACKOFF_S = 60.0
RELAY_BACKOFF_CAP_S = 1800.0
MAX_TASK = 8000
MAX_REPLY = 8000
NEED_KINDS = {"repo": "repos", "host": "hosts", "connector": "connectors", "mcp": "mcp"}
KIND_LABEL = {"repo": "dépôt", "host": "hôte", "connector": "connecteur", "mcp": "serveur MCP"}
OPEN = ("proposed", "accepted", "waiting", "running", "awaiting_approval")
FINAL = ("done", "failed", "refused", "expired")

_SPEC = {"type": "object", "required": ["need", "task"], "properties": {
    "need": {"type": "object", "required": ["kind", "name"], "properties": {
        "kind": {"type": "string", "enum": list(NEED_KINDS),
                 "description": "repo = dépôt de la machine, host = hôte ssh (hosts.yaml), connector = connecteur posé "
                                "dans Lokkal, mcp = serveur MCP de la machine"},
        "name": {"type": "string", "description": "son NOM tel que la machine le déclare (ex. « monapp », « prod-db »)"}}},
    "task": {"type": "string", "description": "la tâche à confier, en entier : la session Lokkal ne voit pas ce fil"}}}


class HandoffError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Handoff:
    id: str
    tenant: str
    account: str
    web_thread: str
    kind: str
    name: str
    task: str
    machine_id: str
    machine_name: str
    state: str = "proposed"
    created_at: float = 0.0
    updated_at: float = 0.0
    online: bool = False  # machine en ligne au moment de la proposition
    lokkal_thread: str | None = None
    reply: str | None = None
    error: str | None = None
    turn_index: int = -1  # dernier tour Lokkal dont le résultat est rangé (une réponse ne s'applique qu'une fois)
    pending_approvals: int = 0
    attempts: int = 0  # essais d'ouverture de session ratés faute de passerelle (migration 030) ; borné, puis `failed`


def relay_backoff_s(attempts: int) -> float:
    """Attente minimale avant l'essai suivant, après `attempts` essais ratés (1 min, 2, 4, 8… plafonnée à 30 min)."""
    return min(RELAY_BACKOFF_CAP_S, RELAY_BACKOFF_S * (2 ** max(0, attempts - 1)))


def view(h: Handoff) -> dict[str, Any]:
    """Forme publique (routes, bulle) : jamais le locataire."""
    d = asdict(h)
    d.pop("tenant", None)
    d["need"] = {"kind": d.pop("kind"), "name": d.pop("name")}
    d["handoff_id"] = d.pop("id")
    return d


class MemoryHandoffStore:
    """Demandes de relais (tests, dev). `PgHandoffStore` (nokker/cloud/pg.py) a la même interface."""

    def __init__(self) -> None:
        self.rows: dict[str, Handoff] = {}
        self.lock = threading.Lock()

    def put(self, h: Handoff) -> None:
        with self.lock:
            self.rows[h.id] = replace(h)

    def get(self, tenant: str, handoff_id: str) -> Handoff | None:
        with self.lock:
            h = self.rows.get(handoff_id)
            return replace(h) if h is not None and h.tenant == tenant else None

    def transition(self, tenant: str, handoff_id: str, expect: tuple[str, ...], **changes: Any) -> Handoff | None:
        """Passage d'état atomique : appliqué seulement si l'état courant est dans `expect` ; None sinon."""
        with self.lock:
            h = self.rows.get(handoff_id)
            if h is None or h.tenant != tenant or h.state not in expect:
                return None
            if "turn_index" in changes and changes["turn_index"] is not None and changes["turn_index"] <= h.turn_index:
                return None  # résultat déjà rangé (réponse synchrone ET relève de la machine)
            self.rows[handoff_id] = h = replace(h, **changes)
            return replace(h)

    def of_account(self, tenant: str, account: str, web_thread: str | None = None) -> list[Handoff]:
        with self.lock:
            out = [replace(h) for h in self.rows.values() if h.tenant == tenant and h.account == account
                   and (web_thread is None or h.web_thread == web_thread)]
        return sorted(out, key=lambda h: -h.created_at)[:100]

    def waiting_for(self, tenant: str, machine_id: str) -> list[Handoff]:
        with self.lock:
            return [replace(h) for h in self.rows.values()
                    if h.tenant == tenant and h.machine_id == machine_id and h.state == "waiting"]


def load_score(load: dict[str, Any] | None) -> float:
    """Plus petit = moins chargée : charge 1 min par CPU + tours en cours + 0,25 × sessions ouvertes + part de RAM
    utilisée. Une valeur absente compte pour 0 (la charge est bornée par `clean_load`). Indicateur seulement depuis
    X15 : le choix classe par `placement.rank_key` (RAM libre d'abord)."""
    if not isinstance(load, dict):
        return float("inf")
    cpu, ram = load.get("cpu") or {}, load.get("ram") or {}
    count = cpu.get("count") or 1
    score = float(cpu.get("load1") or 0) / float(count) if count else 0.0
    score += float(load.get("active_turns") or 0) + 0.25 * float(load.get("sessions") or 0)
    total, free = ram.get("total_mb"), ram.get("free_mb")
    if total and free is not None:
        score += max(0.0, 1.0 - float(free) / float(total))
    return score


def is_online(m: Any, now: float) -> bool:
    return placement.is_online(m, now)


def candidates(machines: list[Any], kind: str, name: str) -> list[Any]:
    """Machines qui DÉCLARENT la ressource (par nom, dans leur inventaire)."""
    needs = placement.Needs.of(kind, name)
    return [m for m in machines if needs.resources() and not placement.lacks(m, needs)]


def choose(machines: list[Any], kind: str, name: str, now: float) -> tuple[Any | None, bool]:
    """(machine, en ligne) : la logique de placement de X15 (`nokker/cloud/placement.py`, la même que pour les agents)
    sur le besoin nommé — la meilleure des machines en ligne qui ont la ressource ; sinon la plus récemment vue de
    celles qui l'ont (la demande attendra) ; (None, False) si aucune ne la déclare."""
    if kind not in NEED_KINDS:
        return None, False
    p = placement.place(machines, placement.Needs.of(kind, name), now)
    if p.machine is not None:
        return p.machine, True
    return p.fallback, False


def task_message(h: Handoff, web_title: str = "") -> str:
    """Ce que reçoit la session Lokkal : la tâche, le besoin, et la règle du retour (seul le résultat repart)."""
    where = f" « {web_title} »" if web_title else ""
    return (f"[Tâche confiée par ta session web{where} de nokker.ai — besoin : {KIND_LABEL.get(h.kind, h.kind)} "
            f"« {h.name} »]\n{h.task}\n\n[Ta réponse finale revient dans le fil web, et elle seule : n'y mets que le "
            "résultat utile (ce qui a été fait, ce qui reste, ce qui bloque), jamais un secret ni le contenu d'un "
            "fichier qui n'est pas demandé.]")


@dataclass
class HandoffService:
    links: Any  # LinkService
    signer: Any  # Signer
    store: Any = field(default_factory=MemoryHandoffStore)
    scrub: Callable[[str], str] = field(default=lambda s: s)  # invariants.scrub_output de nokker.ai
    on_update: Callable[[Handoff], None] | None = None  # bulle et contexte du fil web (nokker/bubbles, posé par serve)
    spawn: Callable[[Callable[[], None]], None] = field(
        default=lambda fn: threading.Thread(target=fn, name="nokker-handoff", daemon=True).start())
    client: Any = None  # httpx.Client injectable (tests)
    scheme: str = "https"
    timeout_s: float = 900.0
    clock: Callable[[], float] = time.time

    # ── proposition (outil du modèle) ──

    def propose(self, account: str, tenant: str, web_thread: str, kind: str, name: str,
                task: str) -> tuple[Handoff | None, str]:
        """(demande, message). Rien ne s'exécute : la demande attend l'acceptation de l'utilisateur."""
        name, task = str(name or "").strip(), str(task or "").strip()
        if kind not in NEED_KINDS or not name or not task:
            return None, f"need.kind ∈ {list(NEED_KINDS)}, need.name et task requis"
        if len(task) > MAX_TASK:
            return None, f"tâche trop longue ({len(task)} car., {MAX_TASK} au plus) : résume-la"
        now = self.clock()
        machines = self.links.machines_with_load(account, tenant)
        if not machines:
            return None, "aucune machine liée à ce compte : installe Nokker Lokkal et relie-le (app.nokker.ai/link)"
        m, online = choose(machines, kind, name, now)
        if m is None:
            known = sorted({n for x in machines if isinstance(x.resources, dict)
                            for n in (x.resources.get(NEED_KINDS[kind]) or [])})
            hint = f" ; déclarés : {', '.join(known[:30])}" if known else ""
            return None, (f"aucune machine liée ne déclare le {KIND_LABEL[kind]} « {name} »{hint}. Une machine "
                          "éteinte depuis longtemps n'a peut-être pas encore envoyé son inventaire.")
        h = Handoff(id=f"ho_{uuid.uuid4().hex[:16]}", tenant=tenant, account=account, web_thread=web_thread,
                    kind=kind, name=name, task=task, machine_id=m.id, machine_name=m.name, created_at=now,
                    updated_at=now, online=online)
        self.store.put(h)
        when = "" if online else " (machine éteinte : la demande attendra qu'elle revienne)"
        return h, (f"proposition {h.id} : passer le relais à la machine « {m.name} »{when}. Rien ne s'exécute tant que "
                   "l'utilisateur n'a pas accepté (bulle dans le fil).")

    # ── décision (utilisateur seulement) ──

    def get(self, tenant: str, account: str, handoff_id: str) -> Handoff:
        h = self.store.get(tenant, handoff_id)
        if h is None or h.account != account:
            raise HandoffError(404, "demande inconnue")
        return self._expire(h)

    def list(self, tenant: str, account: str, web_thread: str | None = None) -> list[Handoff]:
        return [self._expire(h) for h in self.store.of_account(tenant, account, web_thread)]

    def _expire(self, h: Handoff) -> Handoff:
        now = self.clock()
        if h.state == "proposed" and now - h.created_at > PROPOSAL_TTL_S \
                or h.state == "waiting" and now - h.updated_at > WAIT_TTL_S:
            done = self.store.transition(h.tenant, h.id, (h.state,), state="expired", updated_at=now)
            if done is not None:
                self._notify(done)
                return done
        return h

    def decide(self, tenant: str, account: str, handoff_id: str, decision: str) -> Handoff:
        """`accept` (proposée → acceptée, puis exécution) ou `refuse` (proposée ou en attente → refusée)."""
        h = self.get(tenant, account, handoff_id)
        now = self.clock()
        if decision == "refuse":
            done = self.store.transition(tenant, h.id, ("proposed", "waiting"), state="refused", updated_at=now)
            if done is None:
                raise HandoffError(409, f"demande déjà {h.state}")
            self._notify(done)
            return done
        if decision != "accept":
            raise HandoffError(400, "decision ∈ accept | refuse")
        accepted = self.store.transition(tenant, h.id, ("proposed",), state="accepted", updated_at=now)
        if accepted is None:
            raise HandoffError(409, f"demande déjà {h.state}")
        self._notify(accepted)
        self.spawn(lambda: self._run(accepted))
        return accepted

    # ── exécution ──

    def machine_online(self, machine: Any) -> int:
        """La machine vient d'envoyer sa charge : ses demandes acceptées qui l'attendaient partent. Rend leur nombre.
        Une demande dont la passerelle a déjà échoué attend `relay_backoff_s(attempts)` depuis son dernier essai : le
        battement de la machine (60 s) ne la relance pas à chaque fois."""
        now = self.clock()
        due = [h for h in self.store.waiting_for(machine.tenant, machine.id)
               if not h.attempts or now - h.updated_at >= relay_backoff_s(h.attempts)]
        for h in due:
            self.spawn(lambda h=h: self._run(h))
        return len(due)

    def _http(self) -> Any:
        return self.client or httpx.Client(timeout=self.timeout_s)

    def _run(self, h: Handoff) -> None:
        try:
            self._run_inner(h)
        except Exception:  # noqa: BLE001 — un fil de fond qui meurt laisserait la demande « en cours » à jamais
            logger.exception("relais %s en échec", h.id)
            self._finish(h, ("running",), state="failed", error="erreur interne de nokker.ai")

    def _run_inner(self, h: Handoff) -> None:
        now = self.clock()
        m = next((x for x in self.links.machines_with_load(h.account, h.tenant) if x.id == h.machine_id), None)
        if m is None:
            self._finish(h, ("accepted", "waiting"), state="failed", error="machine déliée ou révoquée")
            return
        if not is_online(m, now):
            self._finish(h, ("accepted",), state="waiting", online=False)
            return
        running = self.store.transition(h.tenant, h.id, ("accepted", "waiting"), state="running", updated_at=now,
                                        online=True, error=None)
        if running is None:
            return  # déjà lancée par un autre passage (acceptation et retour de la machine en même temps)
        self._notify(running)
        token = self.signer.delegation_token(h.account, h.tenant, machine=m.id, web_session=h.web_thread,
                                             handoff=h.id)
        base = f"{self.scheme}://{m.host}"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        http = self._http()
        try:
            r = http.post(base + "/v1/local/sessions", headers=headers,
                          json={"resource": {"kind": h.kind, "name": h.name}})
        except httpx.HTTPError:
            # Rien n'a été envoyé à la session : nouvel essai plus tard, espacé et borné (jamais à chaque battement).
            n = running.attempts + 1
            if n >= RELAY_MAX_ATTEMPTS:
                self._finish(running, ("running",), state="failed", online=False, attempts=n,
                             error=f"la passerelle n'a pas pu joindre la machine « {m.name} » après {n} essais : rien "
                                   "n'a été envoyé ; relance la demande quand la machine sera joignable (tunnel "
                                   "ouvert, « nokker doctor » sur la machine)")
            else:
                wait = int(relay_backoff_s(n) // 60)
                self._finish(running, ("running",), state="waiting", online=False, attempts=n,
                             error=f"machine injoignable par la passerelle (essai {n}/{RELAY_MAX_ATTEMPTS}) : nouvel "
                                   f"essai dans {wait} min au plus tôt, au prochain signe de vie de la machine")
            return
        if r.status_code != 201:
            self._finish(running, ("running",), state="failed",
                         error=f"la machine « {m.name} » refuse la session ({r.status_code}) : {_msg(r)}")
            return
        try:
            thread = str(r.json()["thread_id"])
        except (ValueError, KeyError, TypeError):
            self._finish(running, ("running",), state="failed", error="réponse illisible de la machine")
            return
        running = self.store.transition(h.tenant, h.id, ("running",), lokkal_thread=thread) or running
        try:
            r = http.post(base + "/v1/chat/completions", headers={**headers, "X-Session-Id": thread},
                          json={"model": "nokker", "messages": [{"role": "user", "content": task_message(h)}]})
            data = r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            # La tâche est peut-être en cours sur la machine : pas de relance automatique.
            self._finish(running, ("running",), state="failed",
                         error="la session Lokkal ne répond plus : la tâche a peut-être été faite, vérifier sur la "
                               f"machine (fil {thread}) avant de la relancer")
            return
        if data is None:
            self._finish(running, ("running",), state="failed",
                         error=f"la session Lokkal a échoué ({r.status_code}) : {_msg(r)}")
            return
        try:
            reply = str(data["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError):
            reply = ""
        ext = data.get("nokker") if isinstance(data.get("nokker"), dict) else {}
        self.result(h.tenant, h.id, machine_id=m.id, reply=reply, turn_index=_int(ext.get("turn_index"), 0),
                    pending_approvals=_int(ext.get("pending_approvals"), 0))

    def result(self, tenant: str, handoff_id: str, *, machine_id: str, reply: str, turn_index: int,
               pending_approvals: int = 0) -> Handoff | None:
        """Résultat d'un tour de la session Lokkal (réponse synchrone, ou relève de la machine après une carte) :
        rangé une fois par tour, caviardé, borné. Une demande d'une autre machine : None."""
        h = self.store.get(tenant, handoff_id)
        if h is None or h.machine_id != machine_id:
            return None
        text = self.scrub(str(reply or ""))
        if len(text) > MAX_REPLY:
            text = text[:MAX_REPLY] + "\n[… réponse coupée]"
        state = "awaiting_approval" if pending_approvals > 0 else "done"
        return self._finish(h, ("running", "awaiting_approval"), state=state, reply=text, turn_index=turn_index,
                            pending_approvals=max(0, pending_approvals), error=None)

    def _finish(self, h: Handoff, expect: tuple[str, ...], **changes: Any) -> Handoff | None:
        done = self.store.transition(h.tenant, h.id, expect, updated_at=self.clock(), **changes)
        if done is not None:
            self._notify(done)
        return done

    def _notify(self, h: Handoff) -> None:
        if self.on_update is None:
            return
        try:
            self.on_update(h)
        except Exception:  # noqa: BLE001 — une bulle en panne ne change pas l'état de la demande
            logger.exception("bulle du relais %s non mise à jour", h.id)


def _int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _msg(r: Any) -> str:
    try:
        return str(r.json().get("error", {}).get("message") or r.text[:200])
    except (ValueError, AttributeError):
        return str(getattr(r, "text", ""))[:200]


# ─────────────────────────────── outil du modèle ───────────────────────────────

MARK_OPEN, MARK_CLOSE = "<!-- nokker:handoff -->", "<!-- /nokker:handoff -->"


@dataclass
class HandoffTools:
    """`lokkal_handoff` : PROPOSE un relais ; effet READ (il n'exécute rien et ne touche aucune machine : seule
    l'acceptation de l'utilisateur, hors du modèle, lance la session Lokkal). Le résultat porte un bloc
    `nokker:handoff` lu par le producteur de bulles (bulle `handoff` avec ses actions)."""

    service: HandoffService
    tenant_of: Callable[[str], str | None]

    def available_for(self, tenant_id: str) -> bool:
        """Outil proposé seulement à un compte qui a une machine liée et non révoquée (`TenantRegistry`)."""
        return self.service.links.has_machine(tenant_id)

    def specs(self) -> list[ToolSpec]:
        return [ToolSpec(name=TOOL, parameters=_SPEC, effect=Effect.READ, locality="cloud",
                         description="Propose de passer le relais à ta machine Lokkal qui a la ressource locale "
                                     "(dépôt, hôte ssh, connecteur local) ; l'utilisateur accepte, puis la machine "
                                     "fait la tâche et rend son résultat.")]

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def out(text: str, error: bool = False) -> ToolResult:
            return ToolResult(call_id="", name=name, content=text, is_error=error)

        account = ctx.sender or ""
        tenant = self.tenant_of(account) if account.startswith("us_") else None
        if not tenant:
            return out("délégation réservée aux comptes nokker.ai qui ont relié une machine", True)
        need = arguments.get("need") if isinstance(arguments.get("need"), dict) else {}
        h, msg = self.service.propose(account, tenant, ctx.thread_id, str(need.get("kind") or ""),
                                      str(need.get("name") or ""), str(arguments.get("task") or ""))
        if h is None:
            return out(msg, True)
        block = json.dumps(view(h), ensure_ascii=False)
        return out(f"{msg}\n{MARK_OPEN}{block}{MARK_CLOSE}")


def handoff_state(content: str) -> dict[str, Any] | None:
    """Le bloc `nokker:handoff` d'un résultat d'outil (producteur de bulles)."""
    i, j = (content or "").find(MARK_OPEN), (content or "").find(MARK_CLOSE)
    if i < 0 or j < i:
        return None
    try:
        data = json.loads(content[i + len(MARK_OPEN):j])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) and str(data.get("handoff_id", "")).startswith("ho_") else None


STATUS = {"proposed": "idle", "accepted": "working", "waiting": "idle", "running": "working",
          "awaiting_approval": "working", "done": "done", "failed": "failed", "refused": "done", "expired": "failed"}


def bubble_state(v: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    """(state, status, titre) de la bulle `handoff` à partir de la forme publique d'une demande (`view`). Jamais le
    locataire ; la tâche telle que le modèle l'a écrite (l'utilisateur doit voir ce qu'il accepte)."""
    need = v.get("need") or {}
    keep = ("handoff_id", "state", "machine_id", "machine_name", "online", "task", "lokkal_thread", "reply", "error",
            "pending_approvals", "created_at", "updated_at")
    state = {k: v.get(k) for k in keep}
    state["need"] = {"kind": need.get("kind"), "name": need.get("name")}
    title = f"Relais vers « {v.get('machine_name') or '?'} » — {KIND_LABEL.get(str(need.get('kind')), '')} " \
            f"« {need.get('name') or ''} »"
    return state, STATUS.get(str(v.get("state")), "idle"), title[:200]


def bubble_updater(hub: Any) -> Callable[[Handoff], None]:
    """`HandoffService.on_update` branché sur les bulles du fil web : met à jour la bulle `handoff` de la demande et,
    au résultat, dépose la réponse de la machine dans le contexte du prochain tour du fil (`[Contexte reçu]`)."""
    from nokker.tenancy.context import tenant_scope

    def update(h: Handoff) -> None:
        state, status, _ = bubble_state(view(h))
        with tenant_scope(h.tenant):
            b = next((x for x in hub.bubbles.list_thread(h.web_thread, include_closed=True)
                      if x.kind == "handoff" and (x.state or {}).get("handoff_id") == h.id), None)
            if b is not None:
                hub.update_from_kernel(b.id, state=state, status=status)
            if h.state in ("done", "awaiting_approval") and h.reply:
                head = f"[Résultat de la machine « {h.machine_name} » (relais {h.id})]"
                if h.state == "awaiting_approval":
                    head += " — une carte attend une décision sur la machine ou au téléphone"
                hub.threads.push_context(h.web_thread, f"{head} {h.reply[:2000]}")
            elif h.state in ("failed", "expired", "refused"):
                hub.threads.push_context(h.web_thread, f"[Relais {h.id} vers « {h.machine_name} » : {h.state}"
                                                       f"{' — ' + h.error if h.error else ''}]")

    return update


__all__ = ["FRESH_S", "RELAY_MAX_ATTEMPTS", "TOOL", "Handoff", "HandoffError", "HandoffService", "HandoffTools", "MemoryHandoffStore",
           "bubble_state", "bubble_updater", "choose", "handoff_state", "load_score", "relay_backoff_s",
           "task_message", "view"]
