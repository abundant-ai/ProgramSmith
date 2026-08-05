"""Shared agentic execution loop for the synthesis cells (CREATE fill, ORACLE generate, SYNTHESIZE
apply).

Reuse basis (invariant #3): SWE-gen's Claude Code session loop
(`SWE-gen/.../create/claude_code_runner.py`). The pattern is: build a prompt that tells an agent to
edit a Harbor task tree and self-validate via the two-phase verifier, run the agent via the `claude`
CLI (auth = the CLI's own keychain / CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY passthrough), then
read the resulting oracle=1/nop=0 state and iterate, bounded.

Two boundaries are INJECTABLE so the loop is unit-tested fully offline:

  * ``AgentSession`` — runs ONE agentic edit session over a task dir. Default: `claude -p`
    headless agentic. The agent reads/writes/edits files and runs bash.
  * ``Validator`` — returns the oracle/nop verdict for the CURRENT task tree. Default
    (``docker_validator``) wraps the SANITY gate's local two-phase verifier;
    ``baseline_validator`` reads recorded oracle/nop baseline trials instead — **no local Docker**.

The whole point: these cells are real + bounded + tested; their only remaining dependency is a
*configured execution environment* (local Docker), exactly like the rest of the pipeline's
environment-gated stages.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Agentic synthesis cells are HEAVY cells under the ADR-0042 cell-model routing (oracle capture,
# create fill, synthesize apply carry the run's correctness). Cost-conscious default — the operator
# owns the bill; overridable per call / via config.default_cell_model.
DEFAULT_AGENTIC_MODEL = "claude-sonnet-5"  # heavy-cell default (ADR-0042 rework)
DEFAULT_SESSION_TIMEOUT = 1800  # one agent session; the caller bounds the number of sessions

# Lean-config flags for every spawned `claude -p` (cell LLM + agentic session). The host's global
# config (a framework CLAUDE.md + MCP servers + plugins/hooks)
# auto-loads into each session and was making them bog down for the full 30-min timeout doing
# framework ceremony instead of the task. We CANNOT strip CLAUDE.md/hooks (`--bare`/clean config dir
# both disable the OAuth subscription this build runs on), so we do the auth-safe maximum:
#   --strict-mcp-config  → drop ALL inherited MCP servers (no --mcp-config passed ⇒ none load)
#   --append-system-prompt → override the framework's persona/wave/subagent ceremony behaviourally
# Keeps OAuth (default config dir, no simple-mode) intact.
LEAN_MCP_FLAGS = ["--strict-mcp-config"]
# STRUCTURALLY forbid the background / sub-agent tools (determinism sandwich: enforcement is structural,
# not advisory). FOCUS_PROMPT already TELLS the agent "do NOT spawn subagents" — but a prompt is only
# advisory, and the model kept using the `Agent` tool to spawn a background oracle-watcher that gets
# KILLED when the session ends, so the cell's iterate-to-green loop never converged (the libexpat
# create-fill wedge: 3/3 iterations burned on a dying background task, oracle never reached 1). Deny the
# spawners by EVERY known name — `Agent` (current CLI), `Task` (older builds) — plus `Workflow` /
# `ScheduleWakeup`, which also start work that outlives the one-shot session. The cell's real tools
# (Read/Write/Edit/Bash) are untouched, so the agent MUST run the validation INLINE, where both it and
# the loop see the result. Denying a name the CLI doesn't recognize is a harmless no-op.
NO_SUBAGENT_FLAGS = ["--disallowedTools", "Agent", "Task", "Workflow", "ScheduleWakeup"]
FOCUS_PROMPT = (
    "You are a headless worker executing ONE narrowly-scoped task, described in the user message. "
    "IGNORE any global or repository framework instructions that may have auto-loaded (SuperClaude "
    "personas/waves/MCP coordination, multi-agent or subagent delegation, mandatory TodoWrite "
    "ceremony, or pipeline build operating-contracts) — none of them apply to this job. Work directly "
    "and efficiently: read only the files you need, make the edits, run the validation the task "
    "specifies, and stop as soon as it passes. Do NOT spawn subagents, do NOT use MCP tools, do NOT "
    "write long plans."
)


def api_key_cli_flags(env: dict[str, str]) -> list[str]:
    """Use Claude Code's minimal mode when API-key auth makes it available.

    ``--bare`` deliberately refuses OAuth/keychain auth, so it cannot be enabled for the default
    local-login path. With a direct Anthropic API key it removes user hooks, plugins, skills,
    auto-memory, and project customizations that otherwise add startup latency and irrelevant
    context to every farm worker.
    """
    if env.get("ANTHROPIC_API_KEY") and not env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return ["--bare"]
    return []


# Remediation hints for SANITY checks the agentic loop can act on. Keyed by the gate's check name
# (gates.sanity.run_sanity); the value is appended to the agent feedback so it fixes the CAUSE, not
# just oracle/nop. The produced-ownership hint is load-bearing: it is the single most common reason a
# generated verifier passes oracle/nop yet fails the gate (a root-shell `>` redirect owns the file).
_CHECK_REMEDIATION = {
    "produced_owned_by_nobody": (
        "Phase A produced files are owned by root, not `nobody` — so the produce phase did not run "
        "unprivileged as required (reward-hack boundary). CAUSE: a root-shell redirection "
        "`setpriv --reuid=nobody ... -- prog > /logs/verifier/produced/out` opens the output file as "
        "ROOT before dropping privileges, so the file is root-owned. FIX: (1) make the produced dir "
        "writable by nobody — `chmod 777 /logs/verifier/produced` BEFORE Phase A; (2) create the "
        "produced files FROM the nobody process — either pass an output path so the agent program "
        "writes its own files, or move the redirection INSIDE the dropped shell: "
        "`setpriv --reuid=nobody --regid=nogroup --clear-groups -- bash -c 'prog ARGS "
        "> /logs/verifier/produced/out 2>/dev/null'`. Re-check: `stat -c %U` on a produced file must "
        "print `nobody`."
    ),
    "enc_denied_to_nobody": (
        "`nobody` could read /private.enc — it must be denied. Ensure /private.enc is mode 600 "
        "(root-only) and never copied into /app or a world-readable path."
    ),
    "root700_denied_to_nobody": (
        "`nobody` could read the root-only 0700 secret dir — tighten so decrypted goldens live ONLY "
        "in a root-owned 0700 dir, never world-readable."
    ),
}


@dataclass
class ValidationState:
    """The SANITY verdict for a task tree at one point in the loop. `oracle_passed`/`nop_passed` are
    the always-measured baselines; `gate_verdict` + `failed_checks` carry the FULL gate result when
    the validator can measure it (local Docker) so the loop iterates to the same bar the SANITY gate
    enforces — not a weaker oracle/nop-only bar (which let `produced_owned_by_nobody` defects ship a
    'green' fill that the gate then rejected)."""

    oracle_passed: bool
    nop_passed: bool
    detail: dict = field(default_factory=dict)
    gate_verdict: str | None = None          # full SANITY verdict when measured (docker path); else None
    failed_checks: list[str] = field(default_factory=list)  # specific gate checks that failed

    @property
    def green(self) -> bool:
        # When the validator measured the full gate (local Docker), green means the WHOLE gate passes
        # — including produced_owned_by_nobody / priv-drop probes. When it can only measure baselines
        # (baseline-trials path, priv-drop deferred per ADR-0017), fall back to oracle=1/nop=0.
        if self.gate_verdict is not None:
            return self.gate_verdict == "pass"
        return self.oracle_passed and self.nop_passed

    def feedback(self) -> str:
        parts = []
        if not self.oracle_passed:
            parts.append("ORACLE did not reward 1 (the reference solution must make the verifier pass)")
        if not self.nop_passed:
            parts.append("NOP did not reward 0 (an empty submission must fail the verifier)")
        for chk in self.failed_checks:
            if chk in ("oracle_reward_1", "nop_reward_0"):
                continue  # already covered by the oracle/nop messages above
            parts.append(_CHECK_REMEDIATION.get(chk, f"SANITY check `{chk}` failed — fix the cause."))
        return "; ".join(parts) or "validation green"


@dataclass
class AgentResult:
    """Outcome of a bounded agentic loop."""

    success: bool
    iterations: int
    state: ValidationState | None
    transcript_tail: str = ""
    reason: str = ""


# (prompt, task_dir) -> agent transcript/stdout. The agent edits files under task_dir in place.
AgentSession = Callable[[str, Path], str]
# (task_dir) -> ValidationState. Builds + runs the verifier's oracle/nop baselines.
Validator = Callable[[Path], "ValidationState"]


AGENT_LOG_DIR = "agent-logs"   # under the RUN dir (sibling of task/), so it never ships to a sweep/CI
AGENT_LOG_FILE = "agent.log"


def _agent_log_path(work_dir: Path) -> Path | None:
    """The live agent-output log for the run that owns `work_dir` (the cell's task/bundle dir). Found by
    walking up to the run root (the dir with state.json) and writing under `<run>/agent-logs/` — OUTSIDE
    task/, so it never ships in a sweep bundle or gets seen by STATIC CI. None when there's no run root (tests)."""
    for p in [Path(work_dir), *Path(work_dir).parents]:
        if (p / "state.json").exists():
            d = p / AGENT_LOG_DIR
            d.mkdir(exist_ok=True)
            return d / AGENT_LOG_FILE
    return None


def log_pipeline_event(work_dir: Path, event: dict) -> None:
    """Append a DETERMINISTIC pipeline marker (a `{"type":"lh", ...}` JSON line) to the run's agent
    log, so the UI's live terminal interleaves phase/validation results with the agent's own streamed
    events. No-op when there's no run root (tests / a bare task dir)."""
    import json as _json

    path = _agent_log_path(Path(work_dir))
    if path is None:
        return
    try:
        with open(path, "a") as f:
            f.write(_json.dumps({"type": "lh", **event}) + "\n")
    except OSError:
        pass


def claude_code_session(
    model: str | None = None, timeout: int = DEFAULT_SESSION_TIMEOUT
) -> AgentSession:
    """Default agentic session: a headless `claude -p` run, allowed to edit files + run bash, with the
    task dir as the working directory. Resolves the binary directly (bypassing the interactive alias)
    like the `llm` boundary does.

    Streams the agent's stdout/stderr LIVE to `<run>/agent-logs/agent.log` (the UI tails it for the
    'Agent output' panel) by handing the child the log fd directly — so progress is visible mid-run
    instead of buffered until exit. The runner is injectable so tests never shell out.

    Auth comes from the CLI keychain, process environment, or the local owner-only ProgramSmith
    settings file; a timeout RAISES so the bg-job wrapper treats it as a retryable error.
    """
    claude_bin = os.getenv("CC_LOGGER_REAL_CLAUDE") or "claude"

    def _run_once(args: list[str], prompt: str, task_dir: Path, log_path: Path | None,
                  env: dict | None) -> tuple[int, str]:
        """One agent session → (returncode, transcript). Raises subprocess.TimeoutExpired on timeout
        (the original contract — the bg-job wrapper treats a raised timeout as a retryable 'error')."""
        if log_path is None:                           # no run root (tests) → buffer as before
            proc = subprocess.run(args, input=prompt, cwd=str(task_dir),
                                  capture_output=True, text=True, timeout=timeout, env=env)
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        # Live path: hand the child the log fd so it writes AS IT RUNS (visible to the UI tail). Recover
        # this session's output afterward by slicing the file from where we started. A timestamped
        # header delimits successive agents/iterations within one run.
        import time as _time
        with open(log_path, "a") as logf:
            logf.write(f"\n===== {_time.strftime('%H:%M:%S')} · agent on {task_dir.name} =====\n")
            logf.flush()
            start = log_path.stat().st_size
            try:
                proc = subprocess.run(args, input=prompt, cwd=str(task_dir),
                                      stdout=logf, stderr=subprocess.STDOUT, text=True,
                                      timeout=timeout, env=env)
            finally:
                logf.flush()
        # the agent conveys success through the task tree + validator, not its exit code
        return proc.returncode, log_path.read_bytes()[start:].decode(errors="replace")

    def _run(prompt: str, task_dir: Path) -> str:
        task_dir = Path(task_dir)
        args = [
            claude_bin, "-p",
            "--model", model or DEFAULT_AGENTIC_MODEL,
            "--permission-mode", "bypassPermissions",  # headless: auto-approve edits/bash
            "--add-dir", str(task_dir),
            # Stream STRUCTURED events (assistant text, thinking, tool calls + results, final result)
            # to the log so the UI renders a LIVE transcript instead of just a final blob. The
            # stream-json format requires --verbose.
            "--output-format", "stream-json", "--verbose",
            *LEAN_MCP_FLAGS,                            # drop inherited MCP servers (auth-safe)
            *NO_SUBAGENT_FLAGS,                         # remove the sub-agent/background spawners (wedge)
            "--append-system-prompt", FOCUS_PROMPT,    # override framework ceremony → work directly
        ]
        log_path = _agent_log_path(task_dir)
        from ..config import model_subprocess_env
        env = model_subprocess_env()
        run_args = [args[0], *api_key_cli_flags(env), *args[1:]]
        _rc, text = _run_once(run_args, prompt, task_dir, log_path, env)
        from ..costlog import record_envelope
        record_envelope(text, model=model or DEFAULT_AGENTIC_MODEL)
        return text

    return _run


def _full_gate_state(res) -> "ValidationState":
    """run_sanity GateResult → ValidationState, measuring the FULL gate (oracle/nop + priv-drop) so the
    loop iterates to the gate's bar."""
    checks = res.detail.get("checks", {})
    return ValidationState(
        oracle_passed=bool(checks.get("oracle_reward_1")),
        nop_passed=bool(checks.get("nop_reward_0")),
        gate_verdict=res.verdict,
        failed_checks=[k for k, ok in checks.items() if not ok],
        detail={"sanity_verdict": res.verdict, **res.detail},
    )


def docker_validator(image_tag: str = "lh-agentic:task", build: bool = True) -> Validator:
    """Local-Docker validator: build the task image and run the SANITY gate's two-phase verifier,
    mapping its oracle=1/nop=0 + priv-drop checks to a ``ValidationState``. Needs local Docker."""
    from ..gates.sanity import run_sanity  # local import: avoids a hard Docker import at module load

    def _validate(task_dir: Path) -> ValidationState:
        return _full_gate_state(run_sanity(Path(task_dir), image_tag=image_tag, build=build))

    return _validate


def baseline_validator(trials_provider: Callable[[Path], list[dict]]) -> Validator:
    """Docker-less validator: given a callable that returns recorded oracle/nop baseline trials for
    the current task tree, compute the SANITY verdict from them. `trials_provider` wraps a sweep
    launch+read cycle (billable) or reads already-produced trial records."""
    from ..gates.sanity import run_sanity_trials

    def _validate(task_dir: Path) -> ValidationState:
        trials = trials_provider(Path(task_dir))
        res = run_sanity_trials(trials)
        checks = res.detail.get("checks", {})
        return ValidationState(
            oracle_passed=bool(checks.get("oracle_baseline_reward_1")),
            nop_passed=bool(checks.get("nop_baseline_reward_0")),
            detail={"sanity_verdict": res.verdict, "source": "baseline-trials", **res.detail},
        )

    return _validate


def default_validator(task_dir: Path, image_tag: str = "lh-agentic:task") -> Validator:
    """The apply-phase validator the agentic cells use when none is injected: local Docker (the full
    SANITY gate). The baseline-trials validator stays EXPLICIT (it needs a trials provider) —
    inject it via ctx."""
    return docker_validator(image_tag=image_tag)


def run_to_green(
    base_prompt: str,
    task_dir: Path,
    *,
    session: AgentSession,
    validator: Validator,
    max_iters: int = 3,
    label: str = "fill",
) -> AgentResult:
    """Bounded iterate-to-(oracle=1, nop=0) loop (the SWE-gen CC pattern).

    Each iteration: run one agent session over `task_dir`, then validate. On failure the verifier
    feedback is appended to the prompt for the next session (so the agent self-corrects), bounded by
    `max_iters`. Pure control flow over the two injected boundaries — no Docker/LLM at import time.
    """
    task_dir = Path(task_dir)
    state: ValidationState | None = None
    transcript = ""
    prompt = base_prompt
    for i in range(1, max_iters + 1):
        log_pipeline_event(task_dir, {"event": "iteration", "n": i, "of": max_iters, "label": label})
        transcript = session(prompt, task_dir)
        state = validator(task_dir)
        if state.green:
            log_pipeline_event(task_dir, {"event": "validated", "n": i, "ok": True,
                                          "detail": f"{label}: oracle=1 / nop=0"})
            return AgentResult(True, i, state, transcript[-800:],
                               f"{label}: oracle=1/nop=0 after {i} iteration(s)")
        log_pipeline_event(task_dir, {"event": "validated", "n": i, "ok": False,
                                      "detail": state.feedback()})
        prompt = (
            f"{base_prompt}\n\n## Previous attempt (iteration {i}) did not validate\n"
            f"{state.feedback()}\nInspect the verifier output, fix the cause, and re-validate."
        )
    log_pipeline_event(task_dir, {"event": "exhausted", "ok": False,
                                  "detail": f"{label}: not green within {max_iters} iteration(s)"})
    return AgentResult(False, max_iters, state, transcript[-800:],
                       f"{label}: did not reach oracle=1/nop=0 within {max_iters} iteration(s) — "
                       f"{state.feedback() if state else 'no validation produced'}")
