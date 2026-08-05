"""Offline tests for the CALIBRATE smoke gate (ADR-0040 decision order)."""

from programsmith.gates.calibrate import calibrate


def test_none_band_flags_broken():
    assert calibrate(None).verdict == "flag_broken"   # the only remaining drop reason here


def test_saturated_hardens_with_saturation_kind():
    r = calibrate(1.0, saturate_above=0.90)           # GLM 3/3 at the new smoke ceiling
    assert r.verdict == "harden" and r.detail["kind"] == "saturation"


def test_ceiling_is_exclusive():
    assert calibrate(0.90, saturate_above=0.90).verdict == "proceed"  # ≤ ceiling is in-band
    assert calibrate(0.6667, saturate_above=0.90).verdict == "proceed"  # 2/3 never saturates smoke


def test_band_verdict_wins_over_scalar():
    # per-model policy: groups said keep even though the scalar exceeds the ceiling (an "any"
    # combinator kept it) — the classification wins over the single-number fallback
    assert calibrate(1.0, saturate_above=0.90, band_verdict="keep").verdict == "proceed"
    assert calibrate(0.5, saturate_above=0.90, band_verdict="too_easy").verdict == "harden"


def test_bad_success_hardens_as_reward_hack_before_anything_else():
    # a gamed smoke pass must be fixed BEFORE spending the frontier — even when the band is in-range
    r = calibrate(0.33, saturate_above=0.90, breakdown={"BAD_SUCCESS": 1, "GOOD_SUCCESS": 1})
    assert r.verdict == "harden" and r.detail["kind"] == "reward-hack"


def test_zero_all_good_failures_proceeds():
    # "if GLM 0/3 but good failure, keep and wait to harden if needed with opus"
    r = calibrate(0.0, labels=["GOOD_FAILURE", "GOOD_FAILURE", "GOOD_FAILURE"])
    assert r.verdict == "proceed" and "headroom" in r.reason


def test_zero_with_bad_failure_eases():
    r = calibrate(0.0, labels=["GOOD_FAILURE", "BAD_FAILURE"])
    assert r.verdict == "ease" and "BAD_FAILURE" in r.reason


def test_zero_inconclusive_proceeds_to_frontier():
    # no labels / infra noise: measure, don't predict — deep audits are reserved for the frontier
    r = calibrate(0.0, labels=[])
    assert r.verdict == "proceed" and "frontier" in r.reason
    assert calibrate(0.0, labels=["HARNESS_ERROR"]).verdict == "proceed"


def test_in_band_proceeds():
    r = calibrate(0.25)
    assert r.verdict == "proceed" and "sweet-spot" in r.reason
    assert calibrate(0.50).verdict == "proceed"
