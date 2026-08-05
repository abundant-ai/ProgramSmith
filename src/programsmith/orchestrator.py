"""The dumb orchestrator driver (invariant #1).

A deterministic loop: look up the current stage's handler, run it, route on the verdict it returns,
advance the FSM, persist, repeat — until a terminal state, a human-review gate (only when a gate is
config'd "human" — both TASK_MATRIX and QA_GATE default AUTO, ADR-0039), an operational pause, or an
environment-blocked stage (Docker down, a billable sweep not yet launched). The driver never
reasons; handlers wrap existing cells/gates and return a `StageResult`. Blocked stages halt with a
clear reason (not an error) so a parked run explains itself instead of looking "stuck". Accepted
tasks are EXPORTED to the outbox (no PR is ever opened — ADR-0039); a run landing on EASY_SHELF is
exported to the easy shelf the same way.

Handlers are injectable (pass a `registry`) so the loop is unit-tested without side effects.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .cells.create import assemble_skeleton
from .cells.oracle_golden import (
    MIN_CASES,
    MINPACK_EPSILON,
    _bundle_output_diversity,
    _bundle_status,
    _minimum_unique_outcomes,
    adopt_existing,
)
from .fsm import Stage
from .gates.calibrate import calibrate
from .gates.sanity import run_sanity, run_sanity_trials
from .gates.static_ci import run_static_ci
from .manifest import Manifest
from .state import RunState, StageEvent
from .workflow_notes import record_backward_move


@dataclass
class StageResult:
    verdict: str | None = None     # a value in the stage's ALLOWED_VERDICTS, or None
    reason: str = ""
    blocked: bool = False          # cannot run here (Docker / spend / external input)
    human: bool = False            # awaiting a human-review verdict


@dataclass
class DriveResult:
    steps: list[dict] = field(default_factory=list)   # [{stage, verdict, next, reason}]
    final_stage: str = ""
    final_status: str = ""
    halted: str = ""               # human | paused | terminal | blocked | max_steps
    halt_reason: str = ""


Handler = Callable[[RunState, Manifest, Path, dict], StageResult]


def _docker_ok() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=8).returncode == 0
    except Exception:
        return False


def _task_bundle(manifest: Manifest, run_dir: Path, ctx: dict) -> Path | None:
    """Resolve the complete Harbor task dir to sweep: explicit ctx['task_path'] >
    manifest.snapshot['task_bundle_path'] > the run's generated task dir (only if it looks complete:
    has a task.toml). A skeleton with unfilled TODOs is not a valid bundle for a real sweep."""
    explicit = ctx.get("task_path") or (manifest.snapshot or {}).get("task_bundle_path")
    if explicit:
        return Path(explicit)
    gen = Path(run_dir) / "task" / (manifest.slug or "rewrite-task")
    return gen if (gen / "task.toml").exists() else None


# Held-out / intermediate dirs that generate-mode leaves in the task tree but that must NEVER be in
# the solver-facing task: `oracle` (the reference port — reading it is the ultimate reward
# hack), plaintext `goldens` (expected outputs), and capture/jobs scratch. The encrypted `private.enc`
# + `environment/` (vendored offline deps) DO ship. Stripping these also keeps the staged bundle
# small (a generated oracle's cargo `target/` is hundreds of MB).
_UPLOAD_EXCLUDE_DIRS = ("oracle", "goldens", "capture", "jobs")
_UPLOAD_EXCLUDE_GLOBS = ("target", "node_modules", ".git", "__pycache__", "*.tar.gz", "*.rlib",
                         "*.rmeta", ".create-fill-ok", ".staticci", ".upload", ".sweeps")


def _sweep_upload_bundle(bundle: Path, run_dir: Path, slug: str) -> Path:
    """Stage a CLEAN copy of the task for a sweep: drop held-out artifacts (oracle/goldens) and
    build outputs (target/…). Anti-hack AND size. A bundle that's already clean (an adopted shipped
    task) is returned as-is. Idempotent (rebuilt each launch)."""
    import fnmatch
    import shutil

    src = Path(bundle)
    if not any((src / x).exists() for x in _UPLOAD_EXCLUDE_DIRS):
        return src  # already minimal (e.g. minpack's adopted shipped task)

    def _excluded(name: str) -> bool:  # top-level filter (ignore_patterns only filters inside copytree)
        return name in _UPLOAD_EXCLUDE_DIRS or any(fnmatch.fnmatch(name, g) for g in _UPLOAD_EXCLUDE_GLOBS)

    dest = Path(run_dir) / ".upload" / slug
    shutil.rmtree(dest.parent, ignore_errors=True)
    dest.mkdir(parents=True)
    ignore = shutil.ignore_patterns(*_UPLOAD_EXCLUDE_GLOBS)
    for entry in src.iterdir():
        if _excluded(entry.name):
            continue
        if entry.is_dir():
            shutil.copytree(entry, dest / entry.name, ignore=ignore)
        else:
            shutil.copy2(entry, dest / entry.name)
    return dest


# An agentic cell (oracle clean-room port / create fill) runs many minutes across bounded sessions —
# far longer than a clone/ingest job — so its background job gets a generous lost-job bound.
_AGENTIC_STALE_SEC = 3 * 1800 + 600  # ≈ max_iters × session timeout + buffer
# A timed-out/errored agentic job auto-retries (self-heal: a timeout/`claude CLI exited 1` is often
# transient — subscription contention from concurrent agents) before hard-blocking. The agent's
# partial work is on disk, so a retry continues it. Retries are SPACED OUT (backoff) so a contention
# spike clears before the next attempt instead of all retries hitting the same spike.
_AGENTIC_MAX_ATTEMPTS = 3
_AGENTIC_RETRY_BACKOFF_SEC = 30
# A job that finished CLEANLY but left the artifact incomplete gets the same bounded retry as an
# error — an unattended fleet must not wedge on it (the pb10 toybox stall: the agent's session
# ended mid-qemu-build, exited 'done', and the run sat "STOP-and-flag" forever with nobody there
# to clear it). The backoff is LONG because this case often means an ORPHANED background build
# (docker cross-compile) is still writing the bundle — complete() is re-checked every driver pass
# during the wait, so a landing build unwedges the run without spending a fresh agent, and a
# premature relaunch would race the build for the same artifact dir.
_AGENTIC_INCOMPLETE_BACKOFF_SEC = 15 * 60

# Max concurrent `claude -p` CELL agents across the WHOLE fleet. The agents share ONE OAuth
# subscription, which THROTTLES token delivery (~2-4 tok/s vs 50+) under concurrent load — so several
# parallel agents make every one of them slow, and the longest-thinking cell (a hard synthesize plan)
# times out and wedges. Serialising to 1 gives each agent full throughput. (Interim local fix; the
# structural fix is moving agent execution to an API-keyed backend — ADR-0030. Tunable.)
_AGENTIC_CONCURRENCY = 1
_AGENTIC_JOB_PREFIXES = ("synthesize-", "create-fill", "oracle-generate", "generate")
# A cell agent running longer than this is likely THROTTLED (a healthy cell finishes well under it);
# surfaced in the UI as a slow/throttle hint so the operator isn't left guessing (as we were on cjson).
_AGENT_SLOW_SEC = 150


def _agentic_concurrency() -> int:
    """Fleet-wide cap on concurrent `claude -p` cell agents. Config-driven (re-read each step so a
    tuning change applies live, like the other auto-driver knobs) with the module constant as the
    fallback. Stays 1 on the single throttled OAuth subscription (ADR-0030); raise it to ≈ the cell
    credential-pool size once cells run on a pool of separate-billing creds (ADR-0033)."""
    try:
        from .config import LhConfig
        return max(1, int(LhConfig.load().agentic_concurrency))
    except Exception:
        return _AGENTIC_CONCURRENCY


def _running_agentic_count(this_run_dir: Path) -> int:
    """Count `claude -p` cell-agent jobs currently 'running' across the fleet EXCEPT this run. Used to
    cap concurrency: get_jobs() already demotes dead-pid 'running' jobs to 'orphaned', so only genuinely
    live agents (this server's children) are counted."""
    from . import jobs
    try:
        siblings = [d for d in this_run_dir.parent.iterdir() if d.is_dir() and d != this_run_dir]
    except OSError:
        return 0
    n = 0
    for d in siblings:
        for name, job in jobs.get_jobs(d).items():
            if job.get("status") == "running" and any(name.startswith(p) for p in _AGENTIC_JOB_PREFIXES):
                n += 1
    return n


def _cell_session(model: str | None = None):
    """A fresh agentic `claude -p` session for a background cell job (auth = the CLI's own
    keychain / CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY, straight env passthrough)."""
    from .cells.agentic import claude_code_session
    return claude_code_session(model=model)


def _agentic_bg_step(job_name: str, run_dir: Path, *, produce, complete, apply_result,
                     done_is_success: bool = False) -> StageResult:
    """Run a long agentic cell as a NON-BLOCKING background job (jobs.py daemon thread) so a
    multi-minute `claude -p` session never freezes the fleet auto-driver. Mirrors the sweep
    state machine: launch → poll → apply.
      produce()      — the slow agentic work; runs in the bg thread; returns a detail string; RAISES
                       to signal failure (→ job 'error').
      complete()     — bool: is the artifact already complete on disk? (idempotent main-loop guard,
                       survives restarts).
      apply_result() — StageResult, run on the MAIN loop once complete (deterministic manifest
                       mutation + verdict).
      done_is_success — when the cell has no idempotent disk artifact (e.g. an in-place patch), treat
                       a 'done' job (produce returned without raising) as success → apply_result. The
                       caller MUST then use a job_name unique per attempt so a prior success isn't
                       mistaken for the current one.
    State lives in <run_dir>/jobs.json[job_name]."""
    from . import jobs

    if complete():                      # artifact already on disk → apply deterministically + advance
        return apply_result()
    job = jobs.get_jobs(run_dir).get(job_name, {})
    st = job.get("status")
    attempts = job.get("attempts", 0)
    if st == "running":
        return StageResult(blocked=True, reason=f"{job_name}: agent running in background "
                           f"({job.get('detail') or 'started'})")

    def _queued() -> str | None:
        # The global fleet cap is the throttle guard: the cell agents share ONE credential (the
        # operator's OAuth login / API key), which slows under concurrent load.
        cap = _agentic_concurrency()
        if _running_agentic_count(run_dir) >= cap:
            return (f"{job_name}: waiting for an agent slot ({cap} max — shared-credential "
                    "throttle guard)")
        return None

    def _launch(att: int) -> None:
        jobs.run_in_background(run_dir, job_name, produce, stale_sec=_AGENTIC_STALE_SEC,
                               attempts=att)

    if st == "error":
        # Self-heal: a timeout/error is often transient. Auto-retry, bounded — partial work is on disk.
        if attempts < _AGENTIC_MAX_ATTEMPTS:
            import time
            waited = time.time() - job.get("errored_at", 0)
            if waited < _AGENTIC_RETRY_BACKOFF_SEC:  # space retries so a contention spike clears first
                return StageResult(blocked=True, reason=f"{job_name}: agent errored "
                                   f"({(job.get('detail') or '')[:70]}) — backing off "
                                   f"~{int(_AGENTIC_RETRY_BACKOFF_SEC - waited)}s "
                                   f"(retry {attempts + 1}/{_AGENTIC_MAX_ATTEMPTS})")
            queued = _queued()
            if queued:
                return StageResult(blocked=True, reason=queued)
            _launch(attempts + 1)
            return StageResult(blocked=True, reason=f"{job_name}: agent errored "
                               f"({(job.get('detail') or '')[:70]}) — auto-retry {attempts + 1}/"
                               f"{_AGENTIC_MAX_ATTEMPTS}")
        return StageResult(blocked=True, reason=f"{job_name}: agent errored after {attempts} attempt(s) "
                           f"— {(job.get('detail') or '')[:120]} (NOT advanced; needs investigation)")
    if st == "done":
        if done_is_success:             # no disk artifact; a clean return IS the success signal
            return apply_result()
        # Done-but-INCOMPLETE: the agent exited cleanly yet complete() (checked above, every pass)
        # says the artifact isn't there — a session cap hit mid-build, an agent that misjudged its
        # own output, or an orphaned background build still running. Same bounded self-heal as an
        # error, but with a long grace period first so a still-writing build can land (complete()
        # keeps being polled during the wait). Only after the bounded attempts does it hard-block.
        if attempts < _AGENTIC_MAX_ATTEMPTS:
            import time
            waited = time.time() - job.get("finished_at", 0)
            if waited < _AGENTIC_INCOMPLETE_BACKOFF_SEC:
                return StageResult(blocked=True, reason=(
                    f"{job_name}: agent finished but artifact incomplete — "
                    f"{(job.get('detail') or '')[:100]} — waiting ~"
                    f"{int((_AGENTIC_INCOMPLETE_BACKOFF_SEC - waited) / 60)}min for a straggling "
                    f"build to land, then retry {attempts + 1}/{_AGENTIC_MAX_ATTEMPTS}"))
            queued = _queued()
            if queued:
                return StageResult(blocked=True, reason=queued)
            _launch(attempts + 1)
            return StageResult(blocked=True, reason=(
                f"{job_name}: artifact still incomplete after grace period — auto-retry "
                f"{attempts + 1}/{_AGENTIC_MAX_ATTEMPTS}"))
        return StageResult(blocked=True, reason=f"{job_name}: agent finished but artifact incomplete "
                           f"after {attempts} attempt(s) — {(job.get('detail') or '')[:160]} "
                           "(NOT advanced; needs investigation)")
    queued = _queued()
    if queued:
        return StageResult(blocked=True, reason=queued)
    _launch(attempts)
    return StageResult(blocked=True, reason=f"{job_name}: launched agent in background; "
                       "polling each pass")


# A sweep whose frontier trials ALL errored measured nothing → re-run a fresh experiment, bounded.
_SWEEP_MAX_ATTEMPTS = 2
# A LAUNCH failure (the backend never returned a handle — an environment/daemon hiccup) is usually
# TRANSIENT, so retry it more generously and SPACED OUT (backoff) — a brief outage then
# self-heals on the next pass instead of needing a manual reset — while still bounding a real outage.
_LAUNCH_MAX_ATTEMPTS = 5
_LAUNCH_BACKOFF_SEC = 45
# After the fast launch-retry burst is exhausted on TRANSIENT launch failures (a backend outage),
# don't wedge the run forever needing a manual reset. Once this long cooldown passes (the environment
# has likely recovered), reset for ONE fresh burst — self-healing. Bounded to ~1 burst per cooldown,
# so a persistent outage never fast-spins/over-spends.
_LAUNCH_RECOVER_SEC = 1200  # 20 min
# The difficulty sweep runs `--run-analysis` so each Opus trial is TrialClassifier-labelled
# (GOOD/BAD × SUCCESS/FAILURE, HARNESS_ERROR). The classifier runs as a PHASE AFTER the trials and lags
# them by minutes, so we WAIT for it (block while pending) — the human needs the good-vs-bad-failure
# labels at QA_GATE, and a difficulty sweep whose frontier trials FAILED is exactly where "appropriately
# hard (GOOD_FAILURE)" vs "broken (BAD_FAILURE)" matters. The bound is GENEROUS (≈10 min at the 8s
# driver interval) only so a genuinely-hung classifier eventually proceeds with a LOUD flag rather than
# wedging forever — it is not a quick advisory short-circuit.
_ANALYSIS_MAX_POLLS = 75

# TrialClassifier labels that do NOT reflect a genuine Opus capability measurement, so they make the
# difficulty band untrustworthy (a spurious failure makes a task look harder than it is; a gamed
# success makes it look easier). Surfaced to the human + the harden, never used to silently rewrite
# the deterministic band (determinism-sandwich: the classifier is an advisory annotation).
_SPURIOUS_LABELS = ("HARNESS_ERROR", "BAD_FAILURE")
_GAMED_LABELS = ("BAD_SUCCESS",)

# pass@1 at/above which a task is "saturated-hard" — Opus solves ~every trial. One more harden won't
# rescue it (that's a SCOPE problem, fixed upstream at TASK_MATRIX), so such a BLOCKED run is NOT
# auto-revived when HARDEN_MAX rises. Below it ("borderline"/"moderate") a further harden can plausibly
# push the band under 0.60, so a bound increase self-applies. (Same threshold as the harden severity.)
_SEVERE_SATURATION = 0.95


def _bundle_too_large(path: Path) -> str | None:
    """Return a human size string ('NNN MB / N files') if the task bundle exceeds the cap, else None.
    A remote-upload backend ships the whole task dir; an oversized bundle (e.g. raw multi-MB media
    captured as goldens) fails the upload with a cryptic storage error after burning the launch-retry
    budget. Cap (PROGRAMSMITH_MAX_TASK_MB, default 256) → fail fast with the REAL reason instead. Counts files
    once (the staged bundle is already stripped of held-out/build artifacts). The in-place local
    backend never applies it (needs_upload=False)."""
    import os
    cap_mb = int(os.getenv("PROGRAMSMITH_MAX_TASK_MB", "256"))
    total = n = 0
    for p in Path(path).rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
                n += 1
            except OSError:
                pass
    return f"{total // 1_000_000} MB / {n} files (cap {cap_mb} MB)" if total > cap_mb * 1_000_000 else None


# Launch-error signatures that will NOT self-heal on retry — an oversize upload, a storage/quota
# rejection, or an auth failure is permanent until the operator fixes the cause. Distinguished from
# transient network/timeout blips (which DO get the spaced retry budget). Matched case-insensitively
# on the error string, so it is backend-agnostic.
_PERMANENT_LAUNCH_SIGNS = (
    "entitytoolarge", "too large", "413", "request entity", "payload too large",
    "quota", "insufficient storage", "507",
    "unauthorized", "forbidden", "invalid api key", "access denied",
)


def _is_permanent_launch_error(msg: str) -> bool:
    m = (msg or "").lower()
    return any(s in m for s in _PERMANENT_LAUNCH_SIGNS)


def _resolve_backend(ctx: dict):
    """The sweep backend for this driver pass. Thin indirection so every live-I/O seam resolves the
    same way and offline tests can inject `ctx['sweep_backend']` or the local-engine knobs
    (`local_trial_runner`/`local_classifier`/`local_executor`)."""
    from .sweepbackend import get_backend
    return get_backend(ctx)


def _lh_config():
    """LhConfig, or None when unreadable — handlers must degrade to defaults, never crash the
    driver on a malformed/missing config file."""
    try:
        from .config import LhConfig
        return LhConfig.load()
    except Exception:  # noqa: BLE001 — config is an input, not a dependency
        return None


def _gate_mode(ctx: dict | None, key: str) -> str:
    """Resolve a human-gate mode ('auto' | 'human') for `key` in {task_matrix_mode, qa_gate_mode}:
    ctx wins (per-drive override, tests), else the persisted config, else the ADR-0039 default."""
    v = (ctx or {}).get(key)
    if v in ("auto", "human"):
        return v
    cfg = _lh_config()
    v = getattr(cfg, key, None) if cfg else None
    return v if v in ("auto", "human") else "auto"


def active_human_stages(cfg=None, *, ctx: dict | None = None) -> frozenset[Stage]:
    """The stages that ACTUALLY block for a human under the current config (ADR-0039). fsm.
    HUMAN_STAGES is the static 'may be human-gated' set; this is the live one — both gates default
    AUTO, so the default is EMPTY (zero-touch farm runs). `cfg` (an LhConfig) short-circuits the
    config load; `ctx` overrides win either way. daemon._eligible and peek consult THIS, never
    HUMAN_STAGES directly."""
    def mode(key: str) -> str:
        v = (ctx or {}).get(key)
        if v in ("auto", "human"):
            return v
        v = getattr(cfg, key, None) if cfg is not None else None
        if v in ("auto", "human"):
            return v
        return _gate_mode(ctx, key)
    out = set()
    if mode("task_matrix_mode") == "human":
        out.add(Stage.TASK_MATRIX)
    if mode("qa_gate_mode") == "human":
        out.add(Stage.QA_GATE)
    return frozenset(out)


def _launch_extra_flags(ctx: dict, backend, extra_flags=None) -> list[str] | None:
    """Sweep/probe launch flags. The local backend builds every trial fresh via `docker build`, so
    there is no image cache to bust — only analysis switches (e.g. ['--run-analysis']) pass through."""
    return list(extra_flags) if extra_flags else None


def _sweep_step(
    stage_key: str, manifest: Manifest, run_dir: Path, ctx: dict,
    *, agents, experiment_suffix: str, finalize, verdict_of, extra_flags=None, generation: int = 0,
) -> StageResult:
    """Shared launch→poll→read→validate→record state machine for a live sweep stage (driven
    repeatedly by the auto-driver). State lives in manifest.sweeps[stage_key].status ∈
    {running, done, errored}. Only launches when ctx['sweep_live'] is set (explicit spend
    authorization — trials bill the operator's own key). Each launch uses a DETERMINISTIC,
    timestamp-free experiment name (trials.experiment_name): `lh-<slug>-<stage>` bumped by the run
    `generation` (harden+revise) and the error-retry `attempt`, so a re-measurement is never folded
    into a prior experiment yet the names stay readable. A completed sweep with NO valid frontier
    measurement (every solver trial errored) is NOT advanced — it is recorded `errored` and re-run a
    fresh experiment, bounded by `_SWEEP_MAX_ATTEMPTS`, then hard-blocked for investigation.
    `extra_flags` carries analysis switches (e.g. ['--run-analysis'])."""
    from datetime import datetime, timezone

    from . import trials as tr
    from .sweepbackend import sweep_live

    entry = (manifest.sweeps or {}).get(stage_key) or {}
    backend = _resolve_backend(ctx)
    status = entry.get("status")
    attempts = entry.get("attempts", 0)

    if status == "done":
        return StageResult(verdict=verdict_of(entry), reason=f"{stage_key}: result present")

    if status == "running":
        exp = entry["experiment"]
        # A local foreground process may have exited after cancelling its named solver containers.
        # Reclaim only unfinished persisted-plan trials, and only on a drive pass that explicitly
        # authorizes spend. Read-only status/peek calls never invoke resume.
        if sweep_live(ctx) and hasattr(backend, "resume"):
            try:
                backend.resume(exp)
            except Exception as e:  # noqa: BLE001 — surface a recoverable local resume failure cleanly
                return StageResult(blocked=True, reason=(
                    f"{stage_key}: could not resume interrupted {backend.name} sweep {exp} "
                    f"({str(e)[:100]})"))
        try:
            poll = backend.status(exp)
        except Exception as e:  # noqa: BLE001 — a transient poll error must not crash the driver
            return StageResult(blocked=True, reason=f"{stage_key}: polling {exp} (status error: {str(e)[:80]})")
        if not poll["complete"]:
            if poll.get("incomplete"):
                return StageResult(blocked=True, reason=(
                    f"{stage_key} is interrupted on {backend.name} ({exp}): "
                    f"{poll['trials_completed']}/{poll['trials_total']} trials complete; re-run the "
                    "create/farm command and confirm spend to resume unfinished trials"))
            return StageResult(blocked=True, reason=(
                f"{stage_key} running on {backend.name} ({exp}): {poll['tasks_running']} task(s) running, "
                f"{poll['trials_completed']}/{poll['trials_total']} trials"))
        out_dir = Path(run_dir) / backend.artifact_subdir / exp
        # Scope the results to THIS sweep's own agents before ANYTHING reads them (validity check,
        # finalize/pass@1, integrity). A results source that carries ANOTHER stage's trials — an
        # imported pull tree, a task-scoped cloud pull — must never stand in for this sweep's
        # frontier (the upstream pb10 false-DONE: a full sweep finalized pass@1 over co-pulled
        # smoke trials while every frontier trial errored — see trials.scope_trials). Baselines
        # pass through for the integrity check.
        trials = tr.scope_trials(backend.results(exp, out_dir), agents)
        # Validity (standardized across sweeps): the sweep must produce at least one NON-errored
        # frontier measurement (reward not None). A reward of 0 is a valid "failed-but-measured"
        # trial; reward None means the trial errored and measured nothing. The frontier EXCLUDES the
        # oracle/nop baselines AND any flagged probe — a completion carrying only those (every solver
        # trial errored, e.g. a CANCELLED sweep) measured nothing and must re-run, never finalize a
        # band over the probe.
        frontier = tr.frontier_trials(trials)
        measured = [t for t in frontier if t.get("reward") is not None]
        errored = len(frontier) - len(measured)
        if not measured:
            manifest.sweeps[stage_key] = {
                "status": "errored", "experiment": exp, "pull_dir": str(out_dir),
                "attempts": attempts, "n_frontier": len(frontier), "n_errored": errored,
                "summary": f"all {len(frontier)} frontier trial(s) errored — no measurement"}
            return StageResult(blocked=True, reason=(
                f"{stage_key} errored on {backend.name} ({exp}): all {len(frontier)} frontier trial(s) "
                f"errored (no measurement)" + (f" — re-running a fresh sweep (attempt "
                f"{attempts + 1}/{_SWEEP_MAX_ATTEMPTS})" if attempts < _SWEEP_MAX_ATTEMPTS else
                f" after {attempts} attempt(s); NOT advanced — needs investigation")))
        result = finalize(trials)
        manifest.sweeps[stage_key] = {"status": "done", "experiment": exp, "pull_dir": str(out_dir),
                                      "n_errored": errored, **result}
        return StageResult(verdict=verdict_of(manifest.sweeps[stage_key]),
                           reason=f"{stage_key} complete ({exp}): {result.get('summary', '')}"
                           + (f" ({errored} trial(s) errored, excluded)" if errored else ""))

    if status == "errored":
        launch_fail = entry.get("experiment") is None  # never got a handle → launch-side (often transient)
        permanent = "permanent" in (entry.get("summary") or "").lower()  # oversized/quota/auth (see launch guard)
        bound = _LAUNCH_MAX_ATTEMPTS if launch_fail else _SWEEP_MAX_ATTEMPTS
        try:                                            # seconds since this entry errored
            waited = (datetime.now(timezone.utc)
                      - datetime.fromisoformat(entry["errored_at"])).total_seconds()
        except (KeyError, ValueError):
            waited = float("inf")
        if attempts >= bound:  # beyond the fast-retry burst
            # SELF-HEAL a TRANSIENT launch outage: after a long cooldown (the environment likely
            # recovered) reset for one fresh burst rather than wedging forever. A frontier-trials-errored
            # (non-launch) failure OR a PERMANENT launch error (oversized/quota/auth) stays hard-blocked —
            # those need a real fix, not a blind re-run.
            if launch_fail and not permanent and waited >= _LAUNCH_RECOVER_SEC:
                attempts = 0                            # fresh burst; fall through to relaunch
            else:
                kind = "transient launch failures" if launch_fail else "all frontier trials errored"
                heal = (f" — auto-retry in ~{max(1, int((_LAUNCH_RECOVER_SEC - waited) / 60))}min "
                        "(launch-outage self-heal)") if (launch_fail and not permanent) else \
                       " — NOT advanced; investigate before re-running"
                return StageResult(blocked=True, reason=f"{stage_key} errored on {backend.name} after "
                                   f"{attempts} attempt(s) ({kind}){heal}")
        elif launch_fail and waited < _LAUNCH_BACKOFF_SEC:  # within-burst backoff — space retries out
            return StageResult(blocked=True, reason=f"{stage_key}: {backend.name} launch failed — backing off "
                               f"~{int(_LAUNCH_BACKOFF_SEC - waited)}s before retry "
                               f"(launch attempt {attempts}/{_LAUNCH_MAX_ATTEMPTS})")
        # else fall through to relaunch a fresh experiment

    # launch (no entry yet, or an errored entry under the retry bound → fresh experiment)
    if not sweep_live(ctx):
        return StageResult(blocked=True, reason=f"{stage_key} needs a live sweep (billable on cloud; spends "
                           "the operator's own API key locally) — enable ctx 'sweep_live' to auto-launch")
    bundle = _task_bundle(manifest, run_dir, ctx)
    if bundle is None or not bundle.exists():
        return StageResult(blocked=True, reason=f"{stage_key}: no complete task bundle to run "
                           "(set manifest.snapshot.task_bundle_path or finish CREATE fill)")
    slug = manifest.slug or "rewrite-task"
    exp_name = tr.experiment_name(slug, experiment_suffix, generation=generation, attempt=attempts)
    # Stage a CLEAN copy either way — stripping the held-out oracle/goldens from the solver-facing
    # bundle is anti-hack (invariant #6), not just an upload concern. Only the remote-upload backend
    # applies the storage SIZE CAP (a local sweep runs the dir in place, no storage limit).
    upload = _sweep_upload_bundle(bundle, Path(run_dir), slug)
    if backend.needs_upload:
        oversized = _bundle_too_large(upload)
        if oversized:
            # Fail FAST with the real reason — an oversized task can't upload, and retrying the launch
            # just burns the budget on a cryptic storage error. Block (max attempts) so the operator
            # shrinks the oracle battery (smaller/representative inputs) rather than re-running blindly.
            manifest.sweeps[stage_key] = {"status": "errored", "experiment": None,
                                          "attempts": _LAUNCH_MAX_ATTEMPTS,
                                          "errored_at": datetime.now(timezone.utc).isoformat(),
                                          "summary": f"task bundle too large to upload: {oversized}"}
            return StageResult(blocked=True, reason=f"{stage_key}: task bundle is {oversized} — too large "
                               "to upload. The oracle battery is oversized (raw multi-MB inputs/"
                               "goldens); shrink it to small/representative cases and re-run (or raise "
                               "PROGRAMSMITH_MAX_TASK_MB).")
    try:
        exp = backend.launch(upload, agents, experiment=exp_name,
                             extra_flags=_launch_extra_flags(ctx, backend, extra_flags))
    except Exception as e:  # noqa: BLE001 — a launch failure (oversize bundle, a dead Docker daemon)
        # is a clean block, NOT a crash that aborts the whole driver pass. Record errored so the retry
        # bound applies. PERMANENT errors (oversize upload / quota / auth) will NEVER self-heal —
        # retrying them just burns the budget and mislabels the block "transient launch failures".
        # Detect them and hard-block IMMEDIATELY with the real reason; only genuinely transient
        # errors (network/timeout) get the spaced retry budget.
        permanent = _is_permanent_launch_error(str(e))
        manifest.sweeps[stage_key] = {
            "status": "errored", "experiment": None,
            "attempts": _LAUNCH_MAX_ATTEMPTS if permanent else attempts + 1,
            "errored_at": datetime.now(timezone.utc).isoformat(),
            "summary": f"{backend.name} launch failed{' (permanent)' if permanent else ''}: {str(e)[:160]}"}
        if permanent:
            return StageResult(blocked=True, reason=f"{stage_key}: {backend.name} launch failed with a "
                               f"PERMANENT error — NOT retrying ({str(e)[:140]}). Likely an oversize "
                               "task bundle (shrink the oracle battery / lower PROGRAMSMITH_MAX_TASK_MB) or a "
                               "quota/auth issue; fix the cause and re-run.")
        return StageResult(blocked=True, reason=f"{stage_key}: {backend.name} launch failed "
                           f"({str(e)[:120]}) — backing off; will retry (launch attempt "
                           f"{attempts + 1}/{_LAUNCH_MAX_ATTEMPTS})")
    manifest.sweeps[stage_key] = {"status": "running", "experiment": exp, "attempts": attempts + 1,
                                  "launched_at": datetime.now(timezone.utc).isoformat()}
    retry = f" (attempt {attempts + 1}/{_SWEEP_MAX_ATTEMPTS})" if attempts else ""
    return StageResult(blocked=True, reason=f"launched {stage_key} on {backend.name} ({exp}){retry}; polling each pass")


def clear_errored_sweeps(manifest: Manifest, run_dir: Path, ctx: dict | None = None) -> list[str]:
    """The sweep half of the operator's `retry` lever: drop every `errored` manifest.sweeps entry
    (the agentic-job half is jobs.clear_errored_jobs) so the next drive pass relaunches that sweep
    as a FRESH burst (attempts 0). The poisoned experiment dir is retired via backend.discard —
    kept on disk for the postmortem, but never re-adopted as complete. Returns the cleared keys."""
    cleared: list[str] = []
    backend = _resolve_backend(ctx or {})
    for key, entry in list((manifest.sweeps or {}).items()):
        if isinstance(entry, dict) and entry.get("status") == "errored":
            if (exp := entry.get("experiment")) and hasattr(backend, "discard"):
                try:
                    backend.discard(exp)
                except Exception:  # noqa: BLE001 — a failed retire must not block the reset
                    pass
            manifest.sweeps.pop(key)
            cleared.append(key)
    if cleared:
        manifest.save(run_dir)
    return cleared


def _parse_pass_at_1(manifest: Manifest) -> float | None:
    diff = (manifest.sweeps or {}).get("difficulty", {})
    val = diff.get("pass_at_1", diff.get("claude_code_pass_at_1"))
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        import re
        m = re.search(r"\d*\.?\d+", val)
        return float(m.group()) if m else None
    return None


def _harness_sweep_name(harness: str) -> str:
    """Catalog harness key → the name a sweep registers it under (mini-swe → "mini-swe-agent").
    Read paths fold it back via trials._canon_agent."""
    from .runconfig import sweep_agent_name
    return sweep_agent_name(harness)


def _stage_sweep_agents(stage_spec):
    """Build the agent list for a sweep from the run's StageSpec: the cheap oracle/nop baselines
    (always) + the configured frontier agents (harness × model × n_trials). Harness keys are
    translated to their sweep-registered names at LAUNCH time; read paths canonicalize back
    (trials._canon_agent), so bands/groups stay keyed by the catalog name."""
    from .trials import SweepAgent
    agents = [SweepAgent("oracle", "default", 1), SweepAgent("nop", "default", 1)]
    agents += [SweepAgent(_harness_sweep_name(a.harness), a.model, a.n_trials)
               for a in (stage_spec.agents or [])]
    return agents


def _analysis_agents(stage_spec) -> tuple[str, ...]:
    """The agent-name filter for TrialClassifier reads on this stage's CONFIGURED harnesses. Both
    the catalog key AND the sweep-registered name are included: fetch paths canonicalize trial
    agents back to catalog keys, but a raw record may still carry the registered name — a superset
    filter matches either without ever admitting the oracle/nop baselines."""
    names: list[str] = []
    for a in (getattr(stage_spec, "agents", None) or []):
        names.append(a.harness)
        names.append(_harness_sweep_name(a.harness))
    return tuple(dict.fromkeys(names)) or ("claude-code",)


def _band_from_entry(entry: dict, basis: str) -> float | None:
    """Resolve a sweep entry's band value for the configured basis. Prefers the per-agent `groups`
    (works for any agent set); falls back to the legacy scalar fields for older/injected entries."""
    from .runconfig import band_value
    v = band_value(entry.get("groups"), basis)
    if v is not None:
        return v
    if basis == "aggregate":
        for k in ("aggregate", "pass_at_1", "claude_code"):
            if isinstance(entry.get(k), (int, float)):
                return float(entry[k])
        return None
    legacy = entry.get(basis.replace("-", "_"))
    return float(legacy) if isinstance(legacy, (int, float)) else None


# ---- handlers (each wraps an existing cell/gate) -------------------------------------

def _h_human(state, manifest, run_dir, ctx) -> StageResult:
    which = "#1 (pick tasks)" if state.current_stage is Stage.TASK_MATRIX else "#2 (accept/revise/reject)"
    return StageResult(human=True, reason=f"awaiting HUMAN REVIEW {which}")


def _h_ingest(state, manifest, run_dir, ctx) -> StageResult:
    return StageResult(blocked=True, reason="INGEST runs at run creation; nothing to drive here")


def _h_oracle(state, manifest, run_dir, ctx) -> StageResult:
    bundle = ctx.get("oracle_bundle")
    bundle_slug = ctx.get("oracle_bundle_slug")
    # A pre-built bundle is SOURCE-SPECIFIC (e.g. /opt/minpack-crate is minpack's reference port). The
    # fleet driver passes ONE global ctx to every run, so a bundle_slug scopes the bundle to the run it
    # belongs to — adopt ONLY when this run is that source. Without this guard EVERY source adopts the
    # same bundle and silently becomes that task (the minpack-contamination bug: protobuf/micropython
    # runs all rendered the minpack instruction.md). A mismatch falls through to generate-mode. (No
    # bundle_slug = a single explicit run that chose this bundle for itself → adopt as before.)
    if bundle and (bundle_slug in (None, "") or manifest.slug == bundle_slug):
        _out, res = adopt_existing(
            manifest, Path(bundle), ctx.get("epsilon", MINPACK_EPSILON),
            epsilon_justification=ctx.get("epsilon_justification", "adopted from reference bundle"),
            capture_method=ctx.get("capture_method"),
        )
        return StageResult(verdict=res.verdict, reason=res.reason)
    # Agentic clean-room generate-mode (opt-in: needs an execution env for the source-WITHOUT a
    # pre-built reference bundle). Generating minpack's 8h reference port stays out of scope (ADR-0016).
    gen_dir = ctx.get("oracle_generate_dir")
    if not (gen_dir or ctx.get("oracle_generate") or ctx.get("agentic")):
        return StageResult(blocked=True, reason="ORACLE+GOLDEN needs a reference bundle to adopt "
                           "(supply oracle_bundle), or an execution env for clean-room generate-mode "
                           "(set oracle_generate_dir / ctx 'agentic')")
    from .cells.oracle_golden import generate  # adopt_existing is imported at module level
    # The bundle dir must NEVER be CREATE's task dir (run_dir/task/<slug>): the vendored
    # ProgramBench build_task rmtree's its output dir first, so a colliding bundle is DESTROYED
    # by the very stage that consumes it (the pb10 wipe: all 8 captured bundles deleted, CREATE
    # then crashed copying the oracle_bin it had just removed). Held-out materials also must not
    # live inside the shipped task tree. Default: a sibling `oracle-bundle/` in the run dir.
    bundle_dir = Path(gen_dir) if gen_dir else (Path(run_dir) / "oracle-bundle")
    eps = ctx.get("epsilon", MINPACK_EPSILON)
    justification = ctx.get("epsilon_justification", "agentic clean-room generate-mode")

    def _snapshot_if_task(m: Manifest):
        # LEGACY generate produced a full rewrite-port TASK in bundle_dir, and task_bundle_path
        # told the sweeps to upload it. A ProgramBench oracle bundle is NOT a task (no task.toml)
        # — pointing task_bundle_path at it would upload the held-out oracle pair + goldens into
        # the agent-visible sweep env. Only record it when the bundle really is a task.
        if (bundle_dir / "task.toml").exists():
            m.snapshot = {**(m.snapshot or {}), "task_bundle_path": str(bundle_dir)}

    if not ctx.get("agentic_background"):   # synchronous path (tests / CLI direct)
        _out, res, _agent = generate(manifest, bundle_dir, eps, session=ctx.get("agent_session"),
                                     max_iters=ctx.get("max_iters", 3))
        if res.verdict == "pass":
            _snapshot_if_task(manifest)
            manifest.save(run_dir)
        return StageResult(verdict=res.verdict, reason=res.reason)

    def _complete():
        # ProgramBench bundle completeness — THE SAME check adopt_existing enforces (ADR-0038 layout:
        # oracle_bin + prebuilt_bin + docs/help.txt + testsuite/cases.json + fixtures/ + determinism
        # marker). Anything weaker/stronger than adopt would either false-block a good bundle (the
        # pb10 stall: generate succeeded, the old legacy oracle/+goldens/ check said incomplete) or
        # apply an incomplete one.
        missing, n_cases, determinism_ok = _bundle_status(bundle_dir)
        unique_outcomes = _bundle_output_diversity(bundle_dir)
        if (not missing and n_cases is not None and n_cases >= MIN_CASES and determinism_ok
                and unique_outcomes is not None
                and unique_outcomes >= _minimum_unique_outcomes(n_cases)):
            return True
        # Legacy rewrite-port bundle drain-through (pre-ADR-0038 layout; adopt_existing detects the
        # shape and adopts it via _adopt_legacy).
        return ((bundle_dir / "oracle").is_dir()
                and (bundle_dir / "goldens" / "goldens_public.json").exists()
                and (bundle_dir / "goldens" / "goldens_heldout.json").exists())

    def _produce():
        m2 = Manifest.load(run_dir)  # disposable: the bg thread must not mutate the loop's manifest
        session = ctx.get("agent_session") or _cell_session(ctx.get("model"))
        _o, res, _a = generate(m2, bundle_dir, eps, session=session,
                               max_iters=ctx.get("max_iters", 3))
        return res.reason

    def _apply():
        _o, res = adopt_existing(manifest, bundle_dir, eps, epsilon_justification=justification,
                                 capture_method="agentic clean-room generate-mode")
        if res.verdict == "pass":
            _snapshot_if_task(manifest)
        return StageResult(verdict=res.verdict, reason=res.reason)

    return _agentic_bg_step("oracle-generate", Path(run_dir),
                            produce=_produce, complete=_complete, apply_result=_apply)


def _h_create(state, manifest, run_dir, ctx) -> StageResult:
    task_dir = Path(run_dir) / "task" / (manifest.slug or "rewrite-task")
    marker = task_dir / ".create-fill-ok"
    # In background-agentic mode: skip skeleton regeneration while the fill agent is live or done.
    # The agent writes files directly; overwriting them each poll cycle would erase its work.
    _skip_regen = False
    if ctx.get("agentic_background"):
        from . import jobs as _jobs
        _job_st = _jobs.get_jobs(Path(run_dir)).get("create-fill", {}).get("status")
        _skip_regen = marker.exists() or _job_st == "running"
    if _skip_regen:
        from types import SimpleNamespace
        out = SimpleNamespace(todos=[])
    else:
        from .llm import CellError
        try:
            # runner/model thread the ONE creative sub-cell (TaskCopy) — everything else in the
            # assembly is the deterministic vendored generator (DESIGN §6.5).
            out = assemble_skeleton(manifest, task_dir,
                                    runner=ctx.get("llm_runner"), model=ctx.get("model"))
        except CellError as e:
            # A missing/legacy oracle bundle or an unvalidatable TaskCopy is an HONEST halt (fix
            # upstream / retry next pass), never a raised exception that aborts the fleet pass.
            return StageResult(blocked=True, reason=f"CREATE cannot assemble: {str(e)[:200]}")
        if not out.todos:
            # Nothing to fill — the ORACLE generate already produced a complete task (no
            # `# TODO(create-fill)` blocks). Do NOT launch a fill agent to flounder on an empty task
            # (it just burns the 30-min session timeout); advance and let SANITY validate the
            # skeleton — a real defect surfaces there and routes to SYNTHESIZE.
            return StageResult(verdict="pass", reason="skeleton already complete (0 TODO fill-points); "
                               "SANITY validates downstream")
    # Agentic fill of the `# TODO(create-fill)` blocks (opt-in: needs an execution env). Default
    # drive only assembles the STATIC-clean skeleton and lets SANITY/STATIC validate downstream.
    if not (ctx.get("agentic_fill") or ctx.get("agentic")):
        return StageResult(verdict="pass", reason=f"assembled hybrid skeleton ({len(out.todos)} TODO fill-points)")
    from .cells.create import agentic_fill

    if not ctx.get("agentic_background"):   # synchronous path (tests / CLI direct)
        res = agentic_fill(manifest, task_dir, session=ctx.get("agent_session"),
                           validator=ctx.get("validator"), max_iters=ctx.get("max_iters", 3))
        if not res.success:
            return StageResult(blocked=True, reason=f"CREATE agentic fill incomplete: {res.reason}")
        return StageResult(verdict="pass", reason=res.reason)

    marker = task_dir / ".create-fill-ok"  # success sentinel (fill validates oracle=1/nop=0, not just file presence)

    def _produce():
        m2 = Manifest.load(run_dir)  # disposable copy; bg thread must not mutate the loop's manifest
        assemble_skeleton(m2, task_dir, runner=ctx.get("llm_runner"), model=ctx.get("model"))
        session = ctx.get("agent_session") or _cell_session(ctx.get("model"))
        res = agentic_fill(m2, task_dir, session=session,
                           validator=ctx.get("validator"), max_iters=ctx.get("max_iters", 3))
        if not res.success:
            raise RuntimeError(res.reason)
        marker.write_text(res.reason)
        return res.reason

    return _agentic_bg_step("create-fill", Path(run_dir),
                            produce=_produce, complete=marker.exists,
                            apply_result=lambda: StageResult(verdict="pass",
                                reason=f"create fill validated oracle=1/nop=0 ({marker.read_text()[:80]})"))


def _h_sanity(state, manifest, run_dir, ctx) -> StageResult:
    # Docker-less path (ADR-0017): satisfy oracle=1/nop=0 from recorded baseline trials if present.
    baseline = (manifest.sweeps or {}).get("sanity") or {}
    trials = baseline.get("trials")
    if trials:
        res = run_sanity_trials(trials)
        return StageResult(verdict=res.verdict, reason=res.reason)
    task_dir = Path(run_dir) / "task" / (manifest.slug or "rewrite-task")
    if _docker_ok():
        res = run_sanity(task_dir, image_tag=f"lh-sanity:{manifest.slug or 'task'}")
        return StageResult(verdict=res.verdict, reason=res.reason)
    return StageResult(blocked=True, reason="SANITY needs local Docker (the two-phase oracle=1/nop=0 "
                       "+ priv-drop verifier), or recorded oracle/nop baseline trials — import them "
                       "with `programsmith sweep-read --kind sanity --from-pull <dir>` and re-drive (ADR-0017)")


def _h_static(state, manifest, run_dir, ctx) -> StageResult:
    from .gates.static_ci import vendored_ci_dir
    override = ctx.get("ci_repo_root")
    if override and not (Path(override) / "ci_checks").exists():
        return StageResult(blocked=True, reason=f"STATIC CI: ci_repo_root override {override!r} has "
                           "no ci_checks/ — fix or unset it to use the vendored in-tree suite")
    import shutil
    slug = manifest.slug or "rewrite-task"
    task_dir = Path(run_dir) / "task" / slug
    if not (task_dir / "task.toml").exists():
        return StageResult(blocked=True, reason=f"STATIC CI: this run's task isn't assembled at {task_dir} "
                           "(finish CREATE first)")
    # Stage into a WRITABLE area — NEVER mutate the read-only check source. Copy the check suite
    # (vendored in-tree by default, an operator checkout's ci_checks/ when overridden) and overlay
    # THIS run's task at tasks/<slug>, then run the checks against it (cwd = the staging root).
    # ABSOLUTE — run_static_ci builds script paths from repo_root and runs them with cwd=repo_root,
    # so a relative root would resolve the scripts against the cwd (double path → rc=127 not-found).
    staging = (Path(run_dir) / ".staticci").resolve()
    shutil.rmtree(staging, ignore_errors=True)
    (staging / "tasks").mkdir(parents=True)
    if override:
        for entry in Path(override).iterdir():
            if entry.name in ("tasks", ".git"):
                continue
            # COPY ci_checks (real dir) so a check that resolves its own dir via $0 lands in THIS
            # staging root, not back through a symlink into the ground-truth checkout. Symlink the
            # rest (shared files the scripts may read, read-only ref).
            if entry.name == "ci_checks":
                shutil.copytree(entry, staging / entry.name)
            else:
                (staging / entry.name).symlink_to(entry.resolve())
    else:
        shutil.copytree(vendored_ci_dir(), staging / "ci_checks")
    shutil.copytree(task_dir, staging / "tasks" / slug)
    res = run_static_ci(staging, f"tasks/{slug}")
    return StageResult(verdict=res.verdict, reason=res.reason)


def _label_breakdown(analyses: list[dict]) -> dict:
    """Count TrialClassifier labels → {LABEL: n}. Pure tally (no LLM)."""
    out: dict[str, int] = {}
    for a in analyses:
        lbl = str(a.get("label", "?")).upper()
        out[lbl] = out.get(lbl, 0) + 1
    return out


def _analysis_summary(breakdown: dict) -> str:
    n = sum(breakdown.values())
    parts = ", ".join(f"{v} {k}" for k, v in sorted(breakdown.items(), key=lambda kv: -kv[1]))
    spurious = sum(breakdown.get(l, 0) for l in _SPURIOUS_LABELS)
    gamed = sum(breakdown.get(l, 0) for l in _GAMED_LABELS)
    flags = []
    if spurious:
        flags.append(f"⚠ {spurious} spurious failure(s) — band may understate easiness")
    if gamed:
        flags.append(f"⚠ {gamed} gamed success(es) — verifier may be gameable")
    return f"analysis: {n} trial(s) [{parts}]" + ("; " + "; ".join(flags) if flags else "")


def _attach_analysis(stage_key: str, manifest, ctx, *, agents: tuple[str, ...]) -> StageResult | None:
    """Attach the per-trial TrialClassifier labels (`--run-analysis`) of the CONFIGURED frontier
    family/families to a completed sweep entry, so the downstream gate — CALIBRATE at smoke, the
    frontier decision + good-failure gate at full — can tell a GENUINE miss (GOOD_FAILURE) from a
    spurious one (HARNESS_ERROR/BAD_FAILURE) or a gamed pass (BAD_SUCCESS). Returns a blocked
    StageResult while the classifier is still PENDING (bounded wait), or None once the labels are
    recorded (caller proceeds to its decision). The classifier is ADVISORY: it is recorded + routed
    on deterministically, but the reward-based pass@1 still drives the band (determinism-sandwich)."""
    entry = manifest.sweeps[stage_key]
    exp = entry.get("experiment")
    backend = _resolve_backend(ctx)
    try:
        a = backend.analyses(exp, agents=agents)
    except Exception as e:  # noqa: BLE001 — a classifier-fetch hiccup must not wedge the run
        entry["analysis"] = {"status": "unavailable", "error": str(e)[:120]}
        entry["analysis_summary"] = "analysis unavailable (fetch error) — proceeding on the band"
        return None
    polls = entry.get("analysis_polls", 0)
    if a["pending"] and polls < _ANALYSIS_MAX_POLLS:
        entry["analysis_polls"] = polls + 1
        return StageResult(blocked=True, reason=f"{stage_key}: TrialClassifier running on {exp} "
                           f"({a['pending']} trial(s) pending, poll {polls + 1}/{_ANALYSIS_MAX_POLLS})")
    breakdown = _label_breakdown(a["analyses"])
    entry["analysis"] = {"labels": a["analyses"], "breakdown": breakdown,
                         "pending": a["pending"], "failed": a["failed"], "total": a["total"]}
    if a["pending"]:   # exhausted the generous wait with trials still unclassified — proceed, but LOUD
        entry["analysis_summary"] = (
            f"⚠ trajectory analysis INCOMPLETE: {a['pending']}/{a['total']} trial(s) still unclassified "
            f"after {_ANALYSIS_MAX_POLLS} polls — good/bad-failure unknown for those (investigate the trials)")
    else:
        entry["analysis_summary"] = _analysis_summary(breakdown) if breakdown else "analysis: no labels returned"
    return None


def _difficulty_attach_analysis(manifest, run_dir, ctx) -> StageResult:
    """Phase 2 of the SMOKE sweep: attach the configured smoke harness's labels, then advance."""
    from .runconfig import effective_run_config
    entry = manifest.sweeps["difficulty"]
    waiting = _attach_analysis("difficulty", manifest, ctx,
                               agents=_analysis_agents(effective_run_config(manifest).difficulty))
    if waiting is not None:
        return waiting
    return StageResult(verdict="done", reason=f"difficulty pass@1={entry.get('pass_at_1')}; "
                       f"{entry.get('analysis_summary', 'analysis recorded')}")


def _h_difficulty(state, manifest, run_dir, ctx) -> StageResult:
    entry = (manifest.sweeps or {}).get("difficulty") or {}
    # A band is present (a real sweep just finished, or one was pre-recorded) and the sweep isn't still
    # running → don't re-launch. If it came from a LIVE experiment, attach the --run-analysis labels
    # before CALIBRATE; a pre-recorded band (no experiment to classify) just advances.
    if entry.get("status") != "running" and _parse_pass_at_1(manifest) is not None:
        if entry.get("status") == "done" and entry.get("experiment") and "analysis" not in entry:
            return _difficulty_attach_analysis(manifest, run_dir, ctx)
        tail = f"; {entry['analysis_summary']}" if entry.get("analysis_summary") else "; result present"
        return StageResult(verdict="done", reason=f"difficulty pass@1={entry.get('pass_at_1')}{tail}")
    from .trials import pass_at_1
    from .runconfig import effective_run_config

    def _finalize(trials):
        pa = pass_at_1(trials)
        return {"pass_at_1": pa["aggregate"], "groups": pa["groups"],
                "summary": f"pass@1={pa['aggregate']}"}

    res = _sweep_step(
        "difficulty", manifest, run_dir, ctx,
        agents=_stage_sweep_agents(effective_run_config(manifest).difficulty),
        experiment_suffix="difficulty", finalize=_finalize, verdict_of=lambda _e: "done",
        extra_flags=["--run-analysis"],  # classify each frontier trial (good/bad × success/failure)
        generation=state.harden + state.revise + state.ease)
    # If THIS pass completed the sweep, don't advance yet — attach analysis first (so CALIBRATE sees it).
    if (manifest.sweeps.get("difficulty") or {}).get("status") == "done":
        return _difficulty_attach_analysis(manifest, run_dir, ctx)
    return res


def _harden_gate(manifest, *, stage: str, pass_at_1, breakdown, harden_count: int, ctx: dict):
    """Run the HARDEN REVIEW auditor (gates.harden_review) and RECORD this saturated generation into
    manifest.harden_history so the next generation's review sees the trajectory. Returns the
    GateResult (verdict 'harden' | 'drop'). Called only when a stage would otherwise harden."""
    from .gates.harden_review import harden_review
    history = [h["pass_at_1"] for h in (manifest.harden_history or [])
              if isinstance(h.get("pass_at_1"), (int, float))]
    review = harden_review(
        pass_at_1, history=history, harden_count=harden_count, breakdown=breakdown,
        drop_after=ctx.get("harden_drop_after", 3),
        min_improvement=ctx.get("harden_min_improvement", 0.10))
    manifest.harden_history = [*(manifest.harden_history or []), {
        "stage": stage, "generation": harden_count, "pass_at_1": pass_at_1,
        "verdict": review.verdict, "breakdown": breakdown or {}}]
    return review


def _h_calibrate(state, manifest, run_dir, ctx) -> StageResult:
    """SMOKE decision (ADR-0040). The gate (gates.calibrate) decides deterministically on the band
    + TrialClassifier labels; this handler only overlays the harden-review auditor on a SATURATION
    harden — and, per the new doctrine, maps a review "drop" (too easy to harden / not converging)
    to PROCEED with `smoke_saturated=true`: the smoke model is never the authority, so a task GLM
    aces still gets its frontier measurement (Opus decides shelf-vs-keep). "Too easy" is NEVER a
    drop at CALIBRATE anymore. The decision is recorded into manifest.sweeps["difficulty"]."""
    from .runconfig import band_verdict as rc_band_verdict
    from .runconfig import effective_run_config
    band = effective_run_config(manifest).difficulty.band
    diff = (manifest.sweeps or {}).get("difficulty") or {}
    pa = _band_from_entry(diff, band.basis)
    if pa is None:
        pa = _parse_pass_at_1(manifest)
    analysis = diff.get("analysis") or {}
    breakdown = analysis.get("breakdown") or {}
    labels = [str(a.get("label", "?")) for a in (analysis.get("labels") or [])]
    # band_verdict carries the per-model combinator policy (any/all) when configured; None (no
    # per-agent groups — a legacy/injected scalar entry) falls back to the gate's scalar ceiling.
    bv = rc_band_verdict(diff.get("groups"), band)
    res = calibrate(pa, saturate_above=band.max_pass, band_verdict=bv,
                    breakdown=breakdown, labels=labels)
    out = StageResult(verdict=res.verdict, reason=f"calibrate: {res.reason}")
    if res.verdict == "harden" and (res.detail or {}).get("kind") == "saturation":
        # Saturation hardens go through the harden-review auditor (reward-hack hardens do NOT —
        # a verifier hole must be closed regardless of convergence history).
        review = _harden_gate(manifest, stage="CALIBRATE", pass_at_1=pa, breakdown=breakdown,
                              harden_count=state.harden, ctx=ctx)
        if review.verdict == "drop":
            entry = manifest.sweeps.setdefault("difficulty", {})
            entry["smoke_saturated"] = True   # QA/frontier context: GLM aced it, hardening futile
            out = StageResult(verdict="proceed", reason=(
                f"calibrate: smoke saturated and hardening not converging ({review.reason}) — "
                "proceeding to the frontier anyway (measure, don't predict; Opus decides shelf)"))
        else:
            out = StageResult(verdict="harden", reason=f"calibrate: {res.reason}; {review.reason}")
    # Record the smoke decision for QA_GATE/UI provenance (DESIGN §4.1).
    entry = manifest.sweeps.setdefault("difficulty", {})
    entry["calibrate"] = {"verdict": out.verdict, "reason": out.reason,
                          "labels_summary": _analysis_summary(breakdown) if breakdown else "no labels"}
    return out


def _recorded_band_verdict(entry: dict, band) -> str | None:
    """The frontier window classification for a recorded sweep entry: "keep" | "too_easy" |
    "too_hard" | None (nothing measured). Prefers the per-agent groups (runconfig.band_verdict —
    carries the per-model combinator policy); falls back to the legacy scalar basis fields for
    older/injected entries. The floor is REAL only when min_pass>0 (the default frontier band sets
    0.30, so 0/3 reads too_hard and must earn its keep through the good-failure gate)."""
    from .runconfig import band_verdict
    v = band_verdict(entry.get("groups"), band)
    if v is not None:
        return v
    val = _band_from_entry(entry, band.basis)
    if not isinstance(val, (int, float)):
        return None
    if val > band.max_pass:
        return "too_easy"
    if band.min_pass > 0 and val < band.min_pass:
        return "too_hard"
    return "keep"


def _goodfail_frontier(state, manifest, entry, ctx, *, on_too_hard: str = "keep_verified_hard") -> StageResult:
    """The FRONTIER good-failure gate (ADR-0041, hardened per ADR-0048) for a too-hard band.

    The deep trajectory audit — OUR OWN LLM reading the failed trials' actual transcripts against
    the task's stated spec — is MANDATORY for keep_hard. TrialClassifier labels are a
    PREFILTER only: a BAD_FAILURE label still short-circuits to ease (a known defect is a defect),
    but an all-GOOD_FAILURE label set is no longer sufficient evidence to ship a 0-pass task — the
    classifier is a third-party annotation that never read the instruction text it's vouching for.
    Verified headroom must come from the audit (schema-validated, full-coverage-guarded), once per
    generation (cached in manifest.sweeps["full"]["goodfail"]; a new generation re-audits).

    `on_too_hard` (BandSpec, ADR-0048) decides what verified headroom is WORTH:
      * keep_verified_hard (default) → done + hard_keep (QA_GATE accepts a verified-hard task)
      * enforce_window               → EASE toward the window (bounded tune budget; exhausted →
                                       DROPPED by the FSM) — the window is a hard contract and a
                                       0% task never ships, however genuine its difficulty.
    ease → the bounded frontier ease tune; revise → env fix (both policies)."""
    from .goodfail import audit_gate, deep_audit, label_gate
    gen = state.harden + state.revise + state.ease
    analysis = entry.get("analysis") or {}
    label_records = analysis.get("labels") or []
    labels = [str(a.get("label", "?")) for a in label_records]
    # pending = trials the classifier hasn't labelled yet — only used to fail FAST on a visible
    # BAD_FAILURE; keep_hard never comes from labels anymore.
    pending = int(analysis.get("pending") or 0)
    gf = label_gate(labels, pending=pending)
    if gf["verdict"] == "ease":     # a labelled defect needs no audit to act on
        verdict, reason = "ease", gf["reason"]
        entry["goodfail"] = {"generation": gen, "verdict": verdict, "reason": reason,
                             "source": "label_gate"}
    else:
        cached = entry.get("goodfail") or {}
        if cached.get("generation") == gen and cached.get("verdict") and cached.get("source") != "label_gate":
            verdict, reason = cached["verdict"], f"cached deep audit: {cached.get('reason', '')}"
        else:
            attempts = entry.get("goodfail_attempts", 0)
            if attempts >= 3:
                return StageResult(blocked=True, reason=(
                    "frontier zero/low-pass and the deep audit failed "
                    f"{attempts}× — NOT advanced (a 0-pass task ships only on audit-verified "
                    "headroom); investigate the trajectories"))
            try:
                report = deep_audit(entry.get("pull_dir") or "", label_records,
                                    runner=ctx.get("llm_runner"),
                                    model=ctx.get("model") or getattr(_lh_config(), "cell_model_analysis", None))
            except Exception as e:  # noqa: BLE001 — a cell failure blocks honestly, bounded above
                entry["goodfail_attempts"] = attempts + 1
                return StageResult(blocked=True, reason=(
                    f"frontier good-failure deep audit errored ({str(e)[:100]}) — retry "
                    f"{attempts + 1}/3 next pass"))
            # keep_hard requires the audit to have covered EVERY failed trial (invariant #4 — the
            # LLM must not shrink the trial set to manufacture an all-headroom verdict).
            expected_ids = [str(a.get("trial_id")) for a in label_records if a.get("trial_id")]
            ag = audit_gate(report, expected_trial_ids=expected_ids or None)
            verdict, reason = ag["verdict"], ag["reason"]
            entry["goodfail"] = {"generation": gen, "verdict": verdict, "reason": reason,
                                 "source": "deep_audit", "labels_prefilter": gf["verdict"],
                                 "report": report.model_dump()}
    if verdict == "keep_hard":
        if on_too_hard == "enforce_window":
            return StageResult(verdict="ease", reason=(
                f"frontier below the band window; failures ARE audit-verified headroom ({reason}) "
                "but this run enforces the window (on_too_hard=enforce_window) — easing toward it"))
        entry["hard_keep"] = True   # QA_GATE reads this: a verified-hard task is acceptable at 0/N
        return StageResult(verdict="done", reason=f"frontier below band but failures are audit-verified "
                           f"capability headroom ({reason}) — KEPT as a hard task")
    if verdict == "revise":
        return StageResult(verdict="revise", reason=f"frontier zero/low-pass on an environment "
                           f"defect ({reason}) — fix the env/verifier")
    return StageResult(verdict="ease", reason=f"frontier zero/low pass with task-design blockers "
                       f"({reason}) — ease (remove universal blockers, keep semantic depth)")


def _full_sweep_decide(state, manifest, entry, band, ctx) -> StageResult:
    """Turn a recorded frontier band into a verdict (DESIGN §4.2 order). Shared by the
    already-recorded branch AND the just-finalized branch so no gate is ever bypassed:
      1. integrity fail (oracle≠1/nop≠0)            → revise
      2. any BAD_SUCCESS frontier label             → revise (a gameable verifier NEVER ships —
                                                      fix it, don't tune difficulty around it)
      3. band keep (Opus 1/3–2/3)                   → done
         band too_easy (> 0.70)                     → harden-review: drop-recommendation → SHELVE
                                                      (EASY_SHELF — a good-but-easy task is kept),
                                                      else harden (FSM bounds FRONTIER_TUNE_MAX)
         band too_hard (< 0.30, incl. 0/3)          → good-failure gate: keep_hard → done+hard_keep |
                                                      ease | revise (env)"""
    integ = entry.get("integrity") or {}
    if integ.get("verdict") == "fail":
        return StageResult(verdict="revise", reason="full sweep integrity broken — "
                           f"oracle≠1/nop≠0 on the authoritative band ({integ.get('reason')})")
    breakdown = (entry.get("analysis") or {}).get("breakdown") or {}
    gamed = sum(breakdown.get(l, 0) for l in _GAMED_LABELS)
    if gamed:
        return StageResult(verdict="revise", reason=(
            f"reward-hack on the frontier: {gamed} BAD_SUCCESS trial(s) — the verifier is gameable; "
            "fix it before shipping (never tune difficulty over a hackable reward)"))
    pa = _band_from_entry(entry, band.basis)
    v = _recorded_band_verdict(entry, band)
    entry["band_verdict"] = v   # recorded for QA_GATE + provenance
    if v == "too_easy":
        review = _harden_gate(manifest, stage="FULL_SWEEP", pass_at_1=pa,
                              breakdown=breakdown or None, harden_count=state.harden, ctx=ctx)
        if review.verdict == "drop":
            # "Too easy to harden" is a SHELF, not a trash can (ADR-0040): the task is good, the
            # frontier just aces it — keep it on the easy shelf.
            return StageResult(verdict="shelve", reason=f"frontier saturated; too easy to harden "
                               f"({review.reason}) — shelved as easy")
        return StageResult(verdict="harden", reason=f"frontier pass@1={pa} above the band → harden "
                           f"({review.reason})")
    if v == "too_hard":
        return _goodfail_frontier(state, manifest, entry, ctx,
                                  on_too_hard=getattr(band, "on_too_hard", "keep_verified_hard"))
    if v is None:
        return StageResult(verdict="done", reason="frontier sweep recorded no measurable band — "
                           "deferring to QA_GATE (it rejects unmeasured evidence)")
    return StageResult(verdict="done", reason=f"frontier band pass@1={pa} in the target window")


def _h_full_sweep(state, manifest, run_dir, ctx) -> StageResult:
    from .runconfig import effective_run_config
    cfg = effective_run_config(manifest).full
    entry = (manifest.sweeps or {}).get("full") or {}
    # A recorded result is acted on ONLY if it actually measured a band (a real `done`, or an injected
    # band with a non-None aggregate). An `errored` entry (all trials errored) must fall through to
    # the state machine to re-run — never treat it as a measured band.
    if entry.get("status") == "done" or (entry.get("status") != "errored" and entry.get("aggregate") is not None):
        # Attach the frontier TrialClassifier labels before deciding (the decision routes on
        # BAD_SUCCESS + the good-failure gate needs GOOD/BAD_FAILURE). Only a LIVE experiment has
        # labels to fetch; injected/pre-recorded bands decide on whatever analysis they carry.
        if entry.get("status") == "done" and entry.get("experiment") and "analysis" not in entry:
            waiting = _attach_analysis("full", manifest, ctx, agents=_analysis_agents(cfg))
            if waiting is not None:
                return waiting
            entry = manifest.sweeps["full"]
        return _full_sweep_decide(state, manifest, entry, cfg.band, ctx)
    from .trials import family_band, pass_at_1
    from .runconfig import band_value

    def _finalize(trials):
        from .gates.sanity import run_sanity_trials
        pa = pass_at_1(trials)  # per-agent groups so any configured basis (mini-swe/gemini/…) resolves
        agg = band_value(pa["groups"], "aggregate")
        out = {"pass_at_1": agg, "groups": pa["groups"],
               "band_verdict": _recorded_band_verdict({"groups": pa["groups"]}, cfg.band),
               "summary": f"pass@1={agg}"}
        # N-family read (families map + max pairwise fairness gap) whenever any family measured;
        # the legacy dual-family fields (claude_code/codex) ride along for readers of the old shape.
        fb = family_band(trials)
        if fb["families"]:
            out["families"] = fb["families"]
            out["fairness_gap"] = fb["fairness_gap"]
            if (cc := fb["families"].get("claude-code")) is not None:
                out["claude_code"] = cc
            if (cx := fb["families"].get("codex")) is not None:
                out["codex"] = cx
            fam_str = " ".join(f"{k}={v}" for k, v in sorted(fb["families"].items()))
            out["summary"] = f"{fam_str} agg={agg}"
        # Authoritative oracle=1/nop=0 integrity on the REAL (closed-internet) band. The baselines are
        # excluded from the difficulty band, so record their verdict here — a broken verifier/environment
        # (oracle didn't pass / nop passed) is NOT a difficulty signal and must route to REVISE, not
        # advance to QA_GATE. Only assessed when the sweep actually measured the baselines.
        if any(t.get("agent") in ("oracle", "nop") for t in trials):
            integ = run_sanity_trials(trials)
            out["integrity"] = {"verdict": integ.verdict, "reason": integ.reason}
        return out

    res = _sweep_step(
        "full", manifest, run_dir, ctx,
        agents=_stage_sweep_agents(cfg),
        experiment_suffix="full", finalize=_finalize, verdict_of=lambda _e: "done",
        extra_flags=["--run-analysis"],  # classify every frontier trajectory (goodfail + reward-hack)
        generation=state.harden + state.revise + state.ease)
    # If THIS pass finalized the sweep, re-decide via the band + gates (don't advance on the raw
    # step verdict — that would skip the harden-review auditor and the good-failure gate).
    if (manifest.sweeps.get("full") or {}).get("status") == "done":
        if manifest.sweeps["full"].get("experiment") and "analysis" not in manifest.sweeps["full"]:
            waiting = _attach_analysis("full", manifest, ctx, agents=_analysis_agents(cfg))
            if waiting is not None:
                return waiting
        return _full_sweep_decide(state, manifest, manifest.sweeps["full"], cfg.band, ctx)
    return res


def _h_qa_probe(state, manifest, run_dir, ctx) -> StageResult:
    """QA/PROBE — run the Task Construction Auditor as a normal task trial: the frontier model, ONE
    trial, with the auditor prompt PREPENDED to the task's `instruction.md`, original kept below
    (programsmith.probes — the same overlay harbor's `/cheat` uses). The auditor emits
    a JSON verdict; the gate maps SOLVABLE_AS_WRITTEN with no blocker findings → `clean`, and a
    gameable verdict (SOLVABLE_ONLY_BY_GUESSING / UNSOLVABLE) or any blocker finding → `harden`. Real
    only — the verdict is the agent's own output; the experiment is recorded + linked. Driven
    repeatedly: launch → poll → read verdict."""
    import shutil
    from . import trials as od
    from .probes import GAMEABLE_VERDICTS, TASK_CONSTRUCTION_AUDITOR
    from .sweepbackend import sweep_live
    entry = (manifest.sweeps or {}).get("qa_probe") or {}
    if entry.get("verdict") in ("clean", "harden"):
        return StageResult(verdict=entry["verdict"], reason=f"QA/PROBE {entry['verdict']}: {entry.get('summary', '')}")
    backend = _resolve_backend(ctx)
    if entry.get("status") == "running":
        exp = entry["experiment"]
        if sweep_live(ctx) and hasattr(backend, "resume"):
            try:
                backend.resume(exp)
            except Exception as e:  # noqa: BLE001
                return StageResult(blocked=True, reason=(
                    f"QA/PROBE: could not resume interrupted {backend.name} probe {exp} "
                    f"({str(e)[:100]})"))
        try:
            status = backend.status(exp)
        except Exception as e:  # noqa: BLE001 — a transient poll error must not crash the driver
            return StageResult(blocked=True, reason=f"QA/PROBE: polling {exp} (status error: {str(e)[:80]})")
        if not status["complete"]:
            if status.get("incomplete"):
                return StageResult(blocked=True, reason=(
                    f"QA/PROBE auditor is interrupted on {backend.name} ({exp}); re-run the "
                    "create/farm command and confirm spend to resume it"))
            return StageResult(blocked=True, reason=f"QA/PROBE (auditor) running on {backend.name} ({exp}): "
                               f"{status['tasks_running']} task(s) running")
        out_dir = Path(run_dir) / backend.artifact_subdir / exp
        backend.pull_artifacts(exp, out_dir)
        info = od.extract_auditor_verdict(out_dir)
        if not info["found"]:
            manifest.sweeps["qa_probe"] = {"status": "complete_unparsed", "experiment": exp,
                                           "pull_dir": str(out_dir)}
            return StageResult(blocked=True, reason=f"QA/PROBE auditor complete on {backend.name} ({exp}) but no "
                               "JSON verdict parsed — review the trajectory (verdict not invented)")
        gameable = info["verdict"] in GAMEABLE_VERDICTS or info["blockers"] > 0
        verdict = "harden" if gameable else "clean"
        manifest.sweeps["qa_probe"] = {
            "status": "done", "experiment": exp, "pull_dir": str(out_dir), "verdict": verdict,
            "auditor_verdict": info["verdict"], "blocker_findings": info["blockers"],
            "summary": f"auditor: {info['verdict']}, {info['blockers']} blocker finding(s)"}
        return StageResult(verdict=verdict, reason=f"QA/PROBE {verdict} on {backend.name} ({exp}): auditor "
                           f"{info['verdict']}, {info['blockers']} blocker(s)")
    if not sweep_live(ctx):
        return StageResult(blocked=True, reason="QA/PROBE needs a live auditor run (billable on cloud) — "
                           "enable ctx 'sweep_live' to auto-launch")
    bundle = _task_bundle(manifest, run_dir, ctx)
    if bundle is None or not bundle.exists():
        return StageResult(blocked=True, reason="QA/PROBE: no complete task bundle to probe "
                           "(set manifest.snapshot.task_bundle_path or finish CREATE fill)")
    # Build the auditor probe bundle: a copy of the task with the Task Construction Auditor prompt
    # PREPENDED to instruction.md (the original task instruction kept verbatim below) — the same
    # overlay harbor's `/cheat` uses. Prepending (not overwriting) matters: STEP 1 of the auditor is
    # "read instruction.md", so the auditor must still see the real instruction it is grading; a
    # literal solver reading top-down hits the auditor role first. A per-generation task-dir name +
    # deterministic experiment name (no timestamp) keep each generation a distinct uploaded task while
    # staying human-readable across generations.
    from datetime import datetime, timezone
    slug = manifest.slug or "rewrite-task"
    gen = state.harden + state.revise + state.ease
    probe_dir = Path(run_dir) / "probe-task" / od.probe_task_dirname(slug, generation=gen)
    if probe_dir.exists():
        shutil.rmtree(probe_dir)                 # a prior generation's overlay — rebuild from the task
    probe_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(bundle, probe_dir)
    instr = probe_dir / "instruction.md"
    original = instr.read_text() if instr.exists() else ""
    instr.write_text(TASK_CONSTRUCTION_AUDITOR + (f"\n\n{original}" if original else ""))
    # Stage a CLEAN, size-checked bundle — the auditor probe runs over a staged copy just like a sweep,
    # so it must strip held-out/build artifacts (anti-hack + size) and fail FAST on an oversized/
    # rejected bundle with a CLEAN BLOCK, not a raised exception that crashes the whole drive pass
    # (_sweep_step guards launches the same way).
    upload = _sweep_upload_bundle(probe_dir, Path(run_dir), f"{slug}-probe")
    if backend.needs_upload and (oversized := _bundle_too_large(upload)):
        manifest.sweeps["qa_probe"] = {"status": "errored", "experiment": None,
                                       "summary": f"probe bundle too large to upload: {oversized}"}
        return StageResult(blocked=True, reason=f"QA/PROBE: probe bundle is {oversized} — too large for the "
                           "upload; shrink the task's oracle battery (small/representative inputs) and re-run.")
    try:
        exp = backend.launch(upload, od.auditor_probe_agents(),
                             experiment=od.experiment_name(slug, "probe", generation=gen),
                             extra_flags=_launch_extra_flags(ctx, backend))  # --force-build (ADR-0043)
    except Exception as e:  # noqa: BLE001 — an upload/launch failure is a clean block, NOT a drive crash
        permanent = _is_permanent_launch_error(str(e))
        manifest.sweeps["qa_probe"] = {"status": "errored", "experiment": None,
                                       "summary": f"QA/PROBE launch failed{' (permanent)' if permanent else ''}: {str(e)[:160]}"}
        return StageResult(blocked=True, reason=f"QA/PROBE: {backend.name} launch failed"
                           f"{' with a PERMANENT error — fix the cause, not retrying' if permanent else ' — will retry'}"
                           f" ({str(e)[:120]})")
    manifest.sweeps["qa_probe"] = {"status": "running", "experiment": exp, "kind": "auditor",
                                   "launched_at": datetime.now(timezone.utc).isoformat()}
    return StageResult(blocked=True, reason=f"launched QA/PROBE auditor on {backend.name} ({exp}); polling each pass")


def _h_qa_on_gpt(state, manifest, run_dir, ctx) -> StageResult:
    """LEGACY drain (ADR-0039): the GPT-family QA pass was folded into the frontier analysis gate
    (_full_sweep_decide routes on BAD_SUCCESS/BAD_FAILURE for EVERY configured family). Kept only so
    pre-ADR-0039 state files at QA_ON_GPT drain forward — advances immediately, reads nothing."""
    return StageResult(verdict="done",
                       reason="legacy stage — auto-completes (QA-on-GPT folded into the frontier "
                              "analysis gate, ADR-0039)")


def _synth_trigger(state) -> tuple[str, str, str]:
    """Derive (move, from_stage, reason) for the patch from the history event that routed INTO
    SYNTHESIZE. The move maps 1:1 from the verdict: harden→harden, ease→ease (ADR-0040
    bidirectional tuning), everything else (fail / revise) → a 'revise' patch."""
    for ev in reversed(state.history):
        if ev.next is Stage.SYNTHESIZE:
            move = ev.verdict if ev.verdict in ("harden", "ease", "revise") else "revise"
            return move, ev.stage.value, ev.reason
    return "revise", "STATIC_CI", "patch the task"


# Empirical measurements a SYNTHESIZE patch invalidates: the task changed, so every prior sweep is
# stale and MUST be dropped, or the rejoin (STATIC_CI → … → FULL_SWEEP) short-circuits on the old band
# and the tune loop spins without ever re-measuring (ADR-0024). "qa_gpt" is gone with the stage.
_STALE_ON_PATCH = ("difficulty", "full", "qa_probe")


def _per_case_findings(band_entry: dict) -> dict | None:
    """Per-case pass/fail aggregates parsed from the pulled sweep artifacts (DESIGN §6.6): a
    `diff_<case>.txt` in a trial's verifier output means THAT case failed for THAT trial, so a case
    whose diff appears in EVERY trial is a UNIVERSAL BLOCKER candidate (the prime ease target) and
    the per-case tallies tell a harden which families are already covered. metrics.json `by_case`
    maps are folded in when present. Best-effort, read-only; None when nothing is parseable."""
    pull = band_entry.get("pull_dir")
    if not pull or not Path(pull).exists():
        return None
    fails: dict[str, int] = {}
    n_trial_dirs = 0
    for res_file in sorted(Path(pull).rglob("result.json")):
        trial_dir = res_file.parent
        n_trial_dirs += 1
        for d in trial_dir.rglob("diff_*.txt"):
            case = d.name[len("diff_"):-len(".txt")]
            fails[case] = fails.get(case, 0) + 1
        m = trial_dir / "metrics.json"
        if m.exists():
            try:
                by_case = (json.loads(m.read_text()) or {}).get("by_case") or {}
                for case, ok in by_case.items():
                    if not ok:
                        fails[case] = fails.get(case, 0) + 1
            except Exception:  # noqa: BLE001 — a malformed metrics file must not abort the patch
                pass
    if not fails:
        return None
    universal = sorted(c for c, n in fails.items() if n_trial_dirs and n >= n_trial_dirs)
    top = sorted(fails.items(), key=lambda kv: (-kv[1], kv[0]))[:20]
    return {"n_trials": n_trial_dirs, "universal_blockers": universal,
            "top_failing": [f"{c}×{n}" for c, n in top]}


def _saturation_findings(manifest: Manifest, task_dir: Path) -> list[dict]:
    """Unblind a `saturation` harden: describe HOW saturated the task is and WHICH fair, identity-
    preserving ProgramBench difficulty levers apply (DESIGN §6.6), so the SYNTHESIZE LLM patches
    with evidence instead of guessing. Pure read-only derivation from the recorded band + pulled
    artifacts — no scope change is ever proposed."""
    from .runconfig import effective_run_config
    sw = manifest.sweeps or {}
    band = sw.get("full") or sw.get("difficulty") or {}
    ceiling = effective_run_config(manifest).full.band.max_pass
    pa = band.get("pass_at_1")
    if not isinstance(pa, (int, float)):
        pa = band.get("claude_code")
    findings: list[dict] = []
    if isinstance(pa, (int, float)):
        sev = ("severe (frontier solves nearly every trial — needs a substantial difficulty increase)"
               if pa >= _SEVERE_SATURATION else "moderate" if pa >= 0.80 else
               "borderline (just over the band — a small, targeted increase may suffice)")
        findings.append({"kind": "saturation", "detail":
            f"frontier pass@1={pa:.2f}, {pa - ceiling:+.2f} over the {ceiling:.2f} band ceiling — "
            f"severity: {sev}. Raise the correctness bar with fair, identity-preserving levers; "
            "do NOT change the tool or the graded flag surface."})
    # TrialClassifier breakdown (--run-analysis): WHY the frontier passed. GOOD_SUCCESS = honest
    # solve (genuinely too easy → deepen); BAD_SUCCESS = gamed verifier (a reward-hack — close the
    # hole structurally, not just more cases).
    bd = (band.get("analysis") or {}).get("breakdown") or {}
    if bd:
        gamed = sum(bd.get(l, 0) for l in _GAMED_LABELS)
        findings.append({"kind": ("reward-hack" if gamed else "saturation"), "detail":
            f"TrialClassifier on the frontier trials: {_analysis_summary(bd)}. "
            + ("A BAD_SUCCESS means the verifier was GAMED — close that hole structurally (compute the "
               "tally in the grader; tighten the boundary), not just raise difficulty."
               if gamed else "Passes are honest solves — the task is genuinely within reach; deepen it.")})
    cases = _per_case_findings(band)
    if cases:
        findings.append({"kind": "saturation", "detail":
            f"per-case aggregates over {cases['n_trials']} trial(s): top failing cases "
            f"{cases['top_failing'] or ['(none)']} — the UNTOUCHED families are where the passes come "
            "from; extend there."})
    # ProgramBench HARDEN levers (farm §8, condensed — DESIGN §6.6): case-suite depth, never scope.
    findings.append({"kind": "saturation", "detail":
        "LEVER add-cases: add cases in the UNCOVERED feature families (15-30 per family), generated "
        "from the ORACLE — never hand-written expected outputs. "
        "LEVER adversarial-errors: add 10+ adversarial error-path cases (bad flags, malformed "
        "input, boundary values). "
        "LEVER deepen-surface: deepen the held-out surface — edge semantics of EXISTING families, "
        "cover every output mode. "
        "LEVER tighten-timeout: tighten the verifier timeout ONLY if there is real slack. "
        "The task must stay SOLVABLE_AS_WRITTEN and fair; the tool, flag surface, and grading "
        "mechanism are frozen."})
    return findings


def _ease_findings(manifest: Manifest, task_dir: Path) -> list[dict]:
    """Unblind an `ease` move (ADR-0040): point the patch at the UNIVERSAL BLOCKERS — the cases
    every trial fails the same way — parsed from the per-case verifier artifacts, plus the
    good-failure evidence recorded by the gate. Easing removes blockers, never semantic depth."""
    sw = manifest.sweeps or {}
    band = sw.get("full") or sw.get("difficulty") or {}
    findings: list[dict] = []
    gf = band.get("goodfail") or {}
    if gf.get("verdict") == "keep_hard":
        # ADR-0048 enforce_window: the audit found NO defect — this ease is a pure difficulty
        # reduction toward the band window, not blocker removal. Aim the patch accordingly.
        findings.append({"kind": "underspecification", "detail":
            f"window enforcement (on_too_hard=enforce_window): the deep audit verified the failures "
            f"are genuine capability headroom ({gf.get('reason')}), but this run requires the pass "
            "band to land INSIDE the target window. REDUCE DIFFICULTY toward the window: soften the "
            "hardest case families (per-case aggregates below), trim the flag/feature surface the "
            "trials consistently died on, or move a few of the hardest held-out cases into the "
            "public smoke set. The task must remain non-trivial — do NOT gut it below the floor."})
    elif gf.get("reason"):
        findings.append({"kind": "underspecification", "detail":
            f"good-failure gate: {gf['reason']} (verdict {gf.get('verdict')})."})
    cases = _per_case_findings(band)
    if cases:
        blockers = cases["universal_blockers"]
        findings.append({"kind": "underspecification", "detail":
            f"per-case aggregates over {cases['n_trials']} trial(s): universal-blocker candidates "
            f"{blockers or '(none isolated)'}; top failing {cases['top_failing']}. Remove/soften ONLY "
            "the universal blockers (e.g. drop exact-error-text cases, move a few blockers to the "
            "public smoke set) — NEVER gut whole feature families or lower the agent budget."})
    else:
        findings.append({"kind": "underspecification", "detail":
            "no per-case artifacts parsed — identify the universal blockers from the trajectory "
            "evidence above and remove ONLY those; keep the semantic depth intact."})
    return findings


def _finalize_patch(manifest: Manifest, task_dir: Path) -> None:
    """After a successful SYNTHESIZE patch: (1) drop the now-stale empirical sweeps so the rejoin
    re-measures, and (2) repoint the sweep source to the PATCHED local copy. (2) matters for an
    adopted external bundle (e.g. minpack): SYNTHESIZE edits the run's local `task/<slug>`, so the
    next sweep must upload THAT — not the pristine reference bundle — for the harden to take effect.
    The ground-truth repo is never mutated."""
    for k in _STALE_ON_PATCH:
        (manifest.sweeps or {}).pop(k, None)
    manifest.snapshot = {**(manifest.snapshot or {}), "task_bundle_path": str(task_dir)}


def _h_synth(state, manifest, run_dir, ctx) -> StageResult:
    if not ctx.get("agentic"):
        return StageResult(blocked=True, reason="SYNTHESIZE patch-apply is agentic + needs an "
                           "execution env — set ctx 'agentic' (and run on a Docker/cloud-exec host)")
    from .cells.synthesize import apply, build_prompt, synthesize_plan
    from .promptlog import write_prompt
    task_dir = Path(run_dir) / "task" / (manifest.slug or "rewrite-task")
    move, from_stage, reason = _synth_trigger(state)

    def _do(_m, session=None):
        # A `harden` is triggered by saturation (the frontier passes too easily) and an `ease` by
        # verified task-design blockers — both previously would patch blind. Derive evidence-based
        # findings (band severity / universal blockers + the fair levers) and prepend them.
        findings = list(ctx.get("findings") or [])
        if move == "harden":
            findings = _saturation_findings(_m, task_dir) + findings
        elif move == "ease":
            findings = _ease_findings(_m, task_dir) + findings
        write_prompt(run_dir, "SYNTHESIZE",  # inspectable in the step viewer
                     build_prompt(str(task_dir), move, from_stage, reason, findings or None))
        plan = synthesize_plan(str(task_dir), move, from_stage, reason,
                               findings=findings or None, runner=ctx.get("llm_runner"),
                               model=ctx.get("model"))
        return apply(plan, str(task_dir), session=session or ctx.get("agent_session"),
                     validator=ctx.get("validator"), max_iters=ctx.get("max_iters", 2))

    if not ctx.get("agentic_background"):   # synchronous path (tests / CLI direct)
        res = _do(manifest)
        if not res.success:
            return StageResult(blocked=True, reason=f"SYNTHESIZE apply incomplete: {res.reason}")
        _finalize_patch(manifest, task_dir)
        return StageResult(verdict="done", reason=res.reason)

    # background: a unique job per tune/revise attempt (so a prior patch's success isn't reused);
    # the ease counter is part of the id — an ease after a harden is a DISTINCT patch generation.
    job = f"synthesize-h{state.harden}-r{state.revise}-e{state.ease}"

    def _produce():
        # disposable copy; edits are applied in-place to task_dir. Pin to the routed credential source.
        res = _do(Manifest.load(run_dir), session=_cell_session(ctx.get("model")))
        if not res.success:
            raise RuntimeError(res.reason)
        return res.reason

    def _apply():
        _finalize_patch(manifest, task_dir)   # mutate the loop's manifest (persisted by the driver)
        return StageResult(verdict="done", reason=f"patch applied; downstream sweeps invalidated ({job})")

    return _agentic_bg_step(job, Path(run_dir), produce=_produce, complete=lambda: False,
                            apply_result=_apply, done_is_success=True)


def _h_pr(state, manifest, run_dir, ctx) -> StageResult:
    """LEGACY drain (ADR-0039): the PR output stage is gone — accepted tasks are exported to the
    outbox at QA_GATE. Kept only so pre-ADR-0039 state files at PR drain to DONE. Advances
    immediately and NEVER opens anything (pr.py is deleted; nothing here imports it)."""
    return StageResult(verdict="done",
                       reason="legacy stage — auto-completes (PR output removed by ADR-0039; "
                              "finished tasks land in the outbox)")


# ---- outbox export (ADR-0039: the pipeline's output is a directory, not a PR) --------

def _export_task(manifest: Manifest, run_dir: Path, ctx: dict, *, shelf: str) -> tuple[Path | None, str]:
    """Deterministically export the FULL task dir (tests included — the outbox consumer is a
    harbor-lh-style checkout, so nothing is stripped; the outbox is NOT agent-facing) to
    `<outbox_dir>/<shelf>/<slug>/` plus a `.provenance.json` audit stamp. `shelf` is "tasks"
    (QA_GATE accept), "easy" (EASY_SHELF), or "drafts" (Static-CI-only mode). Idempotent: a
    re-export replaces the prior copy.
    Returns (dest|None, human note) — a missing task dir is a LOUD note, never a crash."""
    import shutil
    from datetime import datetime, timezone
    slug = manifest.slug or "task"
    src = Path(run_dir) / "task" / slug
    if not src.is_dir():
        alt = _task_bundle(manifest, Path(run_dir), ctx)   # adopted-bundle runs keep the task elsewhere
        src = alt if alt is not None and Path(alt).is_dir() else None
    if src is None:
        return None, "export SKIPPED: no task dir found (investigate — an accepted run should have one)"
    cfg = _lh_config()
    outbox = Path(ctx.get("outbox_dir") or getattr(cfg, "outbox_dir", None) or "out")
    dest = outbox / shelf / slug
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)
    full = (manifest.sweeps or {}).get("full") or {}
    src_info = manifest.source
    prov = {
        "run_id": manifest.run_id,
        "task_identity": manifest.task_identity,
        "repo": (f"{src_info.repo}@{src_info.pinned_sha}" if src_info else None),
        "shelf": shelf,
        "pipeline_mode": manifest.pipeline_mode,
        "calibrated": shelf != "drafts",
        "band": {"band_verdict": full.get("band_verdict"), "pass_at_1": full.get("pass_at_1"),
                 "hard_keep": bool(full.get("hard_keep"))},
        "sweeps": {k: {"experiment": v.get("experiment"), "status": v.get("status"),
                       "summary": v.get("summary")}
                   for k, v in (manifest.sweeps or {}).items() if isinstance(v, dict)},
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    (dest / ".provenance.json").write_text(json.dumps(prov, indent=2) + "\n")
    manifest.snapshot = {**(manifest.snapshot or {}), "outbox_path": str(dest)}
    return dest, f"exported to {dest}"


def export_on_human_accept(run_dir: str | Path, ctx: dict | None = None) -> tuple[Path | None, str]:
    """Export the task for a HUMAN-mode QA_GATE accept (the CLI `programsmith qa-gate … accept` and the UI
    endpoint). The auto handler exports inline (_h_qa_gate), but the human accept goes straight
    through `state.advance("accept")` → DONE, which is a pure FSM transition with no side effects —
    so without this the accepted task is never written to the outbox (the pipeline's ONLY output).
    Idempotent (re-export replaces); loads+saves the manifest so `snapshot.outbox_path` is recorded.
    Safe to call unconditionally on accept — a missing task dir returns a loud note, never raises."""
    run_dir = Path(run_dir)
    manifest = Manifest.load(run_dir)
    dest, note = _export_task(manifest, run_dir, ctx or {}, shelf="tasks")
    manifest.save(run_dir)
    return dest, note


# ---- TASK_MATRIX auto-pick (ADR-0039: human review optional, default auto) -----------

def _pick_candidate(candidates: list) -> tuple[int, object] | None:
    """Deterministic pick over schema-validated candidates: the FIRST candidate at the strongest
    available recommendation tier (recommended > viable > marginal — cells.task_matrix.AUTOPICK_ORDER,
    the farm posture), else None (only when there are NO candidates). Routes ONLY on the validated
    `recommendation` enum (invariant #4 — the cell annotates, this picks). Duck-typed (attr or dict)
    so the candidate schema can evolve without touching this gate."""
    from .cells.task_matrix import AUTOPICK_ORDER
    def rec(c) -> str:
        v = getattr(c, "recommendation", None)
        if v is None and isinstance(c, dict):
            v = c.get("recommendation")
        return str(v or "")
    for want in AUTOPICK_ORDER:
        for i, c in enumerate(candidates):
            if rec(c) == want:
                return i, c
    return None


def _apply_pick(state, manifest, cand) -> str:
    """Apply a picked candidate via cells.task_matrix.apply_selection — the ONE shared code path
    with `programsmith pick` and the UI pick endpoint, so the task-identity dedup hash (ADR-0012/0038) can
    never become path-dependent (auto-pick vs human-pick MUST hash identically). Candidates loaded
    back from a persisted task_matrix.json arrive as dicts — re-validate through TaskCandidate
    first; a legacy (port-era) candidate that no longer validates keeps the provisional identity
    LOUDLY instead of guessing."""
    from .cells.task_matrix import TaskCandidate, apply_selection
    try:
        candidate = cand if isinstance(cand, TaskCandidate) else TaskCandidate.model_validate(
            cand.model_dump() if hasattr(cand, "model_dump") else dict(cand))
    except Exception as e:  # noqa: BLE001 — legacy-schema candidate: pick it, keep provisional id
        from .manifest import Dimensions
        dump = cand.model_dump() if hasattr(cand, "model_dump") else dict(cand)
        manifest.dimensions = Dimensions(**{k: v for k, v in dump.items()
                                            if k in Dimensions.model_fields})
        return (f"picked (identity NOT recomputed: candidate predates the ProgramBench schema "
                f"— {str(e)[:80]}; keeping the provisional identity)")
    apply_selection(manifest, candidate)
    state.task_identity = manifest.task_identity
    return "picked; task identity recomputed"


def _h_task_matrix(state, manifest, run_dir, ctx) -> StageResult:
    """TASK_MATRIX — mode-aware (ADR-0039). "human": the pre-ADR blocking review (programsmith pick / UI).
    "auto" (default): run the propose() cell on the LIGHT model, persist task_matrix.json + the
    promptlog, then pick deterministically (_pick_candidate) and advance. Idempotent: an existing
    task_matrix.json (a crash-resume, or a human-triggered cell) is REUSED, never re-proposed."""
    if _gate_mode(ctx, "task_matrix_mode") == "human":
        return _h_human(state, manifest, run_dir, ctx)
    matrix_file = Path(run_dir) / "task_matrix.json"
    desired_profile = "draft" if manifest.pipeline_mode == "draft" else "full"
    no_candidate_reason = None
    candidates = None
    if matrix_file.exists():
        try:
            _mx = json.loads(matrix_file.read_text()) or {}
            candidates = _mx.get("candidates") or []
            no_candidate_reason = _mx.get("no_candidate_reason")
            stored_profile = _mx.get("profile") or "full"
        except (OSError, json.JSONDecodeError) as e:
            return StageResult(blocked=True, reason=f"TASK_MATRIX: task_matrix.json unreadable "
                               f"({e}) — delete it to re-propose")
        # A non-empty full matrix is stricter than draft and remains safe to reuse. An EMPTY full
        # matrix is not: it may have rejected a small/easy tool solely on calibration criteria, so
        # archive it and re-propose under the draft rubric. Matching-profile empties remain final.
        if stored_profile != desired_profile and not candidates:
            backup = Path(run_dir) / f"task_matrix.{stored_profile}.json"
            if not backup.exists():
                backup.write_text(matrix_file.read_text())
            matrix_file.unlink()
            candidates = None
            no_candidate_reason = None
    if candidates is None:
        # Structural source screening is deterministic and free. Persist it before any paid model
        # call so the UI/audit can distinguish a rejected source from a failed task pipeline.
        from .source_screen import screen_source
        screening = screen_source(manifest)
        manifest.source_screen = screening.model_dump()
        (Path(run_dir) / "source_screen.json").write_text(screening.model_dump_json(indent=2) + "\n")
        if not screening.eligible:
            return StageResult(
                verdict="none_selected",
                reason=f"source screened out ({screening.profile}): {screening.reason}",
            )
        # The propose() cell is a real LLM call — like every other cell it needs the agentic opt-in
        # (or an injected runner) so an offline/test drive never shells out to `claude`.
        if not (ctx.get("llm_runner") or ctx.get("agentic")):
            return StageResult(blocked=True, reason="TASK_MATRIX auto-pick needs the LLM cell — "
                               "set ctx 'agentic' (fleet driver default) or inject 'llm_runner'")
        from .cells.task_matrix import build_prompt, propose
        from .promptlog import write_prompt
        from .source_screen import build_source_dossier
        entry = (manifest.sweeps or {}).setdefault("task_matrix", {})
        attempts = entry.get("attempts", 0)
        if attempts >= 3:
            return StageResult(blocked=True, reason=f"TASK_MATRIX: propose() failed {attempts}× — "
                               f"NOT advanced ({entry.get('error', '')[:100]}); needs investigation")
        cfg = _lh_config()
        model = ctx.get("model") or getattr(cfg, "cell_model_light", None)  # light one-shot (ADR-0042)
        dossier = build_source_dossier(manifest)
        (Path(run_dir) / "source_dossier.json").write_text(
            json.dumps(dossier, ensure_ascii=False, indent=2) + "\n"
        )
        write_prompt(run_dir, "TASK_MATRIX", build_prompt(manifest))
        try:
            out = propose(manifest, runner=ctx.get("llm_runner"), model=model)
        except Exception as e:  # noqa: BLE001 — a cell failure blocks honestly, bounded above
            entry["attempts"] = attempts + 1
            entry["error"] = str(e)[:200]
            return StageResult(blocked=True, reason=f"TASK_MATRIX propose() errored "
                               f"({str(e)[:100]}) — retry {attempts + 1}/3 next pass")
        matrix_file.write_text(out.model_dump_json(indent=2) + "\n")
        candidates = list(out.candidates)
        no_candidate_reason = getattr(out, "no_candidate_reason", None)
    pick = _pick_candidate(candidates)
    if pick is None:
        why = (no_candidate_reason or "").strip()
        return StageResult(verdict="none_selected", reason=(
            f"auto-pick: no viable task for this repo — {why}" if why else
            "auto-pick: the cell proposed no candidates — dropped (repo likely unsuitable: a library, "
            "monorepo, or no deterministic CLI surface; marginal candidates ARE accepted when they exist)"))
    idx, cand = pick
    note = _apply_pick(state, manifest, cand)
    return StageResult(verdict="selected", reason=f"auto-pick: candidate [{idx}] — {note}")


# ---- QA_GATE auto decision (ADR-0039: final gate, auto by default) -------------------

def _h_qa_gate(state, manifest, run_dir, ctx) -> StageResult:
    """QA_GATE — mode-aware (ADR-0039). "human": the pre-ADR blocking review (programsmith qa-gate / UI
    panel). "auto" (default): compute the deterministic gate inputs from the RECORDED manifest.
    sweeps (nothing is re-measured here) and advance on gates.qa_gate's verdict. On accept, the
    task is deterministically EXPORTED to `<outbox_dir>/tasks/<slug>/` (the pipeline's output)."""
    if _gate_mode(ctx, "qa_gate_mode") == "human":
        return _h_human(state, manifest, run_dir, ctx)
    from .gates.qa_gate import qa_gate
    from .runconfig import effective_run_config
    sweeps = manifest.sweeps or {}
    full = sweeps.get("full") or {}
    band = effective_run_config(manifest).full.band
    bv = full.get("band_verdict")
    if bv is None:
        bv = _recorded_band_verdict(full, band)
    breakdown = (full.get("analysis") or {}).get("breakdown") or {}
    concern = any(breakdown.get(l, 0) for l in ("BAD_SUCCESS", "BAD_FAILURE"))
    res = qa_gate(
        bv,
        integrity_ok=(full.get("integrity") or {}).get("verdict") != "fail",
        # If we reached QA_GATE the probe either verdicted clean or never gated (absent = clean);
        # a probe 'harden' would have routed back at QA_PROBE, so only an explicit record counts.
        probe_clean=(sweeps.get("qa_probe") or {}).get("verdict") != "harden",
        hard_keep=bool(full.get("hard_keep")),
        analysis_concern=concern,
    )
    reason = f"final gate (auto): {res.reason}"
    if res.verdict == "accept":
        _dest, note = _export_task(manifest, Path(run_dir), ctx, shelf="tasks")
        reason = f"{reason}; {note}"
    return StageResult(verdict=res.verdict, reason=reason)


REGISTRY: dict[Stage, Handler] = {
    Stage.INGEST_LOCK: _h_ingest,
    Stage.TASK_MATRIX: _h_task_matrix,    # auto-pick by default; human mode blocks (ADR-0039)
    Stage.ORACLE_GOLDEN: _h_oracle,
    Stage.CREATE: _h_create,
    Stage.SANITY: _h_sanity,
    Stage.STATIC_CI: _h_static,
    Stage.DIFFICULTY_SWEEP: _h_difficulty,
    Stage.CALIBRATE: _h_calibrate,
    Stage.QA_PROBE: _h_qa_probe,
    Stage.FULL_SWEEP: _h_full_sweep,
    Stage.QA_ON_GPT: _h_qa_on_gpt,        # LEGACY drain — advances immediately (ADR-0039)
    Stage.QA_GATE: _h_qa_gate,            # auto-decide by default; human mode blocks (ADR-0039)
    Stage.SYNTHESIZE: _h_synth,
    Stage.PR: _h_pr,                      # LEGACY drain — advances to DONE, opens nothing (ADR-0039)
}


# ---- read-only preview (what is this run waiting on?) --------------------------------

def _stage_status(stage: Stage, manifest: Manifest, ctx: dict) -> tuple[str, str]:
    """Side-effect-free mirror of each handler's *gating* condition. Returns (kind, reason) where
    kind ∈ {runnable, blocked}. Used by `peek` to explain a parked run WITHOUT running any cell/gate
    or shelling out (safe to call on every UI poll). Reasons mirror the handlers above."""
    from .sweepbackend import backend_name
    sweeps = manifest.sweeps or {}
    bname = backend_name(ctx)   # backend label for the waiting/blocked messages
    if stage is Stage.INGEST_LOCK:
        return "blocked", "INGEST runs at run creation; nothing to drive here"
    if stage is Stage.TASK_MATRIX:
        # Only reached in AUTO mode (peek intercepts the human-mode stages before calling here).
        if ctx.get("llm_runner") or ctx.get("agentic"):
            return "runnable", "auto-pick: propose candidates (light model) + deterministic pick"
        return "blocked", ("TASK_MATRIX auto-pick needs the LLM cell — set ctx 'agentic' "
                           "(fleet driver default) or inject 'llm_runner'")
    if stage is Stage.ORACLE_GOLDEN:
        if ctx.get("oracle_bundle"):
            return "runnable", "ready to adopt the supplied reference bundle"
        if ctx.get("oracle_generate_dir") or ctx.get("oracle_generate") or ctx.get("agentic"):
            # generate-mode (the fleet driver sets ctx 'agentic') — the agent builds the oracle in the
            # background (oracle = the original, for golden-io/differential). Not a stall: it's working.
            return "runnable", "generate-mode: agent produces the oracle bundle (oracle = the original)"
        return "blocked", ("ORACLE+GOLDEN needs a reference bundle to adopt (supply oracle_bundle), an "
                           "execution env for generate-mode (oracle_generate_dir), or agentic generate "
                           "(ctx 'agentic')")
    if stage is Stage.CREATE:
        return "runnable", "ready to assemble the hybrid task skeleton"
    if stage is Stage.SANITY:
        if (sweeps.get("sanity") or {}).get("trials"):
            return "runnable", "oracle/nop baseline trials recorded; ready to verdict"
        return "blocked", ("SANITY needs local Docker, or recorded oracle/nop baseline trials — "
                           "import them with `programsmith sweep-read --kind sanity --from-pull <dir>` "
                           "(ADR-0017)")
    if stage is Stage.STATIC_CI:
        root = ctx.get("ci_repo_root")
        if root and not (Path(root) / "ci_checks").exists():
            return "blocked", (f"STATIC CI: ci_repo_root override {root!r} has no ci_checks/ — "
                               "fix or unset it to use the vendored in-tree suite")
        return "runnable", "ready to replay the CHECK_ORDER (vendored suite, or the ci_repo_root override)"
    if stage is Stage.DIFFICULTY_SWEEP:
        diff = sweeps.get("difficulty") or {}
        if _parse_pass_at_1(manifest) is not None:
            return "runnable", "difficulty sweep result present"
        if diff.get("status") == "running":   # already launched + polling — NOT "needs launching"
            return "waiting", f"difficulty sweep running on {bname} ({diff.get('experiment', '')}); polling"
        if diff.get("status") == "errored":
            return "waiting", f"difficulty sweep errored on {bname} ({diff.get('experiment', '')}); re-running with backoff"
        return "blocked", (f"DIFFICULTY SWEEP needs a live sweep on {bname} (billable) — launch it, "
                           "record it with `programsmith sweep-read --kind difficulty`, then re-drive")
    if stage is Stage.CALIBRATE:
        if _parse_pass_at_1(manifest) is not None:
            return "runnable", "ready to calibrate the difficulty band"
        return "blocked", "CALIBRATE needs the difficulty pass@1 (record the sweep first)"
    if stage is Stage.QA_PROBE:
        qp = sweeps.get("qa_probe") or {}
        if qp.get("verdict"):
            return "runnable", f"probe verdict recorded ({qp['verdict']}) on {bname} {qp.get('experiment','')}"
        if qp.get("status") == "running":
            return "waiting", f"QA/PROBE running on {bname} ({qp.get('experiment')}); polling"
        return "blocked", f"QA/PROBE launches a real auditor probe on {bname} — needs sweep_live + bundle"
    if stage is Stage.FULL_SWEEP:
        full = sweeps.get("full") or {}
        if full.get("status") == "done" or "aggregate" in full:
            return "runnable", f"full sweep recorded on {bname} {full.get('experiment', '')}"
        if full.get("status") == "running":   # already launched + polling — NOT "needs launching"
            return "waiting", f"full sweep running on {bname} ({full.get('experiment', '')}); polling"
        if full.get("status") == "errored":
            return "waiting", f"full sweep errored on {bname} ({full.get('experiment', '')}); re-running with backoff"
        return "blocked", f"FULL SWEEP needs a live dual-family sweep on {bname} (billable)"
    if stage is Stage.QA_ON_GPT:
        return "runnable", "legacy stage — auto-completes (folded into the frontier analysis gate)"
    if stage is Stage.QA_GATE:
        # Only reached in AUTO mode (human mode is intercepted by peek). The recorded sweeps are the
        # gate's whole input, so it is always runnable once the run is here.
        return "runnable", "final gate (auto): ready to decide from the recorded sweeps; accept exports to the outbox"
    if stage is Stage.SYNTHESIZE:
        return "blocked", ("SYNTHESIZE patch-apply is agentic — needs an execution env (local "
                           "Docker); enable with ctx 'agentic'")
    if stage is Stage.PR:
        return "runnable", "legacy stage — auto-completes (PR output removed; tasks export to the outbox)"
    return "blocked", f"no handler for {stage.value}"


def peek(run_dir: str | Path, *, ctx: dict | None = None) -> dict:
    """Read-only: describe what the run's CURRENT stage is waiting on, with NO side effects (no
    cell/gate execution, no Docker probe, no spend). Returns {stage, kind, reason}; kind ∈
    {terminal, paused, human, runnable, blocked}. The UI shows this so a parked run explains itself
    without the operator having to click Advance."""
    ctx = ctx or {}
    rd = Path(run_dir)
    state = RunState.load(rd)
    manifest = Manifest.load(rd)
    stage = state.current_stage
    if state.terminal:
        # Surface the SPECIFIC reason the run ended (the last history event), not a generic "run
        # dropped" — so the UI can explain WHY it dropped/blocked (e.g. the harden-review verdict).
        last = next((ev for ev in reversed(state.history) if ev.reason), None)
        why = last.reason if last else f"run {state.status}"
        pre_create_drop = bool(
            state.history
            and state.history[-1].stage in (Stage.INGEST_LOCK, Stage.TASK_MATRIX)
        )
        return {"stage": stage.value, "kind": "terminal", "reason": why,
                "can_reopen": stage in (Stage.DROPPED, Stage.BLOCKED) and not pre_create_drop}
    if state.paused:
        return {"stage": stage.value, "kind": "paused", "reason": "operational pause/stop"}
    # Only the stages the CONFIG actually human-gates read as "human" (ADR-0039: both default auto
    # — a default fleet shows TASK_MATRIX/QA_GATE as runnable auto gates, not review stops).
    if stage in active_human_stages(ctx=ctx):
        which = "#1 (pick tasks)" if stage is Stage.TASK_MATRIX else "#2 (accept/revise/reject)"
        return {"stage": stage.value, "kind": "human", "reason": f"awaiting HUMAN REVIEW {which}"}
    kind, reason = _stage_status(stage, manifest, ctx)
    return {"stage": stage.value, "kind": kind, "reason": reason}


def _is_harden_revivable(state: RunState) -> bool:
    """True iff a BLOCKED run is parked there SOLELY because it exhausted a TUNING bound at an older,
    lower budget — so raising the bound should grant it the now-available attempt(s). Deterministic,
    over recorded fields only (no LLM): the last transition was a tuning-budget exhaustion AND the
    relevant counter is below the *current* bound (in steady state a budget-block sits AT the bound, so
    this is exactly "the bound rose since it blocked"). We do NOT pre-judge by the band — raising the
    bound means "give every budget-blocked run the extra attempt"; the real re-measurement decides
    whether it helps (measure-don't-predict).

    Recognizes both the post-ADR-0040 smoke-tuning exhaustion (the probe-found-exploit BLOCK, reason
    "smoke tuning budget N exhausted", counter smoke_tunes vs SMOKE_TUNE_MAX) and the legacy
    pre-ADR-0040 "harden bound" reason (counter harden vs HARDEN_MAX) so old state files still revive.
    A frontier-budget exhaustion never lands on BLOCKED (it shelves/drops), and a revise/capture
    exhaustion is a defect not a difficulty budget — both stay terminal."""
    from .fsm import HARDEN_MAX, SMOKE_TUNE_MAX
    if state.current_stage is not Stage.BLOCKED or not state.history:
        return False
    reason = state.history[-1].reason or ""
    if "smoke tuning budget" in reason:
        return state.smoke_tunes < SMOKE_TUNE_MAX
    if "harden bound" in reason:  # legacy (pre-ADR-0040) block
        return state.harden < HARDEN_MAX
    return False  # blocked for a DIFFERENT reason (revise/capture/env defect) → genuinely terminal


def _revive_harden_blocked(state: RunState, manifest: Manifest) -> str | None:
    """Make a HARDEN_MAX increase SELF-APPLY: re-enter a harden-exhausted BLOCKED run (when now
    revivable) at the stage that wanted to harden, so the driver re-evaluates it and uses the new
    attempt — no manual reset. Mutates `state` in place; returns a human reason, else None. Bounded:
    after the granted harden it re-measures and, if still saturated at the new bound, re-BLOCKS with
    harden == MAX (no longer revivable) — it cannot loop."""
    from .fsm import HARDEN_MAX, SMOKE_TUNE_MAX
    if not _is_harden_revivable(state):
        return None
    rejoin = state.history[-1].stage  # the stage that emitted the tune (QA_PROBE / legacy CALIBRATE/FULL_SWEEP)
    pa = _parse_pass_at_1(manifest)
    is_smoke = "smoke tuning budget" in (state.history[-1].reason or "")
    n, bound = (state.smoke_tunes, SMOKE_TUNE_MAX) if is_smoke else (state.harden, HARDEN_MAX)
    reason = (f"tuning budget raised to {bound}; granting attempt {n + 1}/{bound} "
              f"(band pass@1={pa}) — re-entering at {rejoin.value}")
    state.history.append(StageEvent(stage=Stage.BLOCKED, verdict="revive", next=rejoin, reason=reason))
    state.current_stage = rejoin
    state.status = "in_progress"
    return reason


def drive(
    run_dir: str | Path,
    *,
    ctx: dict | None = None,
    registry: dict[Stage, Handler] | None = None,
    max_steps: int = 50,
    notes_path: str | Path | None = None,
) -> DriveResult:
    """Advance a run through its runnable stages until it halts. Persists state+manifest after each
    step and writes `drive.json` with the result (the UI reads the halt reason from it).

    There is NO synthetic/simulate path: every verdict comes from a real gate, a real cell, or a real
    sweep whose experiment handle is recorded + linked. A stage that cannot run here (no exec env,
    no complete bundle, spend not authorized) HALTS honestly with its reason — it is never faked."""
    ctx = ctx or {}
    if registry is None:
        registry = REGISTRY
    rd = Path(run_dir)
    state = RunState.load(rd)
    manifest = Manifest.load(rd)
    # Creation-time choices are part of the run, not ephemeral invocation flags. This makes daemon,
    # web, and CLI resumes use the same model and prevents a draft run from entering billable sweeps.
    if manifest.cell_model:
        ctx = {**ctx, "model": manifest.cell_model}
    steps: list[dict] = []
    halted, reason = "max_steps", "step budget exhausted"

    for _ in range(max_steps):
        if manifest.pipeline_mode == "draft" and state.current_stage is Stage.DIFFICULTY_SWEEP:
            _dest, note = _export_task(manifest, rd, ctx, shelf="drafts")
            manifest.save(rd)
            halted = "draft"
            reason = f"draft complete — passed Static CI; {note}; no sweeps or calibration launched"
            break
        if state.terminal:
            # A BLOCKED run whose harden bound was since raised re-enters here (HARDEN_MAX self-applies)
            # instead of staying parked — so a tuning change never needs a manual un-block.
            revived = _revive_harden_blocked(state, manifest)
            if revived:
                state.save(rd)
                steps.append({"stage": "BLOCKED", "verdict": "revive",
                              "next": state.current_stage.value, "reason": revived})
                continue
            # Presentation only: carry the WHY into the halt reason (the step/history entry that
            # made the run terminal), so the CLI never prints a bare "run dropped".
            why = (steps[-1]["reason"] if steps
                   else (state.history[-1].reason or "" if state.history else ""))
            halted = "terminal"
            reason = f"run {state.status}" + (f" — {why}" if why else "")
            break
        if state.paused:
            halted, reason = "paused", "operational pause/stop"
            break
        handler = registry.get(state.current_stage)
        if handler is None:
            halted, reason = "blocked", f"no handler for {state.current_stage.value}"
            break
        from .costlog import cost_context
        with cost_context(rd, state.current_stage.value):
            res = handler(state, manifest, rd, ctx)
        # Persist manifest side effects on EVERY pass — a handler that launched a real sweep
        # records the experiment id into manifest.sweeps even when it then returns `blocked` (polling).
        # Without this, the launch state is lost and the next pass relaunches → double spend.
        manifest.save(rd)
        if res.human:
            halted, reason = "human", res.reason
            break
        if res.blocked or res.verdict is None:
            halted, reason = "blocked", res.reason or "no verdict produced"
            break
        prev = state.current_stage
        decision = state.advance(res.verdict, detail=res.reason)  # gate reason enriches revise edges
        if notes_path:
            record_backward_move(state, decision, trigger=f"{prev.value}: {res.reason}",
                                 notes_path=notes_path)
        step_reason = res.reason
        if decision.next is Stage.EASY_SHELF:
            # Landing on the easy shelf EXPORTS the task (ADR-0040: a good-but-easy task is kept,
            # never trashed) — same deterministic export as a QA_GATE accept, different shelf.
            _dest, note = _export_task(manifest, rd, ctx, shelf="easy")
            step_reason = f"{step_reason}; {note}"
        steps.append({"stage": prev.value, "verdict": res.verdict,
                      "next": decision.next.value, "reason": step_reason})
        manifest.save(rd)
        state.save(rd)

    result = DriveResult(steps=steps, final_stage=state.current_stage.value,
                         final_status=state.status, halted=halted, halt_reason=reason)
    from .statestore import store_for
    _store, _key = store_for(rd)
    _store.write_atomic(f"{_key}/drive.json", json.dumps(asdict(result), indent=2) + "\n")
    return result
