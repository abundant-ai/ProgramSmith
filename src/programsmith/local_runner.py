"""Local sweep execution engine — trials run in a local Docker sandbox.

Runs ONE Harbor-task trial in a local Docker sandbox and classifies its trajectory with the model,
so `LocalSweepBackend` produces the same normalized trial records + analysis labels the cloud path
does. Every external effect (docker build, the solver run, the verifier run, the model call) is
injectable, so the dispatch/parse logic is unit-tested fully offline; the live path needs a Docker
host + a provider API key.

Three network policies are honored (anti-hack invariant #6 — "keep the three network policies
distinct"):
  * SOLVE phase — the agent loop reaches the model API (network on) so it can attempt the task;
  * VERIFY phase — runs `--network=none` (closed internet) so the produced solution can't fetch the
    reference/goldens or phone home;
  * the held-out oracle/goldens are never mounted into the task env — the caller already staged a
    clean bundle (plaintext oracle/goldens stripped); the verifier decrypts `private.enc` exactly
    like the real harbor env.

Reuse basis: the two-phase Docker verifier in `gates.sanity` (build + reward snippets) and the
model-call boundary in `llm.py` (`run_cell` + injectable runner). This module wires the SOLVE phase
in front of that verifier and adds a trajectory classifier.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, field_validator

# The closed-internet verifier snippet (produced solution already in place) — mirror gates.sanity.
_VERIFY_SNIPPET = r'''
  bash /run_test.sh >/tmp/v.log 2>&1
  echo "REWARD=$(cat /logs/verifier/reward.txt 2>/dev/null)"
'''

# TrialClassifier label vocabulary — the labels the `--run-analysis` phase emits, so the
# orchestrator's `_label_breakdown`/`_analysis_summary` (GOOD/BAD × SUCCESS/FAILURE, HARNESS_ERROR)
# read a local classification identically to a cloud one.
TRIAL_LABELS = ("GOOD_SUCCESS", "BAD_SUCCESS", "GOOD_FAILURE", "BAD_FAILURE", "HARNESS_ERROR")

DEFAULT_SOLVE_TIMEOUT = int(os.getenv("PROGRAMSMITH_LOCAL_SOLVE_TIMEOUT", "3600"))
DEFAULT_VERIFY_TIMEOUT = int(os.getenv("PROGRAMSMITH_LOCAL_VERIFY_TIMEOUT", "1800"))
DEFAULT_BUILD_TIMEOUT = int(os.getenv("PROGRAMSMITH_LOCAL_BUILD_TIMEOUT", "1800"))


class TrialInterrupted(RuntimeError):
    """The foreground operator cancelled a billable solver trial.

    This is deliberately different from a harness error: an interrupted trial measured nothing and
    must remain resumable instead of becoming a completed ``reward=None`` record.
    """


_ACTIVE_SOLVES: set[str] = set()
_ACTIVE_SOLVES_LOCK = threading.RLock()
_SOLVE_LAUNCH_LOCK = threading.RLock()
_CANCEL_EPOCH = 0


def cancellation_token() -> int:
    """Return the process-local cancellation generation captured by a newly scheduled trial."""
    with _ACTIVE_SOLVES_LOCK:
        return _CANCEL_EPOCH


def _raise_if_cancelled(token: int | None) -> None:
    if token is None:
        return
    with _ACTIVE_SOLVES_LOCK:
        cancelled = token != _CANCEL_EPOCH
    if cancelled:
        raise TrialInterrupted("solver trial interrupted by the operator")


def interrupt_active_solves() -> int:
    """Cancel queued trials and stop every currently billable solver container.

    The epoch increment cancels work that was queued but had not reached ``docker run`` yet. Named
    containers are snapshotted under the same lock, so a solver cannot slip between the cancellation
    check and registration. Docker calls happen outside the lock and never include credentials.
    Returns the number of active containers targeted.
    """
    global _CANCEL_EPOCH
    # Fence process creation and registration together. If launch wins, the container process is
    # registered before this snapshots it; if cancellation wins, launch observes the new epoch and
    # never starts. RLock also makes this safe if SIGINT lands on a direct/main-thread launch.
    with _SOLVE_LAUNCH_LOCK:
        with _ACTIVE_SOLVES_LOCK:
            _CANCEL_EPOCH += 1
            names = tuple(_ACTIVE_SOLVES)
    for name in names:
        try:
            stopped = subprocess.run(
                ["docker", "stop", "--time", "1", name], capture_output=True, text=True, timeout=15)
            if stopped.returncode != 0:
                subprocess.run(["docker", "kill", name], capture_output=True, text=True, timeout=15)
        except Exception:  # noqa: BLE001 — best-effort shutdown must not mask the operator interrupt
            try:
                subprocess.run(["docker", "kill", name], capture_output=True, text=True, timeout=15)
            except Exception:  # noqa: BLE001
                pass
    return len(names)

# Native-CLI harnesses run via a lazily-built SOLVER OVERLAY image (`FROM <task-image>` + node +
# that vendor's CLI), cached per (task-image content, harness) — the task images themselves stay
# CLI-free (only mini-swe-agent is pre-installed by the generator's Dockerfile block). The npm
# package names are the vendors' published CLIs; headless flags below were verified IN-CONTAINER
# against these packages (claude 2.1.202 / codex 0.142.5 / gemini 0.49.0), never guessed.
NATIVE_CLI_PKGS = {
    "claude-code": "@anthropic-ai/claude-code",
    "codex": "@openai/codex",
    "gemini-cli": "@google/gemini-cli",
}

_OVERLAY_DOCKERFILE = """FROM {base}
ENV DEBIAN_FRONTEND=noninteractive
RUN command -v node >/dev/null 2>&1 || (apt-get update && apt-get install -y curl ca-certificates \\
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && apt-get install -y nodejs \\
 && rm -rf /var/lib/apt/lists/*)
RUN npm install -g {pkg}
"""


# Injectable seams (all default to the real Docker/CLI path):
#   builder(task_dir, tag)              -> (ok, log)
#   solver(task_dir, tag, agent, model) -> trajectory text (agent edits the shared workspace)
#   verifier(task_dir, tag, *, restore_reference=False) -> combined output containing REWARD=<v>
#     (restore_reference=True is the ORACLE baseline: replay solution/solve.sh before grading)
Builder = Callable[[Path, str], "tuple[bool, str]"]
Solver = Callable[[Path, str, str, str], str]
Verifier = Callable[..., str]


class _Label(BaseModel):
    """Schema for the local trajectory classifier's structured answer."""
    label: str

    @field_validator("label")
    @classmethod
    def _known(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in TRIAL_LABELS:
            raise ValueError(f"label must be one of {TRIAL_LABELS}")
        return v


# --------------------------------------------------------------------------------------------------
# reward parsing (shared with gates.sanity's contract: REWARD=<0|1> printed by the verifier)
# --------------------------------------------------------------------------------------------------
def _reward_value(output: str) -> float | None:
    m = re.search(r"REWARD=(\S+)", output or "")
    if not m:
        return None
    tok = m.group(1)
    try:
        return float(tok)
    except ValueError:
        return None


def _image_tag(task_dir: Path, agent: str, trial: int) -> str:
    slug = task_dir.name.replace("/", "-")[:40] or "task"
    return f"programsmith-local:{slug}-{agent}-{trial}"


# --------------------------------------------------------------------------------------------------
# default (live) Docker/CLI implementations
# --------------------------------------------------------------------------------------------------

# The ProgramBench genre is linux/amd64 end-to-end (same pin as gates.sanity): the sealed oracle
# pair is amd64 ELF and the Dockerfile fetches amd64 toolchains. An unpinned build on an arm64
# host (Apple Silicon) produces an arm64 image in which the amd64 oracle cannot exec — the oracle
# BASELINE trial silently scores 0 and poisons the whole sweep (the tengo dev-run failure).
_PLATFORM = "linux/amd64"


def _docker_build(task_dir: Path, tag: str) -> tuple[bool, str]:
    proc = subprocess.run(
        ["docker", "build", "--platform", _PLATFORM, "-t", tag,
         "-f", str(task_dir / "environment" / "Dockerfile"),
         str(task_dir / "environment")],
        capture_output=True, text=True, timeout=DEFAULT_BUILD_TIMEOUT,
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]


def _bare_model(model: str) -> str:
    """Strip the litellm provider prefix for a NATIVE CLI (`anthropic/claude-opus-4-8` →
    `claude-opus-4-8`): each vendor's CLI names models without a provider path. mini-swe keeps the
    full litellm id."""
    return model.split("/", 1)[1] if "/" in model else model


def _trial_cost_limit() -> float:
    """The per-trial cost cap passed to mini-swe (`-l`; 0 disables — mini's own default silently
    caps at a few dollars, so we ALWAYS pass an explicit value). Default 0: the product policy is
    cost preview + confirm, not a hard cap; operators opt in via config `trial_cost_limit`."""
    try:
        from .config import LhConfig
        return float(LhConfig.load().trial_cost_limit)
    except Exception:  # noqa: BLE001 — config is an input, not a dependency
        return 0.0


def _solver_command(agent: str, model: str) -> list[str]:
    """The in-container command that runs the solver harness against the task. mini-swe is the
    UNIVERSAL harness (litellm model id on the provider's own API key, pre-installed in every task
    image); claude-code / codex / gemini-cli drive their vendor CLI inside the solver overlay
    image. Every flag set below was verified in-container against the shipped CLI (`--help`).
    The agent works in the container's task workspace; its transcript lands at
    /workspace/.trajectory[.json] so the VERIFY phase volume carries it back out for
    classification."""
    import shlex
    prompt = "Complete the task described in instruction.md"
    if agent in ("mini-swe", "mini-swe-agent"):
        # mini v2.4.5: -m <litellm model id> -t <task text> -y (no confirmations)
        # -o <trajectory file> -l <cost limit; 0 disables>
        lim = _trial_cost_limit()
        return ["bash", "-lc",
                f'mini -m {shlex.quote(model)} -t "$(cat instruction.md)" -y '
                f"-o /workspace/.trajectory.json -l {lim} 2>&1 | tee /workspace/.trajectory"]
    if agent == "codex":
        # codex 0.142.5: exec = non-interactive; the container IS the sandbox (its intended use);
        # the task workspace is not a git repo, so the repo check must be skipped explicitly.
        return ["bash", "-lc",
                f"codex exec -m {shlex.quote(_bare_model(model))} "
                f"--dangerously-bypass-approvals-and-sandbox --skip-git-repo-check "
                f"{shlex.quote(prompt)} 2>&1 | tee /workspace/.trajectory"]
    if agent == "gemini-cli":
        # gemini 0.49.0: -p = non-interactive (headless) prompt; --yolo auto-approves all actions.
        return ["bash", "-lc",
                f"gemini -m {shlex.quote(_bare_model(model))} --yolo -p {shlex.quote(prompt)} "
                f"2>&1 | tee /workspace/.trajectory"]
    if agent == "claude-code":
        # claude 2.1.202: -p = print (non-interactive); bypassPermissions inside the sandbox.
        # Auth: CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY from the passed env.
        # stream-json (+ --verbose, which it requires) streams events AS THEY HAPPEN — plain -p
        # buffers everything until the final answer, so a trial killed at the solve budget left an
        # EMPTY .trajectory and the classifier could only call it HARNESS_ERROR (the tengo frontier
        # misdiagnosis: a genuinely-hard task audited as an environment defect).
        return ["bash", "-lc",
                f"claude -p --model {shlex.quote(_bare_model(model))} "
                f"--permission-mode bypassPermissions "
                f"--output-format stream-json --verbose "
                f"{shlex.quote(prompt)} 2>&1 | tee /workspace/.trajectory"]
    # An unknown harness must fail FAST and loud (the backend records the trial as errored, never
    # crashes the sweep) instead of burning a solve timeout on a command that cannot exist.
    raise NotImplementedError(
        f"no local solver wiring for harness {agent!r}: use one of mini-swe (universal, litellm), "
        f"claude-code, codex, or gemini-cli")


def _ensure_overlay(base_tag: str, harness: str) -> str:
    """Build (or reuse) the SOLVER OVERLAY image for a native-CLI harness: `FROM <task-image>` +
    node + that vendor's CLI. Cached per (task-image CONTENT, harness): the tag is keyed by the
    base image's ID, so a rebuilt task image gets a fresh overlay while an unchanged one reuses the
    cached image (and docker's layer cache makes the npm step a no-op across siblings). Raises on
    a failed build — run_trial records that trial as errored."""
    pkg = NATIVE_CLI_PKGS[harness]
    ins = subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", base_tag],
                         capture_output=True, text=True, timeout=60)
    if ins.returncode != 0:
        raise RuntimeError(f"overlay: base image {base_tag} not found: {ins.stderr[-200:]}")
    digest = ins.stdout.strip().split(":")[-1][:12]
    tag = f"programsmith-overlay:{digest}-{harness}"
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True,
                      timeout=60).returncode == 0:
        return tag                                   # cache hit — already built for this base
    build = subprocess.run(["docker", "build", "--platform", _PLATFORM, "-t", tag, "-"],
                           input=_OVERLAY_DOCKERFILE.format(base=base_tag, pkg=pkg),
                           capture_output=True, text=True, timeout=DEFAULT_BUILD_TIMEOUT)
    if build.returncode != 0:
        raise RuntimeError(f"overlay build failed for {harness}: "
                           f"{(build.stdout + build.stderr)[-300:]}")
    return tag


def _submission_mount(task_dir: Path) -> list[str]:
    """The bridge between the SOLVE and VERIFY containers: ProgramBench tasks contract the agent to
    build into /app/submission (an IMAGE path, not /workspace) — with two separate `--rm` containers
    the submission would die with the solve container and verify would grade an empty dir (every
    agent trial structurally 0.0: the tengo frontier wipe-out — Sonnet finished a working build and
    still scored 0). A per-trial host dir mounted at /app/submission in BOTH phases persists exactly
    the artifact the task grades, nothing else."""
    sub = task_dir / ".submission"
    sub.mkdir(exist_ok=True)
    return ["-v", f"{sub}:/app/submission:rw"]


def _solver_workspace(task_dir: Path) -> Path:
    """Create the agent-visible workspace for a local trial.

    Harbor exposes the task instruction during SOLVE and mounts the verifier/tests only during the
    later VERIFY phase. Mounting the whole exported task directory here leaked cases.json and every
    expected stdout fixture to the model, turning a difficulty run into a test-fitting exercise.
    Keep only instruction.md in this workspace; the image already contains the executable oracle,
    public docs, and empty /app/submission required by the task contract.
    """
    import shutil

    instruction = task_dir / "instruction.md"
    if not instruction.is_file():
        raise FileNotFoundError(f"task is missing instruction.md: {task_dir}")
    workspace = task_dir / ".solver-workspace"
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir()
    shutil.copy2(instruction, workspace / "instruction.md")
    return workspace


def _docker_solve(task_dir: Path, tag: str, agent: str, model: str, *,
                  cancel_token: int | None = None) -> str:
    """SOLVE phase — network ON so the agent loop can reach the model API. A clean workspace that
    contains ONLY instruction.md is mounted read-write at /workspace; held-out tests and expected
    outputs remain on the host until VERIFY. We read back /workspace/.trajectory (or mini's
    .trajectory.json). Native-CLI harnesses run on the solver overlay image; the VERIFY phase stays
    on the bare task image (the overlay adds solver CLIs only — grading never sees them).

    A solver that exhausts DEFAULT_SOLVE_TIMEOUT is an AGENT failure, not a harness error: the
    container is killed at the budget (subprocess timeout alone leaves the container running —
    and billing — indefinitely) and whatever partial work sits in /workspace goes to VERIFY,
    exactly like a timed benchmark grades at the bell."""
    import uuid
    if agent in NATIVE_CLI_PKGS:
        tag = _ensure_overlay(tag, agent)
    sandbox_flags: list[str] = []
    if agent == "claude-code":
        # The overlay container runs as root, and claude refuses bypassPermissions under root
        # UNLESS it knows it's sandboxed ("--dangerously-skip-permissions cannot be used with
        # root/sudo privileges" — the tengo HARNESS_ERROR wipe-out). IS_SANDBOX=1 is the CLI's
        # own escape hatch for exactly this containerized case (verified in-container).
        sandbox_flags = ["-e", "IS_SANDBOX=1"]
    model_env = _model_env(agent, model)
    solve_workspace = _solver_workspace(task_dir)
    cname = f"programsmith-solve-{uuid.uuid4().hex[:12]}"
    cmd = ["docker", "run", "--rm", "--name", cname, "--platform", _PLATFORM, "--network=bridge",
           "-v", f"{solve_workspace}:/workspace:rw", "-w", "/workspace",
           *_submission_mount(task_dir),
           *sandbox_flags, *_model_env_flags(model_env), tag, *_solver_command(agent, model)]
    # `docker run -e NAME` copies NAME from the Docker client's environment. Supplying values only
    # through subprocess.env keeps tokens out of argv, where `ps` and other local process listings
    # would expose them as `-e NAME=secret`.
    host_env = os.environ.copy()
    host_env.update(model_env)
    timed_out = False
    # Create and register under one launch fence. Popen returns as soon as the Docker client exists;
    # communicate() below owns the long wait. This closes the dangerous gap where Ctrl-C could try
    # to stop a registered container just before `docker run` had actually started it.
    cancelled_during_launch = False
    with _SOLVE_LAUNCH_LOCK:
        with _ACTIVE_SOLVES_LOCK:
            if cancel_token is not None and cancel_token != _CANCEL_EPOCH:
                raise TrialInterrupted("solver trial interrupted before launch")
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=host_env)
        with _ACTIVE_SOLVES_LOCK:
            _ACTIVE_SOLVES.add(cname)
            cancelled_during_launch = cancel_token is not None and cancel_token != _CANCEL_EPOCH
    if cancelled_during_launch:
        # A main-thread signal can run re-entrantly during Popen. It could not see the not-yet-
        # registered name, so the launching thread performs the stop itself after registration.
        subprocess.run(["docker", "stop", "--time", "1", cname],
                       capture_output=True, text=True, timeout=15)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=DEFAULT_SOLVE_TIMEOUT)
            traj = (stdout or "") + (stderr or "")
        except subprocess.TimeoutExpired as e:
            timed_out = True
            subprocess.run(["docker", "kill", cname], capture_output=True, timeout=60)
            try:
                stdout, stderr = proc.communicate(timeout=60)
                traj = (stdout or "") + (stderr or "")
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                traj = (stdout or "") + (stderr or "")
            if not traj:
                out = e.stdout or b""
                traj = out.decode(errors="ignore") if isinstance(out, bytes) else str(out)
    finally:
        with _ACTIVE_SOLVES_LOCK:
            _ACTIVE_SOLVES.discard(cname)
    _raise_if_cancelled(cancel_token)
    for name in (".trajectory.json", ".trajectory"):   # mini's -o file is the richer record
        tfile = solve_workspace / name
        if tfile.exists():
            try:
                text = tfile.read_text(errors="ignore")
            except OSError:
                continue
            if text:
                traj = text
                # Preserve the transcript beside the disposable trial for local inspection/UI,
                # without ever mounting the held-out task tree into the solver container.
                import shutil
                shutil.copy2(tfile, task_dir / name)
                break
    if timed_out:
        traj += f"\n[programsmith] solver hit the {DEFAULT_SOLVE_TIMEOUT}s budget; container " \
                "killed and partial work graded (raise PROGRAMSMITH_LOCAL_SOLVE_TIMEOUT to allow more)"
    return traj


def _docker_verify(task_dir: Path, tag: str, *, restore_reference: bool = False) -> str:
    """VERIFY phase — `--network=none` (closed internet). Grades the produced solution now sitting in
    the bind-mounted task tree, mirroring gates.sanity's verifier phase — including its /tests
    mount: the ProgramBench test.sh invokes /tests/verify.py + /tests/scan_*.py and reads
    /tests/testsuite/, so a test.sh-only mount silently no-ops the scans and zeroes the reward
    (the tengo oracle-baseline=0 sweep poisoning; same defect the SANITY gate had).

    `restore_reference` is the ORACLE-baseline phase: run the shipped solution/solve.sh first
    (exactly gates.sanity's oracle snippet) — a ProgramBench task tree contains NO solved state,
    so grading it un-restored measures the NOP, not the oracle."""
    tests_dir = task_dir / "tests"
    solve_sh = task_dir / "solution" / "solve.sh"
    restore = ("bash /verify/solve.sh >/tmp/solve.log 2>&1 || { echo SOLVE_FAILED; tail -8 /tmp/solve.log; }\n"
               if restore_reference else "")
    wrapper = f"cp /tests/test.sh /run_test.sh && chmod 755 /run_test.sh\n{restore}{_VERIFY_SNIPPET}"
    mounts = ["-v", f"{task_dir}:/workspace:rw", "-v", f"{tests_dir}:/tests:ro",
              *_submission_mount(task_dir)]   # the solve phase's /app/submission, persisted
    if restore_reference:
        mounts += ["-v", f"{solve_sh}:/verify/solve.sh:ro"]
    cmd = ["docker", "run", "--rm", "--platform", _PLATFORM, "--network=none",
           *mounts, "-w", "/workspace", tag, "bash", "-lc", wrapper]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=DEFAULT_VERIFY_TIMEOUT)
    return proc.stdout + proc.stderr


def _read_keychain_oauth() -> dict:
    """The `claude` CLI's login record from the macOS login keychain ({} when absent/unparsable)."""
    import json as _json
    try:
        p = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=10)
        if p.returncode != 0:
            return {}
        return _json.loads(p.stdout.strip()).get("claudeAiOauth") or {}
    except Exception:  # noqa: BLE001 — no keychain (Linux/CI) or unparsable entry = no fallback
        return {}


def _keychain_oauth_token() -> str | None:
    """macOS fallback for the zero-env-var user: the `claude` CLI keeps its subscription login in
    the login keychain ("Claude Code-credentials"), which a trial container can never reach — so
    the runner lifts the CURRENT access token at launch time and passes it through the Docker
    client's environment (never persisted in the image or placed in command arguments). The access
    token is short-lived (~1h) and only the HOST CLI holds the refresh
    token, so when it's close to expiry the runner pokes a minimal host `claude -p` call first —
    the CLI refreshes its keychain entry on any invocation — and re-reads. A trial that still
    outlives the token records an honest HARNESS_ERROR; the durable path is `claude setup-token`
    → CLAUDE_CODE_OAUTH_TOKEN (long-lived), which wins over this fallback."""
    import time as _time
    o = _read_keychain_oauth()
    tok = o.get("accessToken")
    if not tok:
        return None
    if (exp := o.get("expiresAt", 0)) and exp / 1000 - _time.time() < 1800:
        try:  # cheapest possible poke; its only job is the CLI's token refresh side effect
            subprocess.run(["claude", "-p", "--model", "claude-haiku-4-5", "ok"],
                           capture_output=True, text=True, timeout=120)
            tok = _read_keychain_oauth().get("accessToken") or tok
        except Exception:  # noqa: BLE001 — refresh is best-effort; the current token may still work
            pass
    return tok


def _model_env(agent: str, model: str) -> dict[str, str]:
    """Credential values for this SOLVE container, scoped to its provider and harness."""
    env: dict[str, str] = {}
    from .config import LhConfig
    from .runconfig import model_provider
    cfg = LhConfig.load()
    provider = model_provider(model)

    def _env(name: str, value: str) -> None:
        env[name] = value

    if provider == "anthropic" or agent == "claude-code":
        # The claude-code CLI prefers the subscription OAuth token (the zero-API-key path); the
        # API key serves both the CLI and litellm. mini-swe on an Anthropic model gets the API
        # key only — litellm cannot bill an OAuth token.
        if agent == "claude-code" and (tok := cfg.claude_code_oauth_token
                                       or _keychain_oauth_token()):
            _env("CLAUDE_CODE_OAUTH_TOKEN", tok)
        if key := (cfg.anthropic_api_key or os.getenv("ANTHROPIC_API_KEY")):
            _env("ANTHROPIC_API_KEY", key)
    if provider == "openai" or agent == "codex":
        if key := (cfg.openai_api_key or os.getenv("OPENAI_API_KEY")):
            _env("OPENAI_API_KEY", key)
    if provider == "google" or agent == "gemini-cli":
        # litellm's gemini/ route accepts either name; the gemini CLI reads GEMINI_API_KEY.
        key = cfg.gemini_api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if key:
            _env("GEMINI_API_KEY", key)
            _env("GOOGLE_API_KEY", key)
    if provider == "zai":
        if key := (cfg.zai_api_key or os.getenv("ZAI_API_KEY")):
            _env("ZAI_API_KEY", key)
    return env


def _model_env_flags(env: dict[str, str]) -> list[str]:
    """Docker flags that inherit named credentials without exposing their values in argv."""
    return [part for name in env for part in ("-e", name)]


# --------------------------------------------------------------------------------------------------
# public: one trial
# --------------------------------------------------------------------------------------------------
def run_trial(spec: dict, *, builder: Builder | None = None, solver: Solver | None = None,
              verifier: Verifier | None = None) -> dict:
    """Execute one trial and return a normalized trial record ({agent, model, reward, is_probe,
    status, trajectory}). Baselines (oracle/nop) skip the solve phase (oracle replays the reference
    via the image's solve.sh; nop grades an untouched tree). A frontier trial solves then verifies.

    A build failure or a verifier that prints no REWARD yields reward=None/status='errored' — the
    exact shape any trial that errored has, so the orchestrator's 'all frontier errored → re-run'
    logic applies unchanged. Never raises for an expected failure; only a truly unexpected error
    propagates to the backend, which records it as errored too."""
    agent = spec["agent"]
    model = spec.get("model", "default")
    trial = int(spec.get("trial", 0))
    task_dir = Path(spec["task_dir"])
    build = builder or _docker_build
    solve = solver or _docker_solve
    verify = verifier or _docker_verify
    tag = _image_tag(task_dir, agent, trial)
    cancel_token = spec.get("_cancel_token")

    _raise_if_cancelled(cancel_token)
    ok, log = build(task_dir, tag)
    _raise_if_cancelled(cancel_token)
    if not ok:
        return _record(agent, model, None, "errored", error=f"build failed: {log[-200:]}")

    trajectory = ""
    if agent == "oracle":
        # Replay the reference (solution/solve.sh) then grade; reward must be 1 on a sound task.
        out = verify(task_dir, tag, restore_reference=True)
        reward = _reward_value(out)
    elif agent == "nop":
        out = verify(task_dir, tag)
        reward = _reward_value(out)
    else:
        if solve is _docker_solve:
            trajectory = solve(task_dir, tag, agent, model, cancel_token=cancel_token)
        else:
            trajectory = solve(task_dir, tag, agent, model)
        _raise_if_cancelled(cancel_token)
        out = verify(task_dir, tag)
        reward = _reward_value(out)

    _raise_if_cancelled(cancel_token)
    status = "completed" if reward is not None else "errored"
    return _record(agent, model, reward, status, trajectory=trajectory)


def _record(agent: str, model: str, reward, status: str, *, trajectory: str = "", error: str = "") -> dict:
    rec = {"agent": agent, "model": model, "reward": reward, "is_probe": False, "status": status}
    if trajectory:
        rec["trajectory"] = trajectory
    if error:
        rec["error"] = error[:300]
    return rec


# --------------------------------------------------------------------------------------------------
# public: classify one trajectory (the local TrialClassifier)
# --------------------------------------------------------------------------------------------------
_CLASSIFIER_PROMPT = """You are a trajectory classifier for a code-porting benchmark. A solver agent
attempted a task and the deterministic verifier assigned a reward (1 = passed, 0 = failed). Read the
agent's trajectory and the reward, then classify the trial with exactly ONE label:

- GOOD_SUCCESS  — passed by genuinely solving the task.
- BAD_SUCCESS   — passed but by gaming/exploiting the verifier (a reward hack), not real work.
- GOOD_FAILURE  — failed because the task is genuinely hard (an honest, on-task attempt).
- BAD_FAILURE   — failed because the TASK or ENVIRONMENT is broken (missing deps, wrong harness,
                  impossible setup), not because it is appropriately hard.
- HARNESS_ERROR — the run errored on infrastructure (timeout, crashed tooling), no real signal.

Reward for this trial: {reward}

Trajectory (truncated):
{trajectory}
"""


def classify_trajectory(trajectory: str, trial: dict, *, runner=None) -> str:
    """Classify one trajectory into a TRIAL_LABELS value via the model (`run_cell`, tool-free JSON).
    `runner` is injectable so tests never call the model. Falls back to HARNESS_ERROR if the model is
    unreachable — a missing label is never invented as a real GOOD/BAD verdict."""
    from .llm import CellError, run_cell
    reward = trial.get("reward")
    prompt = _CLASSIFIER_PROMPT.format(reward=reward, trajectory=(trajectory or "")[:12000])
    try:
        return run_cell(prompt, _Label, runner=runner).label
    except CellError:
        return "HARNESS_ERROR"
