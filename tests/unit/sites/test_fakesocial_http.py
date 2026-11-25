"""fakesocial servi en HTTP (§13.3 étape 1) : rendu accessible, session en cookie, gestes par formulaire."""
from __future__ import annotations

from nokker.sites.fakesocial import LINKEDIN_FR, Account, FakeServer
from nokker.sites.fakesocial_http import FakeSocialHttp
from nokker.sites.recipes.base import SocialRecipe

HOST = "fakesocial:8080"


def _app(mfa: str = "none") -> FakeSocialHttp:
    srv = FakeServer(skin=dict(LINKEDIN_FR, host=HOST)).seed()
    srv.accounts["alex@example.org"] = Account("alex@example.org", "Mdp-Test-91xQ", "Alex Martin", mfa=mfa)
    return FakeSocialHttp(srv)


def _cookies(headers: dict, jar: dict) -> dict:
    for c in headers.get("Set-Cookie", []):
        k, _, v = c.split(";")[0].partition("=")
        jar[k] = v
    return jar


def test_login_page_renders_labelled_fields_and_password_type():
    st, h, body = _app().handle("GET", "/login", {}, {})
    assert st == 200 and 'type="password"' in body and "E-mail ou téléphone" in body and "S&#x27;identifier" in body


def test_login_sets_a_session_cookie_then_the_feed_is_reachable():
    app, jar = _app(), {}
    _st, h, _b = app.handle("GET", "/login", jar, {})
    _cookies(h, jar)
    st, h, _ = app.handle("POST", "/__act", jar, {"f_e1": "alex@example.org", "f_e2": "Mdp-Test-91xQ", "__click": "e4"})
    _cookies(h, jar)
    assert st == 303 and h["Location"] == "/feed/" and jar.get("fs_session")
    st, _h, body = app.handle("GET", "/feed/", {"fs_session": jar["fs_session"]}, {})  # nouvel onglet, même profil
    assert st == 200 and "Moi — Alex Martin" in body


def test_favicon_never_moves_the_tab_to_the_login_wall():
    app, jar = _app("code"), {}
    _st, h, _ = app.handle("GET", "/login", jar, {})
    _cookies(h, jar)
    _st, h, _ = app.handle("POST", "/__act", jar, {"f_e1": "alex@example.org", "f_e2": "Mdp-Test-91xQ", "__click": "e4"})
    _cookies(h, jar)
    app.handle("GET", "/checkpoint/code", jar, {})
    assert app.handle("GET", "/favicon.ico", jar, {})[0] == 404
    st, h, _ = app.handle("POST", "/__act", jar, {"f_e2": "482913", "__click": "e3"})
    assert st == 303 and h["Location"] == "/feed/"


def test_recipe_fallbacks_without_role_hints():
    from nokker.sites.driver import Node

    r = SocialRecipe(site="x", name="X", hosts=("h",), labels=dict(LINKEDIN_FR))

    class Nav:
        def goto(self, url):
            pass

        def nodes(self):
            return [Node("e1", "heading", "Sam Martin"), Node("e2", "paragraph", "", "Directeur"),
                    Node("e3", "paragraph", "", "2e"), Node("e4", "article", "Alex", "Le post"),
                    Node("e5", "article", "Camille", "Bravo !")]

    _kind, prof = r.read(Nav(), "in", "https://h/in/sam/")
    assert prof[0].fields["headline"] == "Directeur" and prof[0].meta["degree"] == "2e"
    _kind, post = r.read(Nav(), "post", "https://h/feed/update/p1/")
    assert [(i.ref_kind, i.fields["author"]) for i in post] == [("post", "Alex"), ("comment", "Camille")]
