"""Comprehensive offline tests for the learnprod cluster.

Servers: interview-prep, learn-tracker, task-manager, notes, time-tracker, bookmark-vault,
habit-tracker. Exercises each tool's happy path + key edge cases (validation, no-network).
Uses a fresh temp MCP_DATA_DIR so real ~/.mcp-suite data is never touched. Network-dependent
paths (title fetch / archive) are only tested for graceful offline fallback.
"""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Isolate storage to a throwaway dir, recreated clean every run.
_DATA = Path(tempfile.gettempdir()) / "mcp_learnprod_test_data"
if _DATA.exists():
    shutil.rmtree(_DATA)
_DATA.mkdir(parents=True, exist_ok=True)
os.environ["MCP_DATA_DIR"] = str(_DATA)

ROOT = Path(__file__).resolve().parents[1]
from fastmcp import Client  # noqa: E402


async def one(name, fn):
    sys.path.insert(0, str(ROOT / "servers" / name))
    import server  # noqa
    async with Client(server.mcp) as c:
        await fn(c, server)
    del sys.modules["server"]
    sys.path.pop(0)


def data(res):
    return res.data


async def main():
    # ---------------- interview-prep ----------------
    async def ip(c, s):
        # add_card happy + edge (empty front/back rejected)
        r = await c.call_tool("add_card", {"front": "Q", "back": "A", "deck": "dsa", "topic": "graphs"})
        cid = data(r)["id"]
        assert "error" in data(await c.call_tool("add_card", {"front": "", "back": "A"}))
        # bulk add tolerates junk items (security fix: non-dict skipped)
        bulk = await c.call_tool("add_cards", {"items": [{"front": "x", "back": "y"}, {"front": "", "back": ""}]})
        assert data(bulk)["created"] == 1
        # due_cards filters
        due = await c.call_tool("due_cards", {"deck": "dsa"})
        assert any(d["id"] == cid for d in data(due))
        # review happy + bad grade type (fix: clean error not crash)
        rv = await c.call_tool("review", {"card_id": cid, "grade": 4})
        assert data(rv)["interval_days"] >= 1
        # out-of-range grade is clamped internally, not crashed
        assert data(await c.call_tool("review", {"card_id": cid, "grade": 9}))["interval_days"] >= 1
        assert "error" in data(await c.call_tool("review", {"card_id": 999999, "grade": 3}))
        # FSRS
        f = await c.call_tool("review_fsrs", {"card_id": cid, "rating": "good"})
        assert data(f)["interval_days"] >= 1
        assert "error" in data(await c.call_tool("review_fsrs", {"card_id": cid, "rating": "bogus"}))
        # set_retention happy + out-of-range clamps into [0.70, 0.97]
        assert data(await c.call_tool("set_retention", {"target": 0.85}))["fsrs_retention"] == 0.85
        assert data(await c.call_tool("set_retention", {"target": 5.0}))["fsrs_retention"] == 0.97
        # decks
        assert any(d["deck"] == "dsa" for d in data(await c.call_tool("list_decks", {})))
        await c.call_tool("move_card", {"card_id": cid, "deck": "core"})
        # problems
        p = await c.call_tool("add_problem", {"title": "Two Sum", "difficulty": "easy", "pattern": "hashmap"})
        pid = data(p)["id"]
        assert "error" in data(await c.call_tool("add_problem", {"title": ""}))
        await c.call_tool("track_attempt", {"problem_id": pid, "solved": True, "review_in_days": 3})
        await c.call_tool("tag_problem", {"problem_id": pid, "pattern": "two-pointers"})
        assert data(await c.call_tool("list_problems", {"pattern": "two-pointers"}))
        cov = await c.call_tool("pattern_coverage", {})
        assert data(cov)["coverage_pct"] > 0
        assert isinstance(data(await c.call_tool("problems_by_pattern", {})), list)
        # mock interviews
        m = await c.call_tool("schedule_mock", {"topic": "Graphs", "when_at": "2030-01-01T10:00:00+00:00"})
        mid = data(m)["id"]
        assert "error" in data(await c.call_tool("schedule_mock", {"topic": "", "when_at": "2030-01-01T10:00:00+00:00"}))
        assert data(await c.call_tool("upcoming_mocks", {}))
        cp = await c.call_tool("mock_calendar_payload", {"mock_id": mid})
        assert "Graphs" in data(cp)["summary"]
        # complete_mock happy + out-of-range score clamps (no crash)
        assert data(await c.call_tool("complete_mock", {"mock_id": mid, "score": 95}))["ok"]
        assert "error" in data(await c.call_tool("complete_mock", {"mock_id": 99999, "score": 50}))
        st = data(await c.call_tool("stats", {}))
        assert "pattern_coverage_pct" in st and st["cards_total"] >= 2
        assert isinstance(data(await c.call_tool("review_forecast", {"days": 3})), list)
        print("interview-prep OK")
    await one("interview-prep", ip)

    # ---------------- learn-tracker ----------------
    async def lt(c, s):
        cid = data(await c.call_tool("add_course", {"title": "Rust", "hours": 10, "tags": "systems"}))["id"]
        assert "error" in data(await c.call_tool("add_course", {"title": ""}))
        await c.call_tool("log_progress", {"course_id": cid, "progress_pct": 40})
        assert "error" in data(await c.call_tool("log_progress", {"course_id": 99999, "progress_pct": 10}))
        assert data(await c.call_tool("list_courses", {"tag": "systems"}))
        assert data(await c.call_tool("whats_next", {}))
        st = await c.call_tool("log_study", {"minutes": 50, "course_id": cid})
        assert data(st)["streak"] >= 1
        assert "error" in data(await c.call_tool("log_study", {"minutes": 0}))
        assert "error" in data(await c.call_tool("log_study", {"minutes": 30, "course_id": 99999}))
        assert data(await c.call_tool("current_streak", {}))["streak_days"] >= 1
        assert isinstance(data(await c.call_tool("study_calendar", {"days": 30})), list)
        await c.call_tool("set_goal", {"kind": "minutes", "target": 100, "period": "week"})
        assert "error" in data(await c.call_tool("set_goal", {"kind": "bogus", "target": 1}))
        gp = await c.call_tool("goal_progress", {})
        assert data(gp)[0]["current"] == 50
        # add_resource with fetch_title=False (no network)
        await c.call_tool("add_resource", {"course_id": cid, "url": "http://x", "title": "Doc", "fetch_title": False})
        assert data(await c.call_tool("list_resources", {"course_id": cid}))
        await c.call_tool("mark_for_review", {"course_id": cid, "days": 1})
        assert isinstance(data(await c.call_tool("reviews_due", {})), list)
        assert "weeks" in data(await c.call_tool("generate_plan", {"hours_per_week": 5}))
        assert data(await c.call_tool("report", {"days": 7}))["sessions"] == 1
        # offline fetch helper returns "" not crash; also rejects non-http schemes
        assert s._fetch_title("ftp://nope") == ""
        print("learn-tracker OK")
    await one("learn-tracker", lt)

    # ---------------- task-manager ----------------
    async def tm(c, s):
        assert data(await c.call_tool("parse_due", {"text": "tomorrow 5pm"}))["parsed"]
        assert not data(await c.call_tool("parse_due", {"text": "qwertyzzz"}))["parsed"]
        t = await c.call_tool("add_task", {"title": "Ship", "priority": "high", "due": "tomorrow 9am",
                                           "project": "suite", "importance": True})
        tid = data(t)["id"]
        assert "error" in data(await c.call_tool("add_task", {"title": ""}))
        await c.call_tool("add_subtask", {"parent_id": tid, "title": "sub1"})
        assert "error" in data(await c.call_tool("add_subtask", {"parent_id": 99999, "title": "x"}))
        assert len(data(await c.call_tool("subtasks", {"task_id": tid}))) == 1
        await c.call_tool("update_task", {"task_id": tid, "notes": "go"})
        await c.call_tool("snooze", {"task_id": tid, "until": "in 2 days"})
        assert "error" in data(await c.call_tool("snooze", {"task_id": tid, "until": "zzzz"}))
        rec = await c.call_tool("add_task", {"title": "Standup", "due": "today 9am", "recurrence": "daily"})
        comp = await c.call_tool("complete", {"task_id": data(rec)["id"]})
        assert "next_occurrence" in data(comp)
        await c.call_tool("reopen", {"task_id": tid})
        assert data(await c.call_tool("list_tasks", {"project": "suite"}))
        assert isinstance(data(await c.call_tool("list_due", {})), list)
        assert data(await c.call_tool("list_projects", {}))
        assert "overdue" in data(await c.call_tool("agenda", {"days": 7}))
        await c.call_tool("set_importance", {"task_id": tid, "important": True})
        e = await c.call_tool("eisenhower", {})
        assert len(data(e)["do_now"]) >= 1
        assert data(await c.call_tool("search_tasks", {"query": "Ship"}))
        assert data(await c.call_tool("search_tasks", {"query": ""})) == []
        assert "completed" in data(await c.call_tool("completion_report", {"days": 7}))
        assert "open" in data(await c.call_tool("stats", {}))
        assert isinstance(data(await c.call_tool("overdue", {})), list)
        await c.call_tool("move_to_project", {"task_id": tid, "project": "other"})
        bc = await c.call_tool("bulk_complete", {"task_ids": [tid]})
        assert data(bc)["requested"] == 1
        ics = await c.call_tool("export_ics", {})
        assert "BEGIN:VEVENT" in data(ics)["ics"]
        # write_ics to explicit temp path (path handling)
        wp = str(_DATA / "out" / "tasks.ics")
        assert data(await c.call_tool("write_ics", {"path": wp}))["ok"]
        assert Path(wp).is_file()
        assert data(await c.call_tool("calendar_payload", {"task_id": tid}))["summary"] in ("Ship",)
        await c.call_tool("delete_task", {"task_id": tid})
        print("task-manager OK")
    await one("task-manager", tm)

    # ---------------- notes ----------------
    async def nt(c, s):
        await c.call_tool("new_note", {"title": "MCP", "body": "On [[FastMCP]] and [[SQLite]]. #infra"})
        await c.call_tool("new_note", {"title": "FastMCP", "body": "A #python framework. See [[MCP]]."})
        assert "error" in data(await c.call_tool("new_note", {"title": ""}))
        await c.call_tool("edit_note", {"title": "MCP", "body": "Edited [[FastMCP]] and [[SQLite]] #infra"})
        await c.call_tool("append_note", {"title": "MCP", "text": "more"})
        g = await c.call_tool("graph", {})
        assert {"from": "MCP", "to": "FastMCP"} in data(g)["edges"]
        assert any(e["to"] == "SQLite" for e in data(g)["broken"])
        assert data(await c.call_tool("search", {"query": "framework"}))
        assert any(n["title"] == "MCP" for n in data(await c.call_tool("backlinks", {"title": "FastMCP"})))
        assert data(await c.call_tool("list_notes", {}))
        await c.call_tool("tag_note", {"title": "MCP", "tags": "core,arch"})
        assert data(await c.call_tool("list_tags", {}))
        assert data(await c.call_tool("notes_by_tag", {"tag": "infra"}))
        # daily notes
        dn = await c.call_tool("daily_note", {})
        assert data(dn)["title"]
        await c.call_tool("append_to_daily", {"text": "logged something"})
        # templates
        await c.call_tool("save_template", {"name": "m", "body": "# {{ topic }}"})
        assert data(await c.call_tool("list_templates", {}))
        await c.call_tool("new_from_template", {"title": "Sync", "template": "m", "vars": {"topic": "Q3"}})
        assert "Q3" in data(await c.call_tool("get", {"title": "Sync"}))["body"]
        assert "error" in data(await c.call_tool("get", {"title": "nope"}))
        assert isinstance(data(await c.call_tool("orphans", {})), list)
        assert isinstance(data(await c.call_tool("broken_links", {})), list)
        assert isinstance(data(await c.call_tool("outline", {"title": "Sync"})), list)
        assert isinstance(data(await c.call_tool("link_suggestions", {"title": "MCP"})), list)
        assert isinstance(data(await c.call_tool("find_by_link", {"target": "FastMCP"})), list)
        assert isinstance(data(await c.call_tool("recent", {"days": 7})), list)
        await c.call_tool("rename_note", {"old_title": "Sync", "new_title": "Sync2"})
        await c.call_tool("merge_notes", {"source": "Sync2", "target": "MCP"})
        # exports — security: title with traversal chars must not escape dir
        await c.call_tool("new_note", {"title": "../../evil", "body": "x"})
        exp = await c.call_tool("export_note", {"title": "../../evil", "dir": str(_DATA / "nexport")})
        outpath = Path(data(exp)["path"]).resolve()
        assert str(_DATA / "nexport") in str(outpath.parent), outpath
        assert data(await c.call_tool("export_all", {}))["exported"] >= 3
        gj = await c.call_tool("export_graph_json", {})
        assert Path(data(gj)["path"]).is_file()
        # import from a dir of md files
        idir = _DATA / "md_in"
        idir.mkdir(parents=True, exist_ok=True)
        (idir / "Imported.md").write_text("---\ntitle: x\n---\nbody here", encoding="utf-8")
        assert data(await c.call_tool("import_markdown", {"dir": str(idir)}))["imported"] == 1
        assert "error" in data(await c.call_tool("import_markdown", {"dir": str(_DATA / "no_such_dir")}))
        await c.call_tool("delete_note", {"title": "../../evil"})
        assert "notes" in data(await c.call_tool("stats", {}))
        print("notes OK")
    await one("notes", nt)

    # ---------------- time-tracker ----------------
    async def tt(c, s):
        await c.call_tool("log_block", {"label": "coding", "minutes": 90, "category": "build"})
        await c.call_tool("log_block", {"label": "email", "minutes": 30, "category": "comms"})
        assert "error" in data(await c.call_tool("log_block", {"label": "x", "minutes": 0}))
        assert data(await c.call_tool("weekly_report", {}))["total_hours"] == 2.0
        assert data(await c.call_tool("report", {"days": 7}))["sessions"] == 2
        # start/stop/running
        await c.call_tool("start", {"label": "focus", "category": "build"})
        assert data(await c.call_tool("running", {}))["running"]
        assert "error" in data(await c.call_tool("start", {"label": ""}))
        stp = await c.call_tool("stop", {})
        assert "seconds" in data(stp)
        assert data(await c.call_tool("stop", {})).get("error")
        # recent + edit + delete
        rb = data(await c.call_tool("recent_blocks", {}))
        assert rb
        bid = rb[0]["id"]
        await c.call_tool("edit_block", {"block_id": bid, "label": "renamed"})
        assert "error" in data(await c.call_tool("edit_block", {"block_id": bid}))
        # pomodoro
        await c.call_tool("start_pomodoro", {"label": "deep", "work_min": 25})
        assert data(await c.call_tool("pomodoro_status", {}))["active"]
        assert data(await c.call_tool("pomodoro_done", {}))["take_break_min"] == 5
        assert data(await c.call_tool("pomodoro_count", {"days": 1}))["pomodoros"] >= 1
        # goals
        await c.call_tool("set_time_goal", {"minutes": 60, "category": "build", "period": "day"})
        assert data(await c.call_tool("goal_status", {}))[0]["current_min"] >= 90
        # reports
        assert "date" in data(await c.call_tool("daily_report", {}))
        assert isinstance(data(await c.call_tool("category_breakdown", {})), list)
        assert isinstance(data(await c.call_tool("top_labels", {})), list)
        assert "by_hour" in data(await c.call_tool("productivity_by_hour", {}))
        assert "current" in data(await c.call_tool("focus_streak", {}))
        assert "date" in data(await c.call_tool("today", {}))
        assert "label" in data(await c.call_tool("export_csv", {}))["csv"]
        wc = await c.call_tool("write_csv", {"path": str(_DATA / "tt" / "time.csv")})
        assert Path(data(wc)["path"]).is_file()
        await c.call_tool("delete_block", {"block_id": bid})
        print("time-tracker OK")
    await one("time-tracker", tt)

    # ---------------- bookmark-vault ----------------
    async def bv(c, s):
        await c.call_tool("add_bookmark", {"url": "https://gofastmcp.com", "title": "FastMCP docs",
                                           "tags": "mcp/python,docs", "fetch_title": False})
        assert "error" in data(await c.call_tool("add_bookmark", {"url": ""}))
        r = await c.call_tool("search", {"query": "fastmcp"})
        assert data(r) and "gofastmcp" in data(r)[0]["url"]
        bid = data(r)[0]["id"]
        # content index (simulate archive offline) then content search
        s._index_content(bid, "pytest fixtures and async testing patterns")
        sc = await c.call_tool("search", {"query": "fixtures", "content": True})
        assert any(b["id"] == bid for b in data(sc))
        assert "text" in data(await c.call_tool("get_archive", {"bookmark_id": bid}))
        await c.call_tool("tag", {"bookmark_id": bid, "tags": "mcp/python,reading"})
        assert "error" in data(await c.call_tool("tag", {"bookmark_id": 99999, "tags": "x"}))
        assert data(await c.call_tool("get_bookmark", {"bookmark_id": bid}))
        await c.call_tool("update_bookmark", {"bookmark_id": bid, "notes": "great"})
        assert data(await c.call_tool("list_bookmarks", {"tag": "mcp"}))
        # dedupe
        await c.call_tool("add_bookmark", {"url": "https://www.gofastmcp.com/?utm_source=x", "fetch_title": False})
        assert data(await c.call_tool("find_duplicates", {}))
        assert data(await c.call_tool("dedupe", {"apply": True}))["removable"]
        # tags
        tree = await c.call_tool("tag_tree", {})
        assert "mcp" in data(tree)
        assert isinstance(data(await c.call_tool("bookmarks_by_tag", {"tag": "mcp"})), list)
        assert isinstance(data(await c.call_tool("domains", {})), list)
        assert isinstance(data(await c.call_tool("untagged", {})), list)
        assert isinstance(data(await c.call_tool("recent", {"days": 7})), list)
        assert data(await c.call_tool("random_bookmark", {}))
        # offline _fetch rejects non-http and returns empties (security)
        assert s._fetch("file:///etc/passwd") == ("", "")
        # archive with no fetchable content -> graceful error, not crash
        ar = await c.call_tool("archive", {"url": "file:///etc/passwd"})
        assert "error" in data(ar)
        # import html
        hp = _DATA / "bm.html"
        hp.write_text('<DL><p><DT><A HREF="https://docs.python.org/3/">Py</A>'
                      '<DT><A HREF="https://example.com/">Ex</A>'
                      '<DT><A HREF="javascript:bad()">Bad</A></DL><p>', encoding="utf-8")
        ih = await c.call_tool("import_html", {"path": str(hp)})
        assert data(ih)["imported"] == 2 and data(ih)["skipped"] >= 1
        assert "error" in data(await c.call_tool("import_html", {"path": str(_DATA / "missing.html")}))
        # import json
        jp = _DATA / "bm.json"
        jp.write_text('{"roots":{"bar":{"children":[{"type":"url","url":"https://news.ycombinator.com/","name":"HN"}]}}}',
                      encoding="utf-8")
        ij = await c.call_tool("import_json", {"path": str(jp)})
        assert data(ij)["imported"] == 1
        # exports
        ec = await c.call_tool("export_csv", {"path": str(_DATA / "bv" / "b.csv")})
        assert Path(data(ec)["path"]).is_file() and "url" in data(ec)["csv"]
        eh = await c.call_tool("export_html", {"path": str(_DATA / "bv" / "b.html")})
        assert Path(data(eh)["path"]).is_file()
        assert "bookmarks" in data(await c.call_tool("stats", {}))
        print("bookmark-vault OK")
    await one("bookmark-vault", bv)

    # ---------------- habit-tracker ----------------
    async def ht(c, s):
        await c.call_tool("add_habit", {"name": "read", "cadence": "daily", "target": 1})
        await c.call_tool("add_habit", {"name": "gym", "cadence": "weekly", "target": 3})
        assert "error" in data(await c.call_tool("add_habit", {"name": ""}))
        assert data(await c.call_tool("check_in", {"habit": "read"}))["streak"] >= 1
        assert "error" in data(await c.call_tool("check_in", {"habit": "nonexistent"}))
        await c.call_tool("check_in", {"habit": "gym"})
        sk = await c.call_tool("streak", {"habit": "read"})
        assert data(sk)["streak"] >= 1
        td = await c.call_tool("today", {})
        assert next(h for h in data(td) if h["habit"] == "read")["satisfied"]
        assert not next(h for h in data(td) if h["habit"] == "gym")["satisfied"]
        assert len(data(await c.call_tool("habit_report", {"days": 30}))["habits"]) == 2
        assert data(await c.call_tool("list_habits", {"status": "active"}))
        await c.call_tool("archive_habit", {"habit": "gym"})
        assert not any(h["habit"] == "gym" for h in data(await c.call_tool("today", {})))
        await c.call_tool("delete_habit", {"habit": "read"})
        assert "error" in data(await c.call_tool("delete_habit", {"habit": "read"}))
        print("habit-tracker OK")
    await one("habit-tracker", ht)

    print("\nLEARNPROD CLUSTER OK ✅")


asyncio.run(main())
