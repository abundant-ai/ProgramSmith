"""End-to-end pipeline drive: prove the orchestrator flows the FULL DAG INGEST→…→DONE with ZERO
human touches (ADR-0039: both gates default auto), plus the two new off-ramps — the EASY SHELF
(ADR-0040) and the verified HARD-KEEP (ADR-0041).

Unit tests prove each gate/cell in isolation; this proves they COMPOSE. Environment-gated stages
are satisfied through the SAME injectable boundaries the real pipeline uses: manifest.sweeps for
the cloud reads, duck-typed cell fakes for the LLM one-shots (schema-independent on purpose — the
candidate schema is concurrently evolving), and registry overrides only where a stage needs a
whole external checkout (STATIC_CI). Nothing is synthetic in the driver itself: every verdict
still flows through the real handlers + the real FSM.
"""

import json
from pathlib import Path

from programsmith.fsm import Stage
from programsmith.manifest import Manifest, SourceInfo
from programsmith.orchestrator import REGISTRY, StageResult, drive
from programsmith.state import RunState


def _stub(verdict):
    return lambda s, m, rd, ctx: StageResult(verdict=verdict, reason=f"stub:{verdict}")


def _rc(diff_max=0.90, full_min=0.30, full_max=0.70):
    """Explicit per-run bands (the ADR-0040 defaults, pinned here so the e2e doesn't couple to
    runconfig's default literals)."""
    return {
        "difficulty": {"agents": [{"harness": "mini-swe", "model": "zai/glm-5.2", "n_trials": 3}],
                       "band": {"basis": "aggregate", "min_pass": 0.0, "max_pass": diff_max}},
        "full": {"agents": [{"harness": "mini-swe", "model": "anthropic/claude-opus-4-8", "n_trials": 3}],
                 "band": {"basis": "aggregate", "min_pass": full_min, "max_pass": full_max}},
    }


def _oracle_bundle(tmp: Path) -> Path:
    b = tmp / "bundle"
    (b / "oracle" / "src").mkdir(parents=True)
    (b / "oracle" / "Cargo.toml").write_text("[package]\nname='oracle'\n")
    (b / "goldens").mkdir(parents=True)
    (b / "goldens" / "goldens_public.json").write_text(json.dumps([{"id": "a"}]))
    (b / "goldens" / "goldens_heldout.json").write_text(json.dumps([{"id": "b"}]))
    return b


def _fake_skeleton(monkeypatch, todos=()):
    """CREATE stand-in: the vendored generator needs a full ORACLE bundle + toolchain (covered in
    test_create/test_programbench_generator); the e2e only needs a complete-looking task dir."""
    from types import SimpleNamespace

    from programsmith import orchestrator as orch

    def fake(manifest, out_dir, **_kw):
        d = Path(out_dir)
        (d / "tests").mkdir(parents=True, exist_ok=True)
        (d / "task.toml").write_text("[task]\n")
        (d / "instruction.md").write_text("reimplement the tool\n")
        (d / "tests" / "test.sh").write_text("#!/bin/bash\n")
        return SimpleNamespace(todos=list(todos))
    monkeypatch.setattr(orch, "assemble_skeleton", fake)


def _fake_matrix(monkeypatch, cands):
    """Duck-typed TASK_MATRIX propose() fake — the auto handler must only touch .candidates /
    .recommendation / model_dump() / model_dump_json()."""
    from programsmith.cells import task_matrix as tm

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

    monkeypatch.setattr(tm, "propose", lambda m, runner=None, model=None: _Out())


def _groups(pa: float) -> dict:
    return {"mini-swe@anthropic/claude-opus-4-8": {"passes": round(pa * 3), "n": 3, "pass_at_1": pa}}


def _labels(*labels):
    return {"labels": [{"trial_id": f"t{i}", "label": l} for i, l in enumerate(labels)],
            "breakdown": {l: labels.count(l) for l in set(labels)}}


def test_full_auto_pipeline_ingest_to_done(tmp_path, monkeypatch):
    """The headline flow: one `drive()` call carries a fresh post-ingest run INGEST→…→DONE with
    ZERO human verdicts — auto TASK_MATRIX pick, in-band smoke + frontier, auto QA_GATE accept,
    deterministic outbox export."""
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))  # defaults: both gates auto
    run_dir = tmp_path / "run"
    source = run_dir / "source"
    source.mkdir(parents=True)
    (source / "main.c").write_text("int main(void) { return 0; }\n")
    (source / "README.md").write_text("# widget\nOffline stdin transformer with JSON output.\n")

    m = Manifest(run_id="r", task_identity="task:x", slug="demo")
    m.source = SourceInfo(repo="acme/widget", pinned_sha="abc1234", primary_language="C",
                          license="MIT", license_class="permissive", size_loc=6_000,
                          clone_path=str(source))
    m.run_config = _rc()
    # Pre-recorded sweep reads (the sweep-read import path): in-band smoke (1/3) with clean labels,
    # in-band frontier (2/3 — inside [1/3, 2/3]) with honest labels + green integrity, clean probe.
    m.sweeps = {
        "sanity": {"trials": [{"agent": "oracle", "reward": 1}, {"agent": "nop", "reward": 0}]},
        "difficulty": {"status": "done", "pass_at_1": 0.3333,
                       "groups": {"mini-swe@zai/glm-5.2": {"passes": 1, "n": 3, "pass_at_1": 0.3333}},
                       "analysis": _labels("GOOD_SUCCESS", "GOOD_FAILURE", "GOOD_FAILURE")},
        "full": {"status": "done", "pass_at_1": 0.6667, "groups": _groups(0.6667),
                 "integrity": {"verdict": "pass", "reason": "oracle=1/nop=0"},
                 "analysis": _labels("GOOD_SUCCESS", "GOOD_SUCCESS", "GOOD_FAILURE")},
        "qa_probe": {"verdict": "clean", "summary": "auditor: SOLVABLE_AS_WRITTEN, 0 blockers"},
    }
    m.save(run_dir)
    s = RunState.start("r", "task:x", "demo")
    s.advance("pass")          # INGEST runs at creation (same as cli._create_run)
    s.save(run_dir)

    _fake_matrix(monkeypatch, [{"recommendation": "recommended", "rationale": "the tool itself"}])
    _fake_skeleton(monkeypatch)
    registry = {**REGISTRY, Stage.STATIC_CI: _stub("pass")}   # needs a harbor-lh checkout otherwise
    ctx = {"oracle_bundle": str(_oracle_bundle(tmp_path)), "agentic": True,
           "outbox_dir": str(tmp_path / "outbox")}

    res = drive(run_dir, ctx=ctx, registry=registry)
    assert res.final_stage == "DONE" and res.final_status == "done", res
    assert res.halted == "terminal"
    assert [st["stage"] for st in res.steps] == [
        "TASK_MATRIX", "ORACLE_GOLDEN", "CREATE", "SANITY", "STATIC_CI",
        "DIFFICULTY_SWEEP", "CALIBRATE", "QA_PROBE", "FULL_SWEEP", "QA_GATE"]
    # zero human halts, zero QA_ON_GPT/PR hops — the ADR-0039 shape
    verdicts = {st["stage"]: st["verdict"] for st in res.steps}
    assert verdicts["TASK_MATRIX"] == "selected" and verdicts["QA_GATE"] == "accept"
    assert verdicts["CALIBRATE"] == "proceed" and verdicts["FULL_SWEEP"] == "done"

    # the accepted task was EXPORTED (full dir incl. tests) with provenance
    dest = tmp_path / "outbox" / "tasks" / "demo"
    assert (dest / "task.toml").exists() and (dest / "tests" / "test.sh").exists()
    prov = json.loads((dest / ".provenance.json").read_text())
    assert prov["repo"] == "acme/widget@abc1234" and prov["band"]["band_verdict"] == "keep"
    final = RunState.load(run_dir)
    assert final.terminal and final.status == "done"
    assert (Manifest.load(run_dir).snapshot or {})["outbox_path"] == str(dest)


def test_easy_shelf_path(tmp_path, monkeypatch):
    """A frontier-saturated task whose hardening isn't converging is SHELVED, not trashed
    (ADR-0040): FULL_SWEEP → shelve → EASY_SHELF terminal (+ export to <outbox>/easy)."""
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    run_dir = tmp_path / "run"
    m = Manifest(run_id="r", task_identity="task:x", slug="demo")
    m.source = SourceInfo(repo="acme/widget", pinned_sha="abc", primary_language="C",
                          license="MIT", license_class="permissive")
    m.run_config = _rc()
    m.sweeps = {"full": {"status": "done", "pass_at_1": 1.0, "groups": _groups(1.0),
                         "integrity": {"verdict": "pass", "reason": "ok"},
                         "analysis": _labels("GOOD_SUCCESS", "GOOD_SUCCESS", "GOOD_SUCCESS")}}
    # two prior non-converging hardens → the harden-review auditor recommends drop → SHELVE
    m.harden_history = [{"pass_at_1": 1.0}, {"pass_at_1": 1.0}]
    m.save(run_dir)
    s = RunState.start("r", "task:x", "demo")
    for v in ("pass", "selected", "pass", "pass", "pass", "pass", "done", "proceed", "clean"):
        s.advance(v)
    s.harden = 2
    s.save(run_dir)
    task = run_dir / "task" / "demo"
    task.mkdir(parents=True)
    (task / "task.toml").write_text("[task]\n")

    res = drive(run_dir, ctx={"outbox_dir": str(tmp_path / "outbox")},
                notes_path=tmp_path / "WF.md")
    assert res.final_stage == "EASY_SHELF" and res.final_status == "easy"
    step = res.steps[-1]
    assert step["verdict"] == "shelve" and "exported" in step["reason"]
    dest = tmp_path / "outbox" / "easy" / "demo"
    assert (dest / "task.toml").exists()
    assert json.loads((dest / ".provenance.json").read_text())["shelf"] == "easy"
    assert "move: shelve" in (tmp_path / "WF.md").read_text()   # logged for the mining loop


def test_hard_keep_path_zero_pass_all_good_failures(tmp_path, monkeypatch):
    """ADR-0041 (hardened per ADR-0048): a frontier 0/3 ships as hard_keep ONLY after OUR OWN deep
    trajectory audit confirms capability headroom — all-GOOD_FAILURE classifier labels are just the
    prefilter. Verified → kept (hard_keep), auto-accepted at QA_GATE, exported to the outbox."""
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    run_dir = tmp_path / "run"
    m = Manifest(run_id="r", task_identity="task:x", slug="demo")
    m.source = SourceInfo(repo="acme/widget", pinned_sha="abc", primary_language="C",
                          license="MIT", license_class="permissive")
    m.run_config = _rc()   # frontier floor 0.30 → 0/3 reads too_hard, must earn its keep
    m.sweeps = {"full": {"status": "done", "pass_at_1": 0.0, "groups": _groups(0.0),
                         "integrity": {"verdict": "pass", "reason": "ok"},
                         "analysis": _labels("GOOD_FAILURE", "GOOD_FAILURE", "GOOD_FAILURE")},
                "qa_probe": {"verdict": "clean"}}
    m.save(run_dir)
    s = RunState.start("r", "task:x", "demo")
    for v in ("pass", "selected", "pass", "pass", "pass", "pass", "done", "proceed", "clean"):
        s.advance(v)
    s.save(run_dir)
    task = run_dir / "task" / "demo"
    task.mkdir(parents=True)
    (task / "task.toml").write_text("[task]\n")

    # the mandatory deep audit (injected runner) confirms every failed trial is genuine headroom
    audit = json.dumps({"trials": [{"trial_id": f"t{i}", "failure_mode": "capability_headroom",
                                    "evidence": "engaged, partial progress"} for i in range(3)],
                        "summary": "hard"})
    res = drive(run_dir, ctx={"outbox_dir": str(tmp_path / "outbox"),
                              "llm_runner": lambda _p: audit})
    assert res.final_stage == "DONE" and res.final_status == "done", res
    verdicts = {st["stage"]: st["verdict"] for st in res.steps}
    assert verdicts["FULL_SWEEP"] == "done" and verdicts["QA_GATE"] == "accept"
    man = Manifest.load(run_dir)
    assert man.sweeps["full"]["hard_keep"] is True
    assert man.sweeps["full"]["goodfail"]["source"] == "deep_audit"   # never labels alone (ADR-0048)
    prov = json.loads((tmp_path / "outbox" / "tasks" / "demo" / ".provenance.json").read_text())
    assert prov["band"]["hard_keep"] is True and prov["band"]["pass_at_1"] == 0.0


# ---- live-boundary behaviors preserved from the pre-pivot e2e ---------------------------

def _complete_bundle(tmp: Path) -> Path:
    b = tmp / "bundle"
    b.mkdir(parents=True, exist_ok=True)
    (b / "task.toml").write_text("[task]\nname='demo'\n")
    return b


def _at_qa_probe(run_dir: Path) -> RunState:
    m = Manifest(run_id="r", task_identity="task:x", slug="demo")
    m.source = SourceInfo(repo="acme/widget", pinned_sha="abc", primary_language="C",
                          license="MIT", license_class="permissive")
    m.save(run_dir)
    s = RunState.start("r", "task:x", "demo")
    for v in ["pass", "selected", "pass", "pass", "pass", "pass", "done", "proceed"]:
        s.advance(v)
    s.save(run_dir)
    assert s.current_stage.value == "QA_PROBE"
    return s


def test_qa_probe_runs_real_auditor_no_synthetic(tmp_path):
    """QA/PROBE launches the REAL Task Construction Auditor (injected backend — no spend): the
    frontier model, one trial, with the auditor prompt PREPENDED to instruction.md. It records +
    links the experiment, polls, reads the artifacts, and verdicts from the auditor's OWN JSON
    output (SOLVABLE_AS_WRITTEN, no blockers → clean). Nothing is synthetic."""
    run_dir = tmp_path / "run"
    _at_qa_probe(run_dir)
    bundle = _complete_bundle(tmp_path)
    (bundle / "instruction.md").write_text("ORIGINAL TASK INSTRUCTION")

    class _ProbeBackend:
        name = "local"; needs_upload = False; artifact_subdir = ".sweeps"

        def launch(self, task_dir, agents, *, experiment, extra_flags=None):
            assert "probe" in experiment
            # the auditor probe bundle is staged with the prompt prepended
            assert "task-construction auditor" in (Path(task_dir) / "instruction.md").read_text().lower()
            return "ab12cd34"

        def status(self, handle):
            return {"complete": True, "tasks_running": 0, "trials_completed": 1,
                    "trials_total": 1, "incomplete": False}

        def results(self, handle, out_dir):
            return []

        def analyses(self, handle, *, agents):
            return {"analyses": [], "pending": 0, "failed": 0, "total": 0}

        def pull_artifacts(self, handle, out_dir):
            out = Path(out_dir) / "trials" / "t1"
            out.mkdir(parents=True, exist_ok=True)
            (out / "trajectory.json").write_text(json.dumps({"steps": [
                {"source": "agent",
                 "text": '{"verdict": "SOLVABLE_AS_WRITTEN", "findings": []}'}]}))
            return Path(out_dir)

    ctx = {"sweep_live": True, "task_path": str(bundle), "sweep_backend": _ProbeBackend()}
    r1 = drive(run_dir, ctx=ctx)                       # launches the auditor probe, halts polling
    man = Manifest.load(run_dir)
    assert man.sweeps["qa_probe"]["experiment"] == "ab12cd34"
    assert man.sweeps["qa_probe"]["status"] == "running" and man.sweeps["qa_probe"]["kind"] == "auditor"
    assert r1.halted == "blocked" and "ab12cd34" in r1.halt_reason

    r2 = drive(run_dir, ctx=ctx)                       # polls complete → reads auditor verdict → advances
    man = Manifest.load(run_dir)
    assert man.sweeps["qa_probe"]["verdict"] == "clean"
    assert man.sweeps["qa_probe"]["auditor_verdict"] == "SOLVABLE_AS_WRITTEN"
    assert man.sweeps["qa_probe"]["blocker_findings"] == 0
    assert any(st["stage"] == "QA_PROBE" and st["verdict"] == "clean" for st in r2.steps)


def test_full_sweep_does_not_advance_when_all_trials_error(tmp_path):
    """A full sweep where EVERY frontier trial errored measured nothing → it must NOT advance to
    QA_GATE. It records `errored` (not `done`), stays at FULL_SWEEP, and re-runs a fresh
    experiment, bounded. This is the bug where a cancelled/all-errored sweep wrongly advanced."""
    run_dir = tmp_path / "run"
    m = Manifest(run_id="r", task_identity="task:x", slug="demo")
    m.source = SourceInfo(repo="a/b", pinned_sha="abc", primary_language="C",
                          license="MIT", license_class="permissive")
    # pin the full agents to the two harnesses the fake backend records — scoped results
    # (trials.scope_trials) only admit the sweep's OWN agents, so both must be configured
    m.run_config = {
        "difficulty": {"agents": [{"harness": "mini-swe", "model": "zai/glm-5.2", "n_trials": 3}],
                       "band": {"basis": "aggregate", "min_pass": 0.0, "max_pass": 0.90}},
        "full": {"agents": [{"harness": "claude-code", "model": "anthropic/claude-opus-4-8", "n_trials": 1},
                            {"harness": "codex", "model": "openai/gpt-5.5", "n_trials": 1}],
                 "band": {"basis": "aggregate", "min_pass": 0.30, "max_pass": 0.70}},
    }
    m.save(run_dir)
    s = RunState.start("r", "task:x", "demo")
    for v in ["pass", "selected", "pass", "pass", "pass", "pass", "done", "proceed", "clean"]:
        s.advance(v)  # at FULL_SWEEP
    s.save(run_dir)
    assert s.current_stage.value == "FULL_SWEEP"
    bundle = _complete_bundle(tmp_path)

    class _AllErroredBackend:
        name = "local"; needs_upload = False; artifact_subdir = ".sweeps"

        def launch(self, task_dir, agents, *, experiment, extra_flags=None):
            return experiment

        def status(self, handle):
            return {"complete": True, "tasks_running": 0, "trials_completed": 2,
                    "trials_total": 2, "incomplete": False}

        def results(self, handle, out_dir):  # every frontier trial errored (reward None)
            return [
                {"agent": "claude-code", "model": "claude-opus-4-8", "reward": None,
                 "is_probe": False, "status": "errored"},
                {"agent": "codex", "model": "gpt-5.5", "reward": None,
                 "is_probe": False, "status": "errored"},
            ]

        def analyses(self, handle, *, agents):
            return {"analyses": [], "pending": 0, "failed": 0, "total": 0}

        def pull_artifacts(self, handle, out_dir):
            return Path(out_dir)

    ctx = {"sweep_live": True, "task_path": str(bundle), "sweep_backend": _AllErroredBackend()}
    drive(run_dir, ctx=ctx)               # launches
    r2 = drive(run_dir, ctx=ctx)          # polls → all errored → records errored, does NOT advance
    man = Manifest.load(run_dir)
    assert RunState.load(run_dir).current_stage.value == "FULL_SWEEP"   # did NOT advance
    assert man.sweeps["full"]["status"] == "errored" and man.sweeps["full"]["n_errored"] == 2
    assert r2.halted == "blocked" and "errored" in r2.halt_reason
    # a third pass re-runs a FRESH experiment (bounded retry), not the stale one
    r3 = drive(run_dir, ctx=ctx)
    assert Manifest.load(run_dir).sweeps["full"]["status"] == "running"
    assert "attempt 2" in r3.halt_reason


def test_full_pipeline_with_smoke_tune_loop(tmp_path, monkeypatch):
    """A saturated smoke band routes CALIBRATE→harden→SYNTHESIZE and, after the patch, rejoins
    STATIC_CI (the patched task re-measures) — exercising the backward tune edge end-to-end."""
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    run_dir = tmp_path / "run"
    m = Manifest(run_id="r", task_identity="task:x", slug="demo")
    m.source = SourceInfo(repo="acme/widget", pinned_sha="abc", primary_language="C",
                          license="MIT", license_class="permissive")
    m.run_config = _rc(diff_max=0.60)     # explicit ceiling: 0.72 saturates
    m.sweeps = {"sanity": {"trials": [{"agent": "oracle", "reward": 1}, {"agent": "nop", "reward": 0}]},
                "difficulty": {"pass_at_1": 0.72}}
    m.save(run_dir)
    s = RunState.start("r", "task:x", "demo")
    for v in ["pass", "selected"]:
        s.advance(v)
    s.save(run_dir)

    from programsmith.cells.agentic import ValidationState
    _fake_skeleton(monkeypatch)
    ctx = {"oracle_bundle": str(_oracle_bundle(tmp_path)), "agentic": True,
           "agent_session": lambda p, td: "patched",
           "validator": lambda _d: ValidationState(True, True),
           "llm_runner": lambda _p: json.dumps({"type": "result", "result": json.dumps({
               "task_dir": str(run_dir / "task" / "demo"), "move": "harden", "from_stage": "CALIBRATE",
               "reason": "saturated band", "patch": [{"file": "tests/test.sh", "change": "tighten", "regenerated": False}],
               "addresses": [{"kind": "saturation", "detail": "pass@1>0.6"}], "preserves_identity": True})})}
    registry = {**REGISTRY, Stage.STATIC_CI: _stub("pass")}

    r = drive(run_dir, ctx=ctx, registry=registry, notes_path=tmp_path / "WF.md")
    verdicts = [(e["stage"], e["verdict"]) for e in r.steps]
    assert ("CALIBRATE", "harden") in verdicts
    assert ("SYNTHESIZE", "done") in verdicts          # the agentic apply ran + rejoined
    st = RunState.load(run_dir)
    assert st.harden >= 1 and st.smoke_tunes >= 1      # the smoke tune budget was consumed
    # the patch invalidated the stale sweeps → the rejoined DIFFICULTY needs a fresh launch
    assert "difficulty" not in Manifest.load(run_dir).sweeps


def test_autodrive_revives_blocked_run_when_harden_bound_raised(tmp_path, monkeypatch):
    """End-to-end stability: a run BLOCKED by harden-exhaustion at the old bound becomes ELIGIBLE
    again once HARDEN_MAX is higher than its harden count, so the auto-driver picks it up with no
    manual reset. A run already at harden==MAX stays correctly parked."""
    from programsmith.daemon import _eligible
    from programsmith.fsm import HARDEN_MAX, Stage
    from programsmith.state import StageEvent
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))

    def park_blocked(run_dir, pa, harden=HARDEN_MAX - 1):
        run_dir.mkdir(parents=True, exist_ok=True)
        RunState(run_id="r", task_identity="t", slug="demo", current_stage=Stage.BLOCKED, harden=harden,
                 history=[StageEvent(stage=Stage.CALIBRATE, verdict="harden", next=Stage.BLOCKED,
                          reason="calibrate: pass@1 saturated; harden bound exhausted")]).save(run_dir)
        m = Manifest(run_id="r", task_identity="t", slug="demo")
        m.sweeps = {"difficulty": {"pass_at_1": pa}}
        m.save(run_dir)

    borderline, severe, exhausted = tmp_path / "b", tmp_path / "s", tmp_path / "x"
    park_blocked(borderline, 0.667)
    park_blocked(severe, 1.0)
    park_blocked(exhausted, 0.667, harden=HARDEN_MAX)
    assert _eligible(borderline)        # bound rose → driver re-enters it
    assert _eligible(severe)            # saturated-hard ALSO gets the attempt (real sweep decides)
    assert not _eligible(exhausted)     # already at the current bound → correctly stays parked


def test_sweep_launch_failure_backs_off_then_bounded(tmp_path):
    """A transient LAUNCH failure self-recovers without a manual reset: it records `errored`,
    BACKS OFF before retrying (doesn't hammer the backend each pass), and is bounded by
    _LAUNCH_MAX_ATTEMPTS (generous, since launch errors are usually transient) → eventually a
    clean hard-block, not a loop."""
    from programsmith.orchestrator import _LAUNCH_MAX_ATTEMPTS, _h_difficulty
    run_dir = tmp_path / "run"
    m = Manifest(run_id="r", task_identity="t:x", slug="demo")
    m.source = SourceInfo(repo="a/b", pinned_sha="s", primary_language="C",
                          license="MIT", license_class="permissive")
    m.save(run_dir)
    s = RunState.start("r", "t:x", "demo")
    for v in ["pass", "selected", "pass", "pass", "pass"]:
        s.advance(v)  # at DIFFICULTY_SWEEP
    s.save(run_dir)
    bundle = _complete_bundle(tmp_path)

    class _FailingLaunch:  # launch always raises (simulating a transient environment error)
        name = "local"; needs_upload = False; artifact_subdir = ".sweeps"

        def launch(self, *a, **k):
            raise RuntimeError("sweep launch failed: transient boom")

        def status(self, handle):
            return {}

        def results(self, handle, out_dir):
            return []

        def analyses(self, handle, *, agents):
            return {"analyses": [], "pending": 0, "failed": 0, "total": 0}

        def pull_artifacts(self, handle, out_dir):
            return out_dir

    ctx = {"sweep_live": True, "sweep_backend": _FailingLaunch(), "task_path": str(bundle)}
    _h_difficulty(s, m, run_dir, ctx)                       # launch fails → errored, attempt 1
    d = m.sweeps["difficulty"]
    assert d["status"] == "errored" and d["experiment"] is None and d["attempts"] == 1
    r = _h_difficulty(s, m, run_dir, ctx)                   # immediate retry → backs off (no new attempt)
    assert "backing off" in r.reason and m.sweeps["difficulty"]["attempts"] == 1
    last = r
    from datetime import datetime, timedelta, timezone
    # Elapse the WITHIN-BURST backoff each pass with an errored_at ~2 min ago — past the 45s backoff, but
    # WITHIN the 20-min self-heal cooldown — so retries increment toward the bound (a far-past timestamp
    # would instead trigger the transient self-heal). → a clean, BOUNDED hard-block, not an infinite loop.
    recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    for _ in range(_LAUNCH_MAX_ATTEMPTS + 3):
        m.sweeps["difficulty"]["errored_at"] = recent
        last = _h_difficulty(s, m, run_dir, ctx)
    assert m.sweeps["difficulty"]["attempts"] == _LAUNCH_MAX_ATTEMPTS   # generous bound, not 2
    assert f"after {_LAUNCH_MAX_ATTEMPTS} attempt" in last.reason and "auto-retry" in last.reason
    # After the self-heal cooldown (errored >20 min ago, environment assumed recovered) it relaunches a FRESH
    # burst rather than staying wedged forever — attempts resets (the relaunch fails again here → 1).
    m.sweeps["difficulty"]["errored_at"] = (datetime.now(timezone.utc) - timedelta(minutes=25)).isoformat()
    _h_difficulty(s, m, run_dir, ctx)
    assert m.sweeps["difficulty"]["attempts"] == 1                      # self-heal → fresh burst
