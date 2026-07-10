"""recipes — high-level playbooks. Each tool RETURNS an ordered, ready-to-run step plan
(list of {server, tool, args, why}) that the agent executes itself across the other INDEPENDENT
servers. This server NEVER calls the other servers at runtime — it only returns the plan.

The one exception is cross_search(), which reads (read-only) the shared SQLite FTS indexes directly
to give the agent a single unified search across code, notes, snippets, bookmarks and memory."""
from __future__ import annotations

import sqlite3

from mcp_base import base_data_dir, default_repo, err, make_server, ok, resolve_project

mcp = make_server(
    "recipes",
    instructions=("Playbooks that RETURN an ordered step plan ({server,tool,args,why}) for the agent "
                  "to run across the other servers. list_recipes, weekly_outreach, prep_for_company, "
                  "apply_to_job, daily_briefing, ship_project, cross_search."),
)


def _step(server: str, tool: str, args: dict | None = None, why: str = "",
          output_key: str | None = None, input_from: str | None = None,
          on_error: str = "stop") -> dict:
    s: dict = {"server": server, "tool": tool, "args": args or {}, "why": why}
    if output_key:
        s["output_key"] = output_key
    if input_from:
        s["input_from"] = input_from
    if on_error:
        s["on_error"] = on_error
    return s


# ----------------------------- Playbooks -----------------------------
RECIPES = {
    "weekly_outreach": "Plan a week of cold outreach: find leads, draft, queue, schedule follow-ups.",
    "bulk_ceo_outreach": "Bulk CEO email discovery + validate + draft outreach for a company list.",
    "prep_for_company": "Research a target company end-to-end before reaching out or interviewing.",
    "apply_to_job": "Turn a job description into a tailored resume + cover letter + tracked application.",
    "daily_briefing": "Morning briefing: unread feeds, today's tasks, habits, calendar, follow-ups due.",
    "ship_project": "Pre-ship checklist for a repo: health, README/changelog, devlog, memory checkpoint.",
    "contribute_upstream": "Fork upstream, branch, push, and open a PR from your fork.",
    "triage_issue": "File a GitHub issue and track it in project-memory + task-manager.",
}


@mcp.tool
def list_recipes() -> dict:
    """List the available playbooks and one-line descriptions."""
    return ok(recipes=[{"name": k, "description": v} for k, v in RECIPES.items()],
              note="Each playbook tool returns an ordered step plan for you to execute.")


@mcp.tool
def weekly_outreach() -> dict:
    """Return a step plan for a week of outreach across funding-radar/apollo/email-finder/emailcheck/
    reachout/contacts. Execute the steps in order, feeding outputs forward."""
    steps = [
        _step("funding-radar", "list_leads", {"limit": 25},
              why="Surface freshly-funded companies worth contacting this week.",
              output_key="leads"),
        _step("campaign", "filter_uncontacted", {"companies": "<from leads.fresh>"},
              why="Dedup gate — drop already-contacted and suppressed companies.",
              output_key="filtered", input_from="leads.fresh"),
        _step("apollo", "ensure_ready", {},
              why="Auto-start CDP browser + Google login from .env.",
              output_key="apollo_ready"),
        _step("apollo", "bulk_find_ceo_email",
              {"companies": "<from filtered.fresh>", "persist_contacts": True},
              why="Apollo Access Email per company (Path 1 fast; extension only on miss).",
              output_key="ceo_emails", input_from="filtered.fresh"),
        _step("emailcheck", "validate_email", {"email": "<found-email>"},
              why="Verify deliverability before sending to protect sender reputation.",
              input_from="ceo_emails.results[0].email"),
        _step("contacts", "add_contact",
              {"name": "<lead-name>", "company": "<company>", "email": "<found-email>"},
              why="Persist each verified lead as a contact for tracking.",
              input_from="ceo_emails.results[0]"),
        _step("reachout", "create_draft",
              {"to_email": "<found-email>", "subject": "<subject>", "body": "<personalized-body>",
               "company": "<company>"},
              why="Draft a personalized first-touch email per contact.",
              output_key="draft"),
        _step("reachout", "schedule_send", {"outreach_id": "<from create_draft>", "run_at": "<iso-datetime>"},
              why="Queue sends spread across the week.",
              input_from="draft.outreach_id"),
        _step("reachout", "send_draft", {"draft_id": "<draft_id>", "sync_campaign": True},
              why="Send approved drafts and sync the campaign ledger.",
              input_from="draft.draft_id"),
        _step("campaign", "record_outreach",
              {"company": "<company>", "domain": "<company-domain>", "email": "<found-email>"},
              why="Record outreach in the dedup ledger after send."),
        _step("reachout", "thread_followup",
              {"outreach_id": "<from create_draft>", "template_name": "<followup-template>"},
              why="Queue a follow-up for non-repliers.",
              input_from="draft.outreach_id"),
    ]
    return ok(recipe="weekly_outreach", steps=steps)


@mcp.tool
def bulk_ceo_outreach(companies: list[dict] | None = None) -> dict:
    """Bulk CEO email discovery + validate + draft outreach for a company list.
    Each company: {domain, company?}. Uses apollo.bulk_find_ceo_email (Path 1 fast, extension on miss)."""
    steps = [
        _step("campaign", "filter_uncontacted", {"companies": companies or "<companies>"},
              why="Dedup gate — drop already-contacted and suppressed companies.",
              output_key="filtered"),
        _step("apollo", "ensure_ready", {},
              why="Auto-start CDP browser; user clicks Apollo FAB once per session on LinkedIn.",
              output_key="apollo_ready"),
        _step("apollo", "bulk_find_ceo_email",
              {"companies": "<from filtered.fresh>", "persist_contacts": True},
              why="CEO emails via Apollo Access Email; extension only when app search misses.",
              output_key="ceo_emails", input_from="filtered.fresh"),
        _step("emailcheck", "validate_email", {"email": "<found-email>"},
              why="Verify deliverability per found email.",
              input_from="ceo_emails.results"),
        _step("reachout", "create_draft",
              {"to_email": "<found-email>", "subject": "<subject>", "body": "<personalized-body>",
               "company": "<company>", "recipient_name": "<ceo-name>"},
              why="Draft personalized outreach per CEO email found.",
              input_from="ceo_emails.results"),
        _step("campaign", "record_outreach",
              {"company": "<company>", "domain": "<company-domain>", "email": "<found-email>"},
              why="Record outreach in dedup ledger after send.",
              input_from="ceo_emails.results"),
        _step("mailmerge", "import_csv",
              {"source": "<bulk-results-csv>", "email_column": "email"},
              why="Optional: import bulk CEO results for mailmerge render_batch + throttle."),
    ]
    return ok(recipe="bulk_ceo_outreach", steps=steps)


@mcp.tool
def prep_for_company(company: str) -> dict:
    """Return a research step plan for a target `company`: funding, news, contacts, prior outreach,
    and a meeting brief. Execute in order."""
    company = (company or "").strip()
    steps = [
        _step("funding-radar", "list_leads", {"limit": 50},
              "Check recent funding leads, then filter for this company."),
        _step("news-radar", "scan_company", {"name": company},
              "Pull recent news and signals."),
        _step("github-profile", "profile_stats", {"username": company},
              "Inspect their public engineering footprint (pass their GitHub org/user)."),
        _step("contacts", "find", {"company": company},
              "Find anyone you already know there."),
        _step("reachout", "list_outreach", {"company": company},
              "Review any prior outreach to this company."),
        _step("meeting-prep", "build_brief", {"company": company},
              "Assemble a consolidated briefing from local data."),
    ]
    return ok(recipe="prep_for_company", company=company, steps=steps)


@mcp.tool
def apply_to_job(jd_text: str) -> dict:
    """Return a step plan to apply to a job from its description `jd_text`: tailor resume, write a
    cover letter, prep interview answers, and track the application."""
    jd = (jd_text or "").strip()
    preview = jd[:280]
    steps = [
        _step("resume-forge", "keyword_gaps", {"jd": jd},
              "Surface JD keywords missing from your resume."),
        _step("resume-forge", "build_resume", {},
              "Build/tailor the resume to the job description's keywords and requirements."),
        _step("resume-forge", "cover_letter",
              {"company": "<company>", "role": "<role>", "body": "<cover-letter-body>"},
              "Generate a targeted cover letter."),
        _step("interview-prep", "pattern_coverage", {},
              "Review interview pattern coverage to prep likely questions."),
        _step("jobtrack", "add_application",
              {"company": "<company>", "role": "<role>", "notes": preview},
              "Record the application so you can track its status."),
    ]
    return ok(recipe="apply_to_job", jd_preview=preview, steps=steps)


@mcp.tool
def daily_briefing() -> dict:
    """Return a step plan for a morning briefing: unread feeds, today's tasks, habits due, calendar,
    and outreach follow-ups due."""
    steps = [
        _step("daily-digest", "today", {},
              why="Aggregated view of everything needing action today."),
        _step("rss-reader", "unread", {"limit": 15},
              why="What's new in your feeds."),
        _step("task-manager", "agenda", {},
              why="Tasks due today."),
        _step("habit-tracker", "today", {},
              why="Habits to keep streaks alive."),
        _step("time-tracker", "daily_report", {},
              why="Where yesterday's time went."),
        _step("reachout", "list_followups_due", {},
              why="Outreach follow-ups that need to go out today."),
        _step("expense-tracker", "budget_status", {},
              why="Quick check on this month's budgets."),
    ]
    return ok(recipe="daily_briefing", steps=steps)


@mcp.tool
def ship_project(repo: str) -> dict:
    """Return a pre-ship checklist step plan for `repo`: health check, README/changelog refresh,
    devlog entry, and a project-memory checkpoint."""
    repo = (repo or "").strip()
    steps = [
        _step("repo-health", "health_report", {"repo": repo},
              why="Run health checks (tests, lint, deps, license)."),
        _step("readme-changelog", "lint_commits", {"repo": repo},
              why="Lint commit messages before shipping."),
        _step("readme-changelog", "gen_readme", {"repo": repo},
              why="Refresh the README."),
        _step("readme-changelog", "gen_changelog", {"repo": repo},
              why="Generate a changelog entry."),
        _step("codeindex", "reindex", {"project": repo},
              why="Re-index the codebase so search is current."),
        _step("devlog", "weekly_summary", {"repo": repo},
              why="Log what shipped in this cycle."),
        _step("project-memory", "checkpoint", {"summary": "<what shipped>", "project": repo},
              why="Snapshot project state for future context."),
    ]
    return ok(recipe="ship_project", repo=repo, steps=steps)


# ----------------------------- cross_search -----------------------------
# (db_server, fts_table, join_table, join_rowid_col, select_cols) per shared index.
_SEARCH_TARGETS = [
    ("codeindex", "files_fts", None, None, "path, project"),
    ("notes", "notes_fts", "notes", "id", "title"),
    ("snippet-vault", "snippets_fts", "snippets", "id", "title, lang"),
    ("bookmark-vault", "bm_fts", "bookmarks", "id", "title, url"),
    ("project-memory", "memories_fts", "memories", "id", "note, project"),
]


def _open_ro(server: str) -> sqlite3.Connection | None:
    from pathlib import Path
    p = base_data_dir() / server / "store.db"
    if not p.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
            (name,)).fetchone()
        return row is not None
    except Exception:
        return False


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _fts_safe(query: str) -> str:
    """Quote tokens so arbitrary user text can't break FTS5 syntax."""
    import re
    toks = re.findall(r"[A-Za-z0-9]{2,}", query or "")
    return " OR ".join(f'"{t}"' for t in toks)


@mcp.tool
def cross_search(query: str, limit: int = 10) -> dict:
    """Unified read-only search across the shared SQLite FTS indexes — codeindex (files_fts),
    notes (notes_fts), snippet-vault (snippets_fts), bookmark-vault (bm_fts) and project-memory
    (memories_fts). Results are merged with a `source` field. Missing DBs are skipped; never crashes."""
    limit = max(1, min(limit, 50))
    fts = _fts_safe(query)
    results: list[dict] = []
    searched: list[str] = []
    skipped: list[str] = []

    for server, fts_table, join_table, rowid_col, cols in _SEARCH_TARGETS:
        conn = _open_ro(server)
        if conn is None:
            skipped.append(server)
            continue
        try:
            # Introspect: the FTS table must exist; for join targets so must the join table.
            if not _table_exists(conn, fts_table):
                skipped.append(server)
                continue
            if join_table is not None and not _table_exists(conn, join_table):
                skipped.append(server)
                continue

            # Keep only columns that actually exist on the selected-from table, so a
            # renamed/dropped column in an old schema can't break the query.
            present_cols = _columns(conn, join_table or fts_table)
            wanted = [c.strip() for c in cols.split(",") if c.strip()]
            sel_cols = [c for c in wanted if c in present_cols]
            if not sel_cols:
                # nothing recognizable to return from this DB — skip gracefully.
                skipped.append(server)
                continue

            searched.append(server)
            if not fts:
                continue

            if join_table is None:
                # content-table FTS (codeindex): select straight from the fts table.
                rows = conn.execute(
                    f"SELECT {', '.join(sel_cols)} FROM {fts_table} "
                    f"WHERE {fts_table} MATCH ? ORDER BY rank LIMIT ?",
                    (fts, limit)).fetchall()
            else:
                if rowid_col not in present_cols:
                    skipped.append(server)
                    continue
                rows = conn.execute(
                    f"SELECT j.{rowid_col} AS rowid, "
                    f"{', '.join('j.'+c for c in sel_cols)} "
                    f"FROM {fts_table} f JOIN {join_table} j ON j.{rowid_col}=f.rowid "
                    f"WHERE {fts_table} MATCH ? ORDER BY rank LIMIT ?",
                    (fts, limit)).fetchall()
            for r in rows:
                item = dict(r)
                item["source"] = server
                results.append(item)
        except Exception:
            # malformed/old schema — skip this DB rather than crash the whole search.
            if server not in skipped:
                skipped.append(server)
            continue
        finally:
            try:
                conn.close()
            except Exception:
                pass

    return ok(query=query, results=results, count=len(results),
              searched=searched, skipped=skipped)


# ----------------------------- edit/PR playbooks -----------------------------
# These chain the code-editing suite. Tool names follow the fixed CONTRACT so the
# steps resolve once codeedit/gitflow exist:
#   codeedit: preview_patch, apply_patch, replace_in_file, insert_lines, delete_lines,
#             replace_lines, write_file, multi_edit, undo, list_backups, validate,
#             syntax_check, format_code, lint, run_tests
#   gitflow:  status, current_branch, create_branch, stage, commit, push, diff, log,
#             pr_body, open_pr


@mcp.tool
def plan_edits(task: str, repo: str | None = None) -> dict:
    """Return an ordered list of concrete edit steps for `task`, grounded in the codeindex.

    Reads codeindex's read-only store (files/symbols + FTS) to pick the most relevant files and
    line anchors, returning [{path, lineno?, action, reason}]. If the index is unavailable or empty
    it degrades to an empty plan plus a hint to run codeindex.index_project first."""
    task = (task or "").strip()
    if not task:
        return err("task must be a non-empty string", plan=[])

    conn = _open_ro("codeindex")
    if conn is None:
        return ok(task=task, plan=[],
                  hint="codeindex not initialized — run codeindex.index_project(<repo>) first, "
                       "then retry plan_edits.")
    try:
        if not _table_exists(conn, "files_fts") or not _table_exists(conn, "files"):
            return ok(task=task, plan=[],
                      hint="codeindex schema missing files index — run codeindex.index_project first.")

        # Resolve the project: caller-supplied repo (matched against indexed projects) or the last one.
        proj = None
        if repo:
            target = str(__import__("pathlib").Path(repo).expanduser().resolve())
            row = conn.execute("SELECT DISTINCT project FROM files WHERE project=?", (target,)).fetchone()
            if row:
                proj = row["project"]
        if proj is None:
            row = conn.execute("SELECT value FROM meta WHERE key='last_project'").fetchone()
            proj = row["value"] if row else None
        if proj is None:
            r2 = conn.execute("SELECT project FROM files LIMIT 1").fetchone()
            proj = r2["project"] if r2 else None
        if proj is None:
            return ok(task=task, plan=[], hint="no indexed project — run codeindex.index_project first.")

        fts = _fts_safe(task)
        paths: list[str] = []
        if fts:
            try:
                rows = conn.execute(
                    "SELECT path FROM files_fts WHERE files_fts MATCH ? AND project=? "
                    "ORDER BY rank LIMIT 6", (fts, proj)).fetchall()
                paths = [r["path"] for r in rows]
            except Exception:
                paths = []
        if not paths:
            # FTS produced nothing usable — fall back to a few files in the project.
            rows = conn.execute(
                "SELECT path FROM files WHERE project=? ORDER BY path LIMIT 6", (proj,)).fetchall()
            paths = [r["path"] for r in rows]

        if not paths:
            return ok(task=task, project=proj, plan=[],
                      hint="project indexed but has no files — re-run codeindex.index_project.")

        plan: list[dict] = []
        for p in paths:
            # Anchor on the first symbol in the file so the agent has a precise line to inspect/edit.
            srow = conn.execute(
                "SELECT name, kind, lineno FROM symbols WHERE project=? AND path=? "
                "ORDER BY lineno LIMIT 1", (proj, p)).fetchone()
            if srow:
                plan.append({
                    "path": p, "lineno": srow["lineno"], "action": "edit",
                    "reason": f"relevant to task; first symbol {srow['kind']} '{srow['name']}' at "
                              f"line {srow['lineno']} — inspect with codeindex.get_lines then "
                              f"codeedit.replace_lines/replace_in_file.",
                })
            else:
                plan.append({
                    "path": p, "action": "edit",
                    "reason": "relevant to task (no indexed symbols) — inspect with "
                              "codeindex.get_file then codeedit.replace_in_file.",
                })
        return ok(task=task, project=proj, plan=plan,
                  note="Ordered by relevance. Use codeedit with validate=True, then run_tests "
                       "before committing.")
    except Exception as e:
        return ok(task=task, plan=[], hint=f"could not derive a plan from the index ({e}); "
                                           "run codeindex.index_project then retry.")
    finally:
        try:
            conn.close()
        except Exception:
            pass


@mcp.tool
def make_change(task: str, repo: str | None = None) -> dict:
    """Return a step plan to safely make a code change for `task`: gather context, branch, apply a
    validated patch, run tests, then stage + commit. Execute in order, feeding outputs forward."""
    task = (task or "").strip()
    project = repo or default_repo()
    steps = [
        _step("codeindex", "relevant_context", {"task": task, "project": project},
              why="Pull the right files/symbols for this task before editing."),
        _step("gitflow", "create_branch", {"repo": project, "name": "<feature-branch>"},
              why="Create a feature branch before editing."),
        _step("gitflow", "current_branch", {"repo": project},
              why="Confirm the working branch."),
        _step("codeedit", "preview_patch", {"path": "<file>", "patch": "<unified-diff>"},
              why="Preview the change before touching disk."),
        _step("codeedit", "apply_patch", {"path": "<file>", "patch": "<unified-diff>", "validate": True},
              why="Apply the patch with validation so a syntax error can't land."),
        _step("codeedit", "run_tests", {},
              why="Run the test suite to confirm the change is green."),
        _step("gitflow", "stage", {"repo": project, "paths": ["<changed-file>"]},
              why="Stage the edited files."),
        _step("gitflow", "commit", {"repo": project, "message": f"<concise message for: {task[:80]}>"},
              why="Commit the staged change."),
    ]
    return ok(recipe="make_change", task=task, repo=project, steps=steps)


@mcp.tool
def review_and_pr(task: str, repo: str | None = None) -> dict:
    """Return a step plan to review a change and open a PR for `task`: gather context, validate-edit,
    test, stage, commit, push, draft a PR body, then open the PR. Execute in order."""
    task = (task or "").strip()
    project = repo or default_repo()
    steps = [
        _step("codeindex", "relevant_context", {"task": task, "project": project},
              why="Pull the right files/symbols for this task before editing."),
        _step("gitflow", "create_branch", {"repo": project, "name": "<feature-branch>"},
              why="Create a feature branch for this change."),
        _step("codeedit", "apply_patch", {"path": "<file>", "patch": "<unified-diff>", "validate": True},
              why="Apply the patch with validation so a syntax error can't land."),
        _step("codeedit", "run_tests", {},
              why="Run the test suite to confirm the change is green."),
        _step("gitflow", "stage", {"repo": project, "paths": ["<changed-file>"]},
              why="Stage the edited files."),
        _step("gitflow", "commit", {"repo": project, "message": f"<concise message for: {task[:80]}>"},
              why="Commit the staged change."),
        _step("gitflow", "push", {"repo": project},
              why="Push the branch to the remote so a PR can be opened."),
        _step("gitflow", "diff", {"repo": project},
              why="Review the final diff before drafting the PR description."),
        _step("gitflow", "pr_body", {"repo": project, "task": task},
              why="Draft a PR title/body from the diff and task.", output_key="pr"),
        _step("gitflow", "open_pr",
              {"repo": project, "title": "<from pr.title>", "body": "<from pr.body>"},
              why="Open the pull request (or use github.create_pull_request fallback).",
              input_from="pr.title"),
    ]
    return ok(recipe="review_and_pr", task=task, repo=project, steps=steps)


# ----------------------------- suite guide / discovery -----------------------------

_SERVER_ROLES = {
    "codeindex": "Index a repo; pull relevant context, symbols, lines, search, diffs.",
    "codeedit": "Safe file edits: preview/apply patches, line ops, validate, format, lint, run_tests, undo.",
    "gitflow": "Git workflow: branch, stage, commit, push, diff, log, pr_body, open_pr.",
    "project-memory": "Durable project memory: checkpoint/resume, decisions, todos, conventions.",
    "recipes": "Playbooks that return ordered step plans chaining the other servers (this server).",
    "repo-health": "Tests/lint/deps/license health report for a repo.",
    "readme-changelog": "Generate README and changelog entries.",
    "devlog": "Log and summarize what shipped.",
    "notes": "Personal notes with FTS.",
    "snippet-vault": "Reusable code snippets with FTS.",
    "bookmark-vault": "Saved links with FTS.",
}


# Make the new playbooks discoverable from list_recipes too (without changing its return shape).
RECIPES.update({
    "make_change": "Safely make a code change: context -> validated edit -> tests -> commit.",
    "review_and_pr": "Review a change and open a PR: make_change -> push -> pr_body -> open_pr.",
    "contribute_upstream": "Fork upstream, branch, push, and open a PR from your fork.",
    "triage_issue": "File a GitHub issue and track it in project-memory + task-manager.",
})


# ----------------------------- executor + session bootstrap -----------------------------

PLAYBOOK_CALLS: dict[str, tuple[str, ...]] = {
    "weekly_outreach": (),
    "bulk_ceo_outreach": (),
    "prep_for_company": ("company",),
    "apply_to_job": ("jd_text",),
    "daily_briefing": (),
    "ship_project": ("repo",),
    "make_change": ("task", "repo"),
    "review_and_pr": ("task", "repo"),
    "contribute_upstream": ("upstream_owner", "upstream_repo", "task"),
    "triage_issue": ("repo", "title", "body"),
    "prime_session": ("project", "task"),
}


def _invoke_playbook(name: str, params: dict) -> dict:
    """Call a playbook function by name with params."""
    fn = globals().get(name)
    if not fn or not callable(fn):
        return err(f"unknown recipe: {name}", available=list(PLAYBOOK_CALLS))
    keys = PLAYBOOK_CALLS.get(name)
    if keys is None:
        return err(f"unknown recipe: {name}", available=list(PLAYBOOK_CALLS))
    if not keys:
        return fn()
    if name == "prime_session":
        return fn(params.get("project"), params.get("task"))
    if name in ("make_change", "review_and_pr"):
        return fn(params.get("task", ""), params.get("repo"))
    args = []
    for k in keys:
        val = params.get(k)
        if k in ("company", "jd_text", "task", "repo", "upstream_owner", "upstream_repo",
                 "title", "body") and not (val or "").strip() and k in params:
            pass
        args.append(val if val is not None else "")
    return fn(*args)


def _deep_get(obj: dict, path: str):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _apply_input_from(step: dict, outputs: dict) -> dict:
    args = dict(step.get("args") or {})
    spec = step.get("input_from")
    if not spec:
        return args
    val = _deep_get(outputs, spec)
    if val is None and "." in spec:
        key, field = spec.rsplit(".", 1)
        src = outputs.get(key, {})
        if isinstance(src, dict):
            val = src.get(field)
    if val is None:
        return args
    for k, v in list(args.items()):
        if isinstance(v, str) and ("<from" in v or v.startswith("<") and v.endswith(">")):
            args[k] = val
    if isinstance(val, (str, int, float, bool)):
        leaf = spec.rsplit(".", 1)[-1]
        if leaf not in args or str(args.get(leaf, "")).startswith("<"):
            args[leaf] = val
    return args


def _resolve_steps(steps: list[dict], outputs: dict | None = None) -> list[dict]:
    """Resolve input_from chains for dry_run display."""
    outputs = outputs or {}
    resolved = []
    for step in steps:
        s = dict(step)
        s["args"] = _apply_input_from(step, outputs)
        resolved.append(s)
        key = step.get("output_key") or f"{step['server']}.{step['tool']}"
        outputs[key] = outputs.get(key, {})
    return resolved


def _load_server_mcp(server_name: str):
    import importlib.util
    import sys
    from pathlib import Path

    sp = Path(__file__).resolve().parent.parent / server_name / "server.py"
    if not sp.exists():
        return None, f"no server.py for {server_name}"
    mod_name = f"_recipes_run_{server_name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(mod_name, sp)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    srv = getattr(mod, "mcp", None)
    if srv is None:
        return None, f"{server_name} has no mcp"
    return srv, None


@mcp.tool
def resolve_tool(server: str, tool: str, hub_mode: bool = False) -> dict:
    """Return the tool name to call — namespaced when hub_mode is active."""
    if hub_mode:
        return ok(server=server, tool=tool, resolved=f"{server}_{tool}", hub_mode=True)
    return ok(server=server, tool=tool, resolved=tool, hub_mode=False)


@mcp.tool
def run(recipe: str, params: dict | None = None, dry_run: bool = False,
        stop_on_error: bool = True) -> dict:
    """Execute a playbook step-by-step via in-process FastMCP clients, or dry_run the resolved plan."""
    import asyncio

    from fastmcp import Client

    params = params or {}
    plan = _invoke_playbook(recipe, params)
    if not plan.get("ok"):
        return plan
    steps = plan.get("steps") or []
    if dry_run:
        return ok(recipe=recipe, dry_run=True, steps=_resolve_steps(steps),
                  steps_count=len(steps))

    async def _exec():
        outputs: dict = {}
        results: list[dict] = []
        for i, step in enumerate(steps):
            srv_name = step["server"]
            tool_name = step["tool"]
            args = _apply_input_from(step, outputs)
            mcp_srv, load_err = _load_server_mcp(srv_name)
            if mcp_srv is None:
                return ok(ok=False, failed_at=i, error=load_err, steps_completed=i,
                          results=results, partial_context=outputs)
            try:
                async with Client(mcp_srv) as c:
                    res = await c.call_tool(tool_name, args)
                    data = res.data if hasattr(res, "data") else res
            except Exception as e:  # noqa: BLE001
                data = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            key = step.get("output_key") or f"{srv_name}.{tool_name}"
            outputs[key] = data
            results.append({"step": i, "server": srv_name, "tool": tool_name, "result": data})
            if isinstance(data, dict) and data.get("ok") is False and stop_on_error:
                return ok(ok=False, failed_at=i, steps_completed=i, results=results,
                          partial_context=outputs, error=data.get("error"))
        return ok(ok=True, steps_completed=len(steps), results=results, partial_context=outputs)

    return asyncio.run(_exec())


@mcp.tool
def prime_session(project: str | None = None, task: str | None = None) -> dict:
    """Session bootstrap: index (if needed), resume memory, pull relevant context, export files."""
    proj = resolve_project(project)
    if isinstance(proj, dict):
        return proj
    steps = [
        _step("codeindex", "index_project", {"path": proj},
              why="Ensure the repo is indexed (re-run if stale).", output_key="index"),
        _step("project-memory", "resume", {"project": proj},
              why="Rehydrate prior decisions, todos, and conventions.", output_key="memory"),
    ]
    if task and task.strip():
        steps.append(_step("codeindex", "relevant_context", {"task": task.strip(), "project": proj},
                           why="Pull task-relevant files/symbols.", output_key="context"))
    steps += [
        _step("codeindex", "export_context_file", {"project": proj, "write": True},
              why="Write .cursor/context if missing or stale."),
        _step("project-memory", "export_digest", {"project": proj, "write": True, "prime_clients": True},
              why="Export MEMORY.md for client priming."),
    ]
    return ok(recipe="prime_session", project=proj, task=task, steps=steps,
              note="Call recipes.run('prime_session', {project, task}) or execute steps manually.")


@mcp.tool
def contribute_upstream(upstream_owner: str, upstream_repo: str, task: str) -> dict:
    """OSS contribution flow: fork upstream, branch, commit, push to fork, open PR."""
    task = (task or "").strip()
    upstream_owner = (upstream_owner or "").strip()
    upstream_repo = (upstream_repo or "").strip()
    branch = "<feature-branch>"
    steps = [
        _step("github", "fork_repository",
              {"owner": upstream_owner, "repo": upstream_repo},
              why="Fork the upstream repo to your GitHub account.", output_key="fork"),
        _step("gitflow", "add_remote",
              {"name": "upstream", "url": f"https://github.com/{upstream_owner}/{upstream_repo}.git"},
              why="Add upstream remote for sync."),
        _step("gitflow", "create_branch", {"name": branch},
              why="Create a feature branch for your change."),
        _step("codeindex", "relevant_context", {"task": task},
              why="Gather context before editing."),
        _step("codeedit", "apply_patch", {"path": "<file>", "patch": "<unified-diff>", "validate": True},
              why="Apply the validated change."),
        _step("codeedit", "run_tests", {}, why="Confirm tests pass."),
        _step("gitflow", "stage", {"paths": ["<changed-file>"]}, why="Stage changes."),
        _step("gitflow", "commit", {"message": f"<message for: {task[:80]}>"}, why="Commit."),
        _step("gitflow", "push", {"branch": branch, "remote": "origin"},
              why="Push branch to your fork."),
        _step("gitflow", "open_pr",
              {"title": f"<title for: {task[:80]}>", "body": "<from pr_body>",
               "base": "main", "head": f"namansh70747:{branch}"},
              why="Open PR from fork to upstream (head=youruser:branch)."),
        _step("gitflow", "pr_body", {"task": task}, why="Generate PR body with task context.",
              output_key="pr"),
    ]
    return ok(recipe="contribute_upstream", upstream=f"{upstream_owner}/{upstream_repo}",
              task=task, steps=steps)


@mcp.tool
def triage_issue(repo: str, title: str, body: str) -> dict:
    """File a GitHub issue and track it locally."""
    project = repo or default_repo()
    steps = [
        _step("github", "issue_write",
              {"owner": "<owner>", "repo": "<repo>", "title": title, "body": body},
              why="Create the GitHub issue.", output_key="issue"),
        _step("project-memory", "remember",
              {"note": f"Issue: {title}", "kind": "todo", "project": project},
              why="Remember the issue in project memory."),
        _step("task-manager", "add_task",
              {"title": title, "notes": body},
              why="Add a tracked task for follow-up."),
    ]
    return ok(recipe="triage_issue", repo=project, title=title, steps=steps)


def _suite_briefing(hub_mode: bool = False) -> dict:
    return ok(
        title="How to drive this MCP suite",
        first_steps=[
            _step("codeindex", "index_project", {"path": "<repo>"},
                  "Index the repo once so every other tool has fresh structure to work from."),
            _step("project-memory", "resume", {},
                  "Rehydrate prior decisions, todos, and conventions for this project."),
            _step("codeindex", "relevant_context", {"task": "<what you're about to do>"},
                  "Pull a window-sized bundle of the right files/symbols for the task."),
        ],
        edit_loop=[
            "1. recipes.plan_edits(task) -> ordered [{path, lineno, action, reason}] from the index.",
            "2. codeedit.preview_patch -> apply_patch(validate=True) to land the change safely.",
            "3. codeedit.run_tests (and format_code/lint as needed) to confirm it's green.",
            "4. gitflow.stage -> gitflow.commit; push + gitflow.pr_body -> gitflow.open_pr to ship.",
            "5. codeindex.reindex and project-memory.checkpoint to keep context current.",
        ],
        playbooks=[{"name": k, "description": v} for k, v in RECIPES.items()] + [
            {"name": "make_change", "description": "context -> validated edit -> test -> commit."},
            {"name": "review_and_pr", "description": "make_change -> push -> pr_body -> open_pr."},
            {"name": "plan_edits", "description": "index-grounded ordered edit steps for a task."},
        ],
        servers=_SERVER_ROLES,
        note="Each playbook RETURNS a plan ({server,tool,args,why}); you execute the steps yourself "
             "or call recipes.run(recipe, params). Hub tools are prefixed server_tool when "
             f"hub_mode=True (e.g. codeindex_search). hub_mode={hub_mode}. "
             "Call recipes.capabilities() to discover the full {server: [tools]} map.",
    )


@mcp.tool
def start_here(hub_mode: bool = False) -> dict:
    """Canonical briefing on how to drive this suite: first steps, edit loop, playbooks, hub naming."""
    return _suite_briefing(hub_mode=hub_mode)


@mcp.tool
def suite_guide(hub_mode: bool = False) -> dict:
    """Alias for start_here(): the canonical 'how to drive this suite' briefing."""
    return _suite_briefing(hub_mode=hub_mode)


@mcp.tool
def capabilities() -> dict:
    """Discover the whole suite: import every servers/*/server.py and return a {server: [tool names]}
    map so any agent can see exactly which tools exist. Servers that fail to import are reported under
    `unavailable` rather than crashing the call."""
    import asyncio
    import importlib.util
    import sys
    from pathlib import Path

    servers_dir = Path(__file__).resolve().parent.parent

    def _tools_of(srv) -> list[str]:
        from fastmcp import Client

        async def _go():
            async with Client(srv) as c:
                return sorted(t.name for t in await c.list_tools())
        return asyncio.run(_go())

    out: dict[str, list[str]] = {}
    unavailable: dict[str, str] = {}
    for d in sorted(servers_dir.iterdir()):
        sp = d / "server.py"
        if not sp.exists():
            continue
        name = d.name
        mod_name = f"_recipes_caps_srv_{name.replace('-', '_')}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, sp)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            srv = getattr(mod, "mcp", None)
            if srv is None:
                unavailable[name] = "no `mcp` attribute"
                continue
            out[name] = _tools_of(srv)
        except Exception as e:  # an import error in one server must not break discovery
            unavailable[name] = f"{type(e).__name__}: {e}"

    return ok(servers=out, count=len(out),
              total_tools=sum(len(v) for v in out.values()),
              unavailable=unavailable)


if __name__ == "__main__":
    mcp.run()
