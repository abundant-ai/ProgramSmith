"""QA GATE decision (deterministic) — the FINAL accept / revise / reject after the frontier sweep.

AUTO by default (ADR-0039): the orchestrator computes these flags from the recorded manifest.sweeps
and the gate decides; in human mode the same verdict vocabulary is supplied by the operator. All
inputs are validated upstream — this gate never reads trajectories or calls an LLM.

Inputs:
  * band_verdict     — runconfig.band_verdict over the frontier groups: "keep" (in the 1/3–2/3
                       Opus window) | "too_easy" | "too_hard" | None (nothing measured)
  * integrity_ok     — oracle=1/nop=0 held on the authoritative (closed-internet) frontier sweep
  * probe_clean      — QA/PROBE auditor verdict was `clean` (no reward-hack found)
  * hard_keep        — the good-failure gate verified a zero/low-pass band as genuine capability
                       headroom (ADR-0041): manifest.sweeps["full"]["hard_keep"]
  * analysis_concern — any BAD_* TrialClassifier label on the frontier trials (a gamed pass or a
                       task-defect failure) survived to this point

Routing:
  accept — (band_verdict == "keep" OR hard_keep) AND integrity_ok AND probe_clean AND
           NOT analysis_concern
  revise — a FIXABLE concern: broken integrity, a dirty probe, or a BAD_* analysis label
           (all are verifier/task defects a SYNTHESIZE patch can close)
  reject — broken/underspecified evidence only (rare — most rejects already happened upstream):
           the band is out-of-window or unmeasured with no verified hard-keep, so there is
           nothing left to fix by revision.
"""

from __future__ import annotations

from . import GateResult


def qa_gate(
    band_verdict: str | None,
    *,
    integrity_ok: bool = True,
    probe_clean: bool = True,
    hard_keep: bool = False,
    analysis_concern: bool = False,
) -> GateResult:
    detail = {"band_verdict": band_verdict, "integrity_ok": integrity_ok,
              "probe_clean": probe_clean, "hard_keep": hard_keep,
              "analysis_concern": analysis_concern}

    # Fixable concerns first — a revise routes to SYNTHESIZE and re-measures, so any defect that a
    # patch can close must never fall through to accept OR reject.
    if not integrity_ok:
        return GateResult("revise", "integrity broken (oracle≠1/nop≠0 on the frontier sweep) — "
                          "fix the verifier/environment", detail)
    if not probe_clean:
        return GateResult("revise", "reward-hack unresolved (QA/PROBE not clean) — harden", detail)
    if analysis_concern:
        return GateResult("revise", "BAD_* TrialClassifier label on the frontier trials "
                          "(gamed pass or task-defect failure) — fix before shipping", detail)

    if band_verdict == "keep" or hard_keep:
        note = " (hard-keep: zero/low pass verified as genuine headroom)" if hard_keep \
            and band_verdict != "keep" else ""
        return GateResult("accept", f"accepted: frontier band in window, all checks clean{note}",
                          detail)

    # Nothing fixable and nothing acceptable: the band is out-of-window (a leak past FULL_SWEEP's
    # own tuning edges) or unmeasured — broken/underspecified evidence.
    return GateResult("reject", f"no acceptable evidence (band_verdict={band_verdict!r}, "
                      "no verified hard-keep) — broken/underspecified", detail)
