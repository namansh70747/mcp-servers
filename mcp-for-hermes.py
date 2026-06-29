#!/usr/bin/env python3
"""
mcp-for-hermes — Quick reference for all 65 MCP servers available in Hermes.
Run this after starting Hermes to verify all tools are loaded.
"""

from pathlib import Path
import json

CONFIG_PATH = Path.home() / ".hermes" / "config.yaml"

def show_status():
    with open(CONFIG_PATH) as f:
        content = f.read()

    if "mcp_servers:" not in content:
        print("❌ MCP servers not yet configured in Hermes")
        print("   Run: python /Users/namansharma/mcp-servers/mcp-to-hermes.py --install")
        return

    # Count servers
    import re
    servers = re.findall(r'^  ([a-z][\w-]+):$', content, re.MULTILINE)
    print(f"✅ {len(servers)} MCP servers configured in ~/.hermes/config.yaml")
    print()
    
    # Categorize and show
    cats = {
        "Ready-made": ["filesystem", "git", "github", "google_workspace", "memory", "playwright"],
        "macOS": ["applescript", "shortcuts", "messages", "apple-events", "apple-notes", "screenshot", "spotlight"],
        "Dev": ["codeindex", "project-memory", "codeedit", "gitflow", "recipes", "scaffold", "devlog", "readme-changelog", "repo-health", "api-tester"],
        "Career": ["resume-forge", "deckforge", "portfolio-site", "github-profile", "linkedin-optimizer", "blog-drafter", "interview-prep"],
        "Outreach": ["funding-radar", "apollo", "email-finder", "emailcheck", "contacts", "pitchbuilder", "campaign", "reachout", "mailbox", "mailmerge"],
        "Productivity": ["task-manager", "notes", "time-tracker", "bookmark-vault", "habit-tracker", "expense-tracker", "learn-tracker", "snippet-vault", "daily-digest", "meeting-prep", "news-radar", "rss-reader"],
        "System": ["mac-control", "homebrew", "chrome", "browser", "background", "deskpilot"],
        "Media": ["voice", "videoforge", "webengine", "webscrape", "spotify"],
        "Comm": ["whatsapp", "hub"],
        "Other": ["jobtrack"],
    }

    for cat, names in cats.items():
        present = [n for n in names if n in servers]
        if present:
            print(f"  {cat}: {', '.join(present)}")

    print()
    print("🔄 Restart Hermes to load all MCP tools (they appear as mcp_{server}_{tool})")
    print("📖 Full docs: hermes mcp list  (after restart)")
    print("🛠️  Manage:   mcp-to-hermes.py --check | --preview | --install")

if __name__ == "__main__":
    show_status()
