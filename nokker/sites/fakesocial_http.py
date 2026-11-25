"""Site simulé servi en HTTP, pour le VRAI navigateur de `sites-chrome` (docs/CONNECTED-SITES.md §13.3, étape 1).

Même logique que `fakesocial.FakeBrowser` (écrans, connexion en un ou deux temps, code, application, CAPTCHA, lien
magique, contenu piégé), rendue en HTML accessible : chaque nœud devient l'élément dont Chrome tire le même rôle et le
même nom (libellés de champ, `aria-label`, `<time>`, liens). Les gestes reviennent par un formulaire unique
(`POST /__act` : valeurs des champs, puis le bouton cliqué).

- La SESSION du site est un vrai cookie (`fs_session`, 30 jours) : elle vit dans le profil de Chrome, et c'est ce que
  l'export / réimport du profil doit garder.
- L'état de PASSAGE (champs tapés, étape de connexion, message d'erreur) vit côté serveur, par onglet (`fs_tab`, cookie
  de session du navigateur).

Test seulement : jamais servi par Lokkal. `python -m nokker.sites.fakesocial_http --skin linkedin --port 8080`.
"""
from __future__ import annotations

import argparse
import os
import html
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from nokker.sites.driver import Node
from nokker.sites.fakesocial import FAKESOCIAL, LINKEDIN_FR, Account, FakeBrowser, FakeServer

_PAGE = ("<!doctype html><html lang=fr><head><meta charset=utf-8><title>{title}</title></head><body>"
         "<form method=post action=/__act><main>{body}</main></form></body></html>")


def _e(s: Any) -> str:
    return html.escape(str(s or ""), quote=True)


def render(nodes: list[Node], title: str) -> str:
    """Nœuds de `FakeBrowser.nodes()` → HTML dont l'arbre d'accessibilité a les mêmes rôles et noms."""
    out: list[str] = []
    items: list[str] = []

    def flush() -> None:
        if items:
            out.append("<ul>" + "".join(items) + "</ul>")
            items.clear()

    for n in nodes:
        a, name, value = n.attrs, _e(n.name), _e(n.value)
        time = f'<time datetime="{_e(a["date"])}">{_e(a["date"])}</time>' if a.get("date") else ""
        if n.role == "listitem":
            unread = "<span>non lu</span>" if a.get("unread") == "1" else ""
            items.append(f'<li aria-label="{name}"><a href="{_e(a.get("href"))}">{name}</a><p>{value}</p>{time}{unread}</li>')
            continue
        flush()
        if n.role == "alert":
            out.append(f'<p role="alert">{name}</p>')
        elif n.role == "textbox":
            typ = "password" if a.get("type") == "password" else "text"
            out.append(f'<label>{name} <input name="f_{n.ref}" type="{typ}" value="{value}" autocomplete="off"></label>')
        elif n.role == "searchbox":
            out.append(f'<input type="search" name="f_{n.ref}" aria-label="{name}">')
        elif n.role == "button":
            out.append(f'<button name="__click" value="{n.ref}">{name}</button>')
        elif n.role == "heading":
            out.append(f"<h1>{name}</h1>")
        elif n.role == "iframe":
            out.append(f'<iframe title="{name}" src="about:blank"></iframe>')
        elif n.role == "link":
            extra = f"<span>{value}</span>" if value else ""
            out.append(f'<p><a href="{_e(a.get("href"))}">{name}</a>{extra}</p>')
        elif n.role in ("article", "comment"):
            role = ' role="comment"' if n.role == "comment" else ""
            link = f'<a href="{_e(a["href"])}">{name}</a>' if a.get("href") else ""
            out.append(f'<article{role} aria-label="{name}">{link}<p>{value}</p>{time}</article>')
        elif n.role == "text":
            hint = a.get("role_hint", "")
            out.append(f'<p aria-label="{_e(hint)}">{name}</p>' if hint else f"<p>{name}</p>")
    flush()
    return _PAGE.format(title=_e(title), body="".join(out))


class FakeSocialHttp:
    def __init__(self, server: FakeServer) -> None:
        self.server = server
        self.tabs: dict[str, FakeBrowser] = {}
        self.lock = threading.Lock()

    def browser(self, cookies: dict[str, str]) -> tuple[str, FakeBrowser]:
        tab = cookies.get("fs_tab", "")
        fb = self.tabs.get(tab)
        if fb is None:
            tab = secrets.token_hex(8)
            fb = self.tabs[tab] = FakeBrowser(self.server)
        sess = cookies.get("fs_session", "")
        if sess:
            fb.cookies["session"] = sess
        else:
            fb.cookies.pop("session", None)
        return tab, fb

    def handle(self, method: str, path: str, cookies: dict[str, str], form: dict[str, str]) -> tuple[int, dict, str]:
        if method == "GET" and urlsplit(path).path in ("/favicon.ico", "/robots.txt"):
            return 404, {}, ""  # requêtes du navigateur hors page : ne déplacent jamais l'onglet (mur de connexion)
        if urlsplit(path).path.startswith("/__test/"):
            return self.control(method, path)
        with self.lock:
            tab, fb = self.browser(cookies)
            base = self.server.base()
            if method == "POST" and path == "/__act":
                nodes = {n.ref: n for n in fb.nodes()}
                for k, v in form.items():
                    if k.startswith("f_") and k[2:] in nodes and v:
                        fb.type(k[2:], v)
                ref = form.get("__click", "")
                if ref in nodes:
                    fb.click(ref)
                if os.environ.get("FAKESOCIAL_TRACE"):
                    print("POST", sorted(k for k in form), "clic", nodes.get(ref).name if ref in nodes else "?",
                          "→", fb.url(), "erreur", fb._login_error, "session", bool(fb.cookies.get("session")), flush=True)
                status, body = 303, ""
            elif urlsplit(path).path.startswith("/checkpoint/") and urlsplit(fb.url()).path == urlsplit(path).path:
                fb.refresh()  # rechargement d'un écran d'attente : la validation faite ailleurs aboutit
                status, body = 200, render(fb.nodes(), urlsplit(fb.url()).path)
                if urlsplit(fb.url()).path != urlsplit(path).path:
                    status, body = 303, ""
            else:
                url = base + path
                fb.goto(url)
                status = 200
                body = render(fb.nodes(), urlsplit(fb.url()).path)
                if urlsplit(fb.url()).path != urlsplit(url).path:  # le site a redirigé (mur de connexion…)
                    status, body = 303, ""
            headers = {"Set-Cookie": [f"fs_tab={tab}; Path=/; HttpOnly"]}
            if fb.cookies.get("session"):
                headers["Set-Cookie"].append(f"fs_session={fb.cookies['session']}; Path=/; Max-Age=2592000; HttpOnly")
            if status == 303:
                u = urlsplit(fb.url())
                headers["Location"] = u.path + (f"?{u.query}" if u.query else "")
            return status, headers, body


    def control(self, method: str, path: str) -> tuple[int, dict, str]:
        """Commandes du banc (`FAKESOCIAL_CONTROL=1` seulement) : ce qu'une personne ferait ailleurs que dans l'onglet
        (résoudre l'écran inconnu, valider dans l'application) ou ce que le site décide (activité inhabituelle)."""
        if os.environ.get("FAKESOCIAL_CONTROL") != "1" or method != "POST":
            return 404, {}, ""
        parts = urlsplit(path)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        with self.lock:
            if parts.path == "/__test/unusual":
                self.server.unusual = q.get("on") == "1"
            elif parts.path == "/__test/solve":
                self.server.captcha_solved.add(q.get("u", ""))
            elif parts.path == "/__test/approve":
                self.server.app_approved.add(q.get("u", ""))
            else:
                return 404, {}, ""
        return 204, {}, ""


def make_handler(app: FakeSocialHttp) -> type[BaseHTTPRequestHandler]:
    class H(BaseHTTPRequestHandler):
        def _go(self, method: str) -> None:
            cookies = {}
            for c in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = c.strip().partition("=")
                if k:
                    cookies[k] = v
            form: dict[str, str] = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode("utf-8"), keep_blank_values=True).items()}
            status, headers, body = app.handle(method, self.path, cookies, form)
            raw = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            for k, v in headers.items():
                for one in (v if isinstance(v, list) else [v]):
                    self.send_header(k, one)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            self._go("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._go("POST")

        def log_message(self, *a: Any) -> None:
            pass

    return H


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skin", choices=("fakesocial", "linkedin"), default="linkedin")
    ap.add_argument("--host", default="")  # nom servi (défaut : celui de l'habillage) ; le test met le nom du réseau
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--mfa", default="none")
    ap.add_argument("--account", action="append", default=[],
                    help="comptes de plus : identifiant:mot_de_passe:mfa:Nom affiché (banc seulement)")
    a = ap.parse_args(argv)
    skin = dict(LINKEDIN_FR if a.skin == "linkedin" else FAKESOCIAL)
    if a.host:
        skin["host"] = a.host
    server = FakeServer(skin=skin).seed()
    server.accounts["alex@example.org"] = Account("alex@example.org", "Mdp-Test-91xQ", "Alex Martin", mfa=a.mfa)
    for spec in a.account:
        user, pw, mfa, display = spec.split(":", 3)
        server.accounts[user] = Account(user, pw, display, mfa=mfa)
    ThreadingHTTPServer(("0.0.0.0", a.port), make_handler(FakeSocialHttp(server))).serve_forever()


if __name__ == "__main__":
    main()
