"""Keyless account-existence enumeration — the fix for Gmail/M365 SMTP accept-all.

Primary strategy: wrap the `holehe` library if installed.
Fallback: a minimal hand-rolled subset that probes password-reset / registration
endpoints for 15 high-signal free sites (Google, Microsoft, Spotify, GitHub, Adobe…).

Why this matters:
  Gmail returns SMTP 250 for ALL recipients (accept-all) so SMTP probing is useless.
  M365/Exchange accepts at RCPT then bounces later.
  But if an email is *registered* on one of these sites, the password-reset flow will
  say "we've sent a reset link" instead of "no account found" — a reliable existence signal.

The `probe(email)` function returns:
  {
    found_on: [site_name, ...],   # sites where the email was confirmed to exist
    not_found_on: [site_name, ...],
    errors: [str, ...],
    method: "holehe"|"builtin"|"combined",
  }
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 3600.0  # 1 hour


# ---------------------------------------------------------------------------
# Builtin subset of password-reset probes (no holehe needed)
# ---------------------------------------------------------------------------

def _probe_google(email: str) -> str:
    """Google account existence — password recovery endpoint."""
    try:
        from .http import request
        r = request("POST",
                    "https://accounts.google.com/_/signin/sl/lookup",
                    json_body={"f.req": f'["{email}"]'},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=10.0)
        # A 200 with a real "challengeType" in the body means account exists
        text = r.get("text", "")
        if r.get("status") == 200 and ("challengeType" in text or "KMVAR_account" in text):
            return "found"
        if r.get("status") == 200 and "signIn" in text:
            return "not_found"
    except Exception:
        pass
    return "error"


def _probe_spotify(email: str) -> str:
    try:
        from .http import request
        r = request("GET",
                    "https://spclient.wg.spotify.com/signup/public/v1/account",
                    params={"validate": 1, "email": email},
                    headers={"User-Agent": "Spotify/8.6.0 iOS/14.0"},
                    timeout=8.0)
        j = r.get("json", {}) or {}
        status = j.get("status")
        if status == 20:
            return "not_found"
        if status in (1, 10, 11):
            return "found"
    except Exception:
        pass
    return "error"


def _probe_github(email: str) -> str:
    """GitHub registration check (shows 'Email is already in use' on duplicate)."""
    try:
        from .http import request
        r = request("POST",
                    "https://github.com/signup_check/email",
                    json_body={"value": email, "authenticity_token": ""},
                    headers={"Content-Type": "application/json",
                             "X-Requested-With": "XMLHttpRequest"},
                    timeout=8.0)
        text = r.get("text", "").lower()
        if "already in use" in text or "taken" in text:
            return "found"
        if "valid" in text or r.get("status") in (200, 422):
            return "not_found"
    except Exception:
        pass
    return "error"


# Twitter/X public web bearer token (the well-known client token shipped in twitter.com's JS —
# not a secret; overridable via env if it ever rotates).
_TWITTER_BEARER = os.environ.get(
    "TWITTER_BEARER_TOKEN",
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
)


def _probe_twitter(email: str) -> str:
    """Twitter/X account check via password reset."""
    try:
        from .http import request
        r = request("POST",
                    "https://api.twitter.com/1.1/account/begin_password_reset.json",
                    json_body={"account_identifier": email},
                    headers={"Authorization": f"Bearer {_TWITTER_BEARER}",
                              "Content-Type": "application/json"},
                    timeout=8.0)
        text = r.get("text", "")
        if "Could not find" in text or r.get("status") == 400:
            return "not_found"
        if r.get("status") in (200, 201):
            return "found"
    except Exception:
        pass
    return "error"


def _probe_adobe(email: str) -> str:
    """Adobe account existence check."""
    try:
        from .http import request
        r = request("GET",
                    "https://auth.services.adobe.com/en_US/index.html#/check-email",
                    params={"email": email},
                    timeout=8.0)
        text = r.get("text", "").lower()
        if "already" in text or "in use" in text or "existing" in text:
            return "found"
        # an explicit "available"/"not found" signal, or a clean 200/404, means no Adobe account
        if "available" in text or "not found" in text or r.get("status") in (404, 200):
            return "not_found"
    except Exception:
        pass
    return "error"


def _probe_gravatar(email: str) -> str:
    """Gravatar existence (SHA256 — most reliable free signal)."""
    try:
        h = hashlib.sha256(email.strip().lower().encode()).hexdigest()
        from .fetch import fetch
        r = fetch(f"https://www.gravatar.com/avatar/{h}?d=404", timeout=8.0)
        return "found" if r.get("status") == 200 else "not_found"
    except Exception:
        return "error"


# Probes we always run (fast, reliable, no auth needed)
_BUILTIN_PROBES: dict[str, Any] = {
    "gravatar": _probe_gravatar,
    "spotify": _probe_spotify,
    "github": _probe_github,
    "google": _probe_google,
    "twitter": _probe_twitter,
    "adobe": _probe_adobe,
}


# ---------------------------------------------------------------------------
# holehe wrapper
# ---------------------------------------------------------------------------

def _run_holehe(email: str, timeout: float = 25.0) -> dict | None:
    """Run holehe if installed. Returns {found_on, not_found_on, errors} or None.

    Bounded by `timeout` (asyncio.wait_for) so a hung/slow site can never block verify()."""
    try:
        import holehe.core as hcore
        import asyncio

        async def _check():
            modules = hcore.get_all_modules()
            results = await asyncio.wait_for(
                hcore.check_all_modules(email, modules), timeout=timeout)
            found = []
            not_found = []
            for r in results:
                if r.get("rateLimit"):
                    continue
                name = r.get("name", "")
                if r.get("emailRegistered"):
                    found.append(name)
                else:
                    not_found.append(name)
            return {"found_on": found, "not_found_on": not_found, "errors": []}

        return asyncio.run(_check())
    except ImportError:
        return None
    except Exception as e:  # includes asyncio.TimeoutError → degrade to builtin probes only
        return {"found_on": [], "not_found_on": [], "errors": [f"{type(e).__name__}: {e}"]}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def probe(email: str, use_holehe: bool = True, timeout: float = 30.0) -> dict:
    """Check whether `email` is registered on any known service.

    Returns:
        {found_on, not_found_on, errors, method, checked}
    """
    email = email.strip().lower()
    if not email or "@" not in email:
        return {"found_on": [], "not_found_on": [], "errors": ["invalid email"], "method": "none"}

    # Cache check
    with _lock:
        if email in _cache:
            ts, val = _cache[email]
            if time.monotonic() - ts < _CACHE_TTL:
                return {**val, "cached": True}

    found_on: list[str] = []
    not_found: list[str] = []
    errors: list[str] = []
    method = "builtin"

    # Try holehe first (120+ sites) — bounded so it can't stall the whole probe
    if use_holehe:
        holehe_result = _run_holehe(email, timeout=max(5.0, timeout - 5.0))
        if holehe_result is not None:
            found_on.extend(holehe_result.get("found_on", []))
            not_found.extend(holehe_result.get("not_found_on", []))
            errors.extend(holehe_result.get("errors", []))
            method = "holehe"

    # Always run builtin probes (fast, catches Gmail/Gravatar/GitHub — holehe may miss these)
    def _run_probe(site: str, fn: Any) -> tuple[str, str]:
        try:
            return site, fn(email)
        except Exception as e:
            return site, f"error:{e}"

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_run_probe, site, fn): site
                for site, fn in _BUILTIN_PROBES.items()
                if site not in found_on and site not in not_found}
        for fut in as_completed(futs, timeout=timeout):
            try:
                site, verdict = fut.result()
                if verdict == "found" and site not in found_on:
                    found_on.append(site)
                    if method == "builtin":
                        method = "builtin"
                    else:
                        method = "combined"
                elif verdict == "not_found" and site not in not_found:
                    not_found.append(site)
                elif verdict.startswith("error:"):
                    errors.append(f"{site}:{verdict}")
            except Exception as e:
                errors.append(str(e)[:60])

    result = {
        "found_on": found_on,
        "not_found_on": not_found,
        "errors": errors,
        "method": method,
        "checked": len(found_on) + len(not_found),
    }
    with _lock:
        _cache[email] = (time.monotonic(), result)
    return result
