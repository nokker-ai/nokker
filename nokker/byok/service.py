"""BYOK cloud (docs/CLOUD-BYOK.md) : un locataire de nokker.ai branche sa propre inférence compatible OpenAI au lieu du
Router. Réservé aux formules qui l'incluent (`byok_entitled`). Même forme que le PUT local (nokker/local/setup.py),
avec ce que le multi-locataire impose :

- **Clé** : au coffre du noyau (portée `system`, nom `byok.<tn_…>.<nom>`, `ByokSecrets`), jamais dans la base, jamais
  rendue par l'API, jamais journalisée, jamais publiée sur le bus. Lue au moment de construire le fournisseur du tour.
- **Adresse** : https, toutes les IP du nom publiques (`nokker/byok/guard.py`), vérifiée à l'écriture, au test, et de
  nouveau à CHAQUE connexion.
- **Réglages** : `tenant_byok` sous RLS (migration 066), lus et écrits dans le `tenant_scope` de l'appelant.
- **Tour** : `route_for_turn()` rend le fournisseur du locataire courant quand son défaut est `byok:<nom>` ET qu'il y a
  droit ; sinon None (le Router, comme avant).
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from nokker.byok.context import ByokRoute
from nokker.byok.guard import BlockedEndpoint, check_endpoint, guarded_client
from nokker.byok.store import ByokSettings, ByokStore
from nokker.providers.sokkan import SokkanProvider
from nokker.secrets.base import SecretsError
from nokker.tenancy.context import INTERNAL_TENANT, current_tenant, current_tenant_or_none

logger = logging.getLogger("nokker.byok")

# Formules qui incluent le BYOK (décision du 05.11.2026 : la formule « byok » à 9 CHF/mois, et Solo). Réglable par
# `cloud_byok.plans` (config), pas en code.
DEFAULT_BYOK_PLANS: tuple[str, ...] = ("byok", "solo", "famille", "large")
# Formules SANS inférence Nokker : la formule « byok » n'inclut aucun tour sur le Router (`cloud_byok.byok_only_plans`).
DEFAULT_BYOK_ONLY_PLANS: tuple[str, ...] = ("byok",)
ENTRY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
MAX_ENTRIES = 8
MAX_MODEL = 200
MAX_KEY = 4096
MAX_MODELS_LISTED = 200
TEST_TIMEOUT_S = 8.0
ROUTE_TTL_S = 30.0
KEY_MASK = "[clé]"


class ByokError(Exception):
    """Refus de l'API : `status` HTTP, `code` stable (lu par le client), `message` lisible."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def body(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message}}


# ─────────────────────────────────────── droit d'usage ───────────────────────────────────────


def byok_entitled(tenant_id: str, *, billing: Any, plans: Iterable[str] = DEFAULT_BYOK_PLANS,
                  open_to_all: bool = False) -> bool:
    """Le locataire a-t-il une formule qui inclut le BYOK ? Lu dans la facturation du noyau : `Billing.active_plan`
    (nokker/billing/service.py, abonnement actif, `past_due` compris, cache de 30 s vidé par le webhook Stripe). Sans
    facturation configurée (`billing_enabled` éteint) : non, sauf `open_to_all` (dev, banc). Le locataire interne : non
    (ses paliers sont ceux de la config)."""
    if not tenant_id or tenant_id == INTERNAL_TENANT:
        return False
    if open_to_all:
        return True
    if billing is None:
        return False
    try:
        plan = billing.active_plan(tenant_id)
    except Exception:  # noqa: BLE001 — facturation injoignable : pas de droit (le Router reste servi)
        logger.warning("byok : palier illisible pour %s, droit refusé", tenant_id, exc_info=True)
        return False
    return plan is not None and plan in set(plans)


def byok_only_plan(tenant_id: str, *, billing: Any, plans: Iterable[str] = DEFAULT_BYOK_ONLY_PLANS) -> bool:
    """La formule active du locataire est-elle SANS inférence Nokker (formule `byok`) ? Sans facturation : non.
    Facturation injoignable : l'exception remonte (`CloudByok._byok_only` la traite comme « sans Router »)."""
    if not tenant_id or tenant_id == INTERNAL_TENANT or billing is None:
        return False
    plan = billing.active_plan(tenant_id)
    return plan is not None and plan in set(plans)


# ─────────────────────────────────────── coffre ───────────────────────────────────────


def secret_name(tenant: str, name: str) -> str:
    """Nom au coffre, à portée de locataire : deux locataires qui nomment leur entrée pareil ne se voient jamais."""
    return f"byok.{tenant}.{name}"


class ByokSecrets:
    """Sur un `SecretsProvider` de portée `system` (fichier chiffré ou OpenBao, nokker/secrets/), comme les secrets
    des clients SSO (nokker/sso/secrets.py). `provider=None` : en mémoire (tests, dev)."""

    def __init__(self, provider: Any = None) -> None:
        self.provider = provider
        self._mem: dict[str, str] = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:  # jamais les valeurs
        return f"ByokSecrets({getattr(self.provider, 'kind', 'memory')})"

    def get(self, tenant: str, name: str) -> str | None:
        if self.provider is not None:
            return self.provider.get(secret_name(tenant, name))
        with self._lock:
            return self._mem.get(secret_name(tenant, name))

    def set(self, tenant: str, name: str, value: str) -> None:
        if self.provider is not None:
            self.provider.set(secret_name(tenant, name), value)
            return
        with self._lock:
            self._mem[secret_name(tenant, name)] = value

    def delete(self, tenant: str, name: str) -> None:
        if self.provider is not None:
            self.provider.delete(secret_name(tenant, name))
            return
        with self._lock:
            self._mem.pop(secret_name(tenant, name), None)


# ─────────────────────────────────────── fournisseur du tour ───────────────────────────────────────


class ByokProvider(SokkanProvider):
    """`SokkanProvider` vers le point d'accès d'un client. La clé n'apparaît dans aucun message d'erreur qui remonte
    (un amont peut recopier l'en-tête `Authorization` dans sa réponse d'erreur)."""

    def __repr__(self) -> str:  # jamais la clé (le repr de la dataclass la montrerait)
        return f"ByokProvider({self.base_url!r}, {self.model!r})"

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, reasoning: bool,
                 session_key: str, max_tokens: int = 2048,
                 on_delta: Callable[[str], None] | None = None,
                 on_tool_delta: Callable[[int, str, str, str], None] | None = None):
        try:
            return super().complete(messages, tools, reasoning=reasoning, session_key=session_key,
                                    max_tokens=max_tokens, on_delta=on_delta, on_tool_delta=on_tool_delta)
        except Exception as e:
            if self.api_key and self.api_key in str(e):
                raise RuntimeError(str(e).replace(self.api_key, KEY_MASK)) from None
            raise


class _Unavailable:
    """Défaut `byok:<nom>` qu'on ne peut pas servir (coffre indisponible) : chaque appel échoue avec un motif lisible,
    le tour finit sur l'erreur du point d'accès (jamais un repli vers le Router)."""

    vision = False
    vision_probe = None
    max_context_tokens = 0

    def __init__(self, model: str, why: str) -> None:
        self.model, self._why = model, why

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        raise RuntimeError(self._why)


# ─────────────────────────────────────── service ───────────────────────────────────────


@dataclass
class CloudByok:
    store: ByokStore
    secrets: ByokSecrets
    entitled: Callable[[str], bool]
    # Formule active SANS inférence Nokker (formule `byok`, 9 CHF) : le Router n'est jamais servi à ce locataire.
    byok_only: Callable[[str], bool] = lambda tid: False
    resolver: Any = None  # résolveur DNS (None = celui du système) ; injecté par les tests
    # Tests seulement : transport httpx à la place du client gardé (faux point d'accès en mémoire).
    transport_factory: Callable[[str], httpx.BaseTransport] | None = None
    test_timeout_s: float = TEST_TIMEOUT_S
    route_ttl_s: float = ROUTE_TTL_S
    timeout_s: float = 120.0  # délai de lecture d'un appel modèle vers le point d'accès du client
    clock: Callable[[], float] = time.monotonic
    _routes: dict[str, tuple[float, ByokRoute | None]] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    # ── clients HTTP ──────────────────────────────────────────────────────────────────────────────────────────────
    def _client(self, base_url: str, timeout: httpx.Timeout | float) -> httpx.Client:
        if self.transport_factory is not None:
            return httpx.Client(transport=self.transport_factory(base_url), timeout=timeout, follow_redirects=False,
                                trust_env=False)
        return guarded_client(base_url, timeout, resolver=self.resolver)

    def _probe(self, base_url: str, api_key: str | None) -> dict[str, Any]:
        """`GET <base_url>/models` par le client gardé. Jamais la clé ni le corps de la réponse dans le résultat."""
        t0 = time.monotonic()
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            with self._client(base_url, self.test_timeout_s) as client:
                r = client.get(base_url + "/models", headers=headers)
        except httpx.HTTPError as e:
            msg = str(e)
            reason = msg if msg.startswith(("adresse refusée", "connexion refusée")) else type(e).__name__
            return {"ok": False, "models": [], "latency_ms": None, "error": reason}
        ms = int((time.monotonic() - t0) * 1000)
        if 300 <= r.status_code < 400:
            return {"ok": False, "models": [], "latency_ms": ms, "error": f"redirection refusée (HTTP {r.status_code})"}
        if r.status_code != 200:
            return {"ok": False, "models": [], "latency_ms": ms, "error": f"HTTP {r.status_code}"}
        try:
            data = r.json().get("data") or []
            models = sorted({str(m.get("id"))[:MAX_MODEL] for m in data if isinstance(m, dict) and m.get("id")})
        except (ValueError, AttributeError):
            models = []
        return {"ok": True, "models": models[:MAX_MODELS_LISTED], "latency_ms": ms}

    # ── droit ─────────────────────────────────────────────────────────────────────────────────────────────────────
    def _byok_only(self, tid: str) -> bool:
        try:
            return bool(self.byok_only(tid))
        except Exception:  # noqa: BLE001 — facturation illisible : traité comme une formule sans Router (prudence)
            logger.warning("byok : formule illisible pour %s", tid, exc_info=True)
            return True

    def _require_entitled(self, tid: str) -> None:
        if not self.entitled(tid):
            raise ByokError(403, "plan_required", "ta formule n'inclut pas l'inférence personnelle (BYOK)")

    # ── GET ───────────────────────────────────────────────────────────────────────────────────────────────────────
    def view(self) -> dict[str, Any]:
        tid = current_tenant()
        s = self.store.get()
        entitled = self.entitled(tid)
        out = []
        for e in s.entries:
            try:
                has_key = self.secrets.get(tid, e["name"]) is not None
            except SecretsError:
                has_key = None  # coffre indisponible : inconnu, jamais une erreur qui porte la clé
            item = {"name": e["name"], "base_url": e["base_url"], "model": e.get("model"), "has_key": has_key,
                    "ok": e.get("ok")}
            if e.get("models"):
                item["models"] = list(e["models"])
            out.append(item)
        only = self._byok_only(tid)
        effective = s.default if entitled and s.default.startswith("byok:") else ("none" if only else "router")
        if only and effective.startswith("byok:"):  # sans Router : le défaut doit être utilisable (modèle ET clé)
            chosen = next((e for e in out if e["name"] == effective[5:]), None)
            if chosen is None or not chosen["model"] or chosen["has_key"] is False:
                effective = "none"
        return {"default": s.default, "effective": effective, "byok": out, "entitled": entitled,
                "router_included": not only}

    # ── PUT ───────────────────────────────────────────────────────────────────────────────────────────────────────
    def put(self, body: Any) -> dict[str, Any]:
        """Enregistre les points d'accès (clé au coffre, jamais dans la base) et le défaut. Tout est validé avant
        d'écrire : un refus (400) ne touche ni la base ni le coffre."""
        tid = current_tenant()
        self._require_entitled(tid)
        if not isinstance(body, dict):
            raise ByokError(400, "bad_request", "corps JSON objet attendu")
        cur = self.store.get()
        entries = cur.entries
        keys: dict[str, str] = {}
        drop_keys: set[str] = set()
        if "byok" in body:
            raw = body.get("byok")
            if raw is None:
                raw = []
            if not isinstance(raw, list) or len(raw) > MAX_ENTRIES:
                raise ByokError(400, "bad_byok", f"byok : liste de {MAX_ENTRIES} entrées au plus")
            entries, seen = [], set()
            for b in raw:
                if not isinstance(b, dict):
                    raise ByokError(400, "bad_byok", "byok : objets {name, base_url, model?, api_key?}")
                name = str(b.get("name") or "").strip()
                if not ENTRY_NAME.fullmatch(name):
                    raise ByokError(400, "bad_name", "byok : name = lettres, chiffres, « _ », « . », « - » (32 au plus)")
                if name in seen:
                    raise ByokError(400, "bad_name", f"byok : nom en double « {name} »")
                seen.add(name)
                try:
                    base = check_endpoint(b.get("base_url"), resolver=self.resolver)
                except BlockedEndpoint as e:
                    raise ByokError(400, e.code, f"byok « {name} » : {e}") from None
                model = b.get("model")
                if model is not None and not isinstance(model, str):
                    raise ByokError(400, "bad_model", "byok : model = chaîne")
                model = model.strip()[:MAX_MODEL] if isinstance(model, str) and model.strip() else None
                if b.get("api_key") not in (None, ""):
                    keys[name] = _clean_key(b["api_key"])
                prev = cur.entry(name)
                if name not in keys and prev is not None and _origin(prev["base_url"]) != _origin(base):
                    # Hôte changé sans clé : la clé enregistrée ne part pas vers une autre adresse que celle où elle a
                    # été donnée ; elle est retirée (à redonner).
                    drop_keys.add(name)
                if name in keys:
                    key = keys[name]
                elif prev is not None and name not in drop_keys:
                    key = self._secret(tid, name)
                else:
                    key = None
                if prev is not None and prev["base_url"] == base and name not in keys and prev.get("ok") is not None:
                    ok, models = prev["ok"], prev.get("models") or []
                else:  # nouveau point d'accès, ou adresse ou clé changée : testé ici (jamais la clé renvoyée)
                    t = self._probe(base, key)
                    ok, models = bool(t.get("ok")), list(t.get("models") or [])
                entry: dict[str, Any] = {"name": name, "base_url": base, "model": model, "ok": ok}
                if models:
                    entry["models"] = models
                entries.append(entry)
        by_name = {e["name"]: e for e in entries}
        default = cur.default
        if "default" in body:
            if not isinstance(body["default"], str):
                raise ByokError(400, "bad_default", "default : router | byok:<nom>")
            default = body["default"].strip()
            if default != "router" and not default.startswith("byok:"):
                raise ByokError(400, "bad_default", "default : router | byok:<nom>")
        if default.startswith("byok:"):
            entry = by_name.get(default[5:])
            if entry is None:
                if "default" in body:
                    raise ByokError(400, "bad_default", f"default : aucune entrée byok « {default[5:]} »")
                default = "router"  # l'entrée par défaut a été retirée : retour au Router
            elif not entry.get("model"):
                if "default" in body:
                    raise ByokError(400, "bad_default", f"default : l'entrée byok « {default[5:]} » n'a pas de modèle")
                default = "router"
        # Écriture : coffre d'abord (une clé sans ligne est inerte ; une ligne sans clé dirait has_key faux).
        try:
            for name, key in keys.items():
                self.secrets.set(tid, name, key)
            for name in drop_keys:
                self.secrets.delete(tid, name)
            for old in cur.entries:  # une entrée retirée de la liste emporte sa clé du coffre
                if old["name"] not in by_name:
                    self.secrets.delete(tid, old["name"])
        except SecretsError as e:
            logger.warning("byok : coffre indisponible pour %s (%s)", tid, e.code)
            raise ByokError(503, "vault_unavailable", "coffre des clés indisponible, réessaie plus tard") from None
        self.store.put(ByokSettings(default=default, entries=entries))
        self.forget(tid)
        logger.info("byok : réglages de %s enregistrés (%d entrée(s), défaut %s)", tid, len(entries), default)
        return self.view()

    # ── test ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def test(self, body: Any) -> dict[str, Any]:
        """`{base_url, api_key?, model?}` → `{ok, models, latency_ms, error?, model_listed?}`. Sans `api_key` : la clé
        enregistrée d'une entrée de MÊME adresse, s'il y en a une (re-tester sans redonner la clé)."""
        tid = current_tenant()
        self._require_entitled(tid)
        if not isinstance(body, dict):
            raise ByokError(400, "bad_request", "corps JSON objet attendu")
        try:
            base = check_endpoint(body.get("base_url"), resolver=self.resolver)
        except BlockedEndpoint as e:
            raise ByokError(400, e.code, str(e)) from None
        if body.get("api_key") not in (None, ""):
            key: str | None = _clean_key(body["api_key"])
        else:
            prev = next((e for e in self.store.get().entries if e["base_url"] == base), None)
            key = self._secret(tid, prev["name"]) if prev is not None else None
        out = self._probe(base, key)
        model = body.get("model")
        if isinstance(model, str) and model.strip():
            out["model_listed"] = model.strip() in out["models"]
        return out

    def _secret(self, tid: str, name: str) -> str | None:
        try:
            return self.secrets.get(tid, name)
        except SecretsError as e:
            logger.warning("byok : coffre indisponible pour %s (%s)", tid, e.code)
            raise ByokError(503, "vault_unavailable", "coffre des clés indisponible, réessaie plus tard") from None

    # ── tour ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def forget(self, tid: str) -> None:
        with self._lock:
            self._routes.pop(tid, None)

    def route_for_turn(self) -> ByokRoute | None:
        """Le fournisseur du tour pour le locataire COURANT, ou None (Router). Gardé `route_ttl_s` secondes par
        locataire ; vidé par un PUT du locataire."""
        tid = current_tenant_or_none()
        if not tid or tid == INTERNAL_TENANT:
            return None
        now = self.clock()
        with self._lock:
            hit = self._routes.get(tid)
            if hit is not None and now - hit[0] < self.route_ttl_s:
                return hit[1]
        route = self._build_route(tid)
        with self._lock:
            self._routes[tid] = (now, route)
        return route

    def _build_route(self, tid: str) -> ByokRoute | None:
        # Formule sans inférence Nokker (`byok`) : sans point d'accès utilisable, le tour est REFUSÉ (jamais le Router).
        only = self._byok_only(tid)

        def blocked(why: str) -> ByokRoute | None:
            return ByokRoute(name="", provider=None, blocked=why) if only else None

        try:
            s = self.store.get()
        except Exception:  # noqa: BLE001 — base injoignable : le tour garde le Router (il échouera de toute façon)
            logger.warning("byok : réglages illisibles pour %s", tid, exc_info=True)
            return blocked("réglages illisibles")
        if not s.default.startswith("byok:") or not self.entitled(tid):
            return blocked("aucun modèle byok par défaut")
        name = s.default[5:]
        entry = s.entry(name)
        if entry is None or not entry.get("model"):
            return blocked(f"entrée byok « {name} » absente ou sans modèle")
        try:
            key = self.secrets.get(tid, name) or ""
        except SecretsError as e:
            logger.warning("byok : coffre indisponible pour %s (%s)", tid, e.code)
            return ByokRoute(name=name, provider=_Unavailable(entry["model"], "coffre des clés indisponible"))
        if not key and only:
            return blocked(f"entrée byok « {name} » sans clé")
        base = entry["base_url"]
        provider = ByokProvider(
            base_url=base, api_key=key, model=entry["model"],
            # Un amont tiers ne connaît pas forcément `reasoning_effort` (refusé par plusieurs API) : rien n'est ajouté.
            reasoning_off_body={}, reasoning_on_body={}, session_header=False,
            timeout_s=self.timeout_s, call_budget_s=self.timeout_s, max_retries=1,
            client_factory=lambda timeout, _base=base: self._client(_base, timeout))
        provider.max_context_tokens = 0
        return ByokRoute(name=name, provider=provider)


def _origin(url: str) -> tuple[str, int]:
    p = urlsplit(url)
    return (p.hostname or "").lower(), p.port or 443


def _clean_key(raw: Any) -> str:
    """Une clé d'API : chaîne imprimable sans espace (elle part dans un en-tête HTTP), bornée. Le message d'erreur ne
    la recopie jamais."""
    if not isinstance(raw, str):
        raise ByokError(400, "bad_api_key", "api_key : chaîne attendue")
    key = raw.strip()
    if not key or len(key) > MAX_KEY or any(ord(c) < 0x21 or ord(c) > 0x7E for c in key):
        raise ByokError(400, "bad_api_key", f"api_key : caractères ASCII visibles, sans espace ({MAX_KEY} au plus)")
    return key


__all__ = ["DEFAULT_BYOK_ONLY_PLANS", "DEFAULT_BYOK_PLANS", "ByokError", "ByokProvider", "ByokSecrets", "CloudByok", "byok_entitled",
           "byok_only_plan", "secret_name"]
