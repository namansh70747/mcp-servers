#!/usr/bin/env python3
"""
mcp-to-hermes: Seamlessly integrate your mcp-servers suite into Hermes Agent.
Installs all 65 MCP servers into Hermes's mcp_servers config with proper formatting,
essential env var handling, and optional selective server activation.

Usage:
    python mcp-to-hermes.py [--install] [--servers SERVER1,SERVER2,...] [--preview]

Options:
    --install       Merge mcp_servers into ~/.hermes/config.yaml (default: preview only)
    --servers       Comma-separated list of servers to enable (default: all)
    --preview       Show what would be installed without modifying config
    --check         Verify all server binaries exist and are executable
    --minimal       Install only ready-made + dev + system servers (no outreach/career)
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

MCP_ROOT = Path("/Users/namansharma/mcp-servers")
HERMES_CONFIG = Path.home() / ".hermes" / "config.yaml"


def get_all_servers():
    """Get full server catalog from .mcp.json."""
    mcp_json = MCP_ROOT / ".mcp.json"
    with open(mcp_json) as f:
        data = json.load(f)
    return data.get("mcpServers", {})


def categorize_servers(servers):
    """Categorize servers by function."""
    cats = {
        "Ready-made (Community)": [
            "filesystem", "git", "github", "google_workspace", "memory", "playwright"
        ],
        "macOS Ready-made": [
            "applescript", "shortcuts", "messages", "apple-events", "apple-notes",
            "screenshot", "spotlight"
        ],
        "Dev Tools": [
            "codeindex", "project-memory", "codeedit", "gitflow", "recipes",
            "scaffold", "devlog", "readme-changelog", "repo-health", "api-tester"
        ],
        "Career": [
            "resume-forge", "deckforge", "portfolio-site", "github-profile",
            "linkedin-optimizer", "blog-drafter", "interview-prep"
        ],
        "Outreach": [
            "funding-radar", "apollo", "email-finder", "emailcheck", "contacts",
            "pitchbuilder", "campaign", "reachout", "mailbox", "mailmerge"
        ],
        "Productivity": [
            "task-manager", "notes", "time-tracker", "bookmark-vault", "habit-tracker",
            "expense-tracker", "learn-tracker", "snippet-vault", "daily-digest",
            "meeting-prep", "news-radar", "rss-reader"
        ],
        "System": [
            "mac-control", "homebrew", "chrome", "browser", "background", "deskpilot"
        ],
        "Media": [
            "voice", "videoforge", "webengine", "webscrape", "spotify"
        ],
        "Communication": [
            "whatsapp", "hub"
        ],
        "Other": ["jobtrack"]
    }

    result = {}
    all_listed = set()
    for cat, names in cats.items():
        present = [n for n in names if n in servers]
        if present:
            result[cat] = present
            all_listed.update(present)

    # Catch any uncategorized
    uncategorized = [n for n in servers if n not in all_listed]
    if uncategorized:
        result["Uncategorized"] = uncategorized

    return result


def server_to_hermes_yaml(name, config):
    """Convert a single server config to Hermes YAML block."""
    lines = [
        "  %s:" % name,
        '    command: "%s"' % config["command"],
    ]
    if config.get("args"):
        lines.append("    args:")
        for arg in config["args"]:
            lines.append('      - "%s"' % arg)
    if config.get("env"):
        lines.append("    env:")
        for k, v in config["env"].items():
            lines.append('      %s: "%s"' % (k, v))
    return "\n".join(lines)


def merge_into_hermes_config(yaml_block, dry_run=True):
    """Merge the mcp_servers block into Hermes config.yaml."""
    if not HERMES_CONFIG.exists():
        new_config = "# Hermes Configuration\n\n" + yaml_block + "\n"
        if not dry_run:
            HERMES_CONFIG.parent.mkdir(parents=True, exist_ok=True)
            with open(HERMES_CONFIG, "w") as f:
                f.write(new_config)
        return new_config

    with open(HERMES_CONFIG) as f:
        content = f.read()

    # Remove existing mcp_servers block
    if "mcp_servers:" in content:
        lines = content.split("\n")
        new_lines = []
        in_mcp = False
        mcp_indent = None

        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("mcp_servers:"):
                in_mcp = True
                mcp_indent = len(line) - len(stripped)
                continue
            if in_mcp:
                if not stripped:
                    continue
                current_indent = len(line) - len(stripped)
                if current_indent <= mcp_indent and stripped and not stripped.startswith("#"):
                    in_mcp = False
                    new_lines.append(line)
                continue
            new_lines.append(line)

        content = "\n".join(new_lines)

    new_content = content.rstrip() + "\n\n" + yaml_block + "\n"

    if not dry_run:
        backup = Path(str(HERMES_CONFIG) + ".bak")
        backup.write_text(Path(HERMES_CONFIG).read_text())
        HERMES_CONFIG.write_text(new_content)

    return new_content


def check_binaries(servers):
    """Check which server binaries/commands are available."""
    results = {"ok": [], "missing": [], "env_needed": []}

    for name, config in servers.items():
        command = config.get("command", "")
        env = config.get("env", {})

        if command.startswith("/"):
            if Path(command).exists():
                results["ok"].append(name)
            else:
                results["missing"].append((name, "Binary not found: %s" % command))
        else:
            try:
                subprocess.run(["which", command], check=True,
                              capture_output=True, timeout=5)
                results["ok"].append(name)
            except (subprocess.CalledProcessError, FileNotFoundError):
                results["missing"].append((name, "Command not in PATH: %s" % command))

        if env:
            for k, v in env.items():
                if v.startswith("${") and v.endswith("}"):
                    var_name = v[2:-1]
                    if not os.getenv(var_name):
                        results["env_needed"].append((name, k, var_name))

    return results


# Lite profile: hub router + high-utility daily-driver servers (~80 tools instead of 700+).
# Everything else remains reachable via hub.run('server','tool',{...}) and hub.search_tools().
LITE_PROFILE = [
    "hub",          # router to all 65+ servers — 5 tools: list_servers/search_tools/list_tools/help/run
    "email-finder", # core outreach tool
    "emailcheck",
    "whatsapp",
    "voice",
    "browser",
    "contacts",
    "webscrape",
    "task-manager",
    "mailbox",
    "reachout",
    "notes",
    "devlog",
    "codeindex",
]


def main():
    parser = argparse.ArgumentParser(description="Integrate mcp-servers into Hermes")
    parser.add_argument("--install", action="store_true", help="Actually modify Hermes config")
    parser.add_argument("--servers", type=str, help="Comma-separated server names to include")
    parser.add_argument("--preview", action="store_true", help="Show what would be installed")
    parser.add_argument("--check", action="store_true", dest="check_binaries_flag", help="Check binary availability")
    parser.add_argument("--minimal", action="store_true", help="Install essential subset only")
    parser.add_argument("--profile", choices=["lite", "full"], default="full",
                        help="lite: hub + ~14 daily-driver servers (~80 tools, fast thinking); "
                             "full: all 65+ servers (~700 tools, maximum breadth). Default: full")
    args = parser.parse_args()

    all_servers = get_all_servers()
    print("📦 Total MCP servers available: %d" % len(all_servers))

    if args.servers:
        selected = [s.strip() for s in args.servers.split(",")]
    elif args.profile == "lite":
        selected = LITE_PROFILE
        print("🚀 Lite profile: hub router + %d daily-driver servers (~80 tools)" % len(LITE_PROFILE))
        print("   Everything else reachable via: hub.run('server', 'tool', {...})")
    elif args.minimal:
        selected = [
            "filesystem", "git", "github", "memory", "playwright",
            "codeindex", "project-memory", "codeedit", "gitflow", "recipes",
            "mac-control", "homebrew", "browser", "chrome",
            "task-manager", "notes", "daily-digest", "news-radar"
        ]
    else:
        selected = list(all_servers.keys())

    selected_servers = {k: all_servers[k] for k in selected if k in all_servers}

    if args.check_binaries_flag:
        print("\n🔍 Checking server binaries...")
        results = check_binaries(selected_servers)
        print("\n✅ Available (%d): %s" % (len(results["ok"]), ", ".join(results["ok"])))
        if results["missing"]:
            print("\n❌ Missing (%d):" % len(results["missing"]))
            for name, reason in results["missing"]:
                print("   %s: %s" % (name, reason))
        if results["env_needed"]:
            print("\n⚠️  Environment variables needed (%d):" % len(results["env_needed"]))
            for name, var, env_name in results["env_needed"]:
                print("   %s: %s → set %s" % (name, var, env_name))
        return

    yaml_lines = ["mcp_servers:"]

    categorized = categorize_servers(selected_servers)
    for cat, names in categorized.items():
        if not names:
            continue
        yaml_lines.append("\n  # ─── %s ───" % cat)
        for name in names:
            if name in selected_servers:
                yaml_lines.append(server_to_hermes_yaml(name, selected_servers[name]))

    yaml_output = "\n".join(yaml_lines)

    if args.preview or not args.install:
        print("\n" + "=" * 70)
        print("PREVIEW — Hermes MCP Configuration")
        print("=" * 70)
        print(yaml_output)
        print("\n" + "=" * 70)
        print("Run with --install to merge this into %s" % HERMES_CONFIG)
        print("Selected: %d/%d servers" % (len(selected_servers), len(all_servers)))
    else:
        new_config = merge_into_hermes_config(yaml_output, dry_run=False)
        print("✅ Merged %d MCP servers into %s" % (len(selected_servers), HERMES_CONFIG))
        print("🔄 Restart Hermes to load all MCP tools")


if __name__ == "__main__":
    main()
