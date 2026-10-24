"""Producteurs (docs/BUBBLES.md §2) : web_search → search, infra_* → monitor, hook d'aperçus."""
from __future__ import annotations

import json

from nokker.bubbles.producers import Producers, is_monitor_tool
from nokker.core.ports import ToolSpec
from nokker.core.turn import Effect, ToolCall, ToolResult


def _web_content(query="lvm extend", n=12):
    results = [{"rank": i + 1, "url": f"https://site{i}.org/p", "title": f"T{i}", "snippet": f"s{i}",
                "domain": f"site{i}.org", "engines": ["exa"]} for i in range(n)]
    payload = {"query": query, "limit": n, "engines_queried": ["exa", "keenable"], "results": results}
    return f"1. T0 …\n\n<!-- nokker:web_search:data -->\n{json.dumps(payload)}\n<!-- /nokker:web_search:data -->"


def test_web_search_produces_search_bubble_and_requests_previews():
    asked = []
    p = Producers(request_previews=asked.append)
    out = p.produce(ToolCall("c1", "web_search", {"query": "lvm extend"}), None,
                    ToolResult("c1", "web_search", _web_content()), took_ms=812)
    assert out.kind == "search" and out.title == "lvm extend"
    st = out.state
    assert st["query"] == "lvm extend" and st["took_ms"] == 812 and st["engines"] == ["exa", "keenable"]
    assert len(st["results"]) == 10
    assert st["results"][0] == {"url": "https://site0.org/p", "title": "T0", "snippet": "s0", "site": "site0.org"}
    from nokker.bubbles.types import Bubble

    b = Bubble(id="bu_x", thread_id="th", kind="search", state=st)
    p.after_create(b)
    assert asked == [b]


def test_web_search_error_or_unparsable_produces_nothing():
    p = Producers()
    assert p.produce(ToolCall("c", "web_search", {}), None, ToolResult("c", "web_search", "x", is_error=True)) is None
    assert p.produce(ToolCall("c", "web_search", {}), None, ToolResult("c", "web_search", "pas de données")) is None


def test_disk_usage_produces_monitor():
    p = Producers()
    out = p.produce(ToolCall("c", "infra_disk_usage", {"host": "hote21", "path": "/srv/share"}),
                    ToolSpec("infra_disk_usage", "", {}, Effect.READ), ToolResult("c", "infra_disk_usage", "87% utilisé"))
    assert out.kind == "monitor"
    assert out.state["target"] == "hote21:/srv/share" and out.state["metric"] == "disk"
    assert out.state["value"] == 87.0 and out.state["unit"] == "%" and out.state["max"] == 100


def test_gpu_info_produces_monitor_with_items():
    p = Producers()
    text = "GPU 0: NVIDIA GeForce RTX 3090\nGPU 1: NVIDIA GeForce RTX 3090"
    out = p.produce(ToolCall("c", "infra_gpu_info", {"host": "hote21"}), ToolSpec("infra_gpu_info", "", {}, Effect.READ),
                    ToolResult("c", "infra_gpu_info", text))
    assert out.kind == "monitor" and out.state["metric"] == "gpu"
    assert [i["label"] for i in out.state["items"]] == text.splitlines()


def test_monitor_only_for_read_tools():
    assert is_monitor_tool("infra_disk_usage", None)
    assert not is_monitor_tool("infra_lvextend", ToolSpec("infra_lvextend", "", {}, Effect.DESTRUCTIVE))
    assert not is_monitor_tool("infra_restart_status", ToolSpec("x", "", {}, Effect.WRITE))
    assert is_monitor_tool("infra_net_stats", ToolSpec("x", "", {}, Effect.READ))
