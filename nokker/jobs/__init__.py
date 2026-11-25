"""Pack Emploi (docs/JOBS-PACK.md) : outils `jobs_*` adossés au service emploi (API partenaire, §13).

Bloc de config `jobs_pack` (éteint par défaut : aucune source, aucune route, contexte du modèle inchangé à l'octet) :

    jobs_pack:
      enabled: true
      base_url: https://…            # adresse du service emploi (réseau privé, jamais par le proxy public)
      private_key: secret:JOBS_PARTNER_KEY   # clé privée P-256 (PEM) du client de service, lue dans l'environnement
      issuer: nokker                 # `iss` du JWT ES256 (identifiant du client chez le service)
      audience: …                    # `aud` exigée par le service (fixée par lui)
      tokens_per_turn: 12000         # grille de débit : jetons du service = 1 tour du quota Nokker
      residence_country: CH          # pays de résidence transmis à la création d'un compte partenaire
      proxy: http://egress-proxy:1055   # facultatif : proxy HTTP de sortie (bêta : nœud Tailscale), http:// seulement

Sans `base_url`, `audience` ou référence de clé : aucune source (le pack n'existe pas sur cette instance).
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from nokker.jobs.client import Es256Signer, PartnerClient, Transport, httpx_transport
from nokker.jobs.quota import DEFAULT_TOKENS_PER_TURN, NoQuota, TurnGridQuota


def enabled(block: dict[str, Any] | None) -> bool:
    return bool(isinstance(block, dict) and block.get("enabled"))


def key_reader(block: dict[str, Any], resolve: Callable[[str], str] | None = None) -> Callable[[], str] | None:
    """Lecteur de la clé privée (`secret:NOM`, jamais en clair dans la config) ; None si la référence manque."""
    ref = str(block.get("private_key") or "")
    if not ref.startswith("secret:"):
        return None
    read = resolve or (lambda r: os.environ.get(r[len("secret:"):], ""))
    return lambda: read(ref).replace("\\n", "\n")


logger = logging.getLogger("nokker.jobs")


def proxy_of(block: dict[str, Any]) -> str | None:
    """Proxy de sortie du bloc (`proxy`), ou None s'il est absent. Une valeur présente mais invalide (autre schéma que
    http://, pas d'hôte, identifiants, chemin) lève ValueError : le pack n'est pas construit plutôt que de sortir en
    direct."""
    raw = str(block.get("proxy") or "").strip()
    if not raw:
        return None
    try:
        u = urlsplit(raw)
        port = u.port
    except ValueError:
        raise ValueError("jobs_pack.proxy illisible") from None
    if u.scheme != "http" or not u.hostname or u.username or u.password or u.path not in ("", "/") \
            or u.query or u.fragment:
        raise ValueError("jobs_pack.proxy : http://hôte:port attendu")
    return f"http://{u.hostname}:{port}" if port else f"http://{u.hostname}"


def build_client(block: dict[str, Any], resolve: Callable[[str], str] | None = None,
                 transport: Transport | None = None) -> PartnerClient | None:
    base = str(block.get("base_url") or "").strip()
    read = key_reader(block, resolve)
    audience = str(block.get("audience") or "").strip()
    if not base or read is None or not audience:
        return None
    try:
        proxy = proxy_of(block)
    except ValueError as e:
        logger.warning("pack Emploi : %s ; service emploi non configuré", e)
        return None
    if transport is None and proxy:
        transport = httpx_transport(proxy=proxy)
    signer = Es256Signer(read, str(block.get("issuer") or "nokker"), audience,
                         int(block.get("token_ttl_s") or 120))
    return PartnerClient(base, signer, transport)


def build_jobs(block: dict[str, Any] | None, *, store: Any, account_of: Callable[[str], Any], households: Any = None,
               limits: Any = None, resolve: Callable[[str], str] | None = None, transport: Transport | None = None,
               renderer: Any = None, planner: Any = None, uploads: Any = None,
               on_change: Callable[[str], None] | None = None) -> Any:
    """La source `jobs_*`, ou None (drapeau éteint, service non configuré)."""
    if not enabled(block):
        return None
    client = build_client(block, resolve, transport)
    if client is None:
        return None
    from nokker.jobs.source import JobsSource, MemoryUploads

    quota = TurnGridQuota(limits, int(block.get("tokens_per_turn") or DEFAULT_TOKENS_PER_TURN)) \
        if limits is not None else NoQuota()
    return JobsSource(store, client, account_of=account_of, households=households, quota=quota,
                      uploads=uploads if uploads is not None else MemoryUploads(), renderer=renderer, planner=planner,
                      residence_country=str(block.get("residence_country") or "CH"), on_change=on_change)


def partner_eraser(dsn: str, block: dict[str, Any], resolve: Callable[[str], str] | None = None,
                   transport: Transport | None = None) -> Callable[[Any], None]:
    """Crochet d'effacement d'un compte Nokker (`nokker/admin/erase.py`) : AVANT la validation, efface chez le service
    emploi chaque compte partenaire du locataire effacé ; un échec lève et rien n'est validé (effacement Nokker =
    effacement chez le service, §13.3)."""

    def run(report: Any) -> None:
        from nokker.admin.erase import EraseError
        from nokker.jobs.client import PartnerError
        from nokker.jobs.store import PgJobsStore
        from nokker.tenancy.context import tenant_scope

        client = build_client(block, resolve, transport)
        if client is None:
            raise EraseError("service emploi non configuré : effacement des comptes partenaires impossible, rien "
                             "n'est effacé")
        with tenant_scope(report.tenant_id):
            ids = PgJobsStore(dsn).partner_accounts()
        for pid in ids:
            try:
                client.delete_account(pid)
            except PartnerError as e:
                raise EraseError(f"service emploi injoignable ({e.code}) : comptes partenaires non effacés, rien "
                                 "n'est effacé ; relancer plus tard") from None
        report.deleted["service emploi : comptes partenaires"] = len(ids)

    return run


__all__ = ["build_client", "build_jobs", "enabled", "key_reader", "partner_eraser", "proxy_of"]
