"""nokker/tools/web.py — recherche web « sans clé » (P1, décision Nick 18.10.2026).

Contexte : pas de moteur payant (coût incontrôlable à l'échelle), pas d'index maison (trop long,
trop complexe). On reprend les mêmes offres gratuites et publiques que le mode « sans clé »
d'Hermes (`hermes-nokker/plugins/web/keyless_mcp.py` + `plugins/web/_common.py`, lu en entier avant
d'écrire ce module) : Exa (MCP `https://mcp.exa.ai/mcp`), Parallel (MCP
`https://search.parallel.ai/mcp`), Firecrawl (cloud public `https://api.firecrawl.dev`, sans en-tête
d'auth — `plugins/web/firecrawl/provider.py::_KeylessFirecrawlClient`) et Keenable
(`https://api.keenable.ai/v1/search/public`, en-tête `X-Keenable-Title` obligatoire, pas de clé).

**Choix d'implémentation : réimplémentation minimale en `httpx` (déjà une dépendance nokker,
`providers/sokkan.py`), PAS une enveloppe du module Hermes.** Justification : l'anneau d'Hermes
(`search_with_failover`) est construit pour un repli SÉQUENTIEL — un moteur après l'autre, le
suivant seulement si le précédent est throttled — câblé sur les chokepoints internes d'Hermes
(`hermes_cli.config`, `tools.web_tools`, `pm.ensure_import`, `tools.interrupt`, son propre anneau
round-robin par processus). Le contrat demandé ici est différent par nature : les QUATRE moteurs
lancés EN PARALLÈLE sous un budget global commun, fusion par consensus (une URL vue par plusieurs
moteurs monte), disjoncteur et cache PROPRES à nokker, garde PII avant tout envoi. Envelopper
l'anneau aurait forcé soit à l'appeler 4× en séquence (contraire au budget global — on perdrait la
propriété « ce qui est arrivé dans le budget, rien de plus »), soit à ouvrir ses fonctions internes
par moteur un par un (perdant l'intérêt de « dépendance épinglée, pas un fork » : on deviendrait
couplé à sa config et à ses effets de bord au lieu de rester derrière NOTRE port `ToolSource`).

Le code ci-dessous est écrit à neuf : mêmes URLs/formats de requête (ce sont des faits d'API, pas une
œuvre), logique de fan-out/fusion/disjoncteur/cache propre à nokker. Rien n'est copié texte pour
texte depuis `keyless_mcp.py`/`_common.py`.

**Licence** : hermes-nokker est Apache-2.0 (`LICENSE` de hermes-nokker) ; nokker aussi
(`pyproject.toml`, `license = {text = "Apache-2.0"}`) — licences identiques, donc de toute façon
compatibles même si une ligne de code s'en était trouvée proche par construction (mêmes contraintes
d'API). Voir `docs/WEB-SEARCH.md` pour le format de résultat et les limites (éditeurs US, paliers
gratuits, garde PII).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult

logger = logging.getLogger("nokker.tools.web")

EXA_MCP_URL = "https://mcp.exa.ai/mcp"
PARALLEL_MCP_URL = "https://search.parallel.ai/mcp"
FIRECRAWL_API_URL = "https://api.firecrawl.dev"
KEENABLE_API_URL = "https://api.keenable.ai"
_KEENABLE_TITLE = "nokker"  # X-Keenable-Title : identifie l'appli appelante, jamais un utilisateur

# Parallel (free tier) : corrélation anti-abus demandée par le vendeur, aléatoire par PROCESSUS,
# jamais persistée ni journalisée — même soin que l'anneau Hermes (_common.py en-tête).
_SESSION_ID = uuid.uuid4().hex

_ALL_ENGINES = ("exa", "parallel", "firecrawl", "keenable")
_TRACKING_PREFIX = "utm_"


class WebEngineError(RuntimeError):
    """Un appel moteur a échoué (transport, HTTP, JSON-RPC, ou réponse "isError")."""


# ─────────────────────────────── garde PII ───────────────────────────────
#
# Règle Nick : rien d'identifiable chez des éditeurs US (Exa, Parallel, Firecrawl ; Keenable aussi,
# tous hors de notre contrôle). Vérifié AVANT tout envoi réseau — un refus ici ne part jamais.

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Téléphone suisse : international (+41/0041, puis 9 chiffres — l'indicatif national "0" est
# supprimé en forme internationale) ou national (0 + 9 chiffres = 10 chiffres), séparateurs
# espace/point/tiret optionnels entre chaque chiffre.
_PHONE_CH_RE = re.compile(
    r"(?:\+41|0041)[ .\-]?\(?0?\)?[ .\-]?[1-9](?:[ .\-]?\d){8}"
    r"|\b0[1-9](?:[ .\-]?\d){8}\b"
)

# AVS suisse : 756.XXXX.XXXX.XX (13 chiffres), avec ou sans points.
_AVS_RE = re.compile(r"\b756\.\d{4}\.\d{4}\.\d{2}\b|\b756\d{10}\b")

# IBAN CH : candidat "CH" + chiffres/espaces/tirets (le BBAN suisse est numérique — 5 chiffres de
# clearing + 12 de compte — contrairement à d'autres pays ; limiter la classe aux chiffres évite
# d'avaler le mot suivant la phrase). Validé en comparant la forme compactée (espaces/tirets
# retirés) au format exact IBAN CH : "CH" + 2 chiffres de contrôle + 17 chiffres = 21 caractères.
_IBAN_CANDIDATE_RE = re.compile(r"\bCH[\d .\-]{15,28}\b")
_IBAN_CH_EXACT_RE = re.compile(r"^CH\d{19}$")


def _has_ch_iban(text: str) -> bool:
    for m in _IBAN_CANDIDATE_RE.finditer(text):
        compact = re.sub(r"[ .\-]", "", m.group(0))
        if _IBAN_CH_EXACT_RE.match(compact):
            return True
    return False


def pii_violation(text: str, blocked_terms: list[str] | None = None) -> str | None:
    """Renvoie une étiquette courte si *text* porte une donnée personnelle ou un terme bloqué par
    config, sinon `None`. Ordre de détection fixe (pas de signification au-delà du premier trouvé)."""
    if _EMAIL_RE.search(text):
        return "adresse e-mail"
    if _PHONE_CH_RE.search(text):
        return "numéro de téléphone"
    if _has_ch_iban(text):
        return "IBAN"
    if _AVS_RE.search(text):
        return "numéro AVS"
    lowered = text.lower()
    for term in blocked_terms or []:
        term_l = term.strip().lower()
        if term_l and term_l in lowered:
            return f"terme bloqué : {term.strip()}"
    return None


# ─────────────────────────────── URL : normalisation pour le dédoublonnage ───────────────────────────────


def normalize_url(url: str) -> str:
    """Clé de dédoublonnage : schéma ignoré, `www.` retiré, slash final retiré, paramètres
    `utm_*` retirés (reste trié pour un ordre stable). Ne prétend pas à la canonicalisation RFC
    complète — juste assez pour fusionner les doublons évidents entre quatre moteurs."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/")
    query = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith(_TRACKING_PREFIX)
    )
    return urlunsplit(("http", host, path, urlencode(query), ""))


def _domain_of(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


# ─────────────────────────────── transport MCP (Exa, Parallel) ───────────────────────────────


def _mcp_call(client: httpx.Client, url: str, tool: str, arguments: dict[str, Any], timeout_s: float) -> str:
    """POST JSON-RPC `tools/call` et renvoie le texte de la réponse. Lève `WebEngineError` pour
    tout échec (transport, HTTP ≥ 400, erreur JSON-RPC, résultat `isError`)."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "User-Agent": "nokker"}
    try:
        response = client.post(url, json=payload, headers=headers, timeout=timeout_s)
    except httpx.HTTPError as exc:
        raise WebEngineError(f"requête échouée : {exc}") from exc
    if response.status_code >= 400:
        raise WebEngineError(f"HTTP {response.status_code}: {response.text.strip()[:300]}")
    return _parse_mcp_text(response.text)


def _parse_mcp_text(body: str) -> str:
    """Corps JSON-RPC brut OU trames SSE `data: {...}` (Exa) → premier contenu texte."""

    def _from_payload(payload: str) -> str | None:
        payload = payload.strip()
        if not payload.startswith("{"):
            return None
        data = json.loads(payload)
        err = data.get("error")
        if err:
            raise WebEngineError(str(err.get("message") or err))
        result = data.get("result") or {}
        texts = [c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict)]
        if result.get("isError"):
            raise WebEngineError(" ".join(t for t in texts if t) or "appel MCP en échec")
        return next((str(t) for t in texts if t), "")

    stripped = body.strip()
    candidates = [stripped] if stripped.startswith("{") else []
    candidates += [line[len("data: "):] for line in re.split(r"\r\n|\r|\n", body) if line.startswith("data: ")]
    seen_envelope = False
    for candidate in candidates:
        try:
            text = _from_payload(candidate)
        except json.JSONDecodeError:
            continue
        if text:
            return text
        seen_envelope = seen_envelope or text == ""
    raise WebEngineError("réponse MCP sans contenu" if seen_envelope else "forme de réponse MCP inconnue")


# ─────────────────────────────── moteurs : recherche ───────────────────────────────


def _after(line: str, prefix: str) -> str:
    return line[len(prefix):].strip()


_EXA_LABELS = ("Title:", "URL:", "Highlights:", "Published:", "Author:")


def _parse_exa_search_text(text: str, limit: int) -> list[dict[str, Any]]:
    """Blocs `---`-séparés `Title:/URL:/Highlights:/...` renvoyés par `web_search_exa`."""
    rows: list[dict[str, Any]] = []
    for block in text.split("\n---\n"):
        title = url = ""
        highlight_lines: list[str] = []
        in_highlights = False
        for stripped in map(str.strip, block.splitlines()):
            if stripped.startswith("Title:"):
                title = _after(stripped, "Title:")
            elif stripped.startswith("URL:"):
                url = _after(stripped, "URL:")
            elif in_highlights and stripped and not stripped.startswith(_EXA_LABELS):
                highlight_lines.append(stripped)
            if stripped.startswith(_EXA_LABELS):
                in_highlights = stripped.startswith("Highlights:")
        if url:
            rows.append({"url": url, "title": title, "snippet": " ".join(highlight_lines)})
        if limit and len(rows) >= limit:
            break
    return rows


def _exa_search(client: httpx.Client, query: str, limit: int, timeout_s: float) -> list[dict[str, Any]]:
    text = _mcp_call(client, EXA_MCP_URL, "web_search_exa", {"query": query, "numResults": max(1, limit)}, timeout_s)
    return _parse_exa_search_text(text, limit)


def _parallel_search(client: httpx.Client, query: str, limit: int, timeout_s: float) -> list[dict[str, Any]]:
    text = _mcp_call(client, PARALLEL_MCP_URL, "web_search",
                     {"objective": query, "search_queries": [query], "session_id": _SESSION_ID}, timeout_s)
    try:
        results = json.loads(text).get("results") or []
    except (json.JSONDecodeError, AttributeError) as exc:
        raise WebEngineError(f"réponse Parallel inattendue : {exc}") from exc
    return [
        {"url": r.get("url") or "", "title": r.get("title") or "", "snippet": " ".join(r.get("excerpts") or [])}
        for r in results[:limit]
    ]


def _firecrawl_search(client: httpx.Client, query: str, limit: int, timeout_s: float) -> list[dict[str, Any]]:
    try:
        response = client.post(f"{FIRECRAWL_API_URL}/v2/search", json={"query": query, "limit": limit},
                               headers={"Content-Type": "application/json"}, timeout=timeout_s)
    except httpx.HTTPError as exc:
        raise WebEngineError(f"requête échouée : {exc}") from exc
    if response.status_code >= 400:
        raise WebEngineError(f"HTTP {response.status_code}: {response.text.strip()[:300]}")
    data = response.json()
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise WebEngineError("réponse Firecrawl inattendue (pas de liste `data`)")
    return [
        {"url": r.get("url") or "", "title": r.get("title") or "", "snippet": r.get("description") or ""}
        for r in rows[:limit] if isinstance(r, dict)
    ]


def _keenable_request(client: httpx.Client, method: str, path: str, timeout_s: float, **kwargs: Any) -> dict[str, Any]:
    headers = {"X-Keenable-Title": _KEENABLE_TITLE}
    if method == "post":
        headers["Content-Type"] = "application/json"
    try:
        response = getattr(client, method)(f"{KEENABLE_API_URL}{path}", headers=headers, timeout=timeout_s, **kwargs)
    except httpx.HTTPError as exc:
        raise WebEngineError(f"requête échouée : {exc}") from exc
    if response.status_code >= 400:
        raise WebEngineError(f"HTTP {response.status_code}: {response.text.strip()[:300]}")
    return response.json()


def _keenable_search(client: httpx.Client, query: str, limit: int, timeout_s: float) -> list[dict[str, Any]]:
    data = _keenable_request(client, "post", "/v1/search/public", timeout_s,
                             json={"query": query, "max_results": max(1, limit)})
    rows = data.get("results") or []
    return [
        {"url": r.get("url") or "", "title": r.get("title") or "", "snippet": r.get("snippet") or r.get("description") or ""}
        for r in rows[:limit]
    ]


_ENGINE_SEARCHERS: dict[str, Callable[[httpx.Client, str, int, float], list[dict[str, Any]]]] = {
    "exa": _exa_search, "parallel": _parallel_search, "firecrawl": _firecrawl_search, "keenable": _keenable_search,
}


# ─────────────────────────────── moteurs : extraction (web_fetch) ───────────────────────────────


def _exa_extract(client: httpx.Client, url: str, timeout_s: float) -> dict[str, Any]:
    text = _mcp_call(client, EXA_MCP_URL, "web_fetch_exa", {"urls": [url]}, timeout_s)
    titles = (_after(s, "# " if s.startswith("# ") else "Title:")
              for s in map(str.strip, text.splitlines()) if s.startswith(("# ", "Title:")))
    return {"title": next(titles, ""), "content": text}


def _parallel_extract(client: httpx.Client, url: str, timeout_s: float) -> dict[str, Any]:
    text = _mcp_call(client, PARALLEL_MCP_URL, "web_fetch",
                     {"urls": [url], "objective": "contenu complet de la page", "session_id": _SESSION_ID}, timeout_s)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WebEngineError(f"réponse Parallel inattendue : {exc}") from exc
    results = data.get("results") or []
    if results:
        r = results[0]
        content = r.get("full_content") or r.get("content") or "\n\n".join(r.get("excerpts") or [])
        return {"title": r.get("title") or "", "content": content}
    errors = data.get("errors") or []
    if errors:
        raise WebEngineError(str(errors[0].get("content") or errors[0].get("error_type") or "extraction en échec"))
    raise WebEngineError("aucun contenu renvoyé")


def _firecrawl_extract(client: httpx.Client, url: str, timeout_s: float) -> dict[str, Any]:
    try:
        response = client.post(f"{FIRECRAWL_API_URL}/v2/scrape", json={"url": url, "formats": ["markdown"]},
                               headers={"Content-Type": "application/json"}, timeout=timeout_s)
    except httpx.HTTPError as exc:
        raise WebEngineError(f"requête échouée : {exc}") from exc
    if response.status_code >= 400:
        raise WebEngineError(f"HTTP {response.status_code}: {response.text.strip()[:300]}")
    data = response.json()
    payload = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
    if not isinstance(payload, dict):
        raise WebEngineError("réponse Firecrawl inattendue")
    metadata = payload.get("metadata") or {}
    title = metadata.get("title") if isinstance(metadata, dict) else ""
    return {"title": title or "", "content": payload.get("markdown") or payload.get("html") or ""}


def _keenable_extract(client: httpx.Client, url: str, timeout_s: float) -> dict[str, Any]:
    data = _keenable_request(client, "get", "/v1/fetch/public", timeout_s, params={"url": url})
    return {"title": data.get("title") or "", "content": data.get("content") or ""}


_DIRECT_MIN_CHARS = 300      # en dessous : page JS / mur de connexion -> on passe aux moteurs
_DIRECT_MAX_CHARS = 20_000
_DIRECT_MAX_BYTES = 3_000_000


def _direct_extract(url: str, timeout_s: float) -> dict[str, Any]:
    """Lecture DIRECTE de la page (20.10.2026) : GET gardé du service d'aperçus (SSRF par hop, PII, taille, délai
    global) + texte principal par trafilatura. Aucun tiers, aucune ressource par client, ~0,3-1 s ; les moteurs
    externes (Exa, Parallel, Firecrawl, Keenable) ne servent plus qu'en repli. Import tardif : `previews.fetch`
    importe ce module (garde PII)."""
    from nokker.previews.fetch import guarded_get
    from nokker.previews.parse import extract_meta

    fetched = guarded_get(url, max_bytes=_DIRECT_MAX_BYTES, timeout_s=timeout_s)
    if fetched.status_code >= 400:
        raise WebEngineError(f"HTTP {fetched.status_code}")
    ctype = fetched.content_type.lower()
    body = fetched.body.decode("utf-8", errors="replace")
    if "html" not in ctype and "xml" not in ctype:
        if ctype.startswith("text/") or "json" in ctype:
            return {"title": "", "content": body[:_DIRECT_MAX_CHARS]}
        raise WebEngineError(f"type non textuel : {ctype or 'inconnu'}")
    import trafilatura

    text = (trafilatura.extract(body, url=fetched.final_url, include_comments=False, include_tables=True) or "").strip()
    if len(text) < _DIRECT_MIN_CHARS:
        raise WebEngineError(f"texte trop court en lecture directe ({len(text)} car.)")
    return {"title": extract_meta(body, fetched.final_url).title, "content": text[:_DIRECT_MAX_CHARS]}


_ENGINE_EXTRACTORS: dict[str, Callable[[httpx.Client, str, float], dict[str, Any]]] = {
    "exa": _exa_extract, "parallel": _parallel_extract, "firecrawl": _firecrawl_extract, "keenable": _keenable_extract,
}


# ─────────────────────────────── disjoncteur par moteur ───────────────────────────────


@dataclass
class _CircuitBreaker:
    """3 échecs consécutifs (hors lenteur : la lenteur est absorbée par le budget, pas par le
    disjoncteur — un moteur qui répond en 3,7 s mais répond n'est pas en panne) → pause 60 s."""

    fail_threshold: int = 3
    cooldown_s: float = 60.0
    _fails: dict[str, int] = field(default_factory=dict)
    _opened_at: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def is_open(self, engine: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            opened = self._opened_at.get(engine)
            if opened is None:
                return False
            if now - opened >= self.cooldown_s:
                del self._opened_at[engine]
                self._fails[engine] = 0
                return False
            return True

    def record_success(self, engine: str) -> None:
        with self._lock:
            self._fails[engine] = 0
            self._opened_at.pop(engine, None)

    def record_failure(self, engine: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            fails = self._fails.get(engine, 0) + 1
            self._fails[engine] = fails
            if fails >= self.fail_threshold:
                self._opened_at[engine] = now


# ─────────────────────────────── cache mémoire LRU + TTL ───────────────────────────────


@dataclass
class _QueryCache:
    maxsize: int = 256
    ttl_s: float = 600.0
    _store: "OrderedDict[str, tuple[float, Any]]" = field(default_factory=OrderedDict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @staticmethod
    def key(query: str, limit: int) -> str:
        return f"{limit}:{' '.join(query.strip().lower().split())}"

    def get(self, key: str, now: float | None = None) -> Any:
        now = time.monotonic() if now is None else now
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            ts, value = entry
            if now - ts > self.ttl_s:
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return value

    def set(self, key: str, value: Any, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._store[key] = (now, value)
            self._store.move_to_end(key)
            while len(self._store) > self.maxsize:
                self._store.popitem(last=False)


# ─────────────────────────────── fusion / consensus ───────────────────────────────


@dataclass
class EngineOutcome:
    engine: str
    rows: list[dict[str, Any]] | None  # None = échec
    error: str | None = None
    elapsed_s: float = 0.0


def merge_results(engine_rows: dict[str, list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    """Fusionne les lignes de plusieurs moteurs : dédoublonnage par URL normalisée, score de
    consensus (nombre de moteurs distincts, DESC) puis par rang moyen (ASC, meilleur d'abord)."""
    buckets: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for engine, rows in engine_rows.items():
        for rank, row in enumerate(rows, start=1):
            url = (row.get("url") or "").strip()
            if not url:
                continue
            key = normalize_url(url)
            bucket = buckets.get(key)
            if bucket is None:
                domain = _domain_of(url)
                bucket = {
                    "url": url, "title": row.get("title") or "", "snippet": row.get("snippet") or "",
                    "domain": domain, "favicon_url": f"https://{domain}/favicon.ico",
                    "engines": [], "_ranks": [],
                }
                buckets[key] = bucket
            if engine not in bucket["engines"]:
                bucket["engines"].append(engine)
            bucket["_ranks"].append(rank)
            if not bucket["title"] and row.get("title"):
                bucket["title"] = row["title"]
            if not bucket["snippet"] and row.get("snippet"):
                bucket["snippet"] = row["snippet"]

    scored = []
    for bucket in buckets.values():
        consensus = len(bucket["engines"])
        avg_rank = sum(bucket["_ranks"]) / len(bucket["_ranks"])
        best_rank = min(bucket["_ranks"])
        scored.append((-consensus, avg_rank, best_rank, bucket))
    scored.sort(key=lambda t: (t[0], t[1], t[2]))

    out: list[dict[str, Any]] = []
    for i, (_, _, _, bucket) in enumerate(scored[:max(limit, 0)] if limit else scored, start=1):
        bucket.pop("_ranks", None)
        bucket["rank"] = i
        out.append(bucket)
    return out


def _format_search_text(results: list[dict[str, Any]], engines_queried: list[str]) -> str:
    if not results:
        used = ", ".join(engines_queried) if engines_queried else "aucun"
        return f"Aucun résultat (moteurs interrogés : {used})."
    lines = []
    for r in results:
        head = f"{r['rank']}. {r['title'] or r['url']} — {r['domain']} [{', '.join(r['engines'])}]"
        body = f"\n   {r['url']}"
        if r.get("snippet"):
            body += f"\n   {r['snippet']}"
        lines.append(head + body)
    return "\n".join(lines)


# ─────────────────────────────── ToolSource ───────────────────────────────


_SEARCH_PARAMS = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "la requête à chercher sur le web"},
        "limit": {"type": "integer", "description": "nombre de résultats souhaités (défaut 8, plafond 20)"},
    },
    "required": ["query"],
}

_FETCH_PARAMS = {
    "type": "object",
    "properties": {
        "urls": {"type": "array", "items": {"type": "string"}, "description": "URLs à récupérer en texte"},
    },
    "required": ["urls"],
}


@dataclass
class WebToolSource:
    """`ToolSource` recherche web sans clé : `web_search` (fan-out 4 moteurs, budget global,
    fusion par consensus) + `web_fetch` (extraction, 1er moteur disponible qui répond).

    Résilience : un moteur en erreur/429/lent n'empêche jamais la réponse — budget global pour la
    lenteur, disjoncteur par moteur (3 échecs → pause 60 s) pour les pannes. Cache LRU (10 min) par
    requête normalisée. Garde PII avant tout envoi (voir `pii_violation`). Aucun identifiant
    utilisateur envoyé aux moteurs — `_SESSION_ID` est une corrélation anti-abus aléatoire par
    processus pour Parallel, jamais un identifiant de l'utilisateur."""

    engines: list[str] = field(default_factory=lambda: list(_ALL_ENGINES))
    budget_s: float = 2.5
    blocked_terms: list[str] = field(default_factory=list)
    breaker: _CircuitBreaker = field(default_factory=_CircuitBreaker)
    cache: _QueryCache = field(default_factory=_QueryCache)
    _client: httpx.Client = field(init=False, repr=False)
    _executor: ThreadPoolExecutor = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.engines = [e for e in self.engines if e in _ALL_ENGINES] or list(_ALL_ENGINES)
        self._client = httpx.Client()
        self._executor = ThreadPoolExecutor(max_workers=max(4, len(_ALL_ENGINES)), thread_name_prefix="nokker-web")

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._client.close()

    # --- ToolSource ---
    def specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(name="web_search", description=(
                "Recherche sur le web (moteurs publics, fusionnés). La requête reprend les noms propres et le "
                "sujet de la demande EN ENTIER (« détroit d'Hormuz », jamais « détroit » seul) ; pour un pseudo de "
                "réseau social, ajoute le nom du réseau (« jdupont instagram »)."),
                     parameters=_SEARCH_PARAMS, effect=Effect.READ, source="builtin"),
            ToolSpec(name="web_fetch", description="Récupère le texte d'une ou plusieurs pages web.",
                     parameters=_FETCH_PARAMS, effect=Effect.READ, source="builtin"),
        ]

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if name == "web_search":
            return self._call_search(arguments)
        if name == "web_fetch":
            return self._call_fetch(arguments)
        return ToolResult(call_id="", name=name, content=f"outil inconnu : {name}", is_error=True)

    # --- web_search ---
    def _call_search(self, arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return ToolResult(call_id="", name="web_search", content="requête vide.", is_error=True)
        violation = pii_violation(query, self.blocked_terms)
        if violation:
            return ToolResult(call_id="", name="web_search", is_error=True,
                              content=f"requête refusée : donnée personnelle ({violation}) — reformule sans elle.")
        try:
            limit = max(1, min(20, int(arguments.get("limit", 8) or 8)))
        except (TypeError, ValueError):
            limit = 8

        cache_key = self.cache.key(query, limit)
        cached = self.cache.get(cache_key)
        if cached is not None:
            results, engines_queried = cached
        else:
            results, engines_queried = self._fan_out_search(query, limit)
            self.cache.set(cache_key, (results, engines_queried))

        text = _format_search_text(results, engines_queried)
        payload = {"query": query, "limit": limit, "engines_queried": engines_queried, "results": results}
        content = (f"{text}\n\n<!-- nokker:web_search:data -->\n"
                   f"{json.dumps(payload, ensure_ascii=False)}\n<!-- /nokker:web_search:data -->")
        return ToolResult(call_id="", name="web_search", content=content)

    def _fan_out_search(self, query: str, limit: int) -> tuple[list[dict[str, Any]], list[str]]:
        futures: dict[Future, str] = {}
        searchers = _ENGINE_SEARCHERS
        for engine in self.engines:
            if self.breaker.is_open(engine):
                logger.info("moteur %s en pause (disjoncteur ouvert) : ignoré pour cette requête", engine)
                continue
            fn = searchers[engine]
            futures[self._executor.submit(_run_search_engine, engine, fn, self._client, query, limit, self.budget_s)] = engine

        if not futures:
            return [], []

        done, pending = wait(futures.keys(), timeout=self.budget_s)
        collected: dict[str, list[dict[str, Any]]] = {}
        for fut in done:
            engine = futures[fut]
            outcome = fut.result()
            if outcome.rows is not None:
                collected[engine] = outcome.rows
                self.breaker.record_success(engine)
            else:
                logger.info("moteur %s en échec : %s", engine, outcome.error)
                self.breaker.record_failure(engine)
        for fut in pending:
            logger.info("moteur %s trop lent (> budget %.1fs) : ignoré pour cette requête, achève en arrière-plan",
                       futures[fut], self.budget_s)

        merged = merge_results(collected, limit)
        return merged, sorted(collected.keys())

    # --- web_fetch ---
    def _call_fetch(self, arguments: dict[str, Any]) -> ToolResult:
        raw = arguments.get("urls") or arguments.get("url") or []
        if isinstance(raw, str):
            # Vu en réel 19.10 : le modèle passe parfois la liste ENCODÉE en chaîne JSON (« ["https://a", …] ») —
            # elle partait telle quelle comme UNE url vers les moteurs (3 échecs → bascule Deep).
            text = raw.strip()
            parsed = None
            if text.startswith("["):
                try:
                    parsed = json.loads(text)
                except ValueError:
                    parsed = None
            raw = parsed if isinstance(parsed, list) else re.split(r"[\s,]+", text)
        urls = [str(u).strip() for u in raw if str(u).strip()]
        if not urls:
            # Vu en réel 19.10 (Qwen3.6-35B) : `{"query": "https://…"}` au lieu de `urls` → 2 échecs → bascule Deep.
            # Toute URL http(s) présente dans les arguments, quel que soit le nom du champ, est prise.
            found = re.findall(r"https?://[^\s\"'<>\],]+", json.dumps(arguments, ensure_ascii=False))
            urls = list(dict.fromkeys(u.rstrip(".);") for u in found))
        if not urls:
            return ToolResult(call_id="", name="web_fetch", content="aucune URL fournie (paramètre `urls`).",
                              is_error=True)

        futures = {self._executor.submit(self._fetch_one, u): u for u in urls}
        done, pending = wait(futures.keys(), timeout=max(self.budget_s * 4, 10.0))
        docs_by_url = {}
        for fut in done:
            docs_by_url[futures[fut]] = fut.result()
        for fut in pending:
            docs_by_url[futures[fut]] = {"url": futures[fut], "title": "", "content": "",
                                         "error": "délai dépassé"}
        docs = [docs_by_url[u] for u in urls]

        parts = []
        for i, d in enumerate(docs, start=1):
            if d.get("error"):
                parts.append(f"{i}. {d['url']} — échec : {d['error']}")
            else:
                parts.append(f"{i}. {d['url']} — {d['title'] or '(sans titre)'}\n{d['content']}")
        payload = {"documents": docs}
        content = ("\n\n".join(parts) + f"\n\n<!-- nokker:web_fetch:data -->\n"
                   f"{json.dumps(payload, ensure_ascii=False)}\n<!-- /nokker:web_fetch:data -->")
        any_ok = any(not d.get("error") for d in docs)
        return ToolResult(call_id="", name="web_fetch", content=content, is_error=not any_ok)

    def _fetch_one(self, url: str) -> dict[str, Any]:
        try:
            doc = _direct_extract(url, self.budget_s)
            return {"url": url, "title": doc.get("title") or "", "content": doc.get("content") or "", "engine": "direct"}
        except Exception as exc:  # noqa: BLE001 — refus SSRF/PII, page JS, timeout : on tente les moteurs
            last_error = f"direct : {exc}"
        for engine in self.engines:
            if self.breaker.is_open(engine):
                continue
            fn = _ENGINE_EXTRACTORS[engine]
            try:
                doc = fn(self._client, url, self.budget_s)
            except Exception as exc:  # un moteur qui échoue sur CETTE url : on tente le suivant
                last_error = str(exc)
                self.breaker.record_failure(engine)
                continue
            self.breaker.record_success(engine)
            return {"url": url, "title": doc.get("title") or "", "content": doc.get("content") or "", "engine": engine}
        return {"url": url, "title": "", "content": "", "error": last_error}


def _run_search_engine(name: str, fn: Callable[[httpx.Client, str, int, float], list[dict[str, Any]]],
                       client: httpx.Client, query: str, limit: int, timeout_s: float) -> EngineOutcome:
    start = time.monotonic()
    try:
        rows = fn(client, query, limit, timeout_s)
        return EngineOutcome(engine=name, rows=rows, elapsed_s=time.monotonic() - start)
    except Exception as exc:
        return EngineOutcome(engine=name, rows=None, error=str(exc), elapsed_s=time.monotonic() - start)


__all__ = [
    "EngineOutcome", "WebEngineError", "WebToolSource", "merge_results", "normalize_url", "pii_violation",
]
