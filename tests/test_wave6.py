"""Offline tests for Wave 6: interview-prep, learn-tracker, blog-drafter, api-tester, portfolio-site.
(github-profile + api-tester live runs need network/PAT.)"""
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
    async def ip(c):
        r = await c.call_tool("add_card", {"front": "What is FTS5?", "back": "SQLite full-text search", "topic": "sqlite"})
        due = await c.call_tool("due_cards", {})
        assert due.data
        rv = await c.call_tool("review", {"card_id": r.data["id"], "grade": 5})
        assert rv.data["interval_days"] >= 1
        await c.call_tool("add_problem", {"title": "Two Sum", "difficulty": "easy"})
        s = await c.call_tool("stats", {})
        print("interview-prep OK — review schedule:", rv.data["interval_days"], "d; stats:", s.data)
    await one("interview-prep", ip)

    async def lt(c):
        await c.call_tool("add_course", {"title": "Distributed Systems", "hours": 20, "status": "in_progress"})
        await c.call_tool("add_course", {"title": "Rust Book", "hours": 15})
        await c.call_tool("log_progress", {"course_id": 1, "progress_pct": 40})
        plan = await c.call_tool("generate_plan", {"hours_per_week": 6})
        print("learn-tracker OK — plan weeks:", len(plan.data["weeks"]))
    await one("learn-tracker", lt)

    async def bd(c):
        r = await c.call_tool("new_draft", {"title": "Building 30 MCP servers", "kind": "blog",
                                            "body": "## Why\n..."})
        e = await c.call_tool("export_md", {"draft_id": r.data["id"]})
        assert Path(e.data["path"]).exists()
        print("blog-drafter OK — exported:", Path(e.data['path']).name)
    await one("blog-drafter", bd)

    async def at(c):
        await c.call_tool("add_request", {"name": "gh", "url": "https://api.github.com", "method": "GET"})
        lst = await c.call_tool("list_requests", {})
        assert lst.data and lst.data[0]["name"] == "gh"
        print("api-tester OK — saved request listed")
    await one("api-tester", at)

    async def ps(c):
        r = await c.call_tool("build_site", {"theme": "dark"})
        html = Path(r.data["path"]).read_text()
        assert "Naman Sharma" in html and "<title>" in html
        print("portfolio-site OK — built", Path(r.data["path"]).name, f"({len(html)} bytes)")
    await one("portfolio-site", ps)

    print("\nWAVE 6 (offline) OK ✅  (github-profile needs your PAT)")


asyncio.run(main())
