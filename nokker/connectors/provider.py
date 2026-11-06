"""`ConnectorProvider.sources_for(tenant_id)` : les ToolSource (mail, agenda) du locataire courant, pour le registre du tour.
Lit `connector_credentials` ; en bêta, la connexion du noyau est celle du locataire (RLS). Une source illisible
(clé changée, réglages périmés) est sautée et journalisée : un connecteur cassé ne casse jamais le tour.

Magasin multi (068, docs/CONNECTORS-MULTI.md) : UNE source par nature (`boxes.BoxSource`) regroupe les boîtes du compte
et celles partagées avec lui (`shared(account_id)`, le service des connecteurs) ; les droits sont relus à chaque appel.
Magasin d'avant (Lokkal, fichier) : un connecteur par nature, comme avant."""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from nokker.connectors import build_connector
from nokker.connectors.boxes import Box, BoxSource
from nokker.connectors.errors import ConnectorError
from nokker.connectors.store import KINDS, ConnectorStore, is_multi
from nokker.core.ports import ToolSource
from nokker.secrets.base import SecretsError

logger = logging.getLogger("nokker.connectors")


class ConnectorProvider:
    """`resolve` (Lokkal, lot X4) : l'identifiant rangé peut être une référence `secret:NOM`, lue au coffre ici, au
    moment de construire la source ; nom absent ou coffre scellé → connecteur sauté et journalisé (sans valeur).
    `shared(account_id)` : les connecteurs partagés avec ce compte (`Connectors.shared`), None = aucun partage."""

    def __init__(self, store: ConnectorStore, account_for_tenant: Callable[[str], Any],
                 resolve: Callable[[str], str] | None = None, locality: str = "cloud",
                 shared: Callable[[str], list[Any]] | None = None) -> None:
        # `locality` (docs/LOKKAL.md §6.7) : « cloud » chez nokker.ai, « local » dans Lokkal ; portée par chaque outil.
        self.store, self._account_for_tenant, self._resolve = store, account_for_tenant, resolve
        self.locality = locality
        self.shared = shared
        # nokker/connectors/oauth.py::OAuth (jetons des connecteurs `oauth:<id>`) ; `oauth_from` : lu à l'usage (relais)
        self._oauth: Any = None
        self.oauth_from: Callable[[], Any] | None = None

    @property
    def oauth(self) -> Any:
        return self._oauth if self._oauth is not None else (self.oauth_from() if self.oauth_from else None)

    @oauth.setter
    def oauth(self, value: Any) -> None:
        self._oauth = value

    def _account(self, tenant_id: str) -> str | None:
        found = self._account_for_tenant(tenant_id)
        return getattr(found, "account_id", found) or None  # un Account (tenancy) ou directement son id

    def _make(self, kind: str, settings: dict[str, Any], secret: str, *, check_only: bool = False) -> ToolSource | None:
        """Connecteur d'un secret rangé : mot de passe (ou `secret:NOM` lu au coffre), ou `oauth:<id>` = jeton d'accès
        FRAIS du compte OAuth (rafraîchi s'il le faut), lu dans le locataire COURANT (celui du propriétaire).
        `check_only` : vérifie sans réseau (compte OAuth présent), pour le catalogue ; ne rend rien."""
        if secret.startswith("oauth:"):
            oa_id = secret[len("oauth:"):]
            if self.oauth is None:
                raise ConnectorError("unsupported", "connexion OAuth éteinte sur cette instance")
            if check_only:
                if self.oauth.store.get(oa_id) is None:
                    raise ConnectorError("not_configured", "compte relié retiré")
                return None
            return build_connector(kind, settings, self.oauth.access_token(oa_id, kind), locality=self.locality)
        secret = self._resolve(secret) if self._resolve else secret
        return build_connector(kind, settings, secret, locality=self.locality)

    def _build(self, account_id: str, kind: str) -> ToolSource | None:
        """Le connecteur PAR DÉFAUT `kind` du compte, tel qu'un tour le reçoit : réglages du magasin, identifiant
        déchiffré, puis lu au coffre s'il est une référence `secret:NOM`. Lève `ConnectorError` / `SecretsError`."""
        cred = self.store.get(account_id, kind)
        if cred is None:
            return None
        return self._make(kind, cred.settings, cred.secret)

    # ── boîtes accessibles (magasin multi) ──
    def catalog(self, account_id: str) -> list[Box]:
        """Mes connecteurs puis ceux partagés avec moi, MAINTENANT (relu à chaque appel d'outil). Un connecteur
        illisible (réglages périmés, clé changée, coffre scellé) est sauté et journalisé, sans valeur."""
        out = []
        for b in self._listed(account_id):
            try:
                self._build_box(account_id, b, check_only=True)  # construction seule : aucun accès réseau
            except (ConnectorError, SecretsError) as exc:
                logger.warning("connecteur %s ignoré pour %s : %s", b.kind, account_id, getattr(exc, "code", exc))
                continue
            out.append(b)
        return out

    def _listed(self, account_id: str) -> list[Box]:
        out = [Box(id=s.id, kind=s.kind, label=s.label, access="owner", is_default=s.is_default,
                   address=str(s.settings.get("address") or ""), owner_account=account_id,
                   mode=str(s.settings.get("mode") or "")) for s in self.store.statuses(account_id)]
        if self.shared is not None:
            try:
                shared = self.shared(account_id)
            except Exception:  # noqa: BLE001 — partages illisibles : mes boîtes seulement (fermé par défaut)
                logger.warning("connecteurs : partages de %s illisibles", account_id, exc_info=True)
                shared = []
            for sh in shared:
                s, g = sh.status, sh.grant
                out.append(Box(id=s.id, kind=s.kind, label=s.label, access=g.level, address=str(s.settings.get("address") or ""),
                               owner_account=g.owner_account, owner_tenant=g.tenant_id, owner_name=sh.owner.name,
                               mode=str(s.settings.get("mode") or "")))
        return out

    def boxes_for(self, tenant_id: str) -> list[Box]:
        account_id = self._account(tenant_id)
        return self.catalog(account_id) if account_id else []

    def _build_box(self, account_id: str, box: Box, *, check_only: bool = False) -> ToolSource:
        from contextlib import nullcontext

        from nokker.tenancy.context import tenant_scope

        # le secret (et le jeton OAuth) restent dans le locataire de leur propriétaire : lus ET construits là-bas
        with nullcontext() if box.mine else tenant_scope(box.owner_tenant):
            cred = self.store.by_id(account_id if box.mine else box.owner_account, box.id)  # type: ignore[attr-defined]
            if cred is None:
                raise ConnectorError("not_configured", "connecteur retiré")
            if check_only and cred.secret.startswith("oauth:"):
                return self._make(box.kind, cred.settings, cred.secret, check_only=True)  # type: ignore[return-value]
            return self._make(box.kind, cred.settings, cred.secret)  # type: ignore[return-value]

    def sources_for(self, tenant_id: str) -> list[ToolSource]:
        account_id = self._account(tenant_id)
        if not account_id:
            return []
        if is_multi(self.store):
            return self._grouped(account_id, tenant_id)
        out: list[ToolSource] = []
        for kind in KINDS:
            try:
                src = self._build(account_id, kind)
                if src is not None:
                    out.append(src)
            except ConnectorError as exc:
                logger.warning("connecteur %s ignoré pour %s : %s", kind, tenant_id, exc.code)
            except SecretsError as exc:
                logger.warning("connecteur %s ignoré pour %s : %s", kind, tenant_id, exc)
        return out

    def _grouped(self, account_id: str, tenant_id: str) -> list[ToolSource]:
        from nokker.connectors.calendar import CalendarConnector
        from nokker.connectors.mail import _SPECS as MAIL_SPECS

        boxes = self.catalog(account_id)
        out: list[ToolSource] = []
        for kind in KINDS:
            mine = [b for b in boxes if b.kind == kind]
            if not mine:
                continue
            if kind == "mail":
                mail_specs = [_located(sp, self.locality) for sp in MAIL_SPECS]
                base = lambda _boxes, specs=mail_specs: specs  # noqa: E731
            else:
                # specs d'agenda : celles de CalDAV dès qu'un agenda accessible s'écrit (CalDAV) ; sinon, comme avant
                # avec un abonnement iCal seul, la lecture seulement (calendar_events)
                caldav = CalendarConnector({"mode": "caldav", "url": "https://caldav.invalid/", "username": "x"}, "x",
                                           locality=self.locality).specs()
                base = lambda boxes, specs=caldav: specs if any(b.mode in ("caldav", "google", "graph") for b in boxes) else specs[:1]  # noqa: E731
            out.append(BoxSource(kind, catalog=lambda a=account_id: self.catalog(a),
                                 build=lambda b, a=account_id: self._build_box(a, b), base=base))
        return out

    def grouped(self, account_id: str) -> dict[str, ToolSource]:
        """Sources regroupées par nature d'un compte (relais Lokkal, X20 : une entrée par nature)."""
        return {src.kind: src for src in self._grouped(account_id, "")}  # type: ignore[attr-defined]

    def secrets_for(self, account_id: str, kind: str) -> list[str]:
        """Identifiants des boîtes accessibles de cette nature : filet de caviardage d'un résultat relayé."""
        from nokker.tenancy.context import tenant_scope

        out = []
        for b in self._listed(account_id):
            if b.kind != kind:
                continue
            try:
                if b.mine:
                    cred = self.store.by_id(account_id, b.id)  # type: ignore[attr-defined]
                else:
                    with tenant_scope(b.owner_tenant):
                        cred = self.store.by_id(b.owner_account, b.id)  # type: ignore[attr-defined]
            except (ConnectorError, SecretsError):
                continue
            if cred is not None and cred.secret:
                out.append(cred.secret)
        return out

    def mail_for(self, tenant_id: str) -> Any:
        """Le connecteur mail PAR DÉFAUT du locataire, construit par le MÊME chemin que pour un tour (même compte, même
        magasin, même coffre) : la relève du déclencheur `mail:<filtre>` (`nokker/agents/mailwatch.py`). None : aucune
        boîte reliée. Lève `ConnectorError` / `SecretsError` (identifiant illisible), que l'appelant montre sans valeur."""
        account_id = self._account(tenant_id)
        return self._build(account_id, "mail") if account_id else None


def _located(sp: Any, locality: str | None) -> Any:
    from dataclasses import replace

    return replace(sp, locality=locality) if locality else sp


__all__ = ["ConnectorProvider"]
