"""hub — one router to reach EVERY connector in this suite without flooding the agent's tool list.

The problem: with ~50 servers / ~700 tools wired directly, an agent's model can't reliably pick the
right tool (it may not even be sent them). The fix: load a small DIRECT set of daily-driver servers PLUS
this `hub`. The hub exposes only a handful of tools but can reach ALL servers:

    list_servers()                       → every connector + one-line purpose
    search_tools("whatsapp send")        → find tools across all servers by keyword
    list_tools("whatsapp")               → that server's tools
    help("whatsapp", "send")             → a tool's description + parameters
    run("whatsapp", "send", {...})       → run any tool on any connector and get the result

Lazy + cached: the hub starts instantly and only imports a target server the first time you use it.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

from fastmcp import Client
from mcp_base import err, make_server, not_found

mcp = make_server(
    "hub",
    instructions=(
        "Router to every MCP connector in the suite. To use a connector that ISN'T already one of your "
        "direct tools: call list_servers() (or search_tools(query)) to find it, then "
        "run(server, tool, args) to execute it and get the result. Example: "
        "run('whatsapp','send',{'contact':'Mamma','message':'hi'}). Use list_tools(server)/help(server,tool) "
        "to see exact tool names and parameters."
    ),
)

SERVERS_DIR = Path(__file__).resolve().parent.parent
ROOT = SERVERS_DIR.parent
SELF = "hub"
_mod_cache: dict[str, object] = {}


def _server_names() -> list[str]:
    return sorted(p.parent.name for p in SERVERS_DIR.glob("*/server.py") if p.parent.name != SELF)


def _purpose(name: str) -> str:
    """First line of a server's module docstring — cheap (no import)."""
    try:
        tree = ast.parse((SERVERS_DIR / name / "server.py").read_text(encoding="utf-8", errors="ignore"))
        doc = (ast.get_docstring(tree) or "").strip()
        return doc.splitlines()[0][:160] if doc else ""
    except Exception:
        return ""


_mod_errors: dict[str, str] = {}  # name → last import error message (for diagnostics)

# Curated set exposed in lite/minimal profiles — high-utility daily-driver servers.
LITE_SERVERS = [
    "email-finder", "emailcheck", "whatsapp", "voice", "browser",
    "contacts", "webscrape", "task-manager", "mailbox", "reachout",
    "notes", "devlog", "codeindex",
]


def _load(name: str):
    """Lazy-import a target server module once, error-isolated + cached.

    A broken server import is recorded in _mod_errors and returns None — it never
    propagates an exception that would crash the hub or poison the module cache.
    """
    if name in _mod_cache:
        return _mod_cache[name]
    sp = SERVERS_DIR / name / "server.py"
    if not sp.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location(f"hub_t_{name.replace('-', '_')}", sp)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        _mod_cache[name] = mod
        _mod_errors.pop(name, None)   # clear any previous error
        return mod
    except Exception as exc:  # noqa: BLE001
        _mod_errors[name] = str(exc)[:200]
        _mod_cache.pop(name, None)    # don't cache a broken module
        return None


@mcp.tool
def list_servers() -> dict:
    """List every connector the hub can reach, each with a one-line purpose."""
    names = _server_names()
    return {"count": len(names),
            "servers": [{"name": n, "purpose": _purpose(n)} for n in names],
            "hint": "run(server, tool, args) to use one; search_tools(query) to find a tool"}


@mcp.tool
async def list_tools(server: str) -> dict:
    """List the tools of one connector (name + one-line description)."""
    names = _server_names()
    if server not in names:
        return not_found("server", server, available=names, hint="call list_servers()")
    mod = _load(server)
    if mod is None:
        return err(f"could not load server '{server}'")
    try:
        async with Client(mod.mcp) as c:
            tools = await c.list_tools()
        return {"server": server, "tools": [
            {"name": t.name, "description": (t.description or "").split("\n")[0][:160]} for t in tools]}
    except Exception as e:  # noqa: BLE001
        return err(f"could not list tools for '{server}': {e}")


@mcp.tool
async def help(server: str, tool: str) -> dict:
    """Show a tool's full description and its parameter schema."""
    names = _server_names()
    if server not in names:
        return not_found("server", server, available=names, hint="call list_servers()")
    mod = _load(server)
    if mod is None:
        return err(f"could not load server '{server}'")
    try:
        async with Client(mod.mcp) as c:
            for t in await c.list_tools():
                if t.name == tool:
                    return {"server": server, "tool": tool, "description": t.description,
                            "parameters": (getattr(t, "inputSchema", {}) or {}).get("properties", {})}
            return not_found("tool", tool, available=[t.name for t in await c.list_tools()],
                             hint=f"call list_tools('{server}')")
    except Exception as e:  # noqa: BLE001
        return err(f"help failed for {server}.{tool}: {e}")


@mcp.tool
def search_tools(query: str, limit: int = 40) -> dict:
    """Find tools across ALL connectors by keyword. Cheap: matches server names/purposes and, if present,
    the generated TOOLS.md catalog — without importing every server."""
    q = (query or "").strip().lower()
    if not q:
        return err("query is required")
    matches: list[dict] = []
    # 1) server name + purpose (no import)
    for n in _server_names():
        if q in n.lower() or q in _purpose(n).lower():
            matches.append({"server": n, "tool": "*", "hint": _purpose(n)})
    # 2) tool-level matches from TOOLS.md (generated catalog), tracking the current server heading
    tools_md = ROOT / "TOOLS.md"
    if tools_md.exists():
        cur = None
        try:
            for line in tools_md.read_text(encoding="utf-8", errors="ignore").splitlines():
                s = line.strip()
                if s.startswith("#"):
                    head = s.lstrip("#").strip().split()[0] if s.lstrip("#").strip() else ""
                    if head in set(_server_names()):
                        cur = head
                elif q in s.lower() and cur:
                    matches.append({"server": cur, "line": s[:200]})
                if len(matches) >= limit:
                    break
        except Exception:
            pass
    # dedupe + cap
    seen, out = set(), []
    for m in matches:
        k = (m.get("server"), m.get("tool"), m.get("line"))
        if k not in seen:
            seen.add(k)
            out.append(m)
    return {"query": query, "count": len(out[:limit]), "matches": out[:limit],
            "hint": "then: run(server, tool, args)"}


@mcp.tool
async def run(server: str, tool: str, args: dict | None = None) -> dict:
    """Run ANY tool on ANY connector and return its result. This is how you reach a connector that isn't
    one of your direct tools. Example: run('whatsapp','send',{'contact':'Mamma','message':'I'm late'})."""
    names = _server_names()
    if server not in names:
        return not_found("server", server, available=names, hint="call list_servers()")
    mod = _load(server)
    if mod is None:
        return err(f"could not load server '{server}'")
    try:
        async with Client(mod.mcp) as c:
            avail = [t.name for t in await c.list_tools()]
            if tool not in avail:
                return not_found("tool", tool, available=avail, hint=f"call list_tools('{server}')")
            res = await c.call_tool(tool, args or {})
            return {"server": server, "tool": tool, "result": getattr(res, "data", None)}
    except Exception as e:  # noqa: BLE001
        return err(f"{server}.{tool} failed: {e}")


@mcp.tool
def warm(servers: list | None = None) -> dict:
    """Pre-load a list of servers into the hub's module cache so subsequent run() calls are instant.

    Defaults to the curated LITE_SERVERS list. Pass a custom list to warm specific servers.
    Use this at session start to eliminate cold-start latency for your daily-driver tools.
    Returns a per-server status (loaded / error / unknown)."""
    targets = servers if isinstance(servers, list) and servers else LITE_SERVERS
    results: dict[str, str] = {}
    for name in targets:
        if name not in _server_names():
            results[name] = "unknown"
            continue
        mod = _load(name)
        if mod is not None:
            results[name] = "loaded"
        else:
            results[name] = f"error: {_mod_errors.get(name, 'could not load')}"
    return {"warmed": sum(1 for s in results.values() if s == "loaded"),
            "errors": sum(1 for s in results.values() if s.startswith("error")),
            "results": results}


if __name__ == "__main__":
    mcp.run()
