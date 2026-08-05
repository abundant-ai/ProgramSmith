"""The local sweep backend — trials run in a local Docker sandbox on the operator's own key.

A *sweep* is the pipeline's evaluation engine: run N solver trials (claude-code / codex) plus the
deterministic oracle/nop baselines against a Harbor task in a closed-internet sandbox, collect the
verifier reward per trial, and (optionally) classify each trajectory (GOOD/BAD × SUCCESS/FAILURE).
The orchestrator only ever needs four live operations — launch, status, results, analyses — so this
module funnels them behind a `SweepBackend` Protocol. Everything downstream (pass@1, frontier
filtering, band math in `trials.py`) is pure data and reused unchanged.

One backend ships: `LocalSweepBackend`, which runs each trial in a local Docker sandbox
(`local_runner.py`) and writes trial records in the normalized schema `trials.read_pulled_trials`
reads. The Protocol seam is kept deliberately: it is the injection point the offline tests use
(fake runners/classifiers/executors) and the extension point for anyone wiring a remote fleet —
the orchestrator's sweep state machine never knows which engine is underneath.

`get_backend(ctx)` honors the test-injection knobs threaded through `ctx`
(`local_trial_runner` / `local_classifier` / `local_executor` / `local_sweeps_root`, or a ready
`sweep_backend` object).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Protocol, runtime_checkable

from .trials import SweepAgent

# Handles are opaque strings the backend assigns and echoes back (local: the experiment dir name).
# The orchestrator stores it as manifest.sweeps[stage]["experiment"] and treats it as a token,
# never parsing it.
Handle = str


@runtime_checkable
class SweepBackend(Protocol):
    """The four live operations the orchestrator's sweep state machine needs, plus three descriptors.

    All methods raise on unrecoverable backend errors; the orchestrator catches and records `errored`
    with its own retry bound (a transient poll/launch failure must never crash the driver)."""

    name: str                 # "local" — recorded in the manifest + shown in the UI
    needs_upload: bool         # a remote backend stages+size-caps an upload; local runs the dir in place
    artifact_subdir: str       # where produced trial artifacts land under the run dir

    def launch(self, task_dir: Path, agents: list[SweepAgent], *, experiment: str,
               extra_flags: list[str] | None = None) -> Handle:
        """Submit a sweep of `agents` against `task_dir` and return the backend handle. `extra_flags`
        carries analysis switches (e.g. ['--run-analysis'])."""

    def status(self, handle: Handle) -> dict:
        """Poll completion. Returns at least {complete: bool, tasks_running, trials_completed,
        trials_total, incomplete}. `complete` means every trial reached a terminal state."""

    def results(self, handle: Handle, out_dir: Path) -> list[dict]:
        """Materialize the trial artifacts into `out_dir` and return normalized trials
        ({agent, model, reward, is_probe, status}) — the schema `trials.pass_at_1` consumes."""

    def analyses(self, handle: Handle, *, agents: tuple[str, ...]) -> dict:
        """Per-trial trajectory-classifier labels for the given agent families. Returns
        {analyses: [{trial_id, label}], pending, failed, total}. `pending`>0 means the caller waits."""

    def pull_artifacts(self, handle: Handle, out_dir: Path) -> Path:
        """Materialize raw trial artifacts (trajectories/logs) to `out_dir` and return it. Used by
        QA/PROBE, which text-scans the trajectory for the auditor verdict rather than reading a reward."""


# --------------------------------------------------------------------------------------------------
# LocalSweepBackend — runs each trial in a local Docker sandbox and writes trial records in the
# normalized schema so results/pass@1 reuse the same readers.
# --------------------------------------------------------------------------------------------------
class LocalSweepBackend:
    """A single-box sweep engine. `launch` records a plan of (agent, model, trial) specs and kicks off
    background execution via an injectable `trial_runner`; `status` polls the on-disk trial records;
    `results` reads them with `trials.read_pulled_trials`; `analyses` classifies trajectories with an
    injectable `classifier`. The Docker/solver execution lives in `local_runner.py` (default runner)
    and is fully injectable so the state machine + schema are unit-tested offline.

    Three network policies (anti-hack invariant #6) are honored by the default runner: the solver's
    agent loop reaches the model API, the build+verify step runs `--network=none`, and the held-out
    oracle/goldens are never mounted into the task env (the launch staging already strips them)."""

    name = "local"
    needs_upload = False          # runs the task dir in place; no remote storage, no size cap
    artifact_subdir = ".sweeps"

    def __init__(self, *, root: Path | None = None, trial_runner=None, classifier=None,
                 executor=None):
        # root: where experiment dirs live (defaults under the run dir at launch time).
        self._root = Path(root) if root else None
        # trial_runner(spec: dict) -> normalized trial dict; None → the real Docker runner.
        self._trial_runner = trial_runner
        # classifier(trajectory_text: str, trial: dict) -> label str; None → the real LLM classifier.
        self._classifier = classifier
        # executor: submit(fn) for background trial execution; None → a daemon ThreadPool. Injectable
        # so tests run trials synchronously and deterministically.
        self._executor = executor

    # ---- helpers -------------------------------------------------------------------------------
    def _exp_dir(self, handle: Handle) -> Path:
        base = self._root or Path(os.getenv("PROGRAMSMITH_LOCAL_SWEEPS", ".programsmith/local-sweeps"))
        return Path(base) / handle

    @staticmethod
    def _plan(agents: list[SweepAgent]) -> list[dict]:
        """Flatten agents × n_trials into per-trial specs."""
        specs: list[dict] = []
        for a in agents:
            for i in range(max(1, a.n_trials)):
                specs.append({"agent": a.name, "model": a.model_name, "trial": i})
        return specs

    @staticmethod
    def _trial_dir(exp_dir: Path, spec: dict) -> Path:
        return exp_dir / "trials" / f"{spec['agent']}-{spec['trial']}"

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _claim_trial(self, exp_dir: Path, spec: dict) -> Path | None:
        """Atomically claim one unfinished trial across processes.

        The claim is created before executor submission, so queued trials cannot be duplicated by a
        second backend instance. A claim owned by a live process is respected; a dead owner's claim
        is reclaimed on an explicitly authorized resume.
        """
        tdir = self._trial_dir(exp_dir, spec)
        tdir.mkdir(parents=True, exist_ok=True)
        if (tdir / ".done").exists():
            return None
        claim = tdir / ".running.json"
        payload = _json_dumps({"pid": os.getpid()})
        for _ in range(2):
            try:
                with claim.open("x") as fh:
                    fh.write(payload)
                return claim
            except FileExistsError:
                try:
                    owner = _json_loads(claim.read_text())
                except (FileNotFoundError, ValueError):
                    owner = {}
                try:
                    owner_pid = int(owner.get("pid", 0))
                except (TypeError, ValueError):
                    owner_pid = 0
                if self._pid_alive(owner_pid):
                    return None
                try:
                    claim.unlink()
                except FileNotFoundError:
                    pass
        return None

    @staticmethod
    def _reset_incomplete_trial(tdir: Path, claim: Path) -> None:
        """Remove partial solver output before replaying an interrupted trial from pristine input."""
        for child in tuple(tdir.iterdir()):
            if child == claim:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)

    def _run_trial(self, spec: dict, task_dir: Path, exp_dir: Path, extra_flags,
                   claim: Path, cancel_token: int) -> None:
        """Execute one trial and persist its normalized record. Never raises into the executor — a
        failed trial is recorded with reward=None (errored) exactly like any trial that errored, so
        the orchestrator's 'all frontier errored → re-run' logic applies unchanged.

        Each trial gets its OWN copy of the task tree (trials/<id>/<task>): the runner bind-mounts
        the tree read-write for the agent to work in, so a shared tree means concurrent trials edit
        each other's workspace and later trials inherit earlier trials' solutions — every reward in
        the sweep becomes meaningless (the tengo dev-run contamination). pass@1 is only pass@1 if
        every trial starts from the same pristine state."""
        from .local_runner import TrialInterrupted
        run = self._trial_runner or _default_trial_runner
        tdir = self._trial_dir(exp_dir, spec)
        tdir.mkdir(parents=True, exist_ok=True)
        try:
            self._reset_incomplete_trial(tdir, claim)
            work = task_dir
            if task_dir.is_dir():                # injected-runner tests pass paths that don't exist
                # keep the task's name — image tags derive from it; absolute so docker -v accepts it
                work = (tdir / task_dir.name).resolve()
                if not work.exists():
                    shutil.copytree(task_dir, work, symlinks=True)
            rec = run({**spec, "task_dir": str(work), "_cancel_token": cancel_token,
                       "run_analysis": bool(extra_flags and "--run-analysis" in extra_flags)})
        except TrialInterrupted:
            # Interrupted means unmeasured, not errored/completed. Leave no .done record so an
            # explicitly authorized resume reclaims this exact trial from pristine input.
            (tdir / "result.json").unlink(missing_ok=True)
            (tdir / ".done").unlink(missing_ok=True)
            (tdir / ".interrupted").write_text("operator interrupt\n")
            return
        except Exception as e:  # noqa: BLE001 — a crashed trial errors, it does not crash the sweep
            rec = {"agent": spec["agent"], "model": spec["model"], "reward": None,
                   "is_probe": False, "status": "errored", "error": str(e)[:200]}
        finally:
            claim.unlink(missing_ok=True)
        rec.setdefault("agent", spec["agent"])
        rec.setdefault("model", spec["model"])
        (tdir / "result.json").write_text(_json_dumps(rec))
        (tdir / ".done").write_text("1")

    def _schedule_missing(self, exp_dir: Path, plan: dict) -> int:
        from .local_runner import cancellation_token
        submit = self._executor or _default_executor()
        task_dir = Path(plan.get("task_dir") or ".")
        extra_flags = list(plan.get("extra_flags") or [])
        token = cancellation_token()
        submitted = 0
        for spec in plan.get("specs", []):
            claim = self._claim_trial(exp_dir, spec)
            if claim is None:
                continue
            try:
                submit(lambda s=spec, c=claim: self._run_trial(
                    s, task_dir, exp_dir, extra_flags, c, token))
            except Exception:
                claim.unlink(missing_ok=True)
                raise
            submitted += 1
        return submitted

    # ---- SweepBackend interface ----------------------------------------------------------------
    def launch(self, task_dir, agents, *, experiment, extra_flags=None) -> Handle:
        # Only `--run-analysis` is meaningful here (recorded in the plan, threaded per-trial);
        # anything unknown is recorded for provenance and otherwise IGNORED, never an error (every
        # local trial builds fresh via `docker build`, so there is no image cache to bust).
        exp_dir = self._exp_dir(experiment)
        (exp_dir / "trials").mkdir(parents=True, exist_ok=True)
        specs = self._plan(agents)
        plan = {"specs": specs, "task_dir": str(task_dir),
                "extra_flags": list(extra_flags or [])}
        (exp_dir / "plan.json").write_text(_json_dumps(plan))
        self._schedule_missing(exp_dir, plan)
        return experiment

    def resume(self, handle: Handle) -> int:
        """Reclaim only unfinished trials from a persisted plan.

        This method can spend and is intentionally separate from ``status``. The orchestrator calls
        it only from a drive pass carrying explicit sweep/spend authorization; dashboards and
        ``programsmith status`` remain read-only.
        """
        exp_dir = self._exp_dir(handle)
        plan_path = exp_dir / "plan.json"
        if not plan_path.exists():
            raise FileNotFoundError(f"local sweep plan missing for {handle}")
        return self._schedule_missing(exp_dir, _json_loads(plan_path.read_text()))

    def status(self, handle) -> dict:
        exp_dir = self._exp_dir(handle)
        plan = _json_loads((exp_dir / "plan.json").read_text()) if (exp_dir / "plan.json").exists() else {"specs": []}
        total = len(plan.get("specs", []))
        done = sum(1 for _ in exp_dir.glob("trials/*/.done"))
        claims = []
        for path in exp_dir.glob("trials/*/.running.json"):
            try:
                owner = _json_loads(path.read_text())
            except (FileNotFoundError, ValueError):
                owner = {}
            try:
                pid = int(owner.get("pid", 0))
            except (TypeError, ValueError):
                pid = 0
            if self._pid_alive(pid):
                claims.append(path)
        running = bool(claims)
        return {
            "tasks_total": 1, "tasks_running": 1 if running else 0, "tasks_done": 1 if done >= total else 0,
            "trials_completed": done, "trials_total": total, "trials_failed": 0,
            "complete": total > 0 and done >= total,
            "incomplete": total > done and not running,
        }

    def results(self, handle, out_dir) -> list[dict]:
        from .trials import read_pulled_trials
        exp_dir = self._exp_dir(handle)
        # Records already live under the experiment dir in the normalized schema; point the reader at it.
        src = exp_dir if (exp_dir / "trials").exists() else Path(out_dir)
        # Materialize out_dir per the Protocol contract — the caller records it as the entry's
        # `pull_dir`, which downstream readers resolve LATER (the good-failure deep audit reads
        # trajectory tails from it; _per_case_findings parses diff_*.txt). A symlink (not a copy —
        # trial dirs carry whole task-tree workspaces) keeps that recorded path live; without it
        # every pull_dir consumer silently saw an empty/missing dir (the audit ran blind).
        out = Path(out_dir)
        if src == exp_dir and not out.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
            out.symlink_to(exp_dir.resolve(), target_is_directory=True)
        return read_pulled_trials(src)

    def pull_artifacts(self, handle, out_dir) -> Path:
        import shutil
        exp_dir = self._exp_dir(handle)
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        if exp_dir.exists() and exp_dir.resolve() != out.resolve():
            shutil.copytree(exp_dir, out, dirs_exist_ok=True)
        return out

    def discard(self, handle) -> None:
        """Retire a poisoned experiment dir (operator retry after a harness fix): renamed aside, not
        deleted — the errored records stay on disk for the postmortem, but a fresh launch under the
        SAME experiment name starts clean instead of adopting stale `.done` trials as complete."""
        import time
        exp_dir = self._exp_dir(handle)
        if exp_dir.exists():
            exp_dir.rename(exp_dir.with_name(f"discarded-{int(time.time())}-{exp_dir.name}"))

    def analyses(self, handle, *, agents) -> dict:
        """Classify each frontier trajectory locally. Reads the trajectory text a trial recorded and
        labels it with the injectable classifier, emitting the shape the orchestrator's
        `_attach_analysis` consumes."""
        classify = self._classifier or _default_classifier
        from .trials import _canon_agent
        exp_dir = self._exp_dir(handle)
        out: list[dict] = []
        pending = failed = total = 0
        for tdir in sorted(exp_dir.glob("trials/*")):
            rec_path = tdir / "result.json"
            if not rec_path.exists():
                continue
            rec = _json_loads(rec_path.read_text())
            agent = _canon_agent(rec.get("agent"))
            if agents and agent not in agents:
                continue
            total += 1
            if rec.get("agent") in ("oracle", "nop"):
                total -= 1                       # baselines are not classified
                continue
            traj = rec.get("trajectory") or _read_first(tdir, ("trajectory.txt", "trajectory.json"))
            if traj is None:
                pending += 1 if not (tdir / ".done").exists() else 0
                failed += 1 if (tdir / ".done").exists() else 0
                continue
            try:
                label = classify(traj, rec)
            except Exception:  # noqa: BLE001 — a classifier hiccup fails that trial, not the farm
                failed += 1
                continue
            out.append({"trial_id": tdir.name, "label": str(label)})
        return {"analyses": out, "pending": pending, "failed": failed, "total": total}


# --------------------------------------------------------------------------------------------------
# factory
# --------------------------------------------------------------------------------------------------
def backend_name(ctx: dict | None = None) -> str:
    """The backend name: an explicit ctx override wins, else "local" (the only shipped engine)."""
    if ctx and ctx.get("sweep_backend"):
        b = ctx["sweep_backend"]
        return getattr(b, "name", str(b))
    return "local"


def get_backend(ctx: dict | None = None) -> SweepBackend:
    """Resolve the sweep backend for a driver pass. An explicit `ctx['sweep_backend']` (a ready
    backend object) wins — used by tests and by a caller that constructed a custom backend.
    Otherwise the local Docker engine, threading the injection knobs so offline tests run the
    whole state machine without Docker or a model call."""
    ctx = ctx or {}
    if isinstance(ctx.get("sweep_backend"), SweepBackend):
        return ctx["sweep_backend"]
    return LocalSweepBackend(root=ctx.get("local_sweeps_root"),
                             trial_runner=ctx.get("local_trial_runner"),
                             classifier=ctx.get("local_classifier"),
                             executor=ctx.get("local_executor"))


def sweep_live(ctx: dict) -> bool:
    """Whether a live (billable) sweep may launch. Running trials spends the operator's own
    API-key/OAuth tokens, so a launch is always an explicit authorization (`ctx['sweep_live']`)."""
    return bool(ctx.get("sweep_live"))


# --------------------------------------------------------------------------------------------------
# small stdlib helpers (kept local so the module has no hard deps beyond trials.py)
# --------------------------------------------------------------------------------------------------
def _json_dumps(obj) -> str:
    import json
    return json.dumps(obj, indent=2) + "\n"


def _json_loads(text: str):
    import json
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return {}


def _read_first(d: Path, names: tuple[str, ...]) -> str | None:
    for n in names:
        p = d / n
        if p.exists():
            try:
                return p.read_text(errors="ignore")
            except OSError:
                return None
    return None


def _default_executor():
    """A daemon thread pool that runs trials in the background so the driver's status poll returns
    immediately. Bounded to keep a single OAuth/API key from being throttled by too many concurrent
    solver loops; PROGRAMSMITH_LOCAL_TRIAL_CONCURRENCY overrides the config knob per-process."""
    from concurrent.futures import ThreadPoolExecutor
    max_workers = int(os.getenv("PROGRAMSMITH_LOCAL_TRIAL_CONCURRENCY", "0") or 0)
    if max_workers <= 0:
        try:
            from .config import LhConfig
            max_workers = max(1, int(LhConfig.load().local_trial_concurrency))
        except Exception:  # noqa: BLE001 — config is an input, not a dependency
            max_workers = 2
    pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="lh-trial")
    return lambda fn: pool.submit(fn)


def _default_trial_runner(spec: dict) -> dict:
    """Real single-trial executor (Docker). Delegates to local_runner.run_trial, imported lazily so a
    machine without Docker can still import this module and run the offline tests."""
    from .local_runner import run_trial
    return run_trial(spec)


def _default_classifier(trajectory: str, trial: dict) -> str:
    """Real local trajectory classifier (LLM). Delegates to local_runner.classify_trajectory."""
    from .local_runner import classify_trajectory
    return classify_trajectory(trajectory, trial)
