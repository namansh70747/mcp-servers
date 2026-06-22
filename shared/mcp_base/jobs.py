"""Reusable background-job engine for servers that run slow/async work off the request path.

A `Jobs(name)` instance gives you: persisted job records (JSON under ~/.mcp-suite/<name>/jobs/),
status/list/cancel tools, an inline-wait so SHORT tasks return their result directly while LONG ones
return a job_id to poll, and an optional thread pool (`spawn`) with a concurrency cap.

Two execution styles are supported:
  • thread-per-job — `jobs.run_or_job(kind, worker, ...)` / `jobs.spawn(kind, worker)`: each job runs
    in its own daemon thread under a BoundedSemaphore. Good for independent work (subprocesses).
  • single-owner — `jobs.new_job(kind)` + your own worker thread that calls `jobs.set/finish`, then
    `jobs.await_inline(job_id, ...)` in the tool. Good when one resource (e.g. a browser session) must
    be driven serially.

Nothing here raises into the tool; failures become an err() envelope.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import data_dir
from .errors import err, not_found, ok
from .log import get_logger

# Job-record bookkeeping keys NOT surfaced as result fields when a job finishes inline.
_META_KEYS = {"id", "kind", "status", "percent", "output", "started", "ended", "error", "cmd"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Jobs:
    def __init__(self, name: str, max_concurrent: int = 3, inline_wait: float = 15.0):
        self.name = name
        self.dir = data_dir(name) / "jobs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log = get_logger(name)
        self.default_inline = max(0.0, float(inline_wait))
        self._jobs: dict[str, dict] = {}
        self._procs: dict[str, object] = {}   # live cancel handles (e.g. subprocess.Popen)
        self._active: set[str] = set()         # job ids whose worker thread is alive
        self._lock = threading.RLock()
        self._sem = threading.BoundedSemaphore(max(1, int(max_concurrent)))

    # ----------------------------------------------------------------- persistence
    def _path(self, jid: str) -> Path:
        return self.dir / f"{jid}.json"

    def _persist(self, job: dict) -> None:
        try:
            tmp = self.dir / f".{job['id']}.tmp"
            tmp.write_text(json.dumps(job, indent=2))
            tmp.replace(self._path(job["id"]))
        except Exception:  # noqa: BLE001 — persistence is best-effort
            self.log.warning("persist failed for job %s", job.get("id"))

    def set(self, jid: str, **fields) -> dict:
        with self._lock:
            job = self._jobs.get(jid, {"id": jid})
            job.update(fields)
            self._jobs[jid] = job
            self._persist(job)
            return dict(job)

    def new_job(self, kind: str, output: str = "") -> dict:
        jid = f"{kind}_{datetime.now():%Y%m%d-%H%M%S}_{secrets.token_hex(3)}"
        job = {"id": jid, "kind": kind, "status": "queued", "percent": 0.0, "output": output,
               "started": _now(), "ended": None, "error": None}
        with self._lock:
            self._jobs[jid] = job
            self._persist(job)
        return job

    def finish(self, jid: str, ok_: bool = True, error: str = "", **extra) -> dict:
        if not ok_:
            if self._jobs.get(jid, {}).get("status") == "cancelled":
                return self._jobs.get(jid, {})
            return self.set(jid, status="error", error=error or "failed", ended=_now())
        return self.set(jid, status="done", percent=100.0, ended=_now(), **extra)

    # ----------------------------------------------------------------- thread-per-job execution
    def spawn(self, kind: str, worker, output: str = "") -> dict:
        """Run worker(job) in a daemon thread under the concurrency cap. Returns the started job."""
        job = self.new_job(kind, output)

        def run():
            with self._lock:
                self._active.add(job["id"])
            acquired = False
            try:
                self._sem.acquire()
                acquired = True
                if self._jobs.get(job["id"], {}).get("status") == "cancelled":
                    return
                self.set(job["id"], status="running")
                worker(job)
            except Exception as e:  # noqa: BLE001
                self.log.exception("job %s failed", job["id"])
                self.set(job["id"], status="error", error=str(e), ended=_now())
            finally:
                if acquired:
                    self._sem.release()
                with self._lock:
                    self._active.discard(job["id"])
                    if self._jobs.get(job["id"], {}).get("status") in ("running", "queued"):
                        self.set(job["id"], status="error",
                                 error="worker exited without a terminal status", ended=_now())

        threading.Thread(target=run, daemon=True).start()
        return job

    def run_or_job(self, kind: str, worker, inline_wait: float | None = None, output: str = "",
                   **meta) -> dict:
        """spawn() + await_inline(): short tasks return their result; long ones return a job_id."""
        job = self.spawn(kind, worker, output)
        return self.await_inline(job["id"], inline_wait, **meta)

    def mark_active(self, jid: str, on: bool = True) -> None:
        """For single-owner workers (not using spawn): declare the job's worker alive so status
        reconciliation doesn't flag it 'interrupted'."""
        with self._lock:
            (self._active.add if on else self._active.discard)(jid)

    # ----------------------------------------------------------------- inline wait
    def await_inline(self, jid: str, inline_wait: float | None = None, **meta) -> dict:
        """Block up to inline_wait sec for a terminal status. Returns ok(result, **meta) if done,
        err(...) on failure, or ok(job_id, status='running', ...) if still going."""
        wait = self.default_inline if inline_wait is None else max(0.0, float(inline_wait))
        deadline = time.time() + wait
        while True:
            j = self._jobs.get(jid) or self.load(jid) or {}
            st = j.get("status")
            if st == "done":
                fields = {k: v for k, v in j.items() if k not in _META_KEYS}
                fields.update(meta)  # caller context; same key wins once (no duplicate kwarg)
                return ok(job_id=jid, **fields)
            if st in ("error", "cancelled", "interrupted"):
                return err(j.get("error") or f"job {st}", job_id=jid, status=st)
            if time.time() >= deadline:
                meta.pop("status", None)
                meta.pop("hint", None)
                return ok(job_id=jid, status=st or "running", **meta,
                          hint="running in the background — poll job_status(job_id) until status='done'")
            time.sleep(0.25)

    # ----------------------------------------------------------------- cancel handles
    def track_proc(self, jid: str, proc) -> None:
        with self._lock:
            self._procs[jid] = proc

    def untrack_proc(self, jid: str) -> None:
        with self._lock:
            self._procs.pop(jid, None)

    # ----------------------------------------------------------------- read API (wire to @mcp.tool)
    def _reconcile(self, job: dict) -> dict:
        if job.get("status") in ("running", "queued"):
            with self._lock:
                alive = job["id"] in self._active
            if not alive:
                return {**job, "status": "interrupted",
                        "error": "worker not running (process restarted while this job was active)"}
        return job

    def load(self, jid: str) -> dict | None:
        if not jid or not str(jid).strip():
            return None
        with self._lock:
            if jid in self._jobs:
                return dict(self._jobs[jid])
        p = self._path(str(jid))
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:  # noqa: BLE001
                return None
        return None

    def all(self) -> list[dict]:
        seen: dict[str, dict] = {}
        for f in self.dir.glob("*.json"):
            try:
                j = json.loads(f.read_text())
                seen[j["id"]] = j
            except Exception:  # noqa: BLE001
                continue
        with self._lock:
            seen.update({k: dict(v) for k, v in self._jobs.items()})
        return list(seen.values())

    def status(self, jid: str) -> dict:
        j = self.load(jid)
        if not j:
            return not_found("job", jid, available=[x["id"] for x in self.all()][:10],
                             hint="use list_jobs()")
        return ok(**self._reconcile(j))

    def listing(self, limit: int = 20) -> dict:
        jobs = [self._reconcile(j) for j in self.all()]
        jobs.sort(key=lambda j: (j.get("status") != "running", j.get("started") or ""), reverse=True)
        return ok(items=jobs[:max(1, int(limit))], total=len(jobs))

    def cancel(self, jid: str) -> dict:
        j = self.load(jid)
        if not j:
            return not_found("job", jid, available=[x["id"] for x in self.all()][:10])
        with self._lock:
            proc = self._procs.get(jid)
        if proc is not None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    proc.kill()
            except Exception:  # noqa: BLE001
                pass
        return ok(**self.set(jid, status="cancelled", ended=_now()))
