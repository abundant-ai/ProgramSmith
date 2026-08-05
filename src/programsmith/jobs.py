"""Per-run async-job tracking (so long operations survive the client leaving the page).

create-run (clone+ingest) and task-matrix (live LLM) run in background threads server-side; their
status is persisted to `<run_dir>/jobs.json` so the UI reflects "running" on poll and nothing is
lost on navigation. A `running` job older than STALE_SEC is reported as `error` (server restarted
mid-job) so the UI never spins forever.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

JOBS_FILE = "jobs.json"
STALE_SEC = 900  # a "running" job older than this is treated as lost (e.g. server restart)
_LOCK = threading.Lock()

# Per-SERVER-BOOT identity for job ownership. A "running" job belongs to the CURRENT server only if
# its recorded boot id matches this one; any other (or missing) id means a prior server owned it and
# its daemon thread died on restart, so the job is orphaned + respawnable.
#
# Why not os.getpid()? In a container the server is ALWAYS pid 1/2, so `job['pid'] != os.getpid()`
# is FALSE across a restart (2 == 2) — the pid check silently fails to detect restart-orphans, and a
# killed agent's job lingers as phantom-"running" until the 100-min stale bound, clogging the
# concurrency gate and wedging the agentic fleet after every deploy. A fresh UUID per process boot
# never collides, so restart detection is reliable. (Regression: 2 mid-session deploys wedged all 15
# agentic runs — no agent alive, but every job still read "running".)
_BOOT_ID = uuid.uuid4().hex


def _pid_is_alive(pid: int) -> bool:
    """Best-effort cross-process ownership check for CLI readers on the same host."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (TypeError, ValueError):
        return False
    return True


def _normalize_running_jobs(jobs: dict, *, now: float | None = None) -> None:
    """Classify running records that no longer have a live owner.

    This mutates ``jobs`` in memory. Keeping the classification in one helper is important: status
    reads and the operator's retry command must agree about whether a persisted job is still live.
    """
    now = time.time() if now is None else now
    for job in jobs.values():
        if job.get("status") != "running":
            continue
        owner_pid = job.get("pid")
        # `programsmith status` and `programsmith retry` commonly run in a second CLI process while a
        # foreground create/farm process owns the worker. A different boot id alone does not make
        # that live foreign process an orphan. Conversely, a matching/reused PID in THIS process is
        # not evidence of ownership after a container restart — the boot id remains authoritative.
        live_foreign_owner = (isinstance(owner_pid, int) and owner_pid != os.getpid()
                              and _pid_is_alive(owner_pid))
        if job.get("boot_id") != _BOOT_ID and not live_foreign_owner:
            job["status"] = "orphaned"
            job["detail"] = "lost on server restart — will respawn"
        elif now - job.get("started_at", now) > (job.get("stale_sec") or STALE_SEC):
            job["status"] = "error"
            job["detail"] = "job hung past its stale bound; retry"


def get_jobs(run_dir: str | Path) -> dict:
    from .statestore import store_for
    store, key = store_for(run_dir)
    try:
        raw = store.read(f"{key}/{JOBS_FILE}")
    except OSError:
        return {}
    if not raw:
        return {}
    try:
        jobs = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    # A running job belongs to THIS server BOOT only if its recorded boot id matches ours. Any
    # other (or missing) boot id — a previous server (restart/crash) or a legacy pre-boot-id entry
    # — means the owning daemon thread is gone, so the job can't progress on its own. Demote to
    # `orphaned` (a respawnable state) so the driver relaunches it on the next pass, instead of
    # showing "running" until the stale window elapses. A job whose artifact actually finished on
    # disk is still picked up first by the stage handler's complete() check, so finished work is
    # never re-done. (boot id, NOT pid — container pids collide across restarts; see module note.)
    _normalize_running_jobs(jobs)
    return jobs


def set_job(run_dir: str | Path, name: str, status: str, detail: str = "",
            stale_sec: int | None = None, attempts: int | None = None) -> None:
    from .statestore import store_for
    with _LOCK:
        store, key = store_for(run_dir)
        jobs = {}
        raw = store.read(f"{key}/{JOBS_FILE}")
        if raw:
            try:
                jobs = json.loads(raw)
            except json.JSONDecodeError:
                jobs = {}
        entry = jobs.get(name, {})  # preserves prior fields (attempts, etc.) across status changes
        entry["status"] = status
        entry["detail"] = detail
        if status == "error":
            entry["errored_at"] = time.time()  # so the caller can back off before a transient retry
        if status == "done":
            # so the done-but-incomplete retry can grace-period first: an agent that exits cleanly
            # mid-build often leaves an ORPHANED background build (docker cross-compile) still
            # writing the artifact — relaunching instantly would race it (see _agentic_bg_step).
            entry["finished_at"] = time.time()
        if status == "running":
            entry["started_at"] = time.time()
            entry["boot_id"] = _BOOT_ID  # owning server BOOT — a different id on read ⇒ orphaned
            entry["pid"] = os.getpid()   # kept for debugging only; boot_id is the authoritative check
            # Per-job staleness bound: a multi-iteration agentic port runs far longer than a clone/
            # ingest job, so the default STALE_SEC would falsely flag it. Carry the caller's bound.
            if stale_sec is not None:
                entry["stale_sec"] = stale_sec
            if attempts is not None:    # launch attempt number (for bounded auto-retry on error)
                entry["attempts"] = attempts
        jobs[name] = entry
        # write_atomic (tmp + os.replace) — a concurrent get_jobs() reader
        # (the UI polls while a background thread runs) must never observe a truncated/partial file.
        store.write_atomic(f"{key}/{JOBS_FILE}", json.dumps(jobs, indent=2) + "\n")


def clear_errored_jobs(run_dir: str | Path) -> list[str]:
    """Delete job entries in a terminal state (``error``/``orphaned``/``done``) so the driver
    relaunches them FRESH (attempts 0) on its next pass. A blocked agentic stage whose job exhausted
    its bounded auto-retry stays parked until the entry is cleared — this is the "clear the job to
    retry" lever for when the operator fixed the underlying cause (e.g. a deploy fix) and wants a
    clean re-run. ``done`` entries are clearable too: a done job whose artifact IS complete never
    relaunches (the stage handler's complete() check short-circuits first, and done_is_success cells
    use a unique job name per attempt), while a done-but-INCOMPLETE one that exhausted its bounded
    retry (see _agentic_bg_step) is exactly what this lever must unpark. ``running`` jobs are left
    untouched. Returns the cleared job names."""
    from .statestore import store_for
    with _LOCK:
        store, key = store_for(run_dir)
        raw = store.read(f"{key}/{JOBS_FILE}")
        if not raw:
            return []
        try:
            jobs = json.loads(raw)
        except json.JSONDecodeError:
            return []
        # Retry must classify persisted ``running`` records exactly as get_jobs() does. Previously a
        # dead prior-process job appeared as ``orphaned`` in status but remained raw ``running`` here,
        # so `programsmith retry` misleadingly cleared nothing.
        _normalize_running_jobs(jobs)
        cleared = [n for n, j in jobs.items() if j.get("status") in ("error", "orphaned", "done")]
        for n in cleared:
            del jobs[n]
        if cleared:
            store.write_atomic(f"{key}/{JOBS_FILE}", json.dumps(jobs, indent=2) + "\n")
        return cleared


def active_job(run_dir: str | Path) -> str | None:
    """Name of the currently-running job, if any (for a fleet indicator)."""
    for name, job in get_jobs(run_dir).items():
        if job.get("status") == "running":
            return name
    return None


def run_in_background(run_dir: str | Path, name: str, fn: Callable[[], None],
                      stale_sec: int | None = None, attempts: int | None = None) -> None:
    """Mark `name` running and execute `fn` in a daemon thread, recording done/error. `stale_sec`
    overrides the default lost-job timeout for long agentic jobs. `attempts` records the launch
    attempt number so the caller can bound auto-retries on error."""
    set_job(run_dir, name, "running", stale_sec=stale_sec, attempts=attempts)

    def _wrapped() -> None:
        try:
            from .costlog import cost_context
            with cost_context(run_dir, name):
                detail = fn() or ""
            set_job(run_dir, name, "done", str(detail), stale_sec=stale_sec)
        except Exception as e:  # noqa: BLE001 - record any failure for the UI (attempts preserved)
            set_job(run_dir, name, "error", str(e)[:300], stale_sec=stale_sec)

    threading.Thread(target=_wrapped, daemon=True).start()
