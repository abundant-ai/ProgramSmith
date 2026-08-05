"""HARDEN REVIEW (deterministic auditor) — at a saturation harden checkpoint, decide whether the task
can ACTUALLY be hardened into the band, or whether iterative hardening is futile and it should be
DROPPED.

The failure mode this catches: a source that is fundamentally too easy/small for a long-horizon
rewrite task. The orchestrator keeps hardening it (SYNTHESIZE → re-measure), but the frontier keeps
acing it — pass@1 never comes down. Without this review the run just burns harden iterations and
ends BLOCKED at the bound. This auditor reads the EVIDENCE already on hand — the pass@1 trajectory
across harden generations + the TrialClassifier breakdown — and drops a task that isn't converging.

It is a real review of two signals, kept deterministic (determinism-sandwich: the gate decides on
validated fields, no LLM in the control path):

  1. Trajectory — is hardening MOVING the band? If pass@1 isn't dropping across generations, the
     patches aren't making the task harder for the frontier.
  2. Nature of the saturation — honest solves (GOOD_SUCCESS) vs a gamed verifier (BAD_SUCCESS). A
     GAMED saturation is FIXABLE (close the verifier hole) → keep hardening. An honest, non-
     converging saturation means the task is genuinely too easy → drop.

Verdicts: "harden" (viable — keep going) | "drop" (futile — too easy to harden).
"""

from __future__ import annotations

from . import GateResult

SATURATED = 0.60          # the spec'd band (mirror gates.calibrate.ACCEPTABLE_MAX)
GAMED_LABELS = ("BAD_SUCCESS",)         # verifier was gamed — fixable, not "too easy"
HONEST_SUCCESS_LABELS = ("GOOD_SUCCESS",)


PERFECT = 0.99  # "the frontier aces essentially every trial"


def harden_review(
    pass_at_1: float | None,
    *,
    history: list[float] | None = None,
    harden_count: int = 0,
    breakdown: dict | None = None,
    drop_after: int = 3,
    min_improvement: float = 0.10,
) -> GateResult:
    """Decide harden vs drop at a saturation checkpoint.

    The bias is to EXHAUST the harden budget: a task keeps hardening until it has had `drop_after`
    rounds (default 3 = HARDEN_MAX) and STILL isn't converging — only then is it dropped (turning the
    would-be BLOCKED-at-bound into a reasoned DROP). The sole early exit is STRONG evidence that
    hardening is futile: the frontier perfectly aces every trial across ≥2 honest generations.

    pass_at_1     — the frontier band that just triggered the harden (> the configured ceiling).
    history       — prior saturated pass@1 values, one per earlier harden generation (oldest→newest).
    harden_count  — hardens ALREADY applied (state.harden, i.e. before this one).
    breakdown     — TrialClassifier label tally for the frontier trials (advisory), e.g.
                    {"GOOD_SUCCESS": 3, "BAD_SUCCESS": 1}.
    drop_after    — hardens that must already have been tried (non-converging) before dropping.
    min_improvement — pass@1 must drop at least this much vs the best prior attempt to count as
                    "converging" (hardening is working).
    """
    if pass_at_1 is None:
        return GateResult("harden", "no band to assess viability — defer to the existing harden flow")

    prior = [p for p in (history or []) if isinstance(p, (int, float))]
    best_prior = min(prior) if prior else None
    improved = (best_prior - pass_at_1) if best_prior is not None else None
    converging = improved is not None and improved >= min_improvement

    gamed = sum((breakdown or {}).get(lbl, 0) for lbl in GAMED_LABELS)
    honest = sum((breakdown or {}).get(lbl, 0) for lbl in HONEST_SUCCESS_LABELS)

    detail = {"pass_at_1": pass_at_1, "best_prior": best_prior, "improved": improved,
              "harden_count": harden_count, "gamed": gamed, "honest": honest}

    # A GAMED saturation is a verifier hole, not an easy task — closing it is exactly what SYNTHESIZE
    # does. Keep hardening (don't drop a fixable task), regardless of convergence.
    if gamed and not converging:
        return GateResult("harden", f"saturation looks GAMED ({gamed} BAD_SUCCESS trial(s)) — a verifier "
                          "hole to close, not a too-easy task; keep hardening", detail)

    # STRONG-evidence early drop (the only exit before the full budget): the frontier perfectly aces
    # EVERY trial NOW and in EVERY recorded prior generation, across ≥2 honest hardens — hardening is
    # demonstrably doing nothing. A single aced round, an empty history (best_prior None), or any prior
    # dip is NOT strong enough; the task uses its full harden budget otherwise.
    if (harden_count >= 2 and pass_at_1 >= PERFECT and best_prior is not None
            and best_prior >= PERFECT and not gamed):
        return GateResult("drop", f"frontier still aces every trial (pass@1 {pass_at_1:.2f}) after "
                          f"{harden_count} honest harden(s), best prior {best_prior:.2f} — hardening is "
                          "doing nothing; fundamentally too easy → DROP", detail)

    # Budget exhausted and the band still won't move → genuinely too easy to harden → drop (this is
    # the case that used to end BLOCKED at the harden bound; now it's a reasoned DROP).
    if harden_count >= drop_after and not converging:
        bp = f"{best_prior:.2f}" if best_prior is not None else "n/a"
        return GateResult("drop", f"iterative hardening not converging — pass@1 still {pass_at_1:.2f} "
                          f"after {harden_count} harden(s) (best prior {bp}); too easy to harden into "
                          "the band → DROP", detail)

    trend = (f"improving (−{improved:.2f} vs best prior {best_prior:.2f})" if converging
             else "more headroom to try" if best_prior is None
             else f"flat so far (best prior {best_prior:.2f})")
    return GateResult("harden", f"harden viable ({harden_count}/{drop_after}): pass@1 {pass_at_1:.2f}, "
                      f"{trend}", detail)
