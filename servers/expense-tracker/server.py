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


if __name__ == "__main__":
    mcp.run()
