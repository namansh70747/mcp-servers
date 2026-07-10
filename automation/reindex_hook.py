"""Reindex a repo with codeindex and refresh its CLAUDE.md/AGENTS.md context files.
Called by the post-commit hook (install_hooks.sh / install_hooks.ps1).
Usage: reindex_hook.py [repo_path]"""
import asyncio
import importlib.util
import os
import sys
from pathlib import Path


def _suite_root() -> Path:
    env = os.environ.get("MCP_SUITE_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


SUITE = _suite_root()
repo = str(Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve())

spec = importlib.util.spec_from_file_location("codeindex_srv", SUITE / "servers" / "codeindex" / "server.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["codeindex_srv"] = mod
spec.loader.exec_module(mod)
from fastmcp import Client  # noqa: E402


async def main():
    async with Client(mod.mcp) as c:
        try:
            await c.call_tool("reindex", {"project": repo})
        except Exception:
            await c.call_tool("index_project", {"path": repo})
        await c.call_tool("export_context_file", {"project": repo})


asyncio.run(main())
print(f"codeindex: reindexed + context files refreshed for {repo}")
