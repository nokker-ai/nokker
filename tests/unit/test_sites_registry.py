"""Image privée `sites-chrome` (docs/CONNECTED-SITES.md §3.1.1, nokker/cloud/sites_registry.py) contre un FAUX Harbor
(httpx.MockTransport) : création du robot de lecture du compte, renouvellement du même robot (jamais un second),
quota de 6 par jour, 503 sans identifiants dans l'env, jeton de téléphone refusé, suppression à la déliaison de la
dernière machine, à l'effacement du compte et par la console ; le secret n'apparaît dans aucun journal. Côté Lokkal :
le noyau local relaie la demande par le lien et rend les identifiants à la CLI, sans rien en garder."""
from __future__ import annotations

import base64
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import httpx
import pytest

from nokker.api.server import make_handler
from nokker.cloud import http as cloud_http
from nokker.cloud.http import CloudLink
from nokker.cloud.link import LinkService, Signer
from nokker.cloud.sites_registry import HarborIssuer, SitesRegistry, SitesRegistryError, iso_utc
from tests.unit.api.test_server import make_kernel
from tests.unit.test_cloud_http import _call

ISSUER_PW = "issuer-PW-0123456789"
NOW = 2_000_000_000.0


class FakeHarbor:
    """API Harbor v2.0 réduite à ce que l'émetteur appelle, avec ses règles : nom complet `robot$<projet>+<nom>`,
    409 sur un nom pris, échéance = création + durée (jours), secret régénéré par PATCH."""

    def __init__(self, clock=lambda: NOW):
        self.robots: dict[int, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.next_id = 40
        self.secrets = 0
        self.down = False
        self.clock = clock

    def _secret(self) -> str:
        self.secrets += 1
        return f"RobotSecret{self.secrets:04d}xyz"

    def handler(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("injoignable")
        assert req.headers["Authorization"] == "Basic " + base64.b64encode(
            f"lokkal-issuer:{ISSUER_PW}".encode()).decode()
        path = req.url.path.removeprefix("/api/v2.0")
        self.calls.append((req.method, path))
        # Comme le vrai Harbor : un cookie `sid` renvoyé fait de la requête une session navigateur, et toute écriture
        # exige alors un jeton CSRF (403 « CSRF token not found », vu en prod le 06.11.2026).
        if req.method != "GET" and "sid=" in req.headers.get("cookie", ""):
            return httpx.Response(403, json={"errors": [{"code": "FORBIDDEN",
                                                         "message": "CSRF token not found in request"}]})
        resp = self._route(req, path)
        resp.headers["set-cookie"] = "sid=fake-session; Path=/; HttpOnly"
        return resp

    def _route(self, req: httpx.Request, path: str) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        if req.method == "GET" and path == "/projects/lokkal-private":
            return httpx.Response(200, json={"project_id": 7, "name": "lokkal-private"})
        if req.method == "GET" and path == "/robots":
            q = req.url.params["q"]
            assert "Level=project" in q and "ProjectID=7" in q
            frag = q.split("name=~", 1)[1]
            return httpx.Response(200, json=[{k: v for k, v in r.items() if k != "secret"}
                                             for r in self.robots.values() if frag in r["name"]])
        if req.method == "POST" and path == "/robots":
            assert body["level"] == "project" and body["permissions"] == [
                {"kind": "project", "namespace": "lokkal-private",
                 "access": [{"resource": "repository", "action": "pull"}]}]
            full = f"robot$lokkal-private+{body['name']}"
            if any(r["name"] == full for r in self.robots.values()):
                return httpx.Response(409, json={"errors": [{"code": "CONFLICT"}]})
            rid, self.next_id = self.next_id, self.next_id + 1
            created = datetime.fromtimestamp(self.clock(), timezone.utc)
            self.robots[rid] = {"id": rid, "name": full, "level": "project", "duration": body["duration"],
                                "description": body["description"], "disable": False, "editable": True,
                                "permissions": body["permissions"], "secret": self._secret(),
                                "creation_time": created.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                                "expires_at": int(created.timestamp()) + body["duration"] * 86400}
            r = self.robots[rid]
            return httpx.Response(201, json={k: r[k] for k in ("id", "name", "secret", "creation_time",
                                                                "expires_at")})
        if path.startswith("/robots/"):
            rid = int(path.rsplit("/", 1)[1])
            r = self.robots.get(rid)
            if r is None:
                return httpx.Response(404, json={})
            if req.method == "GET":
                return httpx.Response(200, json={k: v for k, v in r.items() if k != "secret"})
            if req.method == "PUT":
                assert body["name"] == r["name"] and body["level"] == r["level"]
                if body["duration"] != r["duration"]:
                    created = datetime.fromisoformat(r["creation_time"].replace("Z", "+00:00"))
                    r["expires_at"] = int(created.timestamp()) + body["duration"] * 86400
                r.update(duration=body["duration"], disable=body["disable"], permissions=body["permissions"])
                return httpx.Response(200, json={})
            if req.method == "PATCH":
                assert body == {"secret": ""}
                r["secret"] = self._secret()
                return httpx.Response(200, json={"secret": r["secret"]})
            if req.method == "DELETE":
                del self.robots[rid]
                return httpx.Response(200, json={})
        return httpx.Response(404, json={})


def _issuer(harbor: FakeHarbor, clock=lambda: NOW) -> HarborIssuer:
    return HarborIssuer(url="https://registry.example.test", user="lokkal-issuer", password=ISSUER_PW,
                        client=httpx.Client(transport=httpx.MockTransport(harbor.handler)), clock=clock)


@pytest.fixture
def world():
    harbor = FakeHarbor()
    reg = SitesRegistry(issuer=_issuer(harbor))
    links = LinkService()
    cloud_http.configure(CloudLink(
        links=links, signer=Signer.generate(),
        identity=lambda h: {"Bearer sess-pierre": "us_pierre", "Bearer sess-anna": "us_anna"}.get(
            h.headers.get("Authorization")),
        tenant_of=lambda acc: {"us_pierre": "tn_pierre", "us_anna": "tn_anna"}.get(acc),
        router_base="http://127.0.0.1:9/v1", router_key=lambda: "rk", sites_registry=reg))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), {}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield {"port": httpd.server_address[1], "harbor": harbor, "reg": reg, "links": links}
    cloud_http.configure(None)
    httpd.shutdown()


def _link(port, name="atelier", session="sess-pierre"):
    dev = _call(port, "/v1/link/device", {"machine": name})[1]
    _call(port, "/v1/link/approve", {"user_code": dev["user_code"]}, token=session)
    return _call(port, "/v1/link/token", {"device_code": dev["device_code"]})[1]


def _issue(port, token):
    return _call(port, "/v1/lokkal/sites-registry", {}, token=token)


def test_first_call_creates_a_read_only_robot_for_the_account(world, caplog):
    caplog.set_level(logging.DEBUG)
    port, harbor = world["port"], world["harbor"]
    tok = _link(port)
    status, body = _issue(port, tok["machine_token"])
    assert status == 200 and set(body) == {"registry", "username", "password", "expires_at"}
    assert body["registry"] == "registry.example.test"
    assert body["username"] == "robot$lokkal-private+lokkal-us_pierre"
    assert body["password"] == harbor.robots[40]["secret"]
    assert body["expires_at"] == iso_utc(int(NOW) + 30 * 86400) == "2033-06-17T03:33:20Z"
    assert harbor.robots[40]["duration"] == 30
    assert body["password"] not in caplog.text and ISSUER_PW not in caplog.text
    assert "créé pour us_pierre" in caplog.text  # journal : action, compte, machine, échéance ; sans secret


def test_second_call_renews_the_same_robot_with_a_new_secret(world, caplog):
    caplog.set_level(logging.DEBUG)
    port, harbor = world["port"], world["harbor"]
    tok = _link(port)
    first = _issue(port, tok["machine_token"])[1]
    # 10 jours plus tard : même robot, nouveau secret, échéance repoussée à ~30 jours de maintenant
    world["reg"].issuer.clock = lambda: NOW + 10 * 86400
    status, second = _issue(port, tok["machine_token"])
    assert status == 200 and len(harbor.robots) == 1 and list(harbor.robots) == [40]
    assert second["username"] == first["username"] and second["password"] != first["password"]
    assert second["password"] == harbor.robots[40]["secret"]
    assert second["expires_at"] == iso_utc(int(NOW) + 40 * 86400)
    # une seconde machine du même compte : toujours le même robot
    tok2 = _link(port, "portable")
    assert _issue(port, tok2["machine_token"])[1]["username"] == first["username"]
    assert len(harbor.robots) == 1 and ("POST", "/robots") in harbor.calls
    assert sum(1 for c in harbor.calls if c == ("POST", "/robots")) == 1
    for secret in (first["password"], second["password"], ISSUER_PW):
        assert secret not in caplog.text


def test_a_robot_created_elsewhere_is_renewed_not_duplicated(world):
    """Course entre deux réplicas : la création répond 409 → le robot existant est retrouvé et renouvelé."""
    harbor = world["harbor"]
    issuer = world["reg"].issuer
    issuer.issue("us_pierre")
    real_find, seen = issuer.find, []

    def find_once_missing(account):  # la première recherche ne le voit pas encore
        seen.append(account)
        return None if len(seen) == 1 else real_find(account)

    issuer.find = find_once_missing
    out = issuer.issue("us_pierre")
    assert out["renewed"] and len(harbor.robots) == 1


def test_quota_six_per_day_per_account(world):
    port, harbor = world["port"], world["harbor"]
    tok = _link(port)
    for _ in range(6):
        assert _issue(port, tok["machine_token"])[0] == 200
    status, body = _issue(port, tok["machine_token"])
    assert status == 429 and body["error"]["code"] == "sites_registry_quota" and body["retry_after_s"] > 0
    assert "password" not in body
    # un autre compte n'est pas touché
    other = _link(port, "chez-anna", "sess-anna")
    assert _issue(port, other["machine_token"])[0] == 200
    assert len(harbor.robots) == 2


def test_without_issuer_env_the_route_says_503(world, monkeypatch):
    for k in ("NOKKER_HARBOR_ISSUER_URL", "NOKKER_HARBOR_ISSUER_USER", "NOKKER_HARBOR_ISSUER_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    reg = SitesRegistry.from_env()
    assert reg.issuer is None and not reg.available
    cloud_http._cloud.sites_registry = reg
    tok = _link(world["port"])
    status, body = _issue(world["port"], tok["machine_token"])
    assert status == 503 and body["error"]["code"] == "sites_registry_unavailable"
    cloud_http._cloud.sites_registry = None
    assert _issue(world["port"], tok["machine_token"])[0] == 503
    # Les trois variables posées : allumé, registre = hôte de l'URL
    env = {"NOKKER_HARBOR_ISSUER_URL": "https://reg.nokker.test/", "NOKKER_HARBOR_ISSUER_USER": "lokkal-issuer",
           "NOKKER_HARBOR_ISSUER_PASSWORD": ISSUER_PW}
    on = SitesRegistry.from_env(env)
    assert on.available and on.issuer.registry == "reg.nokker.test" and ISSUER_PW not in repr(on)
    assert not SitesRegistry.from_env({**env, "NOKKER_HARBOR_ISSUER_PASSWORD": ""}).available


def test_machine_token_required_and_phone_token_refused(world):
    port, harbor = world["port"], world["harbor"]
    assert _issue(port, None)[0] == 401
    assert _issue(port, "sess-pierre")[0] == 401  # une session du compte n'est pas un jeton de machine
    tok = _link(port, "telephone")
    m = world["links"].machine_for_token(tok["machine_token"])
    m.kind = "phone"  # nœud téléphone (P7a, migration 077) : son jeton ne vaut pas ici
    status, body = _issue(port, tok["machine_token"])
    assert status == 403 and body["error"]["code"] == "phone_scope"
    assert harbor.robots == {}


def test_harbor_down_is_502_without_detail(world, caplog):
    caplog.set_level(logging.DEBUG)
    port, harbor = world["port"], world["harbor"]
    tok = _link(port)
    harbor.down = True
    status, body = _issue(port, tok["machine_token"])
    assert status == 502 and body["error"]["code"] == "sites_registry_upstream"
    assert ISSUER_PW not in caplog.text


def test_robot_deleted_when_the_last_machine_is_unlinked(world):
    port, harbor = world["port"], world["harbor"]
    a, b = _link(port, "atelier"), _link(port, "portable")
    _issue(port, a["machine_token"])
    assert len(harbor.robots) == 1
    # une machine reste liée : le robot reste
    assert _call(port, "/v1/link/revoke", {}, token=a["machine_token"])[1] == {"revoked": True}
    assert len(harbor.robots) == 1
    # la dernière part (depuis l'écran « mes machines ») : le robot est supprimé
    st, _ = _call(port, f"/v1/link/machines/{b['machine_id']}/revoke", {}, token="sess-pierre")
    assert st == 200 and harbor.robots == {}
    # déliaison alors que Harbor ne répond pas : la déliaison passe quand même
    c = _link(port, "salon")
    _issue(port, c["machine_token"])
    harbor.down = True
    assert _call(port, "/v1/link/revoke", {}, token=c["machine_token"])[1] == {"revoked": True}


def test_erase_account_deletes_the_robot_before_the_commit(monkeypatch):
    """Chemin d'effacement existant (`erase_account`, portée `erase_scope`) : à blanc, le robot est compté ; validé,
    il est supprimé avant la validation ; Harbor injoignable → EraseError (rien n'est validé, tout se rejoue)."""
    from nokker.admin import erase as erase_mod

    harbor = FakeHarbor()
    reg = SitesRegistry(issuer=_issuer(harbor))
    reg.issuer.issue("us_cccccccccccc")
    committed = []

    def fake_db(dsn, *, email, account_id, apply, confirm, session_memory=None, beta_stripe=None, before_commit=None):
        rep = erase_mod.Report(account_id=account_id, tenant_id="tn_cccccccccccc", email="c@x.test", devices=[])
        if apply and before_commit is not None:
            try:
                before_commit(rep)
            except Exception as e:  # noqa: BLE001 — comme _erase_db : rien n'est validé
                raise erase_mod.EraseError(str(e)) from None
        rep.applied = apply
        if apply:
            committed.append(account_id)
        return rep

    monkeypatch.setattr(erase_mod, "_erase_db", fake_db)
    dry = erase_mod.erase_account("dsn", account_id="us_cccccccccccc", sites_registry=reg)
    assert dry.deleted[erase_mod.SITES_REGISTRY_KEY] == 1 and len(harbor.robots) == 1
    harbor.down = True
    with pytest.raises(erase_mod.EraseError) as e:
        erase_mod.erase_account("dsn", account_id="us_cccccccccccc", apply=True, confirm="us_cccccccccccc",
                                sites_registry=reg)
    assert committed == [] and len(harbor.robots) == 1 and ISSUER_PW not in str(e.value)
    harbor.down = False
    done = erase_mod.erase_account("dsn", account_id="us_cccccccccccc", apply=True, confirm="us_cccccccccccc",
                                   sites_registry=reg)
    assert done.deleted[erase_mod.SITES_REGISTRY_KEY] == 1 and harbor.robots == {} and committed == ["us_cccccccccccc"]
    # sans robot : rien à supprimer, l'effacement passe
    again = erase_mod.erase_account("dsn", account_id="us_cccccccccccc", apply=True, confirm="us_cccccccccccc",
                                    sites_registry=reg)
    assert again.deleted[erase_mod.SITES_REGISTRY_KEY] == 0


def test_erase_scope_carries_the_registry_only_where_nokker_ai_issues(monkeypatch):
    from nokker.admin.erase import erase_scope
    from nokker.config import NokkerConfig

    monkeypatch.setenv("NOKKER_HARBOR_ISSUER_URL", "https://reg.nokker.test")
    monkeypatch.setenv("NOKKER_HARBOR_ISSUER_USER", "lokkal-issuer")
    monkeypatch.setenv("NOKKER_HARBOR_ISSUER_PASSWORD", ISSUER_PW)
    cfg = NokkerConfig()
    assert erase_scope(cfg)["sites_registry"] is None  # pas de cloud_link : pas nokker.ai
    cfg.cloud_link = {"signing_key_env": "X"}
    assert erase_scope(cfg)["sites_registry"].available
    monkeypatch.delenv("NOKKER_HARBOR_ISSUER_PASSWORD")
    assert erase_scope(cfg)["sites_registry"] is None


def test_admin_console_revokes_the_robot_with_audit():
    from nokker.admin.server import serve_admin
    from nokker.admin.store import MemoryAdminStore
    from tests.unit.admin.test_admin import post, verifier

    harbor = FakeHarbor()
    reg = SitesRegistry(issuer=_issuer(harbor))
    reg.issuer.issue("us_pierre")
    store = MemoryAdminStore()
    store.seed_operators({"bruno@example.ch": "ops", "lire@example.ch": "lecteur"}, "system:config")
    httpd = serve_admin(store, verifier(), "127.0.0.1", 0, sites_registry=reg)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    path = "/admin/v1/accounts/us_pierre/sites-registry/revoke"
    try:
        assert post(base, path, {}, "lire@example.ch")[0] == 403
        assert post(base, path, {}, "bruno@example.ch", console_header=False)[0] == 403
        assert len(harbor.robots) == 1
        assert post(base, path, {}, "bruno@example.ch") == (200, {"revoked": True})
        assert harbor.robots == {}
        assert post(base, path, {}, "bruno@example.ch") == (200, {"revoked": False})
        assert [a["action"] for a in store.audit(limit=10)].count("sites_registry.revoke") == 2
        harbor.down = True
        assert post(base, path, {}, "bruno@example.ch") == (502, {"error": "sites_registry_upstream"})
    finally:
        httpd.shutdown()
    bare = serve_admin(store, verifier(), "127.0.0.1", 0)
    try:
        assert post(f"http://127.0.0.1:{bare.server_address[1]}", path, {}, "bruno@example.ch") == (
            503, {"error": "sites_registry_unavailable"})
    finally:
        bare.shutdown()


def test_issuer_errors_never_carry_harbor_bodies():
    class Leaky:
        def request(self, *a, **kw):
            return httpx.Response(500, text=f"boom {ISSUER_PW}")

    issuer = HarborIssuer(url="https://r.test", user="lokkal-issuer", password=ISSUER_PW, client=Leaky())
    with pytest.raises(SitesRegistryError) as e:
        issuer.issue("us_pierre")
    assert ISSUER_PW not in str(e.value) and "500" in str(e.value)


# ── Lokkal : le noyau local relaie la demande par le lien et rend les identifiants à la CLI seulement ──

def _local_api(tmp_path, port):
    from nokker.local.api import LocalApi
    from nokker.local.setup import LocalLink, LocalSetup, LocalVault

    root = tmp_path / "projets"
    root.mkdir()
    api = LocalApi.from_config({"state_dir": str(tmp_path / "etat"), "token_path": str(tmp_path / "tok"),
                                "code": {"roots": [str(root)], "backend": "local"}}, port=8742)
    api.setup = LocalSetup(state_path=tmp_path / "setup.json", vault=LocalVault(tmp_path / "vault.bin"),
                           detect=lambda: [])
    api.link = LocalLink(setup=api.setup, base_url=f"http://127.0.0.1:{port}", machine_arch=lambda: "x86_64")
    return api


LOCAL = "/v1/local/sites/registry"


def _code(out):
    status, body = out
    assert set(body) <= {"error", "retry_after_s"} and set(body["error"]) == {"code", "message"}, body
    return status, body["error"]["code"]


def test_local_core_relays_to_the_cloud_and_keeps_nothing(world, tmp_path, monkeypatch, caplog):
    from cryptography.fernet import Fernet

    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("NOKKER_LOCAL_SECRET_KEY", Fernet.generate_key().decode())
    port, harbor = world["port"], world["harbor"]
    tok = _link(port)
    api = _local_api(tmp_path, port)
    auth = f"Bearer {api.token}"
    # pas lié : 409 not_linked, rien demandé à nokker.ai
    assert _code(api.handle("POST", LOCAL, auth, {})) == (409, "not_linked")
    api.setup.vault.put("machine_token", tok["machine_token"])
    assert api.handle("POST", LOCAL, None, {})[0] == 401  # jeton local requis
    assert api.handle("POST", LOCAL, "Bearer autre", {})[0] == 401
    assert _code(api.handle("GET", LOCAL, auth, {})) == (405, "method")
    assert api.handle("POST", LOCAL, auth, {}, relayed=True)[0] in (401, 403)  # jamais par la passerelle
    assert harbor.robots == {}
    status, body = api.handle("POST", LOCAL, auth, {})
    assert status == 200 and set(body) == {"registry", "username", "password", "expires_at"}
    assert body["password"] == harbor.robots[40]["secret"]
    assert body["registry"] == "registry.example.test" and body["expires_at"].endswith("Z")
    _sed_reads(body)
    # rien gardé : ni l'état, ni le coffre, ni aucun fichier de la machine, ni le journal
    assert set(api.setup.vault.names()) == {"machine_token"}
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert body["password"].encode() not in f.read_bytes(), f
    assert body["password"] not in caplog.text


def _sed_reads(body):
    """Ce que fait l'installeur (deploy/lokkal/install.sh, `sites_field`) : `sed` sur la réponse JSON d'une ligne."""
    import re

    raw = json.dumps(body, ensure_ascii=False)  # ce que le noyau écrit (`_json`)
    assert "\n" not in raw
    for k, v in body.items():
        m = re.search(rf'"{k}": *"([^"]*)"', raw)
        assert m and m.group(1) == v, k


@pytest.mark.parametrize("bad", ['pw"x', "pw\\x", "pw\nx", "pw\rx", "pw x", "", "pw\x00x", "pwé"])
def test_local_route_never_returns_a_value_sed_cannot_read(tmp_path, monkeypatch, bad):
    """Un secret (ou tout champ) que `sed` lirait de travers n'est jamais rendu : 503, jamais du JSON échappé."""
    from cryptography.fernet import Fernet

    from nokker.local.setup import LocalLink, LocalSetup, LocalVault

    monkeypatch.setenv("NOKKER_LOCAL_SECRET_KEY", Fernet.generate_key().decode())
    good = {"registry": "registry.example.test:5000", "username": "robot$lokkal-private+lokkal-us_x",
            "password": "Abc123secret", "expires_at": "2026-12-06T00:00:00Z"}
    setup = LocalSetup(state_path=tmp_path / "setup.json", vault=LocalVault(tmp_path / "vault.bin"), detect=lambda: [])
    setup.vault.put("machine_token", "mt")
    for field in good:
        answer = {**good, field: bad}
        client = httpx.Client(transport=httpx.MockTransport(lambda r, a=answer: httpx.Response(200, json=a)))
        link = LocalLink(setup=setup, client=client, machine_arch=lambda: "x86_64")
        status, body = link.sites_registry()
        assert (status, body["error"]["code"]) == (503, "sites_registry_unavailable"), field
        assert "password" not in body
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=good)))
    out = LocalLink(setup=setup, client=client, machine_arch=lambda: "x86_64").sites_registry()
    assert out == (200, good)
    _sed_reads(out[1])
    # un registre avec un chemin ou un schéma n'est pas un hôte
    for reg in ("https://r.test", "r.test/lokkal-private"):
        client = httpx.Client(transport=httpx.MockTransport(lambda r, g={**good, "registry": reg}: httpx.Response(200, json=g)))
        assert LocalLink(setup=setup, client=client, machine_arch=lambda: "x86_64").sites_registry()[0] == 503


def test_local_route_error_codes(world, tmp_path, monkeypatch):
    """Contrat de la route locale (§3.1.1) : `{error: {code, message}}`, codes unsupported_arch, not_linked,
    quota_exceeded (+ retry_after_s), forbidden, sites_registry_unavailable, link_unavailable."""
    from cryptography.fernet import Fernet

    monkeypatch.setenv("NOKKER_LOCAL_SECRET_KEY", Fernet.generate_key().decode())
    port, harbor = world["port"], world["harbor"]
    tok = _link(port)
    api = _local_api(tmp_path, port)
    auth = f"Bearer {api.token}"
    api.setup.vault.put("machine_token", tok["machine_token"])
    # hôte arm64 : refus AVANT tout appel à nokker.ai (même lié)
    seen = []
    api.link.client = httpx.Client(transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(500)))
    for arch in ("aarch64", "arm64"):
        api.link.machine_arch = lambda a=arch: a
        assert _code(api.handle("POST", LOCAL, auth, {})) == (400, "unsupported_arch")
    assert seen == []
    api.link.machine_arch, api.link.client = (lambda: "x86_64"), None
    # quota : 6 puis 429 quota_exceeded avec retry_after_s
    for _ in range(6):
        assert api.handle("POST", LOCAL, auth, {})[0] == 200
    out = api.handle("POST", LOCAL, auth, {})
    assert _code(out) == (429, "quota_exceeded") and out[1]["retry_after_s"] > 0
    # registre indisponible (émetteur non configuré, ou Harbor injoignable)
    world["reg"]._hits.clear()
    cloud_http._cloud.sites_registry = SitesRegistry(issuer=None)
    assert _code(api.handle("POST", LOCAL, auth, {})) == (503, "sites_registry_unavailable")
    cloud_http._cloud.sites_registry = world["reg"]
    harbor.down = True
    assert _code(api.handle("POST", LOCAL, auth, {})) == (503, "sites_registry_unavailable")
    harbor.down = False
    # compte sans droit : jeton d'un nœud téléphone
    world["links"].machine_for_token(tok["machine_token"]).kind = "phone"
    assert _code(api.handle("POST", LOCAL, auth, {})) == (403, "forbidden")
    world["links"].machine_for_token(tok["machine_token"]).kind = "lokkal"
    # lien coupé (jeton de machine révoqué côté nokker.ai) ; nokker.ai injoignable
    world["links"].revoke(tok["machine_token"])
    assert _code(api.handle("POST", LOCAL, auth, {})) == (503, "link_unavailable")
    api.link.base_url = "http://127.0.0.1:9"
    assert _code(api.handle("POST", LOCAL, auth, {})) == (503, "link_unavailable")
    assert len(harbor.robots) == 1
    # page HTML 502/503/504 du CDN (vrai 502 Cloudflare du 06.11) : registre indisponible, pas un lien coupé ;
    # un 200 non JSON reste « réponse illisible »
    for status in (502, 503, 504):
        api.link.client = httpx.Client(transport=httpx.MockTransport(
            lambda r, s=status: httpx.Response(s, text="<!DOCTYPE html><title>502: Bad gateway</title>",
                                               headers={"content-type": "text/html"})))
        assert _code(api.handle("POST", LOCAL, auth, {})) == (503, "sites_registry_unavailable"), status
    api.link.client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>")))
    assert _code(api.handle("POST", LOCAL, auth, {})) == (503, "link_unavailable")


def test_renewal_window_counts_days_since_creation():
    """L'échéance de Harbor part de la création du robot : 45 jours après, la durée devient 45 + 30."""
    harbor = FakeHarbor()
    issuer = _issuer(harbor)
    issuer.issue("us_pierre")
    issuer.clock = lambda: NOW + 45 * 86400 + 3600
    out = issuer.issue("us_pierre")
    assert harbor.robots[40]["duration"] == 75 and out["expires_at"] == int(NOW) + 75 * 86400
    assert out["expires_at"] - (NOW + 45 * 86400) == pytest.approx(30 * 86400, abs=86400)
    assert timedelta(seconds=out["expires_at"] - NOW).days == 75
