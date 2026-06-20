"""FastMCP app factory with suite-standard defaults."""
from __future__ import annotations

from fastmcp import FastMCP


def make_server(name: str, instructions: str | None = None) -> FastMCP:
    """Create a FastMCP server preconfigured for the suite.

    Adds a standard `health` tool so every server is independently verifiable
    (call it from any client to confirm the process is up).
    """
    mcp = FastMCP(name, instructions=instructions)

    @mcp.tool
    def health() -> dict:
        """Liveness check: confirm this server is running and reachable."""
        return {"ok": True, "server": name}

    return mcp
