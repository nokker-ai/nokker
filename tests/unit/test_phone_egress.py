"""Pages par la connexion du téléphone (P11) : liste d'hôtes, mode liste dans web_fetch (jamais de repli cloud), offre
sur un refus, GESTE (droit signé, usage unique, 10 min) puis tour de la personne `phone_fetch`, textes figés. Le modèle
ne choisit jamais le téléphone. Par le vrai serveur et la vraie file du nœud (P7a)."""
from __future__ import annotations

import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from nokker.cloud import egress as egress_mod
from nokker.cloud.egress import (
    REASONS,
    EgressService,
    MemoryEgressStore,
    host_allowed,
    normalize_host,
    refusal_reason,
    url_check,
)
from nokker.tenancy.context import tenant_scope
from tests.unit.test_crew_deck import _call, cloud_http  # noqa: F401 — fixture
from tests.unit.test_phone_node import enroll, keypair, phone_world, req  # noqa: F401 — fixtures et aides

TEXTS_SHA256 = "4ec87841c4fb77436c7ff6769949bf9bccaa1acec86195f5390a9f5c16fe8360"


@pytest.fixture
def egress(phone_world):  # noqa: F811
    f, port, svc, priv, spki = phone_world
    ex = EgressService(phone=svc, store=MemoryEgressStore())
    egress_mod.configure(ex)
    tok = enroll(f, port, svc, spki, priv=priv)["machine_token"]
    yield f, port, svc, ex, priv, tok
    egress_mod.configure(None)


def beat(port, tok, priv, egress="on"):
    st, out = req(port, "POST", "/v1/gateway/phone/beat", token=tok, priv=priv,
                  body={"v": 1, "reach": "live", "caps": {"egress": egress}})
    assert st == 200, out


def phone_answers(port, tok, priv, reply):
    """L'app : relève une tâche fetch.get et répond `reply(task)` (corps du résultat)."""
    got = []

    def run():
        for _ in range(60):
            st, out = req(port, "GET", "/v1/gateway/phone/tasks?wait=1", token=tok, priv=priv)
            for t in (out or {}).get("tasks", []):
                got.append(t)
                req(port, "POST", f"/v1/gateway/phone/tasks/{t['task_id']}/result", token=tok, body=reply(t), priv=priv)
                return

    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th, got


def ctx(f, thread="t1", index=0, channel="web", origin=""):
    turn = SimpleNamespace(input=SimpleNamespace(origin=origin))
    with tenant_scope(f.p.tenant_id):
        from nokker.tenancy.context import scoped_thread_id

        tid = scoped_thread_id(f.p.tenant_id, thread)
    return SimpleNamespace(thread_id=tid, turn_index=index, channel=channel, sender=f.p.account_id, turn=turn)


# ── règles (mêmes que l'appareil, packages/sdk/src/egress.ts) ──
def test_host_rules_and_refusal_reasons():
    assert normalize_host(" Exemple.CH. ") == "exemple.ch" and normalize_host("*.exemple.ch") == "*.exemple.ch"
    for bad in ("*", "*.ch", "ch", "1.2.3.4", "exemple.ch/x", "a:b.ch", "box.local", "nas.home", "x.lan", "*.*.ch", 3):
        assert normalize_host(bad) is None, bad
    assert host_allowed("a.exemple.ch", ["*.exemple.ch"]) and not host_allowed("exemple.ch", ["*.exemple.ch"])
    assert host_allowed("exemple.ch", ["exemple.ch"]) and not host_allowed("autre.ch", ["exemple.ch"])
    assert url_check("https://news.example.org/a?b=1") == ("news.example.org", None)
    for bad, code in (("http://x.ch", "scheme_not_allowed"), ("https://x.ch:8443/", "port_not_allowed"),
                      ("https://u:p@x.ch/", "host_not_allowed"), ("https://10.0.0.1/", "host_not_allowed"),
                      ("https://router.local/", "host_not_allowed")):
        assert url_check(bad) == (None, code), bad
    assert refusal_reason("direct : HTTP 403 Forbidden") == "http_403"
    assert refusal_reason("statut 451") == "http_451" and refusal_reason("Cloudflare challenge") == "captcha"
    assert refusal_reason("délai dépassé") is None


def test_texts_are_frozen_and_rendered_outside_and_inside_the_fence(caplog):
    assert egress_mod.TEXTS["phone_fetch_input"] == "Ouvre cette page par mon téléphone : {url}"
    assert REASONS["phone_timeout"] == "le téléphone n'a pas répondu (app fermée ou hors ligne)" and len(REASONS) == 20
    assert REASONS["low_battery"] == "batterie du téléphone trop faible (sous 20 %, hors charge)"
    assert REASONS["cellular_off"] == ("téléphone en données mobiles, non autorisées pour ouvrir des pages "
                                     "(réglage « Aussi en données mobiles » dans l'app Nokker)")
    assert egress_mod.texts_sha256() == TEXTS_SHA256
    ok = EgressService.attached("https://ex.org/a", {"host": "ex.org", "ok": True, "status": 200},
                                {"status": 200, "title": "Titre", "text": "Corps de la page"})
    head, rest = ok.split("\n", 1)
    assert head == ("Page https://ex.org/a ouverte par le téléphone de la personne, à sa demande (statut 200, 16 "
                    "caractères ; vue depuis son téléphone : la langue, la région ou la session peuvent différer "
                    "d'une lecture depuis le cloud) :")  # l'en-tête du noyau, HORS clôture
    assert rest.startswith('<untrusted-data kind="web:page"') and "Titre\n\nCorps de la page" in rest
    evil = EgressService.attached("https://ex.org/[/x]", {"host": "ex.[org]", "ok": True, "status": 200},
                                  {"status": 200, "text": "t"})
    assert "(/x)" in evil.split("\n", 1)[0]  # <url> passe par inline()
    bad = EgressService.attached("https://ex.org/a", {"host": "ex.[org]", "ok": False, "error": "dns_failed"}, None)
    assert bad == ("phone_fetch_failed : la page « ex.(org) » n'a pas pu être ouverte par le téléphone (nom de "
                   "domaine introuvable) ; rien n'a été lu.")
    with caplog.at_level(logging.WARNING):
        odd = EgressService.attached("https://ex.org/a", {"host": "ex.org", "ok": False, "error": "zzz"}, None)
    assert "(raison non précisée)" in odd and "zzz" not in odd and "code de refus inconnu" in caplog.text


# ── liste d'hôtes ──
def test_hosts_route(egress):
    f, port, svc, ex, priv, tok = egress
    assert _call(port, "GET", "/v1/me/egress/hosts", token="sess-p") == (200, {"hosts": []})
    st, out = _call(port, "PUT", "/v1/me/egress/hosts", {"hosts": ["News.Example.org", "*.example.net", "news.example.org"]},
                    token="sess-p")
    assert (st, out) == (200, {"hosts": ["news.example.org", "*.example.net"]})
    assert _call(port, "GET", "/v1/me/egress/hosts", token="sess-q")[1] == {"hosts": []}  # à chacun la sienne
    st, out = _call(port, "PUT", "/v1/me/egress/hosts", {"hosts": ["ok.ch", "box.local"]}, token="sess-p")
    assert st == 400 and out["error"]["code"] == "bad_host" and out["error"]["index"] == 1
    assert _call(port, "PUT", "/v1/me/egress/hosts", {"hosts": [f"h{i}.ch" for i in range(51)]}, token="sess-p")[1][
        "error"]["code"] == "too_many_hosts"
    assert _call(port, "PUT", "/v1/me/egress/hosts", {"hosts": [], "x": 1}, token="sess-p")[1]["error"]["code"] \
        == "bad_hosts"


# ── (a) mode liste dans le vrai web_fetch ──
def web(monkeypatch, cloud_error=None):
    from nokker.tools.web import WebToolSource

    src = WebToolSource.__new__(WebToolSource)  # sans réseau : seul _fetch_one (le cloud) est remplacé
    from concurrent.futures import ThreadPoolExecutor

    src._executor, src.budget_s, src.connected_site = ThreadPoolExecutor(2), 1.0, None
    cloud = []

    def fetch_one(url):
        cloud.append(url)
        return {"url": url, "title": "", "content": "", "error": cloud_error} if cloud_error else {
            "url": url, "title": "Cloud", "content": "depuis le cloud", "engine": "direct"}

    monkeypatch.setattr(src, "_fetch_one", fetch_one)
    return src, cloud


def test_allowlist_goes_through_the_phone_never_the_cloud(egress, monkeypatch):
    f, port, svc, ex, priv, tok = egress
    beat(port, tok, priv)
    _call(port, "PUT", "/v1/me/egress/hosts", {"hosts": ["*.example.org"]}, token="sess-p")
    src, cloud = web(monkeypatch)
    th, got = phone_answers(port, tok, priv, lambda t: {"status": "done", "result": json.dumps(
        {"status": 200, "final_url": t["args"]["url"], "content_type": "text/html", "charset": "utf-8",
         "text": "page du téléphone", "bytes": 17, "truncated": False, "hops": 0})})
    c = ctx(f)
    with tenant_scope(f.p.tenant_id):
        r = src.call("web_fetch", {"urls": ["https://news.example.org/a"]}, c)
    th.join(5)
    assert cloud == [] and "page du téléphone" in r.content and r.data["documents"][0]["engine"] == "phone"
    assert got[0]["args"]["trigger"] == "allowlist" and got[0]["args"]["allow_hosts"] == ["*.example.org"]
    assert ex.message_fields(c.thread_id, 0)["phone_fetched"] == [{"host": "news.example.org", "ok": True, "status": 200}]
    # hors liste : le cloud, comme avant ; un agent ou une suite de carte : jamais le téléphone
    with tenant_scope(f.p.tenant_id):
        src.call("web_fetch", {"urls": ["https://autre.example.com/"]}, c)
        src.call("web_fetch", {"urls": ["https://news.example.org/b"]}, ctx(f, channel="agent"))
        src.call("web_fetch", {"urls": ["https://news.example.org/c"]}, ctx(f, origin="approval"))
    assert cloud == ["https://autre.example.com/", "https://news.example.org/b", "https://news.example.org/c"]
    # le téléphone échoue : document en erreur (phrase de la table), PAS de repli sur le cloud
    th, _ = phone_answers(port, tok, priv, lambda t: {"status": "failed", "error": {"code": "tls_failed"}})
    with tenant_scope(f.p.tenant_id):
        r = src.call("web_fetch", {"urls": ["https://news.example.org/d"]}, c)
    th.join(5)
    assert "https://news.example.org/d" not in cloud and "connexion sécurisée refusée" in r.content


def test_refused_page_offers_the_phone_only_to_the_person(egress, monkeypatch):
    f, port, svc, ex, priv, tok = egress
    src, cloud = web(monkeypatch, cloud_error="direct : HTTP 403 Forbidden")
    c = ctx(f, index=3)
    with tenant_scope(f.p.tenant_id):
        r = src.call("web_fetch", {"urls": ["https://shop.example.ch/p"]}, c)
    assert "téléphone" not in r.content  # le texte vu par le modèle ne change pas
    assert ex.message_fields(c.thread_id, 3) == {}  # pas de téléphone en ligne : pas d'offre
    beat(port, tok, priv)
    with tenant_scope(f.p.tenant_id):
        src.call("web_fetch", {"urls": ["https://shop.example.ch/p"]}, c)
        src.call("web_fetch", {"urls": ["https://shop.example.ch/q"]}, ctx(f, index=4, channel="agent"))
    offers = ex.message_fields(c.thread_id, 3)["phone_fetch_offers"]
    assert offers[0]["host"] == "shop.example.ch" and offers[0]["reason"] == "http_403"
    assert ex.message_fields(c.thread_id, 4) == {}  # un agent : jamais d'offre
    beat(port, tok, priv, egress="off")
    with tenant_scope(f.p.tenant_id):
        src.call("web_fetch", {"urls": ["https://shop.example.ch/r"]}, ctx(f, index=5))
    assert ex.message_fields(c.thread_id, 5) == {}  # egress coupé au battement : pas d'offre


# ── (b) le geste ──
def test_gesture_grant_then_person_turn_with_the_page(egress, monkeypatch):
    f, port, svc, ex, priv, tok = egress
    beat(port, tok, priv)
    src, _ = web(monkeypatch, cloud_error="HTTP 451")
    with tenant_scope(f.p.tenant_id):
        t = f.cloud.bubbles.create_thread("Recherche", f.p.account_id)
        c = SimpleNamespace(thread_id=t.id, turn_index=0, channel="web", sender=f.p.account_id,
                            turn=SimpleNamespace(input=SimpleNamespace(origin="")))
        src.call("web_fetch", {"urls": ["https://geo.example.ch/x"]}, c)
    offer = ex.message_fields(t.id, 0)["phone_fetch_offers"][0]
    path = f"/v1/threads/{t.id}/phone-fetch"
    code = lambda r: r[1]["error"]["code"]  # noqa: E731
    assert code(_call(port, "POST", path, {"url": "https://autre.ch/", "call_id": offer["call_id"]}, token="sess-p")) \
        == "no_offer"
    assert _call(port, "POST", path, {"url": offer["url"], "call_id": offer["call_id"]}, token="sess-q")[0] in (403, 404)
    th, got = phone_answers(port, tok, priv, lambda tk: {"status": "done", "result": json.dumps(
        {"status": 200, "final_url": tk["args"]["url"], "content_type": "text/html", "charset": "utf-8",
         "text": "contenu suisse", "bytes": 14, "truncated": False, "hops": 0, "title": "Page"})})
    st, out = _call(port, "POST", path, {"url": offer["url"], "call_id": offer["call_id"]}, token="sess-p")
    assert st == 202 and out["turn_id"].endswith(":0") or out["turn_id"].split(":")[-1].isdigit()
    th.join(10)
    assert got[0]["args"]["trigger"] == "explicit" and got[0]["args"]["grant_id"].startswith("pfg_")
    assert code(_call(port, "POST", path, {"url": offer["url"], "call_id": offer["call_id"]}, token="sess-p")) \
        == "offer_used"
    for _ in range(100):  # le tour du geste tourne à part
        with tenant_scope(f.p.tenant_id):
            turns = f.cloud.kernel.store.history(t.id)
        if turns:
            break
        time.sleep(0.05)
    turn = turns[-1]
    assert turn.input.origin == "phone_fetch" and turn.input.text == f"Ouvre cette page par mon téléphone : {offer['url']}"
    assert "ouverte par le téléphone de la personne" in turn.head and "contenu suisse" in turn.head
    fetched = ex.message_fields(t.id, turn.index)["phone_fetched"]
    assert fetched == [{"host": "geo.example.ch", "ok": True, "status": 200}]


def test_grant_is_single_use_signed_and_expires(egress):
    f, port, svc, ex, priv, tok = egress
    beat(port, tok, priv)
    g = {"id": "pfg_x", "user": f.p.account_id, "thread": "t", "url": "https://a.ch/", "turn_id": "t:0",
         "exp": time.time() + 600, "used": False}
    g["sig"] = ex._sign(g)  # noqa: SLF001
    ex._grants[g["id"]] = dict(g)  # noqa: SLF001
    forged = {**g, "url": "https://autre.ch/"}
    with tenant_scope(f.p.tenant_id):
        assert ex.explicit_fetch(forged)[0]["error"] == "grant_invalid"  # url différente du droit signé
        assert ex.explicit_fetch(g)[0]["error"] == "grant_invalid"  # consommé par l'essai précédent : usage unique
    g2 = {**g, "id": "pfg_y", "exp": time.time() - 1}
    g2["sig"] = ex._sign(g2)  # noqa: SLF001
    ex._grants[g2["id"]] = dict(g2)  # noqa: SLF001
    with tenant_scope(f.p.tenant_id):
        assert ex.explicit_fetch(g2)[0]["error"] == "grant_invalid"  # échu


def test_rate_and_result_size_for_fetch_get(egress):
    f, port, svc, ex, priv, tok = egress
    beat(port, tok, priv)
    with tenant_scope(f.p.tenant_id):
        node = svc.store.nodes_of(f.p.account_id)[0]
    assert all(ex._rate(node.machine_id) for _ in range(30)) and not ex._rate(node.machine_id)  # noqa: SLF001
    with tenant_scope(f.p.tenant_id):
        t = svc.enqueue(f.p.account_id, node, "fetch.get", {"url": "https://a.example.org/", "trigger": "explicit",
                                                            "turn_id": "x:0", "grant_id": "pfg_1"})
        with pytest.raises(Exception):
            svc.enqueue(f.p.account_id, node, "fetch.get", {"url": "http://a.example.org/", "trigger": "explicit",
                                                            "turn_id": "x:0", "grant_id": "pfg_1"})
    req(port, "GET", "/v1/gateway/phone/tasks?wait=0", token=tok, priv=priv)
    big = "x" * (900 * 1024)  # > 64 Kio, ≤ 1 Mio : permis pour fetch.get SEULEMENT
    st, out = req(port, "POST", f"/v1/gateway/phone/tasks/{t.task_id}/result", token=tok,
                  body={"status": "done", "result": big}, priv=priv)
    assert (st, out) == (200, {"stored": True})
    log = _call(port, "GET", f"/v1/link/phone/{node.machine_id}/tasks", token="sess-p")[1]["tasks"]
    assert log[0]["host"] == "a.example.org" and "/" not in log[0]["host"]  # l'hôte seul, ni chemin ni requête


def test_account_export_carries_phones_tasks_registrations_and_hosts_never_keys(egress):
    from nokker.account.export import AccountExports, MemoryExports

    f, port, svc, ex, priv, tok = egress
    acc = f.p.account_id
    _call(port, "PUT", "/v1/me/egress/hosts", {"hosts": ["news.example.org"]}, token="sess-p")
    with tenant_scope(f.p.tenant_id):
        node = svc.store.nodes_of(acc)[0]
        svc.enqueue(acc, node, "fetch.get", {"url": "https://news.example.org/a", "trigger": "explicit", "grant_id": "g1",
                                                "turn_id": "t1:0"})
        exports = AccountExports(store=MemoryExports(), turns_after=lambda after, n: [],
                                 phone=lambda a: {**svc.export(a), "egress_hosts": ex.get_hosts(a)["hosts"]})
        doc = exports.build(f.p, f.p.tenant_id, time.time())
    ph = doc["phone"]
    assert [p["machine_id"] for p in ph["phones"]] == [node.machine_id] and ph["phones"][0]["key_fingerprint"] == node.key_fp
    assert [(t["op"], t["args"]["url"]) for t in ph["tasks"]] == [("fetch.get", "https://news.example.org/a")]
    assert [r["state"] for r in ph["registrations"]] == ["claimed"] and ph["egress_hosts"] == ["news.example.org"]
    blob = json.dumps(doc, ensure_ascii=False, default=str)
    for secret in (tok, node.pubkey_spki, "sealed_token", "enc_key"):
        assert secret not in blob
    assert "phone keys and tokens (secrets, never exported)" in doc["not_included"]
    assert "phone" not in AccountExports(store=MemoryExports(), turns_after=lambda a, n: []).build(
        f.p, f.p.tenant_id, time.time())
