"""Offline tests for the good-failure gate (ADR-0041): pure label gate, deep-audit cell
(injected runner — never a real LLM), and the deterministic audit gate over validated fields."""

import json

import pytest

from programsmith.goodfail import (
    GoodFailReport,
    TrialAudit,
    audit_gate,
    build_audit_prompt,
    deep_audit,
    label_gate,
)

# ---- label_gate (pure) ---------------------------------------------------------------


def test_label_gate_all_good_failures_keeps_hard():
    r = label_gate(["GOOD_FAILURE", "GOOD_FAILURE", "GOOD_FAILURE"])
    assert r["verdict"] == "keep_hard" and "genuine headroom" in r["reason"]
    assert r["counts"] == {"GOOD_FAILURE": 3}


def test_label_gate_any_bad_failure_eases():
    # A single BAD_FAILURE (task/env defect) outranks any number of good failures — the zero is FAKE
    r = label_gate(["GOOD_FAILURE", "BAD_FAILURE", "GOOD_FAILURE"])
    assert r["verdict"] == "ease" and "BAD_FAILURE" in r["reason"]


def test_label_gate_inconclusive_cases():
    assert label_gate([])["verdict"] == "inconclusive"                       # no labels at all
    assert label_gate(["HARNESS_ERROR"])["verdict"] == "inconclusive"        # infra noise only
    # success labels contradict a zero-pass band → never auto-keep on them
    assert label_gate(["GOOD_FAILURE", "GOOD_SUCCESS"])["verdict"] == "inconclusive"


def test_label_gate_is_case_insensitive():
    assert label_gate(["good_failure"])["verdict"] == "keep_hard"


# ---- audit_gate (pure, over the validated schema) -------------------------------------


def _report(*modes):
    return GoodFailReport(
        trials=[TrialAudit(trial_id=f"t{i}", failure_mode=m, evidence="e") for i, m in enumerate(modes)],
        summary="s")


def test_audit_gate_all_headroom_keeps_hard():
    r = audit_gate(_report("capability_headroom", "capability_headroom"))
    assert r["verdict"] == "keep_hard"


def test_audit_gate_any_design_failure_eases():
    # task_design_failure outranks environment_failure (an ease patch re-runs sanity anyway)
    r = audit_gate(_report("capability_headroom", "task_design_failure", "environment_failure"))
    assert r["verdict"] == "ease" and "task_design_failure" in r["reason"]


def test_audit_gate_env_failure_revises():
    r = audit_gate(_report("capability_headroom", "environment_failure"))
    assert r["verdict"] == "revise" and "environment_failure" in r["reason"]


def test_report_requires_at_least_one_trial():
    with pytest.raises(Exception):
        GoodFailReport(trials=[], summary="empty")   # min_length=1 — the cell must audit something


# ---- deep_audit (one-shot cell, injected runner) ---------------------------------------


def _pull_dir(tmp_path):
    """A pulled-sweep layout: two trial dirs with trajectory files + verifier diff artifacts."""
    for i in range(2):
        d = tmp_path / "trials" / f"t{i}"
        d.mkdir(parents=True)
        (d / "result.json").write_text(json.dumps({"agent_info": {"name": "mini-swe-agent"}}))
        (d / "trajectory.json").write_text("\n".join(f"step {n}: trying" for n in range(300)))
        (d / "diff_case-alpha.txt").write_text("expected X got Y")
    (tmp_path / "trials" / "t0" / "diff_case-beta.txt").write_text("expected A got B")
    return tmp_path


def test_deep_audit_prompt_carries_tails_and_diff_names(tmp_path):
    pull = _pull_dir(tmp_path)
    prompt = build_audit_prompt(pull, [{"trial_id": "t0", "label": "HARNESS_ERROR"}])
    assert "diff_case-alpha.txt" in prompt and "diff_case-beta.txt" in prompt   # NAMES included
    assert "step 299: trying" in prompt                # the TAIL of the trajectory is present…
    assert "step 50: trying" not in prompt             # …but not the whole transcript (last ~200)
    assert "t0: label=HARNESS_ERROR" in prompt


def test_deep_audit_validates_via_run_cell(tmp_path):
    pull = _pull_dir(tmp_path)
    payload = {"trials": [{"trial_id": "t0", "failure_mode": "capability_headroom",
                           "evidence": "real partial progress"}],
               "summary": "genuinely hard"}
    report = deep_audit(pull, [{"trial_id": "t0", "label": "HARNESS_ERROR"}],
                        runner=lambda _p: json.dumps(payload))
    assert isinstance(report, GoodFailReport)
    assert report.trials[0].failure_mode == "capability_headroom"
    assert audit_gate(report)["verdict"] == "keep_hard"


def test_deep_audit_rejects_out_of_vocab_failure_mode(tmp_path):
    from programsmith.llm import CellError
    bad = {"trials": [{"trial_id": "t0", "failure_mode": "shrug", "evidence": "?"}], "summary": "x"}
    with pytest.raises(CellError):
        deep_audit(_pull_dir(tmp_path), [], runner=lambda _p: json.dumps(bad))


def test_trajectory_tails_prefer_local_engine_records(tmp_path):
    """The local sweep engine records each transcript at <trial>/<task>/.trajectory — a
    SUFFIX-LESS dotfile inside a full copy of the task workspace. The generic suffix walk would
    miss it entirely (Path('.trajectory').suffix == '') and instead fill the prompt budget with
    the workspace's own .md/.json fixtures — the audit would read the task tree, never the
    transcripts (the tengo blind-audit shape). When canonical records exist, use ONLY them."""
    from programsmith.goodfail import _trajectory_tails
    d = tmp_path / "trials" / "claude-code-0" / "tengo"
    d.mkdir(parents=True)
    (d / ".trajectory").write_text("\n".join(f"agent step {n}" for n in range(250)))
    (d / "instruction.md").write_text("TASK TREE NOISE — not a transcript")
    (d / "task.toml").write_text("[task]\n")
    (tmp_path / "trials" / "claude-code-0" / "result.json").write_text("{}")
    tails = _trajectory_tails(tmp_path)
    labels = [l for l, _ in tails]
    assert labels == ["trials/claude-code-0/tengo/.trajectory"]
    assert "agent step 249" in tails[0][1]
    assert all("NOISE" not in t for _, t in tails)

    # imported/remote pull trees without .trajectory records keep the generic suffix walk
    generic = tmp_path / "generic"
    (generic / "t0").mkdir(parents=True)
    (generic / "t0" / "trajectory.json").write_text("remote transcript tail")
    tails2 = _trajectory_tails(generic)
    assert [l for l, _ in tails2] == ["t0/trajectory.json"]


def test_audit_prompt_char_caps_tails_so_every_trial_is_seen(tmp_path):
    """keep_hard requires the audit to have SEEN every failed trial, so every tail must land in
    the prompt. stream-json transcripts pack a whole event per line — 200 'lines' can be 300KB —
    and a line-only bound let the FIRST tail eat the whole budget, eliding the rest (the tengo
    blind audit: the model honestly reported zero evidence and refused headroom). Each tail gets
    an equal char share; the END of the tail (the give-up shape) survives the cap."""
    from programsmith.goodfail import _MAX_PROMPT_CHARS, build_audit_prompt
    for i in range(3):
        d = tmp_path / "trials" / f"claude-code-{i}" / "task"
        d.mkdir(parents=True)
        # one enormous line (stream-json shape) ending in a distinctive give-up marker
        (d / ".trajectory").write_text("x" * 120_000 + f" FINAL-STATE-trial-{i}")
    prompt = build_audit_prompt(tmp_path, [{"trial_id": f"claude-code-{i}", "label": "GOOD_FAILURE"}
                                           for i in range(3)])
    assert len(prompt) <= _MAX_PROMPT_CHARS + 200
    assert prompt.count("TRAJECTORY TAIL:") == 3          # every trial present, none elided
    assert "elided" not in prompt
    for i in range(3):
        assert f"FINAL-STATE-trial-{i}" in prompt         # the tail END survives the char cap
