"""nokker/previews/service.py — orchestration d'un aperçu (docs/BUBBLES.md §5).

`GET /v1/previews?url=...` : récupération gardée (`fetch.guarded_get`) -> parsing OpenGraph/Twitter/
oEmbed/Trafilatura (`parse.py`) -> `kind` -> images re-téléchargées puis re-servies par le noyau
(`cache.ImageDiskCache`) -> JSON canonique (forme exacte : `docs/BUBBLES.md` §5).
"""
from __future__ import annotations

import json
import logging
import secrets
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nokker.previews.cache import ImageDiskCache, TtlCache, sign_image, verify_image_signature
from nokker.previews.fetch import ClientFactory, FetchError, guarded_get
from nokker.previews.parse import (
    EmbedRef,
    MetaInfo,
    embed_from_url,
    extract_excerpt,
    extract_meta,
    guess_kind,
    parse_oembed_json,
)
from nokker.previews.ssrf import MAX_REDIRECTS, TIMEOUT_S, SsrfBlocked

logger = logging.getLogger("nokker.previews.service")

MAX_PAGE_BYTES = 3 * 1024 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_OEMBED_BYTES = 256 * 1024


class PreviewError(RuntimeError):
    """Erreur PUBLIQUE (message sûr à renvoyer au client) — refus de politique (SSRF/PII) ou échec
    réseau (`status` = code HTTP conseillé à la route : 400 politique, 502 réseau)."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class PreviewService:
    cache_dir: str
    ttl_s: float = 86400.0
    image_cache_max_bytes: int = 200 * 1024 * 1024
    max_page_bytes: int = MAX_PAGE_BYTES
    max_image_bytes: int = MAX_IMAGE_BYTES
    timeout_s: float = TIMEOUT_S
    max_redirects: int = MAX_REDIRECTS
    # Injection pour les tests (`httpx.MockTransport`, aucun réseau) — `None` = vrai client pinglé
    # SSRF (`ssrf.pinned_client`), le défaut de `fetch.guarded_get`.
    client_factory: ClientFactory | None = None
    # Idem pour la résolution DNS (`None` = `socket.getaddrinfo`, le défaut de `ssrf.resolve_and_check`)
    # — un test peut ainsi exercer le chemin oEmbed/image (hôtes nommés : youtube.com, un CDN…) sans
    # DNS réel, tout en gardant la VRAIE garde SSRF (même code, résolveur remplacé).
    resolver: Any | None = None
    # Clé HMAC des URL d'image signées (docs/BUBBLES.md §9.3) : `str`/`bytes` fournis par l'appelant
    # (`NokkerConfig.previews_sign_secret()`, un secret d'environnement — jamais dans le dépôt), ou
    # `None` -> générée ici au démarrage (`secrets.token_bytes`) et gardée en mémoire pour la durée du
    # processus (limite documentée : un redémarrage invalide les liens déjà distribués, le client
    # relit `GET /v1/previews` qui re-signe).
    sign_secret: str | bytes | None = None
    _cache: TtlCache = field(init=False, repr=False)
    _images: ImageDiskCache = field(init=False, repr=False)
    _sign_secret: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._cache = TtlCache(maxsize=512, ttl_s=self.ttl_s)
        self._images = ImageDiskCache(Path(self.cache_dir), max_bytes=self.image_cache_max_bytes)
        if self.sign_secret:
            self._sign_secret = self.sign_secret.encode("utf-8") if isinstance(self.sign_secret, str) \
                else self.sign_secret
        else:
            self._sign_secret = secrets.token_bytes(32)

    # ─────────────────────────── API publique ───────────────────────────

    def preview(self, url: str) -> dict[str, Any]:
        url = (url or "").strip()
        if not url:
            raise PreviewError("url manquante", status=400)
        cached = self._cache.get(url)
        if cached is not None:
            return cached
        result = self._build(url)
        self._cache.set(url, result)
        return result

    def preview_many(self, urls: list[str], limit: int = 15) -> list[dict[str, Any]]:
        """Équivalent synchrone (le producteur `search` l'appelle en tâche de fond, `docs/BUBBLES.md`
        §5) : N aperçus en parallèle (fils), jamais d'exception qui remonte — une URL en échec
        donne `{"url": …, "error": "..."}` plutôt que de faire échouer tout le lot."""
        batch = [u for u in (urls or []) if u][: max(0, limit)]
        if not batch:
            return []
        with ThreadPoolExecutor(max_workers=min(len(batch), 8), thread_name_prefix="nokker-preview") as ex:
            futures = {ex.submit(self._safe_preview, u): u for u in batch}
            results = {futures[f]: f.result() for f in futures}
        return [results[u] for u in batch]

    def image(self, sha256: str) -> tuple[bytes, str] | None:
        return self._images.get(sha256)

    def signed_image_url(self, sha256: str) -> str:
        exp, sig = sign_image(sha256, self._sign_secret)
        return f"/v1/previews/img/{sha256}?exp={exp}&sig={sig}"

    def verify_image_sig(self, sha256: str, exp: str, sig: str) -> bool:
        return verify_image_signature(sha256, exp, sig, self._sign_secret)

    # ─────────────────────────── interne ───────────────────────────

    def _safe_preview(self, url: str) -> dict[str, Any]:
        try:
            return self.preview(url)
        except PreviewError as exc:
            return {"url": url, "error": str(exc)}
        except Exception as exc:  # défense en profondeur : un lot ne doit jamais planter en entier
            logger.exception("aperçu de %s en échec inattendu", url)
            return {"url": url, "error": f"échec inattendu : {exc}"}

    def _resolver_fn(self):
        return self.resolver or socket.getaddrinfo

    def _build(self, url: str) -> dict[str, Any]:
        try:
            fetched = guarded_get(url, max_bytes=self.max_page_bytes, timeout_s=self.timeout_s,
                                  max_redirects=self.max_redirects, client_factory=self.client_factory,
                                  resolver=self._resolver_fn())
        except SsrfBlocked as exc:
            raise PreviewError(str(exc), status=400) from exc
        except FetchError as exc:
            raise PreviewError(str(exc), status=502) from exc

        content_type = fetched.content_type.split(";")[0].strip().lower()
        is_html = (not content_type) or content_type in ("text/html", "application/xhtml+xml")
        body_text = fetched.body.decode("utf-8", errors="replace") if is_html else ""

        meta = extract_meta(body_text, fetched.final_url) if body_text else MetaInfo()
        embed = embed_from_url(fetched.final_url)
        if embed is not None:
            oembed = self._fetch_oembed(embed)
            if oembed:
                meta.title = oembed.get("title") or meta.title
                meta.image = oembed.get("image") or meta.image
                meta.site_name = oembed.get("site_name") or meta.site_name

        excerpt = extract_excerpt(body_text, fetched.final_url) if body_text else ""
        kind = guess_kind(fetched.final_url, content_type=content_type, og_type=meta.og_type, embed=embed,
                          has_excerpt=bool(excerpt))

        return {
            "url": url,
            "final_url": fetched.final_url,
            "title": meta.title,
            "site_name": meta.site_name,
            "description": meta.description,
            "image": self._cache_remote_image(meta.image) if meta.image else None,
            "favicon": self._cache_remote_image(meta.favicon) if meta.favicon else None,
            "excerpt": excerpt,
            "lang": meta.lang,
            "kind": kind,
            "embed": {"provider": embed.provider, "id": embed.id} if embed else None,
            "fetched_at": _iso_now(),
        }

    def _fetch_oembed(self, embed: EmbedRef) -> dict[str, Any] | None:
        try:
            resp = guarded_get(embed.oembed_url, max_bytes=MAX_OEMBED_BYTES, timeout_s=self.timeout_s,
                               max_redirects=self.max_redirects, client_factory=self.client_factory,
                               resolver=self._resolver_fn(),
                               check_pii=False)  # URL fixe générée par nous (provider + id), rien d'utilisateur
        except (SsrfBlocked, FetchError) as exc:
            logger.info("oEmbed %s en échec : %s", embed.provider, exc)
            return None
        try:
            raw = json.loads(resp.body.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return parse_oembed_json(raw) if isinstance(raw, dict) else None

    def _cache_remote_image(self, image_url: str | None) -> str | None:
        if not image_url:
            return None
        try:
            fetched = guarded_get(image_url, max_bytes=self.max_image_bytes, timeout_s=self.timeout_s,
                                  max_redirects=self.max_redirects, client_factory=self.client_factory,
                                  resolver=self._resolver_fn(),
                                  check_pii=False)  # URL d'image dérivée de la page, pas saisie utilisateur
        except (SsrfBlocked, FetchError) as exc:
            logger.info("image %s non récupérée : %s", image_url, exc)
            return None
        content_type = fetched.content_type.split(";")[0].strip().lower()
        if not content_type.startswith("image/"):
            logger.info("image %s refusée : content-type %s", image_url, content_type or "(absent)")
            return None
        sha = self._images.put(fetched.body, content_type)
        return self.signed_image_url(sha)


__all__ = ["PreviewError", "PreviewService"]
