"""expense-tracker — a local, SQLite-backed personal expense log with per-category monthly budgets.

Log expenses, list/filter by month or category, get monthly summaries, set budgets, and check how
each category is tracking against its budget. Fully offline."""
from __future__ import annotations

import math
from datetime import date, datetime, timezone

from mcp_base import BaseStore, db_path, err, make_server, ok

mcp = make_server(
    "expense-tracker",
    instructions=("Local expense log + monthly budgets (SQLite). add_expense, list_expenses, summary, "
                  "set_budget, budget_status, delete_expense."),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS expenses(
  id INTEGER PRIMARY KEY, amount REAL NOT NULL, category TEXT NOT NULL,
  note TEXT DEFAULT '', date TEXT NOT NULL, created_at TEXT
);
CREATE TABLE IF NOT EXISTS budgets(
  category TEXT PRIMARY KEY, monthly REAL NOT NULL, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_exp_date ON expenses(date);
CREATE INDEX IF NOT EXISTS idx_exp_cat ON expenses(category);
"""
store = BaseStore(db_path("expense-tracker"), schema=SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return date.today().isoformat()


def _norm_date(d: str) -> str | None:
    """Accept YYYY-MM-DD; return normalized or None if invalid."""
    d = (d or "").strip()
    if not d:
        return _today()
    try:
        return datetime.strptime(d, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def _norm_month(m: str) -> str:
    """Return a YYYY-MM month key. Empty -> current month."""
    m = (m or "").strip()
    if not m:
        return _today()[:7]
    return m[:7]


@mcp.tool
def add_expense(amount: float, category: str, note: str = "", date: str = "") -> dict:
    """Record an expense. amount (>0), category, optional note, optional date (YYYY-MM-DD, defaults today)."""
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return err("amount must be a number")
    if not math.isfinite(amount):
        return err("amount must be a finite number")
    if amount <= 0:
        return err("amount must be positive")
    category = (category or "").strip().lower()
    if not category:
        return err("category is required")
    d = _norm_date(date)
    if d is None:
        return err("date must be YYYY-MM-DD")
    eid = store.execute(
        "INSERT INTO expenses(amount,category,note,date,created_at) VALUES(?,?,?,?,?)",
        (round(amount, 2), category, (note or "").strip(), d, _now()))
    return ok(id=eid, amount=round(amount, 2), category=category, date=d)


@mcp.tool
def list_expenses(month: str = "", category: str = "") -> list[dict]:
    """List expenses, optionally filtered by month (YYYY-MM) and/or category. Newest first."""
    where, params = [], []
    if month.strip():
        where.append("date LIKE ?")
        params.append(_norm_month(month) + "%")
    if category.strip():
        where.append("category=?")
        params.append(category.strip().lower())
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return store.query(
        f"SELECT id,amount,category,note,date FROM expenses{clause} ORDER BY date DESC, id DESC",
        tuple(params))


@mcp.tool
def summary(month: str = "") -> dict:
    """Monthly summary: total spend and per-category breakdown for a month (default current)."""
    mk = _norm_month(month)
    rows = store.query(
        "SELECT category, ROUND(SUM(amount),2) AS total, COUNT(*) AS count "
        "FROM expenses WHERE date LIKE ? GROUP BY category ORDER BY total DESC",
        (mk + "%",))
    total = round(sum(r["total"] for r in rows), 2)
    return ok(month=mk, total=total, by_category=rows, count=sum(r["count"] for r in rows))


@mcp.tool
def set_budget(category: str, monthly: float) -> dict:
    """Set (or update) a monthly budget for a category."""
    category = (category or "").strip().lower()
    if not category:
        return err("category is required")
    try:
        monthly = float(monthly)
    except (TypeError, ValueError):
        return err("monthly must be a number")
    if not math.isfinite(monthly):
        return err("monthly must be a finite number")
    if monthly < 0:
        return err("monthly must be >= 0")
    store.execute(
        "INSERT INTO budgets(category,monthly,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(category) DO UPDATE SET monthly=excluded.monthly, updated_at=excluded.updated_at",
        (category, round(monthly, 2), _now()))
    return ok(category=category, monthly=round(monthly, 2))


@mcp.tool
def budget_status(month: str = "") -> dict:
    """Show each budgeted category's spend vs. budget for a month (default current), with remaining
    amount and percent used. Includes over-budget flags."""
    mk = _norm_month(month)
    budgets = store.query("SELECT category, monthly FROM budgets ORDER BY category")
    spent_rows = store.query(
        "SELECT category, ROUND(SUM(amount),2) AS spent FROM expenses WHERE date LIKE ? GROUP BY category",
        (mk + "%",))
    spent = {r["category"]: r["spent"] for r in spent_rows}
    out = []
    for b in budgets:
        s = spent.get(b["category"], 0.0)
        remaining = round(b["monthly"] - s, 2)
        pct = round((s / b["monthly"] * 100), 1) if b["monthly"] > 0 else 0.0
        out.append({"category": b["category"], "budget": b["monthly"], "spent": round(s, 2),
                    "remaining": remaining, "percent_used": pct, "over_budget": s > b["monthly"]})
    # categories with spend but no budget set
    unbudgeted = [{"category": c, "spent": round(v, 2)} for c, v in spent.items()
                  if c not in {b["category"] for b in budgets}]
    return ok(month=mk, categories=out, unbudgeted=unbudgeted)


@mcp.tool
def delete_expense(id: int) -> dict:
    """Delete an expense by id."""
    if not store.query_one("SELECT id FROM expenses WHERE id=?", (id,)):
        return err("expense not found", id=id)
    store.execute("DELETE FROM expenses WHERE id=?", (id,))
    return ok(id=id, deleted=True)


# ---------------------------------------------------------------------------
# Read-only analytics: recurring-charge detection + month-end forecasting.
# Both are pure SQL reads over the existing expenses table and never raise.
# ---------------------------------------------------------------------------

def _parse_iso(d: str) -> date | None:
    try:
        return datetime.strptime((d or "")[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _norm_note(note: str) -> str:
    """Collapse a note to a stable signature for grouping recurring charges."""
    return " ".join((note or "").strip().lower().split())


_INTERVAL_BUCKETS = (
    # (label, min_days, max_days, canonical_days)
    ("daily", 1, 2, 1),
    ("weekly", 6, 8, 7),
    ("biweekly", 13, 16, 14),
    ("monthly", 27, 34, 30),
    ("bimonthly", 55, 65, 61),
    ("quarterly", 85, 96, 91),
    ("semiannual", 178, 190, 182),
    ("annual", 358, 372, 365),
)


def _classify_interval(median_days: float) -> tuple[str, int]:
    """Map a median gap (in days) to a human cadence label + canonical period length."""
    for label, lo, hi, canon in _INTERVAL_BUCKETS:
        if lo <= median_days <= hi:
            return label, canon
    return "irregular", max(1, int(round(median_days)))


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _add_months(d: date, n: int) -> date:
    """Add n calendar months to d, clamping the day to the target month's length."""
    m0 = d.month - 1 + n
    y = d.year + m0 // 12
    m = m0 % 12 + 1
    # last valid day of target month
    if m == 12:
        last = 31
    else:
        last = (date(y + (m // 12), (m % 12) + 1, 1) - date.resolution).day
    return date(y, m, min(d.day, last))


@mcp.tool
def detect_recurring(min_occurrences: int = 3, lookback_months: int = 12,
                     amount_tolerance: float = 0.15) -> dict:
    """Find likely recurring charges (subscriptions, rent, etc.) in the expense log.

    Groups expenses by category + note signature, finds those that repeat on a regular
    cadence (weekly/monthly/etc.), and reports the typical amount, interval, and the next
    expected date. Read-only — never modifies data, never raises.

    min_occurrences: minimum number of charges to qualify (default 3, floored at 2).
    lookback_months: how far back to scan (default 12, 0 or less = all history).
    amount_tolerance: max relative spread of amounts to count as the "same" charge (default 0.15).
    """
    try:
        min_occ = max(2, int(min_occurrences))
    except (TypeError, ValueError):
        min_occ = 3
    try:
        lookback = int(lookback_months)
    except (TypeError, ValueError):
        lookback = 12
    try:
        tol = float(amount_tolerance)
        if not math.isfinite(tol) or tol < 0:
            tol = 0.15
    except (TypeError, ValueError):
        tol = 0.15

    params: tuple = ()
    where = ""
    if lookback > 0:
        cutoff = _add_months(date.today(), -lookback).isoformat()
        where = " WHERE date >= ?"
        params = (cutoff,)
    rows = store.query(
        f"SELECT id, amount, category, note, date FROM expenses{where} ORDER BY date ASC, id ASC",
        params)

    # bucket by (category, note signature)
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        d = _parse_iso(r.get("date", ""))
        if d is None:
            continue
        key = (r.get("category", ""), _norm_note(r.get("note", "")))
        groups.setdefault(key, []).append({"amount": float(r.get("amount") or 0.0), "d": d})

    today = date.today()
    found = []
    for (category, note_sig), items in groups.items():
        # distinct dates only — multiple charges same day count once for cadence
        by_date: dict[date, float] = {}
        for it in items:
            by_date.setdefault(it["d"], 0.0)
            by_date[it["d"]] += it["amount"]
        dates = sorted(by_date)
        if len(dates) < min_occ:
            continue
        amounts = [by_date[d] for d in dates]
        med_amt = _median(amounts)
        if med_amt <= 0:
            continue
        spread = (max(amounts) - min(amounts)) / med_amt if med_amt else 0.0
        if spread > tol:
            continue  # amounts too inconsistent to be the same recurring charge
        gaps = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
        gaps = [g for g in gaps if g > 0]
        if not gaps:
            continue
        med_gap = _median([float(g) for g in gaps])
        label, canon = _classify_interval(med_gap)
        last_seen = dates[-1]
        if label == "monthly":
            next_expected = _add_months(last_seen, 1)
        elif label == "bimonthly":
            next_expected = _add_months(last_seen, 2)
        elif label == "quarterly":
            next_expected = _add_months(last_seen, 3)
        elif label == "semiannual":
            next_expected = _add_months(last_seen, 6)
        elif label == "annual":
            next_expected = _add_months(last_seen, 12)
        else:
            from datetime import timedelta
            next_expected = last_seen + timedelta(days=canon)
        found.append({
            "category": category,
            "note": note_sig,
            "occurrences": len(dates),
            "typical_amount": round(med_amt, 2),
            "amount_min": round(min(amounts), 2),
            "amount_max": round(max(amounts), 2),
            "interval": label,
            "interval_days": round(med_gap, 1),
            "first_seen": dates[0].isoformat(),
            "last_seen": last_seen.isoformat(),
            "next_expected": next_expected.isoformat(),
            "monthly_equivalent": round(med_amt * (30.0 / canon), 2) if canon > 0 else round(med_amt, 2),
            "active": (today - last_seen).days <= canon * 2,
        })

    # most material recurring spend first
    found.sort(key=lambda x: x["monthly_equivalent"], reverse=True)
    monthly_total = round(sum(f["monthly_equivalent"] for f in found if f["active"]), 2)
    return ok(recurring=found, count=len(found),
              estimated_monthly_recurring=monthly_total,
              params={"min_occurrences": min_occ, "lookback_months": lookback,
                      "amount_tolerance": tol})


@mcp.tool
def forecast_month(month: str = "") -> dict:
    """Project month-end total spend from spend-so-far + daily run-rate + recurring charges due later.

    For the current month it blends the elapsed-days run-rate with any detected recurring charges
    expected before month-end (deduping against amounts that have likely already posted). For a past
    month it just reports the actual total; for a future month it projects from recurring + history.
    Read-only — never raises.
    """
    mk = _norm_month(month)
    # validate the YYYY-MM key
    try:
        y, m = mk.split("-")
        y, m = int(y), int(m)
        if not (1 <= m <= 12):
            raise ValueError
        first = date(y, m, 1)
    except (ValueError, AttributeError):
        return err("month must be YYYY-MM")

    last_day = (_add_months(first, 1) - date.resolution)
    days_in_month = last_day.day
    today = date.today()

    rows = store.query(
        "SELECT amount, date FROM expenses WHERE date LIKE ? ORDER BY date ASC",
        (mk + "%",))
    spent = round(sum(float(r.get("amount") or 0.0) for r in rows), 2)

    # determine elapsed days within this month
    if today < first:
        status = "future"
        days_elapsed = 0
    elif today > last_day:
        status = "complete"
        days_elapsed = days_in_month
    else:
        status = "in_progress"
        days_elapsed = today.day

    days_remaining = max(0, days_in_month - days_elapsed)

    # daily run-rate from actual spend so far this month
    daily_rate = round(spent / days_elapsed, 2) if days_elapsed > 0 else 0.0
    runrate_remaining = round(daily_rate * days_remaining, 2)

    # add recurring charges expected in the remaining window that look unpaid this month
    rec = detect_recurring()
    recurring_due = []
    recurring_extra = 0.0
    if rec.get("ok"):
        for item in rec.get("recurring", []):
            if not item.get("active"):
                continue
            nxt = _parse_iso(item.get("next_expected", ""))
            if nxt is None:
                continue
            # only count a recurring hit that falls in this month, after today, and not yet posted
            in_month = (nxt.year == y and nxt.month == m)
            after_now = nxt > today if status == "in_progress" else (status == "future")
            if not (in_month and after_now):
                continue
            amt = float(item.get("typical_amount") or 0.0)
            recurring_due.append({"category": item["category"], "note": item["note"],
                                  "amount": round(amt, 2), "expected": nxt.isoformat(),
                                  "interval": item["interval"]})
            recurring_extra += amt
    recurring_extra = round(recurring_extra, 2)

    if status == "complete":
        projected = spent
    elif status == "future":
        # no actuals yet — project from recurring due this month only (conservative)
        projected = recurring_extra
    else:
        # blend run-rate with recurring; recurring already-posted is part of `spent`,
        # so only ADD the not-yet-seen recurring charges on top of the run-rate.
        projected = round(spent + runrate_remaining + recurring_extra, 2)

    return ok(
        month=mk, status=status,
        spent_so_far=spent,
        days_elapsed=days_elapsed, days_remaining=days_remaining,
        days_in_month=days_in_month,
        daily_run_rate=daily_rate,
        runrate_projection=runrate_remaining,
        recurring_due=recurring_due,
        recurring_remaining=recurring_extra,
        projected_total=round(projected, 2),
    )


if __name__ == "__main__":
    mcp.run()
