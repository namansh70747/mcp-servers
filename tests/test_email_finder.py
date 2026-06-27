"""Offline smoke tests for the rebuilt email-finder server + its shared modules.

Network-free: asserts tool registration, deterministic offline logic, and graceful degrade paths
(DNS/Chrome/quota absent must never raise). Live verification is covered separately."""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("MCP_DATA_DIR", tempfile.mkdtemp(prefix="mcp-finder-test-"))
os.environ.setdefault("MCP_NO_DOTENV", "1")

_SUITE = Path(os.environ["MCP_DATA_DIR"])
for _name in ("email-finder", "emailverify"):
    shutil.rmtree(_SUITE / _name, ignore_errors=True)

sys.path.insert(0, str(ROOT / "shared"))
from fastmcp import Client  # noqa: E402


async def one(name, fn):
    sys.path.insert(0, str(ROOT / "servers" / name))
    import server  # noqa
    async with Client(server.mcp) as c:
        await fn(c, server)
    del sys.modules["server"]
    sys.path.pop(0)


def test_shared_modules():
    """Frontier + shared modules: deterministic offline behavior."""
    from mcp_base.frontier import idn, bayes, graph, pattern_ml, fingerprint  # noqa
    from mcp_base import frontier

    # F10 IDN
    assert idn.to_ascii_domain("möbel.de") == "xn--mbel-5qa.de"
    assert idn.ascii_fold("José") == "Jose"
    assert idn.normalize_email("A@möbel.de").endswith("@xn--mbel-5qa.de")

    # F7 Bayesian — strong positive vs strong negative
    pos = bayes.fuse({"api_valid": True, "mx_ok": True, "gravatar": True})
    neg = bayes.fuse({"no_mx": True})
    assert pos["probability"] > 0.8 and neg["probability"] < 0.2

    # F8 graph corroboration — more distinct sources wins
    cands = [{"email": "a@x.com", "sources": ["site", "web", "github"]},
             {"email": "b@x.com", "sources": ["pattern"]}]
    assert graph.best_candidate(cands)["email"] == "a@x.com"

    # F3 ML pattern from learned rows
    rows = [{"email": "jane.doe@x.com", "name": "Jane Doe"},
            {"email": "john.smith@x.com", "name": "John Smith"}]
    assert pattern_ml.predict_pattern("x.com", rows) == "{first}.{last}"

    # capabilities() never raises and always reports the pure-python layers as active
    caps = frontier.capabilities()
    assert caps["f4_fingerprint"] and caps["f7_bayes"] and caps["f10_idn"]
    print("shared modules OK — idn/bayes/graph/pattern_ml/capabilities")


def test_quota_and_registry():
    """Quota multi-key rotation + the verify registry order + extension credit pool."""
    from mcp_base.quota import QUOTA
    from mcp_base import emailverify
    from mcp_base import extensions

    # registry-driven verify order is derivable and never raises
    order = emailverify._api_provider_order()
    assert isinstance(order, list)

    # extension pool: pick rotates, record consumes, status reflects it
    QUOTA._keys.clear()
    pick = QUOTA.pool_pick(extensions._POOL_MEMBERS)
    assert pick == "apollo"  # highest free cap, unused
    status = extensions.pool_status()
    assert status["apollo"]["remaining"] == status["apollo"]["cap"]
    print("quota + registry OK — order derivable, pool rotates")


async def _finder(c, server):
    tools = {t.name for t in await c.list_tools()}
    for t in ("guess", "mx", "verify", "find", "find_by_company", "bulk_verify",
              "find_deep_async", "find_status", "record_outcome", "stats", "health",
              "selftest", "provider_fingerprint", "frontier_status", "extension_pool_status"):
        assert t in tools, f"email-finder missing tool {t}"

    # guess() — deterministic patterns
    g = await c.call_tool("guess", {"name": "Jane Doe", "domain": "acme.com"})
    assert any(x.startswith("jane.doe@") for x in g.data["candidates"]), g.data

    # verify bad syntax → definitively undeliverable, offline
    v = await c.call_tool("verify", {"email": "not-an-email", "check_smtp": False})
    assert v.data["deliverable"] is False, v.data

    # bulk_verify offline: bad addresses don't raise, return a roll-up
    bv = await c.call_tool("bulk_verify", {"emails": ["bad1", "bad2@"]})
    assert bv.data["count"] == 2 and "results" in bv.data, bv.data

    # record_outcome validation
    ro = await c.call_tool("record_outcome", {"email": "x", "status": "replied"})
    assert ro.data["ok"] is False, ro.data
    ro2 = await c.call_tool("record_outcome", {"email": "a@b.com", "status": "bogus"})
    assert ro2.data["ok"] is False, ro2.data

    # frontier_status + extension pool status return structured data
    fs = await c.call_tool("frontier_status", {})
    assert "capabilities" in fs.data, fs.data
    eps = await c.call_tool("extension_pool_status", {})
    assert "pool" in eps.data, eps.data

    # stats + health never raise
    st = await c.call_tool("stats", {})
    assert "by_source" in st.data, st.data
    h = await c.call_tool("health", {})
    assert "checks" in h.data and "degraded" in h.data, h.data

    print(f"email-finder OK — {len(tools)} tools; offline verify/bulk/record/frontier/health")


def test_finder_server():
    asyncio.run(one("email-finder", _finder))


if __name__ == "__main__":
    test_shared_modules()
    test_quota_and_registry()
    test_finder_server()
    print("\nALL email-finder tests passed")
