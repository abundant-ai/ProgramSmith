"""Tests for the HARDEN REVIEW auditor (gates.harden_review) — harden-vs-drop on the trajectory."""

from programsmith.gates.harden_review import harden_review


def test_first_harden_is_always_viable():
    # no prior generations → give hardening a chance
    r = harden_review(1.0, history=[], harden_count=0)
    assert r.verdict == "harden"


def test_drops_when_budget_exhausted_without_convergence():
    # the FULL budget (default 3) is spent and pass@1 still pinned, no improvement → too easy → drop
    r = harden_review(0.95, history=[1.0, 0.95, 0.95], harden_count=3)
    assert r.verdict == "drop" and "not converging" in r.reason.lower()


def test_uses_full_budget_before_dropping():
    # two hardens in, still saturated but NOT perfectly aced → keep going (don't drop early); the
    # 3-round budget is for a reason.
    r = harden_review(0.95, history=[1.0, 0.95], harden_count=2)
    assert r.verdict == "harden"


def test_does_not_drop_after_a_single_aced_harden():
    # regression: a single aced harden must NOT drop — give the task its full budget.
    assert harden_review(1.0, history=[1.0], harden_count=1, breakdown={"GOOD_SUCCESS": 3}).verdict == "harden"


def test_empty_history_never_drops_early():
    # regression (cjson): a run whose first harden predates the auditor has an EMPTY history
    # (best_prior None) — it must NOT be dropped on the next round just because pass@1 is high.
    assert harden_review(1.0, history=[], harden_count=1).verdict == "harden"
    assert harden_review(1.0, history=[], harden_count=2).verdict == "harden"


def test_strong_evidence_drops_when_perfectly_aced_across_generations():
    # the only early exit: frontier aces EVERY trial now AND in every prior gen, ≥2 honest hardens.
    r = harden_review(1.0, history=[1.0, 1.0], harden_count=2, breakdown={"GOOD_SUCCESS": 3})
    assert r.verdict == "drop" and "doing nothing" in r.reason.lower()
    # but a single dip in the trajectory means it's NOT hopeless → keep going
    assert harden_review(1.0, history=[0.67, 1.0], harden_count=2).verdict == "harden"


def test_keeps_hardening_when_converging():
    # band is coming down across generations → hardening works → keep going
    r = harden_review(0.45, history=[1.0, 0.8], harden_count=2)
    assert r.verdict == "harden" and "improving" in r.reason.lower()


def test_gamed_saturation_is_fixable_not_dropped():
    # a BAD_SUCCESS (gamed verifier) is a hole to close, not a too-easy task → keep hardening
    r = harden_review(1.0, history=[1.0, 1.0, 1.0], harden_count=3, breakdown={"BAD_SUCCESS": 2})
    assert r.verdict == "harden" and "gamed" in r.reason.lower()


def test_no_band_defers_to_existing_flow():
    assert harden_review(None, history=[], harden_count=3).verdict == "harden"


def test_drop_after_is_configurable():
    # with drop_after=1 a non-converging task drops one generation sooner
    assert harden_review(0.9, history=[0.9], harden_count=1, drop_after=1).verdict == "drop"
    assert harden_review(0.9, history=[0.9], harden_count=1, drop_after=3).verdict == "harden"
