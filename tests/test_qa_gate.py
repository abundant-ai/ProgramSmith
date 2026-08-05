"""Offline tests for the final QA GATE decision (auto by default, ADR-0039) — the new signature
over recorded band-verdict + integrity + probe + hard-keep + analysis flags."""

from programsmith.gates.qa_gate import qa_gate


def test_accept_in_window_all_clean():
    r = qa_gate("keep", integrity_ok=True, probe_clean=True, hard_keep=False, analysis_concern=False)
    assert r.verdict == "accept"


def test_accept_hard_keep_at_zero_pass():
    # ADR-0041: a 0/N band ships ONLY with trajectory-verified capability headroom (hard_keep)
    r = qa_gate("too_hard", hard_keep=True)
    assert r.verdict == "accept" and "hard-keep" in r.reason


def test_revise_on_broken_integrity():
    # integrity outranks everything fixable — even an in-window band never ships on oracle≠1/nop≠0
    assert qa_gate("keep", integrity_ok=False).verdict == "revise"


def test_revise_on_dirty_probe():
    assert qa_gate("keep", probe_clean=False).verdict == "revise"


def test_revise_on_analysis_concern():
    # a surviving BAD_* label (gamed pass / task-defect failure) is fixable → revise, never accept
    assert qa_gate("keep", analysis_concern=True).verdict == "revise"
    assert qa_gate("too_hard", hard_keep=True, analysis_concern=True).verdict == "revise"


def test_reject_out_of_window_without_hard_keep():
    # too_hard with NO verified headroom = broken/underspecified evidence (rare — upstream FSM
    # normally eases/drops first); same for an unmeasured band
    assert qa_gate("too_hard", hard_keep=False).verdict == "reject"
    assert qa_gate(None).verdict == "reject"


def test_detail_carries_all_inputs():
    r = qa_gate("keep", integrity_ok=True, probe_clean=True, hard_keep=False, analysis_concern=False)
    assert r.detail == {"band_verdict": "keep", "integrity_ok": True, "probe_clean": True,
                        "hard_keep": False, "analysis_concern": False}
