"""gitflow — local git commit -> PR bridge MCP server.

A thin, safe wrapper over the local `git` (and optional `gh`) CLI so an agent can drive a
real branch/commit/diff/push/PR workflow. Pure subprocess; no SDK, no network of its own,
no writes outside the target repo. Degrades gracefully when the path is not a git repo,
when there is no remote, or when `gh` is not installed.

Contract tools (names are load-bearing — recipes resolve against them):
    status, current_branch, create_branch, stage, commit, push, diff, log, pr_body, open_pr

Security: every git invocation is a list-arg subprocess (never a shell string), so user
input cannot shell-inject. Branch/ref names are validated against `git check-ref-format`
semantics before use, and explicit `--` separators stop pathspecs being read as options.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from mcp_base import err, make_server, ok

mcp = make_server(
    "gitflow",
    instructions=(
        "Local git -> PR bridge. status/current_branch/create_branch/stage/commit/push/"
        "diff/log on a repo path, then pr_body to synthesize a PR title+markdown from the "
        "diff, then open_pr (uses `gh` if installed, else returns a ready-to-run command "
        "and the github MCP tool to call). All ops are local subprocess git; safe to "
        "call read-only ops (status/diff/log) freely."
    ),
)

_TIMEOUT = 30


# ---------------------------------------------------------------- subprocess core
def _run(repo: str, args: list[str], timeout: int = _TIMEOUT) -> dict:
    """Run `git -C <repo> <args>` with list args (no shell). Returns a structured result."""
    if not shutil.which("git"):
        return {"rc": 127, "out": "", "errout": "git not installed", "missing": True}
    try:
        p = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {"rc": p.returncode, "out": p.stdout.rstrip("\n"), "errout": p.stderr.strip()}
    except subprocess.TimeoutExpired:
        return {"rc": -1, "out": "", "errout": f"git timed out after {timeout}s"}
    except Exception as e:  # noqa: BLE001
        return {"rc": -1, "out": "", "errout": str(e)}


def _tool(repo: str, args: list[str]) -> dict:
    """Run an external (non-git) tool like `gh` with list args (no shell)."""
    exe = args[0]
    if not shutil.which(exe):
        return {"rc": 127, "out": "", "errout": f"{exe} not installed", "missing": True}
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=120, cwd=repo)
        return {"rc": p.returncode, "out": p.stdout.strip(), "errout": p.stderr.strip()}
    except Exception as e:  # noqa: BLE001
        return {"rc": -1, "out": "", "errout": str(e)}


def _resolve_repo(repo: str) -> tuple[str, dict | None]:
    """Expand + validate the repo path. Returns (path, error_or_None)."""
    if not isinstance(repo, str) or not repo.strip():
        return "", err("repo path is required")
    p = Path(repo).expanduser()
    if not p.exists():
        return "", err(f"no such path: {repo}", hint="pass an existing directory")
    return str(p), None


def _is_repo(repo: str) -> bool:
    return _run(repo, ["rev-parse", "--is-inside-work-tree"]).get("out") == "true"


def _not_repo_err(repo: str) -> dict:
    return err(f"not a git repo: {repo}", hint="run create_branch on a `git init`ed dir, "
               "or point repo= at one")


# ---------------------------------------------------------------- name validation
# git refnames forbid: spaces, ~ ^ : ? * [ \, control chars, '..', leading/trailing '/',
# '@{', ending in '.' or '.lock', and the single name '@'.
_BAD_SUBSTR = ("..", "@{", "//", " ", "~", "^", ":", "?", "*", "[", "\\", "\t")


def _valid_refname(name: str) -> bool:
    if not isinstance(name, str) or not name or name == "@":
        return False
    if name.startswith("-") or name.startswith("/") or name.endswith("/"):
        return False
    if name.endswith(".") or name.endswith(".lock"):
        return False
    if any(b in name for b in _BAD_SUBSTR):
        return False
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        return False
    return True


# `from_ref`/`to_ref` may be branches, tags, or SHAs, and may carry ~/^ suffixes — allow
# a slightly broader but still injection-proof shape.
_REF_RE = re.compile(r"^[A-Za-z0-9._/@^~{}-]+$")


def _valid_ref(ref: str) -> bool:
    return isinstance(ref, str) and bool(ref) and not ref.startswith("-") \
        and ".." not in ref and bool(_REF_RE.match(ref))


def _valid_head_ref(head: str) -> bool:
    """Allow fork PR heads like owner:branch in addition to local branch names."""
    if ":" in head:
        owner, branch = head.split(":", 1)
        return bool(owner.strip()) and _valid_refname(branch)
    return _valid_refname(head)


def _parse_remote_url(url: str) -> tuple[str | None, str | None]:
    """Parse owner/repo from a GitHub remote URL."""
    if not url:
        return None, None
    u = url.strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    # git@github.com:owner/repo
    m = re.match(r"^git@[^:]+:([^/]+)/(.+)$", u)
    if m:
        return m.group(1), m.group(2)
    # https://github.com/owner/repo
    m = re.match(r"^https?://[^/]+/([^/]+)/(.+)$", u)
    if m:
        return m.group(1), m.group(2)
    return None, None


def _remote_url(repo: str, remote: str = "origin") -> str | None:
    r = _run(repo, ["remote", "get-url", remote])
    return r.get("out") if r.get("rc") == 0 else None


# ---------------------------------------------------------------- tools
@mcp.tool
def remote_owner_repo(repo: str = ".", remote: str = "origin") -> dict:
    """Parse `remote` URL into {owner, repo}. Used for GitHub MCP fallbacks and fork PRs."""
    repo, e = _resolve_repo(repo)
    if e:
        return e
    if not _is_repo(repo):
        return _not_repo_err(repo)
    url = _remote_url(repo, remote)
    if not url:
        return err(f"no remote '{remote}' or empty URL", hint="git remote add origin <url>")
    owner, name = _parse_remote_url(url)
    if not owner or not name:
        return err(f"could not parse owner/repo from: {url}")
    return ok(owner=owner, repo=name, remote=remote, remote_url=url)


@mcp.tool
def default_branch(repo: str = ".", remote: str = "origin") -> dict:
    """Detect the default branch from the remote (origin/HEAD), falling back to main/master."""
    repo, e = _resolve_repo(repo)
    if e:
        return e
    if not _is_repo(repo):
        return _not_repo_err(repo)
    sym = _run(repo, ["symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD"])
    if sym.get("rc") == 0 and sym.get("out"):
        branch = sym["out"].split("/", 1)[-1]
        return ok(branch=branch, remote=remote, repo=repo)
    for candidate in ("main", "master"):
        if _run(repo, ["rev-parse", "--verify", "--quiet", f"refs/heads/{candidate}"]).get("rc") == 0:
            return ok(branch=candidate, remote=remote, repo=repo, inferred=True)
        if _run(repo, ["rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{candidate}"]).get("rc") == 0:
            return ok(branch=candidate, remote=remote, repo=repo, inferred=True)
    return err("could not determine default branch", hint="pass base= explicitly to open_pr")


@mcp.tool
def add_remote(repo: str = ".", name: str = "", url: str = "") -> dict:
    """Add a git remote (e.g. upstream for OSS contributions)."""
    repo, e = _resolve_repo(repo)
    if e:
        return e
    if not _is_repo(repo):
        return _not_repo_err(repo)
    if not _valid_refname(name):
        return err(f"invalid remote name: {name!r}")
    if not isinstance(url, str) or not url.strip():
        return err("remote url is required")
    r = _run(repo, ["remote", "add", name, url.strip()])
    if r["rc"] != 0:
        return err(r["errout"] or "git remote add failed", rc=r["rc"])
    return ok(repo=repo, remote=name, url=url.strip())


@mcp.tool
def fork_push_instructions(repo: str = ".", fork_owner: str = "", branch: str | None = None) -> dict:
    """Return structured steps to push a branch to your fork and open a PR upstream."""
    repo, e = _resolve_repo(repo)
    if e:
        return e
    if not _is_repo(repo):
        return _not_repo_err(repo)
    if branch is None:
        branch = _run(repo, ["rev-parse", "--abbrev-ref", "HEAD"]).get("out")
    if not branch or branch == "HEAD":
        return err("cannot determine branch — pass branch=")
    upstream = remote_owner_repo(repo, "upstream")
    origin = remote_owner_repo(repo, "origin")
    up = upstream if upstream.get("ok") else remote_owner_repo(repo, "origin")
    if not up.get("ok"):
        return up
    fork = fork_owner or (origin.get("owner") if origin.get("ok") else "")
    if not fork:
        return err("fork_owner required when origin owner unknown")
    base = default_branch(repo)
    base_name = base.get("branch", "main") if base.get("ok") else "main"
    head = f"{fork}:{branch}"
    steps = [
        {"action": "push", "command": f"git push -u origin {branch}",
         "why": "Push your feature branch to your fork (origin)."},
        {"action": "open_pr", "head": head, "base": base_name,
         "why": f"Open PR with head={head} against upstream {up['owner']}/{up['repo']}:{base_name}."},
    ]
    return ok(repo=repo, fork_owner=fork, branch=branch, head=head, base=base_name,
              upstream_owner=up.get("owner"), upstream_repo=up.get("repo"), steps=steps)


@mcp.tool
def status(repo: str = ".") -> dict:
    """Working-tree status of a repo: current branch, ahead/behind, and changed files.

    Read-only. Degrades to an error (never raises) when the path is not a git repo."""
    repo, e = _resolve_repo(repo)
    if e:
        return e
    if not _is_repo(repo):
        return _not_repo_err(repo)
    branch = _run(repo, ["rev-parse", "--abbrev-ref", "HEAD"]).get("out") or None
    porcelain = _run(repo, ["status", "--porcelain=v1", "--branch"])
    staged, unstaged, untracked = [], [], []
    ahead = behind = 0
    for line in porcelain.get("out", "").splitlines():
        if line.startswith("##"):
            m = re.search(r"ahead (\d+)", line)
            if m:
                ahead = int(m.group(1))
            m = re.search(r"behind (\d+)", line)
            if m:
                behind = int(m.group(1))
            continue
        if len(line) < 3:
            continue
        x, y, path = line[0], line[1], line[3:]
        if x == "?" and y == "?":
            untracked.append(path)
            continue
        if x not in (" ", "?"):
            staged.append(path)
        if y not in (" ", "?"):
            unstaged.append(path)
    clean = not (staged or unstaged or untracked)
    return ok(
        repo=repo,
        branch=branch,
        clean=clean,
        ahead=ahead,
        behind=behind,
        staged=staged,
        unstaged=unstaged,
        untracked=untracked,
    )


@mcp.tool
def current_branch(repo: str = ".") -> dict:
    """Return the name of the currently checked-out branch (or detached HEAD info)."""
    repo, e = _resolve_repo(repo)
    if e:
        return e
    if not _is_repo(repo):
        return _not_repo_err(repo)
    r = _run(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    name = r.get("out")
    if name == "HEAD":  # detached
        sha = _run(repo, ["rev-parse", "--short", "HEAD"]).get("out")
        return ok(branch=None, detached=True, sha=sha, repo=repo)
    if not name:
        # unborn branch (no commits yet)
        head = _run(repo, ["symbolic-ref", "--short", "-q", "HEAD"]).get("out") or None
        return ok(branch=head, detached=False, unborn=True, repo=repo)
    return ok(branch=name, detached=False, repo=repo)


@mcp.tool
def create_branch(repo: str = ".", name: str = "", from_ref: str | None = None) -> dict:
    """Create and check out a new branch `name`, optionally based on `from_ref`.

    Validates the branch name; refuses if it already exists."""
    repo, e = _resolve_repo(repo)
    if e:
        return e
    if not _is_repo(repo):
        return _not_repo_err(repo)
    if not _valid_refname(name):
        return err(f"invalid branch name: {name!r}",
                   hint="no spaces or ~^:?*[\\, no '..', no leading '-' or '/'")
    if from_ref is not None and not _valid_ref(from_ref):
        return err(f"invalid from_ref: {from_ref!r}")
    # already exists?
    if _run(repo, ["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"]).get("rc") == 0:
        return err(f"branch already exists: {name}", hint="pick another name")
    args = ["checkout", "-b", name]
    if from_ref:
        args.append(from_ref)
    r = _run(repo, args)
    if r["rc"] != 0:
        return err(r["errout"] or "failed to create branch", rc=r["rc"])
    return ok(repo=repo, branch=name, from_ref=from_ref, message=f"created and switched to {name}")


@mcp.tool
def stage(repo: str = ".", paths: list[str] | None = None) -> dict:
    """Stage changes. `paths` selects files (default: all changes, including new/deleted).

    Pathspecs are passed after `--` so they can never be read as git options."""
    repo, e = _resolve_repo(repo)
    if e:
        return e
    if not _is_repo(repo):
        return _not_repo_err(repo)
    if paths is None or paths == []:
        r = _run(repo, ["add", "-A"])
    else:
        if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
            return err("paths must be a list of non-empty strings")
        r = _run(repo, ["add", "--", *paths])
    if r["rc"] != 0:
        return err(r["errout"] or "git add failed", rc=r["rc"])
    # report what is now staged
    staged = _run(repo, ["diff", "--cached", "--name-only"]).get("out", "")
    staged_list = [s for s in staged.splitlines() if s]
    return ok(repo=repo, staged=staged_list, count=len(staged_list))


@mcp.tool
def commit(repo: str = ".", message: str = "", all: bool = False) -> dict:
    """Create a commit with `message`. Set `all=True` to auto-stage tracked modifications.

    Returns the new commit SHA, or a clear error if there is nothing to commit."""
    repo, e = _resolve_repo(repo)
    if e:
        return e
    if not _is_repo(repo):
        return _not_repo_err(repo)
    if not isinstance(message, str) or not message.strip():
        return err("commit message is required")
    args = ["commit", "-m", message]
    if all:
        args.insert(1, "-a")
    r = _run(repo, args)
    if r["rc"] != 0:
        blob = (r["out"] + "\n" + r["errout"]).lower()
        if "nothing to commit" in blob or "no changes added" in blob:
            return err("nothing to commit", hint="stage changes first or pass all=True")
        return err(r["errout"] or r["out"] or "git commit failed", rc=r["rc"])
    sha = _run(repo, ["rev-parse", "HEAD"]).get("out")
    short = _run(repo, ["rev-parse", "--short", "HEAD"]).get("out")
    return ok(repo=repo, sha=sha, short=short, message=message.splitlines()[0])


@mcp.tool
def push(repo: str = ".", branch: str | None = None, remote: str = "origin",
         set_upstream: bool = True) -> dict:
    """Push `branch` (default: current) to `remote`. Sets upstream on first push.

    Degrades gracefully when no remote is configured."""
    repo, e = _resolve_repo(repo)
    if e:
        return e
    if not _is_repo(repo):
        return _not_repo_err(repo)
    if not _valid_refname(remote):
        return err(f"invalid remote name: {remote!r}")
    if branch is not None and not _valid_refname(branch):
        return err(f"invalid branch name: {branch!r}")
    # resolve branch
    if branch is None:
        cur = _run(repo, ["rev-parse", "--abbrev-ref", "HEAD"]).get("out")
        if not cur or cur == "HEAD":
            return err("cannot determine current branch (detached HEAD?) — pass branch=")
        branch = cur
    # remote exists?
    remotes = _run(repo, ["remote"]).get("out", "").split()
    if remote not in remotes:
        return err(f"no remote '{remote}' configured", remotes=remotes,
                   hint="add one with `git remote add origin <url>` (run locally)")
    args = ["push"]
    if set_upstream:
        args += ["--set-upstream", remote, branch]
    else:
        args += [remote, branch]
    r = _run(repo, args, timeout=120)
    if r["rc"] != 0:
        return err(r["errout"] or "git push failed", rc=r["rc"], remote=remote, branch=branch)
    return ok(repo=repo, remote=remote, branch=branch,
              message=(r["errout"] or r["out"] or f"pushed {branch} -> {remote}"))


@mcp.tool
def diff(repo: str = ".", from_ref: str | None = None, to_ref: str | None = None,
         staged: bool = False) -> dict:
    """Show a diff. With both refs -> `from..to`; with `staged=True` -> index vs HEAD;
    otherwise -> working tree vs HEAD. Returns patch text plus per-file +/- stats. Read-only."""
    repo, e = _resolve_repo(repo)
    if e:
        return e
    if not _is_repo(repo):
        return _not_repo_err(repo)
    for ref in (from_ref, to_ref):
        if ref is not None and not _valid_ref(ref):
            return err(f"invalid ref: {ref!r}")
    base = ["diff"]
    if from_ref and to_ref:
        rng = [f"{from_ref}..{to_ref}"]
    elif from_ref:
        rng = [from_ref]
    elif staged:
        rng = ["--cached"]
    else:
        rng = []
    patch = _run(repo, [*base, *rng])
    if patch["rc"] != 0:
        return err(patch["errout"] or "git diff failed", rc=patch["rc"])
    numstat = _run(repo, [*base, "--numstat", *rng]).get("out", "")
    files, add_total, del_total = [], 0, 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        a, d, path = parts
        add = int(a) if a.isdigit() else 0
        rem = int(d) if d.isdigit() else 0
        binary = a == "-" and d == "-"
        files.append({"file": path, "added": add, "deleted": rem, "binary": binary})
        add_total += add
        del_total += rem
    return ok(
        repo=repo,
        patch=patch["out"],
        files=files,
        files_changed=len(files),
        insertions=add_total,
        deletions=del_total,
    )


@mcp.tool
def log(repo: str = ".", n: int = 20) -> dict:
    """Return the latest `n` commits (sha, short sha, author, date, subject). Read-only."""
    repo, e = _resolve_repo(repo)
    if e:
        return e
    if not _is_repo(repo):
        return _not_repo_err(repo)
    try:
        n = max(1, min(int(n), 1000))
    except (TypeError, ValueError):
        n = 20
    fmt = "%H%x1f%h%x1f%an%x1f%ad%x1f%s"
    r = _run(repo, ["log", f"-n{n}", f"--pretty=format:{fmt}", "--date=short"])
    if r["rc"] != 0:
        blob = (r["errout"] or "").lower()
        if "does not have any commits" in blob or "bad default revision" in blob:
            return ok(repo=repo, commits=[], total=0)
        return err(r["errout"] or "git log failed", rc=r["rc"])
    commits = []
    for line in r["out"].splitlines():
        parts = line.split("\x1f")
        if len(parts) != 5:
            continue
        sha, short, author, date, subject = parts
        commits.append({"sha": sha, "short": short, "author": author,
                        "date": date, "subject": subject})
    return ok(repo=repo, commits=commits, total=len(commits))


def _conventional_type(files: list[dict], commits: list[str]) -> str:
    """Best-effort conventional-commit type from filenames + commit subjects."""
    joined = " ".join(commits).lower()
    for kw, typ in (("fix", "fix"), ("bug", "fix"), ("docs", "docs"), ("refactor", "refactor"),
                    ("test", "test"), ("perf", "perf"), ("chore", "chore")):
        if kw in joined:
            return typ
    paths = " ".join(f.get("file", "") for f in files).lower()
    if files and all("test" in f.get("file", "").lower() for f in files):
        return "test"
    if paths and all(p.endswith((".md", ".rst", ".txt")) for p in paths.split()):
        return "docs"
    return "feat"


@mcp.tool
def pr_body(repo: str = ".", from_ref: str | None = None, to_ref: str = "HEAD",
            task: str | None = None) -> dict:
    """Synthesize a PR title + markdown description from the diff between `from_ref` and
    `to_ref`. Optional `task` is included in the Summary section. Assembles the skeleton —
    files changed, +/- stats, a conventional-commit-style summary line, the commit list, and a
    review checklist. The agent supplies real prose; this fills in the structure. If `from_ref`
    is omitted, compares against the merge-base with the default branch (origin/HEAD or
    main/master), falling back to the parent of `to_ref`."""
    repo, e = _resolve_repo(repo)
    if e:
        return e
    if not _is_repo(repo):
        return _not_repo_err(repo)
    if not _valid_ref(to_ref):
        return err(f"invalid to_ref: {to_ref!r}")
    if from_ref is not None and not _valid_ref(from_ref):
        return err(f"invalid from_ref: {from_ref!r}")

    # Determine the base ref to diff against.
    base = from_ref
    if base is None:
        candidates = ["origin/HEAD", "origin/main", "origin/master", "main", "master"]
        chosen = None
        for c in candidates:
            if _run(repo, ["rev-parse", "--verify", "--quiet", c]).get("rc") == 0:
                chosen = c
                break
        if chosen:
            mb = _run(repo, ["merge-base", chosen, to_ref])
            base = mb["out"] if mb["rc"] == 0 and mb["out"] else chosen
        else:
            parent = _run(repo, ["rev-parse", "--verify", "--quiet", f"{to_ref}^"])
            base = parent["out"] if parent["rc"] == 0 and parent["out"] else None

    rng = [f"{base}..{to_ref}"] if base else [to_ref]

    numstat = _run(repo, ["diff", "--numstat", *rng]).get("out", "")
    files, add_total, del_total = [], 0, 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        a, d, path = parts
        add = int(a) if a.isdigit() else 0
        rem = int(d) if d.isdigit() else 0
        files.append({"file": path, "added": add, "deleted": rem})
        add_total += add
        del_total += rem

    # Commit subjects in range.
    if base:
        clog = _run(repo, ["log", "--pretty=format:%s", f"{base}..{to_ref}"])
    else:
        clog = _run(repo, ["log", "--pretty=format:%s", to_ref])
    subjects = [s for s in clog.get("out", "").splitlines() if s] if clog["rc"] == 0 else []

    ctype = _conventional_type(files, subjects)
    if subjects:
        headline = subjects[0]
        # strip an existing conventional prefix from the headline to avoid doubling
        headline = re.sub(r"^(feat|fix|docs|refactor|test|perf|chore)(\([^)]*\))?!?:\s*",
                          "", headline, flags=re.IGNORECASE)
    else:
        headline = "update " + (files[0]["file"] if files else "repository")
    title = f"{ctype}: {headline}"

    # Markdown body.
    lines: list[str] = ["## Summary", ""]
    if task and str(task).strip():
        lines.append(str(task).strip())
    elif subjects:
        lines.append(subjects[0])
    else:
        lines.append("_Describe the change here._")
    lines.append("")

    lines.append("## Changes")
    if files:
        for f in files:
            lines.append(f"- `{f['file']}` (+{f['added']} / -{f['deleted']})")
    else:
        lines.append("- _No file changes detected in range._")
    lines.append("")

    if len(subjects) > 1:
        lines.append("## Commits")
        for s in subjects:
            lines.append(f"- {s}")
        lines.append("")

    lines.append("## Stats")
    lines.append(f"- Files changed: {len(files)}")
    lines.append(f"- Insertions: {add_total}")
    lines.append(f"- Deletions: {del_total}")
    lines.append("")

    lines.append("## Checklist")
    lines += [
        "- [ ] Code compiles / lints",
        "- [ ] Tests added or updated",
        "- [ ] Docs updated if needed",
        "- [ ] Self-reviewed the diff",
    ]
    body = "\n".join(lines)

    return ok(
        repo=repo,
        title=title,
        body=body,
        type=ctype,
        base=base,
        to_ref=to_ref,
        task=task,
        files=files,
        files_changed=len(files),
        insertions=add_total,
        deletions=del_total,
        commits=subjects,
    )


def _q(s: str) -> str:
    """Single-quote a string for inclusion in a copy-pasteable shell command."""
    import shlex
    return shlex.quote(s)


@mcp.tool
def open_pr(repo: str = ".", title: str = "", body: str = "", base: str = "main",
            head: str | None = None) -> dict:
    """Open a GitHub pull request. Uses `gh pr create` when the GitHub CLI is installed;
    otherwise returns a ready-to-run `gh` command plus a pointer to the github-server MCP
    tool to call (graceful degradation — never hard-requires gh)."""
    repo, e = _resolve_repo(repo)
    if e:
        return e
    if not _is_repo(repo):
        return _not_repo_err(repo)
    if not isinstance(title, str) or not title.strip():
        return err("PR title is required")
    if not _valid_refname(base):
        return err(f"invalid base branch: {base!r}")
    if head is not None and not _valid_head_ref(head):
        return err(f"invalid head branch: {head!r}")

    # resolve head to current branch if omitted
    if head is None:
        cur = _run(repo, ["rev-parse", "--abbrev-ref", "HEAD"]).get("out")
        head = cur if cur and cur != "HEAD" else None

    owner_info = remote_owner_repo(repo)
    gh_owner = owner_info.get("owner") if owner_info.get("ok") else None
    gh_repo = owner_info.get("repo") if owner_info.get("ok") else None

    args = ["gh", "pr", "create", "--title", title, "--body", body, "--base", base]
    if head:
        args += ["--head", head]
    # shell-quoted command string for the degraded path / for the agent's reference
    cmd_str = "gh pr create --title " + _q(title) + " --body " + _q(body) + " --base " + _q(base)
    if head:
        cmd_str += " --head " + _q(head)

    if not shutil.which("gh"):
        fb_args = {"title": title, "body": body, "base": base, "head": head}
        if gh_owner:
            fb_args["owner"] = gh_owner
        if gh_repo:
            fb_args["repo"] = gh_repo
        return ok(
            created=False,
            tool_available=False,
            command=cmd_str,
            argv=args,
            repo=repo,
            base=base,
            head=head,
            fallback={
                "server": "github",
                "tool": "create_pull_request",
                "args": fb_args,
            },
            next_step={
                "server": "github",
                "tool": "create_pull_request",
                "args": fb_args,
            },
            hint="gh not installed: run the `command` locally, or call the github "
                 "create_pull_request tool with fallback.args.",
        )

    r = _tool(repo, args)
    if r["rc"] != 0:
        return err(r["errout"] or r["out"] or "gh pr create failed", rc=r["rc"],
                   command=cmd_str, base=base, head=head,
                   hint="ensure the branch is pushed and `gh auth login` is done, or use "
                        "the github create_pull_request tool.")
    url = r["out"].strip().splitlines()[-1] if r["out"].strip() else None
    return ok(created=True, tool_available=True, url=url, repo=repo,
              base=base, head=head, title=title,
              next_step={"server": "project-memory", "tool": "remember",
                         "args": {"note": f"Opened PR: {title}", "kind": "milestone"}})


if __name__ == "__main__":
    mcp.run()
