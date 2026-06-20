"""OFFLINE tests for the P2/P3 outreach-pipeline additions across:
funding-radar, pitchbuilder, email-finder, mailmerge.

Covers the additive cross-server wiring:
  - funding-radar.as_targets now carries round/amount/amount_usd/sector (plus company/domain).
  - pitchbuilder.suggest_angles optionally prefills from the funding-radar DB (read-only); the
    old no-company / unknown-company behavior is unchanged.
  - email-finder.persist_to_contacts resolves an email AND writes it to the shared contacts DB
    (read/write), returning the contact id.
  - mailmerge.dedupe(use_ledger=True) subtracts already-contacted emails read from the campaign
    ledger DB (read-only) without a manually-passed list; the old list param still works.

Network / Gmail / DNS are never required: email resolution uses a disposable-domain address that
short-circuits verify() before any MX lookup, and the cross-server DBs are seeded directly in an
isolated MCP_DATA_DIR so no real ~/.mcp-suite data is touched.

Run:
    VIRTUAL_ENV= .venv/bin/python tests/test_outreach-pipeline.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Isolate data + skip dotenv BEFORE importing any server.
_TMP = tempfile.mkdtemp(prefix="outreach-pipeline-test-")
os.environ["MCP_DATA_DIR"] = _TMP
os.environ["MCP_NO_DOTENV"] = "1"
# Ensure no real API keys leak in from the host environment.
for _k in ("HUNTER_API_KEY", "REOON_API_KEY", "TOMBA_API_KEY", "TOMBA_SECRET",
           "GITHUB_PERSONAL_ACCESS_TOKEN", "SEC_USER_AGENT"):
    os.environ.pop(_k, None)
for _name in ("funding-radar", "pitchbuilder", "email-finder", "mailmerge",
              "contacts", "campaign"):
    shutil.rmtree(Path(_TMP) / _name, ignore_errors=True)

from fastmcp import Client  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def one(name, fn, *, setup=None):
    sys.path.insert(0, str(ROOT / "servers" / name))
    import server  # noqa
    if setup:
        setup(server)
    async with Client(server.mcp) as c:
        await fn(c, server)
    del sys.modules["server"]
    sys.path.pop(0)


async def tool_names(c):
    return {t.name for t in await c.list_tools()}


def _data(*parts) -> Path:
    return Path(_TMP, *parts)


def _seed_funding_radar() -> None:
    """Create the funding-radar DB the way the server does and insert one lead."""
    db = _data("funding-radar", "store.db")
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS leads("
        "id INTEGER PRIMARY KEY, company TEXT, amount TEXT, round TEXT, sector TEXT, "
        "source TEXT, url TEXT, headline TEXT, found_at TEXT, amount_usd REAL, domain TEXT, "
        "published_at TEXT, UNIQUE(company, source));"
    )
    conn.execute(
        "INSERT INTO leads(company,amount,round,sector,source,url,headline,found_at,"
        "amount_usd,domain,published_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("Acme AI", "$12M", "Series A", "ai", "techcrunch",
         "https://example.com/acme", "Acme AI raises $12M Series A", _now(),
         12_000_000.0, "acme.ai", _now()),
    )
    conn.commit()
    conn.close()


def _seed_campaign_ledger(emails: list[str]) -> None:
    """Create the campaign DB the way the server does and log some sent emails."""
    db = _data("campaign", "store.db")
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS outreach_log("
        "id INTEGER PRIMARY KEY, company TEXT, domain TEXT, email TEXT, contact_name TEXT, "
        "sent_at TEXT, run_id INTEGER);"
    )
    for e in emails:
        conn.execute(
            "INSERT INTO outreach_log(company,domain,email,sent_at) VALUES(?,?,?,?)",
            ("Old Co", "oldco.com", e, _now()))
    conn.commit()
    conn.close()


async def main():
    # ---------------------------------------------------------------- funding-radar
    async def fr(c, mod):
        names = await tool_names(c)
        assert {"scan", "as_targets", "enrich_targets", "list_leads"} <= names, names
        # http was swapped in (no top-level httpx import dependency for these paths).
        assert hasattr(mod, "http"), "mcp_base.http should be imported"
        # Seed a lead directly through the server's store, then check as_targets shape.
        mod._store_lead("Beta Labs", "$3M", "Seed", "fintech", "rss",
                        "https://example.com/beta", "Beta Labs raises $3M Seed",
                        amount_usd=3_000_000.0, domain="beta.io", published_at=_now())
        res = await c.call_tool("as_targets", {"limit": 10})
        targets = res.data
        assert isinstance(targets, list) and targets, targets
        t = next(x for x in targets if x["company"] == "Beta Labs")
        # Existing keys preserved AND new keys present.
        for k in ("company", "domain", "round", "amount", "amount_usd", "sector"):
            assert k in t, (k, t)
        assert t["round"] == "Seed" and t["sector"] == "fintech", t
        assert t["amount"] == "$3M" and t["amount_usd"] == 3_000_000.0, t
        assert t["domain"] == "beta.io", t
        print("PASS funding-radar.as_targets carries round/amount/sector")

    await one("funding-radar", fr)

    # ---------------------------------------------------------------- pitchbuilder
    _seed_funding_radar()

    async def pb(c, mod):
        names = await tool_names(c)
        assert "suggest_angles" in names, names

        # (a) No company -> old behavior: angles with raw {placeholders}, no funding block.
        res0 = await c.call_tool("suggest_angles", {})
        d0 = res0.data
        assert "funding" not in d0, d0
        assert d0["angles"] and all("starter" in a for a in d0["angles"]), d0
        assert all("prefilled_starter" not in a for a in d0["angles"]), d0

        # (b) Unknown company -> still old behavior (no matching lead).
        res1 = await c.call_tool("suggest_angles", {"company": "Nonexistent Q Corp"})
        assert "funding" not in res1.data, res1.data

        # (c) Known company -> funding block + prefilled starters from the seeded lead.
        res2 = await c.call_tool("suggest_angles", {"company": "Acme AI"})
        d2 = res2.data
        assert "funding" in d2, d2
        assert d2["funding"]["round"] == "Series A", d2["funding"]
        assert d2["funding"]["sector"] == "ai", d2["funding"]
        assert d2["funding"]["amount"] == "$12M", d2["funding"]
        assert d2["funding"]["domain"] == "acme.ai", d2["funding"]
        rf = next(a for a in d2["angles"] if a["angle"] == "recent_funding")
        assert "prefilled_starter" in rf, rf
        # {round} should be substituted; {pain} (unknown) should remain literal.
        assert "Series A" in rf["prefilled_starter"], rf["prefilled_starter"]
        assert "{pain}" in rf["prefilled_starter"], rf["prefilled_starter"]
        print("PASS pitchbuilder.suggest_angles prefills from funding-radar (old call unchanged)")

    await one("pitchbuilder", pb)

    # ---------------------------------------------------------------- email-finder
    async def ef(c, mod):
        names = await tool_names(c)
        assert "persist_to_contacts" in names, names
        assert hasattr(mod, "http"), "mcp_base.http should be imported"

        # Use a disposable-domain email: verify() short-circuits before any MX/DNS lookup.
        res = await c.call_tool("persist_to_contacts", {
            "name": "Jane Doe", "company": "Acme AI", "domain": "acme.ai",
            "email": "jane@mailinator.com", "role": "CTO",
        })
        d = res.data
        assert d["contact_id"] is not None, d
        assert d["email"] == "jane@mailinator.com", d
        assert d["source"] == "provided", d
        cid = d["contact_id"]

        # Verify it actually landed in the shared contacts DB (read/write).
        cdb = _data("contacts", "store.db")
        assert cdb.exists(), "contacts DB should have been created"
        conn = sqlite3.connect(cdb)
        row = conn.execute(
            "SELECT id, name, company, email, role, domain, source FROM contacts WHERE id=?",
            (cid,)).fetchone()
        conn.close()
        assert row is not None, "contact row missing"
        assert row[1] == "Jane Doe" and row[2] == "Acme AI", row
        assert row[3] == "jane@mailinator.com", row
        assert row[4] == "CTO", row
        assert row[5] == "acme.ai", row  # explicit domain param wins over email domain
        assert row[6].startswith("email-finder:"), row

        # Idempotent upsert on (email, company): same id, no duplicate.
        res2 = await c.call_tool("persist_to_contacts", {
            "name": "Jane D.", "company": "Acme AI",
            "email": "jane@mailinator.com",
        })
        assert res2.data["contact_id"] == cid, (res2.data, cid)
        conn = sqlite3.connect(cdb)
        n = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        conn.close()
        assert n == 1, f"expected 1 contact, got {n}"
        print("PASS email-finder.persist_to_contacts resolves + writes to contacts DB")

    await one("email-finder", ef)

    # ---------------------------------------------------------------- mailmerge
    _seed_campaign_ledger(["already@oldco.com", "Seen@OLDCO.com"])

    async def mm(c, mod):
        names = await tool_names(c)
        assert "dedupe" in names, names

        recips = [
            {"to_email": "already@oldco.com", "name": "A"},   # in ledger
            {"to_email": "seen@oldco.com", "name": "B"},      # in ledger (case-insensitive)
            {"to_email": "fresh@new.com", "name": "C"},       # new
            {"to_email": "fresh@new.com", "name": "C2"},      # in-batch duplicate
        ]

        # (a) Old behavior preserved: no ledger read unless asked.
        d_old = (await c.call_tool("dedupe", {"recipients": recips})).data
        # both oldco kept (no ledger read) + one fresh; the in-batch duplicate fresh removed.
        assert d_old["kept_count"] == 3, d_old
        assert d_old["removed_already_contacted"] == [], d_old
        assert len(d_old["removed_duplicates"]) == 1, d_old

        # (b) against_emails param still works.
        d_list = (await c.call_tool("dedupe", {
            "recipients": recips, "against_emails": ["already@oldco.com"]})).data
        assert "already@oldco.com" in d_list["removed_already_contacted"], d_list

        # (c) use_ledger=True subtracts already-contacted from the campaign ledger DB, no list.
        d_led = (await c.call_tool("dedupe", {"recipients": recips, "use_ledger": True})).data
        blocked = set(d_led["removed_already_contacted"])
        assert {"already@oldco.com", "seen@oldco.com"} <= blocked, d_led
        kept_emails = {r["to_email"] for r in d_led["kept"]}
        assert kept_emails == {"fresh@new.com"}, d_led
        assert d_led["kept_count"] == 1, d_led
        print("PASS mailmerge.dedupe(use_ledger=True) reads campaign ledger (list param intact)")

    await one("mailmerge", mm)

    print("\nALL outreach-pipeline tests passed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
