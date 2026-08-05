"""Offline tests for the SANITY gate verdict logic (docker calls injected). The real two-phase
verifier run on the shipped minpack image is a manual/backgrounded dogfood, not a unit test."""

from pathlib import Path

from programsmith.gates.sanity import run_sanity, run_sanity_trials

_OK_BUILD = lambda: (True, "built")


def _phaser(oracle: str, nop: str, priv: str):
    def run(snippet: str) -> str:
        if "solve.sh" in snippet:
            return oracle
        if "private.enc" in snippet:
            return priv
        return nop
    return run


def test_sanity_all_pass():
    phaser = _phaser(
        oracle="REWARD=1\nPRODUCED_OWNER=nobody\n",
        nop="REWARD=0\n",
        priv="ENC=DENIED\nKEY=DENIED\n",
    )
    res = run_sanity(Path("/x"), build=True, builder=_OK_BUILD, phase_runner=phaser)
    assert res.verdict == "pass"
    assert all(res.detail["checks"].values())


def test_sanity_fails_when_nop_rewards_one():
    phaser = _phaser("REWARD=1\nPRODUCED_OWNER=nobody\n", "REWARD=1\n", "ENC=DENIED\nKEY=DENIED\n")
    res = run_sanity(Path("/x"), builder=_OK_BUILD, phase_runner=phaser)
    assert res.verdict == "fail"
    assert "nop_reward_0" in res.reason


def test_sanity_fails_when_oracle_not_nobody():
    phaser = _phaser("REWARD=1\nPRODUCED_OWNER=root\n", "REWARD=0\n", "ENC=DENIED\nKEY=DENIED\n")
    res = run_sanity(Path("/x"), builder=_OK_BUILD, phase_runner=phaser)
    assert res.verdict == "fail"
    assert "produced_owned_by_nobody" in res.reason


def test_sanity_passes_when_verifier_produced_no_files():
    """Regression (pb10 tengo false-fail): the ProgramBench verify.py writes only reward/metrics —
    nothing under /logs/verifier/produced — so PRODUCED_OWNER=none is vacuously fine (the ENC/KEY
    probes still prove the privilege boundary). Only a wrong OWNER (e.g. root) is a failure."""
    phaser = _phaser("REWARD=1\nPRODUCED_OWNER=none\n", "REWARD=0\n", "ENC=DENIED\nKEY=DENIED\n")
    res = run_sanity(Path("/x"), builder=_OK_BUILD, phase_runner=phaser)
    assert res.verdict == "pass"


def test_sanity_fails_when_nobody_reads_enc():
    phaser = _phaser("REWARD=1\nPRODUCED_OWNER=nobody\n", "REWARD=0\n", "ENC=READ\nKEY=DENIED\n")
    res = run_sanity(Path("/x"), builder=_OK_BUILD, phase_runner=phaser)
    assert res.verdict == "fail"
    assert "enc_denied_to_nobody" in res.reason


def test_sanity_fails_on_build_error():
    res = run_sanity(Path("/x"), builder=lambda: (False, "boom"), phase_runner=_phaser("", "", ""))
    assert res.verdict == "fail"
    assert "build failed" in res.reason


# ---- baseline-trials SANITY (Docker-less read path, ADR-0017) -------------------------

def test_sanity_trials_passes_on_oracle1_nop0():
    trials = [
        {"agent": "oracle", "model": "default", "reward": 1.0},
        {"agent": "nop", "model": "default", "reward": 0.0},
    ]
    res = run_sanity_trials(trials)
    assert res.verdict == "pass"
    assert all(res.detail["checks"].values())
    assert "priv-drop" in res.reason.lower()  # documents the deferred A/B probe


def test_sanity_trials_fails_when_nop_rewards_one():
    trials = [
        {"agent": "oracle", "reward": 1},
        {"agent": "nop", "reward": 1},
    ]
    res = run_sanity_trials(trials)
    assert res.verdict == "fail" and "nop_baseline_reward_0" in res.reason


def test_sanity_trials_fails_when_baseline_missing():
    res = run_sanity_trials([{"agent": "oracle", "reward": 1}])  # no nop
    assert res.verdict == "fail" and "baselines_present" in res.reason
