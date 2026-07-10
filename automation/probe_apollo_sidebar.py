"""Probe Apollo extension side panel with shadow DOM + live poll."""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "servers" / "apollo"))
sys.path.insert(0, str(ROOT / "shared"))

from playwright.sync_api import sync_playwright  # noqa: E402
from mcp_base.browser_cdp import cdp_url  # noqa: E402

EXT_ID = "alhgpfoeiimagjlnfekdhkjlkiomcapa"
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

DEEP_SCAN_JS = """() => {
  const hits = [];
  function walk(root, depth) {
    if (depth > 12) return;
    const nodes = root.querySelectorAll('*');
    for (const el of nodes) {
      const t = (el.innerText || el.textContent || '').trim().slice(0, 200);
      const label = (el.getAttribute('aria-label') || el.title || '').trim();
      const blob = (t + ' ' + label).toLowerCase();
      if (t && /apollo|contact information|access email|emails?|phone/.test(blob)) {
        const r = el.getBoundingClientRect();
        hits.push({text: t.slice(0, 100), tag: el.tagName, label, w: Math.round(r.width), h: Math.round(r.height)});
      }
      if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
    }
  }
  walk(document, 0);
  return hits.slice(0, 30);
}"""


def deep_scan(page) -> dict:
    out = {"url": page.url, "body_len": 0, "hits": [], "emails": []}
    try:
        body = page.inner_text("body")
        out["body_len"] = len(body)
        out["body_preview"] = body[:600]
        out["emails"] = EMAIL_RE.findall(body)[:5]
    except Exception as ex:
        out["body_error"] = str(ex)
    try:
        out["hits"] = page.evaluate(DEEP_SCAN_JS)
    except Exception as ex:
        out["hits_error"] = str(ex)
    out["has_apollo_ui"] = bool(out.get("hits")) or any(
        x in (out.get("body_preview") or "").lower()
        for x in ("apollo.io", "contact information", "access email")
    )
    return out


def main():
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url())
        ctx = browser.contexts[0]
        li_page = None
        ext_page = None
        for page in ctx.pages:
            url = page.url or ""
            if EXT_ID in url and "side-panel" in url:
                ext_page = page
            elif "linkedin.com/in/willahmed" in url:
                li_page = page

        if not li_page:
            for page in ctx.pages:
                if "linkedin.com/in/" in (page.url or ""):
                    li_page = page
                    break

        print("LinkedIn tab:", (li_page.url if li_page else "NONE")[:80])
        print("Extension side panel:", (ext_page.url if ext_page else "NONE")[:80])
        print()
        print(">>> If sidebar is closed: click Apollo FAB on the LinkedIn profile NOW <<<")
        print(">>> Polling extension side panel for 60 seconds... <<<")
        print()

        best = None
        for i in range(30):
            scans = {}
            if ext_page:
                scans["extension"] = deep_scan(ext_page)
            if li_page:
                scans["linkedin"] = deep_scan(li_page)

            ext_ok = scans.get("extension", {}).get("has_apollo_ui")
            li_ok = scans.get("linkedin", {}).get("has_apollo_ui")
            ext_hits = len(scans.get("extension", {}).get("hits") or [])
            ext_body = scans.get("extension", {}).get("body_len", 0)

            print(f"[{i*2:2d}s] ext_body={ext_body} ext_hits={ext_hits} ext_ui={ext_ok} li_ui={li_ok}")

            if ext_ok or (ext_hits > 0 and ext_body > 50):
                best = scans
                print("\nDETECTED Apollo UI in extension side panel!")
                break
            time.sleep(2)

        if not best:
            best = {"extension": deep_scan(ext_page) if ext_page else None,
                    "linkedin": deep_scan(li_page) if li_page else None}
            print("\nFinal scan:")

        print(json.dumps(best, indent=2, default=str))
        out_path = ROOT / "automation" / "probe_apollo_sidebar_result.json"
        out_path.write_text(json.dumps(best, indent=2, default=str), encoding="utf-8")
        print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
