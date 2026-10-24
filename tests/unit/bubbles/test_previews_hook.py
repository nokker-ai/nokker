"""Aperçus posés sur une bulle `search` (docs/BUBBLES.md §5) via `make_preview_requester`."""
from __future__ import annotations

from nokker.bubbles.previews_hook import attach_previews, make_preview_requester


def _search_bubble(hub):
    t = hub.create_thread("Infra", "alice")
    state = {"query": "q", "results": [{"url": "https://a.example/1", "title": "A"},
                                       {"url": "https://b.example/2", "title": "B"}]}
    return hub.bubbles.create(thread_id=t.id, kind="search", title="q", state=state, status="idle",
                              actions=[], created_by="nina")


def test_previews_attached_and_bubble_updated(hub):
    b = _search_bubble(hub)
    asked = []

    def fake_many(urls, n):
        asked.append((urls, n))
        return [{"url": "https://a.example/1", "title": "Page A", "image": "/v1/previews/img/abc"},
                {"url": "https://b.example/2", "error": "SSRF"}]

    make_preview_requester(hub, fake_many, background=False)(b)
    got = hub.bubbles.require(b.id)
    assert asked == [(["https://a.example/1", "https://b.example/2"], 15)]
    assert got.state["results"][0]["preview"]["title"] == "Page A"
    assert "preview" not in got.state["results"][1]  # échec omis
    assert got.version > b.version
    assert got.state["previews_done"] is True


def test_failing_preview_service_never_raises(hub):
    b = _search_bubble(hub)

    def boom(urls, n):
        raise RuntimeError("réseau")

    make_preview_requester(hub, boom, background=False)(b)
    got = hub.bubbles.require(b.id)
    # La bulle est tout de même marquée terminée : l'interface arrête les squelettes, sans aperçu posé.
    assert got.state["previews_done"] is True
    assert all("preview" not in r for r in got.state["results"])


def test_attach_previews_nothing_to_attach(hub):
    b = _search_bubble(hub)
    assert attach_previews(b, [{"url": "https://a.example/1", "error": "x"}]) is None
