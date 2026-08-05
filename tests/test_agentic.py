"""Offline tests for the shared agentic loop (cells/agentic.py): the session + validator are
injected so nothing shells out or touches Docker. Covers the bounded iterate-to-green control flow,
feedback on retry, and the validator adapters."""

from pathlib import Path

from programsmith.cells.agentic import (
    ValidationState,
    baseline_validator,
    run_to_green,
)


def _session_calls(record):
    def _s(prompt: str, task_dir: Path) -> str:
        record.append(prompt)
        return "session ran"
    return _s


def test_default_agentic_model_is_cost_conscious():
    """ADR-0042 rework: the agentic synthesis cells (oracle capture, create fill, synthesize apply)
    are HEAVY cells → Sonnet default (the operator owns the bill; overridable per call)."""
    from programsmith.cells.agentic import DEFAULT_AGENTIC_MODEL
    assert DEFAULT_AGENTIC_MODEL == "claude-sonnet-5"


def test_validation_state_green_and_feedback():
    assert ValidationState(True, True).green
    assert not ValidationState(True, False).green
    fb = ValidationState(False, False).feedback()
    assert "ORACLE" in fb and "NOP" in fb


def test_full_gate_bar_blocks_oracle_nop_only_green():
    """The fill/synthesize loop must iterate to the FULL SANITY gate, not an oracle/nop-only bar.
    A tree that rewards oracle=1/nop=0 but whose Phase-A produced files are root-owned
    (produced_owned_by_nobody) is NOT green when the validator measured the full gate (local Docker),
    and the agent feedback names the specific check + how to fix it. (Regression: this mismatch let a
    'green' fill advance to SANITY, which then failed and burned the bounded revise loop blind.)"""
    st = ValidationState(True, True, gate_verdict="fail",
                         failed_checks=["produced_owned_by_nobody"])
    assert not st.green                       # full gate failed despite oracle=1/nop=0
    fb = st.feedback()
    assert "nobody" in fb and "chmod 777" in fb and "root-shell" in fb
    # with no full-gate measurement (baseline-trials path), green falls back to oracle=1/nop=0
    assert ValidationState(True, True).green
    assert ValidationState(True, True, gate_verdict="pass").green


def test_run_to_green_succeeds_first_iter(tmp_path):
    calls = []
    res = run_to_green("base", tmp_path, session=_session_calls(calls),
                       validator=lambda _d: ValidationState(True, True), max_iters=3)
    assert res.success and res.iterations == 1 and len(calls) == 1


def test_run_to_green_feeds_back_then_succeeds(tmp_path):
    calls = []
    states = iter([ValidationState(True, False), ValidationState(True, True)])

    res = run_to_green("base prompt", tmp_path, session=_session_calls(calls),
                       validator=lambda _d: next(states), max_iters=3)
    assert res.success and res.iterations == 2
    # the second prompt carries the failure feedback from iteration 1
    assert "did not validate" in calls[1] and "NOP did not reward 0" in calls[1]


def test_run_to_green_bounded_failure(tmp_path):
    calls = []
    res = run_to_green("base", tmp_path, session=_session_calls(calls),
                       validator=lambda _d: ValidationState(False, True), max_iters=2, label="fill")
    assert not res.success and res.iterations == 2 and len(calls) == 2
    assert "did not reach oracle=1/nop=0" in res.reason


def test_baseline_validator_maps_baseline_trials(tmp_path):
    trials = [{"agent": "oracle", "reward": 1}, {"agent": "nop", "reward": 0}]
    v = baseline_validator(lambda _d: trials)
    state = v(tmp_path)
    assert state.green and state.detail["source"] == "baseline-trials"

    bad = baseline_validator(lambda _d: [{"agent": "oracle", "reward": 1}, {"agent": "nop", "reward": 1}])
    assert not bad(tmp_path).green


# ---- live agent-output capture (for the UI 'Agent output' panel) ----------------------

def test_agent_log_path_is_under_run_root_outside_task(tmp_path):
    from programsmith.cells.agentic import AGENT_LOG_DIR, _agent_log_path
    (tmp_path / "state.json").write_text("{}")               # run root marker
    task = tmp_path / "task" / "demo"
    task.mkdir(parents=True)
    lp = _agent_log_path(task)
    assert lp == tmp_path / AGENT_LOG_DIR / "agent.log"       # in the run dir…
    assert "task" not in lp.relative_to(tmp_path).parts       # …NOT under task/ (never ships in a sweep)


def test_agent_log_path_none_without_run_root(tmp_path):
    from programsmith.cells.agentic import _agent_log_path
    d = tmp_path / "loose" / "dir"
    d.mkdir(parents=True)
    assert _agent_log_path(d) is None                        # no state.json ancestor → buffer, don't log


def test_session_disallows_subagent_spawner_tools(tmp_path):
    """STRUCTURAL guarantee (not just the FOCUS_PROMPT text): every agentic session denies the
    background/sub-agent spawner tools. FOCUS_PROMPT's 'do NOT spawn subagents' is advisory — the model
    ignored it and spawned a background oracle-watcher that got killed with the session, so the
    iterate-to-green loop never converged (libexpat create-fill wedge). The fix removes the tools, so
    the wedge is impossible. Guard against a future refactor silently dropping the denylist."""
    import os

    from programsmith.cells.agentic import claude_code_session
    (tmp_path / "state.json").write_text("{}")
    task = tmp_path / "task" / "demo"
    task.mkdir(parents=True)
    fake = tmp_path / "fakeclaude"                            # records its own argv, then no-ops
    argv_dump = tmp_path / "argv.txt"
    fake.write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" >{argv_dump}\ncat >/dev/null\necho ok\n')
    fake.chmod(0o755)
    os.environ["CC_LOGGER_REAL_CLAUDE"] = str(fake)
    try:
        claude_code_session(model="x", timeout=30)("do the thing", task)
    finally:
        del os.environ["CC_LOGGER_REAL_CLAUDE"]
    argv = argv_dump.read_text().splitlines()
    assert "--disallowedTools" in argv
    dis = argv[argv.index("--disallowedTools") + 1:]         # the variadic values follow the flag
    for spawner in ("Agent", "Task", "Workflow", "ScheduleWakeup"):
        assert spawner in dis, f"{spawner} must be denied so the cell can't spawn dying background work"
    # the cell's REAL tools are never denied — it still needs to edit files + run the verifier inline
    for real in ("Bash", "Edit", "Read", "Write"):
        assert real not in dis, f"{real} is load-bearing for the cell and must NOT be disallowed"


def test_session_streams_agent_output_to_log_live(tmp_path):
    import os

    from programsmith.cells.agentic import AGENT_LOG_DIR, claude_code_session
    (tmp_path / "state.json").write_text("{}")
    task = tmp_path / "task" / "demo"
    task.mkdir(parents=True)
    fake = tmp_path / "fakeclaude"                            # stand-in for `claude` (no real shell-out)
    fake.write_text("#!/bin/bash\ncat >/dev/null\necho 'AGENT-WORKING-MARKER'\n")
    fake.chmod(0o755)
    os.environ["CC_LOGGER_REAL_CLAUDE"] = str(fake)
    try:
        out = claude_code_session(model="x", timeout=30)("do the thing", task)
    finally:
        del os.environ["CC_LOGGER_REAL_CLAUDE"]
    text = (tmp_path / AGENT_LOG_DIR / "agent.log").read_text()
    assert "AGENT-WORKING-MARKER" in text                    # streamed to the file (UI tails this)
    assert "agent on demo" in text                           # per-run header delimiter
    assert "AGENT-WORKING-MARKER" in out                     # and still returned to the caller


def test_api_key_uses_bare_cli_without_breaking_oauth():
    from programsmith.cells.agentic import api_key_cli_flags

    assert api_key_cli_flags({"ANTHROPIC_API_KEY": "sk-ant-test"}) == ["--bare"]
    assert api_key_cli_flags({"CLAUDE_CODE_OAUTH_TOKEN": "oauth"}) == []
    assert api_key_cli_flags({"ANTHROPIC_API_KEY": "key",
                              "CLAUDE_CODE_OAUTH_TOKEN": "oauth"}) == []


# ---- apply-phase validator (local Docker only) ----------------------------------------------------

def test_default_validator_is_docker():
    """The apply-validator is the local-Docker SANITY gate. Constructing it touches no Docker
    (the import is lazy, inside the returned validator)."""
    from programsmith.cells.agentic import default_validator
    assert callable(default_validator("/tmp/task"))          # constructs without local Docker
