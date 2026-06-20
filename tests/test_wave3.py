"""Offline smoke tests for the outreach pipeline (contacts, campaign, pitchbuilder, email-finder
pattern logic, funding-radar parsing, reachout templating + tracking). Gmail send & live network
are excluded — they need your credentials."""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from fastmcp import Client  # noqa: E402


def load(name):
    sys.path.insert(0, str(ROOT / "servers" / name))
    mod = __import__("server")
    del sys.modules["server"]  # allow re-import of next server.py
    return mod


async def main():
    # --- campaign: dedup gate ---
    sys.path.insert(0, str(ROOT / "servers" / "campaign"))
    import server as campaign  # noqa
    async with Client(campaign.mcp) as c:
        await c.call_tool("record_outreach", {"company": "Acme AI", "domain": "acme.ai"})
        r = await c.call_tool("filter_uncontacted", {"companies": [
            {"company": "Acme AI", "domain": "acme.ai"},      # already contacted -> skip
            {"company": "Nova Robotics", "domain": "nova.io"}, # fresh
        ]})
        assert r.data["fresh_count"] == 1 and r.data["fresh"][0]["company"] == "Nova Robotics"
        await c.call_tool("add_suppression", {"value": "spammy.co", "reason": "test"})
        r2 = await c.call_tool("is_contacted", {"company": "Acme AI", "domain": "acme.ai"})
        assert r2.data["contacted"] is True
        print("campaign OK — dedup + suppression + ledger")
    del sys.modules["server"]

    # --- contacts ---
    sys.path.insert(0, str(ROOT / "servers" / "contacts"))
    import server as contacts  # noqa
    async with Client(contacts.mcp) as c:
        r = await c.call_tool("add_contact", {"name": "Jane Doe", "company": "Nova Robotics",
                                              "email": "jane@nova.io", "role": "CTO"})
        await c.call_tool("update_status", {"contact_id": r.data["id"], "status": "queued"})
        f = await c.call_tool("find", {"query": "Jane"})
        assert f.data and f.data[0]["status"] == "queued"
        print("contacts OK — add/find/status")
    del sys.modules["server"]

    # --- pitchbuilder: store + render ---
    sys.path.insert(0, str(ROOT / "servers" / "pitchbuilder"))
    import server as pitch  # noqa
    async with Client(pitch.mcp) as c:
        await c.call_tool("save_pitch", {"company": "Nova Robotics",
                                         "pitch": "A sim-to-real eval dashboard for your fleet."})
        r = await c.call_tool("render_into_template", {
            "template": "Subject: Idea for {{company}}\nHi {{name}}, {{pitch}}",
            "context": {"company": "Nova Robotics", "name": "Jane", "pitch": "an eval dashboard"}})
        assert r.data["subject"] == "Idea for Nova Robotics" and "eval dashboard" in r.data["body"]
        print("pitchbuilder OK — save + render (subject split)")
    del sys.modules["server"]

    # --- email-finder: pattern generation (offline) ---
    sys.path.insert(0, str(ROOT / "servers" / "email-finder"))
    import server as ef  # noqa
    async with Client(ef.mcp) as c:
        r = await c.call_tool("guess", {"name": "Jane Doe", "domain": "nova.io"})
        cands = r.data["candidates"]
        assert "jane.doe@nova.io" in cands and "jane@nova.io" in cands
        print("email-finder OK — patterns:", cands[:4])
    del sys.modules["server"]

    # --- funding-radar: headline parsing (offline) ---
    sys.path.insert(0, str(ROOT / "servers" / "funding-radar"))
    import server as fr  # noqa
    p = fr.parse_headline("Nova Robotics raises $58M Series B to scale warehouse automation")
    assert p["amount"].startswith("$58") and "Series B" in p["round"] and "Nova Robotics" in p["company"]
    print("funding-radar OK — parsed:", p)
    del sys.modules["server"]

    # --- reachout: templates + tracking (no Gmail) ---
    sys.path.insert(0, str(ROOT / "servers" / "reachout"))
    import server as ro  # noqa
    async with Client(ro.mcp) as c:
        t = await c.call_tool("list_templates", {})
        names = [x["name"] for x in t.data]
        assert "internship_cto" in names
        r = await c.call_tool("render_template", {"template_name": "internship_cto",
              "variables": {"name": "Jane", "company": "Nova Robotics", "round": "Series B",
                            "pitch": "an eval dashboard"}})
        assert "Nova Robotics" in r.data["subject"] and "eval dashboard" in r.data["body"]
        await c.call_tool("log_outreach", {"recipient_name": "Jane", "recipient_email": "jane@nova.io",
                                           "company": "Nova Robotics", "role": "CTO"})
        lo = await c.call_tool("list_outreach", {})
        assert lo.data and lo.data[0]["company"] == "Nova Robotics"
        a = await c.call_tool("auth_status", {})
        print("reachout OK — templates", names, "| gmail creds present:", a.data["credentials_present"])

    print("\nWAVE 3 (offline) OK ✅  (Gmail send + live scan/verify need your credentials)")


asyncio.run(main())
