"""Offline tests for Wave 5: jobtrack, snippet-vault, devlog, readme-changelog, scaffold."""
import asyncio
import sys
import tempfile
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
    async def jt(c):
        r = await c.call_tool("add_application", {"company": "Nova", "role": "SWE Intern", "followup_in_days": 0})
        await c.call_tool("update_status", {"application_id": r.data["id"], "status": "interview"})
        due = await c.call_tool("due_followups", {})
        s = await c.call_tool("stats", {})
        print("jobtrack OK — stats:", s.data["by_status"])
    await one("jobtrack", jt)

    async def sv(c):
        await c.call_tool("save_snippet", {"title": "debounce", "lang": "ts",
                                           "code": "export const debounce=(f,ms)=>{...}", "tags": "util,timing"})
        r = await c.call_tool("search", {"query": "debounce"})
        assert r.data and r.data[0]["title"] == "debounce"
        print("snippet-vault OK — search hit:", r.data[0]["title"])
    await one("snippet-vault", sv)

    async def dl(c):
        r = await c.call_tool("daily_log", {"repo": str(ROOT), "days": 365})
        print("devlog OK — commits in last year:", r.data["count"])
    await one("devlog", dl)

    async def rc(c):
        r = await c.call_tool("gen_readme", {"repo": str(ROOT)})
        assert "Python" in r.data["stack"]
        print("readme-changelog OK — detected stack:", r.data["stack"])
    await one("readme-changelog", rc)

    async def sc(c):
        with tempfile.TemporaryDirectory() as d:
            r = await c.call_tool("new_project", {"stack": "python-uv", "name": "demoapp", "dest": d})
            assert any("pyproject.toml" in f for f in r.data["files"])
            assert (Path(d) / "demoapp" / "src" / "demoapp" / "main.py").exists()
            print("scaffold OK — created", len(r.data["files"]), "files for python-uv")
    await one("scaffold", sc)

    print("\nWAVE 5 OK ✅")


asyncio.run(main())
