"""Hub router smoke test: it sees every server, searches, dispatches a real tool, and degrades
gracefully on bad input."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ["MCP_DATA_DIR"] = tempfile.mkdtemp(prefix="hubtest-")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "servers" / "hub"))
import server  # noqa: E402
from fastmcp import Client  # noqa: E402


async def main():
    async with Client(server.mcp) as c:
        ls = (await c.call_tool("list_servers", {})).data
        assert ls["count"] >= 40, ls["count"]

        st = (await c.call_tool("search_tools", {"query": "whatsapp"})).data
        assert st["count"] >= 1

        # dispatch a safe offline tool through the hub
        r = (await c.call_tool("run", {"server": "time-tracker", "tool": "log_block",
                                       "args": {"label": "via hub", "minutes": 5}})).data
        assert r["result"]["minutes"] == 5, r

        lt = (await c.call_tool("list_tools", {"server": "notes"})).data
        assert any(t["name"] == "search" for t in lt["tools"])

        # graceful errors
        bad = (await c.call_tool("run", {"server": "nope", "tool": "x", "args": {}})).data
        assert bad.get("ok") is False and "available" in bad
        badtool = (await c.call_tool("run", {"server": "notes", "tool": "nope", "args": {}})).data
        assert badtool.get("ok") is False

    print(f"HUB OK ✅ — routes to {ls['count']} servers; run/list/search/graceful-errors all work")


asyncio.run(main())
