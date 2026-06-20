"""devlog → standup — turn your git activity into daily/weekly summaries and standup notes.
Reads `git log` (no DB needed). Point it at any repo path.

Adds per-author metrics, activity calendars, commit streaks, file churn, multi-repo rollups,
and a paste-ready weekly markdown export."""
from __future__ import annotations

import subprocess
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from mcp_base import make_server

mcp = make_server(
    "devlog",
    instructions=("Summarize git activity: daily_log, weekly_summary, standup(repo), authors, "
                  "activity, streak, file_churn, multi_summary, export_markdown, contributors."),
)


def _git(repo: str, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                              timeout=30).stdout.strip()
    except Exception as e:  # noqa: BLE001
        return f"__error__: {e}"


def _is_repo(repo: str) -> bool:
    return _git(repo, "rev-parse", "--is-inside-work-tree") == "true"


def _log(repo: str, since: str, author: str = "") -> list[dict]:
    fmt = "%h%x1f%an%x1f%ad%x1f%s"
    args = ["log", f"--since={since}", f"--pretty={fmt}", "--date=short"]
    if author:
        args.append(f"--author={author}")
    out = _git(repo, *args)
    if out.startswith("__error__"):
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            rows.append({"hash": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3]})
    return rows


def _parse_shortstat(text: str) -> dict:
    """Sum 'N files changed, N insertions(+), N deletions(-)' lines."""
    files = ins = dele = 0
    for line in text.splitlines():
        if "changed" not in line:
            continue
        import re
        f = re.search(r"(\d+) files? changed", line)
        i = re.search(r"(\d+) insertion", line)
        d = re.search(r"(\d+) deletion", line)
        files += int(f.group(1)) if f else 0
        ins += int(i.group(1)) if i else 0
        dele += int(d.group(1)) if d else 0
    return {"files_changed": files, "insertions": ins, "deletions": dele}


def _numstat(repo: str, since: str, author: str = "") -> list[dict]:
    """Per-commit numstat records: author, date, and (added, deleted, file) tuples."""
    fmt = "%x1e%H%x1f%an%x1f%ad"
    args = ["log", f"--since={since}", "--numstat", f"--pretty={fmt}", "--date=short"]
    if author:
        args.append(f"--author={author}")
    out = _git(repo, *args)
    if out.startswith("__error__"):
        return []
    records = []
    for block in out.split("\x1e"):
        block = block.strip()
        if not block:
            continue
        lines = block.splitlines()
        head = lines[0].split("\x1f")
        if len(head) < 3:
            continue
        _, an, ad = head[0], head[1], head[2]
        files = []
        for ln in lines[1:]:
            cols = ln.split("\t")
            if len(cols) == 3:
                add = int(cols[0]) if cols[0].isdigit() else 0
                rem = int(cols[1]) if cols[1].isdigit() else 0
                files.append((add, rem, cols[2]))
        records.append({"author": an, "date": ad, "files": files})
    return records


@mcp.tool
def daily_log(repo: str, days: int = 1, author: str = "") -> dict:
    """Commits in the last N days (optionally filtered by author)."""
    commits = _log(repo, f"{days}.days.ago", author)
    return {"repo": repo, "days": days, "count": len(commits), "commits": commits}


@mcp.tool
def weekly_summary(repo: str, author: str = "") -> dict:
    """Last 7 days of commits, plus parsed files/insertions/deletions — raw material for a weekly update."""
    commits = _log(repo, "7.days.ago", author)
    stat = _git(repo, "log", "--since=7.days.ago", "--shortstat", "--pretty=oneline")
    diff = _parse_shortstat(stat) if not stat.startswith("__error__") else {}
    by_author = dict(Counter(c["author"] for c in commits)) if not author else {}
    return {"repo": repo, "count": len(commits), "commits": commits, "diffstat": diff,
            "by_author": by_author,
            "note": "Summarize these into a 'what I shipped this week' update."}


@mcp.tool
def standup(repo: str, author: str = "") -> dict:
    """Standup helper: yesterday's commits ('did'), uncommitted changes ('in progress')."""
    did = _log(repo, "1.days.ago", author)
    status = _git(repo, "status", "--short")
    return {"repo": repo, "did": did, "in_progress": [l for l in status.splitlines() if l],
            "note": "Format as: Yesterday / Today / Blockers."}


@mcp.tool
def authors(repo: str, since: str = "90.days.ago") -> dict:
    """Per-author leaderboard: commits + lines added/removed over a window."""
    if not _is_repo(repo):
        return {"error": f"not a git repo: {repo}"}
    agg: dict[str, dict] = defaultdict(lambda: {"commits": 0, "added": 0, "removed": 0})
    for rec in _numstat(repo, since):
        a = agg[rec["author"]]
        a["commits"] += 1
        for add, rem, _ in rec["files"]:
            a["added"] += add
            a["removed"] += rem
    board = [{"author": k, **v} for k, v in agg.items()]
    board.sort(key=lambda x: -x["commits"])
    return {"repo": repo, "since": since, "authors": board}


@mcp.tool
def activity(repo: str, days: int = 30) -> dict:
    """Per-day commit counts over a window (a simple contribution calendar)."""
    if not _is_repo(repo):
        return {"error": f"not a git repo: {repo}"}
    commits = _log(repo, f"{days}.days.ago")
    by_day = Counter(c["date"] for c in commits)
    cal = {(date.today() - timedelta(days=i)).isoformat(): 0 for i in range(days)}
    for d, n in by_day.items():
        if d in cal:
            cal[d] = n
    ordered = dict(sorted(cal.items()))
    active = sum(1 for v in ordered.values() if v)
    return {"repo": repo, "days": days, "total_commits": sum(ordered.values()),
            "active_days": active, "calendar": ordered}


@mcp.tool
def streak(repo: str, author: str = "") -> dict:
    """Current and longest consecutive-day commit streaks (last year of history)."""
    if not _is_repo(repo):
        return {"error": f"not a git repo: {repo}"}
    commits = _log(repo, "365.days.ago", author)
    dates = sorted({datetime.strptime(c["date"], "%Y-%m-%d").date() for c in commits})
    if not dates:
        return {"repo": repo, "current_streak": 0, "longest_streak": 0}
    longest = cur = 1
    for prev, nxt in zip(dates, dates[1:]):
        if (nxt - prev).days == 1:
            cur += 1
            longest = max(longest, cur)
        elif nxt != prev:
            cur = 1
    today = date.today()
    current = 0
    if dates[-1] in (today, today - timedelta(days=1)):
        current = 1
        for prev, nxt in zip(reversed(dates), list(reversed(dates))[1:]):
            if (prev - nxt).days == 1:
                current += 1
            else:
                break
    return {"repo": repo, "current_streak": current, "longest_streak": longest,
            "active_days": len(dates)}


@mcp.tool
def file_churn(repo: str, since: str = "30.days.ago", top: int = 20) -> dict:
    """Most-changed files (hotspots) over a window, by total lines added+removed."""
    if not _is_repo(repo):
        return {"error": f"not a git repo: {repo}"}
    churn: dict[str, dict] = defaultdict(lambda: {"changes": 0, "added": 0, "removed": 0, "commits": 0})
    for rec in _numstat(repo, since):
        seen = set()
        for add, rem, path in rec["files"]:
            c = churn[path]
            c["added"] += add
            c["removed"] += rem
            c["changes"] += add + rem
            if path not in seen:
                c["commits"] += 1
                seen.add(path)
    ranked = sorted(({"file": k, **v} for k, v in churn.items()),
                    key=lambda x: -x["changes"])[:top]
    return {"repo": repo, "since": since, "hotspots": ranked}


@mcp.tool
def contributors(repo: str) -> list[dict]:
    """All-time contributors with commit counts and first/last commit dates."""
    if not _is_repo(repo):
        return [{"error": f"not a git repo: {repo}"}]
    out = _git(repo, "log", "--pretty=%an%x1f%ad", "--date=short")
    if out.startswith("__error__"):
        return []
    agg: dict[str, dict] = {}
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 2:
            continue
        an, ad = parts
        rec = agg.setdefault(an, {"author": an, "commits": 0, "first": ad, "last": ad})
        rec["commits"] += 1
        rec["first"] = min(rec["first"], ad)
        rec["last"] = max(rec["last"], ad)
    return sorted(agg.values(), key=lambda x: -x["commits"])


@mcp.tool
def multi_summary(repos: list[str], days: int = 7, author: str = "") -> dict:
    """Roll up commit activity across several repos for the last N days."""
    per_repo, total = [], 0
    for repo in repos:
        commits = _log(repo, f"{days}.days.ago", author)
        total += len(commits)
        per_repo.append({"repo": repo, "count": len(commits),
                         "subjects": [c["subject"] for c in commits][:30]})
    per_repo.sort(key=lambda x: -x["count"])
    return {"days": days, "total_commits": total, "repos": per_repo}


@mcp.tool
def export_markdown(repo: str, days: int = 7, author: str = "") -> dict:
    """Paste-ready markdown weekly update: commits grouped by day."""
    commits = _log(repo, f"{days}.days.ago", author)
    by_day: dict[str, list[str]] = defaultdict(list)
    for c in commits:
        by_day[c["date"]].append(c["subject"])
    lines = [f"# Dev update — last {days} days", "",
             f"_{len(commits)} commits in `{repo}`_", ""]
    for day in sorted(by_day, reverse=True):
        lines.append(f"### {day}")
        lines += [f"- {s}" for s in by_day[day]]
        lines.append("")
    return {"repo": repo, "count": len(commits), "markdown": "\n".join(lines)}


if __name__ == "__main__":
    mcp.run()
