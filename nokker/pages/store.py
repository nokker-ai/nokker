"""Pages créées dans une session (docs/BUBBLES.md §10, docs/LOKKAL.md §6.6) : le magasin du noyau local.

Une page = un fichier HTML autonome du dossier de travail, déclaré par l'outil `page_create`. Le noyau en garde une
COPIE par version (`<root>/<page_id>/v<N>.html` + `meta.json`) : la page reste servable si le fichier bouge, si le
dépôt change de branche, et le miroir (L5) n'aura qu'à copier ce dossier. Jamais de fichiers annexes en v1 (servir un
dossier exposerait le dépôt), jamais autre chose que du HTML (pas de SVG, qui exécute du script).
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
_SIG_HEX_LEN = 32


class PageError(ValueError):
    """Page refusée (taille, type) : le message revient au modèle tel quel."""


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

    def state(self) -> dict:
        """`state` de la bulle `artifact` (§10) : jamais le contenu."""
        return {"page_id": self.page_id, "path": self.path, "title": self.title, "mime": self.mime,
                "version": self.version, "size": self.size, "public": self.public, "updated_at": self.updated_at}


def is_page_id(value: str) -> bool:
    return bool(_ID.match(value or ""))


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
                            self._meta[d.name] = PageMeta(**json.loads(f.read_text(encoding="utf-8")))
                        except (ValueError, TypeError):
                            continue
        return self._meta

    def get(self, page_id: str) -> PageMeta | None:
        if not is_page_id(page_id):
            return None
        with self._mu:
            return self._load().get(page_id)

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

    def update(self, page_id: str, *, thread_id: str | None = None, public: bool | None = None) -> PageMeta | None:
        with self._mu:
            meta = self._load().get(page_id)
            if meta is None:
                return None
            if thread_id is not None:
                meta.thread_id = thread_id
            if public is not None:
                meta.public = public
            self._save(meta, None)
            return meta

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


__all__ = ["LINK_TTL_S", "download_name", "MAX_BYTES", "PAGE_HEADERS", "PageError", "PageMeta", "PageStore", "is_page_id"]
