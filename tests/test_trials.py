"""Offline tests for trials.py — the backend-neutral trial-record schema + band math (the pure
core every sweep reader/gate routes on)."""

import json

import pytest

from programsmith.trials import (
    SweepAgent,
    _canon_agent,
    _is_frontier_trial,
    build_sweep_config,
    dual_family_band,
    experiment_name,
    extract_auditor_verdict,
    frontier_trials,
    load_trials,
    normalize_trial,
    pass_at_1,
    probe_task_dirname,
    read_pulled_analyses,
    read_pulled_trials,
    read_trials,
)


# ---- naming (deterministic, timestamp-free) --------------------------------------------------
def test_experiment_name_generations_and_retries():
    assert experiment_name("demo", "difficulty") == "programsmith-demo-difficulty"
    assert experiment_name("demo", "full", generation=2) == "programsmith-demo-full-v2"
    assert experiment_name("demo", "full", attempt=1) == "programsmith-demo-full-retry1"
    assert experiment_name("demo", "full", generation=1, attempt=2) == "programsmith-demo-full-v1-retry2"


def test_probe_task_dirname():
    assert probe_task_dirname("demo") == "demo-audit"
    assert probe_task_dirname("demo", generation=3) == "demo-audit-v3"


def test_build_sweep_config_omits_empty_task_filter():
    cfg = build_sweep_config(None, [SweepAgent("oracle", "default", 1),
                                    SweepAgent("claude-code", "anthropic/claude-opus-4-8", 3)])
    assert "task_names" not in cfg            # a single-task launch must not filter itself out
    assert cfg["agents"][1] == {"name": "claude-code", "model_name": "anthropic/claude-opus-4-8",
                                "n_trials": 3}
    assert build_sweep_config(["t1"], [])["task_names"] == ["t1"]


# ---- record normalization --------------------------------------------------------------------
def test_normalize_trial_nested_shape():
    rec = normalize_trial({"agent_info": {"name": "claude-code-api-key-no-search",
                                          "model_info": {"name": "claude-opus-4-8"}},
                           "verifier_result": {"rewards": {"reward": 1.0}}})
    assert rec == {"agent": "claude-code", "model": "claude-opus-4-8", "reward": 1.0,
                   "is_probe": False, "status": "completed"}


def test_normalize_trial_nested_errored():
    rec = normalize_trial({"agent_info": {"name": "codex"}, "exception_info": {"e": "boom"}})
    assert rec["reward"] is None and rec["status"] == "errored"


def test_normalize_trial_flat_shape_with_grade_fallback():
    rec = normalize_trial({"agent": "nop", "status": "completed"}, {"reward": 0})
    assert rec["agent"] == "nop" and rec["reward"] == 0
    # a fractional partial_score is NOT a pass — only binary values are accepted as reward
    rec2 = normalize_trial({"agent": "claude-code"}, {"partial_score": 0.66})
    assert rec2["reward"] is None


def test_canon_agent_folds_registered_names_to_catalog_keys():
    assert _canon_agent("mini-swe-agent") == "mini-swe"
    assert _canon_agent("claude-code-api-key-no-search") == "claude-code"
    assert _canon_agent("mini-swe-agent-api-key-no-search") == "mini-swe"
    assert _canon_agent("codex") == "codex"
    assert _canon_agent(None) == "?"


# ---- pulled-artifacts readers ------------------------------------------------------------------
def _seed_trials(root, records):
    for i, rec in enumerate(records):
        d = root / "trials" / f"t{i}"
        d.mkdir(parents=True)
        (d / "result.json").write_text(json.dumps(rec))


def test_read_pulled_trials_skips_job_summaries(tmp_path):
    _seed_trials(tmp_path, [
        {"agent": "oracle", "model": "default", "reward": 1},
        {"stats": {"n": 3}, "evals": []},                       # a job SUMMARY, not a trial
        {"agent_info": {"name": "claude-code", "model_info": {"name": "opus"}},
         "verifier_result": {"rewards": {"reward": 0.0}}},
    ])
    trials = read_pulled_trials(tmp_path)
    assert len(trials) == 2                                     # summary skipped, not double-counted
    assert {t["agent"] for t in trials} == {"oracle", "claude-code"}


def test_read_pulled_analyses_filters_agent_families(tmp_path):
    _seed_trials(tmp_path, [
        {"agent": "codex", "trial_id": "a", "analysis": {"classification": "GOOD_FAILURE"}},
        {"agent": "claude-code", "trial_id": "b", "analysis": {"classification": "BAD_FAILURE"}},
        {"agent": "codex", "trial_id": "c"},                    # no label → omitted, never invented
    ])
    out = read_pulled_analyses(tmp_path, agents=("codex",))
    assert out == [{"trial_id": "a", "label": "GOOD_FAILURE"}]


def test_load_trials_dispatches_dir_file_and_dict(tmp_path):
    _seed_trials(tmp_path, [{"agent": "oracle", "reward": 1}])
    assert load_trials(tmp_path)[0]["agent"] == "oracle"        # a directory of artifacts
    f = tmp_path / "status.json"
    f.write_text(json.dumps({"trials": [{"agent": "nop", "reward": 0}]}))
    assert load_trials(str(f))[0]["agent"] == "nop"             # a status-payload file
    assert read_trials({"trials": [{"agent": "x"}]})[0]["agent"] == "x"   # in-memory


# ---- auditor-verdict scrape ---------------------------------------------------------------------
def test_extract_auditor_verdict_takes_last_and_counts_blockers(tmp_path):
    d = tmp_path / "trials" / "t0"
    d.mkdir(parents=True)
    (d / "trajectory.json").write_text(json.dumps({"steps": [
        {"text": 'schema: "verdict": "SOLVABLE_AS_WRITTEN|SOLVABLE_ONLY_BY_GUESSING|UNSOLVABLE"'},
        {"text": '{"verdict": "SOLVABLE_ONLY_BY_GUESSING", "findings": [{"severity": "blocker"}]}'},
        {"text": '{"verdict": "SOLVABLE_AS_WRITTEN", "findings": []}'},   # the FINAL report wins
    ]}))
    info = extract_auditor_verdict(tmp_path)
    assert info["found"] and info["verdict"] == "SOLVABLE_AS_WRITTEN"
    assert info["blockers"] == 1


def test_extract_auditor_verdict_absent_is_not_invented(tmp_path):
    (tmp_path / "log.txt").write_text("no verdict anywhere")
    info = extract_auditor_verdict(tmp_path)
    assert info == {"verdict": None, "blockers": 0, "found": False}


def test_extract_auditor_verdict_survives_double_json_encoding(tmp_path):
    """Regression (tengo probe false-block): a stream-json trajectory stored inside result.json
    escapes the auditor's report TWICE (\\\\\" at depth 2) — the scrape must find the verdict at
    any escape depth, or every clean probe parks the run for human review."""
    d = tmp_path / "trials" / "t0"
    d.mkdir(parents=True)
    report = '{"verdict": "SOLVABLE_AS_WRITTEN", "findings": [{"severity": "blocker"}]}'
    stream_event = json.dumps({"type": "assistant", "text": report})        # depth 1
    (d / "result.json").write_text(json.dumps({"trajectory": stream_event}))  # depth 2
    info = extract_auditor_verdict(tmp_path)
    assert info["found"] and info["verdict"] == "SOLVABLE_AS_WRITTEN" and info["blockers"] == 1


# ---- frontier filtering + band math -------------------------------------------------------------
def test_frontier_excludes_baselines_and_flagged_probes():
    trials = [
        {"agent": "oracle", "reward": 1},
        {"agent": "nop", "reward": 0},
        {"agent": "claude-code", "model": "anthropic/claude-haiku-4-5", "reward": 1},  # smoke COUNTS
        {"agent": "claude-code", "model": "anthropic/claude-haiku-4-5", "reward": 0, "is_probe": True},
    ]
    assert not _is_frontier_trial(trials[0]) and not _is_frontier_trial(trials[3])
    front = frontier_trials(trials)
    assert len(front) == 1 and front[0]["reward"] == 1
    # a configured cheap smoke model (e.g. Haiku) is a REAL measurement — only the is_probe flag
    # excludes a trial (there is no model-name probe heuristic; the local engine flags explicitly)
    assert _is_frontier_trial(trials[2])


def test_pass_at_1_groups_and_aggregate():
    trials = [
        {"agent": "oracle", "model": "default", "reward": 1},
        {"agent": "nop", "model": "default", "reward": 0},
        {"agent": "claude-code", "model": "opus", "reward": 1},
        {"agent": "claude-code", "model": "opus", "reward": 1},
        {"agent": "claude-code", "model": "opus", "reward": 0},
        {"agent": "codex", "model": "gpt", "reward": None},     # errored → excluded from n
        {"agent": "codex", "model": "gpt", "reward": 0},
    ]
    pa = pass_at_1(trials)
    assert pa["groups"]["claude-code@opus"]["pass_at_1"] == pytest.approx(2 / 3)
    assert pa["groups"]["codex@gpt"]["n"] == 1
    assert pa["aggregate"] == pytest.approx(2 / 3)              # best measured group


def test_pass_at_1_honestly_none_when_nothing_measured():
    trials = [{"agent": "oracle", "reward": 1}, {"agent": "nop", "reward": 0},
              {"agent": "claude-code", "reward": 0, "is_probe": True}]
    pa = pass_at_1(trials)
    assert pa["groups"] == {} and pa["aggregate"] is None       # never a band over a probe's 0.0


def test_dual_family_band_aggregate_and_fairness():
    trials = [
        {"agent": "claude-code", "model": "opus", "reward": 1},
        {"agent": "claude-code", "model": "opus", "reward": 0},
        {"agent": "codex", "model": "gpt", "reward": 0},
        {"agent": "codex", "model": "gpt", "reward": 0},
    ]
    band = dual_family_band(trials)
    assert band["claude_code"] == 0.5 and band["codex"] == 0.0
    assert band["aggregate"] == 0.5                              # solved if EITHER family can
    assert band["fairness_gap"] == 0.5
    assert band["families"] == {"claude-code": 0.5, "codex": 0.0}
    only_cc = dual_family_band(trials[:2])
    assert only_cc["codex"] is None and only_cc["fairness_gap"] is None


def test_family_band_n_families_max_pairwise_gap():
    """The generalized FULL-sweep read: one entry per measured family (any harness), aggregate =
    the best family, fairness_gap = the MAX PAIRWISE spread (here |1.0 − 0.0| between claude-code
    and gemini-cli, not the adjacent |1.0 − 0.5|). One family → no fairness signal (None)."""
    from programsmith.trials import family_band
    trials = [
        {"agent": "claude-code", "model": "opus", "reward": 1},
        {"agent": "codex", "model": "gpt", "reward": 1},
        {"agent": "codex", "model": "gpt", "reward": 0},
        {"agent": "gemini-cli", "model": "gemini", "reward": 0},
        {"agent": "oracle", "model": "default", "reward": 1},    # baselines never count
        {"agent": "nop", "model": "default", "reward": 0},
    ]
    band = family_band(trials)
    assert band["families"] == {"claude-code": 1.0, "codex": 0.5, "gemini-cli": 0.0}
    assert band["aggregate"] == 1.0
    assert band["fairness_gap"] == 1.0                           # max pairwise, not adjacent
    solo = family_band(trials[:1])
    assert solo["families"] == {"claude-code": 1.0} and solo["fairness_gap"] is None
    empty = family_band([])
    assert empty["families"] == {} and empty["aggregate"] is None and empty["fairness_gap"] is None


def test_scope_trials_excludes_foreign_stage_trials():
    """`scope_trials` restricts a results read to THIS sweep's own agents (+ baselines): another
    stage's smoke trials must never stand in for the full sweep's frontier (the upstream pb10
    false-DONE). Matching is rename-tolerant — a runner may rewrite `anthropic/claude-opus-4-8`
    to a Bedrock profile id `global.anthropic.claude-opus-4-8`, and mini-swe is registered as
    mini-swe-agent."""
    from programsmith.trials import SweepAgent, scope_trials
    agents = [SweepAgent("oracle", "default", 3), SweepAgent("nop", "default", 3),
              SweepAgent("mini-swe-agent", "anthropic/claude-opus-4-8", 3)]
    trials = [
        {"agent": "oracle", "model": "nop_oracle", "reward": 1.0},
        {"agent": "nop", "model": "nop_oracle", "reward": 0.0},
        {"agent": "mini-swe", "model": "global.anthropic.claude-opus-4-8", "reward": None},
        {"agent": "mini-swe", "model": "claude-opus-4-8", "reward": 0.0},
        {"agent": "mini-swe", "model": "glm-5.2", "reward": 0.0},              # foreign smoke model
        {"agent": "claude-code", "model": "claude-opus-4-8", "reward": 1.0},   # foreign harness
    ]
    scoped = scope_trials(trials, agents)
    assert [t["agent"] for t in scoped] == ["oracle", "nop", "mini-swe", "mini-swe"]
    # and the smoke stage scopes the other way: GLM in, the frontier trials out
    smoke = scope_trials(trials, [SweepAgent("oracle", "default", 1), SweepAgent("nop", "default", 1),
                                  SweepAgent("mini-swe-agent", "zai/glm-5.2", 3)])
    assert [t["model"] for t in smoke] == ["nop_oracle", "nop_oracle", "glm-5.2"]
