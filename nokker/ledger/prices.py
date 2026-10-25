"""Grille de prix par modèle (F6, PLAN-P1 §5.1 socle).

Source préférée : le catalogue public du Router (`GET {router_url}/v1/models`). Vérifié le
17.10.2026 en lisant `broker/app.py` du Router (route `models()`) (route `models()`) : CHAQUE entrée
porte bien `pricing.{prompt,cached,completion}` en USD par million de tokens, sans authentification
(c'est le catalogue de vente, pas une route protégée). Repli : `prices.yaml` à côté de ce fichier,
versionné, chaque prix daté et sourcé (lecture de code du 17.10.2026, pas un appel réseau).

Taux de change : le catalogue Router utilise 1 CHF = 1,25 USD (confirmé dans le `_doc` de
`catalog.json`). Configurable ici via `chf_usd_rate`.

Devise par entrée (X2, coût par tour, 25.10.2026) : le catalogue est en USD (celui du Router), mais un prix lu
ailleurs garde SA devise (`currency`, ex. EUR pour OVH AI Endpoints en direct) plutôt qu'un taux de change inventé.
`cost()` rend `(montant, devise)` dans la devise de l'entrée ; `cost_usd`/`cost_chf` ne convertissent que ce qu'ils
savent convertir (USD → CHF au taux du catalogue) et rendent None sinon.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover — yaml est une dépendance du paquet, garde-fou de message clair
    yaml = None  # type: ignore[assignment]

DEFAULT_YAML_PATH = Path(__file__).parent / "prices.yaml"
DEFAULT_ROUTER_URL = "https://router.sokkan.ch"
DEFAULT_CHF_USD_RATE = 1.25  # 1 CHF = 1,25 USD (catalogue Router, 17.10.2026)


@dataclass(frozen=True)
class PriceEntry:
    """Un prix par modèle, USD/M. `source` dit d'où il vient (traçabilité — règle `nokker` §1)."""

    model: str
    prompt_usd_per_m: float  # entrée NON cachée
    cached_usd_per_m: float  # entrée cachée (lecture de cache)
    completion_usd_per_m: float  # sortie
    source: str
    # Devise des trois prix ci-dessus (les noms de champs disent USD pour l'historique : c'est la devise du catalogue
    # Router). Une entrée EUR est servie telle quelle par `PriceCatalog.cost`, jamais convertie sans taux connu.
    currency: str = "USD"


class PriceCatalog:
    """Grille chargée (Router ou fichier). `cost_usd`/`cost_chf` : None si le modèle est inconnu."""

    def __init__(self, entries: dict[str, PriceEntry], *, chf_usd_rate: float = DEFAULT_CHF_USD_RATE,
                 source: str = "") -> None:
        self._entries = entries
        self.chf_usd_rate = chf_usd_rate
        self.source = source

    def get(self, model: str) -> PriceEntry | None:
        return self._entries.get(model)

    def models(self) -> list[str]:
        return sorted(self._entries)

    def cost(self, model: str, new_tokens: int, cached_tokens: int,
             completion_tokens: int) -> tuple[float, str] | None:
        """(montant, devise) dans la devise de l'entrée ; None si le modèle est inconnu (jamais un prix inventé)."""
        entry = self.get(model)
        if entry is None:
            return None
        amount = (new_tokens * entry.prompt_usd_per_m + cached_tokens * entry.cached_usd_per_m +
                  completion_tokens * entry.completion_usd_per_m) / 1_000_000
        return amount, entry.currency

    def cost_usd(self, model: str, new_tokens: int, cached_tokens: int, completion_tokens: int) -> float | None:
        got = self.cost(model, new_tokens, cached_tokens, completion_tokens)
        return got[0] if got is not None and got[1] == "USD" else None

    def cost_chf(self, model: str, new_tokens: int, cached_tokens: int, completion_tokens: int) -> float | None:
        got = self.cost(model, new_tokens, cached_tokens, completion_tokens)
        if got is None:
            return None
        amount, currency = got
        if currency == "CHF":
            return amount
        return self.usd_to_chf(amount) if currency == "USD" else None  # EUR → CHF : pas de taux dans le catalogue

    def usd_to_chf(self, usd: float) -> float:
        return usd / self.chf_usd_rate


def _entries_from_models(models: list[dict[str, Any]], pricing_key: str, source_fmt: str) -> dict[str, PriceEntry]:
    entries: dict[str, PriceEntry] = {}
    for m in models:
        pricing = m.get(pricing_key) or {}
        if not all(k in pricing for k in ("prompt", "cached", "completion")):
            continue
        model_id = m["id"]
        entries[model_id] = PriceEntry(
            model=model_id,
            prompt_usd_per_m=float(pricing["prompt"]),
            cached_usd_per_m=float(pricing["cached"]),
            completion_usd_per_m=float(pricing["completion"]),
            source=source_fmt.format(model=model_id),
        )
    return entries


def load_from_yaml(path: Path | str = DEFAULT_YAML_PATH) -> PriceCatalog:
    if yaml is None:
        raise RuntimeError("pyyaml n'est pas installé — ajouter 'pyyaml' aux dépendances de nokker")
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rate = float(data.get("rate_chf_usd", DEFAULT_CHF_USD_RATE))
    fetched_at = data.get("fetched_at", "?")
    default_currency = str(data.get("currency") or "USD").upper()
    # Le format YAML porte prompt/cached/completion directement sur l'item (pas sous une sous-clé
    # « pricing » comme la réponse du Router) : construit à la main plutôt que de forcer
    # `_entries_from_models` (réservée au format Router) sur un format différent.
    entries: dict[str, PriceEntry] = {}
    for m in data.get("models", []):
        try:
            entries[m["id"]] = PriceEntry(
                model=m["id"],
                prompt_usd_per_m=float(m["prompt"]),
                cached_usd_per_m=float(m["cached"]),
                completion_usd_per_m=float(m["completion"]),
                source=f"yaml:{path.name}@{m.get('fetched_at') or fetched_at}",
                currency=str(m.get("currency") or default_currency).upper(),
            )
        except (KeyError, ValueError, TypeError) as e:
            raise ValueError(f"prices.yaml: entrée invalide pour {m!r}: {e}") from e
    return PriceCatalog(entries, chf_usd_rate=rate, source=f"yaml:{path}")


def load_from_router(base_url: str = DEFAULT_ROUTER_URL, *, timeout: float = 5.0,
                      chf_usd_rate: float = DEFAULT_CHF_USD_RATE) -> PriceCatalog:
    """`GET {base_url}/v1/models` — expose `pricing.{prompt,cached,completion}` en USD/M, sans clé
    (vérifié dans le code du broker, voir le docstring du module). Lève en cas d'échec réseau/format ;
    c'est `load()` qui décide du repli, jamais cette fonction (pas de silence sur une panne réseau)."""
    url = base_url.rstrip("/") + "/v1/models"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — URL fixe du Router, jamais une entrée utilisateur
        payload = json.load(resp)
    models = payload.get("data", [])
    entries = _entries_from_models(models, "pricing", "router:" + base_url + ":{model}")
    if not entries:
        raise ValueError(f"{url} n'a renvoyé aucun prix exploitable (format inattendu)")
    return PriceCatalog(entries, chf_usd_rate=chf_usd_rate, source=f"router:{base_url}")


def load(*, prefer_router: bool = False, router_url: str = DEFAULT_ROUTER_URL,
          yaml_path: Path | str = DEFAULT_YAML_PATH, chf_usd_rate: float = DEFAULT_CHF_USD_RATE,
          timeout: float = 5.0) -> PriceCatalog:
    """Point d'entrée recommandé. `prefer_router=True` (prod) tente le Router, repli silencieux sur le
    fichier versionné en cas d'échec réseau/format — jamais de panne du ledger pour un prix. En dev/tests,
    laisser `prefer_router=False` (défaut) : déterministe, aucun appel réseau."""
    if prefer_router:
        try:
            return load_from_router(router_url, timeout=timeout, chf_usd_rate=chf_usd_rate)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            pass
    return load_from_yaml(yaml_path)


__all__ = [
    "DEFAULT_CHF_USD_RATE", "DEFAULT_ROUTER_URL", "DEFAULT_YAML_PATH", "PriceCatalog", "PriceEntry", "load",
    "load_from_router", "load_from_yaml",
]
