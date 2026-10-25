"""Unit tests of the template server: the tools are plain functions, tested without a network."""
import asyncio

import pytest
import server


@pytest.fixture(autouse=True)
def empty_store():
    server._NOTES.clear()


def test_add_then_get():
    note = server.add_note("First", "hello")
    assert server.get_note(note["id"])["text"] == "hello"


def test_list_filters_and_limits():
    for i in range(5):
        server.add_note(f"item {i}")
    server.add_note("other")
    assert len(server.list_notes("item", limit=3)) == 3
    assert [n["title"] for n in server.list_notes("other")] == ["other"]


def test_bad_input_is_an_error():
    with pytest.raises(ValueError):
        server.add_note("")
    with pytest.raises(ValueError):
        server.get_note("missing")


def test_tools_are_declared():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names == {"list_notes", "get_note", "add_note"}
