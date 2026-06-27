"""Offline smoke tests for the upgraded emailcheck verifier (real deliverability + DoH + fingerprint).

Network-free assertions only: tool registration, offline syntax/spam/phishing logic, and that the
DoH/verify/fingerprint tools return structured results without raising when the network is absent."""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("MCP_DATA_DIR", tempfile.mkdtemp(prefix="mcp-emailcheck-test-"))
os.environ.setdefault("MCP_NO_DOTENV", "1")

_SUITE = Path(os.environ["MCP_DATA_DIR"])
for _name in ("emailcheck", "emailverify"):
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


async def _emailcheck(c, server):
    tools = {t.name for t in await c.list_tools()}
    for t in ("validate_email", "extract_emails", "spam_score", "phishing_score",
              "check_mx", "domain_report", "verify_deliverability", "bulk_verify",
              "provider_fingerprint", "health", "selftest"):
        assert t in tools, f"emailcheck missing tool {t}"

    # offline validity
    v = await c.call_tool("validate_email", {"email": "Test@Example.com"})
    assert v.data["valid"] is True, v.data
    bad = await c.call_tool("validate_email", {"email": "nope"})
    assert bad.data["valid"] is False, bad.data

    # verify_deliverability: bad syntax is deterministic offline → undeliverable
    vd = await c.call_tool("verify_deliverability", {"email": "not-an-email", "smtp": False})
    assert vd.data["deliverable"] is False, vd.data

    # bulk_verify roll-up shape, no raise
    bv = await c.call_tool("bulk_verify", {"emails": ["bad1", "x@y"], "smtp": False})
    assert bv.data["count"] == 2 and {"deliverable", "undeliverable", "unknown"} <= set(bv.data), bv.data

    # spam + phishing heuristics are offline and deterministic
    sp = await c.call_tool("spam_score", {"subject": "FREE!!! ACT NOW", "body": "winner click here"})
    assert sp.data["score"] > 0, sp.data
    ph = await c.call_tool("phishing_score", {"text_or_url": "http://paypa1-secure.tk/login"})
    assert ph.data["risk_score"] > 0, ph.data

    # check_mx / provider_fingerprint return structured results (DoH-backed; may be empty offline)
    mx = await c.call_tool("check_mx", {"domain": "gmail.com"})
    assert "method" in mx.data and "has_mx" in mx.data, mx.data
    fp = await c.call_tool("provider_fingerprint", {"domain": "gmail.com"})
    assert "domain" in fp.data, fp.data

    # health + selftest never raise
    h = await c.call_tool("health", {})
    assert "checks" in h.data, h.data
    stf = await c.call_tool("selftest", {"live": False})
    assert "components" in stf.data, stf.data

    print(f"emailcheck OK — {len(tools)} tools; offline verify/bulk/spam/phish/fingerprint")


def test_emailcheck_server():
    asyncio.run(one("emailcheck", _emailcheck))


if __name__ == "__main__":
    test_emailcheck_server()
    print("\nALL emailcheck tests passed")
