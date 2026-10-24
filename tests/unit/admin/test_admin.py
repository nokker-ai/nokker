"""Console opérateur, lot A0 : jeton Access vérifié, rôles, journal d'audit, masquage lecteur, tirage des candidatures."""
from __future__ import annotations

import base64
import json
import time
from urllib import error as urllib_error
from urllib import request as urllib_request

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from nokker.admin.access import AccessDenied, AccessVerifier
from nokker.admin.pull import BetaPuller
from nokker.admin.server import serve_admin
from nokker.admin.store import MemoryAdminStore, normalize_application

TEAM, AUD = "example.cloudflareaccess.com", "aud-console"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def jwk(key, kid):
    n = key.public_key().public_numbers()
    return {"kty": "RSA", "kid": kid, "alg": "RS256", "e": b64(n.e.to_bytes(3, "big")),
            "n": b64(n.n.to_bytes((n.n.bit_length() + 7) // 8, "big"))}


def jwt(claims, key=KEY, kid="k1", alg="RS256"):
    head = b64(json.dumps({"alg": alg, "kid": kid}).encode())
    body = b64(json.dumps(claims).encode())
    sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{b64(sig)}"


def claims(email="alice@example.ch", **kw):
    now = time.time()
    return {"aud": [AUD], "iss": f"https://{TEAM}", "exp": now + 600, "iat": now, "nbf": now - 1, "email": email, **kw}


def verifier():
    return AccessVerifier(team_domain=TEAM, audience=AUD, fetch=lambda url: {"keys": [jwk(KEY, "k1")]})


def test_valid_token_gives_claims():
    assert verifier().verify(jwt(claims()))["email"] == "alice@example.ch"


@pytest.mark.parametrize("token", [
    "", "pas.un.jeton", jwt(claims(aud=["autre-app"])), jwt(claims(iss="https://evil.cloudflareaccess.com")),
    jwt(claims(exp=time.time() - 3600)), jwt(claims(), key=OTHER), jwt(claims(), kid="inconnu"), jwt(claims(email="")),
])
def test_invalid_tokens_are_refused(token):
    with pytest.raises(AccessDenied):
        verifier().verify(token)


def test_alg_none_is_refused():
    head = b64(json.dumps({"alg": "none", "kid": "k1"}).encode())
    with pytest.raises(AccessDenied):
        verifier().verify(f"{head}.{b64(json.dumps(claims()).encode())}.")


def test_no_audience_configured_refuses_everything():
    v = verifier()
    v.audience = ""
    with pytest.raises(AccessDenied):
        v.verify(jwt(claims()))


RAW = [{"id": 1, "ts": "2026-10-21 06:40:00", "prenom": "Léa", "nom": "Martin", "email": "Lea@Example.org",
        "pays": "Suisse", "profil": "pme", "taches": "Trier mes mails clients chaque matin et préparer les réponses.",
        "outils": "chatgpt", "dispo": "quotidien", "source": "post LinkedIn", "lang": "fr", "consent_at": "2026-10-21 06:40:00"},
       {"id": 2, "ts": "2026-10-21 07:10:00", "prenom": "Sam", "nom": "Keller", "email": "sam@example.ch",
        "pays": "Suisse", "profil": "tech", "taches": "Surveiller mes serveurs et résumer les alertes de la nuit.",
        "outils": "assistants de code", "dispo": "hebdo", "source": "hepia", "lang": "en"}]


@pytest.fixture()
def console():
    store = MemoryAdminStore()
    store.seed_operators({"alice@example.ch": "owner", "bruno@example.ch": "ops", "lire@example.ch": "lecteur"},
                         "system:config")
    store.import_applications("front1", [normalize_application(r) for r in RAW], "system:beta-pull")
    httpd = serve_admin(store, verifier(), "127.0.0.1", 0)
    yield store, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def get(base, path, email=None, token=None):
    headers = {}
    if email or token:
        headers["Cf-Access-Jwt-Assertion"] = token or jwt(claims(email))
    req = urllib_request.Request(base + path, headers=headers)
    try:
        with urllib_request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib_error.HTTPError as e:
        return e.code, e.read()


@pytest.mark.parametrize("path", ["/admin/", "/admin/v1/me", "/admin/v1/applications", "/admin/v1/applications/1",
                                  "/admin/v1/stats", "/admin/v1/audit"])
def test_every_route_refuses_without_token_or_role(console, path):
    _, base = console
    assert get(base, path)[0] == 401
    assert get(base, path, token=jwt(claims(aud=["autre-app"])))[0] == 401
    assert get(base, path, email="inconnu@example.ch")[0] == 403


def test_spoofed_email_header_is_ignored(console):
    _, base = console
    req = urllib_request.Request(base + "/admin/v1/me", headers={"Cf-Access-Authenticated-User-Email": "alice@example.ch"})
    with pytest.raises(urllib_error.HTTPError) as e:
        urllib_request.urlopen(req, timeout=5)
    assert e.value.code == 401


def test_healthz_and_favicon_are_open(console):
    _, base = console
    assert get(base, "/admin/healthz")[0] == 200
    assert get(base, "/favicon.ico")[0] == 204


def test_ops_sees_list_with_email_and_lecteur_does_not(console):
    _, base = console
    status, body = get(base, "/admin/v1/applications", email="bruno@example.ch")
    assert status == 200
    items = json.loads(body)["items"]
    assert [a["prenom"] for a in items] == ["Sam", "Léa"]  # plus récente d'abord
    assert items[1]["email"] == "lea@example.org"
    status, body = get(base, "/admin/v1/applications", email="lire@example.ch")
    text = body.decode()
    assert status == 200 and "lea@example.org" not in text and "Martin" not in text and "l***@example.org" in text


def test_viewing_a_fiche_is_audited(console):
    store, base = console
    status, body = get(base, "/admin/v1/applications/1", email="bruno@example.ch")
    assert status == 200 and json.loads(body)["taches"].startswith("Trier")
    assert any(j["action"] == "application.view" and j["actor"] == "bruno@example.ch" and j["object"] == "application:1"
               for j in store.journal)


def test_audit_is_owner_only(console):
    _, base = console
    assert get(base, "/admin/v1/audit", email="bruno@example.ch")[0] == 403
    status, body = get(base, "/admin/v1/audit", email="alice@example.ch")
    assert status == 200 and {"operator.seed", "application.import"} <= {j["action"] for j in json.loads(body)["items"]}


def test_seed_is_audited_once_and_role_change_records_before_after():
    store = MemoryAdminStore()
    store.seed_operators({"a@x.ch": "ops"}, "system:config")
    store.seed_operators({"a@x.ch": "ops"}, "system:config")
    store.seed_operators({"A@x.ch": "owner"}, "system:config")
    seeds = [j for j in store.journal if j["action"] == "operator.seed"]
    assert len(seeds) == 2 and seeds[1]["before"] == {"role": "ops"} and seeds[1]["after"] == {"role": "owner"}
    with pytest.raises(ValueError):
        store.seed_operators({"b@x.ch": "admin"}, "system:config")


class FakeFront1:
    def __init__(self, rows):
        self.rows, self.acks, self.calls = list(rows), [], []

    def __call__(self, method, url, token, body=None):
        self.calls.append((method, url, token))
        assert token == "jeton"
        if method == "GET":
            after = int(url.split("after=")[1].split("&")[0])
            return {"applications": [r for r in self.rows if r["id"] > after]}
        self.acks.append(body["ids"])
        return {"ok": True}


def test_initial_import_sends_one_summary_then_one_message_per_new_application():
    store, sent = MemoryAdminStore(), []
    front = FakeFront1(RAW)
    puller = BetaPuller(store=store, base_url="http://front1:8122", token="jeton", send=sent.append, fetch=front)
    assert puller.pull_once() == 2
    assert len(sent) == 1 and "import initial de 2" in sent[0]
    assert front.acks == [[1, 2]]
    assert puller.pull_once() == 0 and len(sent) == 1  # rien de neuf : pas de message, pas de doublon
    front.rows.append({**RAW[0], "id": 3, "ts": "2026-10-21 09:00:00", "taches": "Encore une fois : " + RAW[0]["taches"]})
    assert puller.pull_once() == 1
    msg = sent[-1]
    assert "Léa" in msg and "pme" in msg and "même adresse" in msg and "/admin/#/a/3" in msg
    assert "lea@example.org" not in msg.lower() and "Martin" not in msg
    assert [a["source_id"] for a in store.applications] == [1, 2, 3]


def test_ack_failure_does_not_lose_or_duplicate():
    store = MemoryAdminStore()
    front = FakeFront1(RAW)

    def flaky(method, url, token, body=None):
        if method == "POST":
            raise OSError("front1 coupé")
        return front(method, url, token, body)

    puller = BetaPuller(store=store, base_url="http://front1:8122", token="jeton", fetch=flaky)
    assert puller.pull_once() == 2 and puller.pull_once() == 0


def test_table_emptied_after_import_is_not_a_new_bootstrap():
    """Vu sur le canari le 21.10 : la ligne de test effacée, la table vide aurait fait de la 1re vraie candidature un
    simple récapitulatif « import initial »."""
    store, sent = MemoryAdminStore(), []
    front = FakeFront1(RAW[:1])
    puller = BetaPuller(store=store, base_url="http://front1:8122", token="jeton", send=sent.append, fetch=front)
    puller.pull_once()
    store.applications.clear()  # effacement (test, demande nLPD)
    front.rows = [{**RAW[1], "id": 5}]
    assert puller.pull_once() == 1
    assert "Nouvelle candidature bêta : Sam" in sent[-1]


@pytest.fixture()
def console_front():
    from nokker.onboarding.front import MemoryFront

    store = MemoryAdminStore()
    store.seed_operators({"alice@example.ch": "owner", "bruno@example.ch": "ops", "lire@example.ch": "lecteur"},
                         "system:config")
    store.import_applications("front1", [normalize_application(r) for r in RAW], "system:beta-pull")
    front = MemoryFront({1: {"email": "lea@example.org", "prenom": "Léa", "lang": "fr"}})
    httpd = serve_admin(store, verifier(), "127.0.0.1", 0, front)
    yield store, front, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def post(base, path, body, email=None, console_header=True):
    headers = {"Content-Type": "application/json"}
    if email:
        headers["Cf-Access-Jwt-Assertion"] = jwt(claims(email))
    if console_header:
        headers["X-Nokker-Console"] = "1"
    req = urllib_request.Request(base + path, data=json.dumps(body).encode(), method="POST", headers=headers)
    try:
        with urllib_request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib_error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_invite_is_sent_by_ops_with_audit_after_the_call(console_front):
    store, front, base = console_front
    status, body = get(base, "/admin/v1/applications/1/invite/preview?lang=en", email="bruno@example.ch")
    assert status == 200 and json.loads(body)["to"] == "lea@example.org" and front.sent == []
    status, body = post(base, "/admin/v1/applications/1/invite", {"lang": "en"}, email="bruno@example.ch")
    assert status == 200 and body["lang"] == "en" and front.sent[-1]["actor"] == "bruno@example.ch"
    assert any(j["action"] == "invitation.sent" and j["object"] == "application:1" for j in store.journal)
    status, body = get(base, "/admin/v1/applications/1/invitations", email="bruno@example.ch")
    assert status == 200 and json.loads(body)["items"][0]["state"] == "sent"


def test_invite_refused_without_console_header_role_or_known_candidature(console_front):
    store, front, base = console_front
    assert post(base, "/admin/v1/applications/1/invite", {}, email="bruno@example.ch", console_header=False)[0] == 403
    assert post(base, "/admin/v1/applications/1/invite", {}, email="lire@example.ch")[0] == 403
    assert post(base, "/admin/v1/applications/1/invite", {}, email=None)[0] == 401
    assert post(base, "/admin/v1/applications/99/invite", {}, email="bruno@example.ch")[0] == 404
    assert front.sent == []


def test_failed_send_is_audited(console_front):
    store, front, base = console_front
    front.fail_send = True
    status, body = post(base, "/admin/v1/applications/1/invite", {}, email="alice@example.ch")
    assert status == 502 and body["error"] == "send_failed"
    assert any(j["action"] == "invitation.send_failed" for j in store.journal)
