"""SYNTHESIZE cell — surgical patch given diff context (not regeneration).

Triggered by `harden` / `ease` (CALIBRATE / QA_PROBE / FULL_SWEEP tuning, ADR-0040) or `revise`
(SANITY / STATIC_CI / FULL_SWEEP integrity / QA GATE). Given what failed + what the sweeps/probe
found + which reward-hack-catalog entries to close, it produces a schema-validated PATCH PLAN
(LLM, quarantined) that edits the existing ProgramBench task in place — never regenerating it,
never changing the task identity (source repo × tool × flag surface). The agentic `apply` edits
files and re-validates oracle=1/nop=0; any patch touching the case suite MUST re-capture fixtures
from the oracle + re-run the determinism check (fixtures are never hand-edited).

The harden/ease playbooks add focused cases from the oracle, remove universal blockers, and use
round-trip grading for encoders; the reward-hack catalog defines what to close. FSM: SYNTHESIZE →
rejoin STATIC_CI (tunes) or SANITY (revise), bounded by the smoke/frontier tune + revise budgets in
fsm.py.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..llm import Runner, run_cell


class PatchEdit(BaseModel):
    file: str
    change: str
    regenerated: bool = False  # must stay False — SYNTHESIZE patches, never rebuilds from scratch


class PatchTarget(BaseModel):
    kind: Literal["reward-hack", "saturation", "underspecification", "verifier-bug", "fairness"]
    catalog_id: str | None = None
    detail: str


# from_stage mirrors every FSM edge that lands on SYNTHESIZE (see orchestrator._synth_trigger):
# tunes from CALIBRATE / QA_PROBE / FULL_SWEEP, revises from SANITY / STATIC_CI / FULL_SWEEP /
# QA_GATE. move "ease" is the ADR-0040 first-class backward move (cheaper than hardening).
class SynthesizeOutput(BaseModel):
    task_dir: str
    move: Literal["harden", "ease", "revise"]
    from_stage: Literal["CALIBRATE", "QA_PROBE", "QA_GATE", "FULL_SWEEP", "SANITY", "STATIC_CI"]
    reason: str
    patch: list[PatchEdit] = Field(min_length=1)
    addresses: list[PatchTarget] = Field(min_length=1)
    preserves_identity: bool = True


_SYSTEM = """You are the SYNTHESIZE cell. You produce a SURGICAL PATCH PLAN for an existing \
ProgramBench-style task (reimplement CLI tool <T> from scratch against a sealed execute-only \
oracle binary, graded black-box by Golden-I/O cases: byte-identical stdout + exit code) — you do \
NOT regenerate the task and you must NOT change its identity (source repo x tool x flag surface \
are FROZEN). Given the trigger (harden | ease | revise), what the sweeps/probe measured, and the \
findings to close, output the MINIMAL set of edits.

If any finding names a reward-hack/exploit (a probe finding, a BAD_SUCCESS trial label, a catalog \
id), close it FIRST and STRUCTURALLY — seal the leak, tighten the anti-cheat tier, compute inside \
the verifier — never cosmetically, whatever the move is.

Lever menu per move (the ProgramBench farm playbook — pick the FEWEST levers that close the \
findings):

HARDEN (pass rate ABOVE the band — the task is too easy):
- Add cases in UNCOVERED feature families: 15-30 per family, with expected outputs captured by \
RUNNING the oracle binary through the fixture builder — NEVER hand-written.
- Add 10+ adversarial error-path cases (bad input / bad regex / bad numeric / missing arg / \
conflicting flags), not 3-5.
- Deepen the held-out surface of EXISTING families: edge semantics, boundary values, degenerate/\
empty inputs, exotic flag combinations — depth, not new scope.
- Cover every output mode the flag surface exposes.
- Tighten the verifier timeout only when there is real slack — never as the primary lever.

EASE (zero/low pass WITH task-design blockers — remove blockers, never gut semantic depth):
- Identify the UNIVERSAL BLOCKER cases from the per-case verifier diffs (diff_<case>.txt): the \
cases EVERY trial fails the same way. Remove or soften ONLY those.
- Typical blockers and their fixes: exact-error-message-text cases -> drop or soften; encoder-ish \
outputs that are inherently non-reproducible byte-for-byte -> grade by ROUNDTRIP instead (decode \
stays byte-exact); byte-exactness where upstream itself is non-reproducible -> drop those cases.
- You MAY move a few blocker cases from the graded suite into the public smoke test (they become \
learnable, not gradable).
- The agent timeout stays 18000s — never lower it; easing never raises the grading bar.
- NEVER remove whole feature families or shrink the flag surface — remove blockers, not families.

REVISE (broken verifier/env — integrity or BAD_FAILURE evidence):
- Fix THE defect the evidence names (verifier bug, env/build breakage, fixture mismatch). Never \
count verifier ambiguity as difficulty — fix the harness instead.
- If the trigger names a SANITY check, fix that check's cause; if it names a STATIC CI check, fix \
that check (e.g. `check-task-absolute-path` means instruction.md references a relative path — \
rewrite every file reference to an absolute /app/... path).

Any patch that touches the case suite (adds/removes/edits cases) MUST re-run the fixture capture \
(programsmith.programbench.fixtures.build_test_fixtures) AND the determinism check as part of the \
patch — fixtures are captured from the oracle, never edited by hand. oracle=1/nop=0 must still \
hold after the patch. Every patch edit sets regenerated=false. preserves_identity must be true."""


def build_prompt(task_dir: str, move: str, from_stage: str, reason: str,
                 findings: list[dict] | None = None) -> str:
    f = "\n".join(f"- {x}" for x in (findings or [])) or "- (none supplied)"
    return (
        f"{_SYSTEM}\n\nTASK DIR: {task_dir}\nTRIGGER: {move} (from {from_stage})\n"
        f"WHY: {reason}\nFINDINGS TO CLOSE:\n{f}\n\nProduce the patch plan now."
    )


def synthesize_plan(
    task_dir: str,
    move: str,
    from_stage: str,
    reason: str,
    *,
    findings: list[dict] | None = None,
    runner: Runner | None = None,
    model: str | None = None,
) -> SynthesizeOutput:
    """Produce a validated patch plan (LLM, quarantined). Raises CellError on bad output."""
    prompt = build_prompt(task_dir, move, from_stage, reason, findings)
    return run_cell(prompt, SynthesizeOutput, runner=runner, model=model)


def apply_prompt(plan: SynthesizeOutput) -> str:
    """Turn the validated patch plan into an apply prompt. The plan is authoritative: apply exactly
    these edits, preserve identity, never regenerate, and close the named catalog entries
    STRUCTURALLY (capability/sandbox defenses, not cosmetic source regexes)."""
    edits = "\n".join(f"  - {e.file}: {e.change}" for e in plan.patch)
    targets = "\n".join(
        f"  - {t.kind}" + (f" [{t.catalog_id}]" if t.catalog_id else "") + f": {t.detail}"
        for t in plan.addresses
    )
    return f"""\
## Your Task: apply a SURGICAL patch to an existing ProgramBench task

Task dir: {plan.task_dir}
Trigger: {plan.move} (from {plan.from_stage}) — {plan.reason}

Apply EXACTLY these edits in place (do NOT regenerate the task, do NOT change its identity —
source repo x tool x flag surface are frozen):
{edits}

These edits must close the following findings STRUCTURALLY (e.g. seal the leak, tighten the
anti-cheat tier, compute inside the verifier) — never cosmetically:
{targets}

If the patch touched the case suite (tests/testsuite cases or fixtures): re-capture the fixtures
from the oracle binary (programsmith.programbench.fixtures.build_test_fixtures) AND re-run the
determinism check (check_determinism, n=3) BEFORE re-validating — NEVER hand-edit an expected
output, and drop (never patch) any case that turns out nondeterministic.

After editing, re-validate by building the image and confirming the verifier is still sound:
  harbor run --agent nop    # must reward 0
  harbor run --agent oracle # must reward 1
Done only when the finding is closed AND NOP=0/Oracle=1 still hold.
"""


def apply(
    plan: SynthesizeOutput,
    task_dir: str | None = None,
    *,
    session=None,
    validator=None,
    max_iters: int = 2,
    model: str | None = None,
):
    """Apply the patch plan to the task tree and re-validate oracle=1/nop=0 (the SWE-gen patch loop;
    reuse basis: create/claude_code_runner.py + the reward-hack-catalog). The agent session + the
    validator are injectable (`cells.agentic`): the default validator builds + runs the verifier on
    local Docker; `baseline_validator` reads recorded oracle/nop trials (no local Docker, ADR-0019).
    The FSM then re-runs from the recorded rejoin stage on the patched tree. Returns an
    `agentic.AgentResult`."""
    from pathlib import Path

    from .agentic import claude_code_session, default_validator, run_to_green

    td = Path(task_dir or plan.task_dir)
    session = session or claude_code_session(model=model)
    # Default validator: the full local-Docker SANITY gate over the patched tree.
    validator = validator or default_validator(td, image_tag=f"lh-agentic:{td.name or 'task'}")
    return run_to_green(apply_prompt(plan), td, session=session, validator=validator,
                        max_iters=max_iters, label=f"synthesize-{plan.move}")
