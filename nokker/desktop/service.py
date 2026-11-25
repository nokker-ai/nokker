"""Bureau, côté noyau Lokkal (docs/DESKTOP.md §1-§3) : inscription des nœuds sous carte forte, preuve de clé, battement,
file d'opérations relevée par le nœud (le noyau ne se connecte jamais au bureau), et les 4 outils du modèle.

- Drapeau `local.desktop.enabled` (éteint par défaut) ; éteint : ni outil, ni route.
- Un nœud par mode (`virtual`, `session`) ; une nouvelle inscription d'un mode remplace l'ancienne.
- Jeton du nœud gardé haché (sha256) dans `<état>/desktop/nodes.json` ; le jeton en clair ne vit qu'en mémoire jusqu'au
  retrait (une fois, sous preuve de la clé inscrite, 10 min au plus).
- File en mémoire : un tour attend `wait_s` (20 s) ; au-delà, la tâche est annulée et le modèle le sait.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from nokker.core import model_texts as mt
from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.desktop import policy as P
from nokker.desktop import texts as T
from nokker.desktop.proof import NONCE_TTL_S, PROOF_SKEW_S, parse_proof, proof_message, verify_signature
from nokker.qa.untrusted import fence_checked, inline, suspect_header

logger = logging.getLogger("nokker.desktop")

ENROLL_TOOL = "desktop_register"     # carte forte d'inscription : un geste du noyau
ENROLL_RULE = "desktop:enroll"
# Durée d'une inscription en attente = celle de la carte FORTE qui la porte (`expires_at` de la carte, 24 h par défaut
# dans approvals) ; ce repli ne sert que si la carte n'en donne pas. Avant le correctif « rafale de cartes » (09.11),
# 600 s : un nœud qui attendait plus de 10 min recevait 410, sortait, et docker le relançait sur une NOUVELLE carte.
CARD_TTL_S = 24 * 3600.0
STALE_NODE_S = 15 * 60        # carte approuvée alors que le nœud ne relève plus depuis 15 min : refusée
# Texte vu par la PERSONNE (résultat de la carte), pas par le modèle.
ENROLL_STALE = ("Ce bureau ne répond plus depuis plus de 15 minutes : rien n'a été relié. "
                "Relancez Nokker sur l'ordinateur : il refera une demande, à approuver à nouveau.")
TOKEN_PICKUP_TTL_S = 600
FRESH_S = 180
LEASE_S = 60
RESULT_MAX = 64 * 1024
WAIT_DEFAULT_S = 20.0
POLL_MAX_S = 25.0
RATE_OPS_PER_H = 600
ENROLL_PER_H = 5
SNAPSHOT_KIB_DEFAULT, SNAPSHOT_KIB_MAX = 12, 32
_TOKEN = re.compile(r"^dn_[A-Za-z0-9_-]{32,64}$")
_TASK = re.compile(r"^dt_[0-9a-f]{16}$")
_NAME = re.compile(r"^[^\x00-\x1f\x7f]{1,40}$")


class DesktopError(Exception):
    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(code)
        self.status, self.code, self.message = status, code, message or code


def err(code: str, message: str = "") -> dict[str, Any]:
    return {"error": {"code": code, "message": message or code}}


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


@dataclass
class DesktopNode:
    node_id: str
    name: str
    mode: str
    pubkey: str
    key_fp: str
    token_sha: str
    created_at: float
    beat_at: float | None = None
    health: dict[str, Any] = field(default_factory=dict)

    def online(self, now: float) -> bool:
        return self.beat_at is not None and now - self.beat_at <= FRESH_S

    def ready(self, now: float) -> bool:
        return self.online(now) and self.health.get("desktop") == "on"


@dataclass
class DesktopTask:
    task_id: str
    node_id: str
    op: str
    args: dict[str, Any]
    policy: dict[str, Any]
    created_at: float
    deadline: float
    status: str = "queued"     # queued | leased | done | failed | cancelled | expired
    lease_until: float = 0.0
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    done: threading.Event = field(default_factory=threading.Event, repr=False)

    def wire(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "op": self.op, "args": self.args, "policy": self.policy,
                "deadline": int(self.deadline)}


@dataclass
class DesktopService:
    state_dir: Path
    # Mode de CETTE machine (`local.desktop.mode`) : `virtual` par défaut ; `session` (le bureau où la personne
    # travaille) seulement si elle l'a choisi explicitement. Jamais de repli de l'un sur l'autre (décision du 06.11).
    mode: str = "virtual"
    on_change: Callable[[], Any] | None = None  # nœud inscrit ou retiré : la surface d'outils change (registre)
    steps: Any = None  # nokker.desktop.steps.DesktopSteps en mode session (B2) ; None en virtual
    apps: dict[str, tuple[str, ...]] = field(default_factory=lambda: {"virtual": (), "session": ()})
    physical: dict[str, str] = field(default_factory=lambda: dict(P.PHYSICAL_DEFAULT))
    snapshot_kib: int = SNAPSHOT_KIB_DEFAULT
    wait_s: float = WAIT_DEFAULT_S
    approvals: Any = None
    clock: Callable[[], float] = time.time
    secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))  # pseudonyme des tâches (fil → job)
    _nodes: dict[str, DesktopNode] = field(default_factory=dict, init=False)
    _regs: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)
    _tasks: dict[str, DesktopTask] = field(default_factory=dict, init=False)
    _nonces: dict[tuple[str, str], float] = field(default_factory=dict, init=False)
    _hits: dict[str, list[float]] = field(default_factory=dict, init=False)
    _log: list[dict[str, Any]] = field(default_factory=list, init=False)
    _cond: threading.Condition = field(default_factory=threading.Condition, init=False)

    def __post_init__(self) -> None:
        self.state_dir = Path(self.state_dir)
        p = self._path()
        if p.is_file():
            try:
                for row in json.loads(p.read_text(encoding="utf-8")).get("nodes", []):
                    n = DesktopNode(**{k: row[k] for k in ("node_id", "name", "mode", "pubkey", "key_fp",
                                                           "token_sha", "created_at")})
                    self._nodes[n.node_id] = n
            except (OSError, ValueError, KeyError, TypeError):
                logger.warning("bureau : %s illisible, aucun nœud chargé", p)
        q = self._regs_path()
        if q.is_file():  # inscriptions EN ATTENTE d'avant un redémarrage du noyau : la carte reste la même
            try:
                now = self.clock()
                for aid, r in json.loads(q.read_text(encoding="utf-8")).get("pending", {}).items():
                    row = {k: r[k] for k in ("name", "mode", "pubkey", "key_fp", "expires_at")}
                    if isinstance(aid, str) and now <= float(row["expires_at"]):
                        self._regs[aid] = {**row, "state": "pending", "seen_at": float(r.get("seen_at") or now)}
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                logger.warning("bureau : %s illisible, aucune inscription en attente reprise", q)

    # ── persistance ──
    def _path(self) -> Path:
        return self.state_dir / "desktop" / "nodes.json"

    def _regs_path(self) -> Path:
        return self.state_dir / "desktop" / "enroll-pending.json"

    @staticmethod
    def _write_0600(p: Path, data: dict[str, Any]) -> None:
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = p.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, p)

    def _save(self) -> None:
        rows = [{k: v for k, v in asdict(n).items() if k not in ("beat_at", "health")} for n in self._nodes.values()]
        self._write_0600(self._path(), {"v": 1, "nodes": rows})

    def _save_regs(self) -> None:
        """Inscriptions en attente seulement (clé PUBLIQUE, jamais de jeton), sous le verrou de l'appelant."""
        pending = {k: {f: r[f] for f in ("name", "mode", "pubkey", "key_fp", "expires_at", "seen_at")}
                   for k, r in self._regs.items() if r["state"] == "pending"}
        try:
            self._write_0600(self._regs_path(), {"v": 1, "pending": pending})
        except OSError:
            logger.warning("bureau : inscriptions en attente non enregistrées", exc_info=True)

    def _rate(self, key: str, limit: int, window_s: float) -> None:
        now = self.clock()
        hits = [t for t in self._hits.get(key, []) if now - t < window_s]
        if len(hits) >= limit:
            raise DesktopError(429, "rate", "trop de requêtes")
        hits.append(now)
        self._hits[key] = hits

    # ── inscription ──
    def enroll(self, body: Any) -> dict[str, Any]:
        """`POST /v1/desktop/node/enroll {name, mode, public_key_spki}` (sur la machine seulement) → carte FORTE
        « Relier le bureau … à Nokker » ; 202 {approval_id, key_fingerprint}."""
        if not isinstance(body, dict):
            raise DesktopError(400, "bad_body")
        name, mode, spki = str(body.get("name") or ""), str(body.get("mode") or ""), str(body.get("public_key_spki")
                                                                                          or "")
        if not _NAME.match(name) or mode not in P.MODES:
            raise DesktopError(400, "bad_body", "name (1-40) et mode virtual | session")
        if mode != self.mode:
            raise DesktopError(409, "mode_not_allowed", f"cette machine n'accepte qu'un bureau {self.mode} "
                                                        "(local.desktop.mode)")
        try:
            from nokker.cloud.phone import load_pubkey

            load_pubkey(spki)
        except Exception:  # noqa: BLE001
            raise DesktopError(400, "bad_key", "clé P-256 SPKI en base64 attendue") from None
        if self.approvals is None:
            raise DesktopError(503, "approvals_disabled")
        fp = _sha(spki)[:8]
        # UNE seule carte « Relier le bureau » vivante (répétition du 06.11) : un nœud qui redémarre avec la même clé
        # retrouve SA carte ; toute autre carte d'inscription en attente est retirée (mark_failed), y compris celles
        # d'avant un redémarrage du noyau (inscriptions en mémoire). Sinon la personne voit deux cartes et en
        # approuver une périmée laisse un nœud sans jeton retiré.
        same = self._live_card_for(spki)
        if same is not None:
            with self._cond:
                self._regs[same]["seen_at"] = self.clock()
            return {"approval_id": same, "key_fingerprint": fp}
        with self._cond:
            self._rate("enroll", ENROLL_PER_H, 3600)
        self._retire_stale_cards()
        from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict

        turn = Turn(thread_id="desktop-enroll", index=0, input=InputCheck(text=""), channel="kernel", sender="local")
        call = ToolCall(id=f"desktop-{secrets.token_hex(6)}", name=ENROLL_TOOL,
                        arguments={"name": name, "mode": mode, "key_fingerprint": fp})
        reason = f"Relier le bureau « {name} » ({mode}) à Nokker (clé {fp})."
        verdict = Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=ENROLL_RULE, reason=reason,
                          subject=call.id, strength="strong")
        from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

        with tenant_scope(INTERNAL_TENANT):  # cartes locales : locataire interne, comme /v1/local/approvals
            card = self.approvals.request(turn, call, verdict,
                                          "Nokker pourra piloter les applis permises de ce bureau.")
        with self._cond:
            now = self.clock()
            for k in [k for k, r in self._regs.items() if now > r["expires_at"]]:
                del self._regs[k]
            exp = getattr(card, "expires_at", None)
            exp = float(exp) if isinstance(exp, (int, float)) and exp > now else now + CARD_TTL_S
            self._regs[card.id] = {"name": name, "mode": mode, "pubkey": spki, "key_fp": fp, "state": "pending",
                                   "expires_at": exp, "seen_at": now}
            self._save_regs()
        return {"approval_id": card.id, "key_fingerprint": fp}

    def _card_pending(self, approval_id: str) -> bool:
        from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

        try:
            with tenant_scope(INTERNAL_TENANT):
                card = self.approvals.get(approval_id)
        except Exception:  # noqa: BLE001
            return False
        return card is not None and getattr(card, "status", "") == "pending"

    def _live_card_for(self, spki: str) -> str | None:
        now = self.clock()
        with self._cond:
            ids = [k for k, r in self._regs.items()
                   if r["pubkey"] == spki and r["state"] == "pending" and now <= r["expires_at"]]
        return next((k for k in ids if self._card_pending(k)), None)

    def _retire_stale_cards(self) -> None:
        from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

        try:
            with tenant_scope(INTERNAL_TENANT):
                stale = [a.id for a in self.approvals.pending() if getattr(a, "tool_name", "") == ENROLL_TOOL]
        except Exception:  # noqa: BLE001 — sans liste, rien n'est retiré
            stale = []
        for aid in stale:
            try:
                with tenant_scope(INTERNAL_TENANT):
                    self.approvals.mark_failed(aid, "remplacée par une nouvelle demande d'inscription du bureau")
            except Exception:  # noqa: BLE001
                logger.warning("bureau : carte d'inscription %s non retirée", aid, exc_info=True)
            with self._cond:
                reg = self._regs.get(aid)
                if reg is not None and reg["state"] == "pending":
                    reg["state"] = "rejected"
                    self._save_regs()

    def on_decision(self, approval: Any) -> str:
        """Carte décidée (exécuteur) : approuvée, le nœud existe et son jeton attend son retrait (une fois)."""
        with self._cond:
            reg = self._regs.get(approval.id)
            if reg is None or reg["state"] != "pending":
                return "inscription inconnue ou périmée"
            if approval.status != "approved":
                reg["state"] = "rejected"
                self._save_regs()
                return "inscription refusée"
            if self.clock() - float(reg.get("seen_at") or 0) > STALE_NODE_S:
                # Le nœud ne relève plus sa carte : approuver donnerait un jeton que personne n'attend (ou qu'un nœud
                # disparu récupérerait plus tard). Il refera une demande à son retour.
                reg["state"] = "rejected"
                self._save_regs()
                return ENROLL_STALE
            token = "dn_" + secrets.token_urlsafe(32)
            for old in [n for n in self._nodes.values() if n.mode == reg["mode"]]:
                del self._nodes[old.node_id]  # un nœud par mode : le nouveau remplace l'ancien
            node = DesktopNode(node_id="dk_" + secrets.token_hex(8), name=reg["name"], mode=reg["mode"],
                               pubkey=reg["pubkey"], key_fp=reg["key_fp"], token_sha=_sha(token),
                               created_at=self.clock())
            self._nodes[node.node_id] = node
            self._save()
            reg.update(state="ready", token=token, node_id=node.node_id, expires_at=self.clock() + TOKEN_PICKUP_TTL_S)
            self._save_regs()
        self._changed()
        return f"bureau {reg['name']} relié"

    def pickup(self, approval_id: str, method: str, host: str, path_qs: str, raw: bytes,
               proof: str | None) -> tuple[int, dict[str, Any]]:
        """`GET /v1/desktop/node/enroll/{id}` sous preuve de la clé INSCRITE : 202 en attente, 201 {node_id, token}
        UNE fois, puis 410 ; 403 refusée ; 404 inconnue."""
        with self._cond:
            reg = self._regs.get(approval_id)
            if reg is None:
                return 404, err("not_found")
            self._check_proof(("reg", approval_id), reg["pubkey"], method, host, path_qs, raw, proof)
            reg["seen_at"] = self.clock()  # relevé en mémoire seulement : pas une écriture disque toutes les 5 s
            if reg["state"] == "rejected":
                return 403, err("rejected")
            if reg["state"] == "claimed" or self.clock() > reg["expires_at"]:
                return 410, err("gone")
            if reg["state"] != "ready":
                return 202, {"status": "pending"}
            token = reg.pop("token")
            reg["state"] = "claimed"
            return 201, {"node_id": reg["node_id"], "token": token}

    # ── authentification des requêtes du nœud ──
    def _check_proof(self, who: tuple[str, str], pubkey: str, method: str, host: str, path_qs: str, raw: bytes,
                     proof: str | None) -> None:
        parsed = parse_proof(proof)
        if parsed is None:
            raise DesktopError(401, "proof_invalid", "en-tête X-Nokker-Node-Proof v1 attendu")
        ts, nonce, sig = parsed
        now = self.clock()
        if abs(now - int(ts)) > PROOF_SKEW_S:
            raise DesktopError(401, "proof_invalid", "horodatage hors des ±60 s")
        if not verify_signature(pubkey, proof_message(method, host, path_qs, ts, nonce, raw), sig):
            raise DesktopError(401, "proof_invalid", "signature invalide")
        for k in [k for k, t in self._nonces.items() if now - t > NONCE_TTL_S]:
            del self._nonces[k]
        if (who[1], nonce) in self._nonces:
            raise DesktopError(401, "proof_invalid", "nonce déjà vu")
        self._nonces[(who[1], nonce)] = now

    def authenticate(self, authorization: str | None, method: str, host: str, path_qs: str, raw: bytes,
                     proof: str | None) -> DesktopNode:
        token = (authorization or "")[7:].strip() if (authorization or "").startswith("Bearer ") else ""
        if not _TOKEN.match(token):
            raise DesktopError(401, "unauthorized")
        sha = _sha(token)
        with self._cond:
            node = next((n for n in self._nodes.values() if hmac.compare_digest(n.token_sha, sha)), None)
            if node is None:
                raise DesktopError(401, "unauthorized")
            self._check_proof(("node", node.node_id), node.pubkey, method, host, path_qs, raw, proof)
            return node

    # ── battement, file ──
    def beat(self, node: DesktopNode, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict) or body.get("v") != 1:
            raise DesktopError(400, "bad_beat")
        state = body.get("desktop") if body.get("desktop") in ("on", "degraded") else "degraded"
        reason = str(body.get("reason") or "")[:40] or None
        with self._cond:
            self._rate(f"beat:{node.node_id}", 5, 60)
            node.beat_at = self.clock()
            node.health = {"desktop": state, "reason": reason}
        return {"ok": True, "next_beat_s": 60}

    def poll(self, node: DesktopNode, wait_s: float) -> list[dict[str, Any]]:
        deadline = self.clock() + max(0.0, min(POLL_MAX_S, wait_s))
        with self._cond:
            node.beat_at = node.beat_at or self.clock()
            while True:
                now = self.clock()
                self._expire(now)
                ready = [t for t in self._tasks.values() if t.node_id == node.node_id and t.status == "queued"]
                if ready:
                    for t in ready:
                        t.status, t.lease_until = "leased", now + LEASE_S
                    return [t.wire() for t in ready[:4]]
                left = deadline - now
                if left <= 0:
                    return []
                self._cond.wait(timeout=min(left, 1.0))

    def lease(self, node: DesktopNode, task_id: str) -> dict[str, Any]:
        with self._cond:
            t = self._task_of(node, task_id)
            if t.status != "leased":
                return {"status": t.status}
            t.lease_until = self.clock() + LEASE_S
            return {"status": "leased", "lease_until": int(t.lease_until)}

    def result(self, node: DesktopNode, task_id: str, body: Any, raw_len: int) -> dict[str, Any]:
        if raw_len > RESULT_MAX:
            raise DesktopError(413, "too_large")
        if not isinstance(body, dict) or body.get("status") not in ("done", "failed"):
            raise DesktopError(400, "bad_result")
        with self._cond:
            t = self._task_of(node, task_id)
            if t.status in ("done", "failed"):
                return {"stored": True}  # idempotent
            if t.status != "leased":
                return {"stored": False, "status": t.status}
            t.status = body["status"]
            t.result = body.get("result") if isinstance(body.get("result"), dict) else None
            t.error = body.get("error") if isinstance(body.get("error"), dict) else None
            t.done.set()
            self._cond.notify_all()
        return {"stored": True}

    def _task_of(self, node: DesktopNode, task_id: str) -> DesktopTask:
        t = self._tasks.get(task_id) if _TASK.match(task_id or "") else None
        if t is None or t.node_id != node.node_id:
            raise DesktopError(404, "not_found")
        return t

    def _expire(self, now: float) -> None:
        for t in list(self._tasks.values()):
            if t.status in ("queued", "leased") and now > t.deadline:
                t.status = "expired"
                t.done.set()
            if t.status not in ("queued", "leased") and now - t.created_at > 600:
                del self._tasks[t.task_id]

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001 — un rafraîchissement raté ne casse pas l'inscription
                logger.warning("bureau : rafraîchissement des outils en échec", exc_info=True)

    # ── pour les outils ──
    def enrolled(self) -> bool:
        """Un nœud du mode de la machine est inscrit : sinon, aucun outil du bureau n'est offert."""
        with self._cond:
            return any(n.mode == self.mode for n in self._nodes.values())

    def node_for(self) -> DesktopNode | None:
        """Le nœud du mode de la machine s'il est prêt ; jamais celui d'un autre mode."""
        now = self.clock()
        with self._cond:
            n = next((x for x in self._nodes.values() if x.mode == self.mode), None)
        return n if n is not None and n.ready(now) else None

    def policy_for(self, mode: str) -> dict[str, Any]:
        return {"apps": list(self.apps.get(mode, ())), "physical": P.physical_mode(self.physical.get(mode), mode),
                "snapshot_max": min(self.snapshot_kib, SNAPSHOT_KIB_MAX) * 1024}

    def job_of(self, thread_id: str) -> str:
        return hmac.new(self.secret, thread_id.encode(), hashlib.sha256).hexdigest()[:32]

    def submit(self, node: DesktopNode, op: str, args: dict[str, Any], wait_s: float | None = None) -> DesktopTask:
        """Dépose et attend. Rend la tâche terminée (done | failed) ; au-delà du délai, elle est annulée
        (`cancelled`)."""
        now = self.clock()
        wait = self.wait_s if wait_s is None else wait_s
        with self._cond:
            self._rate(f"ops:{node.node_id}", RATE_OPS_PER_H, 3600)
            t = DesktopTask(task_id="dt_" + secrets.token_hex(8), node_id=node.node_id, op=op, args=args,
                            policy=self.policy_for(node.mode), created_at=now, deadline=now + wait + 5)
            self._tasks[t.task_id] = t
            self._log.append({"task_id": t.task_id, "op": op, "node": node.mode, "at": int(now)})
            del self._log[:-200]
            self._cond.notify_all()
        if not t.done.wait(timeout=wait):
            with self._cond:
                if t.status in ("queued", "leased"):
                    t.status = "cancelled"
        with self._cond:
            for row in self._log:  # journal SANS contenu : opération, bureau, état, code
                if row["task_id"] == t.task_id:
                    row["status"] = t.status
                    row["code"] = (t.error or {}).get("code")
        return t

    # ── vue du propriétaire ──
    def view(self) -> dict[str, Any]:
        now = self.clock()
        with self._cond:
            nodes = [{"node_id": n.node_id, "name": n.name, "mode": n.mode, "key_fingerprint": n.key_fp,
                      "online": n.online(now), "desktop": n.health.get("desktop", "offline") if n.online(now)
                      else "offline", "reason": n.health.get("reason"), "apps": list(self.apps.get(n.mode, ())),
                      "physical": P.physical_mode(self.physical.get(n.mode), n.mode)} for n in self._nodes.values()]
            log = [dict(r) for r in self._log]
        return {"nodes": nodes, "log": log}

    def revoke(self, node_id: str) -> bool:
        with self._cond:
            if self._nodes.pop(node_id, None) is None:
                return False
            self._save()
            for t in self._tasks.values():
                if t.node_id == node_id and t.status in ("queued", "leased"):
                    t.status = "cancelled"
                    t.done.set()
        self._changed()
        return True


class _Blank(dict):
    """Valeurs de mise en forme d'un refus : une clé absente donne « ? » plutôt qu'une exception."""

    def __missing__(self, key: str) -> str:
        return "?"


def build_desktop(conf: dict[str, Any], state_dir: str | Path) -> DesktopService:
    """`local.desktop` : `{enabled, mode: virtual|session, apps: {virtual: [...], session: [...]},
    physical: {virtual, session}, snapshot_max_kib, wait_s}`. Une appli de la liste refusée en permanence est ignorée
    (et journalisée). Un mode inconnu vaut `virtual` : `session` ne s'obtient qu'écrit tel quel."""
    apps_conf = conf.get("apps") if isinstance(conf.get("apps"), dict) else {}
    apps: dict[str, tuple[str, ...]] = {}
    for mode in P.MODES:
        kept = []
        for a in apps_conf.get(mode) or ():
            if P.app_verdict(a, (a,)) == "app_denied":
                logger.warning("bureau : appli %s refusée en permanence, ignorée", a)
                continue
            kept.append(P.norm_app(a))
        apps[mode] = tuple(dict.fromkeys(kept))
    phys_conf = conf.get("physical") if isinstance(conf.get("physical"), dict) else {}
    physical = {m: P.physical_mode(phys_conf.get(m), m) for m in P.MODES}
    kib = conf.get("snapshot_max_kib")
    kib = int(kib) if isinstance(kib, int) and not isinstance(kib, bool) and 1 <= kib <= SNAPSHOT_KIB_MAX \
        else SNAPSHOT_KIB_DEFAULT
    wait = conf.get("wait_s")
    wait = float(wait) if isinstance(wait, (int, float)) and not isinstance(wait, bool) and 1 <= wait <= 60 \
        else WAIT_DEFAULT_S
    mode = conf.get("mode") if conf.get("mode") in P.MODES else "virtual"
    svc = DesktopService(state_dir=Path(state_dir), mode=mode, apps=apps, physical=physical, snapshot_kib=kib,
                         wait_s=wait)
    if mode == "session":  # B2 : chaque action attend la personne à l'écran (docs/DESKTOP.md §12)
        from nokker.desktop.steps import DesktopSteps

        svc.steps = DesktopSteps(svc)
    return svc


# ─────────────────────────────── outils du modèle ───────────────────────────────
def _look_params() -> dict[str, Any]:
    p = T.LOOK_PARAMS
    props: dict[str, Any] = {"what": {"type": "string", "enum": ["status", "windows", "snapshot", "find", "get"],
                                      "description": p["what"]}}
    for k in ("app", "window", "root", "role", "name", "text", "snap_id", "ref"):
        props[k] = {"type": "string", "description": p[k]}
    return {"type": "object", "properties": props, "required": ["what"]}


def _act_params() -> dict[str, Any]:
    p = T.ACT_PARAMS
    props: dict[str, Any] = {
        "action": {"type": "string", "enum": ["click", "type", "set_value", "select", "check", "press"],
                   "description": p["action"]},
        "checked": {"type": "boolean", "description": p["checked"]},
    }
    for k in ("window", "app", "snap_id", "ref", "expect_role", "expect_name", "text", "keys"):
        props[k] = {"type": "string", "description": p[k]}
    return {"type": "object", "properties": props, "required": ["action", "window", "app"]}


def _open_params() -> dict[str, Any]:
    p = T.OPEN_PARAMS
    return {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["launch", "focus"], "description": p["action"]},
        "app": {"type": "string", "description": p["app"]},
        "window": {"type": "string", "description": p["window"]}}, "required": ["action"]}


def _wait_params() -> dict[str, Any]:
    p = T.WAIT_PARAMS
    return {"type": "object", "properties": {
        "event": {"type": "string", "enum": ["window-opened", "window-closed", "focus-changed"],
                  "description": p["event"]},
        "app": {"type": "string", "description": p["app"]},
        "timeout_s": {"type": "integer", "minimum": 1, "maximum": 30, "description": p["timeout_s"]}},
        "required": ["event"]}


def marks_floor(ctx: ToolContext, limit: int = 200) -> dict[str, int]:
    """Plus grand numéro de marqueur ⟦CODE-n⟧ déjà vu dans le fil, par code : le nœud numérote après (deux valeurs
    différentes n'ont jamais le même marqueur dans un fil)."""
    from nokker.redact.markers import MARKER_RE

    floor: dict[str, int] = {}
    store = getattr(getattr(ctx, "kernel", None), "store", None)
    try:
        history = store.history(ctx.thread_id, limit=limit) if store is not None else []
    except Exception:  # noqa: BLE001
        history = []
    texts: list[str] = []
    for t in history:
        texts.append(str(getattr(getattr(t, "input", None), "text", "") or ""))
        for m in getattr(t, "messages", None) or ():
            texts.append(str(getattr(m, "content", "") or ""))
    for text in texts:
        if "⟦" not in text:
            continue
        for m in MARKER_RE.finditer(text):
            code, n = m.group(0)[1:-1].rsplit("-", 1)
            floor[code] = max(floor.get(code, 0), int(n))
    return floor


_SNAP_LINE = re.compile(r"^\s*(e[0-9]{1,6}) (?P<role>[^«]+?)(?: « (?P<name>.*?) »)?"
                        r"(?: \[[a-z ]*\])?(?: \(\+[0-9]+\))?(?: = .*)?$")
_RANK = {Effect.WRITE: 0, Effect.SEND_EXTERNAL: 1, Effect.DESTRUCTIVE: 2}
_ROW_CAP = 64  # snapshots gardés côté noyau, par source


def significant(name: object) -> bool:
    """Un nom « donné » contient au moins une lettre ou un chiffre : une ponctuation seule (`"`) n'est pas un nom."""
    return bool(re.search(r"[^\W_]", str(name or "")))


def names_of(text: str) -> dict[str, tuple[str, str]]:
    """ref → (rôle, nom) lus dans les lignes rendues d'un snapshot ou d'un find (`eN rôle « nom » …`)."""
    out: dict[str, tuple[str, str]] = {}
    for line in str(text or "").splitlines():
        m = _SNAP_LINE.match(line)
        if m:
            out[m.group(1)] = (m.group("role").strip(), m.group("name") or "")
    return out


@dataclass
class DesktopToolSource:
    svc: DesktopService
    scrub: Any = None
    source_name: str = "builtin:desktop"
    # (job, snap_id) → ref → (rôle, nom), tels que le modèle les a lus : sert à remplir `expect_name` avant la carte
    _seen: dict = field(default_factory=dict, compare=False, repr=False)

    def _remember(self, job: str, result: dict[str, Any]) -> None:
        snap_id = str(result.get("snap_id") or "")
        if not snap_id:
            return
        rows = self._seen.setdefault((job, snap_id), {})
        rows.update(names_of(str(result.get("text") or "")))
        while len(self._seen) > _ROW_CAP:
            self._seen.pop(next(iter(self._seen)))

    def _fill_expect_name(self, job: str, args: dict[str, Any]) -> None:
        """Le modèle ne donne pas de nom, ou un nom sans lettre ni chiffre (`"`) : le noyau met le VRAI nom de la ref,
        lu dans le snapshot que le modèle a eu sous les yeux, AVANT la carte (la personne le voit). Un nom significatif
        reste comparé tel quel par le nœud. Ref ou nom inconnu : rien n'est rempli. Jamais remplir si le vrai nom
        classe l'appel PLUS haut que ce que la porte a vu (un « Supprimer » sans nom donné ne passe pas pour une écriture)."""
        if args.get("action") not in ("click", "type", "set_value", "select", "check"):
            return
        given = args.get("expect_name")
        if given is not None and not isinstance(given, str):
            return
        if given and significant(given):
            return
        if given:
            args["expect_name"] = ""  # ponctuation seule = pas de nom donné : jamais comparée telle quelle
        row = self._seen.get((job, str(args.get("snap_id") or "")), {}).get(str(args.get("ref") or ""))
        if row is None:
            return
        role, name = row
        if not name or not significant(name):
            return
        from nokker.redact.markers import markers_in

        if markers_in({"n": name}):
            return
        want_role = args.get("expect_role")
        if isinstance(want_role, str) and want_role.strip() and want_role.strip() != role:
            return
        try:
            if _RANK[P.act_class({**args, "expect_name": name})] > _RANK[P.act_class(args)]:
                return
        except ValueError:
            return
        args["expect_name"] = name

    def normalize_act(self, thread_id: str, args: dict[str, Any]) -> dict[str, Any]:
        """Crochet de normalisation de `desktop_act` (`ToolSpec.normalize_args`) : avant la carte générique, le nom que
        la personne voit est le VRAI nom lu dans le snapshot (même logique que l'exécution). Rien d'autre n'est touché."""
        self._fill_expect_name(self.svc.job_of(thread_id), args)
        return args

    def specs(self) -> list[ToolSpec]:
        """Rien tant qu'aucun nœud du mode de la machine n'est inscrit (pas d'outil qui ne mène nulle part)."""
        if not self.svc.enrolled():
            return []
        return [
            ToolSpec(name=T.LOOK_TOOL, description=T.LOOK_DESCRIPTION, parameters=_look_params(), effect=Effect.READ,
                     source=self.source_name, fresh=True, locality="local"),
            ToolSpec(name=T.ACT_TOOL, description=T.ACT_DESCRIPTION + (T.ACT_SESSION_SUFFIX if self.svc.steps else ""),
                     parameters=_act_params(),
                     effect=Effect.DESTRUCTIVE, effect_of=P.act_class, source=self.source_name, locality="local",
                     normalize_args=self.normalize_act),
            ToolSpec(name=T.OPEN_TOOL, description=T.OPEN_DESCRIPTION, parameters=_open_params(), effect=Effect.WRITE,
                     source=self.source_name, locality="local"),
            ToolSpec(name=T.WAIT_TOOL, description=T.WAIT_DESCRIPTION, parameters=_wait_params(), effect=Effect.READ,
                     source=self.source_name, fresh=True, locality="local"),
        ]

    # ── appel ──
    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        args = dict(arguments or {})
        if name not in T.TOOLS:
            return ToolResult(call_id="", name=name, content=mt.pick(mt.UNKNOWN_TOOL_BARE).format(name=name), is_error=True)
        from nokker.redact.markers import markers_in

        found = markers_in(args)
        if found:
            return self._refused(name, "masked_value", markers=", ".join(found))
        if name == T.LOOK_TOOL and args.get("what") == "status":
            return self._ok(name, self._status())
        node = self.svc.node_for()
        if node is None:
            return self._refused(name, "no_desktop")
        job = self.svc.job_of(ctx.thread_id)
        if name == T.ACT_TOOL:
            self._fill_expect_name(job, args)
        op, op_args, wait = self._op(name, args)
        if op is None:
            return self._refused(name, "bad_args", detail=op_args.get("detail", ""))
        op_args["job"] = job
        op_args["marks_floor"] = marks_floor(ctx)
        if self.svc.steps is not None and name in (T.ACT_TOOL, T.OPEN_TOOL):
            return self._step(name, node, op, op_args, args, ctx)
        task = self.svc.submit(node, op, op_args, wait_s=wait)
        if task.status == "done" and task.result is not None:
            if op in ("desktop.snapshot", "desktop.find"):
                self._remember(job, task.result)
            return self._ok(name, self._format(op, task.result))
        if task.status == "failed":
            e = task.error or {}
            detail = e.get("detail") if isinstance(e.get("detail"), dict) else {}
            return self._refused(name, str(e.get("code") or "failed"), **detail)
        return self._refused(name, "timeout")

    def _op(self, name: str, a: dict[str, Any]) -> tuple[str | None, dict[str, Any], float | None]:
        """(opération, arguments, délai) ; `None` + {detail} si les arguments sont invalides."""
        def s(k: str) -> str:
            v = a.get(k)
            return v if isinstance(v, str) else ""

        if name == T.LOOK_TOOL:
            what = s("what")
            if what == "windows":
                return "desktop.list_windows", {"app": s("app")} if s("app") else {}, None
            if what == "snapshot" and s("window"):
                out = {"window": s("window")}
                if s("root"):
                    out.update(root=s("root"), snap_id=s("snap_id"))
                return "desktop.snapshot", out, None
            if what == "find" and s("window") and (s("role") or s("name") or s("text")):
                return "desktop.find", {k: s(k) for k in ("window", "role", "name", "text") if s(k)}, None
            if what == "get" and s("snap_id") and s("ref"):
                return "desktop.get", {"snap_id": s("snap_id"), "ref": s("ref"), "property": "value"}, None
            return None, {"detail": "what et ses arguments"}, None
        if name == T.OPEN_TOOL:
            if s("action") == "launch" and s("app"):
                return "desktop.launch", {"app": s("app")}, max(self.svc.wait_s, 30.0)
            if s("action") == "focus" and s("window"):
                return "desktop.focus", {"window": s("window")}, None
            return None, {"detail": "action launch (app) ou focus (window)"}, None
        if name == T.WAIT_TOOL:
            ev = s("event")
            to = a.get("timeout_s") if isinstance(a.get("timeout_s"), int) else 10
            to = max(1, min(30, to))
            if ev not in ("window-opened", "window-closed", "focus-changed"):
                return None, {"detail": "event"}, None
            out = {"event": ev, "timeout_s": to, **({"app": s("app")} if s("app") else {})}
            return "desktop.wait", out, to + self.svc.wait_s
        action = s("action")
        if action in ("launch", "focus", "open"):  # le modèle prend desktop_act pour lancer une appli : on lui dit le bon outil
            return None, {"detail": T.BAD_OPEN_VIA_ACT.format(action=action)}, None
        if action not in ("click", "type", "set_value", "select", "check", "press"):
            return None, {"detail": T.BAD_ACTION}, None
        if action == "press":
            missing = [k for k in ("window", "app", "keys") if not s(k)]
            if missing:
                return None, {"detail": T.BAD_PRESS_MISSING.format(fields=", ".join(missing))}, None
            if P.parse_keys(a.get("keys")) is None:
                return None, {"detail": "keys"}, None
            return "desktop.press", {"window": s("window"), "app": s("app"), "keys": s("keys")}, None
        missing = [k for k in ("window", "app", "snap_id", "ref") if not s(k)]
        if action in ("type", "set_value", "select") and not isinstance(a.get("text"), str):
            missing.append("text")
        if action == "check" and not isinstance(a.get("checked"), bool):
            missing.append("checked")
        if missing:  # le refus NOMME les champs manquants (aucun effet : rien n'est envoyé au nœud)
            return None, {"detail": T.BAD_MISSING.format(fields=", ".join(missing))}, None
        base = {"window": s("window"), "app": s("app")}
        target = {**base, "snap_id": s("snap_id"), "ref": s("ref"), "expect_role": s("expect_role"),
                  "expect_name": s("expect_name")}
        if action == "click":
            return "desktop.click", target, None
        if action in ("type", "set_value", "select"):
            return f"desktop.{action}", {**target, "text": a["text"]}, None
        return "desktop.check", {**target, "checked": a["checked"]}, None  # action == "check" (les autres sont refusées plus haut)

    # ── pas validés à l'écran (mode session, B2) ──
    def _step(self, name: str, node: Any, op: str, op_args: dict[str, Any], args: dict[str, Any],
              ctx: ToolContext) -> ToolResult:
        from nokker.desktop.steps import follow, outcome_text

        if args.get("expect_name") or args.get("expect_role"):
            target = f"{inline(args.get('expect_role'), 40)} « {inline(args.get('expect_name'), 120)} »"
        elif op == "desktop.launch":
            target = f"l'appli {inline(args.get('app'), 60)}"
        else:
            target = f"la fenêtre de {inline(args.get('app'), 60)}" if args.get("app") else "la fenêtre"
        preview = None
        if isinstance(args.get("text"), str):
            preview = inline(args["text"], 80)
        elif args.get("keys"):
            preview = inline(args.get("keys"), 40)
        if preview is not None and self.scrub is not None:
            preview = self.scrub(preview)
        step = self.svc.steps.open(thread_id=ctx.thread_id, turn_index=ctx.turn_index, sender=ctx.sender or "",
                                   channel=ctx.channel, node=node, op=op, args=op_args, target=target,
                                   preview=preview)
        if step is None:
            return self._refused(name, "step_busy")
        if step.event.wait(timeout=self.svc.wait_s) and step.status in ("done", "failed", "expired", "cancelled"):
            text = outcome_text(step, step.status, self._format)
            return self._ok(name, text) if step.status == "done" else ToolResult(
                call_id="", name=name, content=text, is_error=True)
        fw = follow()
        if fw is not None:
            from nokker.cloud.phone_tools import _Follow
            from nokker.tenancy.context import INTERNAL_TENANT, tenant_of_thread

            try:
                tenant = tenant_of_thread(ctx.thread_id) or INTERNAL_TENANT
            except Exception:  # noqa: BLE001
                tenant = INTERNAL_TENANT
            fw.watch(_Follow(task_id=step.step_id, thread_id=ctx.thread_id, turn_index=ctx.turn_index,
                             sender=ctx.sender or "", tenant=tenant, channel=ctx.channel, op=op,
                             name=getattr(node, "name", "bureau"), machine_id=getattr(node, "node_id", ""),
                             render=lambda _t: ""))
        return self._ok(name, T.STEP_PENDING.format(action=step.action, target=target))

    # ── mise en forme ──
    def _status(self) -> str:
        view = self.svc.view()
        if not view["nodes"]:
            return T.STATUS_NONE
        lines = []
        for n in view["nodes"]:
            if n["mode"] != self.svc.mode:
                continue
            state = {"on": T.STATE_ON, "degraded": T.STATE_DEGRADED}.get(n["desktop"], T.STATE_OFFLINE)
            reason = ""
            if n["desktop"] == "degraded" and n.get("reason") in T.REASON_WORDS:
                reason = T.STATUS_REASON.format(reason=T.REASON_WORDS[n["reason"]])
            lines.append(T.STATUS_LINE.format(mode=T.MODE_WORDS[n["mode"]], state=state, reason=reason,
                                              apps=", ".join(inline(a, 60) for a in n["apps"]) or T.NO_APPS,
                                              physical=T.PHYSICAL_WORDS[n["physical"]]))
        return "\n".join(lines) or T.STATUS_NONE

    def _format(self, op: str, r: dict[str, Any]) -> str:
        if op == "desktop.list_windows":
            rows = r.get("windows") or []
            if not rows:
                return T.WINDOWS_NONE
            text = "\n".join(f"{inline(w.get('window'), 200)} · {inline(w.get('app'), 60)} · "
                             f"« {inline(w.get('title'), 120)} »" for w in rows)
            return self._fenced("", text)
        if op == "desktop.snapshot":
            head = T.SNAPSHOT_HEAD.format(snap_id=inline(r.get("snap_id"), 40), window=inline(r.get("window"), 200),
                                          app=inline(r.get("app"), 60), shown=int(r.get("shown") or 0))
            if r.get("skeleton"):
                head += T.SNAPSHOT_SKELETON
            elif int(r.get("shown") or 0) < int(r.get("total") or 0):
                head += T.SNAPSHOT_CUT.format(total=int(r.get("total") or 0))
            return self._fenced(head, str(r.get("text") or ""))
        if op == "desktop.find":
            if not r.get("n"):
                return T.FIND_NONE
            return self._fenced(T.FIND_HEAD.format(snap_id=inline(r.get("snap_id"), 40), n=int(r["n"])),
                                str(r.get("text") or ""))
        if op == "desktop.get":
            return self._fenced(T.GET_VALUE.format(ref=inline(r.get("ref"), 12)), str(r.get("text") or ""))
        if op == "desktop.launch":
            return T.LAUNCHED.format(app=inline(r.get("app"), 60), window=inline(r.get("window"), 200))
        if op == "desktop.focus":
            return T.FOCUSED.format(window=inline(r.get("window"), 200))
        if op == "desktop.wait":
            if not r.get("occurred"):
                return T.WAIT_TIMEOUT.format(event=inline(r.get("event"), 20), timeout_s=int(r.get("timeout_s") or 0))
            return T.WAITED.format(event=inline(r.get("event"), 20), detail=inline(r.get("detail"), 120))
        # actions
        verified = r.get("delivery") == "delivered_verified"
        tpl = T.DELIVERED if verified else T.DELIVERED_UNVERIFIED
        text = tpl.format(action=inline(r.get("action"), 20), target=inline(r.get("target") or "—", 160))
        if r.get("mode") == "physical":
            text += T.DELIVERED_PHYSICAL
        if r.get("focused"):
            text += T.AFTER_FOCUS.format(focused=inline(r.get("focused"), 120))
        return text

    @staticmethod
    def _fenced(head: str, text: str) -> str:
        block, suspect = fence_checked("desktop", text)
        return "\n".join(x for x in (suspect_header(int(suspect)), head, block) if x)

    def _ok(self, name: str, text: str) -> ToolResult:
        if self.scrub is not None:
            text = self.scrub(text)
        return ToolResult(call_id="", name=name, content=text)

    @staticmethod
    def _refused(name: str, code: str, **detail: Any) -> ToolResult:
        if code not in T.WHY:
            detail, code = {"code": code}, "failed"
        safe = _Blank({k: inline(v, 120) for k, v in detail.items()})
        text = T.WHY[code].format_map(safe)
        return ToolResult(call_id="", name=name, content=T.REFUSED.format(why=text), is_error=True)


__all__ = ["DesktopError", "DesktopNode", "DesktopService", "DesktopTask", "DesktopToolSource", "ENROLL_TOOL",
           "build_desktop", "marks_floor"]
