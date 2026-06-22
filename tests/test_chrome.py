"""Offline tests for the generic chrome server. No real Chrome, no osascript.

The Chrome helper functions imported into the server are stubbed, so we exercise the tool surface
(open/run_js/read_tab/list_tabs/close_tab/health) and input validation without driving a browser.

Run:  VIRTUAL_ENV= .venv/bin/python tests/test_chrome.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import tempfile
from pathlib import Path

os.environ["MCP_NO_DOTENV"] = "1"
os.environ["MCP_DATA_DIR"] = tempfile.mkdtemp(prefix="chrome-test-")

from fastmcp import Client  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("chrome_t", ROOT / "servers" / "chrome" / "server.py")
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


def _mock(m):
    m.chrome_running = lambda: True
    m.js_enabled = lambda: (True, "")
    m.find_tab = lambda s: None
    m.open_tab = lambda u: (True, "")
    m.navigate = lambda a, b: True
    m._close = lambda s: True
    m._list = lambda: (True, [{"window": 1, "tab": 1, "url": "https://mail.google.com", "title": "Inbox"}])
    m._run_js = lambda url, js, timeout=30: (True, "BODYTEXT" if "innerText" in js else {"v": 2})


def test_registry():
    m = _load()
    assert {"health", "open", "run_js", "read_tab", "list_tabs", "close_tab"} <= _tools(m.mcp)


def test_validation():
    m = _load()
    _mock(m)
    assert _call(m.mcp, "open", {"url": ""})["ok"] is False
    assert _call(m.mcp, "run_js", {"url": "", "js": ""})["ok"] is False
    assert _call(m.mcp, "read_tab", {"url": ""})["ok"] is False
    assert _call(m.mcp, "close_tab", {"url": ""})["ok"] is False


def test_open_run_read_list():
    m = _load()
    _mock(m)
    assert _call(m.mcp, "open", {"url": "https://web.whatsapp.com/"})["ok"] is True
    rj = _call(m.mcp, "run_js", {"url": "mail.google.com", "js": "return JSON.stringify({v:2})"})
    assert rj["ok"] and rj["value"] == {"v": 2}, rj
    rt = _call(m.mcp, "read_tab", {"url": "mail.google.com"})
    assert rt["ok"] and rt["text"] == "BODYTEXT", rt
    lt = _call(m.mcp, "list_tabs", {})
    assert lt["ok"] and lt["count"] == 1, lt
    assert _call(m.mcp, "close_tab", {"url": "mail.google.com"})["ok"] is True


if __name__ == "__main__":
    for fn in (test_registry, test_validation, test_open_run_read_list):
        fn()
        print(fn.__name__, "OK")
    print("ALL CHROME TESTS PASSED")
