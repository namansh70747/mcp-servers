"""Entry-point launchers for uvx-only clients (Qwen Desktop, etc.).

- `run-mcp-server <name>`: launch one custom server (servers/<name>/server.py) as __main__.
- `run-mcp-hub <hub>`:     launch one hub that re-exposes many servers under namespaced names.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from mcp_base import repo_root

# uvx caches this package elsewhere, so __file__-based paths break. Prefer explicit
# MCP_SUITE_ROOT (set in the client config env); fall back to repo_root() (editable mcp-base).
REPO_ROOT = Path(os.environ.get("MCP_SUITE_ROOT") or repo_root())


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: run-mcp-server <server-name>", file=sys.stderr)
        sys.exit(1)

    name = sys.argv[1]
    server_path = REPO_ROOT / "servers" / name / "server.py"
    if not server_path.exists():
        print(f"run-mcp-server: server '{name}' not found at {server_path}", file=sys.stderr)
        sys.exit(1)

    # Load as __main__ so `if __name__ == "__main__": mcp.run()` fires
    spec = importlib.util.spec_from_file_location("__main__", server_path)
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = "__main__"
    sys.modules["__main__"] = mod
    spec.loader.exec_module(mod)


def _import_server_module(srv: str):
    """Import servers/<srv>/server.py under a NON-__main__ name so its mcp.run() footer does
    not fire, leaving the fully-wired module-level `mcp` object available to mount."""
    server_path = REPO_ROOT / "servers" / srv / "server.py"
    if not server_path.exists():
        raise FileNotFoundError(f"{server_path} does not exist")
    mod_name = "hubmod_" + srv.replace("-", "_")
    spec = importlib.util.spec_from_file_location(mod_name, server_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_proxy(name: str, cfg: dict):
    """Build a proxy FastMCP for a ready-made stdio server from its resolved spec."""
    config = {"mcpServers": {name: cfg}}
    try:
        from fastmcp.server import create_proxy
        return create_proxy(config)
    except Exception:  # noqa: BLE001 — fall back to the (deprecated) classmethod
        from fastmcp import FastMCP
        return FastMCP.as_proxy(config)


def hub_main() -> None:
    if len(sys.argv) < 2:
        print("Usage: run-mcp-hub <hub-name>", file=sys.stderr)
        sys.exit(1)

    from mcp_base import make_server  # noqa: E402 — also triggers .env autoload

    from .hubs import HUBS, hub_servers, ready_spec

    name = sys.argv[1]
    if name not in HUBS:
        print(f"run-mcp-hub: unknown hub '{name}' (have: {', '.join(HUBS)})", file=sys.stderr)
        sys.exit(1)
    spec = hub_servers(name)

    hub = make_server(
        f"{name}-hub",
        instructions=(f"Aggregated '{name}' hub: tools from several servers are exposed under "
                      f"<server>_<tool> names (e.g. notes_search). Call the namespaced tool you want."),
    )

    mounted, skipped = [], []
    # Custom servers: import in-process, mount their `mcp`.
    for srv in spec.get("custom", []):
        try:
            mod = _import_server_module(srv)
            sub = getattr(mod, "mcp", None)
            if sub is None:
                raise AttributeError("module has no module-level `mcp`")
            hub.mount(sub, namespace=srv.replace("-", "_"))
            mounted.append(srv)
        except Exception as e:  # noqa: BLE001 — never let one server kill the hub
            skipped.append(srv)
            print(f"hub {name}: skipped custom {srv}: {e}", file=sys.stderr)

    # Ready-made servers: proxy + mount.
    for rname in spec.get("ready", []):
        cfg = ready_spec(rname)
        if not cfg:
            skipped.append(rname)
            print(f"hub {name}: no spec for ready server {rname}", file=sys.stderr)
            continue
        try:
            proxy = _make_proxy(rname, cfg)
            hub.mount(proxy, namespace=rname.replace("-", "_"))
            mounted.append(rname)
        except Exception as e:  # noqa: BLE001
            skipped.append(rname)
            print(f"hub {name}: skipped ready {rname}: {e}", file=sys.stderr)

    print(f"hub {name}: mounted {len(mounted)} servers"
          + (f", skipped {len(skipped)}: {', '.join(skipped)}" if skipped else ""),
          file=sys.stderr)
    hub.run()
