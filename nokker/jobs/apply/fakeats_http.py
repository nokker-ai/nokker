"""Portails de candidature simulés servis en HTTP, pour le VRAI Chrome de `sites-chrome` (e2e du lot L2, comme
`nokker.sites.fakesocial_http` pour la recette LinkedIn).

Même état que `fakeats.FakeAtsBrowser`, rendu en HTML accessible : chaque nœud devient l'élément dont Chrome tire le
même rôle et le même nom (libellé de champ, liste déroulante et ses options, case, champ fichier avec le nom du fichier
joint affiché dessous, boutons). Les gestes reviennent par un formulaire unique (`POST /__act`, multipart : valeurs,
cases, fichiers, puis le bouton cliqué). La session du portail est un vrai cookie (`fa_session`, gardé dans le profil) ;
l'état de passage (étape, valeurs, fichiers) vit côté serveur, par onglet (`fa_tab`).

Test seulement : jamais servi par Lokkal.
`python -m nokker.jobs.apply.fakeats_http --skin workday --host fakeats:8080 --port 8080`.
"""
from __future__ import annotations

import argparse
import html
import json
import secrets
import threading
from email.parser import BytesParser
from email.policy import default as _policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from nokker.jobs.apply.fakeats import SUCCESSFACTORS, WORKDAY, FakeAtsBrowser, FakeAtsServer
from nokker.sites.driver import Node

_PAGE = ("<!doctype html><html lang=fr><head><meta charset=utf-8><title>{title}</title></head><body>"
         "<form method=post action=/__act enctype=multipart/form-data><main>{body}</main></form></body></html>")


def _e(s: Any) -> str:
    return html.escape(str(s or ""), quote=True)


def render(nodes: list[Node], title: str) -> str:
    out: list[str] = []
    for n in nodes:
        a, name, value = n.attrs, _e(n.name), _e(n.value)
        if n.role == "alert":
            out.append(f'<p role="alert">{name}</p>')
        elif n.role == "heading":
            out.append(f"<h1>{name}</h1>")
        elif n.role == "text":
            out.append(f"<p>{name}</p>")
        elif n.role == "article":
            out.append(f'<article aria-label="{name}"><p>{value}</p></article>')
        elif n.role == "iframe":
            out.append(f'<iframe title="{name}" src="about:blank"></iframe>')
        elif n.role == "link":
            out.append(f'<p><a href="{_e(a.get("href"))}">{name}</a></p>')
        elif n.role == "textbox":
            typ = "password" if a.get("type") == "password" else "text"
            out.append(f'<p><label>{name} <input name="f_{n.ref}" type="{typ}" value="{value}" '
                       f'autocomplete="off"></label></p>')
        elif n.role == "combobox":
            opts = [o for o in a.get("options", "").split("|") if o]
            body = '<option value="">—</option>' + "".join(
                f'<option value="{_e(o)}"{" selected" if o == n.value else ""}>{_e(o)}</option>' for o in opts)
            out.append(f'<p><label>{name} <select name="f_{n.ref}">{body}</select></label></p>')
        elif n.role == "checkbox":
            checked = " checked" if a.get("checked") == "true" else ""
            disabled = " disabled" if a.get("disabled") == "true" else ""
            out.append(f'<p><label><input type="checkbox" name="c_{n.ref}" value="1"{checked}{disabled}> {name}'
                       f'</label><input type="hidden" name="h_{n.ref}" value="1"></p>')
        elif n.role == "button" and a.get("file") == "1":
            shown = f"<p>Fichier joint : {value}</p>" if n.value else ""
            out.append(f'<p><label>{name} <input type="file" name="u_{n.ref}"></label></p>{shown}')
        elif n.role == "button":
            out.append(f'<button name="__click" value="{n.ref}">{name}</button>')
    return _PAGE.format(title=_e(title), body="".join(out))


class FakeAtsHttp:
    def __init__(self, server: FakeAtsServer) -> None:
        self.server = server
        self.tabs: dict[str, FakeAtsBrowser] = {}
        self.lock = threading.Lock()

    def browser(self, cookies: dict[str, str]) -> tuple[str, FakeAtsBrowser]:
        tab = cookies.get("fa_tab", "")
        fb = self.tabs.get(tab)
        if fb is None:
            tab = secrets.token_hex(8)
            fb = self.tabs[tab] = FakeAtsBrowser(self.server)
        sess = cookies.get("fa_session", "")
        if sess:
            fb.cookies["session"] = sess
        else:
            fb.cookies.pop("session", None)
        return tab, fb

    def handle(self, method: str, path: str, cookies: dict[str, str], form: dict[str, Any]) -> tuple[int, dict, str]:
        if method == "GET" and urlsplit(path).path in ("/favicon.ico", "/robots.txt"):
            return 404, {}, ""
        if method == "GET" and urlsplit(path).path == "/__state":  # vérification du test (aucune donnée de page)
            return 200, {"Content-Type": "application/json"}, json.dumps({
                "submissions": [s.__dict__ for s in self.server.submissions], "outbox": self.server.outbox,
                "accounts": sorted(self.server.accounts), "typed_count": len(self.server.typed)})
        if method == "POST" and urlsplit(path).path == "/__captcha":  # « la personne a fait la vérification »
            self.server.captcha_solved = True
            return 200, {}, "ok"
        with self.lock:
            tab, fb = self.browser(cookies)
            base = self.server.base()
            if method == "POST" and path == "/__act":
                nodes = {n.ref: n for n in fb.nodes()}
                for k, v in form.items():
                    if k.startswith("f_") and k[2:] in nodes and isinstance(v, str):
                        n = nodes[k[2:]]
                        if n.role == "combobox":
                            if v:
                                fb.select(k[2:], v)
                        elif v and v != n.value:
                            if n.value:
                                fb.clear(k[2:])
                            fb.type(k[2:], v)
                    elif k.startswith("u_") and k[2:] in nodes and isinstance(v, tuple) and v[0]:
                        fb.upload(k[2:], v[0], v[1])
                for ref, n in nodes.items():  # cases : cochées si postées, décochées sinon (champs actifs)
                    if n.role == "checkbox" and f"h_{ref}" in form and n.attrs.get("disabled") != "true" \
                            and (n.attrs.get("checked") == "true") != (f"c_{ref}" in form):
                        fb.check(ref, f"c_{ref}" in form)
                ref = form.get("__click", "")
                if isinstance(ref, str) and ref in nodes:
                    fb.click(ref)
                status, body = 303, ""
            else:
                url = base + path
                cur = urlsplit(fb.url()).path
                if urlsplit(url).path != cur or path.startswith("/job/"):
                    fb.goto(url)
                status = 200
                body = render(fb.nodes(), urlsplit(fb.url()).path)
                if urlsplit(fb.url()).path != urlsplit(url).path:
                    status, body = 303, ""
            headers = {"Set-Cookie": [f"fa_tab={tab}; Path=/; HttpOnly"]}
            if fb.cookies.get("session"):
                headers["Set-Cookie"].append(f"fa_session={fb.cookies['session']}; Path=/; Max-Age=2592000; HttpOnly")
            if status == 303:
                u = urlsplit(fb.url())
                headers["Location"] = u.path + (f"?{u.query}" if u.query else "")
            return status, headers, body


def parse_form(ctype: str, raw: bytes) -> dict[str, Any]:
    """Champs d'un formulaire : texte → str ; fichier → (nom, octets)."""
    if ctype.startswith("multipart/form-data"):
        msg = BytesParser(policy=_policy).parsebytes(f"Content-Type: {ctype}\r\n\r\n".encode() + raw)
        out: dict[str, Any] = {}
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            data = part.get_payload(decode=True) or b""
            out[name] = (filename or "", data) if filename is not None else data.decode("utf-8")
        return out
    return {k: v[0] for k, v in parse_qs(raw.decode("utf-8"), keep_blank_values=True).items()}


def make_handler(app: FakeAtsHttp) -> type[BaseHTTPRequestHandler]:
    class H(BaseHTTPRequestHandler):
        def _go(self, method: str) -> None:
            cookies = {}
            for c in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = c.strip().partition("=")
                if k:
                    cookies[k] = v
            form: dict[str, Any] = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                form = parse_form(self.headers.get("Content-Type") or "", self.rfile.read(n))
            status, headers, body = app.handle(method, self.path, cookies, form)
            raw = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", headers.pop("Content-Type", "text/html; charset=utf-8"))
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


def serve(skin: str, host: str, port: int, captcha: str = "") -> None:
    s = dict(WORKDAY if skin == "workday" else SUCCESSFACTORS)
    if host:
        s["host"] = host
    server = FakeAtsServer(skin=s, captcha_on=captcha)
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(FakeAtsHttp(server))).serve_forever()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skin", choices=("workday", "successfactors"), default="workday")
    ap.add_argument("--host", default="")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--captcha", default="")
    a = ap.parse_args(argv)
    serve(a.skin, a.host, a.port, a.captcha)


if __name__ == "__main__":
    main()
