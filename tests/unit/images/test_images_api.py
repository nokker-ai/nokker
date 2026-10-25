"""X7 sur la route de chat (`/v1/chat/completions`, forme OpenAI) et les routes de fil : parties `image_url`,
`attachments`, refus en 400, lecture des octets par un membre, effacement avec le fil."""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

from nokker.images import ImageStore
from tests.unit.api.test_bubbles_routes import call, start
from tests.unit.images.test_images import GIF, _engine, png


def _b64(d: bytes) -> str:
    return base64.b64encode(d).decode()


def _chat(srv, message, session="sess-img", key="key-alice"):
    return call(srv, "POST", "/v1/chat/completions", {"model": "nokker", "messages": [message]}, key=key,
                headers={"X-Session-Id": session})


def _start(tmp_path, with_store=True):
    srv, hub, bus = start("rouge")
    if with_store:
        hub.kernel.images = ImageStore(tmp_path / "images")
    return srv, hub


def test_openai_image_url_parts_become_a_reference_in_the_turn(tmp_path):
    srv, hub = _start(tmp_path)
    try:
        data = png()
        st, body = _chat(srv, {"role": "user", "content": [
            {"type": "text", "text": "quelle couleur ?"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_b64(data)}"}}]})
        assert st == 200 and body["choices"][0]["message"]["content"] == "rouge"
        [turn] = hub.kernel.store.history("sess-img")
        assert turn.input.text == "quelle couleur ?"
        assert [(i.media_type, i.size) for i in turn.images] == [("image/png", len(data))]
        # l'historique du fil expose la référence, les octets se lisent par la route d'image
        st, thread = call(srv, "GET", "/v1/threads/sess-img")
        assert thread["messages"][0]["images"][0]["sha256"] == turn.images[0].sha256
        req = urllib.request.Request(srv.url(f"/v1/threads/sess-img/images/{turn.images[0].sha256}"),
                                     headers={"Authorization": "Bearer key-alice"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.read() == data and resp.headers["Content-Type"] == "image/png"
            assert resp.headers["X-Content-Type-Options"] == "nosniff"
        # un non-membre ne lit pas
        st, _ = call(srv, "GET", f"/v1/threads/sess-img/images/{turn.images[0].sha256}", key="key-bruno")
        assert st in (403, 404)
    finally:
        srv.stop()


def test_attachments_field_and_refusals_are_400(tmp_path):
    srv, hub = _start(tmp_path)
    try:
        st, _ = _chat(srv, {"role": "user", "content": "deux images",
                            "attachments": [{"data": _b64(png()), "media_type": "image/png"},
                                            {"data": _b64(GIF), "name": "anim.gif"}]})
        assert st == 200
        assert [i.media_type for i in hub.kernel.store.history("sess-img")[0].images] == ["image/png", "image/gif"]
        cases = [
            {"role": "user", "content": "x", "attachments": [{"data": _b64(png())}] * 5},
            {"role": "user", "content": "x", "attachments": [{"data": _b64(b"<svg/>"), "name": "a.png"}]},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://exemple.ch/a.png"}}]},
            {"role": "user", "content": "x", "attachments": [{"path": "a.png"}]},  # pas de session locale
        ]
        for msg in cases:
            st, body = _chat(srv, msg, session="sess-refus")
            assert st == 400 and body["error"]["message"], msg
        assert hub.kernel.store.history("sess-refus") == []  # rien n'a tourné ni été rangé
    finally:
        srv.stop()


def test_instance_without_image_store_refuses_images(tmp_path):
    srv, _ = _start(tmp_path, with_store=False)
    try:
        st, body = _chat(srv, {"role": "user", "content": "x", "attachments": [{"data": _b64(png())}]})
        assert st == 400 and "non prises en charge" in body["error"]["message"]
        assert _chat(srv, {"role": "user", "content": "sans image"})[0] == 200
    finally:
        srv.stop()


def test_deleting_the_thread_erases_its_images(tmp_path):
    srv, hub = _start(tmp_path)
    try:
        _chat(srv, {"role": "user", "content": "x", "attachments": [{"data": _b64(png())}]}, session="sess-del")
        sha = hub.kernel.store.history("sess-del")[0].images[0].sha256
        assert hub.kernel.images.get("sess-del", sha) is not None
        assert call(srv, "DELETE", "/v1/threads/sess-del")[0] == 200
        assert hub.kernel.images.get("sess-del", sha) is None
        assert list((tmp_path / "images").rglob(sha)) == []
    finally:
        srv.stop()


def test_body_over_the_cap_is_413(tmp_path, monkeypatch):
    import nokker.api.server as server

    monkeypatch.setattr(server, "MAX_CHAT_BODY", 100)
    srv, _ = _start(tmp_path)
    try:
        st, _ = _chat(srv, {"role": "user", "content": "x" * 200})
        assert st == 413
    finally:
        srv.stop()


def test_skipped_event_reaches_the_thread_audience(tmp_path):
    srv, hub = _start(tmp_path)
    got = []
    hub.publish = lambda kind, payload: got.append((kind, payload))
    try:
        hub.kernel.context = _engine(tmp_path)  # le vrai Context Engine : parties d'image dans le message
        hub.kernel.providers[next(iter(hub.kernel.providers))].vision = False  # palier sans vision
        st, _ = _chat(srv, {"role": "user", "content": "x", "attachments": [{"data": _b64(png())}]}, session="s-nv")
        assert st == 200
        [ev] = [p for k, p in got if k == "attachment.skipped"]
        assert ev["count"] == 1 and ev["reason"] == "no_vision" and ev["thread_id"] == "s-nv"
        assert "alice" in ev["_audience"]
        json.dumps(ev)
    finally:
        srv.stop()
