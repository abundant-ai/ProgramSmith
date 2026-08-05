"""Offline tests for the WORKFLOW ANALYSIS data layer (backward-move logging)."""

from programsmith.state import RunState
from programsmith.workflow_notes import record_backward_move


def _to_calibrate(s: RunState) -> None:
    for v in ("pass", "selected", "pass", "pass", "pass", "pass", "done"):
        s.advance(v)


def _to_full_sweep(s: RunState) -> None:
    _to_calibrate(s)
    s.advance("proceed")
    s.advance("clean")


def test_harden_move_recorded(tmp_path):
    s = RunState.start("r", "task:x", "difft")
    _to_calibrate(s)
    dec = s.advance("harden")  # CALIBRATE -> SYNTHESIZE
    notes = tmp_path / "WORKFLOW_NOTES.md"
    rec = record_backward_move(s, dec, trigger="calibrate: pass@1=1.0 saturated",
                               notes_path=notes, what_failed="saturated", what_changed="hardened cases")
    assert rec is not None and rec.move == "harden" and rec.loop_index == 1
    txt = notes.read_text()
    assert "move: harden" in txt
    assert "from_stage: CALIBRATE" in txt and "to_stage: SYNTHESIZE" in txt


def test_ease_move_recorded_from_verdict(tmp_path):
    """The move kind is read from the routed VERDICT (deterministic — no text sniffing): an ease
    edge records move=ease with the ease counter as its loop index."""
    s = RunState.start("r", "task:x", "difft")
    _to_full_sweep(s)
    dec = s.advance("ease")  # FULL_SWEEP -> SYNTHESIZE (frontier tune)
    rec = record_backward_move(s, dec, trigger="frontier: zero-pass with task-design blockers",
                               notes_path=tmp_path / "w.md")
    assert rec is not None and rec.move == "ease" and rec.loop_index == 1
    assert "move: ease" in (tmp_path / "w.md").read_text()


def test_shelving_recorded_as_move(tmp_path):
    """EASY_SHELF is a logged off-ramp (ADR-0040): which sources saturate the frontier is exactly
    what the offline mining loop needs to steer the ideas backlog."""
    s = RunState.start("r", "task:x", "difft")
    _to_full_sweep(s)
    dec = s.advance("shelve")
    rec = record_backward_move(s, dec, trigger="frontier saturated; too easy to harden",
                               notes_path=tmp_path / "w.md")
    assert rec is not None and rec.move == "shelve"
    txt = (tmp_path / "w.md").read_text()
    assert "to_stage: EASY_SHELF" in txt and "shelved" in txt


def test_forward_move_not_recorded(tmp_path):
    s = RunState.start("r", "task:x")
    dec = s.advance("pass")  # INGEST_LOCK -> TASK_MATRIX (forward)
    assert record_backward_move(s, dec, trigger="ok", notes_path=tmp_path / "w.md") is None


def test_drop_recorded(tmp_path):
    s = RunState.start("r", "task:x")
    dec = s.advance("fail")  # INGEST_LOCK -> DROPPED
    rec = record_backward_move(s, dec, trigger="ingest failed", notes_path=tmp_path / "w.md")
    assert rec is not None and rec.move == "drop"
    assert "n/a — dropped" in (tmp_path / "w.md").read_text()


def test_sanity_fail_records_revise(tmp_path):
    s = RunState.start("r", "task:x")
    for v in ("pass", "selected", "pass", "pass"):
        s.advance(v)
    dec = s.advance("fail")  # SANITY fail edge → a 'revise' patch
    rec = record_backward_move(s, dec, trigger="sanity failed", notes_path=tmp_path / "w.md")
    assert rec is not None and rec.move == "revise" and rec.loop_index == 1
