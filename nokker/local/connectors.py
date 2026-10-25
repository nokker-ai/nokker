"""Boîte mail posée DANS Lokkal, pour le propriétaire local (docs/LOKKAL.md §6.7, §7.25 ; docs/en/secrets.md).

Le propriétaire local (`LOCAL_OWNER`, locataire interne de la machine) n'a pas de compte `us_…` : `/v1/connectors` ne
le sert pas, et les tours du locataire interne ne reçoivent pas les connecteurs d'un compte. Ce module lui donne UNE
boîte IMAP/SMTP à lui, de localité `local` :

  - **config** : `<état du noyau>/connectors.json` (0600) — préréglage, hôtes, ports, adresse, utilisateur, et le NOM
    du mot de passe dans le coffre local (X4) sous la forme `secret:NOM`. Jamais une valeur : `put` refuse tout ce qui
    n'est pas une référence, et la route refuse un nom absent du coffre sans le répéter ;
  - **routes** `/v1/local/connectors*` (jeton local seulement ; par la passerelle : 403, la config d'une boîte de la
    machine ne se lit ni ne s'écrit depuis nokker.ai) — mêmes formes que `/v1/connectors` (carte testée avant d'être
    rangée, `nokker/connectors/service.py`), plus `secret` (le NOM) sur la carte ;
  - **outils** (`LocalMailSource`) : `mail_search`, `mail_read`, `mail_draft`, `mail_move`, `mail_trash`, `mail_send`
    du connecteur, localité `local`, dans le registre interne AVANT les connecteurs relayés (X20) : avec les deux, la
    boîte locale gagne (première source qui déclare un nom). Le mot de passe est lu au coffre à CHAQUE appel, dans la
    portée du tour : pendant le passage d'un agent, seul un nom déclaré dans ses `secrets` se résout (X4) ;
  - **relève** du déclencheur `mail:` (`Mailboxes.mail_for`) : la boîte locale pour le locataire interne, jamais pour un
    autre ; une boîte relayée n'est jamais relevée ;
  - **envoi** : `mail_send` et `mail_trash` de la boîte locale sont TOUJOURS une carte forte (`LOCAL_STRONG_RULE`,
    `qa/permissions.py`), quels que soient le mode et les règles `allow`, et aucune règle d'agent ne la lève ;
  - **miroir** (`keep_local_fn`) : un tour qui a appelé un outil de la boîte locale, et tout tour d'un passage déclenché
    par `mail:`, restent sur la machine (le miroir les saute) ; l'inventaire X21 ne publie que la nature `mail`.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nokker.connectors.errors import ConnectorError
from nokker.connectors.store import Credential, Status, _check
from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import ToolResult
from nokker.secrets.base import SecretsError
from nokker.tenancy.context import INTERNAL_TENANT

logger = logging.getLogger("nokker.local.connectors")

FILE_NAME = "connectors.json"
KIND = "mail"
LOCALITY = "local"
from nokker.qa.permissions import LOCAL_STRONG_RULE  # noqa: E402  (envoi, corbeille : carte forte)
MAIL_TOOLS = frozenset({"mail_search", "mail_read", "mail_draft", "mail_move", "mail_trash", "mail_send"})
PREFIX = "/v1/local/connectors"
NOT_A_NAME = ("secret : le NOM d'un secret du coffre local est attendu (le ranger d'abord : PUT /v1/local/secrets/NOM "
              "ou `nokker secret set NOM`), jamais le mot de passe lui-même")
NOT_IN_VAULT = ("secret : ce nom n'est pas dans le coffre local — ranger d'abord le mot de passe (PUT "
                "/v1/local/secrets/NOM ou `nokker secret set NOM`), puis donner ici son NOM")


def _iso(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


class LocalConnectorStore:
    """`ConnectorStore` (contrat C3) d'un seul propriétaire, dans un fichier : le « secret » rangé est TOUJOURS une
    référence `secret:NOM` (la valeur reste au coffre). Relu à chaque accès (une route ou un outil voit tout de suite
    ce qu'une autre a écrit)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            return dict(json.loads(self.path.read_text(encoding="utf-8")).get("connectors") or {})
        except FileNotFoundError:
            return {}

    def _write(self, rows: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"v": 1, "connectors": rows}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def put(self, account_id: str, kind: str, settings: dict[str, Any], secret: str) -> Status:
        from nokker.secrets.refs import ref_name

        try:
            name = ref_name(secret)
        except SecretsError:
            name = None
        if name is None:
            raise ConnectorError("bad_settings", NOT_A_NAME)
        _check(kind, settings, secret)
        now = datetime.now(UTC)
        with self._lock:
            rows = self._read()
            rows[kind] = {"settings": json.loads(json.dumps(settings)), "secret": f"secret:{name}",
                          "created_at": now.isoformat(), "last_ok_at": None}
            self._write(rows)
        return Status(account_id, kind, dict(settings), now, None)

    def get(self, account_id: str, kind: str) -> Credential | None:
        row = self._read().get(kind)
        if row is None:
            return None
        return Credential(account_id, kind, dict(row["settings"]), _iso(row["created_at"]), _iso(row.get("last_ok_at")),
                          str(row["secret"]))

    def statuses(self, account_id: str) -> list[Status]:
        return [Status(account_id, k, dict(r["settings"]), _iso(r["created_at"]), _iso(r.get("last_ok_at")))
                for k, r in sorted(self._read().items())]

    def delete(self, account_id: str, kind: str) -> bool:
        with self._lock:
            rows = self._read()
            if rows.pop(kind, None) is None:
                return False
            self._write(rows)
            return True

    def mark_ok(self, account_id: str, kind: str) -> None:
        with self._lock:
            rows = self._read()
            if kind in rows:
                rows[kind]["last_ok_at"] = datetime.now(UTC).isoformat()
                self._write(rows)

    def secret_name(self, kind: str) -> str | None:
        row = self._read().get(kind)
        return str(row["secret"])[len("secret:"):] if row else None


class LocalConnectors:
    """La boîte du propriétaire local : service des routes, connecteur des tours et de la relève."""

    def __init__(self, path: Path, vault: Any) -> None:
        from nokker.connectors.service import Connectors

        self.store = LocalConnectorStore(path)
        self.vault = vault  # nokker.secrets.vault.Vault (portée `user`)
        self.service = Connectors(self.store, resolve=self._resolve, locality=LOCALITY)
        self.on_change: Callable[[], Any] | None = None  # serve : le registre des outils se relit
        self._source = LocalMailSource(self)

    # ── coffre ──

    def _resolve(self, ref: str) -> str:
        """`secret:NOM` → valeur, dans la portée courante (un passage d'agent : ses noms seulement). SecretsError."""
        from nokker.secrets.refs import SecretResolver

        resolver = self.vault.resolver() if self.vault is not None else SecretResolver(None)
        return resolver.resolve(ref)

    def _vault_names(self) -> set[str]:
        try:
            return {i.name for i in self.vault.list()} if self.vault is not None else set()
        except SecretsError:
            return set()

    # ── la boîte ──

    def configured(self) -> bool:
        return self.store.get(LOCAL_OWNER_ID, KIND) is not None

    def mail(self) -> Any:
        """Le `MailConnector` de la boîte locale (mot de passe lu au coffre maintenant), ou None. Lève SecretsError /
        ConnectorError (sans valeur)."""
        from nokker.connectors import build_connector

        cred = self.store.get(LOCAL_OWNER_ID, KIND)
        if cred is None:
            return None
        return build_connector(KIND, cred.settings, self._resolve(cred.secret), locality=LOCALITY)

    def source(self) -> LocalMailSource:
        return self._source

    def kinds(self) -> list[str]:
        """Natures posées ici (inventaire X21 : des noms, jamais une adresse ni un hôte)."""
        return [s.kind for s in self.store.statuses(LOCAL_OWNER_ID)]

    def _card(self) -> dict[str, Any]:
        card = next(c for c in self.service.cards(LOCAL_OWNER_ID) if c["kind"] == KIND)
        name = self.store.secret_name(KIND)
        return {**card, "secret": name} if name else card

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001 — l'index des outils se relira au prochain rafraîchissement
                logger.exception("connecteurs locaux : rafraîchissement des outils en échec")

    # ── routes ──

    def handle(self, method: str, path: str, body: dict[str, Any], relayed: bool) -> tuple[int, dict[str, Any]] | None:
        """`GET /v1/local/connectors` → `{connectors: [carte], presets}` ; `PUT /v1/local/connectors/mail {settings,
        secret: "NOM" | "secret:NOM"}` → carte testée puis rangée ; `POST …/mail/test` → `{ok, message, code}` ;
        `DELETE …/mail` → `{revoked}`. Par la passerelle : 403 (tout, lecture comprise)."""
        from nokker.connectors.http import STATUS

        route = path.split("?", 1)[0].rstrip("/")
        parts = route[len(PREFIX):].strip("/").split("/") if route != PREFIX else []
        if relayed:
            return 403, {"error": {"message": "la boîte mail de la machine se règle sur la machine seulement (jeton "
                                              "local), jamais par la passerelle"}}
        try:
            if method == "GET" and not parts:
                return 200, {"connectors": [self._card()], "presets": {"mail": self.service.presets()["mail"]}}
            if parts and parts[0] != KIND:
                raise ConnectorError("unsupported", "connecteur local : mail seulement")
            if method == "PUT" and parts == [KIND]:
                return 200, self._put(body if isinstance(body, dict) else {})
            if method == "POST" and parts == [KIND, "test"]:
                return 200, self.service.test(LOCAL_OWNER_ID, KIND)
            if method == "DELETE" and parts == [KIND]:
                revoked = self.service.revoke(LOCAL_OWNER_ID, KIND)
                if revoked:
                    self._changed()
                return 200, {"revoked": revoked}
        except ConnectorError as e:
            return STATUS.get(e.code, 500), {"error": e.code, "message": e.message}
        return None

    def _put(self, body: dict[str, Any]) -> dict[str, Any]:
        from nokker.agents.spec import _looks_like_value
        from nokker.secrets.base import check_name

        settings = body.get("settings")
        if not isinstance(settings, dict):
            raise ConnectorError("bad_settings", "settings requis")
        secret_keys = ("password", "secret", "value", "app_password", "token")
        if any(k in settings for k in secret_keys) or any(k in body for k in secret_keys if k != "secret"):
            raise ConnectorError("bad_settings", NOT_A_NAME)
        raw = str(body.get("secret") or "").strip()
        name = raw[len("secret:"):] if raw.startswith("secret:") else raw
        try:
            check_name(name)
        except SecretsError:
            raise ConnectorError("bad_settings", NOT_A_NAME) from None
        if _looks_like_value(name):
            raise ConnectorError("bad_settings", NOT_A_NAME)
        if name not in self._vault_names():
            raise ConnectorError("bad_settings", NOT_IN_VAULT)  # sans répéter ce qui a été donné
        card = self.service.connect(LOCAL_OWNER_ID, KIND, settings, f"secret:{name}")
        self._changed()
        return {**card, "secret": name}


# Le propriétaire local (nokker/local/api.py::LOCAL_OWNER), sans import circulaire.
LOCAL_OWNER_ID = "local-owner"


class LocalMailSource:
    """`ToolSource` de la boîte locale : les outils du connecteur mail, localité `local` ; vide sans boîte."""

    def __init__(self, owner: LocalConnectors) -> None:
        self.owner = owner

    def specs(self) -> list[ToolSpec]:
        if not self.owner.configured():
            return []
        from nokker.connectors.mail import _SPECS

        return [replace(s, locality=LOCALITY) for s in _SPECS]

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            box = self.owner.mail()
        except SecretsError as e:
            hint = ""
            if e.code == "not_allowed":
                hint = (f" — ce passage d'agent doit déclarer {self.owner.store.secret_name(KIND)} dans ses `secrets` "
                        "pour lire la boîte mail locale")
            return ToolResult(call_id="", name=name, content=f"boîte mail locale : {e}{hint}", is_error=True)
        except ConnectorError as e:
            return ToolResult(call_id="", name=name, content=f"boîte mail locale : {e.code} : {e.message}",
                              is_error=True)
        if box is None:
            return ToolResult(call_id="", name=name, content="aucune boîte mail locale (PUT /v1/local/connectors/mail)",
                              is_error=True)
        return box.call(name, arguments, ctx)


class Mailboxes:
    """Boîte relevée par le déclencheur `mail:` (`mailwatch.MailWatcher.mailbox`) : celle de Lokkal pour le locataire
    interne (le propriétaire local), celle du compte (`ConnectorProvider.mail_for`) pour un locataire de testeur."""

    def __init__(self, local: LocalConnectors | None = None, provider: Any = None) -> None:
        self.local, self.provider = local, provider

    def mail_for(self, tenant_id: str) -> Any:
        if tenant_id == INTERNAL_TENANT and self.local is not None and self.local.configured():
            return self.local.mail()
        if tenant_id != INTERNAL_TENANT and self.provider is not None:
            return self.provider.mail_for(tenant_id)
        return None


def turn_tools(turn: dict[str, Any]) -> set[str]:
    """Noms des outils appelés par un tour sérialisé (`store/pg.py::turn_to_dict`)."""
    out: set[str] = set()
    for m in turn.get("messages") or []:
        for tc in m.get("tool_calls") or []:
            out.add(str(tc.get("name") or ""))
        if m.get("role") == "tool" and m.get("name"):
            out.add(str(m["name"]))
    return out


def keep_local_fn(local: LocalConnectors | None, run_event_of: Callable[[str], str | None] | None
                  ) -> Callable[[str, dict[str, Any]], bool]:
    """Pour le miroir (`MirrorWorker.keep_local`) : True si un tour reste sur la machine — il a appelé un outil de la
    boîte locale (boîte posée : les outils `mail_*` sont les siens, elle passe devant la relayée), ou il appartient au
    passage d'un agent déclenché par `mail:` (son entrée porte expéditeurs et sujets)."""

    def keep(thread_id: str, turn: dict[str, Any]) -> bool:
        if run_event_of is not None:
            try:
                if str(run_event_of(thread_id) or "").startswith("mail:"):
                    return True
            except Exception:  # noqa: BLE001 — dans le doute, le tour reste ici
                logger.exception("miroir : passage du fil %s illisible, tour gardé sur la machine", thread_id)
                return True
        return local is not None and local.configured() and bool(turn_tools(turn) & MAIL_TOOLS)

    return keep


__all__ = ["FILE_NAME", "LOCAL_STRONG_RULE", "LocalConnectorStore", "LocalConnectors", "LocalMailSource",
           "MAIL_TOOLS", "Mailboxes", "keep_local_fn", "turn_tools"]
