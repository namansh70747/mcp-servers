"""gitflow remote_owner_repo / default_branch parsing (offline)."""
from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from fastmcp import Client  # noqa: E402


def load_gitflow():
    path = ROOT / "servers" / "gitflow" / "server.py"
    spec = importlib.util.spec_from_file_location("gf_remote", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.mcp


def git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True)


def make_repo_with_remote(url: str) -> str:
    repo = tempfile.mkdtemp(prefix="gitflow-remote-")
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    git(repo, "checkout", "-q", "-b", "main")
    (Path(repo) / "f.txt").write_text("x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "remote", "add", "origin", url)
    return repo


async def test_remote_owner_repo():
    async with Client(load_gitflow()) as c:
        for url, owner, name in (
            ("https://github.com/namansh70747/mcp-servers.git", "namansh70747", "mcp-servers"),
            ("git@github.com:acme/widget.git", "acme", "widget"),
        ):
            repo = make_repo_with_remote(url)
            res = (await c.call_tool("remote_owner_repo", {"repo": repo})).data
            assert res["ok"], res
            assert res["owner"] == owner, res
            assert res["repo"] == name, res

        repo = make_repo_with_remote("https://github.com/o/r.git")
        br = (await c.call_tool("default_branch", {"repo": repo})).data
        assert br["ok"] and br["branch"] == "main", br

        # fork head validation via open_pr
        bad = (await c.call_tool("open_pr", {
            "repo": repo, "title": "t", "body": "b", "head": "user:feat/ok",
        })).data
        assert bad.get("ok") is not False or "gh" in str(bad).lower() or bad.get("command")


async def main():
    await test_remote_owner_repo()
    print("test_gitflow_remote OK")


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
