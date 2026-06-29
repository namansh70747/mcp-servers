"""Quota / circuit-breaker manager for external email API providers.

Design:
- SQLite `provider_state` table persists cooldowns across server restarts.
- `classify(provider, status, json_body)` detects quota-exhausted / rate-limited responses
  and sets a cooldown_until timestamp.
- `available(provider)` checks whether ANY key for that provider is usable right now.
- `next_key(provider)` returns the first non-cooling key for rotation, or None.
- Multi-key rotation: env vars accept comma-separated lists (HUNTER_API_KEY=k1,k2,k3).
- `record_call(provider, key_id, status, json_body)` should be called after every API call.
- `state()` returns a structured dict suitable for health()/selftest() reporting.
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

from .config import db_path
from .store import BaseStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_state (
    provider       TEXT NOT NULL,
    key_id         TEXT NOT NULL,
    month          TEXT NOT NULL,
    calls          INTEGER DEFAULT 0,
    last_status    TEXT,
    cooldown_until REAL DEFAULT 0,
    PRIMARY KEY (provider, key_id, month)
);
"""

# How long to back off on a rate-limit (seconds) when no Retry-After header is given
_DEFAULT_RATE_LIMIT_BACKOFF = 7200.0   # 2 hours
_MONTH_RESET_BUFFER = 300.0            # 5-min buffer after month flip to ensure reset

# Provider-specific quota signals: (status_code_set, json_key, json_value_fragment)
_QUOTA_SIGNALS: dict[str, list[dict]] = {
    "hunter": [
        {"status": {402, 429}, "key": "errors[0].details", "fragment": ""},
        {"status": {429}, "key": None, "fragment": None},
    ],
    "tomba": [
        {"status": {400, 402, 403, 429}, "key": None, "fragment": None},
    ],
    "reoon": [
        {"status": {200}, "key": "status", "fragment": "quota_exceeded"},
        {"status": {402, 429}, "key": None, "fragment": None},
    ],
    "verifalia": [
        {"status": {402, 429}, "key": None, "fragment": None},
    ],
    "abstract": [
        {"status": {429}, "key": None, "fragment": None},
    ],
    "mailboxlayer": [
        {"status": {104}, "key": "type", "fragment": "quota_reached"},   # their custom code
        {"status": {429}, "key": None, "fragment": None},
    ],
    "skrapp": [
        {"status": {402, 429}, "key": None, "fragment": None},
    ],
    "snov": [
        {"status": {402, 429}, "key": None, "fragment": None},
    ],
}

_RATE_LIMIT_STATUS = {429}
_QUOTA_STATUS = {402}


def _now_ts() -> float:
    return time.time()


def _utc_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _next_month_ts() -> float:
    """Unix timestamp of UTC midnight on the first of next month + buffer."""
    now = datetime.now(timezone.utc)
    if now.month == 12:
        next_m = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_m = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    return next_m.timestamp() + _MONTH_RESET_BUFFER


class QuotaManager:
    """Thread-safe quota tracker and key rotation manager."""

    def __init__(self):
        self._store = BaseStore(db_path("emailverify", "quota.db"), schema=_SCHEMA)
        self._lock = threading.Lock()
        # In-memory cache of loaded keys per provider (from env)
        self._keys: dict[str, list[str]] = {}

    # ------------------------------------------------------------------
    # Key loading

    def _load_keys(self, provider: str) -> list[str]:
        """Load comma-separated keys from env for a provider. Cached per process."""
        if provider in self._keys:
            return self._keys[provider]
        env_map = {
            "hunter":        "HUNTER_API_KEY",
            "hunter_verify": "HUNTER_API_KEY",   # verify shares the one Hunter account/key
            "tomba":         "TOMBA_API_KEY",
            "reoon":         "REOON_API_KEY",
            "verifalia":     "VERIFALIA_USERNAME",
            "abstract":      "ABSTRACT_API_KEY",
            "mailboxlayer":  "MAILBOXLAYER_API_KEY",
            "skrapp":        "SKRAPP_API_KEY",
            "snov":          "SNOV_USER_ID",
        }
        raw = os.environ.get(env_map.get(provider, f"{provider.upper()}_API_KEY"), "")
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        with self._lock:
            self._keys[provider] = keys
        return keys

    # ------------------------------------------------------------------
    # State queries

    def available(self, provider: str) -> bool:
        """True if at least one key for this provider is not currently cooling."""
        keys = self._load_keys(provider)
        if not keys:
            return False
        return self.next_key(provider) is not None

    def next_key(self, provider: str) -> str | None:
        """Return the first non-cooling key for provider, or None if all are cooling."""
        keys = self._load_keys(provider)
        if not keys:
            return None
        now = _now_ts()
        month = _utc_month()
        with self._lock:
            for key_id in keys:
                row = self._store.query_one(
                    "SELECT cooldown_until FROM provider_state "
                    "WHERE provider=? AND key_id=? AND month=?",
                    (provider, key_id, month),
                )
                if row is None or row["cooldown_until"] <= now:
                    return key_id
        return None

    def is_cooling(self, provider: str, key_id: str) -> bool:
        now = _now_ts()
        month = _utc_month()
        row = self._store.query_one(
            "SELECT cooldown_until FROM provider_state "
            "WHERE provider=? AND key_id=? AND month=?",
            (provider, key_id, month),
        )
        return bool(row and row["cooldown_until"] > now)

    def cooldown_until(self, provider: str) -> float:
        """Return the earliest time (unix ts) any key for provider will be available.
        Returns 0 if a key is available right now."""
        keys = self._load_keys(provider)
        if not keys:
            return float("inf")
        now = _now_ts()
        month = _utc_month()
        earliest = float("inf")
        with self._lock:
            for key_id in keys:
                row = self._store.query_one(
                    "SELECT cooldown_until FROM provider_state "
                    "WHERE provider=? AND key_id=? AND month=?",
                    (provider, key_id, month),
                )
                cd = row["cooldown_until"] if row else 0.0
                if cd <= now:
                    return 0.0   # at least one is available right now
                earliest = min(earliest, cd)
        return earliest

    # ------------------------------------------------------------------
    # Generic credit pool (for the browser-extension reveal pool — no env keys, known monthly cap)

    def pool_pick(self, members: list[tuple[str, int]]) -> str | None:
        """Given [(name, monthly_cap), ...] return the first member with month-to-date usage below
        its cap and not cooling, else None. Rotates the free reveal pool like API keys."""
        month = _utc_month()
        now = _now_ts()
        with self._lock:
            for name, cap in members:
                self._ensure_row(name, "pool", month)
                row = self._store.query_one(
                    "SELECT calls, cooldown_until FROM provider_state "
                    "WHERE provider=? AND key_id=? AND month=?", (name, "pool", month))
                calls = (row["calls"] if row else 0) or 0
                cd = (row["cooldown_until"] if row else 0.0) or 0.0
                if cd <= now and calls < cap:
                    return name
        return None

    def record_pool_use(self, name: str, cap: int | None = None) -> None:
        """Record one reveal-credit consumption; cool until next month once the cap is hit."""
        month = _utc_month()
        self._ensure_row(name, "pool", month)
        with self._lock:
            self._store.execute(
                "UPDATE provider_state SET calls=calls+1, last_status='reveal' "
                "WHERE provider=? AND key_id=? AND month=?", (name, "pool", month))
            if cap is not None:
                row = self._store.query_one(
                    "SELECT calls FROM provider_state WHERE provider=? AND key_id=? AND month=?",
                    (name, "pool", month))
                if row and (row["calls"] or 0) >= cap:
                    self._store.execute(
                        "UPDATE provider_state SET cooldown_until=? "
                        "WHERE provider=? AND key_id=? AND month=?",
                        (_next_month_ts(), name, "pool", month))

    # ------------------------------------------------------------------
    # Recording calls + detecting quota/rate exhaustion

    def record_call(self, provider: str, key_id: str, status: int,
                    json_body: Any = None, retry_after: float | None = None) -> str:
        """Record an API call and set cooldown if quota/rate-limited.

        Returns: "ok" | "quota" | "rate_limit"
        """
        month = _utc_month()
        self._ensure_row(provider, key_id, month)
        reason = self.classify(provider, status, json_body)

        with self._lock:
            if reason == "quota":
                # Hard monthly quota → cool until next month
                cd = _next_month_ts()
                self._store.execute(
                    "UPDATE provider_state SET calls=calls+1, last_status=?, cooldown_until=? "
                    "WHERE provider=? AND key_id=? AND month=?",
                    (str(status), cd, provider, key_id, month),
                )
            elif reason == "rate_limit":
                backoff = retry_after if retry_after else _DEFAULT_RATE_LIMIT_BACKOFF
                cd = _now_ts() + backoff
                self._store.execute(
                    "UPDATE provider_state SET calls=calls+1, last_status=?, cooldown_until=? "
                    "WHERE provider=? AND key_id=? AND month=?",
                    (str(status), cd, provider, key_id, month),
                )
            else:
                self._store.execute(
                    "UPDATE provider_state SET calls=calls+1, last_status=? "
                    "WHERE provider=? AND key_id=? AND month=?",
                    (str(status), provider, key_id, month),
                )
        return reason

    def classify(self, provider: str, status: int, json_body: Any = None) -> str:
        """Return "quota" | "rate_limit" | "ok" based on the response."""
        signals = _QUOTA_SIGNALS.get(provider, [])
        for sig in signals:
            if status in sig.get("status", set()):
                jk = sig.get("key")
                jf = sig.get("fragment")
                if jk is None and jf is None:
                    # Status alone is sufficient signal
                    if status in _QUOTA_STATUS:
                        return "quota"
                    if status in _RATE_LIMIT_STATUS:
                        return "rate_limit"
                    # Other 4xx for this provider → treat as quota
                    return "quota"
                # Check json body
                val = _nested_get(json_body, jk) if jk and json_body else None
                if val is not None and (not jf or jf in str(val).lower()):
                    return "quota"
        if status in _QUOTA_STATUS:
            return "quota"
        if status in _RATE_LIMIT_STATUS:
            return "rate_limit"
        return "ok"

    # ------------------------------------------------------------------
    # Health / selftest feed

    def state(self) -> dict:
        """Return provider state dict for health()/selftest() reporting."""
        now = _now_ts()
        month = _utc_month()
        rows = self._store.query(
            "SELECT provider, key_id, calls, last_status, cooldown_until "
            "FROM provider_state WHERE month=?",
            (month,),
        )
        out: dict[str, Any] = {}
        for r in rows:
            p = r["provider"]
            if p not in out:
                out[p] = {"keys": [], "any_available": False}
            cd = r["cooldown_until"]
            cooling = cd > now
            out[p]["keys"].append({
                "key_id": r["key_id"][:6] + "…",
                "calls_this_month": r["calls"],
                "cooling": cooling,
                "cooldown_until": datetime.fromtimestamp(cd, timezone.utc).isoformat() if cooling else None,
            })
            if not cooling:
                out[p]["any_available"] = True

        # Add configured providers that have no calls yet
        for provider in _QUOTA_SIGNALS:
            keys = self._load_keys(provider)
            if keys and provider not in out:
                out[provider] = {"keys": [], "any_available": True}
        return out

    # ------------------------------------------------------------------
    # Internal helpers

    def _ensure_row(self, provider: str, key_id: str, month: str) -> None:
        self._store.execute(
            "INSERT OR IGNORE INTO provider_state (provider, key_id, month) VALUES (?,?,?)",
            (provider, key_id, month),
        )


def _nested_get(obj: Any, key_path: str) -> Any:
    """Safely navigate a dotted key path like 'errors[0].details' into a dict."""
    if not obj or not key_path:
        return None
    parts = re.split(r"\.", key_path) if "." in key_path else [key_path]
    cur = obj
    for part in parts:
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                idx = int(re.search(r"\d+", part).group())
                cur = cur[idx]
            except Exception:
                return None
        else:
            return None
    return cur



# Module-level singleton — shared by both email-finder and emailcheck
QUOTA = QuotaManager()
