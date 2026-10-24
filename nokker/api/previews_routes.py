"""nokker/api/previews_routes.py — `GET /v1/previews`, `GET /v1/previews/img/{sha}` (docs/BUBBLES.md §5).

Module SÉPARÉ de `nokker/api/server.py` par construction (une autre branche, `p1/bubbles`, modifie
`server.py` en parallèle pour `/v1/threads`/`/v1/bubbles`) : server.py n'importe ici que
`register_previews_routes` et appelle `routes.handle_get(self, self.path)` en UN seul point
d'accroche dans `do_GET`, avant ses propres routes — pas de dispatcher partagé à faire évoluer à
deux pendant que les deux chantiers avancent.

Auth : IDENTIQUE aux autres ressources client `/v1/*` (`docs/CLIENT-API.md` — jeton de session, clé
API statique, ou `initData` Telegram validé), donc `handler._resolve_identity()` — PAS
`handler._authenticate()` (réservée à `/v1/chat/completions`, où `sender=None` est un cas valide).

Exception : `GET /v1/previews/img/{sha}` (docs/BUBBLES.md §9.3) accepte AUSSI une signature
`?exp=<epoch>&sig=<hmac>` à la place de l'identité — un `<img src=…>` ne pose jamais d'en-tête
`Authorization`. `GET /v1/previews` (le JSON) reste identité SEULE, inchangé.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from nokker.config import NokkerConfig
from nokker.previews.cache import is_valid_sha256
from nokker.previews.service import PreviewError, PreviewService

logger = logging.getLogger("nokker.api.previews_routes")

_IMG_PREFIX = "/v1/previews/img/"


@dataclass
class PreviewsRoutes:
    service: PreviewService

    def matches(self, path: str) -> bool:
        return path == "/v1/previews" or path.startswith(_IMG_PREFIX)

    def handle_get(self, handler: Any, raw_path: str) -> bool:
        """`True` si la requête a été traitée (réponse déjà écrite sur *handler*) — sinon
        `server.py` continue vers ses propres routes (ex. `False` sur tout chemin hors
        `/v1/previews*`, pour ne jamais intercepter autre chose).

        L'image (`/v1/previews/img/{sha}`) gère SON authentification elle-même (identité OU
        signature, §9.3) — contrairement à `/v1/previews` (JSON), qui exige l'identité avant
        toute chose."""
        parsed = urlparse(raw_path)
        if not self.matches(parsed.path):
            return False

        if parsed.path.startswith(_IMG_PREFIX):
            self._handle_image(handler, parsed.path[len(_IMG_PREFIX):], parse_qs(parsed.query))
            return True

        sender = handler._resolve_identity()
        if sender is None:
            handler._json(401, {"error": {"message": "unauthorized"}})
            return True
        self._handle_preview(handler, parsed.query)
        return True

    def _handle_preview(self, handler: Any, query: str) -> None:
        url = (parse_qs(query).get("url") or [""])[0].strip()
        if not url:
            handler._json(400, {"error": {"message": "paramètre url manquant"}})
            return
        try:
            result = self.service.preview(url)
        except PreviewError as exc:
            handler._json(exc.status, {"error": {"message": str(exc)}})
            return
        except Exception:
            logger.exception("aperçu de %s en échec inattendu", url)
            handler._json(502, {"error": {"message": "aperçu indisponible"}})
            return
        handler._json(200, result)

    def _handle_image(self, handler: Any, sha: str, query: dict[str, list[str]]) -> None:
        if not is_valid_sha256(sha):
            handler._json(404, {"error": {"message": "not found"}})
            return
        exp = (query.get("exp") or [""])[0]
        sig = (query.get("sig") or [""])[0]
        signed_ok = bool(exp and sig and self.service.verify_image_sig(sha, exp, sig))
        if not signed_ok:
            sender = handler._resolve_identity()
            if sender is None:
                handler._json(401, {"error": {"message": "unauthorized"}})
                return
        found = self.service.image(sha)
        if found is None:
            handler._json(404, {"error": {"message": "not found"}})
            return
        body, content_type = found
        if not content_type.startswith("image/"):
            # Défense en profondeur : `PreviewService` ne devrait déjà stocker que de l'`image/*`,
            # mais une entrée de cache d'une version antérieure ne doit jamais être re-servie telle
            # quelle (ni ici ni ailleurs — `nosniff` ci-dessous couvre le navigateur).
            handler._json(404, {"error": {"message": "not found"}})
            return
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        # Cache long : le chemin est adressé par contenu (sha256), jamais réutilisé pour un autre
        # contenu — `immutable` évite une revalidation inutile côté navigateur/CDN.
        handler.send_header("Cache-Control", "public, max-age=86400, immutable")
        handler.send_header("X-Content-Type-Options", "nosniff")
        # Une image SVG ouverte DIRECTEMENT (pas via <img>) exécuterait ses scripts sous notre origine :
        # bac à sable strict, aucune ressource, aucun script.
        handler.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; sandbox")
        handler.end_headers()
        handler.wfile.write(body)


def register_previews_routes(config: NokkerConfig) -> PreviewsRoutes | None:
    """`None` si `config.previews_enabled` est faux — même esprit que `approvals=None` dans
    `make_handler` : un serveur qui ne veut pas de ce chantier n'en expose aucune trace (les deux
    chemins retombent sur le 404 générique existant de `server.py`, pas un code qui révèle que la
    route existe mais est désactivée)."""
    if not config.previews_enabled:
        return None
    from nokker.previews import get_default_service

    return PreviewsRoutes(service=get_default_service(config))

__all__ = ["PreviewsRoutes", "register_previews_routes"]
