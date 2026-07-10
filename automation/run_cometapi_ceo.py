"""Option B pipeline: ensure_ready -> find_ceo_email(cometapi.com).

Uses Apollo's own "Access email" button to get the REAL email Apollo has on file
(spends at most 1 credit). Falls back to email-finder's pattern guess ONLY if Apollo
explicitly has no email on record — and clearly labels that as a guess, not Apollo data.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "servers" / name / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    apollo = load("apollo")
    domain = "cometapi.com"

    print(f"=== Option B: find_ceo_email({domain}) ===")
    result = apollo.find_ceo_email(domain, company="CometAPI")
    print(json.dumps(result, indent=2))

    if result.get("error"):
        return 1

    candidate = result.get("candidate") or {}
    name = candidate.get("name")

    if result.get("email"):
        print(f"\nCEO: {name} ({candidate.get('title')})")
        print(f"Email (from Apollo's own database — Access Email): {result['email']}")
        return 0

    print(f"\nApollo has NO verified email on file for {name!r} (Access Email returned 'No email').")
    if not name:
        return 3

    ef = load("email-finder")
    print(f"\n=== email-finder.find({name!r}, {domain}) [pattern guess — NOT Apollo-verified] ===")
    found = ef.find(name=name, company="CometAPI", domain=domain, scrape=True)
    print(json.dumps(found, indent=2))
    if found.get("best"):
        print(f"\nGuessed email (pattern-based, not Apollo-verified): {found['best']}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
