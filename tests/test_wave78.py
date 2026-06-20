"""Wave 7 (offline) + Wave 8 (live macOS/brew) smoke tests."""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from fastmcp import Client  # noqa: E402


async def one(name, fn):
    sys.path.insert(0, str(ROOT / "servers" / name))
    import server  # noqa
    async with Client(server.mcp) as c:
        await fn(c)
    del sys.modules["server"]


async def main():
    async def tm(c):
        r = await c.call_tool("add_task", {"title": "Ship suite", "priority": "high", "due": "2020-01-01"})
        due = await c.call_tool("list_due", {})
        assert any(t["title"] == "Ship suite" for t in due.data)
        cp = await c.call_tool("calendar_payload", {"task_id": r.data["id"]})
        assert cp.data["summary"] == "Ship suite"
        print("task-manager OK — due + calendar_payload")
    await one("task-manager", tm)

    async def nt(c):
        await c.call_tool("new_note", {"title": "MCP Suite", "body": "Built on [[FastMCP]] and [[SQLite]]."})
        await c.call_tool("new_note", {"title": "FastMCP", "body": "Python MCP framework."})
        bl = await c.call_tool("backlinks", {"title": "FastMCP"})
        assert any(n["title"] == "MCP Suite" for n in bl.data)
        s = await c.call_tool("search", {"query": "framework"})
        print("notes OK — backlinks + FTS:", [n["title"] for n in bl.data])
    await one("notes", nt)

    async def tt(c):
        await c.call_tool("log_block", {"label": "coding", "minutes": 90, "category": "build"})
        await c.call_tool("log_block", {"label": "email", "minutes": 30, "category": "comms"})
        rep = await c.call_tool("weekly_report", {})
        assert rep.data["total_hours"] == 2.0
        print("time-tracker OK — weekly:", rep.data["by_category"])
    await one("time-tracker", tt)

    async def bv(c):
        await c.call_tool("add_bookmark", {"url": "https://gofastmcp.com", "title": "FastMCP docs",
                                           "tags": "mcp,python", "fetch_title": False})
        r = await c.call_tool("search", {"query": "fastmcp"})
        assert r.data and "gofastmcp" in r.data[0]["url"]
        print("bookmark-vault OK — saved + searched")
    await one("bookmark-vault", bv)

    # --- Wave 8: live macOS ---
    async def mc(c):
        b = await c.call_tool("battery", {})
        await c.call_tool("set_clipboard", {"text": "mcp-suite-test"})
        cb = await c.call_tool("get_clipboard", {})
        v = await c.call_tool("get_volume", {})
        print("mac-control OK — clipboard roundtrip:", cb.data["text"] == "mcp-suite-test",
              "| volume:", v.data.get("volume"), "| battery ok:", b.data.get("ok"))
    await one("mac-control", mc)

    async def hb(c):
        r = await c.call_tool("list_installed", {})
        print("homebrew OK — installed query ok:", r.data.get("ok"),
              "| sample:", (r.data.get("out", "") or "")[:60].replace("\n", " "))
    await one("homebrew", hb)

    print("\nWAVE 7 + 8 OK ✅")


asyncio.run(main())
