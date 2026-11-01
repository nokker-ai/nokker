"""Export d'un fil et liens de conversation partagés (docs/CLIENT-API.md « Export et partage d'un fil ») — vraies
requêtes HTTP en boucle locale, stores en mémoire. Résultats d'outils secrets scellés partout, HTML échappé et sans
script, jeton stocké haché, révocation et expiration immédiates, propriétaire seul, débit de la route publique."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from nokker.api.events import EventBus
from nokker.api.server import make_handler
from nokker.bubbles.hub import BubbleHub
from nokker.bubbles.memory import InMemoryBubbles
from nokker.core.ports import ToolSpec
from nokker.core.turn import Effect, InputCheck, Message, ToolCall, Turn
from nokker.qa.secrets import KnownSecrets, redact
from nokker.threads.export import SEALED_TEXT, build_export, render_html, render_md
from nokker.threads.memory import InMemoryThreads
from nokker.threads.shares import MemoryShares, PublicLimiter, ThreadShares, token_sha256
from tests.unit.api.test_server import EmptyTools, _Server, make_kernel

KEYS = {"key-own": "owner1", "key-mem": "member1", "key-out": "outsider1"}
SECRET_VALUE = "Zq7#vault-Pw-93kLmN0pR"  # renvoyée par l'outil secret : ne doit sortir nulle part
KNOWN_VALUE = "sk-live-4f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c"  # valeur du coffre recopiée dans une réponse


class SecretTools(EmptyTools):
    def spec(self, name):
        if name == "coffre_lire":
            return ToolSpec(name=name, description="lit un élément du coffre", parameters={}, effect=Effect.SECRET)
        if name == "web_search":
            return ToolSpec(name=name, description="recherche", parameters={}, effect=Effect.READ)
        return None


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def start(*, per_ip: int = 30):
    bus = EventBus()
    users = frozenset(KEYS.values())
    hub = BubbleHub(threads=InMemoryThreads(), bubbles=InMemoryBubbles(), publish=bus.publish, known_users=users,
                    spawn=lambda fn: fn())
    kernel = make_kernel("ok")
    kernel.tools = SecretTools()
    known = KnownSecrets()
    known.add("cle-test", KNOWN_VALUE)
    kernel.invariants.scrub_output = lambda text: redact(text, known).text
    kernel.observer = hub
    hub.kernel = kernel
    hub.scrub = kernel.invariants.scrub_output
    clock = Clock()
    shares = ThreadShares(MemoryShares(), base_url="https://api.example.test", clock=clock,
                          limiter=PublicLimiter(per_ip=per_ip, global_max=1000))
    handler = make_handler(kernel, KEYS, None, approvals=None, events=bus, hub=hub, known_users=users, shares=shares)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    return _Server(httpd=httpd, thread=th, port=httpd.server_address[1]), hub, kernel, shares, clock


def raw_call(srv, method, path, body=None, key="key-own", headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    if key:
        h["Authorization"] = f"Bearer {key}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(srv.url(path), data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def call(srv, method, path, body=None, key="key-own", headers=None):
    st, _, raw = raw_call(srv, method, path, body, key, headers)
    return st, (json.loads(raw) if raw else None)


def seed(srv, kernel, title="Infra <script>alert(1)</script>"):
    """Fil du propriétaire (membre : member1) avec deux tours : un outil secret, une recherche, une injection HTML."""
    st, t = call(srv, "POST", "/v1/threads", {"title": title})
    assert st == 201
    tid = t["id"]
    assert call(srv, "PATCH", f"/v1/threads/{tid}", {"members": ["member1"]})[0] == 200
    t0 = Turn(thread_id=tid, index=0, input=InputCheck(text="Donne-moi le mot de passe du routeur"), sender="owner1",
              reply="Il est dans le coffre, élément « routeur ».",
              messages=[
                  Message(role="assistant", content="Je regarde.",
                          tool_calls=[ToolCall(id="c1", name="coffre_lire", arguments={"item": "routeur"})]),
                  # Tour d'avant le scellement du noyau : le contenu est resté en clair dans le store.
                  Message(role="tool", content=f"mot de passe {SECRET_VALUE}", tool_call_id="c1", name="coffre_lire"),
                  Message(role="user", content="note interne du noyau"),
                  Message(role="assistant", content="Il est dans le coffre, élément « routeur ».")])
    t1 = Turn(thread_id=tid, index=1, input=InputCheck(text='<img src=x onerror="alert(2)"> cherche ça'),
              sender="owner1", reply=f"Trouvé. </pre><script>alert(3)</script> clé {KNOWN_VALUE}",
              messages=[
                  Message(role="assistant", content="",
                          tool_calls=[ToolCall(id="c2", name="web_search", arguments={"q": "<script>x</script>"})]),
                  Message(role="tool", content='[{"title": "</pre><script>alert(4)</script>"}]', tool_call_id="c2",
                          name="web_search")])
    kernel.store.append(t0)
    kernel.store.append(t1)
    return tid


@pytest.fixture
def env():
    srv, hub, kernel, shares, clock = start()
    try:
        yield srv, hub, kernel, shares, clock
    finally:
        srv.stop()


# ─────────────────────────── export ───────────────────────────


def test_export_json_has_tool_calls_and_seals_secret_results(env):
    srv, _, kernel, _, _ = env
    tid = seed(srv, kernel)
    st, headers, raw = raw_call(srv, "GET", f"/v1/threads/{tid}/export")
    assert st == 200 and headers["Content-Type"].startswith("application/json")
    assert headers["Cache-Control"] == "no-store" and "attachment" in headers["Content-Disposition"]
    assert SECRET_VALUE.encode() not in raw and KNOWN_VALUE.encode() not in raw
    doc = json.loads(raw)
    assert doc["format"] == "nokker-thread-export/1" and doc["thread"]["id"] == tid
    roles = [(m["index"], m["role"]) for m in doc["messages"]]
    assert roles == [(0, "user"), (0, "assistant"), (0, "tool"), (0, "assistant"),
                     (1, "user"), (1, "assistant"), (1, "tool"), (1, "assistant")]
    call0 = doc["messages"][1]
    assert call0["tool_calls"] == [{"id": "c1", "name": "coffre_lire", "arguments": '{"item": "routeur"}'}]
    assert doc["messages"][2] == {"index": 0, "role": "tool", "tool_call_id": "c1", "name": "coffre_lire",
                                  "sealed": True, "content": SEALED_TEXT}
    search = doc["messages"][6]
    assert search["sealed"] is False and "alert(4)" in search["content"]
    assert "[secret:" in doc["messages"][7]["content"]  # valeur connue recopiée dans la réponse : caviardée
    assert "note interne" not in raw.decode()
    assert doc["page"] == {"before": None, "limit": 100, "next_before": None, "truncated": False}


def test_export_without_tools_and_pagination(env):
    srv, _, kernel, _, _ = env
    tid = seed(srv, kernel)
    st, doc = call(srv, "GET", f"/v1/threads/{tid}/export?include_tools=0&limit=1")
    assert st == 200
    assert [(m["index"], m["role"]) for m in doc["messages"]] == [(1, "user"), (1, "assistant")]
    assert doc["page"]["next_before"] == 1
    st, doc = call(srv, "GET", f"/v1/threads/{tid}/export?include_tools=0&before=1")
    assert [m["index"] for m in doc["messages"]] == [0, 0] and doc["page"]["next_before"] is None
    assert call(srv, "GET", f"/v1/threads/{tid}/export?format=pdf")[0] == 400
    assert call(srv, "GET", f"/v1/threads/{tid}/export?limit=0")[0] == 400
    assert call(srv, "GET", f"/v1/threads/{tid}/export?limit=x")[0] == 400


def test_export_html_is_escaped_and_scriptless(env):
    srv, _, kernel, _, _ = env
    tid = seed(srv, kernel)
    st, headers, raw = raw_call(srv, "GET", f"/v1/threads/{tid}/export?format=html")
    assert st == 200 and headers["Content-Type"].startswith("text/html")
    assert "script-src" not in headers["Content-Security-Policy"]
    assert "default-src 'none'" in headers["Content-Security-Policy"] and headers["X-Content-Type-Options"] == "nosniff"
    page = raw.decode()
    assert "<script" not in page.lower() and "<img" not in page.lower()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "&lt;/pre&gt;&lt;script&gt;alert(4)" in page
    assert 'onerror=&quot;alert(2)&quot;' in page
    assert SECRET_VALUE not in page and "scellé" in page


def test_export_markdown(env):
    srv, _, kernel, _, _ = env
    tid = seed(srv, kernel)
    st, headers, raw = raw_call(srv, "GET", f"/v1/threads/{tid}/export?format=md")
    assert st == 200 and headers["Content-Type"].startswith("text/markdown")
    md = raw.decode()
    assert md.startswith("# Infra") and "## Tour 0" in md and "**Appel d'outil** `coffre_lire`" in md
    assert "**Résultat** `coffre_lire` (scellé)" in md and SECRET_VALUE not in md


def test_markdown_fence_outlasts_backticks_in_content():
    turn = Turn(thread_id="f", index=0, input=InputCheck(text="x"), reply="r",
                messages=[Message(role="assistant", tool_calls=[ToolCall(id="c", name="t", arguments={})]),
                          Message(role="tool", content="avant\n````\ninjecté", tool_call_id="c", name="t")])
    doc = build_export({"id": "f", "title": "t"}, [turn], scrub=lambda s: s, effect_of=lambda n: None)
    md = render_md(doc)
    assert "`````\navant\n````\ninjecté\n`````" in md


def test_export_byte_cap_keeps_latest_turns():
    turns = [Turn(thread_id="f", index=i, input=InputCheck(text="x" * 1000), reply="y") for i in range(10)]
    doc = build_export({"id": "f", "title": "t"}, turns, scrub=lambda s: s, effect_of=lambda n: None,
                       max_bytes=3000)
    kept = sorted({m["index"] for m in doc["messages"]})
    assert kept[-1] == 9 and len(kept) < 10
    assert doc["page"]["truncated"] is True and doc["page"]["next_before"] == kept[0]


def test_kernel_sealed_result_stays_sealed_even_for_unknown_tool():
    turn = Turn(thread_id="f", index=0, input=InputCheck(text="x"), reply="r",
                messages=[Message(role="assistant", tool_calls=[ToolCall(id="c", name="disparu", arguments={})]),
                          Message(role="tool", content="[secret:x] Résultat de l'outil secret « disparu » retiré",
                                  tool_call_id="c", name="disparu")])
    doc = build_export({"id": "f"}, [turn], scrub=lambda s: s, effect_of=lambda n: None)
    assert doc["messages"][2]["sealed"] is True and doc["messages"][2]["content"] == SEALED_TEXT


def test_export_access(env):
    srv, _, kernel, _, _ = env
    tid = seed(srv, kernel)
    assert call(srv, "GET", f"/v1/threads/{tid}/export", key="key-mem")[0] == 200  # membre : lecture
    assert call(srv, "GET", f"/v1/threads/{tid}/export", key="key-out")[0] == 403
    assert call(srv, "GET", f"/v1/threads/{tid}/export", key=None)[0] == 401
    assert call(srv, "GET", "/v1/threads/th_000000000000/export")[0] == 404


# ─────────────────────────── partages ───────────────────────────


def _share(srv, tid, **body):
    st, out = call(srv, "POST", f"/v1/threads/{tid}/shares", body)
    assert st == 201, out
    return out


def _token(out) -> str:
    return out["url"].rsplit("/", 1)[-1].removesuffix(".html")


def test_share_lifecycle_snapshot_is_frozen_and_public(env):
    srv, _, kernel, shares, _ = env
    tid = seed(srv, kernel)
    out = _share(srv, tid, expires_in=3600)
    assert out["share_id"].startswith("sh_") and out["url"].startswith("https://api.example.test/v1/public/shares/")
    assert out["status"] == "active" and out["include_tools"] is False and out["turn_count"] == 2
    token = _token(out)
    assert len(token) >= 43
    # stocké haché seulement : ni l'instantané ni l'index ne contiennent le jeton
    assert shares.store.digests() == [token_sha256(token)]
    assert token not in repr(shares.store._rows)
    # lecture publique sans connexion
    st, doc = call(srv, "GET", f"/v1/public/shares/{token}", key=None)
    assert st == 200 and doc["thread"] == {"title": "Infra <script>alert(1)</script>",
                                           "created_at": doc["thread"]["created_at"],
                                           "updated_at": doc["thread"]["updated_at"]}
    assert "owner" not in doc["thread"] and all("sender" not in m for m in doc["messages"])
    assert [m["role"] for m in doc["messages"]] == ["user", "assistant", "user", "assistant"]  # sans outils
    assert doc["share"]["expires_at"] is not None
    # instantané figé : un tour ajouté après le partage n'y entre pas
    kernel.store.append(Turn(thread_id=tid, index=2, input=InputCheck(text="après le partage"), reply="r"))
    st, doc = call(srv, "GET", f"/v1/public/shares/{token}.json", key=None)
    assert st == 200 and "après le partage" not in json.dumps(doc, ensure_ascii=False)
    # page HTML autonome, échappée, sans script
    st, headers, raw = raw_call(srv, "GET", f"/v1/public/shares/{token}.html", key=None)
    assert st == 200 and headers["Content-Type"].startswith("text/html")
    assert headers["X-Robots-Tag"] == "noindex, nofollow" and headers["Referrer-Policy"] == "no-referrer"
    assert "<script" not in raw.decode().lower() and "Content-Disposition" not in headers
    st, _, raw = raw_call(srv, "GET", f"/v1/public/shares/{token}.md", key=None)
    assert st == 200 and raw.decode().startswith("# Infra")
    # liste (propriétaire)
    st, lst = call(srv, "GET", f"/v1/threads/{tid}/shares")
    assert st == 200 and [s["share_id"] for s in lst["shares"]] == [out["share_id"]]
    assert "url" not in lst["shares"][0]  # le lien ne se relit jamais
    # révocation → 404 immédiat
    st, rv = call(srv, "DELETE", f"/v1/shares/{out['share_id']}")
    assert st == 200 and rv["status"] == "revoked"
    assert call(srv, "GET", f"/v1/public/shares/{token}", key=None)[0] == 404
    assert call(srv, "GET", f"/v1/public/shares/{token}.html", key=None)[0] == 404


def test_share_with_tools_keeps_secret_sealed(env):
    srv, _, kernel, _, _ = env
    tid = seed(srv, kernel)
    out = _share(srv, tid, include_tools=True)
    token = _token(out)
    for suffix in ("", ".html", ".md"):
        st, _, raw = raw_call(srv, "GET", f"/v1/public/shares/{token}{suffix}", key=None)
        assert st == 200 and SECRET_VALUE.encode() not in raw and KNOWN_VALUE.encode() not in raw
    doc = json.loads(raw_call(srv, "GET", f"/v1/public/shares/{token}", key=None)[2])
    tool = [m for m in doc["messages"] if m["role"] == "tool" and m["name"] == "coffre_lire"]
    assert tool == [{"index": 0, "role": "tool", "tool_call_id": "c1", "name": "coffre_lire", "sealed": True,
                     "content": SEALED_TEXT}]


def test_share_expires(env):
    srv, _, kernel, _, clock = env
    tid = seed(srv, kernel)
    token = _token(_share(srv, tid, expires_in=120))
    assert call(srv, "GET", f"/v1/public/shares/{token}", key=None)[0] == 200
    clock.t += 121
    assert call(srv, "GET", f"/v1/public/shares/{token}", key=None)[0] == 404
    _, lst = call(srv, "GET", f"/v1/threads/{tid}/shares")
    assert lst["shares"][0]["status"] == "expired"


def test_share_parameters_validated(env):
    srv, _, kernel, _, _ = env
    tid = seed(srv, kernel)
    for body in ({"expires_in": 10}, {"expires_in": 91 * 86400}, {"expires_in": "1h"}, {"expires_in": True},
                 {"include_tools": "oui"}):
        assert call(srv, "POST", f"/v1/threads/{tid}/shares", body)[0] == 400, body


def test_only_owner_creates_lists_and_revokes(env):
    srv, _, kernel, _, _ = env
    tid = seed(srv, kernel)
    out = _share(srv, tid)
    assert call(srv, "POST", f"/v1/threads/{tid}/shares", {}, key="key-mem")[0] == 403
    assert call(srv, "GET", f"/v1/threads/{tid}/shares", key="key-mem")[0] == 403
    assert call(srv, "DELETE", f"/v1/shares/{out['share_id']}", key="key-mem")[0] == 403
    assert call(srv, "POST", f"/v1/threads/{tid}/shares", {}, key="key-out")[0] == 403
    assert call(srv, "DELETE", f"/v1/shares/{out['share_id']}", key="key-out")[0] == 404
    assert call(srv, "DELETE", f"/v1/shares/{out['share_id']}", key=None)[0] == 401
    assert call(srv, "DELETE", "/v1/shares/sh_00000000000000000000")[0] == 404
    assert call(srv, "GET", f"/v1/public/shares/{_token(out)}", key=None)[0] == 200  # rien n'a été révoqué


def test_deleting_thread_revokes_its_shares(env):
    srv, _, kernel, _, _ = env
    tid = seed(srv, kernel)
    token = _token(_share(srv, tid))
    assert call(srv, "DELETE", f"/v1/threads/{tid}")[0] == 200
    assert call(srv, "GET", f"/v1/public/shares/{token}", key=None)[0] == 404


def test_public_route_rejects_bad_tokens_and_is_rate_limited():
    srv, _, kernel, _, _ = start(per_ip=5)
    try:
        tid = seed(srv, kernel)
        token = _token(_share(srv, tid))
        assert call(srv, "GET", "/v1/public/shares/court", key=None)[0] == 404
        assert call(srv, "GET", "/v1/public/shares/" + "A" * 43, key=None)[0] == 404
        assert call(srv, "GET", f"/v1/public/shares/{token}x", key=None)[0] == 404
        assert call(srv, "GET", f"/v1/public/shares/{token}", key=None)[0] == 200
        assert call(srv, "GET", f"/v1/public/shares/{token}", key=None)[0] == 200
        st, headers, _ = raw_call(srv, "GET", f"/v1/public/shares/{token}", key=None)
        assert st == 429 and headers["Retry-After"] == "60"
    finally:
        srv.stop()


def test_html_render_escapes_every_field():
    doc = {"thread": {"title": '"><script>t</script>'}, "exported_at": "<b>", "turn_count": 1, "page": {},
           "messages": [{"index": "0\"><script>", "role": "user", "content": "<script>u</script>", "sender": "<i>"},
                        {"index": "0\"><script>", "role": "assistant",
                         "tool_calls": [{"name": "<x>", "arguments": "</pre><script>a</script>"}]},
                        {"index": "0\"><script>", "role": "tool", "name": "<n>", "content": "<script>r</script>"}]}
    page = render_html(doc)
    assert "<script" not in page.lower() and "<b>" not in page and "<i>" not in page and "<x>" not in page


def test_local_session_export(tmp_path):
    """Équivalent Lokkal : `GET /v1/local/sessions/{id}/export`, jeton local exigé, secrets scellés."""
    from tests.unit.test_local_api import _api, _req
    from tests.unit.test_local_sessions_view import _serve

    api = _api(tmp_path)
    kernel = make_kernel("ok")
    kernel.tools = SecretTools()
    bus = EventBus()
    hub = BubbleHub(threads=InMemoryThreads(), bubbles=InMemoryBubbles(), publish=bus.publish,
                    known_users=frozenset(), spawn=lambda fn: fn())
    kernel.observer, hub.kernel = hub, kernel
    httpd, port = _serve(api, kernel, events=bus, hub=hub)
    try:
        tid = _req(port, "/v1/local/sessions", token=api.token, body={"cwd": str(tmp_path / "projets" / "app")})[1][
            "thread_id"]
        kernel.store.append(Turn(thread_id=tid, index=0, input=InputCheck(text="lis le coffre"), reply="fait",
                                 messages=[Message(role="assistant", tool_calls=[
                                     ToolCall(id="c1", name="coffre_lire", arguments={"item": "x"})]),
                                     Message(role="tool", content=SECRET_VALUE, tool_call_id="c1",
                                             name="coffre_lire")]))
        assert _req(port, f"/v1/local/sessions/{tid}/export")[0] == 401
        st, doc = _req(port, f"/v1/local/sessions/{tid}/export", token=api.token)
        assert st == 200 and [m["role"] for m in doc["messages"]] == ["user", "assistant", "tool", "assistant"]
        assert SECRET_VALUE not in json.dumps(doc) and doc["messages"][2]["sealed"] is True
        assert _req(port, "/v1/local/sessions/th_inconnu/export", token=api.token)[0] == 404
        assert _req(port, "/v1/local/sessions/tn_aaaaaaaaaaaa:s-1/export", token=api.token)[0] == 404
    finally:
        httpd.shutdown()
