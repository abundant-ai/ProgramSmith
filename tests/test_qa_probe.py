"""Offline tests for the QA/PROBE verdict logic (deterministic over validated probe findings)."""

from programsmith.cells.qa_probe import ProbeFinding, build_probe_config, probe_verdict


def test_clean_when_no_exploit():
    findings = [
        ProbeFinding(kind="cheat-detector", exploit_found=False),
        ProbeFinding(kind="verifier-critic", detail="no bugs"),
        ProbeFinding(kind="task-construction-auditor", auditor_verdict="SOLVABLE_AS_WRITTEN"),
    ]
    assert probe_verdict(findings).verdict == "clean"


def test_harden_on_reward_hack():
    findings = [ProbeFinding(kind="cheat-detector", exploit_found=True, reward=1.0, catalog_id="H1")]
    res = probe_verdict(findings)
    assert res.verdict == "harden"
    assert "H1" in res.reason


def test_harden_on_gameable_auditor_verdict():
    findings = [ProbeFinding(kind="task-construction-auditor",
                             auditor_verdict="SOLVABLE_ONLY_BY_GUESSING")]
    assert probe_verdict(findings).verdict == "harden"


def test_exploit_without_reward_is_not_harden():
    # an attempted-but-failed exploit (reward 0) is not a reward-hack
    findings = [ProbeFinding(kind="cheat-detector", exploit_found=True, reward=0.0)]
    assert probe_verdict(findings).verdict == "clean"


def test_build_probe_config():
    cfg = build_probe_config(["minpack-rust-rewrite"])
    assert cfg["mode"] == "probe" and cfg["environment"] == "local"
    assert "task-construction-auditor" in cfg["presets"]
