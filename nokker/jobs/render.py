"""Rendu PDF des versions retouchées (docs/JOBS-PACK.md §5.3) : dans un Chrome SANS RÉSEAU, sur la machine.

Deux briques :
- `html_builder(document, template) -> str` : HTML autonome du gabarit exporté par le service emploi (`template`, §13.6 :
  `{id, version, bundle_url}`). Le format du paquet de gabarit n'est pas encore fixé par le contrat : aucun
  constructeur n'est livré, c'est le point d'extension (`HtmlBuilder`).
- `OfflineChromePdf.html_to_pdf(html)` : Chrome headless, profil jetable, toute requête réseau impossible (mandataire
  vers un port fermé, résolution de noms coupée, réseau de fond désactivé), `--print-to-pdf`. Le lanceur est injectable
  (tests).

`LocalRenderer` assemble les deux ; sans constructeur ou sans Chrome, `RenderUnavailable` : l'appelant prend le repli
de dépannage (rendu sans stockage par le service emploi, `POST /partner/cv/render`).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from typing import Any, Protocol


class RenderUnavailable(RuntimeError):
    pass


class PdfRenderer(Protocol):
    def render(self, document: dict[str, Any], template: dict[str, Any]) -> bytes: ...


HtmlBuilder = Callable[[dict[str, Any], dict[str, Any]], str]
Runner = Callable[[list[str]], int]

# Aucun accès réseau : tout passe par un mandataire sur un port fermé, toute résolution échoue, rien en fond.
OFFLINE_FLAGS = (
    "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    "--proxy-server=http://127.0.0.1:9", "--proxy-bypass-list=<-loopback>",
    "--host-resolver-rules=MAP * ~NOTFOUND", "--disable-background-networking", "--disable-component-update",
    "--disable-sync", "--disable-domain-reliability", "--disable-features=OptimizationHints,MediaRouter",
    "--no-pdf-header-footer",
)


def find_chrome() -> str | None:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _run(argv: list[str]) -> int:
    return subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=False).returncode


class OfflineChromePdf:
    def __init__(self, chrome: str | None = None, runner: Runner | None = None) -> None:
        self.chrome = chrome if chrome is not None else find_chrome()
        self._run = runner or _run

    def argv(self, html_path: str, pdf_path: str, profile_dir: str) -> list[str]:
        # Sous root (conteneur), Chrome refuse de démarrer avec son bac à sable : le rendu ne charge que notre HTML,
        # réseau coupé, profil jetable.
        root = ["--no-sandbox"] if hasattr(os, "geteuid") and os.geteuid() == 0 else []
        return [str(self.chrome), *OFFLINE_FLAGS, *root, f"--user-data-dir={profile_dir}",
                f"--print-to-pdf={pdf_path}", "file://" + html_path]

    def html_to_pdf(self, html: str) -> bytes:
        if not self.chrome:
            raise RenderUnavailable("Chrome absent de cette machine")
        with tempfile.TemporaryDirectory(prefix="nokker-pdf-") as tmp:
            src, out = os.path.join(tmp, "doc.html"), os.path.join(tmp, "doc.pdf")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(html)
            code = self._run(self.argv(src, out, os.path.join(tmp, "profile")))
            if code != 0 or not os.path.exists(out):
                raise RenderUnavailable(f"rendu en échec (code {code})")
            with open(out, "rb") as fh:
                data = fh.read()
        if not data.startswith(b"%PDF"):
            raise RenderUnavailable("sortie qui n'est pas un PDF")
        return data


class LocalRenderer:
    def __init__(self, html_builder: HtmlBuilder | None = None, pdf: OfflineChromePdf | None = None) -> None:
        self.html_builder, self.pdf = html_builder, pdf or OfflineChromePdf()

    def render(self, document: dict[str, Any], template: dict[str, Any]) -> bytes:
        if self.html_builder is None:
            raise RenderUnavailable("constructeur de gabarit absent")
        return self.pdf.html_to_pdf(self.html_builder(document, template))


__all__ = ["OFFLINE_FLAGS", "HtmlBuilder", "LocalRenderer", "OfflineChromePdf", "PdfRenderer", "RenderUnavailable",
           "find_chrome"]
