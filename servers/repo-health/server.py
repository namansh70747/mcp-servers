"""repo-health — a read-only repository hygiene reporter. Surfaces stale branches, large files,
missing project files (README/LICENSE/CI/.gitignore), TODO/FIXME census, commit cadence, and
an overall health score. Pure `git` + filesystem; no deps, no network, no writes.

Complements devlog (activity) and readme-changelog (release docs)."""
from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from mcp_base import make_server

mcp = make_server(
    "repo-health",
    instructions=("Read-only repo hygiene: health_report, stale_branches, large_files, "
                  "missing_files, todo_census, commit_cadence, gitignore_check."),
)


def _git(repo: str, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                              timeout=30).stdout.strip()
    except Exception as e:  # noqa: BLE001
        return f"__error__: {e}"


def _is_repo(repo: str) -> bool:
    return _git(repo, "rev-parse", "--is-inside-work-tree") == "true"


_EXPECTED = {
    "README": ("README.md", "README.rst", "README.txt", "README"),
    "LICENSE": ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"),
    "gitignore": (".gitignore",),
    "CI": (".github/workflows", ".gitlab-ci.yml", ".circleci", "azure-pipelines.yml"),
    "editorconfig": (".editorconfig",),
    "contributing": ("CONTRIBUTING.md", "CONTRIBUTING"),
    "code_of_conduct": ("CODE_OF_CONDUCT.md",),
}


def _missing_files(repo: str) -> dict:
    root = Path(repo).expanduser()
    if not root.exists():
        return {"error": f"no such path: {repo}"}
    present, missing = {}, []
    for label, cands in _EXPECTED.items():
        found = next((c for c in cands if (root / c).exists()), None)
        if found:
            present[label] = found
        else:
            missing.append(label)
    return {"repo": str(root), "present": present, "missing": missing}


def _stale_branches(repo: str, days: int = 90) -> dict:
    if not _is_repo(repo):
        return {"error": f"not a git repo: {repo}"}
    out = _git(repo, "for-each-ref", "--sort=committerdate", "refs/heads/",
               "--format=%(refname:short)%x1f%(committerdate:unix)")
    if out.startswith("__error__"):
        return {"error": out}
    now = datetime.now(timezone.utc).timestamp()
    stale, fresh = [], 0
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 2 or not parts[1].isdigit():
            continue
        name, ts = parts[0], int(parts[1])
        age = int((now - ts) / 86400)
        if age > days:
            stale.append({"branch": name, "age_days": age})
        else:
            fresh += 1
    return {"repo": repo, "threshold_days": days, "stale": stale, "fresh_count": fresh}


def _large_files(repo: str, min_kb: int = 500, top: int = 20, tracked_only: bool = True) -> dict:
    root = Path(repo).expanduser()
    if not root.exists():
        return {"error": f"no such path: {repo}"}
    files: list[Path] = []
    if tracked_only and _is_repo(repo):
        out = _git(repo, "ls-files")
        if not out.startswith("__error__"):
            files = [root / f for f in out.splitlines() if f]
    if not files:
        files = [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts]
    sized = []
    for p in files:
        try:
            kb = p.stat().st_size / 1024
        except OSError:
            continue
        if kb >= min_kb:
            try:
                rel = str(p.relative_to(root))
            except ValueError:
                rel = str(p)
            sized.append({"file": rel, "kb": round(kb, 1)})
    sized.sort(key=lambda x: -x["kb"])
    return {"repo": str(root), "min_kb": min_kb, "count": len(sized), "files": sized[:top]}


_TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX|BUG)\b[:\s]", re.IGNORECASE)
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "target", "__pycache__"}
_TEXT_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".c", ".h",
             ".cpp", ".cs", ".php", ".swift", ".sh", ".sql", ".md", ".html", ".css", ".yaml",
             ".yml", ".toml"}


def _todo_census(repo: str, max_files: int = 2000, limit: int = 200) -> dict:
    root = Path(repo).expanduser()
    if not root.exists():
        return {"error": f"no such path: {repo}"}
    by_kind: dict[str, int] = {}
    hits, scanned = [], 0
    for p in root.rglob("*"):
        if scanned >= max_files:
            break
        if not p.is_file() or p.suffix.lower() not in _TEXT_EXT:
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        scanned += 1
        try:
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                m = _TODO_RE.search(line)
                if m:
                    kind = m.group(1).upper()
                    by_kind[kind] = by_kind.get(kind, 0) + 1
                    if len(hits) < limit:
                        hits.append({"file": str(p.relative_to(root)), "line": i,
                                     "kind": kind, "text": line.strip()[:160]})
        except OSError:
            continue
    return {"repo": str(root), "files_scanned": scanned, "total": sum(by_kind.values()),
            "by_kind": by_kind, "hits": hits}


def _commit_cadence(repo: str, days: int = 90) -> dict:
    if not _is_repo(repo):
        return {"error": f"not a git repo: {repo}"}
    out = _git(repo, "log", f"--since={days}.days.ago", "--pretty=%ad", "--date=short")
    dates = [] if out.startswith("__error__") else [d for d in out.splitlines() if d]
    active = len(set(dates))
    last = _git(repo, "log", "-1", "--pretty=%ad", "--date=short")
    last_age = None
    if last and not last.startswith("__error__"):
        try:
            last_age = (datetime.now(timezone.utc).date()
                        - datetime.strptime(last, "%Y-%m-%d").date()).days
        except ValueError:
            pass
    weeks = max(days / 7, 1)
    return {"repo": repo, "window_days": days, "total_commits": len(dates),
            "active_days": active, "commits_per_week": round(len(dates) / weeks, 2),
            "last_commit": last if not last.startswith("__error__") else None,
            "last_commit_age_days": last_age}


def _gitignore_check(repo: str) -> dict:
    root = Path(repo).expanduser()
    gi = root / ".gitignore"
    leak_patterns = [".env", "node_modules", ".venv", "venv", "__pycache__", "*.pyc",
                     ".DS_Store", "dist", "build"]
    present = gi.read_text(encoding="utf-8", errors="ignore") if gi.exists() else ""
    missing_patterns = [p for p in leak_patterns if p not in present]
    tracked_leaks = []
    if _is_repo(repo):
        out = _git(repo, "ls-files")
        if not out.startswith("__error__"):
            tracked = out.splitlines()
            for needle in (".env", "node_modules/", ".venv/", "__pycache__/", ".DS_Store"):
                if any(needle in t for t in tracked):
                    tracked_leaks.append(needle)
    return {"repo": str(root), "has_gitignore": gi.exists(),
            "suggested_additions": missing_patterns, "tracked_leaks": tracked_leaks}


@mcp.tool
def missing_files(repo: str) -> dict:
    """Check for presence of common project files (README, LICENSE, .gitignore, CI, etc.)."""
    return _missing_files(repo)


@mcp.tool
def stale_branches(repo: str, days: int = 90) -> dict:
    """List local branches whose last commit is older than N days (candidates for cleanup)."""
    return _stale_branches(repo, days)


@mcp.tool
def large_files(repo: str, min_kb: int = 500, top: int = 20, tracked_only: bool = True) -> dict:
    """Find large files in the working tree (default: only git-tracked files >= min_kb)."""
    return _large_files(repo, min_kb, top, tracked_only)


@mcp.tool
def todo_census(repo: str, max_files: int = 2000, limit: int = 200) -> dict:
    """Count TODO/FIXME/HACK/XXX/BUG markers across source files, with locations."""
    return _todo_census(repo, max_files, limit)


@mcp.tool
def commit_cadence(repo: str, days: int = 90) -> dict:
    """Commit cadence over a window: total commits, active days, commits/week, last commit age."""
    return _commit_cadence(repo, days)


@mcp.tool
def gitignore_check(repo: str) -> dict:
    """Sanity-check .gitignore and flag commonly-leaked paths that are tracked (e.g. .env, venv)."""
    return _gitignore_check(repo)


@mcp.tool
def health_report(repo: str) -> dict:
    """Aggregate hygiene report with a 0-100 health score and prioritized recommendations."""
    root = Path(repo).expanduser()
    if not root.exists():
        return {"error": f"no such path: {repo}"}
    is_repo = _is_repo(repo)
    mf = _missing_files(repo)
    gi = _gitignore_check(repo)
    cad = _commit_cadence(repo, 90) if is_repo else {}
    stale = _stale_branches(repo, 90) if is_repo else {}
    large = _large_files(repo, 1000, 10)
    todos = _todo_census(repo)

    score = 100
    recs = []
    for label in mf.get("missing", []):
        if label in ("README", "LICENSE", "gitignore", "CI"):
            score -= 10
            recs.append(f"Add a {label}")
        else:
            score -= 2
    if gi.get("tracked_leaks"):
        score -= 10
        recs.append(f"Untrack leaked paths: {', '.join(gi['tracked_leaks'])}")
    if stale.get("stale"):
        score -= min(10, len(stale["stale"]) * 2)
        recs.append(f"Clean up {len(stale['stale'])} stale branch(es)")
    if large.get("files"):
        score -= min(10, len(large["files"]) * 2)
        recs.append(f"Review {len(large['files'])} large file(s) (>1MB)")
    if cad and cad.get("last_commit_age_days") and cad["last_commit_age_days"] > 60:
        score -= 5
        recs.append("No commits in 60+ days — project may be stale")
    if todos.get("total", 0) > 50:
        score -= 5
        recs.append(f"{todos['total']} TODO/FIXME markers — consider triaging")
    score = max(0, min(100, score))
    grade = ("A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60
             else "D" if score >= 40 else "F")
    return {
        "repo": str(root), "is_git_repo": is_repo, "score": score, "grade": grade,
        "recommendations": recs or ["Looks healthy!"],
        "missing_files": mf.get("missing", []),
        "stale_branches": len(stale.get("stale", [])),
        "large_files": len(large.get("files", [])),
        "todo_markers": todos.get("total", 0),
        "gitignore_leaks": gi.get("tracked_leaks", []),
        "commit_cadence": {k: cad.get(k) for k in ("total_commits", "commits_per_week",
                                                   "last_commit_age_days")} if cad else None,
    }


if __name__ == "__main__":
    mcp.run()
