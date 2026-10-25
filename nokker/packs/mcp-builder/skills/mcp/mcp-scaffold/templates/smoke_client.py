"""Smoke test of a running MCP server: connect, list the tools, call one read tool.

Usage: python smoke_client.py http://127.0.0.1:8765/mcp [tool] [json-arguments]
Exit code 0 when the server answers and the call succeeds.
"""
import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main(url: str, tool: str, arguments: dict) -> int:
    async with streamable_http_client(url) as (read, write, *_), ClientSession(read, write) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
        print("tools:", ", ".join(sorted(t.name for t in tools)))
        if tool not in {t.name for t in tools}:
            print(f"tool {tool!r} not found")
            return 1
        result = await session.call_tool(tool, arguments)
        for block in result.content:
            print(getattr(block, "text", block))
        return 1 if result.is_error else 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/mcp"
    name = sys.argv[2] if len(sys.argv) > 2 else "list_notes"
    args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
    sys.exit(asyncio.run(main(target, name, args)))
