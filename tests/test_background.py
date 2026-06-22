"""Offline tests for the background server. No real `claude -p` is spawned.

We verify the tool registry, input validation, and the run → background-job → inline-result plumbing
by swapping the worker factory for a stub (and faking the claude binary path). The real worker
(detached `claude -p` subprocess) is never launched.

Run:  VIRTUAL_ENV= .venv/bin/python tests/test_background.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import tempfile

os.environ["MCP_NO_DOTENV"] = "1"
os.environ["MCP_DATA_DIR"] = tempfile.mkdtemp(prefix="bg-test-")

from fastmcp import Client  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("background_t", ROOT / "servers" / "background" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _call(mcp, tool, args):
    async def go():
        async with Client(mcp) as c:
            return (await c.call_tool(tool, args)).data
    return asyncio.run(go())


def _tools(mcp):
    async def go():
        async with Client(mcp) as c:
            return {t.name for t in await c.list_tools()}
    return asyncio.run(go())


def test_registry():
    m = _load()
    assert {"health", "run", "job_status", "list_jobs", "result", "cancel_job"} <= _tools(m.mcp)


def test_validation():
    m = _load()
    assert _call(m.mcp, "run", {"task": ""})["ok"] is False
    assert _call(m.mcp, "run", {"task": "   "})["ok"] is False
    assert _call(m.mcp, "job_status", {"job_id": "nope"})["ok"] is False
    assert _call(m.mcp, "list_jobs", {})["ok"] is True
    assert _call(m.mcp, "result", {"job_id": "nope"})["ok"] is False


def test_run_job_plumbing():
    m = _load()
    m._claude_bin = lambda: "/usr/bin/true"            # pretend claude exists
    m._worker_for = lambda task, t: (lambda job: m.JOBS.finish(job["id"], ok_=True,
                                                               summary=f"did: {task}", exit_code=0))
    r = _call(m.mcp, "run", {"task": "send 'hi' to Alice on WhatsApp"})
    assert r["ok"], r
    assert "job_id" in r
    assert r.get("summary", "").startswith("did:") or r.get("status") == "running", r
    # status of that job resolves
    if r.get("job_id"):
        st = _call(m.mcp, "job_status", {"job_id": r["job_id"]})
        assert st["ok"]


if __name__ == "__main__":
    for fn in (test_registry, test_validation, test_run_job_plumbing):
        fn()
        print(fn.__name__, "OK")
    print("ALL BACKGROUND TESTS PASSED")
