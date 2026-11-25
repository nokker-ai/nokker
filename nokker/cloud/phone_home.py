"""Le téléphone en nœud maison (P10a, docs/LOKKAL-PHONE.md §4.4) : un vieux téléphone ou une tablette, branché, sur le
Wi-Fi de la maison, parle à Home Assistant sur le LAN avec le jeton d'un compte HA non administrateur saisi SUR
l'appareil. Le noyau du cloud ne voit jamais ce jeton : il dépose des opérations nommées `ha.*` dans la file du
téléphone (mêmes noms, arguments et résultats que `home-bridge`, nokker/homebridge/ha.py::HaOps).

`PhoneBridge.op(nom, payload)` a l'interface de `nokker.ambient.bridge.HomeBridge` : la source maison
(nokker/home/source.py) s'en sert sans le savoir. Échec : `BridgeError` au format de HomeBridge (« passerelle du foyer :
<op> en échec (<code>) — <phrase> »), la phrase venant de la table fermée `phone_tools.REASONS`.

- **Placement** (nokker/cloud/placement.py) : capacité `lan_home`, déclarée seulement sur le Wi-Fi « maison » ; mode
  `home` avant `pocket` ; un Lokkal avec home-bridge passe avant (la source cloud n'existe que sans Lokkal relié).
- **Propriétaire seulement** : le nœud sert le compte auquel il est rattaché, dans son locataire ; aucune traversée de
  locataire. Le partage avec le foyer sera un lot à part (carte par membre, révocable, journalisée, comme 068).
- **Actions** (`ha.call_service`, `ha.notify`, `ha.tracker_see`) : portées par une décision maison posée par la
  source (`decision()` ci-dessous, contexte du tour) ; le noyau joint l'attestation signée (PhoneService.attest) et le
  téléphone vérifie, recalcule args_sha256 et applique SA liste blanche. Toute autre action est refusée.
- `ha.events {after, timeout ≤ 20}` passe par la file comme le reste.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from nokker.ambient.bridge import BridgeError
from nokker.core import model_texts as mt

_DECISION: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("nokker_home_decision", default=None)
WAIT_PAD_S = 5.0  # au-delà du délai propre de l'opération (ha.events : son timeout)
WAIT_S = 20.0


@contextlib.contextmanager
def decision(d: dict[str, Any]) -> Iterator[None]:
    """La décision maison qui porte les actions déposées dans ce bloc : `{class: direct, domain, service, entity_ids}`
    ou `{class: strong, approval_id}` ou `{class: card, approval_id}` (notification confirmée)."""
    tok = _DECISION.set(dict(d))
    try:
        yield
    finally:
        _DECISION.reset(tok)


def current_decision() -> dict[str, Any] | None:
    return _DECISION.get()


def home_nodes(phone: Any, account: str) -> list[Any]:
    """Les téléphones du compte qui déclarent `lan_home` (sur le Wi-Fi « maison »), dans le locataire courant."""
    from nokker.cloud.placement import caps_of

    return [c for c in phone.candidates(account) if "lan_home" in caps_of(c)]


def _device_class(attrs: Any, entity_id: str) -> str | None:
    try:
        a = attrs(entity_id) if callable(attrs) else None
    except Exception:  # noqa: BLE001 — état illisible : classe inconnue, donc refus
        return None
    v = a.get("device_class") if isinstance(a, dict) else None
    return str(v) if isinstance(v, str) and v else None


def _fail(op: str, code: str) -> BridgeError:
    from nokker.cloud.phone_tools import reason_text

    return BridgeError(f"passerelle du foyer : {op} en échec ({code}) — {reason_text(code)}")


@dataclass
class PhoneBridge:
    """`HomeBridge` d'un membre par la file d'un nœud maison."""

    phone: Any  # PhoneService
    account: str  # propriétaire du nœud = le compte du tour
    tenant: str
    wait_s: float = WAIT_S

    def place(self) -> Any | None:
        from nokker.cloud import placement as pl
        from nokker.tenancy.context import tenant_scope

        with tenant_scope(self.tenant):
            p = pl.place(self.phone.candidates(self.account), pl.Needs(caps=("lan_home",)), self.phone.clock())
        return p.machine

    def op(self, name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        from nokker.cloud.phone import HA_ACTION_OPS, HA_EXCLUDED_OPS, HA_READ_OPS, PhoneError
        from nokker.tenancy.context import tenant_scope

        if name in HA_EXCLUDED_OPS or name not in HA_READ_OPS | HA_ACTION_OPS:
            raise _fail(name, "unknown_op")
        node = self.place()
        if node is None:
            raise _fail(name, "no_home_node")
        origin: dict[str, Any] = {"kind": "turn"}
        args = dict(payload or {})
        if name in HA_ACTION_OPS:
            d = current_decision()
            if d is None:
                raise _fail(name, "decision_required")
            if d.get("class") == "direct":  # l'agent maison n'exécute que ce que la politique classe `direct`
                if name != "ha.call_service":
                    raise _fail(name, "decision_required")
                attrs = d.get("attributes")  # état HA lu par la source pour cette décision (CloudHome)
                ids = list(args.get("entity_ids") or [])
                d = {"class": "direct", "domain": args.get("domain"), "service": args.get("service"), "entity_ids": ids}
                if args.get("domain") == "cover":  # la garde du noyau refuse garage, porte, portail et l'inconnu
                    d["device_classes"] = {e: _device_class(attrs, e) for e in ids}
            origin["decision"] = d
        wait = self.wait_s + (min(float(args.get("timeout") or 0), 20.0) if name == "ha.events" else 0.0) + WAIT_PAD_S
        try:
            with tenant_scope(self.tenant):
                t = self.phone.enqueue(self.account, node.node, name, args, deadline_s=max(60.0, wait + 10),
                                       origin=origin)
                done = self.phone.wait(t.task_id, wait)
                if done is None:
                    self.phone.cancel(t.task_id)
        except PhoneError as e:
            raise _fail(name, e.code) from None
        if done is None or done.status in ("expired", "cancelled"):
            raise _fail(name, "home_timeout")
        if done.status == "failed":
            raise _fail(name, str((done.error or {}).get("code") or "ha_error"))
        try:
            out = json.loads(done.result or "{}")
        except ValueError:
            raise _fail(name, "bad_result") from None
        if not isinstance(out, dict):
            raise _fail(name, "bad_result")
        return out


# ─────────────────────────────── journal en base (081) ───────────────────────────────


@dataclass
class PgHomeJournal:
    """`log` de HomeJournal au cloud : une ligne par événement dans `home_journal`, locataire du tour (RLS)."""

    dsn: str
    account: str
    clock: Callable[[], float] = time.time

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        from nokker.store.conn import connect

        with connect(self.dsn) as c:
            c.execute("INSERT INTO home_journal (account_id, at, record) VALUES (%s, %s, %s)",
                      (self.account, self.clock(), Jsonb(record)))
        return record


def journal_rows(dsn: str, account: str, limit: int = 5000) -> list[dict[str, Any]]:
    """Export du compte : son journal maison (le plus récent en dernier)."""
    from psycopg.rows import dict_row

    from nokker.store.conn import connect

    with connect(dsn, row_factory=dict_row) as c:
        rows = c.execute("SELECT at, record FROM home_journal WHERE account_id = %s ORDER BY at DESC LIMIT %s",
                         (account, limit)).fetchall()
    return [{"at": r["at"], "record": r["record"]} for r in reversed(rows)]


# ─────────────────────────────── la maison du cloud ───────────────────────────────

# Politique par défaut au cloud : rôles et classes de Lokkal (nokker/home/policy.py) ; serrures, alarmes, garage, porte
# et portail, vannes, sirènes, caméras, scripts, automatisations, scènes et chauffage hors bornes en carte forte. Les
# listes blanches sont celles choisies dans l'app (le téléphone ne rend et n'accepte que celles-là) : `*` ici.
CLOUD_POLICY: dict[str, Any] = {"entities": ["*"], "default_role": "adult"}
CLOUD_HIDDEN = frozenset({"maison_config"})  # ha.write_config / delete_config : jamais par le téléphone


@dataclass
class CloudHome:
    """`ToolSource` des testeurs, cloud : les MÊMES outils maison que Lokkal (maison, maison_action_forte), servis par
    une source construite à la demande pour le compte, sur `PhoneBridge`. Offerte seulement à un compte dont un téléphone
    déclare `lan_home` (son propre nœud maison)."""

    phone: Any  # PhoneService
    account_of: Callable[[str], str | None]  # locataire → compte
    journal: Callable[[str], Any] | None = None  # compte → log (PgHomeJournal) ; None : liste en mémoire
    kernel: Any = None
    _sources: dict[str, Any] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def _reach(self, tenant_id: str) -> str | None:
        from nokker.tenancy.context import tenant_scope

        acc = self.account_of(tenant_id)
        if acc is None:
            return None
        with tenant_scope(tenant_id):
            return acc if home_nodes(self.phone, acc) else None

    def available_for(self, tenant_id: str) -> bool:
        try:
            return self._reach(tenant_id) is not None
        except Exception:  # noqa: BLE001 — sans magasin lisible, pas de maison
            return False

    def source(self, tenant_id: str) -> Any | None:
        """La source maison du compte du locataire (construite une fois, puis gardée) ; None sans nœud maison."""
        acc = self._reach(tenant_id)
        if acc is None:
            return None
        with self._lock:
            src = self._sources.get(acc)
            if src is None:
                from nokker.home.client import BridgeHome
                from nokker.home.policy import HomePolicy
                from nokker.home.source import HomeAssistantSource, HomeJournal

                bridge = PhoneBridge(phone=self.phone, account=acc, tenant=tenant_id)
                log = self.journal(acc) if self.journal is not None else []
                src = HomeAssistantSource(client=BridgeHome(bridge), policy=HomePolicy.from_config(CLOUD_POLICY),
                                          journal=HomeJournal(log=log), tiers=("small",), escalate_tier=None,
                                          cloud=True)
                if self.kernel is not None:
                    src.bind(self.kernel, None)  # pas de bulles d'abonnement au cloud (v1)
                self._sources[acc] = src
        return src

    def specs(self) -> list[Any]:
        from nokker.home.policy import HomePolicy
        from nokker.home.source import HomeAssistantSource, HomeJournal

        probe = HomeAssistantSource(client=None, policy=HomePolicy.from_config(CLOUD_POLICY),
                                    journal=HomeJournal(log=[]), cloud=True)
        return probe.specs()

    def call(self, name: str, arguments: dict[str, Any], ctx: Any) -> Any:
        from nokker.core.turn import ToolResult
        from nokker.tenancy.context import current_tenant

        src = self.source(current_tenant()) if name not in CLOUD_HIDDEN else None
        if src is None:
            return ToolResult(call_id="", name=name, content=mt.pick(mt.UNKNOWN_TOOL_BARE).format(name=name), is_error=True)
        if name == "maison":
            def attributes(eid: str) -> dict[str, Any] | None:
                snap = src._snap  # noqa: SLF001 — l'état HA que l'agent maison vient de lire pour classer l'action
                return snap.attributes(eid) if snap is not None else None

            with decision({"class": "direct", "attributes": attributes}):
                return src.call(name, arguments, ctx)
        if name == "maison_action_forte" and ctx.approval_id:
            with decision({"class": "strong", "approval_id": ctx.approval_id}):
                return src.call(name, arguments, ctx)
        return src.call(name, arguments, ctx)

    def verdict(self, call: Any, turn: Any) -> Any:
        from nokker.tenancy.context import current_tenant

        src = self.source(current_tenant())
        return src.verdict(call, turn) if src is not None else None


@dataclass
class CloudHomeGates:
    """Portes du noyau précédées de la porte maison du compte du tour (HomeGates, la même règle que Lokkal)."""

    inner: Any
    home: CloudHome

    def action(self, call: Any, spec: Any, turn: Any) -> list[Any]:
        from nokker.home.source import HOME_TOOLS, HomeGates
        from nokker.tenancy.context import current_tenant

        if call.name not in HOME_TOOLS:
            return self.inner.action(call, spec, turn)
        src = self.home.source(current_tenant())
        if src is None:
            return self.inner.action(call, spec, turn)
        return HomeGates(inner=self.inner, source=src).action(call, spec, turn)

    def switch(self, reply: str, turn: Any, tier: Any) -> list[Any]:
        return self.inner.switch(reply, turn, tier)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


__all__ = ["CLOUD_HIDDEN", "CLOUD_POLICY", "CloudHome", "CloudHomeGates", "PgHomeJournal", "PhoneBridge",
           "current_decision", "decision", "home_nodes", "journal_rows"]
