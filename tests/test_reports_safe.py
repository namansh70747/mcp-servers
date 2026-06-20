"""Regression tests: report/analytics tools must never crash on empty or edge data
(0-minute/0-target goals, new FSRS cards, None/bad due dates, empty tables, 0-budget)."""
import asyncio
import importlib.util
import os
import tempfile

os.environ["MCP_DATA_DIR"] = tempfile.mkdtemp()

from fastmcp import Client

BASE = "/Users/namansharma/mcp-servers/servers"


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, f"{BASE}/{rel}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _call(mod, name, args):
    async with Client(mod.mcp) as c:
        return (await c.call_tool(name, args)).data


def test_time_tracker_zero_minute_goal():
    tt = _load("tt_test", "time-tracker/server.py")
    # Force a 0-minute goal directly (bypasses set_time_goal clamp; mimics a corrupt on-disk row).
    tt.store.execute("INSERT INTO goals(category,minutes,period,created_at) VALUES('',0,'day',?)",
                     (tt._now().isoformat(),))
    out = asyncio.run(_call(tt, "goal_status", {}))
    assert isinstance(out, list) and out
    assert out[0]["pct"] == 100.0
    # productivity_by_hour on empty data must not crash
    pbh = asyncio.run(_call(tt, "productivity_by_hour", {}))
    assert pbh["peak_hour"] is None


def test_learn_tracker_zero_target_goal_and_empty_report():
    lt = _load("lt_test", "learn-tracker/server.py")
    assert asyncio.run(_call(lt, "report", {}))["sessions"] == 0
    assert asyncio.run(_call(lt, "goal_progress", {})) == []
    lt.store.execute("INSERT INTO goals(kind,target,period,created_at) VALUES('minutes',0,'week',?)",
                     (lt._now(),))
    out = asyncio.run(_call(lt, "goal_progress", {}))
    assert out and out[0]["pct"] == 100.0


def test_interview_prep_new_card_fsrs_and_forecast():
    ip = _load("ip_test", "interview-prep/server.py")
    cid = asyncio.run(_call(ip, "add_card", {"front": "f", "back": "b"}))["id"]
    # New card has stability 0; FSRS update must not ZeroDivide.
    for rating in ("again", "good", "easy", "hard"):
        r = asyncio.run(_call(ip, "review_fsrs", {"card_id": cid, "rating": rating}))
        assert r["interval_days"] >= 1 and r["stability"] >= 0.1
    fc = asyncio.run(_call(ip, "review_forecast", {"days": 7}))
    assert len(fc) == 7


def test_task_manager_none_and_bad_due():
    tm = _load("tm_test", "task-manager/server.py")
    tm.store.execute("INSERT INTO tasks(title,priority,due,status,created_at) VALUES('bad','med','not-a-date','open',?)",
                     (tm._now(),))
    tm.store.execute("INSERT INTO tasks(title,priority,due,status,created_at) VALUES('null','med',NULL,'open',?)",
                     (tm._now(),))
    assert "do_now" in asyncio.run(_call(tm, "eisenhower", {}))
    assert isinstance(asyncio.run(_call(tm, "overdue", {})), list)
    assert "overdue" in asyncio.run(_call(tm, "agenda", {}))
    assert asyncio.run(_call(tm, "parse_due", {"text": "13/5"}))["iso"] is None


def test_habit_tracker_malformed_date():
    ht = _load("ht_test", "habit-tracker/server.py")
    hid = asyncio.run(_call(ht, "add_habit", {"name": "h"}))["id"]
    ht.store.execute("INSERT INTO checkins(habit_id,day,count) VALUES(?,'garbage',1)", (hid,))
    assert asyncio.run(_call(ht, "streak", {"habit": "h"}))["streak"] == 0
    assert isinstance(asyncio.run(_call(ht, "today", {})), list)
    assert "habits" in asyncio.run(_call(ht, "habit_report", {}))


def test_jobtrack_analytics_empty():
    jt = _load("jt_test", "jobtrack/server.py")
    a = asyncio.run(_call(jt, "analytics", {}))
    assert a["total"] == 0 and a["response_rate_pct"] is None


def test_expense_tracker_empty_and_zero_budget():
    et = _load("et_test", "expense-tracker/server.py")
    assert asyncio.run(_call(et, "summary", {}))["total"] == 0
    assert asyncio.run(_call(et, "budget_status", {}))["categories"] == []
    et.store.execute("INSERT INTO budgets(category,monthly,updated_at) VALUES('food',0,?)", (et._now(),))
    et.store.execute("INSERT INTO expenses(amount,category,note,date,created_at) VALUES(5,'food','','2026-06-10',?)",
                     (et._now(),))
    bs = asyncio.run(_call(et, "budget_status", {}))
    assert bs["categories"][0]["percent_used"] == 0.0


def test_expense_tracker_non_finite_amounts_return_err():
    """Non-finite floats (NaN/inf/-inf) used to crash with a SQLite IntegrityError
    (NOT NULL) or silently store a corrupt None amount; they must now return a clean err()."""
    et = _load("et_nf_test", "expense-tracker/server.py")
    for bad in ("NaN", "inf", "-inf"):
        r = asyncio.run(_call(et, "add_expense", {"amount": bad, "category": "food"}))
        assert r["ok"] is False and "finite" in r["error"], (bad, r)
        r = asyncio.run(_call(et, "set_budget", {"category": "food", "monthly": bad}))
        assert r["ok"] is False and "finite" in r["error"], (bad, r)
    # valid finite values still succeed with the unchanged success shape
    ok_add = asyncio.run(_call(et, "add_expense", {"amount": 12.5, "category": "food"}))
    assert ok_add["ok"] is True and ok_add["amount"] == 12.5
    ok_bud = asyncio.run(_call(et, "set_budget", {"category": "food", "monthly": 200}))
    assert ok_bud["ok"] is True and ok_bud["monthly"] == 200.0


if __name__ == "__main__":
    for fn in [test_time_tracker_zero_minute_goal, test_learn_tracker_zero_target_goal_and_empty_report,
               test_interview_prep_new_card_fsrs_and_forecast, test_task_manager_none_and_bad_due,
               test_habit_tracker_malformed_date, test_jobtrack_analytics_empty,
               test_expense_tracker_empty_and_zero_budget,
               test_expense_tracker_non_finite_amounts_return_err]:
        fn()
        print(f"PASS {fn.__name__}")
    print("ALL GREEN")
