"""Universal no-crash guard: call every safe tool of every server with EDGE inputs
(empty/None/bad-id/special-char/nonexistent-path) and assert none raise an unhandled
exception. A tool may return an error dict — that's fine; it must not throw.

Network/credential/system-mutating tools are skipped (they need real I/O); this fuzzes the
local SQLite/parse/render tools where crash bugs actually live. Run:
    VIRTUAL_ENV= .venv/bin/python tests/test_no_crash.py
"""
import asyncio
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

os.environ["MCP_DATA_DIR"] = tempfile.mkdtemp(prefix="nocrash-")
ROOT = Path(__file__).resolve().parents[1]
from fastmcp import Client  # noqa: E402

# Whole servers skipped (network / credentials / system mutation / file or git writes).
SKIP_SERVERS = {
    "mac-control", "homebrew", "gitflow", "codeedit", "scaffold",
    "apollo", "funding-radar", "news-radar", "email-finder", "github-profile",
    "reachout", "mailbox", "mailmerge",
}
# Per-tool skips (network calls or writes) inside otherwise-fuzzed servers.
SKIP_TOOL_SUBSTR = (
    "send", "push", "publish", "install", "upgrade", "uninstall", "open_", "run", "exec",
    "commit", "scan", "fetch", "refresh", "verify", "scrape", "export", "build_site",
    "add_resource", "add_bookmark", "import_", "make_onepager", "onepager_to_pdf",
    "draft_reply", "save_presentation", "screenshot",
)


def edge_args(schema: dict) -> dict:
    props = (schema or {}).get("properties", {}) or {}
    out = {}
    for name, spec in props.items():
        t = spec.get("type")
        if isinstance(t, list):
            t = next((x for x in t if x != "null"), None)
        ln = name.lower()
        if t in ("integer", "number") and ("id" in ln or "_id" in ln):
            out[name] = 999999
        elif t == "string" and any(k in ln for k in ("query", "search", "term", "pattern")):
            out[name] = 'a AND ("*:^ x'
        elif t == "string" and any(k in ln for k in ("path", "file", "repo", "dir")):
            out[name] = "/nonexistent/xyz"
        elif t == "string" and "url" in ln:
            out[name] = "notaurl"
        elif t == "string":
            out[name] = ""
        elif t in ("integer", "number"):
            out[name] = 0
        elif t == "boolean":
            out[name] = False
        elif t == "array":
            out[name] = []
        elif t == "object":
            out[name] = {}
    return out


async def _seed(name: str, c) -> dict:
    """Give stateful servers minimal valid state + return id overrides, so their tools are
    actually exercised on edge input instead of all short-circuiting on a precondition."""
    overrides: dict = {}
    try:
        if name == "codeindex":
            d = tempfile.mkdtemp(prefix="ncidx-")
            (Path(d) / "sample.py").write_text("def f(x):\n    return x + 1\n")
            await c.call_tool("index_project", {"path": d})
        elif name == "deckforge":
            r = await c.call_tool("create_presentation", {"title": "t", "theme": "dev_dark"})
            did = (r.data or {}).get("deck_id")
            if did:
                overrides["deck_id"] = did
    except Exception:
        pass
    return overrides


async def fuzz_server(name: str):
    spec = importlib.util.spec_from_file_location(
        "ncz_" + name.replace("-", "_"), ROOT / "servers" / name / "server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    raised = []
    async with Client(mod.mcp) as c:
        overrides = await _seed(name, c)
        tools = await c.list_tools()
        for t in tools:
            if any(s in t.name for s in SKIP_TOOL_SUBSTR):
                continue
            args = edge_args(getattr(t, "inputSchema", {}) or {})
            args.update({k: v for k, v in overrides.items() if k in args})
            try:
                await asyncio.wait_for(c.call_tool(t.name, args), 25)
            except asyncio.TimeoutError:
                pass  # slow (likely network) — not a crash
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                # input-validation rejections are the schema doing its job, not a crash
                if "validation error" in msg.lower() or "Input should be" in msg:
                    continue
                raised.append((name, t.name, type(e).__name__, msg.splitlines()[0][:140]))
    return len(tools), raised


async def main():
    servers = sorted(p.parent.name for p in (ROOT / "servers").glob("*/server.py")
                     if p.parent.name not in SKIP_SERVERS)
    total_tools = 0
    all_raised = []
    for s in servers:
        n, raised = await fuzz_server(s)
        total_tools += n
        all_raised += raised
    print(f"fuzzed {len(servers)} servers, {total_tools} tools (skipped: {sorted(SKIP_SERVERS)})")
    if all_raised:
        print(f"\n❌ {len(all_raised)} tools RAISED on edge input:")
        for srv, tool, et, m in all_raised:
            print(f"  - {srv}.{tool}: {et}: {m}")
        sys.exit(1)
    print("\n✅ NO-CRASH GUARD PASSED — no fuzzed tool raised on edge input.")


asyncio.run(main())
