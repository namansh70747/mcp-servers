"""Smoke test for codeindex: index this repo and exercise the deep-understanding tools."""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "servers" / "codeindex"))

import server  # noqa: E402
from fastmcp import Client  # noqa: E402


async def main():
    async with Client(server.mcp) as c:
        tools = sorted(t.name for t in await c.list_tools())
        print("tools:", tools)

        r = await c.call_tool("index_project", {"path": str(ROOT)})
        print("index:", r.data)
        assert r.data["indexed"] > 0

        r = await c.call_tool("status", {})
        print("status:", r.data)

        r = await c.call_tool("definition", {"name": "make_server"})
        print("definition make_server:", r.data)
        assert any("app.py" in d["path"] for d in r.data), "should find make_server in app.py"

        r = await c.call_tool("references", {"name": "BaseStore", "limit": 5})
        print("references BaseStore (first):", r.data[0] if r.data else None)
        assert r.data, "should find BaseStore references"

        r = await c.call_tool("symbols", {"path": "servers/codeindex/server.py"})
        names = [s["name"] for s in r.data]
        print("codeindex symbols (sample):", names[:6], "...", len(names), "total")
        assert "index_project" in names

        await c.call_tool("set_summary", {"path": "shared/mcp_base/store.py",
                                          "summary": "Thin SQLite wrapper: connections, migrations, dict queries."})
        r = await c.call_tool("file_summary", {"path": "shared/mcp_base/store.py"})
        print("file_summary:", r.data)
        assert r.data["agent_written"] is True

        r = await c.call_tool("relevant_context", {"task": "how do servers store data in sqlite"})
        print("relevant_context files:", [f["path"] for f in r.data["files"]])
        assert r.data["files"], "should return a context bundle"

        r = await c.call_tool("get_lines", {"path": "shared/mcp_base/store.py", "start": 1, "end": 5})
        print("get_lines head:\n", r.data["content"][:160])

        r = await c.call_tool("export_context_file", {"write": False})
        print("export preview (first 200):\n", r.data["preview"][:200])

        print("\nCODEINDEX OK ✅")


asyncio.run(main())
