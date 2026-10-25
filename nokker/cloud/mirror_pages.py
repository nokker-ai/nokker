"""Pages servies depuis le miroir quand la machine est éteinte (abonnés ; docs/LOKKAL.md §6.6 et §7.23,
docs/GATEWAY.md), côté nokker.ai.

La machine copie ici les pages (docs/BUBBLES.md §10) dont l'utilisateur a DEMANDÉ la copie, une par une ; jamais une
page `local` : la machine filtre à l'envoi (`nokker/core/locality.py`) et nokker.ai refuse à son tour une page qui ne
se dit pas `both` (seconde barrière). Une ligne courante par (machine, page) : la dernière version copiée, sous la RLS
du locataire (`lokkal_mirror_pages`, migration 033), plafond de taille PAR COMPTE (somme des pages de toutes ses
machines), purge quand l'utilisateur retire la copie ou supprime la page, quand il efface les copies de sa machine
(`POST /v1/mirror/erase`), quand la machine est révoquée, et avec le compte (`nokker erase`).

Servir : jamais à l'origine de nokker.ai. Le relais de `<nom>--pages.nokker.app`, quand le tunnel de la machine est
fermé, vérifie lui-même la preuve du navigateur (jeton de pages, cookie `__Host-nk_pg` ou capacité d'URL : les mêmes
liens signés qu'en ligne), puis demande la copie ici avec SA clé (`POST /v1/gateway/mirror/page`, en-tête
`X-Nokker-Relay-Key`) en disant le nom, le locataire prouvé et la machine du jeton s'il y en a un. nokker.ai rend la
copie avec un bandeau « Copie du <date>, machine hors ligne » ; le relais la sert avec les en-têtes des pages (CSP
`sandbox`, `frame-ancestors`, `nosniff`, `no-referrer`, `no-store`).

Pages publiques (docs/LOKKAL.md §7.26) : une copie peut porter sa publication (`public: {id_sha256, index, version}`,
l'EMPREINTE de l'id public, jamais l'id) si la version copiée est celle que l'utilisateur a publiée et si le compte y a
droit ; le relais la demande pour `<nom>--pub.nokker.app/<id>` machine éteinte (`POST /v1/gateway/mirror/public-page`).
Dépublier : `DELETE /v1/mirror/pages/{id}/public` (un droit), ou toute nouvelle copie sans `public`.

Registre des jetons de pages consommés (relais à plusieurs répliques) : `MemoryUsedJti` / `PgUsedJti` (migration 035),
empreintes des jti seulement, effacées à l'expiration.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
import threading
from datetime import UTC, datetime
from typing import Any

MAX_PAGE_BYTES = 5 * 1024 * 1024  # = nokker/pages/store.py::MAX_BYTES (une version)
MAX_PUT_BYTES = 8 * 1024 * 1024  # corps JSON d'une copie (base64 d'une page de 5 Mio, et ses champs)
DEFAULT_QUOTA_BYTES = 50 * 1024 * 1024  # par compte, toutes machines (`cloud_link.mirror_pages_quota_mb`)
_PAGE_ID = re.compile(r"^pg_[0-9a-f]{16}$")
_BODY = re.compile(rb"<body\b[^>]*>", re.IGNORECASE)
_HEAD_END = re.compile(rb"</head\s*>", re.IGNORECASE)
_DOCTYPE = re.compile(rb"^\s*<!doctype[^>]*>", re.IGNORECASE)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9_-]{24}$")
MAX_JTI = 128


class QuotaExceeded(Exception):
    def __init__(self, used: int, quota: int) -> None:
        super().__init__(f"{used}/{quota}")
        self.used, self.quota = used, quota


def is_page_id(s: str) -> bool:
    return bool(_PAGE_ID.match(s or ""))


def banner_text(mirrored_at: float) -> str:
    return f"Copie du {datetime.fromtimestamp(mirrored_at, tz=UTC):%Y-%m-%d %H:%M} UTC, machine hors ligne"


def with_banner(html: bytes, mirrored_at: float) -> bytes:
    """La copie avec son bandeau, en tête du corps (après `<body>`, sinon après `</head>`, sinon après le doctype,
    sinon au tout début) : jamais avant le doctype (le navigateur passerait en mode quirks). Styles en ligne seulement,
    aucun script : la CSP `sandbox` de la page s'applique au bandeau comme au reste."""
    div = ('<div data-nokker-mirror-banner role="status" style="position:sticky;top:0;z-index:2147483647;margin:0;'
           'padding:6px 12px;font:13px/1.4 system-ui,sans-serif;background:#fff4d6;color:#4a3b00;'
           f'border-bottom:1px solid #e6cf8a;text-align:center">{banner_text(mirrored_at)}</div>').encode()
    for rx in (_BODY, _HEAD_END, _DOCTYPE):
        m = rx.search(html)
        if m:
            return html[:m.end()] + div + html[m.end():]
    return div + html


# ─────────────────────────────── magasin ───────────────────────────────


class MemoryMirrorPages:
    """Copies des pages (tests, dev) ; `PgMirrorPages` (nokker/cloud/pg.py) a la même interface."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], dict[str, Any]] = {}  # (locataire, machine, page) → copie
        self.lock = threading.Lock()

    def put(self, tenant: str, machine: str, page_id: str, row: dict[str, Any], quota: int) -> int:
        """Range (ou remplace) la copie ; rend l'espace utilisé par le compte ensuite. Lève QuotaExceeded sans rien
        changer si la nouvelle copie dépasserait le plafond (l'ancienne version de CETTE page ne compte pas)."""
        with self.lock:
            used = sum(r["size"] for (t, m, p), r in self.rows.items() if t == tenant and (m, p) != (machine, page_id))
            if used + row["size"] > quota:
                raise QuotaExceeded(used, quota)
            self.rows[(tenant, machine, page_id)] = dict(row)
            return used + row["size"]

    def get(self, tenant: str, machine: str, page_id: str) -> dict[str, Any] | None:
        with self.lock:
            r = self.rows.get((tenant, machine, page_id))
            return dict(r) if r else None

    def delete(self, tenant: str, machine: str, page_id: str) -> bool:
        with self.lock:
            return self.rows.pop((tenant, machine, page_id), None) is not None

    def get_public(self, tenant: str, machine: str, public_sha256: str) -> dict[str, Any] | None:
        """La copie publique de cette machine dont l'id public a cette empreinte, ou None."""
        with self.lock:
            hit = next(((p, r) for (t, m, p), r in self.rows.items()
                        if t == tenant and m == machine and r.get("public_sha256") == public_sha256), None)
            return {**hit[1], "page_id": hit[0]} if hit else None

    def clear_public(self, tenant: str, machine: str, page_id: str) -> bool:
        """Dépublie la copie (elle reste copiée pour son propriétaire, plus jamais servie en public)."""
        with self.lock:
            r = self.rows.get((tenant, machine, page_id))
            if r is None or not r.get("public_sha256"):
                return False
            r["public_sha256"], r["public_index"] = None, False
            return True

    def of_machine(self, tenant: str, machine: str) -> list[dict[str, Any]]:
        with self.lock:
            return sorted(({**_listing(r), "page_id": p} for (t, m, p), r in self.rows.items()
                           if t == tenant and m == machine), key=lambda r: r["page_id"])

    def used(self, tenant: str) -> int:
        with self.lock:
            return sum(r["size"] for (t, _, _), r in self.rows.items() if t == tenant)

    def erase(self, tenant: str, machine: str) -> int:
        with self.lock:
            keys = [k for k in self.rows if k[0] == tenant and k[1] == machine]
            for k in keys:
                del self.rows[k]
            return len(keys)


def _listing(r: dict[str, Any]) -> dict[str, Any]:
    return {k: r[k] for k in ("title", "version", "size", "sha256", "page_updated_at", "mirrored_at")}


# ─────────────────────────────── routes ───────────────────────────────


def _err(status: int, code: str, message: str, **extra: Any) -> tuple[int, dict[str, Any]]:
    return status, {"error": {"code": code, "message": message}, **extra}


def push_page(store: Any, tenant: str, machine: str, page_id: str, body: Any, quota: int,
              now: float, public_allowed: bool = False) -> tuple[int, dict[str, Any]]:
    """`PUT /v1/mirror/pages/{page_id}` (jeton de machine, abonnés). Corps : `{title, version, sha256, locality,
    html_b64, updated_at?, public?}`. 403 `local_page` si la page ne se dit pas `both` ; 400 si le contenu ne correspond
    pas à son empreinte ou n'est pas lisible ; 413 au-delà de 5 Mio, ou `mirror_quota` au-delà du plafond du compte.
    `public` (§7.26) : `{id_sha256, index, version}` de la version copiée, ou null ; gardé seulement si le compte peut
    publier (`public_allowed`), sinon la copie est rangée sans publication (`public: false` dans la réponse)."""
    from nokker.core.locality import may_leave

    if not is_page_id(page_id):
        return _err(404, "not_found", "page inconnue")
    body = body if isinstance(body, dict) else {}
    if not may_leave(body.get("locality")):
        return _err(403, "local_page", "page `local` : elle ne quitte pas la machine")
    try:
        html = base64.b64decode(str(body.get("html_b64") or ""), validate=True)
    except (binascii.Error, ValueError):
        return _err(400, "bad_page", "html_b64 illisible")
    if len(html) > MAX_PAGE_BYTES:
        return _err(413, "too_big", f"page de plus de {MAX_PAGE_BYTES} octets")
    if not html or hashlib.sha256(html).hexdigest() != str(body.get("sha256") or ""):
        return _err(400, "bad_page", "contenu vide ou différent de son empreinte sha256")
    try:
        version = max(1, int(body.get("version") or 1))
        updated = float(body.get("updated_at") or now)
    except (TypeError, ValueError):
        return _err(400, "bad_page", "version ou updated_at illisible")
    pub = body.get("public")
    if pub is not None:
        if not (isinstance(pub, dict) and _SHA256.match(str(pub.get("id_sha256") or ""))
                and isinstance(pub.get("index"), bool) and pub.get("version") == version):
            return _err(400, "bad_page", "public : {id_sha256, index, version de CETTE copie} ou null")
        if not public_allowed:
            pub = None
    row = {"title": str(body.get("title") or "")[:200], "version": version, "size": len(html),
           "sha256": hashlib.sha256(html).hexdigest(), "html": html, "page_updated_at": updated, "mirrored_at": now,
           "public_sha256": pub["id_sha256"] if pub else None, "public_index": bool(pub and pub["index"])}
    try:
        used = store.put(tenant, machine, page_id, row, quota)
    except QuotaExceeded as e:
        return _err(413, "mirror_quota", f"plafond des pages copiées atteint ({e.quota} octets par compte)",
                    used=e.used, quota=e.quota)
    return 200, {"stored": True, "used": used, "quota": quota, "public": pub is not None}


def copy_for_relay(store: Any, links: Any, mirror_allowed: Any, *, host: str, tenant: str, machine: str | None,
                   page_id: str | None) -> dict[str, Any] | None:
    """Ce que le relais peut servir pour `host` (`<nom>.nokker.app`) à un navigateur dont il a vérifié la preuve
    (`tenant`, et `machine` si la preuve est un jeton de pages). None si le nom n'est pas une machine active de CE
    locataire, si la machine ne correspond pas, ou si le compte n'a plus le miroir ; `page: None` si la page n'a pas de
    copie (ou si `page_id` est absent : contrôle de la machine seul, pour l'entrée `/_auth`)."""
    from nokker.tenancy.context import tenant_scope

    m = links.machine_by_host(host)
    if m is None or m.revoked or m.tenant != tenant or (machine and machine != m.id):
        return None
    if mirror_allowed is None or not mirror_allowed(m.account):
        return None
    page = None
    if page_id:
        if not is_page_id(page_id):
            return None
        with tenant_scope(tenant):
            r = store.get(tenant, m.id, page_id)
        if r is not None:
            page = {"title": r["title"], "version": r["version"], "sha256": r["sha256"],
                    "mirrored_at": r["mirrored_at"], "banner": banner_text(r["mirrored_at"]),
                    "html_b64": base64.b64encode(with_banner(bytes(r["html"]), r["mirrored_at"])).decode()}
    return {"machine_id": m.id, "name": m.name, "page": page}


def public_copy_for_relay(store: Any, links: Any, mirror_allowed: Any, public_allowed: Any, *, host: str,
                          public_id: str, moderation: Any = None) -> dict[str, Any] | None:
    """Copie PUBLIQUE pour `<nom>--pub.nokker.app/<public_id>`, machine éteinte (§7.26) : `host` = machine active, compte
    toujours abonné au miroir ET autorisé à publier, copie de cette machine dont l'id public a cette empreinte. La page
    revient avec son bandeau ; None sinon (le relais répond « machine hors ligne », rien de plus). `moderation` (registre
    de nokker/cloud/moderation.py) : la vue y est inscrite, et une page retirée rend `{moderated: True}` (410)."""
    from nokker.tenancy.context import tenant_scope

    if not _PUBLIC_ID.match(public_id or ""):
        return None
    m = links.machine_by_host(host)
    if m is None or m.revoked:
        return None
    if mirror_allowed is None or public_allowed is None or not mirror_allowed(m.account) \
            or not public_allowed(m.account):
        return None
    with tenant_scope(m.tenant):
        r = store.get_public(m.tenant, m.id, hashlib.sha256(public_id.encode()).hexdigest())
    if r is None:
        return None
    if moderation is not None:
        import time

        if moderation.view(m.id, m.tenant, hashlib.sha256(public_id.encode()).hexdigest(), r.get("page_id"),
                           int(r.get("size") or 0), time.time()):
            return {"moderated": True}
    return {"page": {"title": r["title"], "version": r["version"], "mirrored_at": r["mirrored_at"],
                     "index": bool(r.get("public_index")), "banner": banner_text(r["mirrored_at"]),
                     "html_b64": base64.b64encode(with_banner(bytes(r["html"]), r["mirrored_at"])).decode()}}


class MemoryUsedJti:
    """Jti des jetons de pages déjà présentés au relais (toutes répliques) ; `PgUsedJti` (nokker/cloud/pg.py) a la
    même interface. Empreintes seulement, oubliées après expiration."""

    def __init__(self) -> None:
        self.seen: dict[str, float] = {}
        self.lock = threading.Lock()

    def first_use(self, jti_sha256: str, expires_at: float, now: float) -> bool:
        with self.lock:
            if len(self.seen) > 10_000:
                self.seen = {k: e for k, e in self.seen.items() if e > now}
            e = self.seen.get(jti_sha256)
            if e is not None and e > now:
                return False
            self.seen[jti_sha256] = expires_at
            return True


def use_jti(store: Any, body: Any, now: float) -> tuple[int, dict[str, Any]]:
    """`POST /v1/gateway/pages-jti {jti, exp}` (clé du relais) : 200 `{first: true}` au premier usage, 409 ensuite ;
    garde l'empreinte jusqu'à `exp` + 60 s (au plus 10 min : un jeton de pages vit 60 s)."""
    body = body if isinstance(body, dict) else {}
    jti = body.get("jti")
    try:
        exp = float(body.get("exp"))
    except (TypeError, ValueError):
        return _err(400, "bad_request", "{jti, exp} requis")
    if not isinstance(jti, str) or not jti or len(jti) > MAX_JTI:
        return _err(400, "bad_request", "{jti, exp} requis")
    keep = min(exp + 60, now + 600)
    if store.first_use(hashlib.sha256(jti.encode()).hexdigest(), keep, now):
        return 200, {"first": True}
    return 409, {"first": False}


__all__ = ["DEFAULT_QUOTA_BYTES", "MemoryUsedJti", "public_copy_for_relay", "use_jti", "MAX_PAGE_BYTES", "MAX_PUT_BYTES", "MemoryMirrorPages", "QuotaExceeded",
           "banner_text", "copy_for_relay", "is_page_id", "push_page", "with_banner"]
