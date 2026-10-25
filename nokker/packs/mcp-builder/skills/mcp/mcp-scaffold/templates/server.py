"""A small MCP server (streamable HTTP), made from the mcp-scaffold template.

Replace the example tools with yours. Keep each tool small, typed and documented: the docstring is what the agent
reads to decide when to call it.

Run:  python server.py            (listens on 127.0.0.1:8765, path /mcp)
Test: python -m pytest -q test_server.py ; python smoke_client.py http://127.0.0.1:8765/mcp
"""
from __future__ import annotations

import os
from typing import Any

from mcp.server.mcpserver import MCPServer  # mcp >= 2 (requirements.txt)
from mcp.types import ToolAnnotations

SERVER_NAME = "notes"
HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "8765"))
MAX_NOTES = 1000

mcp = MCPServer(SERVER_NAME, instructions="A tiny notes service: add, list and read notes.")

# Example state, in memory. A real server talks to an API or a database instead (see the skill, "Wrapping an
# HTTP API"); its credential comes from the server's own environment, never from a tool argument.
_NOTES: dict[str, dict[str, Any]] = {}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_notes(query: str = "", limit: int = 20) -> list[dict[str, Any]]:
    """List notes whose title contains `query` (all notes when empty), newest first, at most `limit` (1-100)."""
    limit = max(1, min(int(limit), 100))
    found = [n for n in _NOTES.values() if query.lower() in n["title"].lower()]
    return sorted(found, key=lambda n: n["id"], reverse=True)[:limit]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_note(note_id: str) -> dict[str, Any]:
    """Return one note by its id, or an error message if it does not exist."""
    note = _NOTES.get(note_id)
    if note is None:
        raise ValueError(f"no note with id {note_id!r}")
    return note


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def add_note(title: str, text: str = "") -> dict[str, Any]:
    """Add a note. `title`: 1-120 characters. Returns the stored note with its id."""
    title = title.strip()
    if not 1 <= len(title) <= 120:
        raise ValueError("title must be 1 to 120 characters")
    if len(_NOTES) >= MAX_NOTES:
        raise ValueError("note limit reached")
    note_id = f"n{len(_NOTES) + 1:04d}"
    _NOTES[note_id] = {"id": note_id, "title": title, "text": text[:5000]}
    return _NOTES[note_id]


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host=HOST, port=PORT)
