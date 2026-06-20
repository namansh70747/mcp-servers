"""Smoke test for project-memory via FastMCP's in-memory client."""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "servers" / "project-memory"))

import server  # noqa: E402
from fastmcp import Client  # noqa: E402

PROJ = "/tmp/demo-project"


async def main():
    async with Client(server.mcp) as c:
        tools = sorted(t.name for t in await c.list_tools())
        print("tools:", tools)

        r = await c.call_tool("remember", {"note": "chose SQLite over JSON for trackers",
                                           "kind": "decision", "project": PROJ})
        print("remember:", r.data)
        await c.call_tool("remember", {"note": "follow repo naming: kebab-case dirs",
                                       "kind": "convention", "project": PROJ})

        r = await c.call_tool("recall", {"query": "SQLite", "project": PROJ})
        assert any("SQLite" in m["note"] for m in r.data), "recall failed"
        print("recall hit:", r.data[0]["note"])

        await c.call_tool("checkpoint", {"summary": "wired project-memory",
                                         "open_items": "build codeindex next", "project": PROJ})
        r = await c.call_tool("resume", {"project": PROJ})
        assert r.data["last_checkpoint"]["summary"] == "wired project-memory"
        print("resume last_checkpoint:", r.data["last_checkpoint"]["summary"])
        print("resume recent count:", len(r.data["recent"]))

        r = await c.call_tool("stats", {"project": PROJ})
        print("stats:", r.data)
        print("\nPROJECT-MEMORY OK ✅")


asyncio.run(main())
