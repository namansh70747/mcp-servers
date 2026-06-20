"""Offline test: every step emitted by every recipes playbook must reference a
(server, tool) pair that ACTUALLY EXISTS in the current server set.

Builds the ground-truth map {server: {tools}} by importing each servers/*/server.py
in isolation and listing its real tools via the FastMCP in-memory Client, then imports
recipes, calls every playbook, and asserts each step's {server, tool} is real.

No network or credentials required.
"""
import asyncio
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVERS_DIR = ROOT / "servers"

from fastmcp import Client  # noqa: E402


def _load_server_module(name: str):
    """Import servers/<name>/server.py under a unique module name (each file is
    literally `server.py`, so we can't rely on a shared `server` module name)."""
    path = SERVERS_DIR / name / "server.py"
    mod_name = f"_recipes_refs_srv_{name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


async def _tools_of(mod) -> set[str]:
    async with Client(mod.mcp) as c:
        return {t.name for t in await c.list_tools()}


async def build_tool_map() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for d in sorted(SERVERS_DIR.iterdir()):
        if not (d / "server.py").exists():
            continue
        mod = _load_server_module(d.name)
        out[d.name] = await _tools_of(mod)
    return out


async def collect_recipe_steps(recipes_mod) -> list[dict]:
    """Call every playbook tool and collect every emitted step."""
    steps: list[dict] = []
    # Playbook tools and a minimal valid args set for each.
    calls = [
        ("weekly_outreach", {}),
        ("prep_for_company", {"company": "Acme"}),
        ("apply_to_job", {"jd_text": "We need a Python engineer with FTS5 experience."}),
        ("daily_briefing", {}),
        ("ship_project", {"repo": "/tmp/example-repo"}),
    ]
    async with Client(recipes_mod.mcp) as c:
        listed = {t.name for t in await c.list_tools()}
        # sanity: the playbooks we test must all still exist
        for name, _ in calls:
            assert name in listed, f"recipes playbook missing: {name}"
        for name, args in calls:
            res = await c.call_tool(name, args)
            data = res.data
            assert data.get("ok") is True, f"{name} did not return ok: {data}"
            for s in data.get("steps", []):
                s["_recipe"] = name
                steps.append(s)
    return steps


async def main():
    tool_map = await build_tool_map()
    assert "recipes" in tool_map, "recipes server not found"

    recipes_mod = _load_server_module("recipes")
    steps = await collect_recipe_steps(recipes_mod)
    assert steps, "no steps emitted by any playbook"

    problems = []
    for s in steps:
        srv, tool = s.get("server"), s.get("tool")
        if srv not in tool_map:
            problems.append(f"[{s['_recipe']}] unknown server '{srv}' (tool '{tool}')")
        elif tool not in tool_map[srv]:
            problems.append(
                f"[{s['_recipe']}] {srv}.{tool} does not exist. "
                f"available: {sorted(tool_map[srv])}"
            )

    assert not problems, "Recipe steps reference non-existent tools:\n  " + "\n  ".join(problems)

    print(f"OK — {len(steps)} steps across all playbooks, all reference real tools "
          f"across {len(tool_map)} servers.")


if __name__ == "__main__":
    asyncio.run(main())
