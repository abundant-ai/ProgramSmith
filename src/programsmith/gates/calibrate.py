"""CALIBRATE decision (deterministic) — the SMOKE gate after the GLM difficulty sweep (ADR-0040).

The smoke sweep (mini-swe @ GLM 5.2 ×3) is a cheap early signal, never the authority — the
frontier (Opus) sweep decides shelf-vs-keep. Decision order (DESIGN §4.1, all on validated fields):

  1. no band measured                      → flag_broken (all trials errored / broken-zero — the
                                             only remaining drop reason at CALIBRATE)
  2. any BAD_SUCCESS smoke label           → harden (reward-hack: close the verifier hole BEFORE
                                             spending opus; kind "reward-hack" so the caller skips
                                             the saturation harden-review)
  3. band too_easy (GLM 3/3 saturates)     → harden (kind "saturation"; the orchestrator routes it
                                             through the harden-review auditor — a review "drop"
                                             now maps to PROCEED + smoke_saturated, NEVER a drop)
  4. pass@1 == 0                           → good-failure label gate (goodfail.label_gate):
                                             all GOOD_FAILURE → proceed (keep; hard task) |
                                             any BAD_FAILURE → ease (surgical blocker removal) |
                                             inconclusive → proceed (measure, don't predict —
                                             deep audits are reserved for the frontier)
  5. otherwise                             → proceed

"Too easy" is NEVER a drop reason here anymore; the pre-ADR-0040 flag_broken-on-auditor-drop edge
is gone. The gate stays pure: band/labels in, verdict out; no LLM, no I/O.
"""

from __future__ import annotations

from . import GateResult

# Legacy constants (pre-ADR-0040 band): kept because harden_review mirrors ACCEPTABLE_MAX and old
# call sites/tests import them. The LIVE ceiling now comes from the run's BandSpec (smoke default
# max_pass=0.90 — with k=3 only 3/3 saturates), threaded via `saturate_above`.
ACCEPTABLE_MAX = 0.60
SWEET_MAX = 0.30

_GAMED = ("BAD_SUCCESS",)


def calibrate(
    pass_at_1: float | None,
    *,
    saturate_above: float = ACCEPTABLE_MAX,
    band_verdict: str | None = None,
    breakdown: dict | None = None,
    labels: list[str] | None = None,
) -> GateResult:
    """`pass_at_1` is the smoke band value (the run's configured basis). `band_verdict` is the
    runconfig.band_verdict classification over the per-agent groups when available ("keep" |
    "too_easy" | "too_hard" | None) — it wins over the scalar ceiling (it carries the per-model
    combinator policy); the scalar `saturate_above` check is the fallback for legacy/injected
    entries with no groups. `breakdown`/`labels` are the TrialClassifier tallies/labels for the
    smoke trials (advisory annotations; this gate routes deterministically on them).

    GateResult.detail carries {"kind": "reward-hack" | "saturation"} on a harden so the caller
    knows whether the harden-review auditor applies (saturation only)."""
    if pass_at_1 is None:
        return GateResult("flag_broken", "no usable band (all trials errored?)")

    gamed = sum((breakdown or {}).get(l, 0) for l in _GAMED)
    if gamed:
        return GateResult("harden",
                          f"{gamed} BAD_SUCCESS smoke trial(s) — verifier gamed; close the hole "
                          "before spending the frontier sweep",
                          {"kind": "reward-hack", "gamed": gamed})

    too_easy = (band_verdict == "too_easy") if band_verdict is not None \
        else pass_at_1 > saturate_above
    if too_easy:
        return GateResult("harden",
                          f"pass@1={pass_at_1:.2f} > {saturate_above:.2f} (smoke saturated)",
                          {"kind": "saturation"})

    if pass_at_1 == 0:
        from ..goodfail import label_gate  # pure — no LLM, no I/O
        gf = label_gate([str(l) for l in (labels or [])])
        if gf["verdict"] == "ease":
            return GateResult("ease", f"pass@1=0 with task-design blockers ({gf['reason']})",
                              {"goodfail": gf})
        note = "genuine headroom — kept" if gf["verdict"] == "keep_hard" else \
            f"{gf['reason']} — kept; the frontier measures it"
        return GateResult("proceed", f"pass@1=0 ({note})", {"goodfail": gf})

    band = "sweet-spot" if pass_at_1 <= SWEET_MAX else "acceptable"
    return GateResult("proceed", f"pass@1={pass_at_1:.2f} in {band} band")
