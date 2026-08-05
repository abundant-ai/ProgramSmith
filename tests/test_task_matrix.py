"""Offline tests for the TASK MATRIX cell (ProgramBench genre, ADR-0038): schema validation,
the farm selection-criteria prompt, deterministic auto-pick ordering (ADR-0039), and
apply_selection's identity recompute. Runner injected."""

import json

import pytest

from programsmith.cells.task_matrix import (
    TaskCandidate,
    TaskMatrixOutput,
    apply_selection,
    build_prompt,
    pick_candidate,
    propose,
)
from programsmith.llm import CellError
from programsmith.manifest import Manifest, SourceInfo, programbench_task_identity

_CAND = {
    "tool_name": "just",
    "binary_name": "just",
    "upstream_language": "rust",
    "flag_surface": "recipe running + --list --summary --show --dump --evaluate --fmt --unstable",
    "case_families": ["recipe-listing", "dependencies", "variables", "conditionals",
                      "string-functions", "error-paths"],
    "est_kloc": 24,
    "stdin_friendly": False,
    "needs_files_dir": True,
    "deterministic_output": True,
    "expected_difficulty": "hard",
    "expert_hours": 38,
    "recommendation": "recommended",
    "rationale": "rich flag surface, deterministic output, cwd-driven justfile fixture trees",
    "basis_ref": "exported implement-just (programbench-farm)",
}

_GOOD = {
    "source_ref": "casey/just@abc123def0",
    "candidates": [_CAND],
    "source_evidence": [
        "[README.md] documents recipes, variables, and formatting",
        "[src/main.rs] defines the just executable entrypoint",
    ],
}


def _manifest(*, pipeline_mode: str = "full") -> Manifest:
    m = Manifest(run_id="r", task_identity="src:abc")
    m.pipeline_mode = pipeline_mode
    m.source = SourceInfo(
        repo="casey/just", pinned_sha="abc123def0", primary_language="Rust",
        license="CC0-1.0", license_class="permissive", build_systems=["cargo"], size_loc=24000,
    )
    return m


def _envelope(obj: dict) -> str:
    return json.dumps({"type": "result", "result": json.dumps(obj)})


# ---- prompt content -------------------------------------------------------------------

def test_build_prompt_includes_source_and_farm_criteria():
    p = build_prompt(_manifest())
    assert "casey/just" in p
    # farm §2 selection criteria (quote-level fidelity)
    assert ">=5 kLOC" in p and "5-80 kLOC" in p and "12-60 expert-hours" in p
    assert ">=10 subcommands or >=20 flags" in p and "100-300 distinct golden cases" in p
    assert "real CLI utility, not a library" in p
    assert "under 30 minutes" in p
    assert "basis_ref" in p


def test_build_prompt_difficulty_bar_is_opus_window_not_frontier_fail():
    p = build_prompt(_manifest())
    assert "Opus 4.8 pass@1 in [1/3, 2/3]" in p and "5h (18000s)" in p
    assert "do NOT aim for frontier-must-fail" in p and "30-minute toys" in p
    # the old rewrite-port identity/difficulty bar must be gone
    assert "faithful PORT" not in p and "SHOULD FAIL" not in p


def test_build_prompt_draft_accepts_small_simple_deterministic_tasks():
    p = build_prompt(_manifest(pipeline_mode="draft"))
    assert "DRAFT run" in p and "NO sweeps or difficulty" in p
    assert "Do NOT reject a source merely because the task is small" in p
    assert "At least 500 LOC" in p and "2-20" in p
    assert "Simple tasks are wanted" in p
    assert "Opus 4.8 pass@1" not in p


def test_build_prompt_draft_rejects_nondeterministic_core_and_auxiliary_only_surface():
    p = build_prompt(_manifest(pipeline_mode="draft"))
    assert "meaningful deterministic functional" in p
    assert "help, version, completion" in p
    assert "wall-clock timestamps" in p
    assert "return an empty candidates list now" in p
    assert "AFFIRMATIVE evidence" in p
    assert "err toward proposing" not in p


def test_build_prompt_one_repo_one_task_and_max_not_quota():
    p = build_prompt(_manifest())
    assert "One repo usually yields ONE task" in p
    assert "MAXIMUM, not a quota" in p


def test_build_prompt_includes_operator_brief_only_when_present():
    m = _manifest()
    assert "OPERATOR BRIEF" not in build_prompt(m)
    m.task_brief = "Scope to the jq-compatible filter surface only."
    p = build_prompt(m)
    assert "OPERATOR BRIEF" in p and "jq-compatible filter surface" in p


def test_build_prompt_includes_path_labelled_locked_checkout_evidence(tmp_path):
    root = tmp_path / "just"
    (root / "src").mkdir(parents=True)
    (root / "README.md").write_text("# just\nRuns recipes and formats justfiles.\n")
    (root / "Cargo.toml").write_text('[package]\nname = "just"\n')
    (root / "src" / "main.rs").write_text("fn main() { run_recipes(); }\n")
    manifest = _manifest()
    manifest.source.clone_path = str(root)
    p = build_prompt(manifest)
    assert "SOURCE DOSSIER" in p
    assert '"path": "README.md"' in p and "Runs recipes" in p
    assert '"path": "src/main.rs"' in p
    assert "source_evidence" in p and "lack of context is not evidence" in p


# ---- schema validation ----------------------------------------------------------------

def test_propose_validates_good_output():
    out = propose(_manifest(), runner=lambda _p: _envelope(_GOOD))
    assert isinstance(out, TaskMatrixOutput)
    c = out.candidates[0]
    assert c.tool_name == "just" and c.upstream_language == "rust"
    assert c.needs_files_dir and len(c.case_families) >= 5
    assert c.basis_ref
    assert out.profile == "full"


def test_propose_stamps_draft_profile_even_if_model_omits_it():
    out = propose(_manifest(pipeline_mode="draft"), runner=lambda _p: _envelope(_GOOD))
    assert out.profile == "draft"


def test_propose_rejects_unknown_upstream_language():
    bad = json.loads(json.dumps(_GOOD))
    bad["candidates"][0]["upstream_language"] = "fortran"  # not a ProgramBench toolchain
    with pytest.raises(CellError):
        propose(_manifest(), runner=lambda _p: _envelope(bad))


def test_propose_rejects_too_few_case_families():
    bad = json.loads(json.dumps(_GOOD))
    bad["candidates"][0]["case_families"] = ["a", "b", "c", "d"]  # min 5
    with pytest.raises(CellError):
        propose(_manifest(), runner=lambda _p: _envelope(bad))


def test_propose_rejects_empty_basis_ref():
    bad = json.loads(json.dumps(_GOOD))
    bad["candidates"][0]["basis_ref"] = ""
    with pytest.raises(CellError):
        propose(_manifest(), runner=lambda _p: _envelope(bad))


def test_propose_rejects_bad_expected_difficulty():
    bad = json.loads(json.dumps(_GOOD))
    bad["candidates"][0]["expected_difficulty"] = "trivial"  # not a ProgramBench tier
    with pytest.raises(CellError):
        propose(_manifest(), runner=lambda _p: _envelope(bad))


def test_propose_accepts_empty_candidates_with_reason():
    """An unsuitable repo (a library, monorepo, or non-deterministic tool) is a VALID empty answer,
    NOT a schema failure — the pytorch/pytorch block was caused by min_length=1 forcing the cell to
    fabricate a candidate or fail validation. Empty + no_candidate_reason drops the run cleanly."""
    out = propose(_manifest(), runner=lambda _p: _envelope(
        {"source_ref": "pytorch/pytorch@abc", "candidates": [],
         "no_candidate_reason": "a ~2M-LOC ML library, not a single deterministic CLI tool",
         "source_evidence": [
             "[README.md] describes an embeddable tensor library",
             "[file overview] contains no executable command tree",
         ]}))
    assert out.candidates == [] and "library" in (out.no_candidate_reason or "")
    assert pick_candidate(out) is None                     # → orchestrator drops with the reason


def test_propose_requires_evidence_for_fresh_decisions():
    without_evidence = {"source_ref": "o/n@abc", "candidates": [_CAND]}
    with pytest.raises(CellError):
        propose(_manifest(), runner=lambda _p: _envelope(without_evidence))


def test_old_persisted_matrix_without_evidence_remains_loadable():
    old = TaskMatrixOutput.model_validate({"source_ref": "o/n@abc", "candidates": [_CAND]})
    assert old.source_evidence == [] and old.candidates[0].tool_name == "just"


# ---- auto-pick (ADR-0039) -------------------------------------------------------------

def _cand(rec: str, tool: str = "just") -> dict:
    return {**_CAND, "recommendation": rec, "tool_name": tool}


def test_pick_candidate_prefers_first_recommended():
    out = TaskMatrixOutput.model_validate(
        {"source_ref": "s", "candidates": [_cand("marginal"), _cand("viable"),
                                           _cand("recommended"), _cand("recommended", "j2")]})
    assert pick_candidate(out) == 2


def test_pick_candidate_falls_back_to_first_viable():
    out = TaskMatrixOutput.model_validate(
        {"source_ref": "s", "candidates": [_cand("marginal"), _cand("viable"), _cand("viable", "j2")]})
    assert pick_candidate(out) == 1


def test_pick_candidate_accepts_marginal_when_only_option():
    """Farm posture (ADR-0039): TASK_MATRIX is a coarse prefilter, so a lone 'marginal' candidate
    IS picked (the downstream deterministic gates enforce real quality). Since TaskMatrixOutput
    requires >=1 candidate, this makes auto-pick effectively never drop at the cell level — the
    only drop path (empty candidate list) lives in the orchestrator over the raw list."""
    out = TaskMatrixOutput.model_validate(
        {"source_ref": "s", "candidates": [_cand("marginal"), _cand("marginal", "j2")]})
    assert pick_candidate(out) == 0                         # first marginal, not a drop


# ---- apply_selection (identity recompute, ADR-0038) -------------------------------------

def test_apply_selection_sets_dimensions_and_recomputes_identity():
    m = _manifest()
    c = TaskCandidate.model_validate(_CAND)
    ident = apply_selection(m, c)
    d = m.dimensions
    assert d is not None and d.tool_name == "just" and d.binary_name == "just"
    assert d.upstream_language == "rust" and d.flag_surface == _CAND["flag_surface"]
    assert d.case_families == _CAND["case_families"]
    assert d.needs_files_dir is True and d.stdin_friendly is False
    assert d.deterministic_output is True
    assert d.expected_difficulty == "hard" and d.expert_hours == 38
    # identity = sha256(repo@sha | programbench | tool | flag_surface), the dedup hash
    assert ident == m.task_identity == programbench_task_identity(
        "casey/just", "abc123def0", "just", _CAND["flag_surface"])
    assert ident.startswith("task:")


def test_apply_selection_identity_varies_with_flag_surface():
    m1, m2 = _manifest(), _manifest()
    c1 = TaskCandidate.model_validate(_CAND)
    c2 = TaskCandidate.model_validate({**_CAND, "flag_surface": "core recipe running only"})
    assert apply_selection(m1, c1) != apply_selection(m2, c2)


def test_apply_selection_without_source_keeps_identity():
    m = Manifest(run_id="r", task_identity="src:keepme")
    ident = apply_selection(m, TaskCandidate.model_validate(_CAND))
    assert ident == "src:keepme" and m.dimensions.tool_name == "just"


# ---- legacy manifest back-compat -------------------------------------------------------

def test_legacy_rewrite_port_manifest_still_loads():
    legacy = {
        "run_id": "r", "task_identity": "task:old",
        "dimensions": {"target_language": "Rust", "scope_unit": "whole-library",
                       "verifier_mechanism": "golden-io", "objective": "equivalence"},
    }
    m = Manifest.model_validate(legacy)
    assert m.dimensions.target_language == "Rust"
    assert m.dimensions.tool_name is None and m.dimensions.case_families == []
    # and a new-style dump round-trips
    m2 = Manifest.model_validate_json(m.model_dump_json())
    assert m2.dimensions.scope_unit == "whole-library"
