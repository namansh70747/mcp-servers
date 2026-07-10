"""recipes.run(dry_run=True) resolves input_from chains."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from fastmcp import Client  # noqa: E402


def load_recipes():
    path = ROOT / "servers" / "recipes" / "server.py"
    spec = importlib.util.spec_from_file_location("recipes_run", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.mcp


async def test_dry_run_review_and_pr():
    async with Client(load_recipes()) as c:
        res = (await c.call_tool("run", {
            "recipe": "review_and_pr",
            "params": {"task": "fix bug", "repo": str(ROOT)},
            "dry_run": True,
        })).data
        assert res.get("ok") is True, res
        assert res.get("dry_run") is True
        steps = res.get("steps") or []
        assert len(steps) >= 8
        pr_steps = [s for s in steps if s.get("tool") == "pr_body"]
        assert pr_steps and pr_steps[0]["args"].get("task") == "fix bug"

        weekly = (await c.call_tool("run", {
            "recipe": "weekly_outreach",
            "params": {},
            "dry_run": True,
        })).data
        tools = [(s["server"], s["tool"]) for s in weekly.get("steps", [])]
        assert ("campaign", "filter_uncontacted") in tools
        assert ("campaign", "record_outreach") in tools

        prime = (await c.call_tool("prime_session", {"project": str(ROOT)})).data
        assert prime.get("ok") and prime.get("project")


async def main():
    await test_dry_run_review_and_pr()
    print("test_recipes_run OK")


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
