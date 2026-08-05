"""Offline tests for the fleet auto-driver: it advances eligible runs one drive() pass, and skips
paused / terminal / config'd-human-gate runs. ADR-0039: both human gates default AUTO, so a run at
TASK_MATRIX/QA_GATE is ELIGIBLE by default (the auto handlers decide); only a "human" mode config
parks it."""

from pathlib import Path

from programsmith.daemon import _eligible, autodrive_once
from programsmith.manifest import Manifest, SourceInfo
from programsmith.state import RunState


def _mk(runs: Path, key: str, verdicts, *, sweeps=None, paused=False):
    rd = runs / key
    source = rd / "source"
    source.mkdir(parents=True)
    (source / "main.c").write_text("int main(void) { return 0; }\n")
    (source / "README.md").write_text("# tool\nOffline stdin transformer with JSON output.\n")
    m = Manifest(run_id=f"r-{key}", task_identity=f"task:{key}", slug=key)
    m.source = SourceInfo(repo="o/n", pinned_sha="abc", primary_language="C",
                          license="MIT", license_class="permissive", size_loc=6_000,
                          clone_path=str(source))
    if sweeps:
        m.sweeps = sweeps
    m.save(rd)
    s = RunState.start(f"r-{key}", f"task:{key}", key)
    for v in verdicts:
        s.advance(v)
    if paused:
        s.pause()
    s.save(rd)


def _isolate_config(monkeypatch, tmp_path):
    # keep LhConfig reads off the operator's real .programsmith/config.json (defaults = both gates auto)
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))


def _calib_sweeps():
    # explicit run_config-free entry: groups give band_verdict "keep" under any default band
    return {"difficulty": {"status": "done", "pass_at_1": 0.2,
                           "groups": {"mini-swe@zai/glm-5.2": {"passes": 1, "n": 3, "pass_at_1": 0.3333}}}}


def test_autodrive_advances_eligible_and_skips_the_rest(tmp_path, monkeypatch):
    _isolate_config(monkeypatch, tmp_path)
    runs = tmp_path / "runs"
    # eligible: at CALIBRATE with a recorded in-band → advances to QA_PROBE then halts (blocked)
    _mk(runs, "calib", ["pass", "selected", "pass", "pass", "pass", "pass", "done"],
        sweeps=_calib_sweeps())
    # at TASK_MATRIX — AUTO by default, so it IS driven; with no llm_runner/agentic ctx the auto
    # handler blocks honestly (never shells out to a real model in tests)
    _mk(runs, "matrix", ["pass"])
    # paused → skipped
    _mk(runs, "paused", ["pass", "selected"], paused=True)
    # terminal → skipped
    _mk(runs, "dropped", ["fail"])                     # INGEST fail → DROPPED

    records = autodrive_once(runs)
    by_key = {r["key"]: r for r in records}
    assert set(by_key) == {"calib", "matrix"}          # paused/terminal skipped; auto gates driven
    assert by_key["calib"]["advanced"] >= 1 and by_key["calib"]["final_stage"] == "QA_PROBE"
    assert by_key["matrix"]["advanced"] == 0 and by_key["matrix"]["halted"] == "blocked"

    # the paused/terminal runs did not move; the matrix run stayed put (blocked, not advanced)
    assert RunState.load(runs / "matrix").current_stage.value == "TASK_MATRIX"
    assert RunState.load(runs / "paused").current_stage.value == "ORACLE_GOLDEN"
    assert RunState.load(runs / "dropped").status == "dropped"


def test_human_mode_config_parks_the_gate_runs(tmp_path, monkeypatch):
    """Pre-ADR-0039 behavior is one config flip away: task_matrix_mode/qa_gate_mode = "human" makes
    daemon._eligible skip those runs again (they move only on a human verdict)."""
    from programsmith.config import LhConfig
    _isolate_config(monkeypatch, tmp_path)
    cfg = LhConfig()
    cfg.task_matrix_mode = "human"
    cfg.qa_gate_mode = "human"
    cfg.save()
    runs = tmp_path / "runs"
    _mk(runs, "matrix", ["pass"])                      # at TASK_MATRIX
    assert autodrive_once(runs) == []                  # parked for the human — not driven
    # and the eligibility primitive agrees both ways
    assert not _eligible(runs / "matrix")
    cfg.task_matrix_mode = "auto"
    cfg.save()
    assert _eligible(runs / "matrix")


def test_autodrive_is_idempotent_when_nothing_is_runnable(tmp_path, monkeypatch):
    _isolate_config(monkeypatch, tmp_path)
    runs = tmp_path / "runs"
    # at ORACLE_GOLDEN with no bundle/exec-env (empty ctx) → blocked, no forward progress
    _mk(runs, "stuck", ["pass", "selected"])
    records = autodrive_once(runs)
    assert records and records[0]["advanced"] == 0    # driven, but parked (needs a bundle)
    assert RunState.load(runs / "stuck").current_stage.value == "ORACLE_GOLDEN"
    # second pass is still a no-op (idempotent)
    assert autodrive_once(runs)[0]["advanced"] == 0


def test_autodrive_empty_dir(tmp_path):
    assert autodrive_once(tmp_path / "nope") == []


def test_autodrive_fills_background_stages_before_sync_task_matrix(tmp_path, monkeypatch):
    """A slow synchronous matrix proposal must not leave Oracle worker slots idle."""
    from types import SimpleNamespace
    import programsmith.daemon as daemon

    _isolate_config(monkeypatch, tmp_path)
    runs = tmp_path / "runs"
    _mk(runs, "a-matrix", ["pass"])
    _mk(runs, "z-oracle", ["pass", "selected"])
    seen = []

    def fake_drive(run_dir, **_kwargs):
        seen.append(Path(run_dir).name)
        state = RunState.load(run_dir)
        return SimpleNamespace(steps=[], halted="blocked",
                               final_stage=state.current_stage.value,
                               halt_reason="test")

    monkeypatch.setattr(daemon, "drive", fake_drive)
    autodrive_once(runs)
    assert seen == ["z-oracle", "a-matrix"]


def test_autodrive_limits_and_rotates_synchronous_task_matrix(tmp_path, monkeypatch):
    """A matrix backlog must yield between proposals so background jobs are polled every pass."""
    _isolate_config(monkeypatch, tmp_path)
    runs = tmp_path / "runs"
    _mk(runs, "a-matrix", ["pass"])
    _mk(runs, "b-matrix", ["pass"])

    first = autodrive_once(runs)
    second = autodrive_once(runs)

    assert [record["key"] for record in first] == ["a-matrix"]
    assert [record["key"] for record in second] == ["b-matrix"]
