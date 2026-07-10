"""Live smoke test: funding-radar → apollo → email-finder → emailcheck pipeline.
Uses repo .env when present. Run:
  uv run python tests/smoke_outreach_pipeline.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Smoke must not block on hybrid manual-login wait
os.environ.setdefault("APOLLO_MANUAL_WAIT_SECONDS", "0")
os.environ.setdefault("APOLLO_AUTO_LOGIN", "0")

from fastmcp import Client  # noqa: E402


def load_server(name: str):
    path = ROOT / "servers" / name / "server.py"
    spec = importlib.util.spec_from_file_location(f"smoke_{name.replace('-', '_')}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.mcp


async def call(c, tool: str, args: dict) -> dict:
    return (await c.call_tool(tool, args)).data


async def main() -> int:
    issues: list[str] = []
    print("=== Live smoke: outreach pipeline ===\n")

    # 1) funding-radar
    async with Client(load_server("funding-radar")) as c:
        leads = await call(c, "list_leads", {"limit": 3})
        if isinstance(leads, list):
            print(f"funding-radar: {len(leads)} leads")
        elif isinstance(leads, dict) and leads.get("error"):
            issues.append(f"funding-radar.list_leads: {leads['error']}")
        else:
            issues.append(f"funding-radar.list_leads bad shape: {type(leads)}")

    # 2) apollo
    test_domain = "stripe.com"
    async with Client(load_server("apollo")) as c:
        hk = await call(c, "has_key", {})
        hb = await call(c, "has_browser", {})
        cs = await call(c, "check_session", {})
        print(f"apollo: mode={hk.get('mode')}, playwright={hb.get('playwright_installed')}, "
              f"logged_in={cs.get('logged_in')}")

        fp = await call(c, "find_people", {"domain": test_domain, "limit": 2})
        if fp.get("people"):
            src = fp.get("source", "?")
            print(f"apollo.find_people({test_domain}): {fp['count']} people via {src}")
            person = fp["people"][0]
            person_name = person.get("name") or ""
        elif fp.get("error") in ("apollo not logged in", "playwright is not installed", "apollo not ready") or (
                fp.get("error") == "no APOLLO_API_KEY" or not hk.get("configured")):
            person_name = "Patrick Collison"  # fallback for email-finder step
            print(f"apollo.find_people: expected skip ({fp.get('error')}) — continuing email-finder")
        elif fp.get("error"):
            issues.append(f"apollo.find_people: {fp.get('error')} — {fp.get('hint', '')}")
            person_name = "Patrick Collison"
            print(f"apollo.find_people: no people ({fp.get('error')}) — continuing email-finder")
        else:
            issues.append(f"apollo.find_people unexpected: {fp}")
            person_name = "Patrick Collison"

    # 3) email-finder
    async with Client(load_server("email-finder")) as c:
        ef = await call(c, "find", {
            "name": person_name,
            "company": "Stripe",
            "domain": test_domain,
            "scrape": True,
        })
        if ef.get("error"):
            issues.append(f"email-finder.find: {ef['error']}")
        elif "candidates" not in ef and "best" not in ef:
            issues.append(f"email-finder.find bad shape: {list(ef.keys())}")
        else:
            best = ef.get("best") or {}
            email = best.get("email") if isinstance(best, dict) else None
            n_cand = len(ef.get("candidates") or [])
            print(f"email-finder.find: {n_cand} candidates, best={email or 'none'}")

            # 4) emailcheck + verify
            if email:
                async with Client(load_server("emailcheck")) as ec:
                    ev = await call(ec, "validate_email", {"email": email})
                    if not ev.get("valid") and ev.get("error"):
                        issues.append(f"emailcheck.validate_email: {ev}")
                    else:
                        print(f"emailcheck.validate_email: valid={ev.get('valid')}")

                vr = await call(c, "verify", {"email": email})
                if "deliverable" not in vr:
                    issues.append(f"email-finder.verify bad shape: {vr}")
                else:
                    print(f"email-finder.verify: deliverable={vr.get('deliverable')}")
            else:
                print("email-finder: no best email (pattern/Hunter candidates may still exist)")

    print()
    if issues:
        print("PIPELINE ISSUES:")
        for i in issues:
            print(f"  - {i}")
        return 1
    print("PIPELINE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
