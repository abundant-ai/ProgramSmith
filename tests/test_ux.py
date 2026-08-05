"""The console UX layer (ux.py) — SWE-gen grammar: typed outcome taxonomy with the remediation
flag/command NAMED in every actionable message, honest cost preview (exact trial counts, $ only
when a cap is configured), foreground drive loops that park cleanly, and the doctor report.
Presentation only — these tests pin that ux NEVER invents a verdict beyond what drive() recorded."""

from pathlib import Path

from programsmith.orchestrator import DriveResult
from programsmith import ux
from programsmith.runconfig import default_run_config


# ---- typed taxonomy ------------------------------------------------------------------------

def test_classify_done_and_easy_and_dropped():
    done = ux.classify_halt("k", halted="terminal", stage="DONE", status="done", reason="accepted")
    assert done.kind == "done" and done.advice == ""
    easy = ux.classify_halt("k", halted="terminal", stage="EASY_SHELF", status="easy",
                            reason="frontier saturated")
    assert easy.kind == "easy" and "--frontier" in easy.advice   # remediation flag named
    cl = ux.classify_halt("k", halted="terminal", stage="DROPPED", status="dropped",
                          reason="copyleft license AGPL-3.0")
    assert cl.kind == "skipped" and "--allow-copyleft" in cl.advice


def test_classify_human_gate_names_the_command():
    tm = ux.classify_halt("k", halted="human", stage="TASK_MATRIX", status="in_progress",
                          reason="awaiting review")
    assert tm.kind == "needs-review" and "programsmith pick k" in tm.advice
    qa = ux.classify_halt("k", halted="human", stage="QA_GATE", status="in_progress",
                          reason="awaiting review")
    assert "programsmith qa-gate k" in qa.advice


def test_classify_actionable_blocks_name_remediation():
    job = ux.classify_halt("k", halted="blocked", stage="CREATE", status="in_progress",
                           reason="agentic job errored 3/3 attempts")
    assert job.kind == "error" and "programsmith retry k" in job.advice
    dk = ux.classify_halt("k", halted="blocked", stage="SANITY", status="in_progress",
                          reason="Docker daemon not reachable")
    assert dk.kind == "error" and "doctor" in dk.advice
    bound = ux.classify_halt("k", halted="terminal", stage="BLOCKED", status="blocked",
                             reason="harden bound exhausted")
    assert "programsmith reopen k" in bound.advice


# ---- cost preview ---------------------------------------------------------------------------

def test_billable_rows_exact_counts(monkeypatch, tmp_path):
    """Exact billable trials: smoke agents + frontier agents + the 1 QA-probe auditor. Baselines
    (oracle/nop) never appear — they execute binaries, not LLMs."""
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "cfg.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    rows = ux._billable_rows(default_run_config())
    stages = [r[0] for r in rows]
    assert stages == ["smoke", "frontier", "qa-probe"]
    assert sum(r[3] for r in rows) == 3 + 3 + 1
    assert not any("oracle" in r[1] or "nop" in r[1] for r in rows)


# ---- foreground drive loops (injected drive_fn — no docker, no real runs) --------------------

def _terminal_result(stage="DONE", status="done", steps=None):
    return DriveResult(steps=steps or [], final_stage=stage, final_status=status,
                       halted="terminal", halt_reason="accepted + exported")


def test_drive_run_foreground_returns_typed_outcome(tmp_path):
    calls = []

    def fake_drive(run_dir, *, ctx, notes_path=None):
        calls.append(run_dir)
        return _terminal_result(steps=[
            {"stage": "INGEST_LOCK", "verdict": "pass", "next": "TASK_MATRIX", "reason": "ok"},
            {"stage": "TASK_MATRIX", "verdict": "selected", "next": "ORACLE_GOLDEN", "reason": "ok"},
        ])

    out = ux.drive_run_foreground(tmp_path / "demo", ctx={}, drive_fn=fake_drive, interval=0.01)
    assert out.kind == "done" and len(calls) == 1


def test_drive_output_hides_internal_fsm_details(tmp_path, capsys):
    def fake_drive(run_dir, *, ctx, notes_path=None):
        return _terminal_result(steps=[
            {"stage": "TASK_MATRIX", "verdict": "selected", "next": "ORACLE_GOLDEN",
             "reason": "auto-pick: candidate [0] — picked; task identity recomputed"},
        ])

    ux.drive_run_foreground(tmp_path / "demo", ctx={}, drive_fn=fake_drive, interval=0.01)
    rendered = capsys.readouterr().out
    assert "Task matrix" in rendered
    assert "Candidate selected" in rendered
    assert "TASK_MATRIX" not in rendered
    assert "ORACLE_GOLDEN" not in rendered
    assert "identity recomputed" not in rendered


def test_drive_run_foreground_halts_at_human_gate(tmp_path):
    def fake_drive(run_dir, *, ctx, notes_path=None):
        return DriveResult(steps=[], final_stage="QA_GATE", final_status="in_progress",
                           halted="human", halt_reason="awaiting HUMAN REVIEW #2")

    out = ux.drive_run_foreground(tmp_path / "demo", ctx={}, drive_fn=fake_drive, interval=0.01)
    assert out.kind == "needs-review" and "qa-gate" in out.advice


def test_drive_fleet_foreground_parks_every_run(tmp_path):
    """The farm loop drives each key until parked and returns one typed outcome per key —
    a mix of done / easy / human, never dropping a run from the report."""
    results = {
        "a": _terminal_result("DONE", "done"),
        "b": _terminal_result("EASY_SHELF", "easy"),
        "c": DriveResult(steps=[], final_stage="TASK_MATRIX", final_status="in_progress",
                         halted="human", halt_reason="awaiting review"),
    }

    def fake_drive(run_dir, *, ctx, notes_path=None):
        return results[Path(run_dir).name]

    out = ux.drive_fleet_foreground(tmp_path, ["a", "b", "c"], ctx={}, drive_fn=fake_drive,
                                    interval=0.01, prune=False)
    assert {k: o.kind for k, o in out.items()} == {"a": "done", "b": "easy", "c": "needs-review"}


# ---- doctor ----------------------------------------------------------------------------------

def test_doctor_report_exit_codes():
    ready = {"ready": True, "checks": [{"name": "docker", "ok": True, "detail": "running"}],
             "providers": {"anthropic": {"present": True, "detail": "key set"}}}
    assert ux.doctor_report(ready) == 0
    broken = {"ready": False, "checks": [{"name": "docker", "ok": False, "detail": "down"}],
              "providers": {}}
    assert ux.doctor_report(broken) == 1


# ---- CLI wiring ------------------------------------------------------------------------------

def test_cli_doctor_wired(monkeypatch, capsys):
    from programsmith.cli import main
    monkeypatch.setattr("programsmith.preflight.check_preflight",
                        lambda config=None: {"ready": True, "checks": [], "providers": {}})
    assert main(["doctor"]) == 0
    rendered = capsys.readouterr().out
    assert "ready" in rendered
    assert "programsmith create --repo" not in rendered


def test_cli_retry_and_reopen(tmp_path, monkeypatch, capsys):
    """`retry` clears errored jobs (idempotent); `reopen` re-enters a terminal run and refuses a
    non-terminal one with the reason printed — both by KEY, like the UI buttons."""
    from programsmith.cli import main
    from programsmith.state import RunState
    runs = tmp_path / "runs"
    (runs / "r").mkdir(parents=True)
    st = RunState.start("run-x", "x", slug="r")
    st.save(runs / "r")
    assert main(["retry", "r", "--runs-dir", str(runs)]) == 0
    assert "cleared 0" in capsys.readouterr().out
    # non-terminal reopen refuses
    assert main(["reopen", "r", "--runs-dir", str(runs)]) == 1
    # unknown key
    assert main(["retry", "nope", "--runs-dir", str(runs)]) == 2


def test_cli_status_by_key_and_dev_status(tmp_path, monkeypatch, capsys):
    """`status <key>` is the user-facing detail view (old `show`); the plumbing FSM dump moved to
    `dev status --run-dir`."""
    from programsmith.cli import main
    from programsmith.state import RunState
    runs = tmp_path / "runs"
    (runs / "r").mkdir(parents=True)
    RunState.start("run-x", "x", slug="r").save(runs / "r")
    assert main(["status", "r", "--runs-dir", str(runs)]) == 0
    assert "INGEST_LOCK" in capsys.readouterr().out
    assert main(["dev", "status", "--run-dir", str(runs / "r")]) == 0
    assert "stage=INGEST_LOCK" in capsys.readouterr().out
