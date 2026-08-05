import json

import pytest

from programsmith.fsm import Stage
from programsmith.manifest import Manifest, SourceInfo
from programsmith.source_reaudit import apply_reaudit, audit_source_rejection, build_reaudit_plan
from programsmith.state import RunState


def _rejected(runs, key: str, readme: str, *, matrix_reason: str | None = None):
    run_dir = runs / key
    source = run_dir / "source"
    source.mkdir(parents=True)
    (source / "main.go").write_text("package main\nfunc main() {}\n")
    (source / "go.mod").write_text("module example.com/tool\n")
    (source / "README.md").write_text(readme)
    manifest = Manifest(run_id=key, task_identity=f"src:{key}", slug=key, pipeline_mode="draft")
    manifest.source = SourceInfo(
        repo=f"owner/{key}", pinned_sha="abc", primary_language="Go", size_loc=2_000,
        clone_path=str(source),
    )
    manifest.sweeps["task_matrix"] = {"attempts": 1}
    manifest.save(run_dir)
    state = RunState.start(key, f"src:{key}", key)
    state.advance("pass")
    state.advance("none_selected")
    state.save(run_dir)
    if matrix_reason is not None:
        (run_dir / "task_matrix.json").write_text(json.dumps({
            "source_ref": f"owner/{key}@abc", "candidates": [],
            "no_candidate_reason": matrix_reason,
        }))
    (run_dir / "drive.json").write_text(json.dumps({"halted": "terminal"}))
    return run_dir


def test_reaudit_plan_only_retries_high_confidence_offline_sources(tmp_path):
    runs = tmp_path / "runs"
    strong = _rejected(runs, "converter", "# Converter\nReads CSV from stdin, filters rows, emits JSON.\n")
    _rejected(runs, "generic", "# CLI\nA delightful terminal experience.\n")
    item = audit_source_rejection(strong)
    assert item and item["decision"] == "retry" and item["review_score"] >= 5
    plan = build_reaudit_plan(runs)
    assert [entry["key"] for entry in plan if entry["decision"] == "retry"] == ["converter"]


def test_reaudit_does_not_overturn_prior_semantic_hard_mismatch_from_keywords(tmp_path):
    run_dir = _rejected(
        tmp_path / "runs",
        "library",
        "# Parser SDK\nA JSON parser library with search and validation helpers.\n",
        matrix_reason="a library/framework with no standalone executable",
    )
    item = audit_source_rejection(run_dir)
    assert item and item["decision"] == "keep_rejected" and item["hard_mismatch"] is True


def test_apply_reaudit_is_reversible_and_resets_only_source_decision(tmp_path):
    run_dir = _rejected(
        tmp_path / "runs", "converter", "# Converter\nReads CSV from stdin, filters rows, emits JSON.\n",
        matrix_reason="too small for the 5 kLOC difficulty band",
    )
    apply_reaudit(run_dir)
    state = RunState.load(run_dir)
    manifest = Manifest.load(run_dir)
    assert state.current_stage is Stage.TASK_MATRIX and state.status == "in_progress"
    assert state.history[-1].stage is Stage.INGEST_LOCK
    assert not (run_dir / "task_matrix.json").exists()
    assert (run_dir / "task_matrix.pre-source-reaudit.json").exists()
    assert "task_matrix" not in manifest.sweeps
    with pytest.raises(ValueError):
        apply_reaudit(run_dir)
