"""Auto-driver — advance the whole fleet hands-free, one `drive()` pass at a time.

`orchestrator.drive()` already flows a single run through every RUNNABLE stage and halts cleanly at
an environment gate, a human-review gate, an operational pause, or a terminal state. The auto-driver
just invokes `drive()` across all runs on an interval, so runs progress without manual `programsmith run` /
UI-Advance clicks — while preserving every invariant:

  * NEVER crosses a CONFIGURED human gate — `drive` halts `human` there. Which stages actually
    block is config-driven (ADR-0039: TASK_MATRIX/QA_GATE both default AUTO, so the default fleet
    runs end-to-end with zero human touches); `orchestrator.active_human_stages` is the live set.
  * NEVER synthesizes — every verdict is from a real gate/cell or a real sweep (experiment
    recorded). When `ctx['sweep_live']` is set (the server authorizes spend, the operator's
    explicit decision), the billable stages LAUNCH for real and the driver polls them to
    completion; without it they halt `blocked`. A stage with no complete bundle / no exec env halts
    honestly — it is never faked.
  * Respects pause — `drive` halts `paused`.

So the daemon turns "the run is now satisfiable / its sweep finished" into automatic forward
progress, and otherwise leaves every run parked at exactly the gate that needs a human/env/bundle
decision. A global `ctx` (e.g. `sweep_live`, `ci_repo_root`, per-run `task_path`) is threaded into
every `drive` pass.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from .fsm import TERMINAL
from .orchestrator import active_human_stages, drive
from .state import RunState


def _eligible(run_dir: Path, human_stages: frozenset | None = None) -> bool:
    """A run is worth driving iff it's not paused, not at a stage the CONFIG human-gates (those move
    only on a human verdict — ADR-0039: the default config gates NOTHING, so TASK_MATRIX/QA_GATE
    runs are driven through their auto handlers), and not terminal — EXCEPT a BLOCKED run whose
    harden bound was since raised, which is eligible again so the HARDEN_MAX increase self-applies
    (drive() re-enters it). `human_stages` lets the per-pass caller resolve the config ONCE; None
    resolves it here (orchestrator.active_human_stages)."""
    try:
        st = RunState.load(run_dir)
    except (FileNotFoundError, ValueError):
        return False
    if human_stages is None:
        human_stages = active_human_stages()
    if st.paused or st.current_stage in human_stages:
        return False
    if st.current_stage in TERMINAL:
        from .orchestrator import _is_harden_revivable
        return _is_harden_revivable(st)
    return True


_SYNC_TASK_MATRIX_PER_PASS = 1


def _drive_priority(run_dir: Path) -> tuple[int, float, str]:
    """Order a fleet pass so asynchronous heavy cells fill their slots before a synchronous
    TASK_MATRIX call can hold up the entire pass.

    A large draft farm previously walked runs alphabetically. The first matrix proposal could then
    block for minutes while only one Oracle worker was alive, leaving the configured agent pool
    mostly idle. Poll live jobs first, then visit stages that can launch background agents, keep the
    ordinary deterministic gates next, and run synchronous matrix proposals last.
    """
    from .jobs import active_job

    if active_job(run_dir):
        return (0, 0.0, run_dir.name)
    try:
        stage = RunState.load(run_dir).current_stage.value
    except (FileNotFoundError, ValueError):
        return (4, 0.0, run_dir.name)
    if stage in {"ORACLE_GOLDEN", "CREATE", "SYNTHESIZE"}:
        return (1, 0.0, run_dir.name)
    if stage == "TASK_MATRIX":
        # TASK_MATRIX is synchronous and can take minutes. Run only a small batch per fleet pass so
        # completed background Oracle jobs are collected promptly. Oldest-attempted first prevents a
        # blocked matrix from monopolizing every pass; drive() rewrites drive.json after each attempt.
        try:
            last_attempt = (run_dir / "drive.json").stat().st_mtime
        except OSError:
            last_attempt = 0.0
        return (3, last_attempt, run_dir.name)
    return (2, 0.0, run_dir.name)


def autodrive_once(
    runs_dir: str | Path,
    *,
    ctx: dict | None = None,
    notes_path: str | Path | None = None,
) -> list[dict]:
    """Drive every eligible run one `drive()` pass. Returns one record per run actually driven:
    {key, advanced, halted, final_stage, halt_reason}. Pure orchestration over `drive` (idempotent:
    a run already parked at its gate yields advanced=0 and stays put). Runs only ever record REAL
    results; `ctx['sweep_live']` authorizes the billable sweeps to launch (experiment recorded)."""
    runs_dir = Path(runs_dir)
    ctx = ctx or {}
    out: list[dict] = []
    # Run discovery via the StateStore seam (LocalFileStore); run_dir Paths feed the wired loads.
    from .statestore import get_store
    store = get_store(runs_dir)
    keys = sorted(k for k in store.list_dir("") if store.exists(f"{k}/state.json"))
    # Resolve the config'd human gates ONCE per pass (ctx overrides win) — not once per run.
    human_stages = active_human_stages(ctx=ctx)
    run_dirs = sorted((runs_dir / k for k in keys), key=_drive_priority)
    sync_matrices_driven = 0
    for run_dir in run_dirs:
        if not _eligible(run_dir, human_stages):
            continue
        try:
            is_sync_matrix = RunState.load(run_dir).current_stage.value == "TASK_MATRIX"
        except (FileNotFoundError, ValueError):
            is_sync_matrix = False
        if is_sync_matrix and sync_matrices_driven >= _SYNC_TASK_MATRIX_PER_PASS:
            continue
        if is_sync_matrix:
            sync_matrices_driven += 1
        try:
            res = drive(run_dir, ctx=ctx, notes_path=notes_path)
        except Exception as e:  # noqa: BLE001 — ONE run's drive blowing up must not freeze the fleet:
            # record it and keep driving the others (without this, an exception aborts the whole pass
            # and every run after this one stops advancing). Surfaced to the UI via drive.json below.
            import json as _json

            from .statestore import store_for
            reason = f"drive raised: {type(e).__name__}: {str(e)[:200]}"
            _store, _key = store_for(run_dir)
            _store.write_atomic(f"{_key}/drive.json", _json.dumps(
                {"steps": [], "final_stage": "?", "final_status": "in_progress",
                 "halted": "blocked", "halt_reason": reason}, indent=2))
            out.append({"key": run_dir.name, "advanced": 0, "halted": "error",
                        "final_stage": "?", "halt_reason": reason})
            continue
        out.append({
            "key": run_dir.name,
            "advanced": len(res.steps),
            "halted": res.halted,
            "final_stage": res.final_stage,
            "halt_reason": res.halt_reason,
        })
    return out


def autodrive_loop(
    runs_dir: str | Path,
    *,
    interval: float = 5.0,
    ctx: dict | None = None,
    notes_path: str | Path | None = None,
    max_passes: int | None = None,
    stop: Callable[[], bool] | None = None,
    on_pass: Callable[[int, list[dict]], None] | None = None,
) -> None:
    """Loop `autodrive_once` every `interval` seconds until `stop()` is true or `max_passes` reached.
    `on_pass(pass_index, records)` is called after each pass (e.g. to log)."""
    n = 0
    while True:
        if stop and stop():
            return
        records = autodrive_once(runs_dir, ctx=ctx, notes_path=notes_path)
        n += 1
        if on_pass:
            on_pass(n, records)
        if max_passes is not None and n >= max_passes:
            return
        if stop and stop():
            return
        time.sleep(interval)
