"""Tests for the async-job tracking layer (persistence + staleness + background execution)."""

import json
import os
import time

from programsmith.jobs import (
    active_job,
    clear_errored_jobs,
    get_jobs,
    run_in_background,
    set_job,
)


def test_set_and_get(tmp_path):
    set_job(tmp_path, "ingest", "running")
    jobs = get_jobs(tmp_path)
    assert jobs["ingest"]["status"] == "running"
    assert active_job(tmp_path) == "ingest"
    set_job(tmp_path, "ingest", "done", "locked")
    assert get_jobs(tmp_path)["ingest"]["status"] == "done"
    assert active_job(tmp_path) is None


def test_stale_running_reported_as_error(tmp_path):
    # A job owned by THIS server BOOT (boot_id match) but hung past its stale bound → error (a genuine
    # hang; don't auto-respawn it). A job from a *different* boot is `orphaned` instead (restart case →
    # respawn) — see test_running_job_from_dead_server_is_orphaned_for_respawn.
    from programsmith import jobs
    (tmp_path / "jobs.json").write_text(json.dumps(
        {"task_matrix": {"status": "running", "boot_id": jobs._BOOT_ID,
                         "started_at": time.time() - 100000}}))
    assert get_jobs(tmp_path)["task_matrix"]["status"] == "error"


def test_run_in_background_records_done(tmp_path):
    run_in_background(tmp_path, "task_matrix", lambda: "5 candidates")
    for _ in range(50):
        if get_jobs(tmp_path).get("task_matrix", {}).get("status") != "running":
            break
        time.sleep(0.02)
    job = get_jobs(tmp_path)["task_matrix"]
    assert job["status"] == "done" and "5 candidates" in job["detail"]


def test_run_in_background_records_error(tmp_path):
    def boom():
        raise RuntimeError("kaboom")
    run_in_background(tmp_path, "ingest", boom)
    for _ in range(50):
        if get_jobs(tmp_path).get("ingest", {}).get("status") != "running":
            break
        time.sleep(0.02)
    job = get_jobs(tmp_path)["ingest"]
    assert job["status"] == "error" and "kaboom" in job["detail"]


def test_running_job_from_dead_server_is_orphaned_for_respawn(tmp_path):
    """Watchdog: a job left "running" by a PREVIOUS server boot (boot_id mismatch) is demoted to
    `orphaned` on read — immediately, not after the stale window — so the driver respawns it and a
    run is never silently stuck on an orphaned background job. `active_job` ignores orphaned."""
    import json

    from programsmith import jobs

    jobs.set_job(tmp_path, "create-fill", "running", stale_sec=6000)
    assert jobs.active_job(tmp_path) == "create-fill"  # ours, alive

    # rewrite the recorded boot id to a different (dead) prior server boot
    p = tmp_path / "jobs.json"
    d = json.loads(p.read_text())
    d["create-fill"]["boot_id"] = "deadbeef-prior-boot"
    p.write_text(json.dumps(d))

    got = jobs.get_jobs(tmp_path)
    assert got["create-fill"]["status"] == "orphaned"      # → _agentic_bg_step relaunches it
    assert jobs.active_job(tmp_path) is None                 # orphaned ≠ active


def test_container_pid_collision_still_orphans(tmp_path):
    """THE bug this fix targets: in a container the server is always pid 2, so a restart-orphan keeps
    the SAME pid as the new server. Ownership must key on boot_id, NOT pid — a job with a matching pid
    but a different (or missing) boot_id is still correctly orphaned, so a deploy no longer wedges the
    agentic fleet on phantom-'running' jobs until the 100-min stale bound. (Regression: 2 mid-session
    deploys left all 15 agentic runs stuck with no live agent.)"""
    import json
    import os

    from programsmith import jobs

    p = tmp_path / "jobs.json"
    # same pid as us (the container collision), but a prior boot id, recent start (NOT stale)
    p.write_text(json.dumps({"oracle-generate": {
        "status": "running", "pid": os.getpid(), "boot_id": "prior-container-boot",
        "started_at": time.time() - 60, "stale_sec": 6000}}))
    assert jobs.get_jobs(tmp_path)["oracle-generate"]["status"] == "orphaned"

    # a legacy record with NO boot_id at all (pre-fix) is also treated as orphaned (safe → respawn)
    p.write_text(json.dumps({"create-fill": {
        "status": "running", "pid": os.getpid(), "started_at": time.time() - 60, "stale_sec": 6000}}))
    assert jobs.get_jobs(tmp_path)["create-fill"]["status"] == "orphaned"


def test_clear_errored_jobs_removes_terminal_entries(tmp_path):
    """`clear_errored_jobs` is the 'clear the job to retry' lever: a stage parked on a job that
    exhausted its bounded auto-retry stays blocked until the entry is gone, after which
    _agentic_bg_step relaunches it fresh (no entry → attempts 0). It must drop every TERMINAL entry —
    error, orphaned, AND done (a done-but-incomplete job past its retry bound is exactly what the
    lever unparks; a done job whose artifact IS complete never relaunches because the stage handler's
    complete() check short-circuits first) — but PRESERVE running (in-flight, never interrupted)."""
    set_job(tmp_path, "ingest", "done", "locked")
    set_job(tmp_path, "oracle-generate", "running", stale_sec=6000)
    set_job(tmp_path, "create-fill", "error", "Image build ... failed")
    # an orphaned entry (e.g. a prior server) should also be swept
    p = tmp_path / "jobs.json"
    d = json.loads(p.read_text())
    d["task_matrix"] = {"status": "orphaned", "detail": "lost on restart"}
    p.write_text(json.dumps(d))

    cleared = clear_errored_jobs(tmp_path)
    assert sorted(cleared) == ["create-fill", "ingest", "task_matrix"]
    remaining = get_jobs(tmp_path)
    assert set(remaining) == {"oracle-generate"}                 # running survives
    assert "create-fill" not in remaining                        # → relaunches fresh next pass
    assert clear_errored_jobs(tmp_path) == []                    # idempotent


def test_clear_errored_jobs_classifies_raw_prior_boot_record(tmp_path):
    """The retry command reads jobs.json directly, so it must normalize a prior boot's raw
    ``running`` record before deciding what can be cleared. This keeps retry consistent with status.
    """
    p = tmp_path / "jobs.json"
    p.write_text(json.dumps({"synthesize-h0-r2-e0": {
        "status": "running", "boot_id": "prior-process", "pid": os.getpid(),
        "started_at": time.time(), "stale_sec": 6000,
    }}))

    assert clear_errored_jobs(tmp_path) == ["synthesize-h0-r2-e0"]
    assert json.loads(p.read_text()) == {}


def test_foreign_cli_reader_preserves_live_owner(tmp_path, monkeypatch):
    """Status/retry may run beside a foreground create process. Its boot id differs, but a live
    foreign owner PID means the job is still running and must not be cleared or relaunched.
    """
    from programsmith import jobs

    owner_pid = os.getpid() + 1000
    monkeypatch.setattr(jobs, "_pid_is_alive", lambda pid: pid == owner_pid)
    p = tmp_path / "jobs.json"
    p.write_text(json.dumps({"synthesize-h0-r2-e0": {
        "status": "running", "boot_id": "foreground-create-process", "pid": owner_pid,
        "started_at": time.time(), "stale_sec": 6000,
    }}))

    assert get_jobs(tmp_path)["synthesize-h0-r2-e0"]["status"] == "running"
    assert clear_errored_jobs(tmp_path) == []
    assert json.loads(p.read_text())["synthesize-h0-r2-e0"]["status"] == "running"
