"""Offline tests for the SYNTHESIZE plan cell (runner injected): the harden|ease|revise move
vocabulary (ADR-0040), the expanded from_stage sources, the ProgramBench lever menus, identity
preservation, and the fixture-recapture requirement in the apply prompt."""

import json

import pytest

from programsmith.cells.synthesize import (
    SynthesizeOutput,
    _SYSTEM,
    apply_prompt,
    build_prompt,
    synthesize_plan,
)
from programsmith.llm import CellError

_GOOD = {
    "task_dir": "/runs/just/task/implement-just",
    "move": "harden",
    "from_stage": "QA_PROBE",
    "reason": "probe found an oracle-copy shortcut (catalog H2)",
    "patch": [{"file": "tests/test.sh",
               "change": "tighten the Tier-2 encoded-oracle scan; add the missing decoder offset",
               "regenerated": False}],
    "addresses": [{"kind": "reward-hack", "catalog_id": "H2", "detail": "encoded-oracle smuggle"}],
    "preserves_identity": True,
}

_EASE = {
    "task_dir": "/runs/just/task/implement-just",
    "move": "ease",
    "from_stage": "FULL_SWEEP",
    "reason": "frontier 0/3 with a universal blocker: exact-error-text cases",
    "patch": [{"file": "tests/testsuite/cases.json",
               "change": "drop the 4 exact-error-message cases every trial fails identically",
               "regenerated": False}],
    "addresses": [{"kind": "fairness",
                   "detail": "error-text byte-exactness is not reproducible by any honest reimpl"}],
    "preserves_identity": True,
}


def _envelope(obj: dict) -> str:
    return json.dumps({"type": "result", "result": json.dumps(obj)})


# ---- schema ---------------------------------------------------------------------------

def test_synthesize_plan_validates_harden_output():
    out = synthesize_plan("/t", "harden", "QA_PROBE", "H2", runner=lambda _p: _envelope(_GOOD))
    assert isinstance(out, SynthesizeOutput)
    assert out.move == "harden" and out.addresses[0].catalog_id == "H2"
    assert out.preserves_identity is True
    assert all(not e.regenerated for e in out.patch)


def test_synthesize_plan_accepts_ease_move_from_full_sweep():
    out = synthesize_plan("/t", "ease", "FULL_SWEEP", "blockers",
                          runner=lambda _p: _envelope(_EASE))
    assert out.move == "ease" and out.from_stage == "FULL_SWEEP"


@pytest.mark.parametrize("stage", ["CALIBRATE", "QA_PROBE", "QA_GATE", "FULL_SWEEP",
                                   "SANITY", "STATIC_CI"])
def test_from_stage_covers_every_synthesize_source(stage):
    # every FSM edge that lands on SYNTHESIZE must be expressible (orchestrator._synth_trigger)
    out = SynthesizeOutput.model_validate({**_GOOD, "from_stage": stage, "move": "revise"})
    assert out.from_stage == stage


def test_synthesize_plan_rejects_empty_patch():
    bad = json.loads(json.dumps(_GOOD)); bad["patch"] = []
    with pytest.raises(CellError):
        synthesize_plan("/t", "harden", "QA_PROBE", "x", runner=lambda _p: _envelope(bad))


def test_synthesize_plan_rejects_bad_move():
    bad = json.loads(json.dumps(_GOOD)); bad["move"] = "rewrite"
    with pytest.raises(CellError):
        synthesize_plan("/t", "harden", "QA_PROBE", "x", runner=lambda _p: _envelope(bad))


# ---- prompt content (ProgramBench lever menus, farm §8) ---------------------------------

def test_build_prompt_carries_trigger_and_findings():
    p = build_prompt("/t", "ease", "FULL_SWEEP", "universal blocker",
                     findings=["diff_err-text-1.txt failed identically in all 3 trials"])
    assert "ease" in p and "FULL_SWEEP" in p and "diff_err-text-1" in p


def test_system_prompt_is_programbench_with_frozen_identity():
    assert "ProgramBench-style task" in _SYSTEM
    assert "source repo x tool x flag surface" in _SYSTEM
    # the retired rewrite-port identity must be gone
    assert "target-language" not in _SYSTEM


def test_harden_levers_quote_the_farm_playbook():
    assert "15-30 per family" in _SYSTEM and "NEVER hand-written" in _SYSTEM
    assert "10+ adversarial error-path cases" in _SYSTEM
    assert "every output mode" in _SYSTEM
    assert "depth, not new scope" in _SYSTEM


def test_ease_levers_quote_the_farm_playbook():
    assert "UNIVERSAL BLOCKER" in _SYSTEM and "diff_<case>.txt" in _SYSTEM
    assert "ROUNDTRIP" in _SYSTEM                      # encoder grading softener
    assert "exact-error-message-text" in _SYSTEM       # interpreter blocker softener
    assert "18000s" in _SYSTEM and "never lower" in _SYSTEM
    assert "remove blockers, not families" in _SYSTEM


def test_revise_levers_fix_harness_never_count_ambiguity():
    assert "Never count verifier ambiguity as difficulty" in _SYSTEM
    assert "check-task-absolute-path" in _SYSTEM


def test_system_requires_fixture_recapture_on_case_edits():
    assert "build_test_fixtures" in _SYSTEM and "determinism check" in _SYSTEM


# ---- agentic apply (session + validator injected, offline) ---------------------------

def test_apply_prompt_lists_edits_targets_and_recapture_contract():
    plan = SynthesizeOutput.model_validate(_EASE)
    p = apply_prompt(plan)
    assert "SURGICAL patch" in p and "ProgramBench task" in p
    assert "tests/testsuite/cases.json" in p and "fairness" in p
    assert "do NOT regenerate" in p
    # patched case suites MUST re-run fixture capture + determinism check (DESIGN §6.6)
    assert "build_test_fixtures" in p and "check_determinism" in p
    assert "NEVER hand-edit" in p


def test_apply_drives_loop_to_green(tmp_path):
    from programsmith.cells.agentic import ValidationState
    from programsmith.cells.synthesize import apply
    plan = SynthesizeOutput.model_validate({**_GOOD, "task_dir": str(tmp_path)})
    seen = []
    res = apply(plan, str(tmp_path),
                session=lambda prompt, td: seen.append(prompt) or "patched",
                validator=lambda _d: ValidationState(True, True), max_iters=2)
    assert res.success and res.iterations == 1
    assert "SURGICAL patch" in seen[0]


def test_apply_reports_incomplete_when_validation_stays_red(tmp_path):
    from programsmith.cells.agentic import ValidationState
    from programsmith.cells.synthesize import apply
    plan = SynthesizeOutput.model_validate({**_GOOD, "task_dir": str(tmp_path)})
    res = apply(plan, str(tmp_path), session=lambda p, td: "tried",
                validator=lambda _d: ValidationState(False, True), max_iters=2)
    assert not res.success and "did not reach" in res.reason
