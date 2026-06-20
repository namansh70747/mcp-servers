"""Consistent result envelopes so every tool returns the same shape.

    ok(data) / ok(items=[...], total=3)   -> {"ok": True, ...}
    err("not found", code="missing")      -> {"ok": False, "error": "not found", ...}

Adopt incrementally — tools that already return plain dicts keep working.
"""
from __future__ import annotations

from typing import Any


def ok(data: Any = None, **fields: Any) -> dict:
    """Success envelope. Pass a value as `data` and/or extra named fields."""
    out: dict = {"ok": True}
    if data is not None:
        out["data"] = data
    out.update(fields)
    return out


def err(message: str, **fields: Any) -> dict:
    """Failure envelope with a human-readable message and optional extra fields."""
    return {"ok": False, "error": message, **fields}


def not_found(kind: str, given: Any, available: Any = None, hint: str = "") -> dict:
    """Actionable 'not found' for weak/free agents: names what was missing AND what's valid,
    so the model recovers instead of getting stuck.

        not_found("task", 999, available=[1,2,3], hint="use list_tasks()")
        -> {"ok": False, "error": "no task '999' — available: [1, 2, 3]",
            "available": [1,2,3], "hint": "use list_tasks()"}
    """
    msg = f"no {kind} '{given}'"
    fields: dict = {}
    if available is not None:
        sample = list(available)[:10]
        fields["available"] = sample
        if sample:
            msg += f" — available: {sample}"
    if hint:
        fields["hint"] = hint
    return err(msg, **fields)
