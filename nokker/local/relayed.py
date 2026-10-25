"""Connecteurs relayés, côté Lokkal (lot X20, docs/LOKKAL.md §6.7 et §7.12).

Les connecteurs de comptes en ligne posés au web (mail, agenda) apparaissent dans la session Lokkal comme des outils
ordinaires, de localité `cloud` ; leur identifiant ne vient JAMAIS ici. Un appel :
  1. passe d'abord par les portes locales (permissions, invariants). Un outil `cloud` à effet demande TOUJOURS une
     carte, quels que soient le mode et les règles `allow` (`qa/permissions.py`) : nokker.ai exige la preuve ;
  2. part par le lien de compte : `POST <nokker.ai>/v1/gateway/connectors/{id}/call` (jeton de machine), avec, pour
     un outil à effet, la preuve de la carte approuvée (`approval` : id, empreinte des arguments, comment et quand) ;
     pour un envoi externe ou un destructif (X20b), la carte est FORTE, signée sur le téléphone par le chemin X10, et
     la signature vérifiée par la machine part avec l'appel (`approval.signed`) : nokker.ai la vérifie à son tour
     contre les appareils du compte ; une carte forte approuvée sans elle (jeton local) n'envoie rien ;
  3. le résultat revient caviardé par nokker.ai, puis par les invariants locaux (la boucle), et une ligne va au
     journal local (`relay-audit.jsonl` : ni arguments ni résultat, l'`audit_id` de nokker.ai pour croiser).
Machine non liée, jeton révoqué, nokker.ai injoignable : une erreur claire, jamais un faux résultat.

La liste des outils est relue toutes les 60 s (fil de fond, `start`) : `specs()` ne touche jamais le réseau.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult

logger = logging.getLogger("nokker.local.relayed")

LOCALITY = "cloud"
_RE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RE_CONNECTOR = re.compile(r"^[a-z_]{1,32}$")


# Envoi externe et destructif d'un compte nokker.ai : carte FORTE signée au téléphone (X20b), vérifiée par nokker.ai.
STRONG_EFFECTS = frozenset({Effect.SEND_EXTERNAL, Effect.DESTRUCTIVE})
STRONG_LOCAL = ("envoi externe ou action destructive d'un compte nokker.ai : la carte est à signer sur ton téléphone "
                "(app.nokker.ai), une approbation sur la machine ne suffit pas ; rien n'a été envoyé")


def _err(name: str, text: str) -> ToolResult:
    return ToolResult(call_id="", name=name, content=text, is_error=True)


_RANK = {Effect.READ: 0, Effect.WRITE: 1, Effect.SEND_EXTERNAL: 2, Effect.DESTRUCTIVE: 3}


def _send_if(effect: Effect, keys: tuple[str, ...], name: str = "") -> Callable[[dict[str, Any]], Effect] | None:
    """Effet d'un appel d'après la liste de nokker.ai : un argument de `send_if` non vide (des invités) en fait un
    envoi externe, donc une carte forte ; et l'effet que le noyau local donne au même appel d'après ses arguments
    (`tools/effects.py::ARGUMENT_EFFECTS` : `mail_move` vers la corbeille = destructif, carte forte), jamais plus
    faible. nokker.ai classe à nouveau de son côté, c'est lui qui décide. None : l'effet ne dépend pas des arguments."""
    from nokker.tools.effects import ARGUMENT_EFFECTS

    local = ARGUMENT_EFFECTS.get(name)
    if not keys and local is None:
        return None

    def of(arguments: dict[str, Any]) -> Effect:
        out = Effect.SEND_EXTERNAL if any((arguments or {}).get(k) for k in keys) else effect
        if local is not None:
            mine = local(arguments or {})
            if _RANK.get(mine, 4) > _RANK.get(out, 4):
                out = mine
        return out

    return of


@dataclass
class RelayedConnectors:
    """`ToolSource` des connecteurs posés chez nokker.ai. `token()` lit le jeton de machine au coffre (None = non liée) ;
    `approvals` : le port des approbations du noyau local (la carte qui autorise un appel à effet) ; `signed(id)` :
    la signature d'appareil vérifiée d'une carte forte décidée au téléphone (`RemoteApprovals.signed_proof`, X20b ;
    None = aucun envoi externe possible) ; `on_change` : appelé quand la liste des outils change (serve :
    `kernel.tools.refresh`)."""

    base_url: str
    token: Callable[[], str | None]
    approvals: Any = None
    signed: Callable[[str], dict[str, Any] | None] | None = None
    journal_path: Path | None = None
    client: Any = None  # httpx.Client injectable (tests)
    interval_s: float = 60.0
    timeout_s: float = 60.0
    on_change: Callable[[], None] | None = None
    locality: str = LOCALITY
    _specs: list[ToolSpec] = field(default_factory=list)
    _route: dict[str, str] = field(default_factory=dict)  # outil → id du connecteur chez nokker.ai
    _cards: list[dict[str, Any]] = field(default_factory=list)
    _online: bool | None = None
    _synced_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = None

    # ── liste ──

    def _send(self, method: str, path: str, token: str, body: dict[str, Any] | None = None) -> Any:
        url, headers = self.base_url.rstrip("/") + path, {"Authorization": f"Bearer {token}"}
        if self.client is not None:
            return self.client.request(method, url, headers=headers, json=body)
        with httpx.Client(timeout=self.timeout_s) as client:
            return client.request(method, url, headers=headers, json=body)

    def _token(self) -> str | None:
        try:
            return self.token() or None
        except Exception:  # noqa: BLE001 — coffre scellé ou sans clé : machine considérée non liée
            return None

    def sync(self) -> bool:
        """Relit la liste chez nokker.ai. True si elle a été lue. Non liée ou jeton refusé : liste vide ; injoignable :
        la dernière liste reste (un appel dira alors clairement que nokker.ai ne répond pas)."""
        token = self._token()
        if not token:
            self._set([], [], online=None)
            return False
        try:
            r = self._send("GET", "/v1/gateway/connectors", token)
        except httpx.HTTPError as e:
            logger.debug("connecteurs relayés : nokker.ai injoignable (%s)", type(e).__name__)
            with self._lock:
                self._online = False
            return False
        if r.status_code in (401, 403, 404):  # jeton révoqué, ou relais éteint chez nokker.ai
            self._set([], [], online=r.status_code != 401)
            return False
        try:
            data = r.json() if r.status_code == 200 else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            with self._lock:
                self._online = False
            return False
        specs, route, cards = self._parse(data)
        self._set(specs, cards, online=True, route=route)
        return True

    def _parse(self, data: dict[str, Any]) -> tuple[list[ToolSpec], dict[str, str], list[dict[str, Any]]]:
        specs: list[ToolSpec] = []
        route: dict[str, str] = {}
        cards: list[dict[str, Any]] = []
        for c in data.get("connectors") or []:
            if not isinstance(c, dict) or not _RE_CONNECTOR.match(str(c.get("id") or "")):
                continue
            cid, label = str(c["id"]), str(c.get("label") or c["id"])[:120]
            names = []
            for t in c.get("tools") or []:
                if not isinstance(t, dict) or not t.get("relayed"):
                    continue
                name = str(t.get("name") or "")
                try:
                    effect = Effect(str(t.get("effect") or ""))
                except ValueError:
                    continue
                if not _RE_NAME.match(name) or name in route or not isinstance(t.get("parameters"), dict):
                    continue
                desc = " ".join(str(t.get("description") or name).split())[:300]
                send_if = tuple(str(k) for k in t.get("send_if") or [] if isinstance(k, str))
                specs.append(ToolSpec(name=name, description=f"{desc} (compte nokker.ai {label}, relayé)",
                                      parameters=t["parameters"], effect=effect, source=f"relay:{cid}",
                                      locality=LOCALITY, effect_of=_send_if(effect, send_if, name)))
                route[name] = cid
                names.append(name)
            cards.append({"id": cid, "kind": str(c.get("kind") or cid), "label": label, "locality": LOCALITY,
                          "state": str(c.get("state") or "unverified"), "tools": names})
        return specs, route, cards

    def _set(self, specs: list[ToolSpec], cards: list[dict[str, Any]], *, online: bool | None,
             route: dict[str, str] | None = None) -> None:
        with self._lock:
            changed = [(s.name, s.effect, s.description, s.effect_of is None) for s in specs] != \
                [(s.name, s.effect, s.description, s.effect_of is None) for s in self._specs]
            self._specs, self._route, self._cards = specs, dict(route or {}), cards
            self._online, self._synced_at = online, time.time()
        if changed and self.on_change is not None:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001 — un registre qui refuse de se rafraîchir ne casse pas le relais
                logger.warning("connecteurs relayés : rafraîchissement du registre en échec", exc_info=True)

    def status(self) -> dict[str, Any]:
        """Pour `GET /v1/local/setup` (`connectors`) et `nokker doctor` : jamais d'identifiant, il n'y en a pas ici."""
        with self._lock:
            return {"linked": self._token() is not None, "online": self._online, "synced_at": self._synced_at,
                    "connectors": [dict(c) for c in self._cards]}

    def start(self) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.sync()
                except Exception:  # noqa: BLE001 — le fil de fond ne meurt pas
                    logger.debug("connecteurs relayés : relecture en échec", exc_info=True)
                self._stop.wait(self.interval_s)

        self._thread = threading.Thread(target=loop, name="nokker-relayed-connectors", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()

    # ── ToolSource ──

    def specs(self) -> list[ToolSpec]:
        with self._lock:
            return list(self._specs)

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        with self._lock:
            spec = next((s for s in self._specs if s.name == name), None)
            cid = self._route.get(name)
        if spec is None or cid is None:
            return _err(name, f"outil inconnu : {name}")
        token = self._token()
        if not token:
            return _err(name, "machine non liée à un compte nokker.ai : le connecteur relayé n'est pas disponible "
                              "(relier la machine : nokker link)")
        arguments = dict(arguments or {})
        body: dict[str, Any] = {"tool": name, "arguments": arguments}
        effect = spec.effect_for(arguments)
        if effect is not Effect.READ:
            proof = self._proof(name, arguments, ctx, strong=effect in STRONG_EFFECTS)
            if isinstance(proof, str):
                self._journal(cid, name, effect, None, None, "refused_locally", None)
                return _err(name, proof)
            body["approval"] = proof
        approval_id = body.get("approval", {}).get("id")
        try:
            r = self._send("POST", f"/v1/gateway/connectors/{cid}/call", token, body)
        except httpx.ConnectError:
            with self._lock:
                self._online = False
            self._journal(cid, name, effect, approval_id, None, "offline", None)
            return _err(name, "nokker.ai injoignable : l'appel relayé n'a pas été fait, aucun résultat")
        except httpx.HTTPError as e:
            self._journal(cid, name, effect, approval_id, None, "no_answer", None)
            return _err(name, f"aucune réponse de nokker.ai ({type(e).__name__}) : l'appel a peut-être été fait, "
                              "vérifier avant de le relancer")
        try:
            data = r.json()
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        audit_id = data.get("audit_id") if isinstance(data.get("audit_id"), str) else None
        if r.status_code == 401:
            self._set([], [], online=True)  # jeton révoqué : les outils disparaissent au prochain tour
            self._journal(cid, name, effect, approval_id, r.status_code, "unlinked", audit_id)
            return _err(name, "nokker.ai refuse le jeton de machine (machine déliée ou révoquée) : rien n'a été fait")
        if r.status_code != 200:
            err = data.get("error") if isinstance(data.get("error"), dict) else {}
            code, msg = str(err.get("code") or r.status_code), str(err.get("message") or "refusé")[:300]
            self._journal(cid, name, effect, approval_id, r.status_code, f"refused:{code}", audit_id)
            return _err(name, f"refusé par nokker.ai ({code}) : {msg}")
        with self._lock:
            self._online = True
        is_error = bool(data.get("is_error"))
        self._journal(cid, name, effect, approval_id, 200, "error" if is_error else "executed", audit_id)
        handles = [str(h) for h in data.get("secret_handles") or [] if isinstance(h, str)]
        return ToolResult(call_id="", name=name, content=str(data.get("content") or ""), is_error=is_error,
                          secret_handles=handles)

    def _proof(self, name: str, arguments: dict[str, Any], ctx: ToolContext, *,
               strong: bool = False) -> dict[str, Any] | str:
        """La preuve de la carte qui autorise CET appel, ou le motif du refus (texte). `strong` (envoi externe,
        destructif) : la carte doit être forte et signée au téléphone (sa signature vérifiée part avec l'appel)."""
        from nokker.approvals.crypto import args_sha256

        if self.approvals is None:
            return "approbations locales éteintes : un outil à effet d'un connecteur relayé exige une carte"
        if not ctx.approval_id:
            return "outil à effet d'un compte nokker.ai : il faut une carte approuvée (rien n'a été envoyé)"
        card = self.approvals.get(ctx.approval_id)
        digest = args_sha256(arguments)
        if card is None or card.status != "approved" or card.tool_name != name \
                or card.args_sha256 != digest:
            return "la carte ne correspond pas à cet appel (outil, arguments ou statut) : rien n'a été envoyé"
        proof: dict[str, Any] = {"id": card.id, "args_sha256": digest, "decided_via": card.decided_via,
                                 "decided_by": card.decided_by, "decided_at": card.decided_at, "strength": card.strength}
        if strong:
            signed = self.signed(card.id) if self.signed is not None else None
            if card.strength != "strong" or not signed:
                return STRONG_LOCAL
            proof["signed"] = dict(signed)
        return proof

    def _journal(self, cid: str, tool: str, effect: Effect, approval_id: str | None, status: int | None,
                 outcome: str, audit_id: str | None) -> None:
        """Journal local (audit du côté machine) : ni arguments ni résultat."""
        if self.journal_path is None:
            return
        line = json.dumps({"at": round(time.time(), 3), "connector": cid, "tool": tool, "effect": effect.value,
                           "approval_id": approval_id, "status": status, "outcome": outcome, "audit_id": audit_id},
                          ensure_ascii=False)
        try:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.journal_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            logger.warning("connecteurs relayés : journal local illisible (%s)", self.journal_path)


__all__ = ["LOCALITY", "STRONG_EFFECTS", "RelayedConnectors"]
