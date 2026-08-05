"""Deterministic FSM router for the ProgramBench task pipeline (invariant #1).

This module is the dumb orchestrator's brain: a PURE function over (stage, verdict, counters)
that returns the next stage. It contains no I/O, no LLM calls, and no "reasoning" — it routes
only on the validated verdict a gate/decision produced. The DAG and bands it encodes are frozen
in PRODUCT.md (as amended by ADR-0038..0043); changes require a new ADR.

Forward chain (happy path):
    INGEST_LOCK → TASK_MATRIX → ORACLE_GOLDEN → CREATE → SANITY → STATIC_CI
    → DIFFICULTY_SWEEP (SMOKE: mini-swe @ GLM 5.2 ×3)
    → CALIBRATE (smoke decision)
    → QA_PROBE (Task Construction Auditor — pre-frontier reward-hack check)
    → FULL_SWEEP (FRONTIER: mini-swe @ Opus 4.8 ×3, target band 1/3–2/3)
    → QA_GATE (auto by default) → DONE (exported to outbox)

Backward edges (all land on SYNTHESIZE, the surgical-patch cell, which rejoins forward):
    SANITY.fail / STATIC_CI.fail       → SYNTHESIZE (revise; rejoin SANITY / STATIC_CI)
    CALIBRATE.harden / CALIBRATE.ease  → SYNTHESIZE (smoke tune, bounded SMOKE_TUNE_MAX;
                                         budget exhausted → PROCEED anyway: measure, don't predict)
    QA_PROBE.harden                    → SYNTHESIZE (smoke tune; budget exhausted → BLOCKED —
                                         a discovered exploit may NOT be waved through)
    FULL_SWEEP.harden / .ease          → SYNTHESIZE (frontier tune, bounded FRONTIER_TUNE_MAX;
                                         exhausted harden → EASY_SHELF, exhausted ease → DROPPED)
    FULL_SWEEP.revise                  → SYNTHESIZE (broken verifier/env; rejoin SANITY)
    QA_GATE.revise                     → SYNTHESIZE (rejoin STATIC_CI), bounded by REVISE_MAX

Shelf / drop edges:
    FULL_SWEEP.shelve                       → EASY_SHELF (good task, too easy to harden — kept,
                                              exported to the easy shelf, never trashed)
    CALIBRATE.flag_broken / FULL_SWEEP.flag_broken → DROPPED (genuinely broken/defective only;
                                              "too easy" is NEVER a drop reason anymore)

Difficulty doctrine (ADR-0040): easing is cheaper than hardening. Hardening budgets are small
and bounded; a too-easy task that survives its budget is SHELVED, not dropped. A zero-pass task
is kept only when the good-failure gate verified genuine capability headroom from trajectories
(ADR-0041) — that decision arrives here as a plain verdict; this module never reads traces.

Human review MAY happen at two stages only (HUMAN_STAGES): TASK_MATRIX and QA_GATE. Both are
AUTO by default (ADR-0039) — the driver decides whether to pause based on config; this module
stays pure and acts on the verdict once supplied (by human or by the validated auto decision).

Legacy stages QA_ON_GPT and PR remain in the enum ONLY so pre-ADR-0039 state files load and
mid-flight runs drain: both immediately route forward (no GPT QA pass, no PR is ever opened).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

# Loop bounds (PRODUCT.md bands_and_decisions, as amended by ADR-0040). Tunable defaults —
# PRODUCT.md pins the BAND (Opus 4.8 pass@1 target 1/3–2/3), not the retry counts.
SMOKE_TUNE_MAX = 2      # harden+ease rounds triggered from CALIBRATE / QA_PROBE (GLM smoke phase)
FRONTIER_TUNE_MAX = 1   # harden+ease rounds triggered from FULL_SWEEP (Opus frontier phase)
REVISE_MAX = 2          # broken verifier/env fixes (any phase)

# Deprecated alias (pre-ADR-0040 code/tests): the old single harden budget. New code never
# reads it; kept equal to the combined tuning budget so stale imports stay meaningful.
HARDEN_MAX = SMOKE_TUNE_MAX + FRONTIER_TUNE_MAX


class Stage(str, Enum):
    INGEST_LOCK = "INGEST_LOCK"
    TASK_MATRIX = "TASK_MATRIX"
    ORACLE_GOLDEN = "ORACLE_GOLDEN"
    CREATE = "CREATE"
    SANITY = "SANITY"
    STATIC_CI = "STATIC_CI"
    DIFFICULTY_SWEEP = "DIFFICULTY_SWEEP"  # smoke sweep (GLM 5.2)
    CALIBRATE = "CALIBRATE"
    QA_PROBE = "QA_PROBE"
    FULL_SWEEP = "FULL_SWEEP"              # frontier sweep (Opus 4.8)
    QA_ON_GPT = "QA_ON_GPT"                # LEGACY (ADR-0039): drains forward, never re-entered
    QA_GATE = "QA_GATE"
    SYNTHESIZE = "SYNTHESIZE"
    PR = "PR"                              # LEGACY (ADR-0039): drains to DONE, no PR is opened
    # terminal
    DONE = "DONE"
    DROPPED = "DROPPED"
    BLOCKED = "BLOCKED"
    EASY_SHELF = "EASY_SHELF"


TERMINAL: frozenset[Stage] = frozenset({Stage.DONE, Stage.DROPPED, Stage.BLOCKED, Stage.EASY_SHELF})

# Stages that MAY be human-gated. Whether they actually pause is a driver/config decision
# (ADR-0039: both default to auto); the router only reports needs_human for display.
HUMAN_STAGES: frozenset[Stage] = frozenset({Stage.TASK_MATRIX, Stage.QA_GATE})

# Allowed verdict vocabulary per stage. The router rejects anything else.
ALLOWED_VERDICTS: dict[Stage, frozenset[str]] = {
    Stage.INGEST_LOCK: frozenset({"pass", "fail"}),
    Stage.TASK_MATRIX: frozenset({"selected", "none_selected"}),
    Stage.ORACLE_GOLDEN: frozenset({"pass", "fail"}),
    Stage.CREATE: frozenset({"pass", "fail"}),
    Stage.SANITY: frozenset({"pass", "fail"}),
    Stage.STATIC_CI: frozenset({"pass", "fail"}),
    Stage.DIFFICULTY_SWEEP: frozenset({"done"}),
    Stage.CALIBRATE: frozenset({"proceed", "harden", "ease", "flag_broken"}),
    Stage.QA_PROBE: frozenset({"clean", "harden"}),
    Stage.FULL_SWEEP: frozenset({"done", "harden", "ease", "revise", "flag_broken", "shelve"}),
    Stage.QA_ON_GPT: frozenset({"done", "revise"}),  # legacy drain: both route forward
    Stage.QA_GATE: frozenset({"accept", "revise", "reject"}),
    Stage.SYNTHESIZE: frozenset({"done"}),
    Stage.PR: frozenset({"done"}),  # legacy drain
}


@dataclass(frozen=True)
class Counters:
    harden: int = 0
    revise: int = 0
    ease: int = 0
    smoke_tunes: int = 0     # harden+ease rounds consumed at the smoke phase
    frontier_tunes: int = 0  # harden+ease rounds consumed at the frontier phase


@dataclass(frozen=True)
class Decision:
    """The router's verdict: where to go next and the updated loop state."""

    next: Stage
    counters: Counters
    reason: str
    needs_human: bool = False  # True iff the NEXT stage is a (potentially) human-review stage
    synthesize_rejoin: Stage | None = None  # where SYNTHESIZE will rejoin the forward chain


class FsmError(ValueError):
    """Raised when a verdict is not in a stage's allowed vocabulary, or a stage is terminal."""


def allowed_verdicts(stage: Stage) -> frozenset[str]:
    return ALLOWED_VERDICTS.get(stage, frozenset())


def _advance(nxt: Stage, counters: Counters, reason: str, rejoin: Stage | None = None) -> Decision:
    return Decision(
        next=nxt,
        counters=counters,
        reason=reason,
        needs_human=nxt in HUMAN_STAGES,
        synthesize_rejoin=rejoin,
    )


def route(
    stage: Stage,
    verdict: str,
    counters: Counters,
    synthesize_rejoin: Stage | None = None,
    detail: str | None = None,
    *,
    smoke_tune_max: int = SMOKE_TUNE_MAX,
    frontier_tune_max: int = FRONTIER_TUNE_MAX,
    revise_max: int = REVISE_MAX,
) -> Decision:
    """Pure transition. Given the current stage, its validated verdict, and loop counters,
    return the Decision (next stage + updated counters).

    `synthesize_rejoin` is only consulted when `stage is Stage.SYNTHESIZE`: it is the forward
    stage the patch should rejoin (carried in the run state from the backward edge that armed it).

    `detail` is the gate handler's own reason string (e.g. "sanity failed:
    ['produced_owned_by_nobody']"). It NEVER affects routing — the decision is still a pure
    function of the verdict — it only enriches the recorded reason on backward edges so the
    SYNTHESIZE trigger and the UI carry the SPECIFIC failing check.

    The three `*_max` kwargs let config thread per-run bounds; defaults are the module
    constants (ADR-0027 precedent: bounds are tunable, the band is pinned).
    """
    if stage in TERMINAL:
        raise FsmError(f"{stage.value} is terminal; nothing to route")
    if verdict not in allowed_verdicts(stage):
        raise FsmError(
            f"verdict {verdict!r} not allowed at {stage.value}; "
            f"allowed: {sorted(allowed_verdicts(stage))}"
        )

    match stage:
        case Stage.INGEST_LOCK:
            return (
                _advance(Stage.TASK_MATRIX, counters, "source locked")
                if verdict == "pass"
                else _advance(Stage.DROPPED, counters, detail or "ingest failed (unbuildable/unusable/ProgramBench-overlap source)")
            )

        case Stage.TASK_MATRIX:  # auto-picked by default (ADR-0039); human review optional
            return (
                _advance(Stage.ORACLE_GOLDEN, counters, "candidate selected")
                if verdict == "selected"
                else _advance(Stage.DROPPED, counters, "no viable candidate selected")
            )

        case Stage.ORACLE_GOLDEN:
            return (
                _advance(Stage.CREATE, counters, "oracle pair + docs + case suite captured")
                if verdict == "pass"
                else _advance(Stage.BLOCKED, counters, "oracle/case-suite capture failed")
            )

        case Stage.CREATE:
            return (
                _advance(Stage.SANITY, counters, "ProgramBench task built from the generator")
                if verdict == "pass"
                else _advance(Stage.BLOCKED, counters, "create failed to produce a task")
            )

        case Stage.SANITY:
            if verdict == "pass":
                return _advance(Stage.STATIC_CI, counters, "nop fails / oracle solver passes")
            # broken task → patch, then re-run from SANITY. BOUNDED by the revise counter (a task
            # that can't reach oracle=1/nop=0 in REVISE_MAX patches is fundamentally broken →
            # BLOCK, never loop) — and bumping the counter changes the synthesize job id so each
            # attempt RE-RUNS the agent instead of instant-applying a stale 'done' patch.
            return _revise(counters, detail or "sanity failed", rejoin=Stage.SANITY,
                           revise_max=revise_max)

        case Stage.STATIC_CI:
            if verdict == "pass":
                return _advance(Stage.DIFFICULTY_SWEEP, counters, "static CI CHECK_ORDER green")
            return _revise(counters, detail or "static CI failed", rejoin=Stage.STATIC_CI,
                           revise_max=revise_max)  # bounded (see SANITY)

        case Stage.DIFFICULTY_SWEEP:
            return _advance(Stage.CALIBRATE, counters, "GLM-5.2 smoke sweep complete")

        case Stage.CALIBRATE:
            if verdict == "proceed":
                return _advance(Stage.QA_PROBE, counters, "smoke band acceptable; proceed")
            if verdict == "flag_broken":
                # Genuinely broken/defective only (broken-zero, unbuildable). "Too easy" is
                # NEVER a drop reason at CALIBRATE — that maps to harden or, budget exhausted,
                # to proceed (the frontier is the authoritative signal; ADR-0040).
                return _advance(Stage.DROPPED, counters, detail or "calibrate: task broken/impossible")
            # harden (GLM saturated) or ease (GLM zero with task-design blockers)
            return _tune_smoke(counters, verdict, detail
                               or ("calibrate: GLM saturated (too easy)" if verdict == "harden"
                                   else "calibrate: GLM zero-pass with task-design blockers"),
                               smoke_tune_max)

        case Stage.QA_PROBE:
            if verdict == "clean":
                return _advance(Stage.FULL_SWEEP, counters, "no reward-hack found")
            # An exploit/blocker the auditor FOUND must be fixed — it shares the smoke tuning
            # budget, but on exhaustion it BLOCKS (an exploit may not be waved through).
            if counters.smoke_tunes + 1 > smoke_tune_max:
                return _advance(Stage.BLOCKED, counters,
                                f"probe: exploit/blocker found but smoke tuning budget "
                                f"{smoke_tune_max} exhausted — needs human")
            bumped = replace(counters, harden=counters.harden + 1,
                             smoke_tunes=counters.smoke_tunes + 1)
            return _advance(Stage.SYNTHESIZE, bumped,
                            f"probe: reward-hack/shortcut found; smoke tune "
                            f"{bumped.smoke_tunes}/{smoke_tune_max}", rejoin=Stage.STATIC_CI)

        case Stage.FULL_SWEEP:
            # The frontier sweep is the AUTHORITATIVE difficulty signal (real Opus 4.8, mini-swe,
            # full k). Target band: pass@1 in [1/3, 2/3] (ADR-0040).
            if verdict == "flag_broken":
                return _advance(Stage.DROPPED, counters, detail or "frontier: task broken/defective")
            if verdict == "shelve":
                # Good task, too easy to harden (harden-review auditor) → keep on the easy shelf.
                return _advance(Stage.EASY_SHELF, counters,
                                detail or "frontier saturated; too easy to harden — shelved as easy")
            if verdict in ("harden", "ease"):
                return _tune_frontier(counters, verdict, detail
                                      or ("frontier: Opus saturated above band (too easy)"
                                          if verdict == "harden"
                                          else "frontier: zero/low pass with task-design blockers"),
                                      frontier_tune_max)
            # Authoritative integrity FAILED — oracle≠1/nop≠0, or a BAD_SUCCESS reward-hack, or a
            # BAD_FAILURE env defect: fix the task (REVISE, not a tune), never ship a broken band.
            if verdict == "revise":
                return _revise(counters, detail or "frontier: broken verifier/env or gameable reward",
                               rejoin=Stage.SANITY, revise_max=revise_max)
            return _advance(Stage.QA_GATE, counters, "frontier sweep complete; band recorded")

        case Stage.QA_ON_GPT:  # LEGACY drain (ADR-0039): no GPT QA pass exists anymore
            return _advance(Stage.QA_GATE, counters,
                            "legacy QA_ON_GPT stage auto-completed (removed by ADR-0039)")

        case Stage.QA_GATE:  # auto-decided by default (ADR-0039); human review optional
            if verdict == "accept":
                return _advance(Stage.DONE, counters, "accepted — exported to outbox")
            if verdict == "reject":
                return _advance(Stage.DROPPED, counters, "rejected (broken/underspecified/impossible)")
            return _revise(counters, detail or "qa gate: revise (re-run frontier sweep)",
                           revise_max=revise_max)

        case Stage.SYNTHESIZE:
            rejoin = synthesize_rejoin or Stage.STATIC_CI
            return _advance(rejoin, counters, f"patch applied; rejoin {rejoin.value}")

        case Stage.PR:  # LEGACY drain (ADR-0039): no PR is ever opened
            return _advance(Stage.DONE, counters,
                            "legacy PR stage auto-completed (PR output removed by ADR-0039)")

    raise FsmError(f"unhandled stage {stage}")  # pragma: no cover


def _tune_smoke(counters: Counters, verdict: str, reason: str, bound: int) -> Decision:
    """Smoke-phase tuning (harden or ease). Budget exhausted → PROCEED to the frontier anyway:
    the smoke model is a cheap early signal, never the authority (measure, don't predict)."""
    if counters.smoke_tunes + 1 > bound:
        return _advance(Stage.QA_PROBE, counters,
                        f"{reason}; smoke tuning budget {bound} exhausted — "
                        f"proceeding to frontier (measure, don't predict)")
    bumped = replace(
        counters,
        harden=counters.harden + (1 if verdict == "harden" else 0),
        ease=counters.ease + (1 if verdict == "ease" else 0),
        smoke_tunes=counters.smoke_tunes + 1,
    )
    return _advance(Stage.SYNTHESIZE, bumped,
                    f"{reason}; smoke tune {bumped.smoke_tunes}/{bound}", rejoin=Stage.STATIC_CI)


def _tune_frontier(counters: Counters, verdict: str, reason: str, bound: int) -> Decision:
    """Frontier-phase tuning. Budget exhausted: a still-too-easy task is SHELVED (it's a good
    task — keep it); a still-defective task (ease exhausted) is DROPPED."""
    if counters.frontier_tunes + 1 > bound:
        if verdict == "harden":
            return _advance(Stage.EASY_SHELF, counters,
                            f"{reason}; frontier tuning budget {bound} exhausted — shelved as easy")
        return _advance(Stage.DROPPED, counters,
                        f"{reason}; ease budget {bound} exhausted — task-design defect persists")
    bumped = replace(
        counters,
        harden=counters.harden + (1 if verdict == "harden" else 0),
        ease=counters.ease + (1 if verdict == "ease" else 0),
        frontier_tunes=counters.frontier_tunes + 1,
    )
    return _advance(Stage.SYNTHESIZE, bumped,
                    f"{reason}; frontier tune {bumped.frontier_tunes}/{bound}",
                    rejoin=Stage.STATIC_CI)


def _revise(counters: Counters, reason: str, rejoin: Stage = Stage.STATIC_CI,
            revise_max: int = REVISE_MAX) -> Decision:
    if counters.revise + 1 > revise_max:
        return _advance(Stage.BLOCKED, counters, f"{reason}; revise bound {revise_max} exhausted")
    bumped = replace(counters, revise=counters.revise + 1)
    return _advance(Stage.SYNTHESIZE, bumped, f"{reason}; revise {bumped.revise}/{revise_max}",
                    rejoin=rejoin)
