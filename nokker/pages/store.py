"""Pages créées dans une session (docs/BUBBLES.md §10, docs/LOKKAL.md §6.6) : le magasin du noyau local.

Une page = un fichier HTML autonome du dossier de travail, déclaré par l'outil `page_create`. Le noyau en garde une
COPIE par version (`<root>/<page_id>/v<N>.html` + `meta.json`) : la page reste servable si le fichier bouge, si le
dépôt change de branche, et le miroir (L5) n'aura qu'à copier ce dossier. Jamais de fichiers annexes en v1 (servir un
dossier exposerait le dépôt), jamais autre chose que du HTML (pas de SVG, qui exécute du script).

Publication (docs/LOKKAL.md §7.26) : une page est privée tant que l'utilisateur ne l'a pas publiée, sur une carte forte
(`nokker/pages/public.py`). Publier fige UNE version (celle que la carte montrait) et tire un id public au hasard
(`public_id`, 24 caractères), distinct du `page_id` : l'URL publique `https://<nom>--pub.nokker.app/<public_id>` ne
partage rien avec un lien privé. Dépublier efface l'id (l'URL meurt pour toujours ; republier en tire un neuf). Une
page épinglée `local` (`locality: "local"`) ne quitte jamais la machine : ni copie au miroir, ni publication.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024  # par version
LINK_TTL_S = 3600.0  # lien signé de l'iframe et de « ouvrir »
PAGE_SUFFIXES = (".html", ".htm")
_ID = re.compile(r"^pg_[0-9a-f]{16}$")
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9_-]{24}$")
_SIG_HEX_LEN = 32


class PageError(ValueError):
    """Page refusée (taille, type) : le message revient au modèle tel quel."""


MODERATED_MESSAGE = ("page retirée par la modération de nokker.ai : la republier demande l'opérateur "
                     "(nokker.ai/report)")


@dataclass
class PageMeta:
    page_id: str
    thread_id: str
    path: str  # chemin tel que l'outil le voit (relatif au dossier de travail, ou sous le point de montage)
    title: str
    version: int
    size: int
    public: bool
    created_at: float
    updated_at: float
    sha256: str
    mime: str = "text/html"
    # Publication (§7.26) : id public tiré au hasard (jamais dérivé du page_id), version figée par la carte, indexation
    # par les moteurs (non par défaut). None = privée.
    public_id: str | None = None
    public_version: int | None = None
    public_index: bool = False
    # Localité choisie par l'utilisateur : "local" = ne quitte jamais la machine (ni miroir, ni publication) ; None =
    # rien d'épinglé (la page reste locale tant qu'aucun choix explicite ne la fait sortir).
    locality: str | None = None
    # Retirée par la modération de nokker.ai (§7.26, point 6) : heure où la machine l'a appris, et ce qui l'a désignée
    # (`page` : son page_id ; `pub:<empreinte>` : son ancienne adresse publique). Gardée à l'auteur, jamais republiée
    # tant que nokker.ai la liste (`apply_moderation`).
    moderated_at: float | None = None
    moderated_key: str | None = None

    def state(self) -> dict:
        """`state` de la bulle `artifact` (§10) : jamais le contenu, jamais l'id public (il est dans `/link`).
        `moderated_at` non nul = « retirée par la modération » (§7.26, point 6)."""
        return {"page_id": self.page_id, "path": self.path, "title": self.title, "mime": self.mime,
                "version": self.version, "size": self.size, "public": self.public,
                "public_version": self.public_version, "public_index": self.public_index,
                "locality": self.locality, "updated_at": self.updated_at, "moderated_at": self.moderated_at}


def is_page_id(value: str) -> bool:
    return bool(_ID.match(value or ""))


def is_public_id(value: str) -> bool:
    return bool(_PUBLIC_ID.match(value or ""))


class PageStore:
    def __init__(self, root: Path, secret: bytes | None = None, clock=time.time) -> None:
        self.root = Path(root)
        self.secret = secret or secrets.token_bytes(32)  # un redémarrage invalide les liens signés (on en redemande)
        self.clock = clock
        self._mu = threading.Lock()
        self._meta: dict[str, PageMeta] | None = None

    # ── lecture ──
    def _load(self) -> dict[str, PageMeta]:
        if self._meta is None:
            self._meta = {}
            if self.root.is_dir():
                for d in self.root.iterdir():
                    f = d / "meta.json"
                    if is_page_id(d.name) and f.is_file():
                        try:
                            raw = json.loads(f.read_text(encoding="utf-8"))
                            known = set(PageMeta.__dataclass_fields__)
                            self._meta[d.name] = PageMeta(**{k: v for k, v in raw.items() if k in known})
                        except (ValueError, TypeError, AttributeError):
                            continue
        return self._meta

    def get(self, page_id: str) -> PageMeta | None:
        if not is_page_id(page_id):
            return None
        with self._mu:
            return self._load().get(page_id)

    def all(self) -> list[PageMeta]:
        with self._mu:
            return sorted(self._load().values(), key=lambda m: m.created_at)

    def by_path(self, thread_id: str, path: str) -> PageMeta | None:
        with self._mu:
            return next((m for m in self._load().values() if m.thread_id == thread_id and m.path == path), None)

    def read(self, page_id: str, version: int | None = None) -> tuple[PageMeta, bytes] | None:
        meta = self.get(page_id)
        if meta is None:
            return None
        v = meta.version if version is None else version
        if not 1 <= v <= meta.version:
            return None
        f = self.root / page_id / f"v{v}.html"
        return (meta, f.read_bytes()) if f.is_file() else None

    # ── écriture ──
    def _save(self, meta: PageMeta, content: bytes | None) -> None:
        d = self.root / meta.page_id
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        if content is not None:
            (d / f"v{meta.version}.html").write_bytes(content)
        tmp = d / "meta.json.tmp"
        tmp.write_text(json.dumps(asdict(meta), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, d / "meta.json")

    @staticmethod
    def check(path: str, content: bytes) -> None:
        if not path.lower().endswith(PAGE_SUFFIXES):
            raise PageError("une page est un fichier .html (CSS, JS et images en ligne)")
        if len(content) > MAX_BYTES:
            raise PageError(f"page trop grande ({len(content)} octets, {MAX_BYTES} au plus)")

    def create(self, thread_id: str, path: str, title: str, content: bytes) -> PageMeta:
        """Déclare une page, ou rend celle qui existe déjà pour (fil, chemin) avec une nouvelle version si le contenu
        a changé."""
        self.check(path, content)
        existing = self.by_path(thread_id, path)
        if existing is not None:
            if title and title != existing.title:
                with self._mu:
                    existing.title = title[:200]
                    self._save(existing, None)
            return self.snapshot(existing.page_id, content)
        now = self.clock()
        meta = PageMeta(page_id=f"pg_{secrets.token_hex(8)}", thread_id=thread_id, path=path,
                        title=(title or os.path.basename(path))[:200], version=1, size=len(content), public=False,
                        created_at=now, updated_at=now, sha256=hashlib.sha256(content).hexdigest())
        with self._mu:
            self._load()[meta.page_id] = meta
            self._save(meta, content)
        return meta

    def snapshot(self, page_id: str, content: bytes) -> PageMeta:
        """Nouvelle version si le contenu a changé ; sinon la page telle quelle."""
        with self._mu:
            meta = self._load()[page_id]
            self.check(meta.path, content)
            sha = hashlib.sha256(content).hexdigest()
            if sha == meta.sha256:
                return meta
            meta.version += 1
            meta.size, meta.sha256, meta.updated_at = len(content), sha, self.clock()
            self._save(meta, content)
            return meta

    def update(self, page_id: str, *, thread_id: str | None = None) -> PageMeta | None:
        """Le fil d'une page (glisser vers une autre session, §10.5). Rendre publique ne passe JAMAIS par ici : seulement
        par `publish`, après la carte forte (§7.26)."""
        with self._mu:
            meta = self._load().get(page_id)
            if meta is None:
                return None
            if thread_id is not None:
                meta.thread_id = thread_id
            self._save(meta, None)
            return meta

    # ── publication (§7.26) ──
    def by_public_id(self, public_id: str) -> PageMeta | None:
        """La page publique de cet id (comparaison à temps constant), ou None : id inconnu, dépubliée, ou épinglée
        `local` (défense en profondeur : épingler dépublie déjà)."""
        if not is_public_id(public_id):
            return None
        with self._mu:
            for m in self._load().values():
                if m.public and m.public_id and hmac.compare_digest(m.public_id, public_id) \
                        and m.locality != "local" and m.public_version:
                    return m
        return None

    def publish(self, page_id: str, version: int, sha256: str, index: bool = False) -> PageMeta:
        """Rend publique la version `version` de la page, à condition qu'elle ait toujours l'empreinte `sha256` (celle
        que la carte a montrée). Garde l'id public d'une page déjà publique (une nouvelle version approuvée garde son
        URL) ; en tire un sinon. PageError si la page est épinglée `local` ou si la version ne correspond plus."""
        with self._mu:
            meta = self._load().get(page_id)
            if meta is None:
                raise PageError("page inconnue")
            if meta.locality == "local":
                raise PageError("page épinglée `local` : elle ne quitte pas la machine")
            if meta.moderated_at is not None:
                raise PageError(MODERATED_MESSAGE)
            f = self.root / page_id / f"v{int(version)}.html"
            if not (1 <= int(version) <= meta.version and f.is_file()
                    and hashlib.sha256(f.read_bytes()).hexdigest() == sha256):
                raise PageError("cette version de la page n'est plus celle de la carte : republie")
            meta.public, meta.public_version, meta.public_index = True, int(version), bool(index)
            meta.public_id = meta.public_id or secrets.token_urlsafe(18)
            self._save(meta, None)
            return meta

    def unpublish(self, page_id: str) -> PageMeta | None:
        """Retire la publication : l'id public est effacé (son URL ne revivra jamais)."""
        with self._mu:
            meta = self._load().get(page_id)
            if meta is None:
                return None
            meta.public, meta.public_id, meta.public_version, meta.public_index = False, None, None, False
            self._save(meta, None)
            return meta

    def apply_moderation(self, moderated: dict, now: float | None = None) -> list[PageMeta]:
        """Pages de cette machine retirées par l'opérateur de nokker.ai (`{page_ids, public_sha256}` de
        `GET /v1/gateway/machine`, §7.26 point 6) : une page désignée est dépubliée (son id public meurt) et marquée
        `moderated_at` ; une page qui n'est plus désignée (rendue par l'opérateur) perd sa marque et reste privée.
        Rend les pages changées (la bulle suit)."""
        ids = set(moderated.get("page_ids") or [])
        shas = set(moderated.get("public_sha256") or [])
        at = self.clock() if now is None else now
        changed = []
        with self._mu:
            for meta in self._load().values():
                sha = hashlib.sha256(meta.public_id.encode()).hexdigest() if meta.public_id else None
                if meta.page_id in ids:
                    key: str | None = "page"
                elif sha is not None and sha in shas:
                    key = f"pub:{sha}"
                elif (meta.moderated_key or "").startswith("pub:") and meta.moderated_key[4:] in shas:
                    key = meta.moderated_key
                else:
                    key = None
                if key is not None and meta.moderated_at is None:
                    meta.public, meta.public_id, meta.public_version, meta.public_index = False, None, None, False
                    meta.moderated_at, meta.moderated_key = at, key
                elif key is None and meta.moderated_at is not None:
                    meta.moderated_at = meta.moderated_key = None
                else:
                    continue
                self._save(meta, None)
                changed.append(meta)
        return changed

    def set_locality(self, page_id: str, local: bool) -> PageMeta | None:
        """Épingle la page `local` (elle ne quittera plus la machine : dépubliée ici, la copie et le choix du miroir
        sont retirés par l'appelant) ou retire l'épingle."""
        with self._mu:
            meta = self._load().get(page_id)
            if meta is None:
                return None
            meta.locality = "local" if local else None
            if local:
                meta.public, meta.public_id, meta.public_version, meta.public_index = False, None, None, False
            self._save(meta, None)
            return meta

    def delete(self, page_id: str) -> bool:
        """Supprime la page et toutes ses versions (`DELETE /v1/pages/{id}`). Une copie gardée chez nokker.ai part au
        passage suivant du miroir (la page n'existe plus ici : la machine en demande la purge)."""
        import shutil

        if not is_page_id(page_id):
            return False
        with self._mu:
            if self._load().pop(page_id, None) is None:
                return False
            shutil.rmtree(self.root / page_id, ignore_errors=True)
            return True

    # ── liens signés (même schéma que les images d'aperçu, docs/BUBBLES.md §9.3) ──
    def _sig(self, page_id: str, exp: str, download: bool) -> str:
        msg = f"page:{page_id}.{exp}.{'dl' if download else 'view'}"  # la signature couvre le téléchargement
        return hmac.new(self.secret, msg.encode("ascii"), hashlib.sha256).hexdigest()[:_SIG_HEX_LEN]

    def link(self, page_id: str, ttl_s: float = LINK_TTL_S, download: bool = False) -> tuple[str, float]:
        exp = int(self.clock() + ttl_s)
        dl = "&dl=1" if download else ""
        return f"/v1/pages/{page_id}?exp={exp}{dl}&sig={self._sig(page_id, str(exp), download)}", float(exp)

    def verify(self, page_id: str, exp: str, sig: str, download: bool = False) -> bool:
        try:
            if int(exp) < self.clock():
                return False
        except (TypeError, ValueError):
            return False
        return hmac.compare_digest(self._sig(page_id, exp, download), sig or "")


# En-têtes de toute réponse qui sert une page (docs/GATEWAY.md §5) : origine opaque, ni cookie, ni stockage, ni appel
# authentifié à l'API — même servie depuis 127.0.0.1 à côté de l'interface locale.
PAGE_HEADERS = {
    "Content-Type": "text/html; charset=utf-8",
    "Content-Security-Policy": "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads",  # = relais
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

def download_name(meta: PageMeta) -> str:
    """Nom du fichier téléchargé : le titre (ou le nom du fichier), sans caractère qui casse l'en-tête."""
    base = re.sub(r"[^\w .()-]+", "", meta.title or "", flags=re.UNICODE).strip() or \
        os.path.splitext(os.path.basename(meta.path))[0] or meta.page_id
    return base[:80] + ".html"


# En-têtes d'une page PUBLIQUE servie par le noyau (le relais pose les siens par-dessus, docs/LOKKAL.md §7.26) : son
# propre JS en ligne seulement, aucune source externe, aucun appel réseau ni formulaire, jamais dans un cadre.
PUBLIC_PAGE_HEADERS = {
    "Content-Type": "text/html; charset=utf-8",
    "Content-Security-Policy": ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                "img-src data:; font-src data:; media-src data:; connect-src 'none'; "
                                "form-action 'none'; base-uri 'none'; frame-ancestors 'none'; "
                                "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
}

__all__ = ["LINK_TTL_S", "MODERATED_MESSAGE", "download_name", "MAX_BYTES", "PAGE_HEADERS", "PUBLIC_PAGE_HEADERS", "PageError", "PageMeta",
           "PageStore", "is_page_id", "is_public_id"]
