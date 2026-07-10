"""End-to-end check: Apollo extension Access email flow."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "servers" / "apollo"))
sys.path.insert(0, str(ROOT / "shared"))

spec = importlib.util.spec_from_file_location("apollo_server", ROOT / "servers" / "apollo" / "server.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

from mcp_base.apollo_extension import find_extension_sidebar_frame  # noqa: E402
from mcp_base.browser_cdp import with_cdp_page  # noqa: E402


def check_sidebar(ctx) -> dict:
    frame = find_extension_sidebar_frame(ctx)
    if not frame:
        return {"sidebar_open": False, "hint": "Click Apollo FAB on LinkedIn tab"}
    try:
        text = frame.inner_text("body")[:500]
    except Exception as ex:
        text = f"read error: {ex}"
    return {
        "sidebar_open": True,
        "preview": text[:200],
        "has_access_email": "access email" in text.lower(),
        "has_contact_info": "contact information" in text.lower(),
    }


def main():
    print("=== Step 1: CDP + sidebar check ===")
    sidebar_report = {}

    def probe(page, backend):
        nonlocal sidebar_report
        sidebar_report = check_sidebar(page.context)
        sidebar_report["linkedin_tab"] = page.url[:80]
        sidebar_report["backend"] = backend
        return sidebar_report

    with_cdp_page(probe, reuse_tab=True, url_hint="linkedin.com", prefer_fresh_tab=False, close_on_done=False)
    print(json.dumps(sidebar_report, indent=2))

    if not sidebar_report.get("sidebar_open"):
        print("\nBLOCKED: Open apollo-browser, go to LinkedIn, click Apollo FAB once, then re-run.")
        return 1

    print("\n=== Step 2: Extension email flow (Will Ahmed) ===")
    result = mod._find_person_email_flow_sync(
        "Will Ahmed",
        company="WHOOP",
        linkedin_url="https://www.linkedin.com/in/willahmed/",
    )
  # redact full email in stdout if present - show structure only
    safe = dict(result)
    if safe.get("email"):
        em = safe["email"]
        safe["email"] = em[:2] + "***@" + em.split("@")[-1] if "@" in em else "***"
    print(json.dumps(safe, indent=2, default=str))
    print("\n=== Result ===")
    print("path:", result.get("path"))
    print("email_source:", result.get("email_source"))
    print("ok:", bool(result.get("email")))
    if result.get("extension_error"):
        print("extension_error:", result.get("extension_error"))
    if result.get("error"):
        print("error:", result.get("error"))
    return 0 if result.get("email") else 2


if __name__ == "__main__":
    raise SystemExit(main())
