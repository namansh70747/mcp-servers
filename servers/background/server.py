"""background — fire any task into the background and keep working; nothing takes over your screen.

`run("send 'running late' to Alice on WhatsApp")` spawns a DETACHED, headless Claude Code run
(`claude -p …`) with your whole MCP suite available, returns a job_id immediately, and does the work
in a concurrency-capped worker pool — so you can fire many tasks at once and stay in your foreground
app. Claude-in-the-subprocess picks the background-safe channel (headless browser for web apps,
AppleScript/API for iMessage/Mail/Gmail, the whatsapp server, …). Poll with job_status / list_jobs.

This is the "and that too with any app" engine — no per-app connector needed. It reuses the exact
`claude -p` pattern from automation/run_outreach.sh, wrapped in the shared Jobs worker pool.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from mcp_base import Jobs, data_dir, err, get_env, get_env_bool, get_env_int, make_server, ok, repo_root

mcp = make_server(
    "background",
    instructions=("Run ANY task in the background without taking over the screen. run(task) spawns a "
                  "detached headless `claude -p` with the full suite and returns a job_id instantly; "
                  "fire several at once (concurrency-capped). job_status(id)/list_jobs()/result(id)/"
                  "cancel_job(id). Use this so a long or multi-app task doesn't block your session."),
)

ROOT = repo_root()
OUT_DIR = data_dir("background") / "out"
OUT_DIR.mkdir(parents=True, exist_ok=True)
MAX_CONCURRENT = max(1, get_env_int("BACKGROUND_MAX_CONCURRENT", 4) or 4)
DEFAULT_JOB_TIMEOUT = max(30, get_env_int("BACKGROUND_TIMEOUT", 1800) or 1800)
YOLO = get_env_bool("BACKGROUND_YOLO", False)
INLINE_WAIT = max(2, get_env_int("BACKGROUND_INLINE_WAIT", 8) or 8)
MAX_TASK = 20000

JOBS = Jobs("background", max_concurrent=MAX_CONCURRENT, inline_wait=INLINE_WAIT)


def _claude_bin() -> str | None:
    cand = get_env("CLAUDE_BIN") or shutil.which("claude")
    if cand and Path(cand).exists():
        return cand
    for p in (Path.home() / ".claude/local/claude", Path("/opt/homebrew/bin/claude"),
              Path("/usr/local/bin/claude")):
        if p.exists():
            return str(p)
    return cand  # may be None


def _worker_for(task: str, timeout: int):
    def worker(job):
        exe = _claude_bin()
        if not exe:
            JOBS.finish(job["id"], ok_=False, error="`claude` CLI not found on PATH",
                        hint="install Claude Code or set CLAUDE_BIN in .env")
            return
        cmd = [exe, "-p", task]
        cmd += ["--dangerously-skip-permissions"] if YOLO else ["--permission-mode", "acceptEdits"]
        logf = OUT_DIR / f"{job['id']}.log"
        try:
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, stdin=subprocess.DEVNULL)
        except Exception as e:  # noqa: BLE001
            JOBS.finish(job["id"], ok_=False, error=f"could not launch claude: {e}")
            return
        JOBS.track_proc(job["id"], proc)
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out = (proc.communicate()[0] or "")
            JOBS.untrack_proc(job["id"])
            try:
                logf.write_text(out)
            except Exception:  # noqa: BLE001
                pass
            JOBS.finish(job["id"], ok_=False, error=f"task timed out after {timeout}s",
                        log=str(logf))
            return
        JOBS.untrack_proc(job["id"])
        out = out or ""
        try:
            logf.write_text(out)
        except Exception:  # noqa: BLE001
            pass
        if proc.returncode == 0:
            JOBS.finish(job["id"], ok_=True, summary=out.strip()[-4000:], log=str(logf), exit_code=0)
        elif JOBS.load(job["id"]) and JOBS.load(job["id"]).get("status") == "cancelled":
            pass
        else:
            JOBS.finish(job["id"], ok_=False,
                        error=f"claude exited {proc.returncode}: {out.strip()[-1200:]}", log=str(logf))
    return worker


@mcp.tool
def run(task: str, label: str = "", timeout: int = 0) -> dict:
    """Run a natural-language task in the BACKGROUND via a detached headless `claude -p` (full suite).
    Returns a job_id immediately (or the summary inline if it finishes fast). Fire several at once."""
    task = (task or "").strip()
    if not task:
        return err("task is required, e.g. \"send 'running late' to Alice on WhatsApp\"")
    if len(task) > MAX_TASK:
        return err(f"task too long (>{MAX_TASK} chars)")
    if not _claude_bin():
        return err("`claude` CLI not found on PATH", hint="install Claude Code or set CLAUDE_BIN in .env")
    t = int(timeout) if timeout and int(timeout) > 0 else DEFAULT_JOB_TIMEOUT
    kind = re.sub(r"[^a-z0-9]+", "_", (label or task).lower())[:24].strip("_") or "task"
    return JOBS.run_or_job(kind, _worker_for(task, t), inline_wait=INLINE_WAIT, label=label or task[:80])


@mcp.tool
def job_status(job_id: str) -> dict:
    """Status/result of a background task (queued/running/done/error/cancelled)."""
    return JOBS.status(job_id)


@mcp.tool
def list_jobs(limit: int = 20) -> dict:
    """Recent background tasks (running first)."""
    return JOBS.listing(limit)


@mcp.tool
def result(job_id: str) -> dict:
    """Full captured output/log of a finished background task."""
    job = JOBS.load(job_id)
    if not job:
        return err(f"no job '{job_id}'")
    logf = job.get("log")
    text = ""
    if logf and Path(logf).exists():
        try:
            text = Path(logf).read_text()[-20000:]
        except Exception:  # noqa: BLE001
            text = ""
    return ok(job_id=job_id, status=job.get("status"), summary=job.get("summary"),
              error=job.get("error"), output=text)


@mcp.tool
def cancel_job(job_id: str) -> dict:
    """Terminate a running background task."""
    return JOBS.cancel(job_id)


if __name__ == "__main__":
    mcp.run()
