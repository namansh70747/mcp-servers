"""readme-changelog — generate CHANGELOG / release notes from git history, and a README skeleton
from repo structure. Provides structured data; the agent writes the prose.

Adds semver bump suggestions, conventional-commit linting, Keep-a-Changelog formatting,
contributors, and shields.io badge generation."""
from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from datetime import date
from pathlib import Path

from mcp_base import make_server

mcp = make_server(
    "readme-changelog",
    instructions=("gen_changelog/release_notes from git; gen_readme from structure; "
                  "suggest_bump, lint_commits, keep_a_changelog, contributors, badges."),
)

PREFIXES = {"feat": "Features", "fix": "Fixes", "docs": "Docs", "refactor": "Refactors",
            "perf": "Performance", "test": "Tests", "chore": "Chores", "build": "Build", "ci": "CI"}
# Keep-a-Changelog section mapping
KAC = {"feat": "Added", "fix": "Fixed", "perf": "Changed", "refactor": "Changed",
       "docs": "Changed", "build": "Changed", "ci": "Changed", "chore": "Changed",
       "revert": "Removed", "deprecate": "Deprecated", "security": "Security"}
CC_TYPES = set(PREFIXES) | {"revert", "style", "security", "deprecate"}
CC_RE = re.compile(r"^(?P<type>[a-z]+)(?P<scope>\([^)]+\))?(?P<bang>!)?:\s+.+")


def _git(repo: str, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                              timeout=30).stdout.strip()
    except Exception as e:  # noqa: BLE001
        return f"__error__: {e}"


def _commits(repo: str, rng: str) -> list[str]:
    out = _git(repo, "log", rng, "--pretty=%s") if rng else _git(repo, "log", "--pretty=%s")
    return [] if out.startswith("__error__") else [l for l in out.splitlines() if l]


def _bodies(repo: str, rng: str) -> list[str]:
    """Full commit messages (for detecting 'BREAKING CHANGE:' in the body)."""
    out = _git(repo, "log", rng, "--pretty=%B%x1e") if rng else _git(repo, "log", "--pretty=%B%x1e")
    if out.startswith("__error__"):
        return []
    return [b.strip() for b in out.split("\x1e") if b.strip()]


def _categorize(subjects: list[str]) -> dict:
    groups: dict[str, list[str]] = defaultdict(list)
    for s in subjects:
        head = s.split(":", 1)[0].split("(", 1)[0].replace("!", "").strip().lower()
        groups[PREFIXES.get(head, "Other")].append(s)
    return dict(groups)


def _latest_tag(repo: str) -> str:
    t = _git(repo, "describe", "--tags", "--abbrev=0")
    return "" if t.startswith("__error__") or not t else t


def _detect_breaking(repo: str, rng: str) -> list[str]:
    breaking = []
    for b in _bodies(repo, rng):
        first = b.splitlines()[0]
        m = CC_RE.match(first)
        if (m and m.group("bang")) or "BREAKING CHANGE" in b:
            breaking.append(first)
    return breaking


@mcp.tool
def gen_changelog(repo: str, from_ref: str = "", to_ref: str = "HEAD") -> dict:
    """Categorized changelog of commits between two refs (conventional-commit aware).

    Returns grouped sections plus detected breaking changes and a suggested semver bump."""
    rng = f"{from_ref}..{to_ref}" if from_ref else ""
    subjects = _commits(repo, rng)
    breaking = _detect_breaking(repo, rng)
    bump = _bump_from(subjects, breaking)
    return {"repo": repo, "range": rng or "all", "count": len(subjects),
            "grouped": _categorize(subjects), "breaking": breaking, "suggested_bump": bump}


@mcp.tool
def release_notes(repo: str, from_ref: str, to_ref: str = "HEAD") -> dict:
    """Markdown-ready release notes between two refs."""
    rng = f"{from_ref}..{to_ref}"
    grouped = _categorize(_commits(repo, rng))
    breaking = _detect_breaking(repo, rng)
    lines = [f"## {to_ref}", ""]
    if breaking:
        lines.append("### ⚠ BREAKING CHANGES")
        lines += [f"- {b}" for b in breaking]
        lines.append("")
    for section, items in grouped.items():
        lines.append(f"### {section}")
        lines += [f"- {i}" for i in items]
        lines.append("")
    return {"repo": repo, "markdown": "\n".join(lines), "breaking": breaking}


def _bump_from(subjects: list[str], breaking: list[str]) -> str:
    if breaking:
        return "major"
    bump = "patch"
    for s in subjects:
        m = CC_RE.match(s)
        if not m:
            continue
        if m.group("bang"):
            return "major"
        if m.group("type") == "feat":
            bump = "minor"
    return bump


def _next_version(tag: str, bump: str) -> str:
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", tag or "")
    if not m:
        return {"major": "1.0.0", "minor": "0.1.0", "patch": "0.0.1"}[bump]
    major, minor, patch = (int(x) for x in m.groups())
    if bump == "major":
        major, minor, patch = major + 1, 0, 0
    elif bump == "minor":
        minor, patch = minor + 1, 0
    else:
        patch += 1
    prefix = "v" if (tag or "").startswith("v") else ""
    return f"{prefix}{major}.{minor}.{patch}"


@mcp.tool
def suggest_bump(repo: str, from_ref: str = "", to_ref: str = "HEAD") -> dict:
    """Suggest a semver bump from conventional commits since `from_ref` (defaults to latest tag)."""
    if not from_ref:
        from_ref = _latest_tag(repo)
    rng = f"{from_ref}..{to_ref}" if from_ref else ""
    subjects = _commits(repo, rng)
    breaking = _detect_breaking(repo, rng)
    bump = _bump_from(subjects, breaking)
    current = _latest_tag(repo)
    rationale = ("breaking changes present" if breaking else
                 "new features (feat)" if bump == "minor" else "fixes/other changes only")
    return {"repo": repo, "range": rng or "all", "current_tag": current or None,
            "suggested_bump": bump, "next_version": _next_version(current, bump),
            "rationale": rationale, "commits_considered": len(subjects)}


@mcp.tool
def lint_commits(repo: str, from_ref: str = "", to_ref: str = "HEAD") -> dict:
    """Flag commits that don't follow the Conventional Commits spec."""
    rng = f"{from_ref}..{to_ref}" if from_ref else ""
    subjects = _commits(repo, rng)
    offenders, valid = [], 0
    for s in subjects:
        if s.startswith("Merge ") or s.startswith("Revert "):
            valid += 1
            continue
        m = CC_RE.match(s)
        if m and m.group("type") in CC_TYPES:
            valid += 1
        else:
            reason = ("missing 'type: ' prefix" if not m else
                      f"unknown type '{m.group('type')}'")
            offenders.append({"subject": s, "reason": reason})
    total = len(subjects)
    return {"repo": repo, "total": total, "valid": valid, "invalid": len(offenders),
            "pass": not offenders, "pass_rate_pct": round(valid / total * 100, 1) if total else 100.0,
            "offenders": offenders}


@mcp.tool
def keep_a_changelog(repo: str, version: str = "", from_ref: str = "", to_ref: str = "HEAD") -> dict:
    """Emit a Keep-a-Changelog 1.1.0 section (Added/Changed/Fixed/Deprecated/Removed/Security)."""
    rng = f"{from_ref}..{to_ref}" if from_ref else ""
    subjects = _commits(repo, rng)
    breaking = _detect_breaking(repo, rng)
    if not version:
        version = _next_version(_latest_tag(repo), _bump_from(subjects, breaking))
    sections: dict[str, list[str]] = defaultdict(list)
    for s in subjects:
        head = s.split(":", 1)[0].split("(", 1)[0].replace("!", "").strip().lower()
        msg = s.split(":", 1)[1].strip() if ":" in s else s
        sections[KAC.get(head, "Changed")].append(msg)
    order = ["Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"]
    lines = [f"## [{version.lstrip('v')}] - {date.today().isoformat()}", ""]
    if breaking:
        lines += ["### ⚠ Breaking Changes", *[f"- {b}" for b in breaking], ""]
    for sec in order:
        if sections.get(sec):
            lines.append(f"### {sec}")
            lines += [f"- {m}" for m in sections[sec]]
            lines.append("")
    return {"repo": repo, "version": version, "markdown": "\n".join(lines).rstrip() + "\n"}


@mcp.tool
def contributors(repo: str, from_ref: str = "", to_ref: str = "HEAD") -> list[dict]:
    """Contributors with commit counts (via git shortlog), optionally for a ref range."""
    args = ["shortlog", "-sne", "--all"] if not from_ref else ["shortlog", "-sne", f"{from_ref}..{to_ref}"]
    out = _git(repo, *args)
    if out.startswith("__error__"):
        return []
    rows = []
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(.+?)\s+<(.+?)>", line)
        if m:
            rows.append({"commits": int(m.group(1)), "name": m.group(2), "email": m.group(3)})
    return rows


@mcp.tool
def badges(repo: str, owner: str = "", name: str = "") -> dict:
    """Generate common shields.io markdown badges (license/build/version/PRs). Pure strings, no network."""
    root = Path(repo)
    name = name or (root.name if root.exists() else "project")
    lic = ""
    for cand in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"):
        if (root / cand).exists():
            txt = ""
            try:
                txt = (root / cand).read_text(encoding="utf-8", errors="ignore")[:200].lower()
            except OSError:
                pass
            if "mit" in txt:
                lic = "MIT"
            elif "apache" in txt:
                lic = "Apache--2.0"
            elif "gnu general public" in txt or "gpl" in txt:
                lic = "GPL--3.0"
            else:
                lic = "License"
            break
    slug = f"{owner}/{name}" if owner else name
    badges = []
    if lic:
        badges.append(f"![License](https://img.shields.io/badge/license-{lic}-blue)")
    if owner:
        badges.append(f"![CI](https://img.shields.io/github/actions/workflow/status/{slug}/ci.yml)")
        badges.append(f"![Issues](https://img.shields.io/github/issues/{slug})")
        badges.append(f"![Stars](https://img.shields.io/github/stars/{slug})")
    tag = _latest_tag(repo)
    if tag:
        badges.append(f"![Version](https://img.shields.io/badge/version-{tag.lstrip('v')}-green)")
    return {"repo": repo, "license": lic or None, "markdown": " ".join(badges), "badges": badges}


@mcp.tool
def gen_readme(repo: str) -> dict:
    """Scan a repo's structure + detect stack to seed a README (returns data + a draft outline)."""
    root = Path(repo)
    if not root.exists():
        return {"error": f"no such repo: {repo}"}
    markers = {
        "package.json": "Node/JS", "pyproject.toml": "Python", "requirements.txt": "Python",
        "go.mod": "Go", "Cargo.toml": "Rust", "pom.xml": "Java", "Gemfile": "Ruby",
    }
    stack = sorted(set(v for f, v in markers.items() if (root / f).exists()))
    features = {
        "Makefile": "make", "Dockerfile": "docker", ".pre-commit-config.yaml": "pre-commit",
        ".github/workflows": "GitHub Actions CI", "pytest.ini": "pytest",
        "tests": "tests", ".devcontainer": "devcontainer",
    }
    detected = [label for f, label in features.items() if (root / f).exists()]
    top = sorted(p.name + ("/" if p.is_dir() else "") for p in root.iterdir()
                 if not p.name.startswith("."))[:40]
    cmds = []
    if "Python" in stack:
        cmds += ["pip install -e .  # or: uv sync", "pytest"]
    if "Node/JS" in stack:
        cmds += ["npm install", "npm test"]
    if "Go" in stack:
        cmds += ["go build ./...", "go test ./..."]
    if "Rust" in stack:
        cmds += ["cargo build", "cargo test"]
    outline = [f"# {root.name}", "", "> One-line description.", "",
               f"**Stack:** {', '.join(stack) or 'n/a'}",
               f"**Tooling:** {', '.join(detected) or 'n/a'}", "",
               "## Structure", *(f"- `{t}`" for t in top[:20]),
               "", "## Setup", *(f"```bash\n{c}\n```" for c in cmds[:1]),
               "## Usage", "## License"]
    return {"repo": repo, "stack": stack, "tooling": detected, "top_level": top,
            "suggested_commands": cmds, "draft_outline": "\n".join(outline)}


if __name__ == "__main__":
    mcp.run()
