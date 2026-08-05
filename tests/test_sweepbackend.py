"""Offline tests for the sweep backend seam + the local execution engine.

The local path runs the full launch→status→results→analyses state machine with injected fakes
(no Docker, no model), proving the schema + flow without live hardware. The `SweepBackend`
Protocol seam is exercised via the ctx injection knobs the orchestrator threads through.
"""

from pathlib import Path
import threading

import pytest

from programsmith import local_runner as lr
from programsmith import sweepbackend as sb
from programsmith.trials import SweepAgent


@pytest.fixture(autouse=True)
def _local_task_instruction(tmp_path):
    """Direct _docker_solve unit tests use tmp_path as their synthetic task bundle."""
    (tmp_path / "instruction.md").write_text("Reimplement the test tool.\n")


# ---- selection + spend gate ------------------------------------------------------------------
def _isolate_config(monkeypatch, tmp_path):
    """Point config at an empty file so LhConfig.load() can't pick up the repo's real .programsmith/config.json."""
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "cfg.json"))


def test_backend_name_defaults_to_local(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    assert sb.backend_name({}) == "local"
    assert isinstance(sb.get_backend({}), sb.LocalSweepBackend)


def test_ctx_override_wins(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    local = sb.LocalSweepBackend()
    assert sb.get_backend({"sweep_backend": local}) is local
    assert sb.backend_name({"sweep_backend": local}) == "local"


def test_get_backend_threads_injection_knobs(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    runner = lambda spec: {"agent": spec["agent"], "model": spec["model"], "reward": 1,  # noqa: E731
                           "is_probe": False, "status": "completed"}
    be = sb.get_backend({"local_sweeps_root": tmp_path, "local_trial_runner": runner,
                         "local_executor": lambda fn: fn()})
    handle = be.launch(tmp_path / "task", [SweepAgent("claude-code", "opus", 1)], experiment="programsmith-k")
    assert be.status(handle)["complete"] is True


def test_sweep_live_reads_canonical_key():
    assert sb.sweep_live({"sweep_live": True})
    assert not sb.sweep_live({})


def test_backend_satisfies_protocol():
    assert isinstance(sb.LocalSweepBackend(), sb.SweepBackend)


# ---- LocalSweepBackend end-to-end offline (injected trial runner + synchronous executor) -----
def _sync_executor():
    return lambda fn: fn()   # run each trial inline so status() is complete immediately


def test_local_backend_runs_trials_and_reads_pass_at_1(tmp_path):
    seen = []

    def fake_trial(spec):
        seen.append((spec["agent"], spec["model"], spec["trial"]))
        # oracle passes, nop fails, opus solves 2/3
        reward = {"oracle": 1, "nop": 0}.get(spec["agent"])
        if reward is None:
            reward = 1 if spec["trial"] < 2 else 0
        return {"agent": spec["agent"], "model": spec["model"], "reward": reward,
                "is_probe": False, "status": "completed",
                "trajectory": f"work by {spec['agent']} trial {spec['trial']}"}

    be = sb.LocalSweepBackend(root=tmp_path, trial_runner=fake_trial, executor=_sync_executor())
    agents = [SweepAgent("oracle", "default", 1), SweepAgent("nop", "default", 1),
              SweepAgent("claude-code", "anthropic/claude-opus-4-8", 3)]
    handle = be.launch(tmp_path / "task", agents, experiment="programsmith-x-difficulty",
                       extra_flags=["--run-analysis"])
    poll = be.status(handle)
    assert poll["complete"] is True and poll["trials_completed"] == 5

    from programsmith.trials import pass_at_1
    trials = be.results(handle, tmp_path / "out")
    pa = pass_at_1(trials)
    assert pa["aggregate"] == pytest.approx(2 / 3)   # opus 2/3, baselines excluded
    assert ("oracle", "default", 0) in seen
    # results() must MATERIALIZE out_dir (the caller records it as the sweep entry's pull_dir,
    # which the good-failure deep audit and _per_case_findings resolve LATER) — a dangling
    # pull_dir means the audit reads an empty dir and every downstream reader is blind
    out = tmp_path / "out"
    assert out.exists() and list(out.rglob("result.json"))


def test_local_backend_analyses_labels_frontier_only(tmp_path):
    def fake_trial(spec):
        reward = {"oracle": 1, "nop": 0}.get(spec["agent"], 0)
        return {"agent": spec["agent"], "model": spec["model"], "reward": reward,
                "is_probe": False, "status": "completed", "trajectory": "t"}

    labels = {"claude-code": "GOOD_FAILURE"}
    be = sb.LocalSweepBackend(root=tmp_path, trial_runner=fake_trial, executor=_sync_executor(),
                              classifier=lambda traj, rec: labels.get(rec["agent"], "HARNESS_ERROR"))
    agents = [SweepAgent("oracle", "default", 1), SweepAgent("nop", "default", 1),
              SweepAgent("claude-code", "anthropic/claude-opus-4-8", 2)]
    handle = be.launch(tmp_path / "task", agents, experiment="programsmith-x-difficulty")
    out = be.analyses(handle, agents=("claude-code",))
    assert out["total"] == 2 and out["pending"] == 0
    assert all(a["label"] == "GOOD_FAILURE" for a in out["analyses"])


def test_local_backend_trial_crash_records_errored(tmp_path):
    def boom(spec):
        raise RuntimeError("docker exploded")

    be = sb.LocalSweepBackend(root=tmp_path, trial_runner=boom, executor=_sync_executor())
    handle = be.launch(tmp_path / "task", [SweepAgent("claude-code", "opus", 1)], experiment="programsmith-x")
    assert be.status(handle)["complete"] is True
    trials = be.results(handle, tmp_path / "out")
    assert trials and trials[0]["reward"] is None and trials[0]["status"] == "errored"


def test_interrupted_trial_stays_incomplete_and_resumes_from_clean_copy(tmp_path):
    """Ctrl-C is not a measured harness failure: no .done is written, status is read-only and
    reports the persisted plan incomplete, and an explicit resume replays just that trial."""
    calls = []

    def interrupted(spec):
        calls.append("interrupted")
        Path(spec["task_dir"]).joinpath("partial.txt").write_text("partial")
        raise lr.TrialInterrupted("operator interrupt")

    task = tmp_path / "task"
    task.mkdir()
    (task / "pristine.txt").write_text("yes")
    be = sb.LocalSweepBackend(root=tmp_path, trial_runner=interrupted, executor=_sync_executor())
    handle = be.launch(task, [SweepAgent("claude-code", "opus", 1)], experiment="programsmith-resume")
    poll = be.status(handle)
    assert poll["complete"] is False and poll["incomplete"] is True
    trial_dir = tmp_path / handle / "trials" / "claude-code-0"
    assert not (trial_dir / ".done").exists()
    assert (trial_dir / ".interrupted").exists()

    def completed(spec):
        calls.append("completed")
        work = Path(spec["task_dir"])
        assert (work / "pristine.txt").read_text() == "yes"
        assert not (work / "partial.txt").exists()  # interrupted workspace was discarded
        return {"agent": spec["agent"], "model": spec["model"], "reward": 1,
                "is_probe": False, "status": "completed"}

    be._trial_runner = completed
    assert be.resume(handle) == 1
    assert be.status(handle)["complete"] is True
    assert calls == ["interrupted", "completed"]


def test_status_never_launches_an_incomplete_persisted_plan(tmp_path):
    """Polling from the CLI/dashboard must never restart billable work by itself."""
    exp = tmp_path / "programsmith-read-only"
    (exp / "trials").mkdir(parents=True)
    (exp / "plan.json").write_text(sb._json_dumps({
        "specs": [{"agent": "claude-code", "model": "opus", "trial": 0}],
        "task_dir": str(tmp_path / "task"), "extra_flags": []}))
    called = []
    be = sb.LocalSweepBackend(root=tmp_path, trial_runner=lambda spec: called.append(spec),
                              executor=_sync_executor())
    poll = be.status("programsmith-read-only")
    assert poll["incomplete"] is True and poll["tasks_running"] == 0
    assert called == []


def test_live_claim_prevents_duplicate_resume_submission(tmp_path):
    pending = []
    be = sb.LocalSweepBackend(root=tmp_path,
                              trial_runner=lambda spec: {
                                  "agent": spec["agent"], "model": spec["model"], "reward": 1,
                                  "is_probe": False, "status": "completed"},
                              executor=pending.append)
    handle = be.launch(tmp_path / "task", [SweepAgent("claude-code", "opus", 1)],
                       experiment="programsmith-claimed")
    assert len(pending) == 1
    assert be.status(handle)["tasks_running"] == 1
    assert be.resume(handle) == 0
    assert len(pending) == 1
    pending.pop()()
    assert be.status(handle)["complete"] is True


def test_local_backend_ignores_unknown_extra_flags(tmp_path):
    """Unknown launch flags (e.g. a legacy image-cache buster) must be recorded and IGNORED, running
    trials normally — never an error on an unrecognized extra flag."""
    def fake_trial(spec):
        return {"agent": spec["agent"], "model": spec["model"], "reward": 1,
                "is_probe": False, "status": "completed"}

    be = sb.LocalSweepBackend(root=tmp_path, trial_runner=fake_trial, executor=_sync_executor())
    handle = be.launch(tmp_path / "task", [SweepAgent("claude-code", "anthropic/claude-haiku-4-5", 2)],
                       experiment="programsmith-x-difficulty", extra_flags=["--run-analysis", "--force-build"])
    assert be.status(handle)["complete"] is True
    trials = be.results(handle, tmp_path / "out")
    assert len(trials) == 2 and all(t["reward"] == 1 for t in trials)


def test_solver_command_mini_swe_universal_harness():
    """mini-swe is the UNIVERSAL local harness (pre-installed in every task image): it gets the
    FULL litellm model id, yolo mode, an explicit cost limit (mini's own default would cap
    silently), and writes its trajectory into the mounted workspace."""
    for name in ("mini-swe", "mini-swe-agent"):
        cmd = lr._solver_command(name, "zai/glm-5.2")[-1]
        assert "mini -m zai/glm-5.2" in cmd
        assert "-y" in cmd and "-l 0" in cmd            # explicit cost limit, 0 = disabled default
        assert "/workspace/.trajectory.json" in cmd
        assert "$(cat instruction.md)" in cmd            # the task TEXT, not a guessed file flag


def test_solver_command_native_clis_bare_model_and_verified_flags():
    """Native CLIs get the BARE model name (no litellm provider prefix) and only in-container
    verified headless flags: claude -p + bypassPermissions; codex exec with the sandbox bypass
    (the container IS the sandbox) + skip-git-repo-check (the workspace is not a git repo);
    gemini -p + --yolo."""
    cc = lr._solver_command("claude-code", "anthropic/claude-opus-4-8")[-1]
    assert "claude -p --model claude-opus-4-8" in cc and "bypassPermissions" in cc
    # stream-json so a budget-killed trial still leaves a real trajectory (plain -p buffers)
    assert "--output-format stream-json" in cc and "--verbose" in cc
    cx = lr._solver_command("codex", "openai/gpt-5.5")[-1]
    assert "codex exec -m gpt-5.5" in cx
    assert "--dangerously-bypass-approvals-and-sandbox" in cx and "--skip-git-repo-check" in cx
    gm = lr._solver_command("gemini-cli", "gemini/gemini-3-flash")[-1]
    assert "gemini -m gemini-3-flash" in gm and "--yolo" in gm and "-p" in gm


def test_solver_command_unknown_harness_fails_fast():
    """An unknown harness must fail FAST and loud (recorded as an errored trial) instead of
    burning a solve timeout on a command that cannot exist."""
    with pytest.raises(NotImplementedError, match="mini-swe"):
        lr._solver_command("terminus-2", "anthropic/claude-opus-4-8")


def test_model_env_scoped_to_trial_provider_without_argv_values(monkeypatch, tmp_path):
    """Credentials are scoped to THIS trial's provider (+ harness vendor) — a Gemini trial's
    container never sees the Anthropic key, and the OAuth token rides ONLY the claude-code
    harness (litellm can't bill it)."""
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    for v in ("GOOGLE_API_KEY", "ZAI_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-x")
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-tok")

    gm = lr._model_env("mini-swe-agent", "gemini/gemini-3-flash")
    assert gm == {"GEMINI_API_KEY": "g-key", "GOOGLE_API_KEY": "g-key"}

    anth_mini = lr._model_env("mini-swe-agent", "anthropic/claude-haiku-4-5")
    assert anth_mini == {"ANTHROPIC_API_KEY": "sk-ant-x"}
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in anth_mini    # litellm can't bill the OAuth token

    anth_cc = lr._model_env("claude-code", "anthropic/claude-opus-4-8")
    assert anth_cc == {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-tok", "ANTHROPIC_API_KEY": "sk-ant-x"}

    cx = lr._model_env("codex", "openai/gpt-5.5")
    assert cx == {"OPENAI_API_KEY": "sk-oai-x"}

    flags = lr._model_env_flags(anth_cc)
    assert flags == ["-e", "CLAUDE_CODE_OAUTH_TOKEN", "-e", "ANTHROPIC_API_KEY"]
    assert "oauth-tok" not in " ".join(flags) and "sk-ant-x" not in " ".join(flags)


def test_claude_code_oauth_falls_back_to_keychain(monkeypatch, tmp_path):
    """The zero-env-var macOS user: no CLAUDE_CODE_OAUTH_TOKEN in the env, but the claude CLI's
    keychain login exists — the runner lifts the access token for the claude-code trial container
    (which cannot reach the host keychain). Env var, when present, wins (never shadowed)."""
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(lr, "_keychain_oauth_token", lambda: "keychain-tok")
    cc = lr._model_env("claude-code", "anthropic/claude-opus-4-8")
    assert cc["CLAUDE_CODE_OAUTH_TOKEN"] == "keychain-tok"
    # mini-swe never gets the OAuth token, keychain or not (litellm can't bill it)
    mini = lr._model_env("mini-swe-agent", "anthropic/claude-haiku-4-5")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in mini
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "env-tok")
    cc2 = lr._model_env("claude-code", "anthropic/claude-opus-4-8")
    assert cc2["CLAUDE_CODE_OAUTH_TOKEN"] == "env-tok"


def test_solve_timeout_kills_container_and_grades_partial_work(monkeypatch, tmp_path):
    """Regression (tengo frontier wipe-out): a solver that exhausts the solve budget is an AGENT
    outcome, not a harness error — the runner must kill the named container (a bare subprocess
    timeout leaves it running and billing forever) and return the trajectory so the partial work
    in /workspace goes to VERIFY."""
    import subprocess as sp
    killed = []

    class FakeProcess:
        def __init__(self):
            self.calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise sp.TimeoutExpired("docker run", timeout or 0, output=b"partial traj")
            return "partial traj", ""

        def kill(self):
            pass

    def fake_run(cmd, **kw):
        if cmd[:2] == ["docker", "kill"]:
            killed.append(cmd[2])
            class P:  # noqa: N801
                returncode, stdout, stderr = 0, "", ""
            return P()
        raise AssertionError(cmd)
    monkeypatch.setattr(lr, "_ensure_overlay", lambda tag, agent: tag)
    monkeypatch.setattr(lr, "_model_env", lambda a, m: {})
    monkeypatch.setattr(lr.subprocess, "run", fake_run)
    monkeypatch.setattr(lr.subprocess, "Popen", lambda *a, **k: FakeProcess())
    traj = lr._docker_solve(tmp_path, "img", "claude-code", "anthropic/claude-sonnet-5")
    assert killed and killed[0].startswith("programsmith-solve-")
    assert "partial traj" in traj and "budget" in traj


def test_interrupt_stops_registered_solver_container(monkeypatch, tmp_path):
    """The first foreground interrupt targets the named Docker worker and the trial unwinds as
    interrupted rather than being graded or recorded as a harness error."""
    started = threading.Event()
    released = threading.Event()
    stopped = []

    class FakeProcess:
        def communicate(self, timeout=None):
            started.set()
            assert released.wait(2)
            return "", ""

        def kill(self):
            released.set()

    def fake_run(cmd, **kw):
        class P:  # noqa: N801
            returncode, stdout, stderr = 0, "", ""
        if cmd[:2] == ["docker", "stop"]:
            stopped.append(cmd[-1])
            released.set()
        return P()

    monkeypatch.setattr(lr, "_ensure_overlay", lambda tag, agent: tag)
    monkeypatch.setattr(lr, "_model_env", lambda a, m: {})
    monkeypatch.setattr(lr.subprocess, "run", fake_run)
    monkeypatch.setattr(lr.subprocess, "Popen", lambda *a, **k: FakeProcess())
    token = lr.cancellation_token()
    errors = []

    def solve():
        try:
            lr._docker_solve(tmp_path, "img", "claude-code", "anthropic/claude-sonnet-5",
                             cancel_token=token)
        except Exception as exc:  # expected TrialInterrupted from the changed cancellation epoch
            errors.append(exc)

    thread = threading.Thread(target=solve)
    thread.start()
    assert started.wait(2)
    assert lr.interrupt_active_solves() == 1
    thread.join(2)
    assert not thread.is_alive()
    assert stopped and stopped[0].startswith("programsmith-solve-")
    assert len(errors) == 1 and isinstance(errors[0], lr.TrialInterrupted)


def test_interrupt_during_process_launch_stops_new_container(monkeypatch, tmp_path):
    """If SIGINT lands inside Popen before registration, the launching thread observes the changed
    epoch and stops its own just-created container instead of letting an untracked model call run."""
    stopped = []

    class FakeProcess:
        @staticmethod
        def communicate(timeout=None):
            return "", ""

    def fake_popen(*args, **kwargs):
        assert lr.interrupt_active_solves() == 0
        return FakeProcess()

    def fake_run(cmd, **kwargs):
        class P:  # noqa: N801
            returncode, stdout, stderr = 0, "", ""
        if cmd[:2] == ["docker", "stop"]:
            stopped.append(cmd[-1])
        return P()

    monkeypatch.setattr(lr, "_ensure_overlay", lambda tag, agent: tag)
    monkeypatch.setattr(lr, "_model_env", lambda a, m: {})
    monkeypatch.setattr(lr.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(lr.subprocess, "run", fake_run)
    token = lr.cancellation_token()
    with pytest.raises(lr.TrialInterrupted):
        lr._docker_solve(tmp_path, "img", "claude-code", "anthropic/claude-sonnet-5",
                         cancel_token=token)
    assert stopped and stopped[0].startswith("programsmith-solve-")


def test_claude_code_solve_container_gets_is_sandbox(monkeypatch, tmp_path):
    """Regression (tengo HARNESS_ERROR wipe-out): the overlay container runs as root and claude
    refuses bypassPermissions under root unless IS_SANDBOX=1 marks the env as a sandbox. The
    flag rides ONLY the claude-code harness."""
    seen = {}

    def fake_popen(cmd, **kw):
        seen["cmd"] = cmd
        seen["env"] = kw.get("env", {})
        class P:  # noqa: N801 — minimal subprocess.Popen stand-in
            @staticmethod
            def communicate(timeout=None):
                return "", ""
        return P()

    monkeypatch.setattr(lr, "_ensure_overlay", lambda tag, agent: tag)
    monkeypatch.setattr(lr, "_model_env", lambda a, m: (
        {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-secret"} if a == "claude-code" else {}))
    monkeypatch.setattr(lr.subprocess, "Popen", fake_popen)
    lr._docker_solve(tmp_path, "img", "claude-code", "anthropic/claude-opus-4-8")
    assert "IS_SANDBOX=1" in " ".join(seen["cmd"])
    assert "CLAUDE_CODE_OAUTH_TOKEN" in seen["cmd"]
    assert "oauth-secret" not in " ".join(seen["cmd"])
    assert seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-secret"
    lr._docker_solve(tmp_path, "img", "mini-swe-agent", "anthropic/claude-haiku-4-5")
    assert "IS_SANDBOX" not in " ".join(seen["cmd"])


def test_solve_and_verify_share_persisted_submission_mount(monkeypatch, tmp_path):
    """Regression (tengo frontier structural-0): ProgramBench contracts the agent to build into
    /app/submission — an IMAGE path. Solve and verify are separate --rm containers, so without a
    shared host mount the submission dies with the solve container and verify grades an empty dir.
    Both phases must mount the SAME per-trial .submission dir at /app/submission."""
    cmds = []

    def fake_popen(cmd, **kw):
        cmds.append(" ".join(cmd))
        class P:  # noqa: N801
            @staticmethod
            def communicate(timeout=None):
                return "", ""
        return P()

    def fake_run(cmd, **kw):
        cmds.append(" ".join(cmd))
        class P:  # noqa: N801
            returncode, stdout, stderr = 0, "", ""
        return P()

    monkeypatch.setattr(lr, "_ensure_overlay", lambda tag, agent: tag)
    monkeypatch.setattr(lr, "_model_env", lambda a, m: {})
    monkeypatch.setattr(lr.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(lr.subprocess, "run", fake_run)
    lr._docker_solve(tmp_path, "img", "claude-code", "anthropic/claude-sonnet-5")
    (tmp_path / "tests").mkdir()
    lr._docker_verify(tmp_path, "img")
    mount = f"{tmp_path / '.submission'}:/app/submission:rw"
    assert all(mount in c for c in cmds), cmds
    assert (tmp_path / ".submission").is_dir()


def test_solve_workspace_hides_tests_and_expected_outputs(monkeypatch, tmp_path):
    """The model must see the instruction, public image assets, and /app/submission—but never the
    host task's testsuite. Harbor mounts tests only for VERIFY; the local runner must match it."""
    (tmp_path / "tests" / "testsuite").mkdir(parents=True)
    (tmp_path / "tests" / "testsuite" / "cases.json").write_text('[{"secret": true}]')
    seen = {}

    def fake_popen(cmd, **kw):
        seen["cmd"] = cmd
        class P:  # noqa: N801
            @staticmethod
            def communicate(timeout=None):
                return "trajectory", ""
        return P()

    monkeypatch.setattr(lr, "_ensure_overlay", lambda tag, agent: tag)
    monkeypatch.setattr(lr, "_model_env", lambda a, m: {})
    monkeypatch.setattr(lr.subprocess, "Popen", fake_popen)
    lr._docker_solve(tmp_path, "img", "claude-code", "anthropic/claude-sonnet-5")

    workspace = tmp_path / ".solver-workspace"
    assert sorted(p.name for p in workspace.iterdir()) == ["instruction.md"]
    joined = " ".join(seen["cmd"])
    assert f"{workspace}:/workspace:rw" in joined
    assert f"{tmp_path}:/workspace:rw" not in joined
    assert "tests/testsuite" not in joined


def test_ensure_overlay_cached_per_image_and_harness(monkeypatch):
    """The solver overlay is keyed by (base-image CONTENT, harness): an existing overlay image is
    reused without a build; a missing one triggers exactly one `docker build` from the overlay
    Dockerfile (FROM <task image> + the vendor CLI)."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        class P:
            returncode = 0
            stdout = "sha256:abcdef1234567890\n"
            stderr = ""
        if cmd[:3] == ["docker", "image", "inspect"] and cmd[-1].startswith("programsmith-overlay:"):
            P.returncode = 0 if fake_run.overlay_exists else 1
        return P

    fake_run.overlay_exists = True
    monkeypatch.setattr(lr.subprocess, "run", fake_run)
    tag = lr._ensure_overlay("programsmith-local:demo-claude-code-0", "claude-code")
    assert tag == "programsmith-overlay:abcdef123456-claude-code"
    assert not any(c[:2] == ["docker", "build"] for c in calls)   # cache hit → no build

    calls.clear()
    fake_run.overlay_exists = False
    tag2 = lr._ensure_overlay("programsmith-local:demo-claude-code-0", "claude-code")
    assert tag2 == tag
    builds = [c for c in calls if c[:2] == ["docker", "build"]]
    assert len(builds) == 1                                        # miss → exactly one build


# ---- local_runner.run_trial dispatch (injected docker seams) ---------------------------------
def _fakes(reward_out="REWARD=1", build_ok=True, traj="agent did work"):
    def builder(td, tag):
        return build_ok, "build log"

    def solver(td, tag, agent, model):
        return traj

    def verifier(td, tag, *, restore_reference=False):
        verifier.calls.append(restore_reference)
        return reward_out
    verifier.calls = []
    return builder, solver, verifier


def test_run_trial_oracle_reward(tmp_path):
    b, s, v = _fakes("REWARD=1")
    rec = lr.run_trial({"agent": "oracle", "model": "default", "trial": 0, "task_dir": str(tmp_path)},
                       builder=b, solver=s, verifier=v)
    assert rec["reward"] == 1.0 and rec["status"] == "completed" and rec["agent"] == "oracle"


def test_run_trial_oracle_restores_reference_before_grading(tmp_path):
    """Regression (tengo oracle-baseline=0): a ProgramBench task tree ships UNSOLVED — the oracle
    baseline must replay solution/solve.sh before grading (restore_reference=True), while nop and
    frontier trials grade the tree exactly as the (non-)agent left it."""
    b, s, v = _fakes("REWARD=1")
    lr.run_trial({"agent": "oracle", "model": "default", "trial": 0, "task_dir": str(tmp_path)},
                 builder=b, solver=s, verifier=v)
    assert v.calls == [True]
    lr.run_trial({"agent": "nop", "model": "default", "trial": 0, "task_dir": str(tmp_path)},
                 builder=b, solver=s, verifier=v)
    lr.run_trial({"agent": "claude-code", "model": "m", "trial": 0, "task_dir": str(tmp_path)},
                 builder=b, solver=s, verifier=v)
    assert v.calls == [True, False, False]


def test_backend_stages_per_trial_workspace_copy(tmp_path):
    """Regression (tengo dev-run contamination): the runner bind-mounts the task tree read-write, so
    every trial must get its OWN pristine copy — a shared tree lets concurrent trials edit each
    other's workspace and later trials inherit earlier solutions (pass@1 stops meaning pass@1)."""
    src = tmp_path / "task"
    (src / "environment").mkdir(parents=True)
    (src / "environment" / "Dockerfile").write_text("FROM scratch\n")
    seen = {}

    def fake_trial(spec):
        seen[(spec["agent"], spec["trial"])] = spec["task_dir"]
        return {"agent": spec["agent"], "model": spec["model"], "reward": 1,
                "is_probe": False, "status": "completed"}

    be = sb.LocalSweepBackend(root=tmp_path / "sweeps", trial_runner=fake_trial,
                              executor=_sync_executor())
    be.launch(src, [SweepAgent("claude-code", "m", 2), SweepAgent("nop", "default", 1)],
              experiment="programsmith-iso")
    dirs = set(seen.values())
    assert len(dirs) == 3, "each trial must run in its own workspace"
    for (agent, trial), d in seen.items():
        p = Path(d)
        assert p != src and p.name == "task" and f"{agent}-{trial}" in str(p)
        assert (p / "environment" / "Dockerfile").read_text() == "FROM scratch\n"


def test_run_trial_nop_reward(tmp_path):
    b, s, v = _fakes("REWARD=0")
    rec = lr.run_trial({"agent": "nop", "model": "default", "trial": 0, "task_dir": str(tmp_path)},
                       builder=b, solver=s, verifier=v)
    assert rec["reward"] == 0.0


def test_run_trial_frontier_attaches_trajectory(tmp_path):
    b, s, v = _fakes("REWARD=1", traj="opus edited lib.rs")
    rec = lr.run_trial({"agent": "claude-code", "model": "opus", "trial": 1, "task_dir": str(tmp_path)},
                       builder=b, solver=s, verifier=v)
    assert rec["reward"] == 1.0 and rec["trajectory"] == "opus edited lib.rs"


def test_run_trial_build_failure_is_errored(tmp_path):
    b, s, v = _fakes(build_ok=False)
    rec = lr.run_trial({"agent": "claude-code", "model": "opus", "trial": 0, "task_dir": str(tmp_path)},
                       builder=b, solver=s, verifier=v)
    assert rec["reward"] is None and rec["status"] == "errored" and "build failed" in rec["error"]


def test_run_trial_no_reward_is_errored(tmp_path):
    b, s, v = _fakes("no reward printed")
    rec = lr.run_trial({"agent": "nop", "model": "default", "trial": 0, "task_dir": str(tmp_path)},
                       builder=b, solver=s, verifier=v)
    assert rec["reward"] is None and rec["status"] == "errored"


# ---- local classifier (injected model runner, no network) -----------------------------------
def test_classify_trajectory_validates_label():
    lbl = lr.classify_trajectory("agent solved it", {"reward": 1},
                                 runner=lambda p: '{"label": "GOOD_SUCCESS"}')
    assert lbl == "GOOD_SUCCESS"


def test_classify_trajectory_rejects_unknown_then_falls_back():
    # an un-parseable/invalid answer → CellError → HARNESS_ERROR (never invents a real verdict)
    lbl = lr.classify_trajectory("x", {"reward": 0}, runner=lambda p: "not json at all")
    assert lbl == "HARNESS_ERROR"
