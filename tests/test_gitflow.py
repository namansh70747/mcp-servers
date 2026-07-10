"""Offline tests for the gitflow server via FastMCP's in-memory client.

Run: VIRTUAL_ENV= .venv/bin/python tests/test_gitflow.py
Operates on a throwaway `git init`ed temp repo. No network, no credentials, no `gh` required
(open_pr is exercised only on its graceful-degradation path)."""
import asyncio
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from fastmcp import Client  # noqa: E402


def load(name: str):
    path = ROOT / "servers" / name / "server.py"
    spec = importlib.util.spec_from_file_location(f"gf_{name.replace('-', '_')}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.mcp


async def tools(client) -> set:
    return {t.name for t in await client.list_tools()}


async def call(c, name, args):
    return (await c.call_tool(name, args)).data


def git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True)


def make_repo() -> str:
    repo = tempfile.mkdtemp(prefix="gitflow-test-")
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    git(repo, "checkout", "-q", "-b", "main")
    (Path(repo) / "README.md").write_text("# demo\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "chore: initial commit")
    return repo


async def test_gitflow():
    repo = make_repo()
    async with Client(load("gitflow")) as c:
        ts = await tools(c)
        contract = {"status", "current_branch", "create_branch", "stage", "commit",
                    "push", "diff", "log", "pr_body", "open_pr", "remote_owner_repo",
                    "default_branch", "add_remote", "fork_push_instructions"}
        assert contract <= ts, f"missing contract tools: {contract - ts}"

        # current_branch
        cb = await call(c, "current_branch", {"repo": repo})
        assert cb["ok"] and cb["branch"] == "main", cb

        # status on a clean repo
        st = await call(c, "status", {"repo": repo})
        assert st["ok"] and st["clean"] is True, st

        # create a feature branch
        br = await call(c, "create_branch", {"repo": repo, "name": "feature/x"})
        assert br["ok"] and br["branch"] == "feature/x", br
        assert (await call(c, "current_branch", {"repo": repo}))["branch"] == "feature/x"

        # invalid branch names rejected (validation / no injection)
        for bad in ("bad name", "..", "-x", "feat:x", "a~b", ""):
            r = await call(c, "create_branch", {"repo": repo, "name": bad})
            assert not r["ok"], f"name {bad!r} should be rejected: {r}"
        # duplicate rejected
        dup = await call(c, "create_branch", {"repo": repo, "name": "feature/x"})
        assert not dup["ok"] and "exists" in dup["error"], dup

        # make a change -> status sees it
        (Path(repo) / "app.py").write_text("print('hi')\n")
        (Path(repo) / "README.md").write_text("# demo\nmore\n")
        st2 = await call(c, "status", {"repo": repo})
        assert not st2["clean"] and "app.py" in st2["untracked"], st2

        # stage all
        sg = await call(c, "stage", {"repo": repo})
        assert sg["ok"] and "app.py" in sg["staged"], sg

        # staged diff has stats
        d = await call(c, "diff", {"repo": repo, "staged": True})
        assert d["ok"] and d["files_changed"] >= 2 and d["insertions"] >= 2, d
        assert any(f["file"] == "app.py" for f in d["files"]), d

        # nothing-to-commit error path (commit then re-commit)
        cm = await call(c, "commit", {"repo": repo, "message": "feat: add app"})
        assert cm["ok"] and cm["sha"], cm
        empty = await call(c, "commit", {"repo": repo, "message": "again"})
        assert not empty["ok"] and "nothing to commit" in empty["error"], empty
        # empty message rejected
        assert not (await call(c, "commit", {"repo": repo, "message": "  "}))["ok"]

        # log
        lg = await call(c, "log", {"repo": repo, "n": 10})
        assert lg["ok"] and lg["total"] >= 2, lg
        assert any(co["subject"] == "feat: add app" for co in lg["commits"]), lg

        # pr_body synthesizes a title + markdown body from the diff vs main
        pb = await call(c, "pr_body", {"repo": repo, "from_ref": "main", "to_ref": "HEAD"})
        assert pb["ok"], pb
        assert pb["title"] and ":" in pb["title"], pb["title"]
        assert "## Summary" in pb["body"] and "## Checklist" in pb["body"], pb["body"]
        assert pb["files_changed"] >= 1 and "app.py" in {f["file"] for f in pb["files"]}, pb
        assert "feat: add app" in pb["commits"], pb

        # pr_body accepts optional task
        pb_task = await call(c, "pr_body", {"repo": repo, "task": "implement feature X"})
        assert pb_task["ok"] and "implement feature X" in pb_task["body"], pb_task

        # pr_body with default base (auto-detect) still works
        pb2 = await call(c, "pr_body", {"repo": repo})
        assert pb2["ok"] and pb2["title"], pb2
        # invalid ref rejected
        assert not (await call(c, "pr_body", {"repo": repo, "to_ref": "bad ref"}))["ok"]

        # push with no remote -> graceful error, lists remotes
        ps = await call(c, "push", {"repo": repo})
        assert not ps["ok"] and "remote" in ps["error"].lower(), ps
        assert ps.get("remotes") == [], ps

        # open_pr returns a structured result. With no remote it either degrades (gh absent)
        # to a ready-to-run command, or surfaces gh's error (gh present but no remote/auth).
        op = await call(c, "open_pr", {"repo": repo, "title": "feat: add app",
                                       "body": "body text", "base": "main"})
        if op["ok"]:
            # gh missing -> degraded command path
            assert op["created"] is False and op["command"].startswith("gh pr create"), op
            assert op["fallback"]["server"] == "github", op
            assert op["fallback"]["tool"] == "create_pull_request", op
        else:
            # gh present but the temp repo has no remote/auth -> structured error w/ command
            assert op["command"].startswith("gh pr create"), op
        # open_pr requires a title
        assert not (await call(c, "open_pr", {"repo": repo, "title": ""}))["ok"]
        # invalid base rejected
        assert not (await call(c, "open_pr", {"repo": repo, "title": "x", "base": "bad base"}))["ok"]

    # not-a-git-repo degrades gracefully
    nonrepo = tempfile.mkdtemp(prefix="gitflow-nonrepo-")
    async with Client(load("gitflow")) as c:
        r = await call(c, "status", {"repo": nonrepo})
        assert not r["ok"] and "not a git repo" in r["error"], r
        r2 = await call(c, "current_branch", {"repo": nonrepo})
        assert not r2["ok"], r2
        # missing path
        assert not (await call(c, "status", {"repo": nonrepo + "/nope"}))["ok"]

    print("gitflow OK:", len(ts), "tools")


async def main():
    await test_gitflow()
    print("\nALL GITFLOW TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
