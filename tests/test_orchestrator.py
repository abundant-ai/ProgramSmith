"""Tests for the orchestrator driver: the deterministic loop (stub registry) + real handlers."""

import json
from pathlib import Path

from programsmith.fsm import Stage
from programsmith.manifest import Manifest, SourceInfo
from programsmith.orchestrator import (
    REGISTRY,
    StageResult,
    _h_calibrate,
    _h_create,
    _h_oracle,
    _h_sanity,
    drive,
    peek,
)
from programsmith.state import RunState


def _run_at(run_dir, verdicts, sweeps=None, run_config=None):
    s = RunState.start("r", "task:x", "demo")
    for v in verdicts:
        s.advance(v)
    s.save(run_dir)
    m = Manifest(run_id="r", task_identity="task:x", slug="demo")
    m.source = SourceInfo(repo="o/n", pinned_sha="abc123", primary_language="Fortran")
    if sweeps:
        m.sweeps = sweeps
    if run_config:
        m.run_config = run_config
    m.save(run_dir)
    return run_dir


def _make_screenable_source(run_dir: Path, manifest: Manifest, *, loc: int = 6_000) -> None:
    """Give a TASK_MATRIX handler test a deterministic local C CLI checkout."""
    root = run_dir / "source"
    root.mkdir(exist_ok=True)
    (root / "main.c").write_text("int main(void) { return 0; }\n")
    (root / "README.md").write_text("# widget\nOffline stdin transformer with JSON output.\n")
    manifest.source = SourceInfo(
        repo="o/n", pinned_sha="abc123", primary_language="C", size_loc=loc,
        clone_path=str(root), license="MIT", license_class="permissive",
    )


def _rc(diff_max=0.60, full_min=0.0, full_max=0.60, full_on_too_hard="keep_verified_hard"):
    """An EXPLICIT per-run band config so these tests don't couple to the (recently pivoted)
    runconfig defaults — the handler decision logic is what's under test, not the default window."""
    return {
        "difficulty": {"agents": [{"harness": "mini-swe", "model": "zai/glm-5.2", "n_trials": 3}],
                       "band": {"basis": "aggregate", "min_pass": 0.0, "max_pass": diff_max}},
        "full": {"agents": [{"harness": "mini-swe", "model": "anthropic/claude-opus-4-8", "n_trials": 3}],
                 "band": {"basis": "aggregate", "min_pass": full_min, "max_pass": full_max,
                          "on_too_hard": full_on_too_hard}},
    }


def _bundle(tmp):
    b = tmp / "bundle"
    (b / "oracle" / "src").mkdir(parents=True)
    (b / "oracle" / "Cargo.toml").write_text("[package]\nname='o'\n")
    (b / "goldens").mkdir(parents=True)
    (b / "goldens" / "goldens_public.json").write_text("[]")
    (b / "goldens" / "goldens_heldout.json").write_text("[]")
    return b


def _stub(verdict=None, **kw):
    return lambda s, m, rd, ctx: StageResult(verdict=verdict, reason="stub", **kw)


# ---- loop logic (stub registry) ------------------------------------------------------

def test_drive_flows_then_halts_at_human(tmp_path):
    _run_at(tmp_path, [])  # at INGEST_LOCK
    registry = {Stage.INGEST_LOCK: _stub("pass"), Stage.TASK_MATRIX: _stub(human=True)}
    res = drive(tmp_path, registry=registry)
    assert res.halted == "human"
    assert res.final_stage == "TASK_MATRIX"
    assert [s["stage"] for s in res.steps] == ["INGEST_LOCK"]
    assert (tmp_path / "drive.json").exists()


def test_drive_halts_blocked(tmp_path):
    _run_at(tmp_path, ["pass", "selected"])  # at ORACLE_GOLDEN
    registry = {Stage.ORACLE_GOLDEN: _stub(blocked=True)}
    res = drive(tmp_path, registry=registry)
    assert res.halted == "blocked" and res.final_stage == "ORACLE_GOLDEN" and res.steps == []


def test_drive_halts_terminal(tmp_path):
    _run_at(tmp_path, ["fail"])  # INGEST_LOCK -> DROPPED
    res = drive(tmp_path, registry=REGISTRY)
    assert res.halted == "terminal" and res.final_status == "dropped"


def test_drive_halts_paused(tmp_path):
    _run_at(tmp_path, ["pass"])  # at TASK_MATRIX
    st = RunState.load(tmp_path); st.pause(); st.save(tmp_path)
    res = drive(tmp_path, registry={Stage.TASK_MATRIX: _stub(human=True)})
    assert res.halted == "paused"


def test_draft_stops_and_exports_before_any_sweep(tmp_path):
    """Static-CI-only mode is a structural stop, not a spend flag a resume can forget."""
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass"])
    man = Manifest.load(tmp_path)
    man.pipeline_mode = "draft"
    man.cell_model = "claude-opus-4-8"
    man.save(tmp_path)
    task = tmp_path / "task" / "demo"
    task.mkdir(parents=True)
    (task / "task.toml").write_text("[task]\n")

    def must_not_run(*_args):
        raise AssertionError("draft crossed into a sweep")

    outbox = tmp_path / "out"
    res = drive(tmp_path, ctx={"outbox_dir": str(outbox)},
                registry={Stage.DIFFICULTY_SWEEP: must_not_run})
    assert res.halted == "draft"
    assert res.final_stage == "DIFFICULTY_SWEEP"
    assert (outbox / "drafts" / "demo" / "task.toml").exists()
    provenance = json.loads((outbox / "drafts" / "demo" / ".provenance.json").read_text())
    assert provenance["calibrated"] is False


# ---- real handlers -------------------------------------------------------------------

def _fake_skeleton(monkeypatch, todos=("fill me",)):
    """Stand-in for the (heavy, generator-backed) CREATE assembly: the orchestrator tests exercise
    the LOOP + the agentic bg machinery, not the vendored ProgramBench generator (that coverage
    lives in test_create / test_programbench_generator). Writes a minimal complete-looking task."""
    from types import SimpleNamespace

    from programsmith import orchestrator as orch

    def fake(manifest, out_dir, **_kw):
        d = Path(out_dir)
        (d / "tests").mkdir(parents=True, exist_ok=True)
        (d / "task.toml").write_text("[task]\n")
        (d / "tests" / "test.sh").write_text("#!/bin/bash\n")
        return SimpleNamespace(todos=list(todos))
    monkeypatch.setattr(orch, "assemble_skeleton", fake)


def test_real_drive_oracle_create_then_sanity_blocked(tmp_path, monkeypatch):
    _run_at(tmp_path, ["pass", "selected"])  # at ORACLE_GOLDEN
    _fake_skeleton(monkeypatch, todos=())    # 0 TODOs → CREATE passes without a fill agent
    # override SANITY so the test never invokes Docker
    registry = {**REGISTRY, Stage.SANITY: _stub(blocked=True)}
    res = drive(tmp_path, ctx={"oracle_bundle": str(_bundle(tmp_path))}, registry=registry)
    assert [s["stage"] for s in res.steps] == ["ORACLE_GOLDEN", "CREATE"]
    assert res.final_stage == "SANITY" and res.halted == "blocked"
    # ORACLE adopted into the manifest; CREATE wrote the task skeleton
    man = json.loads((tmp_path / "manifest.json").read_text())
    assert man["oracle"] is not None
    assert (tmp_path / "task" / "demo" / "task.toml").exists()


def test_h_oracle_blocked_without_bundle(tmp_path):
    _run_at(tmp_path, ["pass", "selected"])
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    res = _h_oracle(st, man, tmp_path, {})
    assert res.blocked and "bundle" in res.reason


def test_h_oracle_bundle_is_scoped_to_its_source(tmp_path):
    """A pre-built bundle is SOURCE-SPECIFIC. The fleet driver passes one global ctx to every run, so a
    bundle_slug scopes the bundle: _h_oracle must NOT adopt it for a run whose slug differs (else every
    source silently becomes that bundle's task — the minpack-contamination bug). A mismatch falls through
    to the no-bundle path; a MATCHING slug adopts as normal."""
    _run_at(tmp_path, ["pass", "selected"])  # at ORACLE_GOLDEN; fixture slug = 'demo'
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    bundle = str(_bundle(tmp_path))                        # build the reference bundle ONCE
    # bundle scoped to 'minpack' but this run is 'demo' → must be IGNORED (not adopted)
    res = _h_oracle(st, man, tmp_path, {"oracle_bundle": bundle, "oracle_bundle_slug": "minpack"})
    assert res.blocked and "bundle" in res.reason          # fell through; minpack bundle NOT adopted
    # a matching slug DOES adopt (the bundle belongs to this run)
    res2 = _h_oracle(st, man, tmp_path, {"oracle_bundle": bundle, "oracle_bundle_slug": man.slug})
    assert res2.verdict == "pass"                          # adopted
    # no slug guard at all → adopt as before (single explicit run that chose the bundle)
    res3 = _h_oracle(st, man, tmp_path, {"oracle_bundle": bundle})
    assert res3.verdict == "pass"


def _elf_amd64(tail: bytes) -> bytes:
    """Minimal linux/amd64 ELF header + distinguishing tail (satisfies the bundle arch gate)."""
    return (b"\x7fELF" + bytes([2, 1, 1]) + bytes(9) + (2).to_bytes(2, "little")
            + (0x3E).to_bytes(2, "little") + bytes(44) + tail)


def _pb_bundle(bundle_dir, n_cases=100):
    """A complete ProgramBench oracle-pair bundle (ADR-0038 layout) at bundle_dir."""
    b = Path(bundle_dir)
    (b / "docs").mkdir(parents=True, exist_ok=True)
    (b / "docs" / "help.txt").write_text("usage: tool [flags]")
    (b / "oracle_bin").write_bytes(_elf_amd64(b"ORACLE-ELF-BYTES"))
    (b / "prebuilt_bin").write_bytes(_elf_amd64(b"PREBUILT-ELF-BYTES"))
    ts = b / "testsuite"
    (ts / "fixtures").mkdir(parents=True, exist_ok=True)
    (ts / "cases.json").write_text(json.dumps(
        [{"id": f"case-{i}", "args": [],
          "expected_stdout": f"case-{i}.expected.stdout", "expected_rc": 0}
         for i in range(n_cases)]))
    for i in range(n_cases):
        (ts / "fixtures" / f"case-{i}.expected.stdout").write_text(f"result {i}\n")
    (b / "determinism.ok").write_text("")
    return b


def test_h_oracle_bg_completes_on_programbench_bundle(tmp_path):
    """Regression (pb10 fleet stall): the background generate path's _complete() sentinel checked
    ONLY the legacy rewrite-port layout (oracle/ + goldens/*.json), so a run whose generate agent
    had produced a COMPLETE ProgramBench bundle false-blocked forever with 'agent finished but
    artifact incomplete'. _complete() must recognize the ADR-0038 bundle (via _bundle_status, the
    same check adopt_existing enforces) and apply it deterministically."""
    _run_at(tmp_path, ["pass", "selected"])  # at ORACLE_GOLDEN
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    gen = _pb_bundle(tmp_path / "gen-bundle")
    ctx = {"agentic": True, "agentic_background": True, "oracle_generate_dir": str(gen)}
    res = _h_oracle(st, man, tmp_path, ctx)   # complete on disk → adopt, never launch/poll a job
    assert res.verdict == "pass" and "adopted ProgramBench" in res.reason
    assert man.oracle and man.oracle["n_cases"] == 100


def test_h_oracle_generate_default_dir_is_not_the_task_dir(tmp_path):
    """Regression (pb10 bundle wipe): generate-mode's DEFAULT bundle dir used to be CREATE's task
    dir (run_dir/task/<slug>) — build_task rmtree's that dir first, so CREATE destroyed the very
    bundle it consumes. The default must be a sibling (run_dir/oracle-bundle), and a complete
    bundle there must adopt with manifest.oracle paths pointing OUTSIDE the task dir."""
    _run_at(tmp_path, ["pass", "selected"])  # at ORACLE_GOLDEN
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    _pb_bundle(tmp_path / "oracle-bundle")   # complete bundle at the NEW default location
    ctx = {"agentic": True, "agentic_background": True}   # no oracle_generate_dir → default
    res = _h_oracle(st, man, tmp_path, ctx)
    assert res.verdict == "pass"
    task_dir = str(tmp_path / "task" / "demo")
    assert not man.oracle["oracle_bin"].startswith(task_dir)
    # a ProgramBench bundle is NOT a task → task_bundle_path must NOT point at it (the sweeps
    # would upload the held-out oracle pair + goldens into the agent-visible env)
    assert (man.snapshot or {}).get("task_bundle_path") is None


def test_h_oracle_bg_incomplete_pb_bundle_still_blocks(tmp_path):
    """The counterpart guard: a PARTIAL ProgramBench bundle (missing determinism marker) must NOT
    satisfy _complete() — the run stays blocked/polling instead of applying an unchecked bundle."""
    _run_at(tmp_path, ["pass", "selected"])
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    gen = _pb_bundle(tmp_path / "gen-bundle")
    (gen / "determinism.ok").unlink()
    ctx = {"agentic": True, "agentic_background": True, "oracle_generate_dir": str(gen),
           "agent_session": lambda p, td: "x"}
    res = _h_oracle(st, man, tmp_path, ctx)
    assert res.blocked and man.oracle is None  # not applied; job machinery takes over


def test_h_create_assembles(tmp_path, monkeypatch):
    _run_at(tmp_path, ["pass", "selected", "pass"])  # at CREATE
    _fake_skeleton(monkeypatch, todos=())            # generator covered in test_create
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    res = _h_create(st, man, tmp_path, {})
    assert res.verdict == "pass"
    assert (tmp_path / "task" / "demo" / "tests" / "test.sh").exists()


def test_h_create_blocks_cleanly_on_legacy_oracle_bundle(tmp_path):
    """The rewritten CREATE cell needs the ProgramBench ORACLE bundle (DESIGN §6.4 keys); a legacy
    manifest raises CellError inside the cell — _h_create must turn that into an HONEST block
    (fix upstream / retry), never a raised exception that aborts the whole fleet pass."""
    _run_at(tmp_path, ["pass", "selected", "pass"])  # at CREATE; manifest.oracle lacks the bundle
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    res = _h_create(st, man, tmp_path, {})
    assert res.blocked and "CREATE cannot assemble" in res.reason


def test_h_calibrate_reads_sweep(tmp_path):
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass", "done"],
            sweeps={"difficulty": {"pass_at_1": 0.72}}, run_config=_rc(diff_max=0.60))  # at CALIBRATE
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    res = _h_calibrate(st, man, tmp_path, {})
    assert res.verdict == "harden"  # 0.72 > the configured 0.60 ceiling
    # the smoke decision is recorded for provenance (DESIGN §4.1)
    assert man.sweeps["difficulty"]["calibrate"]["verdict"] == "harden"


# ---- SANITY baseline-trials path (Docker-less, ADR-0017) -----------------------------

def test_h_sanity_uses_recorded_baseline_trials(tmp_path):
    sweeps = {"sanity": {"trials": [
        {"agent": "oracle", "reward": 1.0},
        {"agent": "nop", "reward": 0.0},
    ]}}
    _run_at(tmp_path, ["pass", "selected", "pass", "pass"], sweeps=sweeps)  # at SANITY
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    res = _h_sanity(st, man, tmp_path, {})
    assert res.verdict == "pass" and "baseline" in res.reason  # no Docker needed


def test_h_sanity_baseline_failure_routes_fail(tmp_path):
    sweeps = {"sanity": {"trials": [
        {"agent": "oracle", "reward": 1.0},
        {"agent": "nop", "reward": 1.0},  # nop should be 0
    ]}}
    _run_at(tmp_path, ["pass", "selected", "pass", "pass"], sweeps=sweeps)
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    res = _h_sanity(st, man, tmp_path, {})
    assert res.verdict == "fail"


def test_h_sanity_runs_local_docker_gate_or_blocks(tmp_path, monkeypatch):
    """Without recorded baseline trials, SANITY runs the full local-Docker two-phase gate when
    Docker is up, and blocks honestly (never fakes a verdict) when it isn't."""
    from programsmith import orchestrator as orch
    from programsmith.gates import GateResult

    _run_at(tmp_path, ["pass", "selected", "pass", "pass"])  # at SANITY, no baseline recorded
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)

    monkeypatch.setattr(orch, "_docker_ok", lambda: True)
    monkeypatch.setattr(orch, "run_sanity", lambda *a, **k: GateResult("pass", "local docker oracle=1/nop=0"))
    res = orch._h_sanity(st, man, tmp_path, {})
    assert res.verdict == "pass" and "docker" in res.reason

    monkeypatch.setattr(orch, "_docker_ok", lambda: False)
    res2 = orch._h_sanity(st, man, tmp_path, {})
    assert res2.blocked and "Docker" in res2.reason


# ---- read-only peek (what is this run waiting on?) -----------------------------------

def test_peek_task_matrix_mode_aware(tmp_path, monkeypatch):
    """ADR-0039: TASK_MATRIX defaults AUTO — peek reads it as an auto gate (runnable with the LLM
    cell enabled, blocked without), and as 'human' ONLY when the config/ctx says human mode."""
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))  # defaults: both gates auto
    _run_at(tmp_path, ["pass"])  # at TASK_MATRIX
    w = peek(tmp_path)
    assert w["kind"] == "blocked" and "auto-pick" in w["reason"]       # auto, but no LLM cell in ctx
    w2 = peek(tmp_path, ctx={"agentic": True})
    assert w2["kind"] == "runnable" and "auto-pick" in w2["reason"]
    w3 = peek(tmp_path, ctx={"task_matrix_mode": "human"})
    assert w3["kind"] == "human" and "#1" in w3["reason"]


def test_peek_reports_blocked_sanity_without_baseline(tmp_path):
    _run_at(tmp_path, ["pass", "selected", "pass", "pass"])  # at SANITY, no baseline recorded
    w = peek(tmp_path)
    assert w["kind"] == "blocked" and "sweep-read" in w["reason"].lower()


def test_peek_sanity_runnable_with_baseline(tmp_path):
    sweeps = {"sanity": {"trials": [{"agent": "oracle", "reward": 1}, {"agent": "nop", "reward": 0}]}}
    _run_at(tmp_path, ["pass", "selected", "pass", "pass"], sweeps=sweeps)
    w = peek(tmp_path)
    assert w["kind"] == "runnable"


def test_peek_terminal(tmp_path):
    _run_at(tmp_path, ["fail"])  # INGEST -> DROPPED
    w = peek(tmp_path)
    assert w["kind"] == "terminal"
    assert w["reason"]                              # surfaces the SPECIFIC last-event reason, not generic
    # A pre-CREATE drop has no task to harden. Re-open is reserved for downstream task failures.
    assert w["can_reopen"] is False


def test_reopen_for_harden_unblocks_a_dropped_run(tmp_path):
    """A run dropped AFTER a task was built (here: CALIBRATE flag_broken) can be re-opened by the
    operator: it re-enters the harden loop (SYNTHESIZE) with a fresh budget, recorded in history, and is
    no longer terminal. (An ingest-dropped run — no task — is refused; see
    test_reopen_refuses_run_with_no_task_built.)"""
    from programsmith.fsm import Stage
    # drive past CREATE so a task exists, then drop at CALIBRATE → a legitimately reopenable drop
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass", "done", "flag_broken"])
    st = RunState.load(tmp_path)
    assert st.terminal and st.current_stage is Stage.DROPPED
    st.reopen_for_harden()
    assert st.current_stage is Stage.SYNTHESIZE and st.status == "in_progress"
    assert st.harden == 0 and st.synthesize_rejoin is Stage.STATIC_CI
    assert st.history[-1].verdict == "harden" and "re-opened" in st.history[-1].reason
    # synth trigger reads it as a harden patch (not revise)
    from programsmith.orchestrator import _synth_trigger
    move, _from, _why = _synth_trigger(st)
    assert move == "harden"


def test_reopen_rejects_a_live_run(tmp_path):
    _run_at(tmp_path, ["pass"])  # at TASK_MATRIX (not terminal)
    st = RunState.load(tmp_path)
    import pytest
    with pytest.raises(ValueError):
        st.reopen_for_harden()


def test_peek_difficulty_running_is_polling_not_needs_launch(tmp_path):
    """When the difficulty sweep is already running, the 'waiting on' peek says it's polling
    (kind 'waiting', NOT 'blocked' — it's a benign in-flight wait, not a stuck run) and NOT the stale
    'needs a live sweep — launch it' (which contradicted the driver line)."""
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass"],  # at DIFFICULTY_SWEEP
            sweeps={"difficulty": {"status": "running", "experiment": "ac576ac9"}})
    w = peek(tmp_path)
    assert w["kind"] == "waiting"
    assert "running on local" in w["reason"] and "ac576ac9" in w["reason"]
    assert "needs a live" not in w["reason"]


# ---- agentic opt-in handlers (session/validator injected via ctx) --------------------

def test_h_create_agentic_fill_opt_in(tmp_path, monkeypatch):
    from programsmith.cells.agentic import ValidationState
    from programsmith.orchestrator import _h_create
    _run_at(tmp_path, ["pass", "selected", "pass"])  # at CREATE
    _fake_skeleton(monkeypatch)
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    ctx = {"agentic_fill": True, "agent_session": lambda p, td: "ok",
           "validator": lambda _d: ValidationState(True, True)}
    res = _h_create(st, man, tmp_path, ctx)
    assert res.verdict == "pass" and "oracle=1/nop=0" in res.reason


def test_h_create_agentic_fill_blocks_when_red(tmp_path, monkeypatch):
    from programsmith.cells.agentic import ValidationState
    from programsmith.orchestrator import _h_create
    _run_at(tmp_path, ["pass", "selected", "pass"])
    _fake_skeleton(monkeypatch)
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    ctx = {"agentic_fill": True, "agent_session": lambda p, td: "x",
           "validator": lambda _d: ValidationState(False, True), "max_iters": 1}
    res = _h_create(st, man, tmp_path, ctx)
    assert res.blocked and "incomplete" in res.reason


def test_h_create_agentic_fill_background_non_blocking(tmp_path, monkeypatch):
    """With agentic_background, CREATE-fill runs as a NON-BLOCKING bg job: first drive launches
    (blocked 'background'), and once the job lands the next drive applies it (verdict pass). This is
    what keeps a multi-minute `claude -p` from freezing the fleet loop."""
    import time

    from programsmith import jobs
    from programsmith.cells.agentic import ValidationState
    from programsmith.orchestrator import _h_create
    # Nested run dir: the concurrency guard scans run_dir.parent for sibling runs' jobs.json —
    # a bare tmp_path would make EVERY other test's tmp dir a "sibling", so leftover running
    # jobs from unrelated tests would queue this launch (the historical order-dependent flake).
    tmp_path = tmp_path / "runs" / "r"
    tmp_path.parent.mkdir(parents=True)
    _run_at(tmp_path, ["pass", "selected", "pass"])  # at CREATE
    _fake_skeleton(monkeypatch)
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    ctx = {"agentic": True, "agentic_background": True, "agent_session": lambda p, td: "ok",
           "validator": lambda _d: ValidationState(True, True)}
    r1 = _h_create(st, man, tmp_path, ctx)
    assert r1.blocked and "background" in r1.reason          # launched, did not block on the agent
    for _ in range(100):                                      # injected session → finishes near-instantly
        if jobs.get_jobs(tmp_path).get("create-fill", {}).get("status") in ("done", "error"):
            break
        time.sleep(0.05)
    assert jobs.get_jobs(tmp_path)["create-fill"]["status"] == "done"
    r2 = _h_create(st, man, tmp_path, ctx)
    assert r2.verdict == "pass" and "validated" in r2.reason


def test_clear_errored_sweeps_resets_only_errored_and_discards_experiment(tmp_path):
    """The sweep half of the `retry` lever: errored entries drop (fresh burst next pass) and the
    poisoned experiment dir is retired via backend.discard — never re-adopted as complete; done
    sweeps are untouched."""
    from programsmith.orchestrator import clear_errored_sweeps
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass"],
            sweeps={"difficulty": {"status": "errored", "experiment": "e-diff", "attempts": 2},
                    "full": {"status": "done", "experiment": "e-full", "pass_at_1": 0.5}})
    discarded = []

    class _B:
        def discard(self, exp):
            discarded.append(exp)

    import programsmith.orchestrator as orc
    m = Manifest.load(tmp_path)
    old = orc._resolve_backend
    orc._resolve_backend = lambda ctx: _B()
    try:
        cleared = clear_errored_sweeps(m, tmp_path)
    finally:
        orc._resolve_backend = old
    assert cleared == ["difficulty"] and discarded == ["e-diff"]
    m2 = Manifest.load(tmp_path)   # persisted: the errored entry is gone, the done one intact
    assert "difficulty" not in m2.sweeps and m2.sweeps["full"]["status"] == "done"


def test_h_full_sweep_hardens_on_saturated_frontier(tmp_path):
    """FULL SWEEP emits a data-driven verdict from the band (ADR-0024/0040): above the window →
    harden (via the harden-review auditor); in-window → done. No hardcoded forward verdict. The
    recorded band_verdict is persisted for QA_GATE."""
    from programsmith.orchestrator import _h_full_sweep
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass", "done", "proceed", "clean"],
            run_config=_rc(full_min=0.30, full_max=0.70))
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    man.sweeps = {"full": {"status": "done", "claude_code": 1.0, "codex": 0.2, "aggregate": 1.0}}
    assert _h_full_sweep(st, man, tmp_path, {}).verdict == "harden"   # aced → too easy
    assert man.sweeps["full"]["band_verdict"] == "too_easy"
    man.sweeps = {"full": {"status": "done", "claude_code": 0.4, "codex": 0.2, "aggregate": 0.4}}
    assert _h_full_sweep(st, man, tmp_path, {}).verdict == "done"     # in the 1/3–2/3 window
    assert man.sweeps["full"]["band_verdict"] == "keep"


def test_h_full_sweep_zero_pass_routes_through_goodfail(tmp_path):
    """Below the frontier floor (incl. 0/N) the good-failure gate decides (ADR-0041, hardened per
    ADR-0048): the deep trajectory audit — OUR OWN LLM over the failed trials' transcripts — is
    MANDATORY for keep_hard. TrialClassifier labels are only a prefilter: a BAD_FAILURE still
    short-circuits to ease, but all-GOOD_FAILURE labels alone can no longer ship a 0-pass task.
    The audit is bounded to ONE per generation (cached)."""
    import json as _json

    from programsmith.orchestrator import _h_full_sweep
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass", "done", "proceed", "clean"],
            run_config=_rc(full_min=0.30, full_max=0.70))
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)

    def entry(labels):
        return {"full": {"status": "done", "aggregate": 0.0, "pass_at_1": 0.0,
                         "analysis": {"labels": [{"trial_id": f"t{i}", "label": l}
                                                 for i, l in enumerate(labels)],
                                      "breakdown": {l: labels.count(l) for l in set(labels)}}}}

    def headroom_payload(n):
        return _json.dumps({"trials": [{"trial_id": f"t{i}", "failure_mode": "capability_headroom",
                                        "evidence": "e"} for i in range(n)], "summary": "hard"})

    # all-GOOD_FAILURE labels are NOT enough — the deep audit must confirm before keep_hard
    man.sweeps = entry(["GOOD_FAILURE", "GOOD_FAILURE", "GOOD_FAILURE"])
    audits = []
    res = _h_full_sweep(st, man, tmp_path,
                        {"llm_runner": lambda p: (audits.append(p), headroom_payload(3))[1]})
    assert res.verdict == "done" and man.sweeps["full"]["hard_keep"] is True
    assert "headroom" in res.reason
    assert len(audits) == 1                                             # the audit actually ran
    assert man.sweeps["full"]["goodfail"]["source"] == "deep_audit"

    # all-GOOD_FAILURE labels but the audit finds a design defect → EASE (audit outranks labels)
    man.sweeps = entry(["GOOD_FAILURE", "GOOD_FAILURE"])
    defect = _json.dumps({"trials": [{"trial_id": "t0", "failure_mode": "task_design_failure",
                                      "evidence": "ambiguous spec"}], "summary": "defect"})
    assert _h_full_sweep(st, man, tmp_path, {"llm_runner": lambda _p: defect}).verdict == "ease"

    # a BAD_FAILURE label → ease immediately (a known defect needs no audit), no LLM call made
    man.sweeps = entry(["GOOD_FAILURE", "BAD_FAILURE"])
    assert _h_full_sweep(st, man, tmp_path, {}).verdict == "ease"
    assert man.sweeps["full"]["goodfail"]["source"] == "label_gate"

    # inconclusive labels → deep audit; its validated modes decide + are cached.
    # The audit must cover EVERY failed trial (t0 AND t1) to keep_hard (invariant #4 coverage guard).
    man.sweeps = entry(["HARNESS_ERROR", "HARNESS_ERROR"])
    res = _h_full_sweep(st, man, tmp_path, {"llm_runner": lambda _p: headroom_payload(2)})
    assert res.verdict == "done" and man.sweeps["full"]["hard_keep"] is True
    assert man.sweeps["full"]["goodfail"]["verdict"] == "keep_hard"     # cached per generation
    # a re-drive at the SAME generation reuses the cache (no second audit — runner absent is fine)
    man.sweeps["full"].pop("hard_keep")
    res2 = _h_full_sweep(st, man, tmp_path, {})
    assert res2.verdict == "done" and "cached" in res2.reason


def test_h_full_sweep_enforce_window_eases_verified_hard(tmp_path):
    """ADR-0048: with band.on_too_hard='enforce_window', audit-verified capability headroom at 0/N
    does NOT ship — it routes to EASE toward the window (bounded by the frontier tune budget).
    hard_keep is never set; a 0% task cannot exit DONE under enforcement."""
    import json as _json

    from programsmith.orchestrator import _h_full_sweep
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass", "done", "proceed", "clean"],
            run_config=_rc(full_min=0.30, full_max=0.70, full_on_too_hard="enforce_window"))
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    man.sweeps = {"full": {"status": "done", "aggregate": 0.0, "pass_at_1": 0.0,
                           "analysis": {"labels": [{"trial_id": "t0", "label": "GOOD_FAILURE"},
                                                   {"trial_id": "t1", "label": "GOOD_FAILURE"}]}}}
    payload = _json.dumps({"trials": [{"trial_id": "t0", "failure_mode": "capability_headroom", "evidence": "e"},
                                      {"trial_id": "t1", "failure_mode": "capability_headroom", "evidence": "e"}],
                           "summary": "hard"})
    res = _h_full_sweep(st, man, tmp_path, {"llm_runner": lambda _p: payload})
    assert res.verdict == "ease" and "enforce" in res.reason
    assert not man.sweeps["full"].get("hard_keep")
    # the audit verdict is still recorded (provenance): headroom was verified, policy chose the window
    assert man.sweeps["full"]["goodfail"]["verdict"] == "keep_hard"
    # a design defect under enforcement still eases (same route, defect-aimed findings)
    man.sweeps = {"full": {"status": "done", "aggregate": 0.0, "pass_at_1": 0.0,
                           "analysis": {"labels": [{"trial_id": "t0", "label": "BAD_FAILURE"}]}}}
    assert _h_full_sweep(st, man, tmp_path, {}).verdict == "ease"


def test_h_full_sweep_goodfail_partial_audit_coverage_does_not_keep(tmp_path):
    """Invariant #4 coverage guard (review fix): a deep audit that omits some failed trials must NOT
    manufacture keep_hard from the subset it chose to report — an under-covering all-headroom audit
    is downgraded to REVISE (re-audit; bounded), so the LLM's choice of trial set can't ship a
    0-pass task."""
    import json as _json

    from programsmith.orchestrator import _h_full_sweep
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass", "done", "proceed", "clean"],
            run_config=_rc(full_min=0.30, full_max=0.70))
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    man.sweeps = {"full": {"status": "done", "aggregate": 0.0, "pass_at_1": 0.0,
                           "analysis": {"labels": [{"trial_id": "t0", "label": "HARNESS_ERROR"},
                                                   {"trial_id": "t1", "label": "HARNESS_ERROR"},
                                                   {"trial_id": "t2", "label": "HARNESS_ERROR"}]}}}
    # the audit reports only t0 as headroom, silently omitting t1/t2
    payload = {"trials": [{"trial_id": "t0", "failure_mode": "capability_headroom", "evidence": "e"}],
               "summary": "hard"}
    res = _h_full_sweep(st, man, tmp_path, {"llm_runner": lambda _p: _json.dumps(payload)})
    assert res.verdict == "revise"
    assert not man.sweeps["full"].get("hard_keep")   # NOT kept — coverage was incomplete


def test_h_full_sweep_goodfail_pending_labels_escalates(tmp_path):
    """A partial all-GOOD_FAILURE label set with trials still pending classification must NOT keep_hard
    off the incomplete view — it escalates to the deep audit (review fix)."""
    import json as _json

    from programsmith.orchestrator import _h_full_sweep
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass", "done", "proceed", "clean"],
            run_config=_rc(full_min=0.30, full_max=0.70))
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    # 1 GOOD_FAILURE classified, 2 still pending → label_gate withholds keep_hard → deep audit runs
    man.sweeps = {"full": {"status": "done", "aggregate": 0.0, "pass_at_1": 0.0,
                           "analysis": {"labels": [{"trial_id": "t0", "label": "GOOD_FAILURE"}],
                                        "pending": 2}}}
    payload = {"trials": [{"trial_id": "t0", "failure_mode": "task_design_failure", "evidence": "blocker"}],
               "summary": "defect"}
    res = _h_full_sweep(st, man, tmp_path, {"llm_runner": lambda _p: _json.dumps(payload)})
    # the deep audit (not the partial label view) decides — here it found a design defect → ease
    assert res.verdict == "ease"


def test_h_full_sweep_bad_success_revises_before_band(tmp_path):
    """Any frontier BAD_SUCCESS = a gameable verifier → REVISE (fix it) — even when the band reads
    in-window. Never ship, never tune difficulty around, a hackable reward."""
    from programsmith.orchestrator import _h_full_sweep
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass", "done", "proceed", "clean"],
            run_config=_rc(full_min=0.30, full_max=0.70))
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    man.sweeps = {"full": {"status": "done", "aggregate": 0.4, "pass_at_1": 0.4,
                           "analysis": {"breakdown": {"BAD_SUCCESS": 1, "GOOD_FAILURE": 2}}}}
    res = _h_full_sweep(st, man, tmp_path, {})
    assert res.verdict == "revise" and "BAD_SUCCESS" in res.reason


def test_h_full_sweep_revises_on_broken_integrity(tmp_path):
    """A broken authoritative band — oracle≠1 / nop≠0 on the real (closed-internet) sweep — is a broken
    verifier/environment, NOT a difficulty signal: FULL_SWEEP must REVISE (fix the task), never advance to
    QA_GATE — even if the frontier band looks in-range. Integrity is checked BEFORE everything else."""
    from programsmith.orchestrator import _h_full_sweep
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass", "done", "proceed", "clean"],
            run_config=_rc(full_min=0.30, full_max=0.70))
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    # in-band frontier (would otherwise be "done") but the oracle didn't pass → revise, not advance
    man.sweeps = {"full": {"status": "done", "claude_code": 0.4, "codex": 0.2, "aggregate": 0.4,
                           "integrity": {"verdict": "fail", "reason": "ORACLE did not reward 1"}}}
    assert _h_full_sweep(st, man, tmp_path, {}).verdict == "revise"
    # a passing integrity check does not interfere: saturation still hardens
    man.sweeps = {"full": {"status": "done", "claude_code": 1.0, "codex": 0.2, "aggregate": 1.0,
                           "integrity": {"verdict": "pass", "reason": "ok"}}}
    assert _h_full_sweep(st, man, tmp_path, {}).verdict == "harden"


def test_bundle_too_large_guard(tmp_path, monkeypatch):
    """An oversized task bundle is caught BEFORE launch with the real reason (oracle battery
    too big), instead of a cryptic storage error after burning the retry budget. (Regression: flac's
    889MB raw-audio bundle failed the upload 5×.)"""
    from programsmith.orchestrator import _bundle_too_large
    monkeypatch.setenv("PROGRAMSMITH_MAX_TASK_MB", "1")
    d = tmp_path / "task"
    d.mkdir()
    (d / "small.txt").write_bytes(b"x" * 100)
    assert _bundle_too_large(d) is None                       # under the cap → ok
    (d / "big.bin").write_bytes(b"0" * 2_000_000)             # 2 MB > 1 MB cap
    msg = _bundle_too_large(d)
    assert msg and "MB" in msg and "cap 1 MB" in msg


def test_reopen_refuses_run_with_no_task_built():
    """Reopen re-enters at SYNTHESIZE (patch an existing task). A run dropped BEFORE a task was built
    (e.g. INGEST license-fail) has no task → SYNTHESIZE would crash on a missing task dir. Reopen must
    refuse it; a run that reached a post-CREATE stage re-opens normally. (Regression: re-opening
    ingest-dropped farm runs sent them to SYNTHESIZE → 'No such file: /data/runs/<slug>/task/<slug>'.)"""
    from programsmith.fsm import Stage
    from programsmith.state import RunState, StageEvent
    no_task = RunState(run_id="r", task_identity="t", slug="awk", current_stage=Stage.DROPPED,
                       history=[StageEvent(stage=Stage.INGEST_LOCK, verdict="fail",
                                           next=Stage.DROPPED, reason="license unrecognized")])
    try:
        no_task.reopen_for_harden()
        refused = False
    except ValueError as e:
        refused = "no CREATE" in str(e) or "task was built" in str(e)
    assert refused, "reopen must refuse a run dropped before any task was built"
    # a run that DID build a task (reached CALIBRATE) re-opens into the harden loop as before
    built = RunState(run_id="r", task_identity="t", slug="minpack", current_stage=Stage.BLOCKED,
                     history=[StageEvent(stage=Stage.CALIBRATE, verdict="harden",
                                         next=Stage.BLOCKED, reason="saturated")])
    built.reopen_for_harden()
    assert built.current_stage is Stage.SYNTHESIZE


def _blocked_by_harden(slug="demo", harden=None, reason="calibrate: pass@1 saturated (>0.60); harden bound exhausted"):
    from programsmith.fsm import HARDEN_MAX, Stage
    from programsmith.state import StageEvent
    return RunState(run_id="r", task_identity="t", slug=slug, current_stage=Stage.BLOCKED,
                    harden=HARDEN_MAX - 1 if harden is None else harden,
                    history=[StageEvent(stage=Stage.CALIBRATE, verdict="harden",
                                        next=Stage.BLOCKED, reason=reason)])


def test_harden_block_revivable_when_bound_raised(tmp_path):
    """A HARDEN_MAX increase self-applies UNIFORMLY: any run BLOCKED by harden-exhaustion at an older
    bound is revivable iff harden < current MAX — regardless of the band (we don't pre-judge; the real
    re-measurement decides). A genuinely-exhausted block (harden==MAX) or a non-harden block
    (revise-exhaustion) is NOT revived — those are correct terminal outcomes."""
    from programsmith.fsm import HARDEN_MAX
    from programsmith.orchestrator import _is_harden_revivable

    assert _is_harden_revivable(_blocked_by_harden())                         # borderline → revive
    assert _is_harden_revivable(_blocked_by_harden())                         # saturated-hard ALSO revives now
    assert not _is_harden_revivable(_blocked_by_harden(harden=HARDEN_MAX))    # truly exhausted at the bound
    revise_block = _blocked_by_harden(reason="static CI failed; revise bound exhausted")
    revise_block.history[-1].verdict = "fail"
    assert not _is_harden_revivable(revise_block)                            # different block → terminal


def test_drive_revives_harden_block_and_reenters_calibrate(tmp_path):
    """drive() un-blocks a revivable harden-exhausted run in place (records a `revive` step) and
    re-enters the stage that wanted to harden — so the bound bump needs no manual reset."""
    from programsmith.fsm import Stage
    _blocked_by_harden().save(tmp_path)
    m = Manifest(run_id="r", task_identity="t", slug="demo")
    m.sweeps = {"difficulty": {"pass_at_1": 0.667}}
    m.save(tmp_path)
    res = drive(tmp_path, registry={Stage.CALIBRATE: _stub(human=True)})  # stop cleanly after re-entry
    assert any(s["verdict"] == "revive" for s in res.steps)              # the un-block was recorded
    assert res.final_stage == "CALIBRATE" and res.halted == "human"      # re-entered, no longer terminal
    assert RunState.load(tmp_path).current_stage is Stage.CALIBRATE


def test_h_synth_blocked_without_agentic_flag(tmp_path):
    from programsmith.orchestrator import _h_synth
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "fail"])  # SANITY fail -> SYNTHESIZE
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    assert st.current_stage.value == "SYNTHESIZE"
    res = _h_synth(st, man, tmp_path, {})
    assert res.blocked and "agentic" in res.reason


def test_synth_trigger_derives_move_from_history(tmp_path):
    from programsmith.orchestrator import _synth_trigger
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "fail"])  # SANITY fail -> SYNTHESIZE
    st = RunState.load(tmp_path)
    move, from_stage, _reason = _synth_trigger(st)
    assert from_stage == "SANITY" and move == "revise"  # a fail edge maps to a 'revise' patch


# ---- standardized sweep naming + auditor overlay (no timestamps) ---------------------

def _complete_bundle(run_dir):
    task = run_dir / "task" / "demo"
    task.mkdir(parents=True, exist_ok=True)
    (task / "task.toml").write_text("[task]\n")
    (task / "instruction.md").write_text("ORIGINAL TASK INSTRUCTION")
    return task


class _FakeBackend:
    """A minimal SweepBackend Protocol stand-in for exercising the orchestrator's sweep state
    machine transitions (launch→poll→results→analyses) with exact control over each response."""

    name = "local"
    needs_upload = False
    artifact_subdir = ".sweeps"

    def __init__(self, *, status=None, results=None, analyses=None, handle="exp1"):
        self._status = status or {"complete": True, "tasks_running": 0, "trials_completed": 1,
                                  "trials_total": 1, "incomplete": False}
        self._results = results or []
        self._analyses = analyses or {"analyses": [], "pending": 0, "failed": 0, "total": 0}
        self._handle = handle
        self.launches = []           # [(task_dir, agents, experiment, extra_flags)]

    def launch(self, task_dir, agents, *, experiment, extra_flags=None):
        self.launches.append((task_dir, agents, experiment, list(extra_flags or [])))
        return self._handle if self._handle is not None else experiment

    def status(self, handle):
        return dict(self._status)

    def results(self, handle, out_dir):
        return list(self._results)

    def analyses(self, handle, *, agents):
        a = self._analyses
        return a.pop(0) if isinstance(a, list) else dict(a)

    def pull_artifacts(self, handle, out_dir):
        from pathlib import Path
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        return Path(out_dir)


def test_full_sweep_reruns_when_cancelled_measured_nothing(tmp_path):
    """End-to-end of the minpack wedge fix: a sweep whose every frontier trial ERRORED (reward None)
    measured nothing → it records `errored` and RE-RUNS a fresh, distinctly-named experiment instead
    of finalizing an empty band (which would wrongly advance past FULL_SWEEP)."""
    from programsmith.orchestrator import _h_full_sweep
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass", "done", "proceed", "clean"])
    _complete_bundle(tmp_path)
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    man.sweeps = {"full": {"status": "running", "experiment": "592031d2", "attempts": 1}}
    man.save(tmp_path)
    fake = _FakeBackend(handle=None, results=[
        {"agent": "oracle", "model": "default", "reward": 1.0},
        {"agent": "nop", "model": "default", "reward": 0.0},
        {"agent": "claude-code", "model": "opus", "reward": None, "status": "errored"},
    ])
    ctx = {"sweep_live": True, "sweep_backend": fake}
    r1 = _h_full_sweep(st, man, tmp_path, ctx)               # poll→complete→measured nothing
    assert r1.blocked and man.sweeps["full"]["status"] == "errored"
    r2 = _h_full_sweep(st, man, tmp_path, ctx)               # relaunch a fresh experiment
    assert r2.blocked and man.sweeps["full"]["status"] == "running"
    assert fake.launches and fake.launches[0][2] == "programsmith-demo-full-retry1"  # distinct retry name
    assert man.sweeps["full"]["experiment"] == "programsmith-demo-full-retry1"


def test_full_sweep_ignores_foreign_agent_trials(tmp_path):
    """Regression for the upstream pb10 FALSE-DONE (ADR-0047): a results source can carry ANOTHER
    stage's trials (a task-scoped cloud pull; an imported pull tree with mixed experiments). Every
    configured frontier trial errored, yet the sweep used to finalize pass@1=0.0 over the foreign
    completed trials and sail to a false DONE. Scoped results (trials.scope_trials) must exclude
    the foreign trials so the existing all-frontier-trials-errored guard fires: record `errored`
    and re-run, never finalize a band over foreign data."""
    from programsmith.orchestrator import _h_full_sweep
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass", "done", "proceed", "clean"],
            run_config=_rc(full_min=0.30, full_max=0.70))
    _complete_bundle(tmp_path)
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    man.sweeps = {"full": {"status": "running", "experiment": "46b81a7d", "attempts": 0}}
    man.save(tmp_path)
    fake = _FakeBackend(results=[
        # this sweep's own trials: baselines fine, EVERY frontier trial errored at agent startup
        {"agent": "oracle", "model": "default", "reward": 1.0},
        {"agent": "nop", "model": "default", "reward": 0.0},
        {"agent": "mini-swe-agent", "model": "anthropic/claude-opus-4-8", "reward": None, "status": "errored"},
        {"agent": "mini-swe", "model": "global.anthropic.claude-opus-4-8", "reward": None, "status": "errored"},
        {"agent": "mini-swe", "model": "claude-opus-4-8", "reward": None, "status": "errored"},
        # foreign trials co-present in the results source (e.g. the DIFFICULTY smoke stage):
        # completed trials that used to masquerade as the frontier measurement
        {"agent": "claude-code", "model": "anthropic/claude-haiku-4-5", "reward": 0.0},
        {"agent": "claude-code", "model": "anthropic/claude-haiku-4-5", "reward": 0.0},
    ])
    res = _h_full_sweep(st, man, tmp_path, {"sweep_live": True, "sweep_backend": fake})
    assert res.blocked
    assert man.sweeps["full"]["status"] == "errored"          # NOT finalized over foreign data
    assert "frontier trial(s) errored" in man.sweeps["full"]["summary"]
    assert man.sweeps["full"].get("pass_at_1") is None        # no foreign-data band recorded


def test_h_difficulty_launches_with_timestamp_free_name(tmp_path, monkeypatch):
    """The difficulty sweep launches a deterministic, timestamp-free experiment `programsmith-<slug>-difficulty`
    at generation 0 (no tune/revise yet), with --run-analysis, and the configured harness translated
    to its sweep-registered name."""
    from programsmith.orchestrator import _h_difficulty
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass"],  # at DIFFICULTY_SWEEP
            run_config=_rc())
    _complete_bundle(tmp_path)
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    fake = _FakeBackend(handle=None)   # echo the experiment name back as the handle

    res = _h_difficulty(st, man, tmp_path, {"sweep_live": True, "sweep_backend": fake})
    assert res.blocked and "launched difficulty" in res.reason
    _task, agents, experiment, flags = fake.launches[0]
    assert experiment == "programsmith-demo-difficulty"  # no timestamp suffix
    assert "--run-analysis" in flags  # difficulty trials are TrialClassifier-labelled
    # the agent list carries the TRANSLATED harness name (mini-swe → mini-swe-agent) + baselines
    assert [a.name for a in agents] == ["oracle", "nop", "mini-swe-agent"]
    assert man.sweeps["difficulty"]["experiment"] == "programsmith-demo-difficulty"


def test_h_full_sweep_launches_with_run_analysis(tmp_path):
    """The FULL sweep ALWAYS launches with --run-analysis so every frontier trial is GOOD/BAD-classified
    (good-failure / good-success). Regression lock — this label must never silently drop off."""
    from programsmith.orchestrator import _h_full_sweep
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass", "done", "proceed", "clean"])  # at FULL_SWEEP
    _complete_bundle(tmp_path)
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    fake = _FakeBackend(handle="abcdef12")

    res = _h_full_sweep(st, man, tmp_path, {"sweep_live": True, "sweep_backend": fake})
    assert res.blocked  # launched, now polling
    assert "--run-analysis" in fake.launches[0][3]
    assert man.sweeps["full"]["experiment"] == "abcdef12"


def test_difficulty_attaches_trial_analysis_before_calibrate(tmp_path):
    """After the difficulty sweep records its band, the stage attaches the CONFIGURED smoke
    harness's TrialClassifier labels (--run-analysis) — blocking while the classifier is PENDING,
    then recording the good/bad breakdown. Band stays reward-based."""
    from programsmith.orchestrator import _h_difficulty
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass"],  # at DIFFICULTY_SWEEP
            run_config=_rc())
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    man.sweeps = {"difficulty": {"status": "done", "experiment": "exp9", "pass_at_1": 0.6667}}
    man.save(tmp_path)

    # classifier still running → block (bounded), no analysis attached yet
    pending = {"analyses": [], "pending": 1, "failed": 0, "total": 1}
    man = Manifest.load(tmp_path)
    r1 = _h_difficulty(st, man, tmp_path, {"sweep_backend": _FakeBackend(analyses=pending)})
    assert r1.blocked and "TrialClassifier running" in r1.reason

    # classifier done → attach breakdown, advance (verdict done)
    ready = {"analyses": [
        {"trial_id": "r1", "label": "GOOD_SUCCESS"},
        {"trial_id": "r2", "label": "GOOD_SUCCESS"},
        {"trial_id": "r3", "label": "GOOD_FAILURE"},
    ], "pending": 0, "failed": 0, "total": 3}
    r2 = _h_difficulty(st, man, tmp_path, {"sweep_backend": _FakeBackend(analyses=ready)})
    assert r2.verdict == "done"
    bd = man.sweeps["difficulty"]["analysis"]["breakdown"]
    assert bd == {"GOOD_SUCCESS": 2, "GOOD_FAILURE": 1}
    assert "GOOD_SUCCESS" in man.sweeps["difficulty"]["analysis_summary"]


def test_difficulty_analysis_flags_gamed_success_as_reward_hack(tmp_path):
    """A BAD_SUCCESS on the difficulty sweep (Opus passed by gaming the verifier) is surfaced as a
    reward-hack finding for the harden — close the hole structurally, not just raise difficulty."""
    from programsmith.orchestrator import _saturation_findings
    _run_at(tmp_path, ["pass", "selected", "pass"])
    man = Manifest.load(tmp_path)
    (tmp_path / "task" / "demo").mkdir(parents=True)
    man.sweeps = {"difficulty": {"pass_at_1": 1.0,
                                 "analysis": {"breakdown": {"BAD_SUCCESS": 2, "GOOD_SUCCESS": 1}}}}
    f = _saturation_findings(man, tmp_path / "task" / "demo")
    assert any(x["kind"] == "reward-hack" and "GAMED" in x["detail"] for x in f)


def test_h_qa_probe_prepends_auditor_overlay_and_names_deterministically(tmp_path):
    """QA/PROBE prepends the Task Construction Auditor to instruction.md (original kept below) and
    launches a deterministic, timestamp-free experiment + per-generation task dir."""
    from programsmith.orchestrator import _h_qa_probe
    from programsmith.probes import TASK_CONSTRUCTION_AUDITOR
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass", "done", "proceed"])
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    assert st.current_stage.value == "QA_PROBE"
    _complete_bundle(tmp_path)
    fake = _FakeBackend(handle="c5dfead9")

    res = _h_qa_probe(st, man, tmp_path, {"sweep_live": True, "sweep_backend": fake})
    assert res.blocked and "launched QA/PROBE" in res.reason
    overlay = (tmp_path / "probe-task" / "demo-audit" / "instruction.md").read_text()
    assert overlay.startswith(TASK_CONSTRUCTION_AUDITOR)        # auditor role FIRST (prepended)
    assert overlay.rstrip().endswith("ORIGINAL TASK INSTRUCTION")  # original preserved below
    assert fake.launches[0][2] == "programsmith-demo-probe"              # deterministic, no timestamp
    assert man.sweeps["qa_probe"]["experiment"] == "c5dfead9"


def test_agentic_concurrency_cap_defers_when_fleet_busy(tmp_path, monkeypatch):
    """The shared OAuth subscription throttles under parallel agents, so a new cell agent must NOT
    launch while the fleet is already at its agent budget — it waits for a slot. The live cap is
    config-driven (LhConfig.agentic_concurrency, default 2 per ADR-0038 scale posture): with cap
    sibling agents already running, this run defers instead of launching."""
    import json as _json

    from programsmith import jobs
    from programsmith.cells.agentic import ValidationState
    from programsmith.orchestrator import _agentic_concurrency, _running_agentic_count, _h_create
    # EMPTY ladder → the global cap is the throttle guard (ADR-0034 Layer 2 fallback). Isolate config so
    # the operator's real cell-auth.json can't leak a ladder into this test.
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    for _v in ("PROGRAMSMITH_CELL_AUTH_FILE", "PROGRAMSMITH_CELL_OAUTH_TOKENS", "PROGRAMSMITH_CELL_API_KEYS"):
        monkeypatch.delenv(_v, raising=False)
    _fake_skeleton(monkeypatch)
    runs = tmp_path / "runs"
    cap = _agentic_concurrency()
    assert cap >= 2                                # ADR-0038 scale posture: default bumped 1 → 2
    for i in range(cap):                           # fill EVERY slot with a busy sibling
        busy = runs / f"busy{i}"
        busy.mkdir(parents=True)
        jobs.set_job(busy, "synthesize-h1-r0-e0", "running")
    assert _running_agentic_count(runs / "mine") >= cap

    mine = runs / "mine"
    _run_at(mine, ["pass", "selected", "pass"])  # at CREATE
    st, man = RunState.load(mine), Manifest.load(mine)
    ctx = {"agentic": True, "agentic_background": True, "agent_session": lambda p, td: "ok",
           "validator": lambda _d: ValidationState(True, True)}
    res = _h_create(st, man, mine, ctx)
    assert res.blocked and "waiting for an agent slot" in res.reason
    assert jobs.get_jobs(mine).get("create-fill") is None     # did NOT launch (no job created)

    # free one slot → the next pass launches (the cap guard releases)
    p = runs / "busy0" / "jobs.json"
    d = _json.loads(p.read_text()); d["synthesize-h1-r0-e0"]["status"] = "done"; p.write_text(_json.dumps(d))
    res2 = _h_create(st, man, mine, ctx)
    assert res2.blocked and "background" in res2.reason       # launched now (a slot was free)


def test_agentic_job_bounded_auto_retry_then_blocks(tmp_path, monkeypatch):
    """Self-heal: an errored agentic bg job auto-retries (bounded by _AGENTIC_MAX_ATTEMPTS) before
    hard-blocking — so a transient timeout doesn't leave a run stuck until manually cleared."""
    import time

    from programsmith import jobs
    from programsmith.cells.agentic import ValidationState
    from programsmith.orchestrator import _AGENTIC_MAX_ATTEMPTS, _h_create
    tmp_path = tmp_path / "runs" / "r"  # nested: isolate the sibling-scan (see bg-non-blocking test)
    tmp_path.parent.mkdir(parents=True)
    _run_at(tmp_path, ["pass", "selected", "pass"])  # at CREATE
    _fake_skeleton(monkeypatch)
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    # validator always RED → agentic_fill never reaches green → produce raises → job 'error'
    ctx = {"agentic": True, "agentic_background": True, "agent_session": lambda p, td: "x",
           "validator": lambda _d: ValidationState(False, True), "max_iters": 1}

    import json
    reasons = []
    for _ in range(_AGENTIC_MAX_ATTEMPTS + 3):
        reasons.append(_h_create(st, man, tmp_path, ctx).reason)
        for _ in range(80):  # wait for the (re)launched job to settle back to 'error'
            if jobs.get_jobs(tmp_path).get("create-fill", {}).get("status") == "error":
                break
            time.sleep(0.05)
        # elapse the retry backoff so the next pass actually relaunches (simulate time passing)
        p = tmp_path / "jobs.json"
        if p.exists():
            d = json.loads(p.read_text())
            if d.get("create-fill", {}).get("status") == "error":
                d["create-fill"]["errored_at"] = 0
                p.write_text(json.dumps(d))
    joined = " || ".join(reasons)
    assert "auto-retry" in joined and f"{_AGENTIC_MAX_ATTEMPTS}/{_AGENTIC_MAX_ATTEMPTS}" in joined
    assert any(f"after {_AGENTIC_MAX_ATTEMPTS} attempt" in r for r in reasons)  # hard-blocked, not looping


def test_agentic_done_but_incomplete_retries_bounded_after_grace(tmp_path, monkeypatch):
    """Regression (the pb10 toybox wedge): a job that finished CLEANLY but left the artifact
    incomplete (session cap hit mid-build; agent misjudged its output) used to hard-block
    immediately — 'STOP-and-flag; clear the job to retry' — which on an unattended fleet means
    the run wedges forever with nobody there to clear it. It must self-heal like an errored job:
    a GRACE period first (an orphaned background build may still land — complete() is re-polled
    every pass), then bounded relaunches, and only after _AGENTIC_MAX_ATTEMPTS a hard block."""
    import json

    from programsmith import jobs
    from programsmith import orchestrator as orch
    from programsmith.orchestrator import _AGENTIC_MAX_ATTEMPTS, _agentic_bg_step

    monkeypatch.setattr(orch, "_running_agentic_count", lambda _d: 0)  # a slot is always free
    launches: list[int] = []

    def fake_bg(run_dir, name, fn, stale_sec=None, attempts=None):
        # mimic the real lifecycle synchronously: running (records attempts) → done, incomplete
        launches.append(attempts)
        jobs.set_job(run_dir, name, "running", stale_sec=stale_sec, attempts=attempts)
        jobs.set_job(run_dir, name, "done", "bundle incomplete; missing: ['oracle_bin']")

    monkeypatch.setattr(jobs, "run_in_background", fake_bg)
    tmp_path.mkdir(exist_ok=True)
    step = lambda: _agentic_bg_step(  # noqa: E731 — artifact never completes; apply must not run
        "oracle-generate", tmp_path, produce=lambda: "x",
        complete=lambda: False, apply_result=lambda: (_ for _ in ()).throw(AssertionError))

    reasons = [step().reason]                       # pass 1: no job → first launch (attempt 0)
    for _ in range(_AGENTIC_MAX_ATTEMPTS + 2):
        reasons.append(step().reason)               # fresh 'done' → grace period, NOT a relaunch
        p = tmp_path / "jobs.json"
        d = json.loads(p.read_text())
        if d.get("oracle-generate", {}).get("status") == "done":
            d["oracle-generate"]["finished_at"] = 0  # elapse the grace window
            p.write_text(json.dumps(d))
        reasons.append(step().reason)               # grace elapsed → bounded retry (or hard block)
    joined = " || ".join(reasons)
    assert "waiting" in joined                                        # grace period surfaced
    assert launches == [0, 1, 2, 3][: _AGENTIC_MAX_ATTEMPTS + 1]      # bounded relaunches
    assert any(f"after {_AGENTIC_MAX_ATTEMPTS} attempt" in r for r in reasons)  # then hard block


def test_saturation_harden_is_unblinded_with_evidence(tmp_path):
    """A `saturation` harden must not patch blind: _saturation_findings derives the band severity
    and the ProgramBench levers (DESIGN §6.6 — extend uncovered case families FROM THE ORACLE,
    adversarial error paths, deepen existing families, tighten timeout only on slack) plus per-case
    fail aggregates parsed from the pulled verifier artifacts (diff_<case>.txt)."""
    from programsmith.orchestrator import _saturation_findings
    pull = tmp_path / ".sweeps" / "exp1"
    for t in ("t0", "t1"):
        d = pull / "trials" / t
        d.mkdir(parents=True)
        (d / "result.json").write_text("{}")
        (d / "diff_case-zeta.txt").write_text("x")     # fails in BOTH trials
    (pull / "trials" / "t0" / "diff_case-eta.txt").write_text("x")
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass", "done"],
            sweeps={"difficulty": {"status": "done", "pass_at_1": 1.0, "pull_dir": str(pull)}},
            run_config=_rc(full_max=0.70))
    man = Manifest.load(tmp_path)
    task = tmp_path / "task" / "demo"
    task.mkdir(parents=True)

    f = _saturation_findings(man, task)
    blob = " ".join(x["detail"] for x in f)
    assert all(x["kind"] == "saturation" for x in f)             # no BAD_SUCCESS → no reward-hack kind
    assert "pass@1=1.00" in blob and "severe" in blob            # band severity vs the 0.70 ceiling
    assert "0.70 band ceiling" in blob
    assert "case-zeta×2" in blob                                 # per-case aggregates surfaced
    assert "LEVER add-cases" in blob and "never hand-written" in blob
    assert "adversarial error-path" in blob and "deepen the held-out surface" in blob.lower()
    assert "SOLVABLE_AS_WRITTEN" in blob


def test_ease_findings_isolate_universal_blockers(tmp_path):
    """An `ease` move gets universal-blocker evidence: a case whose diff artifact appears in EVERY
    trial is the surgical removal target (never gut whole families)."""
    from programsmith.orchestrator import _ease_findings
    pull = tmp_path / ".sweeps" / "exp2"
    for t in ("t0", "t1", "t2"):
        d = pull / "trials" / t
        d.mkdir(parents=True)
        (d / "result.json").write_text("{}")
        (d / "diff_err-exact-text.txt").write_text("x")   # every trial fails this the same way
    (pull / "trials" / "t0" / "diff_case-hard.txt").write_text("x")
    _run_at(tmp_path, ["pass", "selected", "pass"],
            sweeps={"full": {"status": "done", "pass_at_1": 0.0, "pull_dir": str(pull),
                             "goodfail": {"verdict": "ease", "reason": "2 BAD_FAILURE trial(s)"}}})
    man = Manifest.load(tmp_path)
    f = _ease_findings(man, tmp_path / "task" / "demo")
    blob = " ".join(x["detail"] for x in f)
    assert "err-exact-text" in blob and "universal-blocker" in blob.lower()
    assert "BAD_FAILURE" in blob                              # the goodfail evidence is carried
    assert "NEVER gut" in blob


def test_saturation_findings_borderline_vs_severe(tmp_path):
    """Severity scales with the band so the patch aggressiveness matches: 0.667 → borderline (cjson),
    1.0 → severe (hnswlib). The full-sweep band takes precedence over the difficulty band."""
    from programsmith.orchestrator import _saturation_findings
    _run_at(tmp_path, ["pass", "selected", "pass"])
    man = Manifest.load(tmp_path)
    (tmp_path / "task" / "demo").mkdir(parents=True)
    man.sweeps = {"difficulty": {"pass_at_1": 0.6666666666666666}}
    assert "borderline" in _saturation_findings(man, tmp_path / "task" / "demo")[0]["detail"]
    man.sweeps = {"difficulty": {"pass_at_1": 0.5}, "full": {"claude_code": 1.0}}  # full wins
    assert "severe" in _saturation_findings(man, tmp_path / "task" / "demo")[0]["detail"]


def test_h_static_stages_into_writable_checkout(tmp_path):
    """STATIC_CI stages THIS run's task into a writable copy and runs the checks against it with an
    ABSOLUTE root (so scripts resolve) — never mutating the read-only ground-truth checkout."""
    from programsmith.gates.static_ci import CHECK_ORDER
    from programsmith.orchestrator import _h_static
    run_dir = tmp_path / "run"
    _run_at(run_dir, ["pass", "selected", "pass", "pass", "pass"])  # at STATIC_CI
    st, man = RunState.load(run_dir), Manifest.load(run_dir)
    task = run_dir / "task" / "demo"                 # a minimal complete task (the stub checks
    (task / "tests").mkdir(parents=True)             # only assert on task.toml at $1)
    (task / "task.toml").write_text("[task]\n")
    (task / "tests" / "test.sh").write_text("#!/bin/bash\n")
    repo = tmp_path / "harbor"
    (repo / "ci_checks").mkdir(parents=True)
    for c in CHECK_ORDER:  # each check passes iff the task got staged with its task.toml at $1
        (repo / "ci_checks" / f"{c}.sh").write_text('#!/bin/bash\ntest -f "$1/task.toml"\n')
    res = _h_static(st, man, run_dir, {"ci_repo_root": str(repo)})
    assert res.verdict == "pass" and not res.blocked
    assert not (repo / "tasks").exists()  # ground-truth checkout NOT mutated


def test_sweep_upload_bundle_strips_heldout_and_artifacts(tmp_path):
    """The staged sweep bundle must drop held-out artifacts (oracle/ reference port, plaintext
    goldens/) and build outputs (target/) — both an anti-hack requirement and a size fix
    (EntityTooLarge). The encrypted private.enc + environment/ still ship."""
    from programsmith.orchestrator import _sweep_upload_bundle
    task = tmp_path / "task" / "demo"
    (task / "oracle" / "target").mkdir(parents=True)
    (task / "oracle" / "target" / "big.rlib").write_text("x" * 1000)
    (task / "oracle" / "src").mkdir(parents=True); (task / "oracle" / "src" / "lib.rs").write_text("// answer")
    (task / "goldens").mkdir(); (task / "goldens" / "public.json").write_text("[]")
    (task / "environment").mkdir(); (task / "environment" / "Dockerfile").write_text("FROM ubuntu:24.04")
    (task / "task.toml").write_text("name='demo'")
    (task / "private.enc").write_text("encrypted")
    (task / ".create-fill-ok").write_text("marker")
    out = _sweep_upload_bundle(task, tmp_path / "run", "demo")
    names = sorted(p.name for p in out.iterdir())
    assert "oracle" not in names and "goldens" not in names      # held-out stripped (anti-hack)
    assert ".create-fill-ok" not in names                        # pipeline marker stripped
    assert "environment" in names and "private.enc" in names and "task.toml" in names  # shipped kept


# ---- harden-review auditor wired into CALIBRATE / FULL_SWEEP -------------------------

def test_h_calibrate_saturation_review_drop_maps_to_proceed(tmp_path):
    """ADR-0040: at CALIBRATE a harden-review "drop" (too easy to harden / not converging) is NOT a
    drop anymore — the smoke model is never the authority. It maps to PROCEED with
    smoke_saturated=true recorded, so the frontier measures it and Opus decides shelf-vs-keep."""
    from programsmith.orchestrator import _h_calibrate
    st = RunState.start("r", "task:x", "demo")
    st.current_stage = Stage.CALIBRATE
    st.harden = 2
    st.save(tmp_path)
    man = Manifest(run_id="r", task_identity="task:x", slug="demo")
    man.sweeps = {"difficulty": {"pass_at_1": 1.0, "analysis": {"breakdown": {"GOOD_SUCCESS": 3}}}}
    man.harden_history = [{"pass_at_1": 1.0}, {"pass_at_1": 1.0}]
    man.save(tmp_path)
    res = _h_calibrate(st, man, tmp_path, {})
    assert res.verdict == "proceed" and "measure" in res.reason
    assert man.sweeps["difficulty"]["smoke_saturated"] is True


def test_h_calibrate_zero_pass_goodfail_routing(tmp_path):
    """Smoke zero-pass runs the (cheap, label-only) good-failure gate: all GOOD_FAILURE → proceed
    (keep and wait to harden with opus if needed); any BAD_FAILURE → ease. A BAD_SUCCESS anywhere →
    harden as a reward-hack BEFORE the frontier spend, bypassing the harden-review auditor."""
    from programsmith.orchestrator import _h_calibrate
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass", "done"])
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)

    def diff(pa, labels):
        return {"difficulty": {"pass_at_1": pa, "analysis": {
            "labels": [{"trial_id": f"t{i}", "label": l} for i, l in enumerate(labels)],
            "breakdown": {l: labels.count(l) for l in set(labels)}}}}

    man.sweeps = diff(0.0, ["GOOD_FAILURE", "GOOD_FAILURE", "GOOD_FAILURE"])
    assert _h_calibrate(st, man, tmp_path, {}).verdict == "proceed"
    man.sweeps = diff(0.0, ["GOOD_FAILURE", "BAD_FAILURE"])
    assert _h_calibrate(st, man, tmp_path, {}).verdict == "ease"
    man.sweeps = diff(0.33, ["BAD_SUCCESS", "GOOD_FAILURE"])
    res = _h_calibrate(st, man, tmp_path, {})
    assert res.verdict == "harden" and "BAD_SUCCESS" in res.reason
    assert not man.harden_history  # reward-hack harden bypasses the saturation review/history


def test_h_calibrate_first_harden_is_viable_and_records_history(tmp_path):
    from programsmith.orchestrator import _h_calibrate
    st = RunState.start("r", "task:x", "demo")
    st.current_stage = Stage.CALIBRATE
    st.save(tmp_path)
    man = Manifest(run_id="r", task_identity="task:x", slug="demo")
    man.sweeps = {"difficulty": {"pass_at_1": 1.0}}
    man.save(tmp_path)
    res = _h_calibrate(st, man, tmp_path, {})
    assert res.verdict == "harden"
    assert len(man.harden_history) == 1 and man.harden_history[0]["pass_at_1"] == 1.0


def test_h_full_sweep_shelves_when_too_easy_to_harden(tmp_path):
    """ADR-0040: "too easy to harden" at the FRONTIER is a SHELF, not a trash can — the
    harden-review drop-recommendation maps to verdict `shelve` (FSM routes it to EASY_SHELF)."""
    from programsmith.orchestrator import _h_full_sweep
    st = RunState.start("r", "task:x", "demo")
    st.current_stage = Stage.FULL_SWEEP
    st.harden = 2
    st.save(tmp_path)
    man = Manifest(run_id="r", task_identity="task:x", slug="demo")
    man.sweeps = {"full": {"status": "done", "claude_code": 1.0, "codex": 0.2, "aggregate": 1.0}}
    man.harden_history = [{"pass_at_1": 1.0}, {"pass_at_1": 1.0}]
    man.save(tmp_path)
    res = _h_full_sweep(st, man, tmp_path, {})
    assert res.verdict == "shelve" and "easy" in res.reason.lower()


def test_legacy_pr_and_qa_on_gpt_drain(tmp_path):
    """ADR-0039 legacy drains: a pre-pivot run parked at PR advances straight to DONE (no PR is
    ever opened — nothing here touches pr/gh); one parked at QA_ON_GPT drains forward through
    QA_GATE. peek describes both as auto-completing legacy stages."""
    from programsmith.fsm import Stage
    from programsmith.manifest import Manifest
    from programsmith.orchestrator import REGISTRY, drive, peek
    from programsmith.state import RunState, StageEvent
    assert Stage.PR in REGISTRY and Stage.QA_ON_GPT in REGISTRY  # every non-terminal stage handled
    RunState(run_id="r", task_identity="t", slug="awk", current_stage=Stage.PR,
             history=[StageEvent(stage=Stage.QA_GATE, verdict="accept", next=Stage.PR,
                                 reason="accepted")]).save(tmp_path)
    Manifest(run_id="r", task_identity="t", slug="awk").save(tmp_path)
    w = peek(tmp_path)
    assert w["kind"] == "runnable" and "legacy" in w["reason"]
    res = drive(tmp_path)
    assert res.final_stage == "DONE" and res.final_status == "done"
    assert all("no handler" not in s["reason"] for s in res.steps)

    qd = tmp_path / "qagpt"
    RunState(run_id="r2", task_identity="t2", slug="awk", current_stage=Stage.QA_ON_GPT,
             history=[]).save(qd)
    Manifest(run_id="r2", task_identity="t2", slug="awk").save(qd)
    # QA_ON_GPT drains to QA_GATE; the auto gate then decides on the (empty) sweeps → reject →
    # DROPPED. The point locked here: the legacy stage itself never blocks or reads anything.
    res2 = drive(qd, ctx={"outbox_dir": str(tmp_path / "outbox")})
    assert res2.steps[0]["stage"] == "QA_ON_GPT" and res2.steps[0]["verdict"] == "done"
    assert "legacy" in res2.steps[0]["reason"]


def test_permanent_launch_error_hard_blocks(tmp_path):
    """A PERMANENT launch failure (oversize/quota/auth — e.g. an EntityTooLarge upload) hard-blocks
    IMMEDIATELY instead of burning the 5 'transient' retries and mislabeling the block. (flac's 889MB
    bundle failed the upload 5× and read as 'transient launch failures'.)"""
    from programsmith.orchestrator import (
        _LAUNCH_MAX_ATTEMPTS, _h_difficulty, _is_permanent_launch_error)
    # classification: permanent vs transient
    assert _is_permanent_launch_error("sweep launch failed: EntityTooLarge (413)")
    assert _is_permanent_launch_error("Forbidden: invalid api key")
    assert not _is_permanent_launch_error("connection reset by peer")
    assert not _is_permanent_launch_error("502 Bad Gateway")

    class _Boom:
        name = "local"; needs_upload = False; artifact_subdir = ".sweeps"
        def launch(self, *a, **k):
            raise RuntimeError("sweep launch failed: EntityTooLarge 413 payload too large")
        def status(self, h):
            return {}
        def results(self, h, o):
            return []
        def analyses(self, h, *, agents):
            return {}
        def pull_artifacts(self, h, o):
            return o

    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass"])  # at DIFFICULTY_SWEEP
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    task = tmp_path / "t"; task.mkdir(); (task / "task.toml").write_text("x")
    res = _h_difficulty(st, man, tmp_path,
                        {"sweep_live": True, "sweep_backend": _Boom(), "task_path": str(task)})
    assert res.blocked and "PERMANENT" in res.reason
    ent = man.sweeps["difficulty"]
    assert ent["status"] == "errored" and ent["attempts"] == _LAUNCH_MAX_ATTEMPTS
    assert "(permanent)" in ent["summary"]


def test_qa_probe_launch_failure_blocks_not_raises(tmp_path):
    """QA/PROBE must NOT crash the whole drive pass on a bundle/launch failure — it blocks
    cleanly like _sweep_step (which was already guarded). Regression: riscv-emu's probe upload
    raised 'Failed to upload task directly to storage' and the driver recorded 'drive raised'."""
    from programsmith.orchestrator import _h_qa_probe
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass", "done", "proceed"])  # QA_PROBE
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    task = tmp_path / "t"; task.mkdir()
    (task / "task.toml").write_text("x")
    (task / "instruction.md").write_text("do the task")

    class _Boom:
        name = "local"; needs_upload = False; artifact_subdir = ".sweeps"
        def launch(self, *a, **k):
            raise RuntimeError("sweep launch failed: Failed to upload task directly to storage")
        def status(self, h):
            return {}
        def results(self, h, o):
            return []
        def analyses(self, h, *, agents):
            return {}
        def pull_artifacts(self, h, o):
            return o

    res = _h_qa_probe(st, man, tmp_path,
                      {"sweep_live": True, "sweep_backend": _Boom(), "task_path": str(task)})
    assert res.blocked and "launch failed" in res.reason        # clean block, not a raised exception
    assert man.sweeps["qa_probe"]["status"] == "errored"


def test_difficulty_sweep_self_heals_transient_launch_outage(tmp_path):
    """A TRANSIENT launch outage that exhausted the fast retry burst must SELF-HEAL after
    the cooldown — relaunch a fresh burst rather than wedge forever needing a manual reset (mbedtls/swipl
    hit 'Failed to initialize direct task upload: Internal Server Error'). A RECENT error still backs off;
    a PERMANENT error (oversized/quota/auth) stays hard-blocked."""
    from datetime import datetime, timedelta, timezone

    from programsmith.orchestrator import _h_difficulty
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass"])  # at DIFFICULTY_SWEEP
    task = tmp_path / "t"; task.mkdir(); (task / "task.toml").write_text("x")

    class _OK:
        name = "local"; needs_upload = False; artifact_subdir = ".sweeps"
        def launch(self, *a, **k):
            return "exp-fresh"
        def status(self, h):
            return {"complete": False, "tasks_running": 1, "trials_completed": 0, "trials_total": 3}
        def results(self, h, o):
            return []
        def analyses(self, h, *, agents):
            return {"analyses": [], "pending": 0, "failed": 0, "total": 0}
        def pull_artifacts(self, h, o):
            return o

    def _seed(min_ago, summary):
        man = Manifest.load(tmp_path)
        ts = (datetime.now(timezone.utc) - timedelta(minutes=min_ago)).isoformat()
        man.sweeps = {"difficulty": {"status": "errored", "experiment": None, "attempts": 5,
                                     "errored_at": ts, "summary": summary}}
        man.save(tmp_path)
        return RunState.load(tmp_path), Manifest.load(tmp_path)

    ctx = {"sweep_live": True, "sweep_backend": _OK(), "task_path": str(task)}
    # 1. transient, cooled down (25 min) → SELF-HEAL: relaunch a fresh sweep
    st, man = _seed(25, "sweep launch failed: Failed to initialize direct task upload: Internal Server Error")
    _h_difficulty(st, man, tmp_path, ctx)
    assert man.sweeps["difficulty"]["status"] == "running"
    assert man.sweeps["difficulty"]["experiment"] == "exp-fresh"
    # 2. transient, still within cooldown (2 min) → stays blocked, does NOT relaunch
    st, man = _seed(2, "sweep launch failed: Internal Server Error")
    res = _h_difficulty(st, man, tmp_path, ctx)
    assert res.blocked and man.sweeps["difficulty"]["status"] == "errored" and "auto-retry" in res.reason
    # 3. PERMANENT (oversized), cooled down → stays hard-blocked (no self-heal)
    st, man = _seed(25, "sweep launch failed (permanent): 413 payload too large")
    res = _h_difficulty(st, man, tmp_path, ctx)
    assert res.blocked and man.sweeps["difficulty"]["status"] == "errored" and "investigate" in res.reason


# ---- auto human-gates (ADR-0039) + outbox export ---------------------------------------

def test_active_human_stages_follows_config_and_ctx(tmp_path, monkeypatch):
    from programsmith.config import LhConfig
    from programsmith.orchestrator import active_human_stages
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    assert active_human_stages() == frozenset()               # both default AUTO → zero-touch
    cfg = LhConfig(); cfg.qa_gate_mode = "human"; cfg.save()
    assert active_human_stages() == frozenset({Stage.QA_GATE})
    # ctx overrides win over config (per-drive/test control)
    assert active_human_stages(ctx={"qa_gate_mode": "auto", "task_matrix_mode": "human"}) == \
        frozenset({Stage.TASK_MATRIX})


def _fake_matrix(cands):
    """Duck-typed propose() output — schema-independent on purpose: the auto handler must only
    touch .candidates / .recommendation / model_dump() / model_dump_json() (the candidate schema
    is being rewritten concurrently)."""
    class _C:
        def __init__(self, d):
            self._d = d
            self.recommendation = d.get("recommendation")
        def model_dump(self):
            return dict(self._d)
    class _Out:
        candidates = [_C(d) for d in cands]
        def model_dump_json(self, **kw):
            return json.dumps({"candidates": cands}, indent=2)
    return _Out()


def test_h_task_matrix_auto_picks_and_advances(tmp_path, monkeypatch):
    """Auto TASK_MATRIX: runs propose() (light model), persists task_matrix.json, picks the FIRST
    'recommended' (else first 'viable'), fills dimensions field-agnostically, and advances
    'selected' — the whole human review #1, deterministic."""
    from programsmith import orchestrator as orch
    from programsmith.cells import task_matrix as tm
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    _run_at(tmp_path, ["pass"])  # at TASK_MATRIX
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    _make_screenable_source(tmp_path, man)

    # no LLM cell available → blocks honestly (never shells out to a real model)
    r0 = orch._h_task_matrix(st, man, tmp_path, {})
    assert r0.blocked and "llm_runner" in r0.reason

    # a schema-valid ProgramBench candidate: the pick must flow through the SHARED
    # cells.task_matrix.apply_selection path so auto-pick and human-pick hash identically.
    full = {"tool_name": "widget", "binary_name": "widget", "upstream_language": "c",
            "flag_surface": "core flags", "case_families": ["a", "b", "c", "d", "e"],
            "est_kloc": 12, "stdin_friendly": True, "needs_files_dir": False,
            "deterministic_output": True, "expected_difficulty": "hard", "expert_hours": 20,
            "recommendation": "recommended", "rationale": "yes", "basis_ref": "farm/gojq"}
    cands = [{**full, "recommendation": "marginal", "rationale": "meh"}, full]
    monkeypatch.setattr(tm, "propose", lambda m, runner=None, model=None: _fake_matrix(cands))
    res = orch._h_task_matrix(st, man, tmp_path, {"agentic": True})
    assert res.verdict == "selected" and "[1]" in res.reason   # first RECOMMENDED, not first row
    assert (tmp_path / "task_matrix.json").exists()
    assert man.dimensions is not None and man.dimensions.tool_name == "widget"
    # identity recomputed BYTE-IDENTICALLY to the CLI/UI pick path (ADR-0038 dedup hash)
    from programsmith.manifest import programbench_task_identity
    want = programbench_task_identity("o/n", "abc123", "widget", "core flags")
    assert man.task_identity == want and st.task_identity == want

    # a legacy / schema-invalid candidate still picks but KEEPS the provisional identity, loudly
    st2_dir = tmp_path.parent / "legacy"; st2_dir.mkdir()
    _run_at(st2_dir, ["pass"])
    st2, man2 = RunState.load(st2_dir), Manifest.load(st2_dir)
    _make_screenable_source(st2_dir, man2)
    old = [{"recommendation": "recommended", "rationale": "port-era", "target_language": "Rust",
            "scope_unit": "whole-library"}]
    monkeypatch.setattr(tm, "propose", lambda m, runner=None, model=None: _fake_matrix(old))
    res2 = orch._h_task_matrix(st2, man2, st2_dir, {"agentic": True})
    assert res2.verdict == "selected" and "NOT recomputed" in res2.reason
    assert man2.task_identity == "task:x"


def test_h_task_matrix_auto_picks_marginal_and_drops_only_when_empty(tmp_path, monkeypatch):
    """Farm posture (ADR-0039): a lone 'marginal' candidate is now SELECTED (TASK_MATRIX is a coarse
    prefilter; the downstream deterministic gates enforce real quality). The run drops here ONLY
    when the cell proposed zero candidates."""
    from programsmith import orchestrator as orch
    from programsmith.cells import task_matrix as tm
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    marginal = {"tool_name": "widget", "binary_name": "widget", "upstream_language": "c",
                "flag_surface": "core flags", "case_families": ["a", "b", "c", "d", "e"],
                "est_kloc": 12, "stdin_friendly": True, "needs_files_dir": False,
                "deterministic_output": True, "expected_difficulty": "hard", "expert_hours": 20,
                "recommendation": "marginal", "rationale": "weak but plausible", "basis_ref": "farm/x"}

    _run_at(tmp_path, ["pass"])
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    _make_screenable_source(tmp_path, man)
    monkeypatch.setattr(tm, "propose", lambda m, runner=None, model=None: _fake_matrix([marginal]))
    res = orch._h_task_matrix(st, man, tmp_path, {"agentic": True})
    assert res.verdict == "selected"                       # marginal is accepted, not dropped

    # zero candidates → the only case that still drops
    empty_dir = tmp_path.parent / "empty"; empty_dir.mkdir()
    _run_at(empty_dir, ["pass"])
    st2, man2 = RunState.load(empty_dir), Manifest.load(empty_dir)
    _make_screenable_source(empty_dir, man2)
    monkeypatch.setattr(tm, "propose", lambda m, runner=None, model=None: _fake_matrix([]))
    res2 = orch._h_task_matrix(st2, man2, empty_dir, {"agentic": True})
    assert res2.verdict == "none_selected"


def test_h_task_matrix_hard_source_incompatibility_rejects_before_llm(tmp_path, monkeypatch):
    """A hard unsupported-toolchain mismatch spends zero model calls; size is only a warning."""
    from programsmith import orchestrator as orch
    from programsmith.cells import task_matrix as tm

    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    _run_at(tmp_path, ["pass"])
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    _make_screenable_source(tmp_path, man, loc=100)
    man.source.primary_language = "Python"
    man.pipeline_mode = "draft"

    monkeypatch.setattr(tm, "propose", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("source screen must run before the paid model")))
    result = orch._h_task_matrix(st, man, tmp_path, {"agentic": True})
    assert result.verdict == "none_selected" and "source screened out (draft)" in result.reason
    assert man.source_screen["eligible"] is False
    assert "unsupported upstream language Python" in man.source_screen["reason"]
    assert (tmp_path / "source_screen.json").exists()


def test_h_task_matrix_retries_old_empty_full_matrix_under_draft_profile(tmp_path, monkeypatch):
    """Old full-rubric empty answers are not valid evidence against the easier draft profile."""
    from programsmith import orchestrator as orch
    from programsmith.cells import task_matrix as tm

    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    _run_at(tmp_path, ["pass"])
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    _make_screenable_source(tmp_path, man, loc=1_200)
    man.pipeline_mode = "draft"
    (tmp_path / "task_matrix.json").write_text(json.dumps({
        "source_ref": "o/n@abc123", "profile": "full", "candidates": [],
        "no_candidate_reason": "too small for the calibrated difficulty band",
    }))
    candidate = {"tool_name": "widget", "binary_name": "widget", "upstream_language": "c",
                 "flag_surface": "stdin transform", "case_families": ["a", "b", "c", "d", "e"],
                 "est_kloc": 1, "stdin_friendly": True, "needs_files_dir": False,
                 "deterministic_output": True, "expected_difficulty": "moderate", "expert_hours": 3,
                 "recommendation": "recommended", "rationale": "deterministic", "basis_ref": "farm/x"}
    monkeypatch.setattr(tm, "propose", lambda m, runner=None, model=None: _fake_matrix([candidate]))
    result = orch._h_task_matrix(st, man, tmp_path, {"agentic": True})
    assert result.verdict == "selected"
    assert (tmp_path / "task_matrix.full.json").exists()


def test_h_task_matrix_reuses_existing_matrix_file(tmp_path, monkeypatch):
    """Idempotency/crash-resume: an existing task_matrix.json is picked from directly — propose()
    is NOT re-run (no double LLM spend)."""
    from programsmith import orchestrator as orch
    from programsmith.cells import task_matrix as tm
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    _run_at(tmp_path, ["pass"])
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    (tmp_path / "task_matrix.json").write_text(json.dumps(
        {"candidates": [{"recommendation": "viable", "target_language": "Rust",
                         "scope_unit": "subsystem"}]}))
    def _boom(*a, **k):
        raise AssertionError("propose() must not re-run when task_matrix.json exists")
    monkeypatch.setattr(tm, "propose", _boom)
    res = orch._h_task_matrix(st, man, tmp_path, {})   # no agentic needed — file already there
    assert res.verdict == "selected" and "[0]" in res.reason


def test_h_task_matrix_human_mode_blocks(tmp_path, monkeypatch):
    from programsmith import orchestrator as orch
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    _run_at(tmp_path, ["pass"])
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    res = orch._h_task_matrix(st, man, tmp_path, {"task_matrix_mode": "human"})
    assert res.human and "#1" in res.reason


def _at_qa_gate(tmp_path, full_entry, run_config=None):
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass", "done", "proceed",
                       "clean", "done"],
            sweeps={"full": full_entry, "qa_probe": {"verdict": "clean"}},
            run_config=run_config or _rc(full_min=0.30, full_max=0.70))
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    assert st.current_stage is Stage.QA_GATE
    return st, man


def test_h_qa_gate_auto_accepts_and_exports(tmp_path, monkeypatch):
    """Auto QA_GATE: accept computed from the recorded sweeps ⇒ deterministic export of the FULL
    task dir (tests included) to <outbox>/tasks/<slug>/ + .provenance.json, path recorded in the
    manifest snapshot."""
    from programsmith.orchestrator import _h_qa_gate
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    st, man = _at_qa_gate(tmp_path, {"status": "done", "pass_at_1": 0.4, "aggregate": 0.4,
                                     "band_verdict": "keep",
                                     "integrity": {"verdict": "pass", "reason": "ok"}})
    task = tmp_path / "task" / "demo"
    (task / "tests").mkdir(parents=True)
    (task / "task.toml").write_text("[task]\n")
    (task / "tests" / "test.sh").write_text("#!/bin/bash\n")
    outbox = tmp_path / "outbox"
    res = _h_qa_gate(st, man, tmp_path, {"outbox_dir": str(outbox)})
    assert res.verdict == "accept" and "final gate (auto)" in res.reason
    dest = outbox / "tasks" / "demo"
    assert (dest / "task.toml").exists() and (dest / "tests" / "test.sh").exists()  # FULL dir
    prov = json.loads((dest / ".provenance.json").read_text())
    assert prov["run_id"] == "r" and prov["repo"] == "o/n@abc123"
    assert prov["band"]["band_verdict"] == "keep" and prov["shelf"] == "tasks"
    assert (man.snapshot or {}).get("outbox_path") == str(dest)


def test_h_qa_gate_auto_verdict_matrix(tmp_path, monkeypatch):
    from programsmith.orchestrator import _h_qa_gate
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    ctx = {"outbox_dir": str(tmp_path / "outbox")}
    # broken integrity → revise (fixable), never accept/reject
    st, man = _at_qa_gate(tmp_path, {"status": "done", "pass_at_1": 0.4, "aggregate": 0.4,
                                     "band_verdict": "keep",
                                     "integrity": {"verdict": "fail", "reason": "oracle 0"}})
    assert _h_qa_gate(st, man, tmp_path, ctx).verdict == "revise"
    # hard-keep at zero pass (ADR-0041) → accept
    man.sweeps["full"] = {"status": "done", "pass_at_1": 0.0, "aggregate": 0.0,
                          "band_verdict": "too_hard", "hard_keep": True}
    st2, man2 = st, man
    task = tmp_path / "task" / "demo"; task.mkdir(parents=True, exist_ok=True)
    (task / "task.toml").write_text("[task]\n")
    assert _h_qa_gate(st2, man2, tmp_path, ctx).verdict == "accept"
    # surviving BAD_* label → revise even with an in-window band
    man.sweeps["full"] = {"status": "done", "pass_at_1": 0.4, "band_verdict": "keep",
                          "analysis": {"breakdown": {"BAD_FAILURE": 1}}}
    assert _h_qa_gate(st, man, tmp_path, ctx).verdict == "revise"
    # a dirty probe recorded upstream → revise
    man.sweeps["full"] = {"status": "done", "pass_at_1": 0.4, "band_verdict": "keep"}
    man.sweeps["qa_probe"] = {"verdict": "harden"}
    assert _h_qa_gate(st, man, tmp_path, ctx).verdict == "revise"


def test_h_qa_gate_human_mode_blocks(tmp_path, monkeypatch):
    from programsmith.orchestrator import _h_qa_gate
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    st, man = _at_qa_gate(tmp_path, {"status": "done", "pass_at_1": 0.4, "band_verdict": "keep"})
    res = _h_qa_gate(st, man, tmp_path, {"qa_gate_mode": "human"})
    assert res.human and "#2" in res.reason


def test_drive_exports_easy_shelf(tmp_path, monkeypatch):
    """A run landing on EASY_SHELF is exported to <outbox>/easy/<slug>/ with provenance — kept,
    never trashed (ADR-0040)."""
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    run_dir = tmp_path / "run"
    _run_at(run_dir, ["pass", "selected", "pass", "pass", "pass", "pass", "done", "proceed", "clean"])
    task = run_dir / "task" / "demo"
    task.mkdir(parents=True)
    (task / "task.toml").write_text("[task]\n")
    registry = {Stage.FULL_SWEEP: _stub("shelve")}
    res = drive(run_dir, ctx={"outbox_dir": str(tmp_path / "outbox")}, registry=registry)
    assert res.final_stage == "EASY_SHELF" and res.final_status == "easy"
    dest = tmp_path / "outbox" / "easy" / "demo"
    assert (dest / "task.toml").exists()
    assert json.loads((dest / ".provenance.json").read_text())["shelf"] == "easy"
    assert "exported" in res.steps[-1]["reason"]


def test_synth_trigger_maps_ease_verdict(tmp_path):
    from programsmith.orchestrator import _synth_trigger
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass", "done", "ease"])
    st = RunState.load(tmp_path)
    move, from_stage, _why = _synth_trigger(st)
    assert move == "ease" and from_stage == "CALIBRATE"


def test_synth_job_name_carries_all_counters(tmp_path):
    """The bg synthesize job id must be unique per tune generation INCLUDING ease — an ease after a
    harden is a distinct patch (a stale 'done' job must never instant-apply)."""
    from programsmith import jobs
    from programsmith.orchestrator import _h_synth
    tmp_path = tmp_path / "runs" / "r"  # nested: isolate the sibling-scan (see bg-non-blocking test)
    tmp_path.parent.mkdir(parents=True)
    _run_at(tmp_path, ["pass", "selected", "pass", "pass", "pass", "pass", "done", "ease"])
    st, man = RunState.load(tmp_path), Manifest.load(tmp_path)
    assert st.current_stage is Stage.SYNTHESIZE and st.ease == 1
    (tmp_path / "task" / "demo").mkdir(parents=True)
    ctx = {"agentic": True, "agentic_background": True,
           "agent_session": lambda p, td: "ok",
           "llm_runner": lambda _p: json.dumps({
               "task_dir": str(tmp_path / "task" / "demo"), "move": "revise",
               "from_stage": "CALIBRATE", "reason": "r",
               "patch": [{"file": "tests/test.sh", "change": "soften", "regenerated": False}],
               "addresses": [{"kind": "underspecification", "detail": "d"}],
               "preserves_identity": True}),
           "validator": None}
    res = _h_synth(st, man, tmp_path, ctx)
    assert "synthesize-h0-r0-e1" in res.reason
    # the job exists under the counter-qualified name
    import time
    for _ in range(100):
        if "synthesize-h0-r0-e1" in jobs.get_jobs(tmp_path):
            break
        time.sleep(0.02)
    assert "synthesize-h0-r0-e1" in jobs.get_jobs(tmp_path)
