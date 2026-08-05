"""Good-failure gate (ADR-0041) — "0/N must mean genuine capability headroom, not task-design
failure. Oracle passing is NOT enough — read the trajectories."

A zero-pass sweep is ambiguous: the task may be genuinely hard (keep it — that is the point of a
frontier task) or defective in a way the oracle can't see (an ambiguous instruction, a universal
blocker case every trial dies on the same way, a broken environment). This module decides which,
in two tiers that respect invariant #4 (LLM annotates, gate decides):

  1. `label_gate` — PURE function over the TrialClassifier labels the sweep already produced
     (`--run-analysis`): all GOOD_FAILURE → keep_hard; any BAD_FAILURE → ease; else inconclusive.
     Cheap, deterministic, no LLM. The only tier run at the SMOKE phase.
  2. `deep_audit` — a ONE-SHOT LLM cell (model = LhConfig.cell_model_analysis) over the pulled
     trajectory TAILS of the failed frontier trials + the verifier's per-case diff artifact NAMES.
     It only ANNOTATES (schema-validated `GoodFailReport`); `audit_gate` then decides
     deterministically on the validated `failure_mode` fields. Reserved for the FRONTIER phase,
     bounded to once per generation by the orchestrator (cached in manifest.sweeps["full"]).

Reuse basis: the TrialClassifier label vocabulary (local_runner.classify_trajectory) + the same run_cell quarantine
every other cell uses (llm.py). The gate never routes on prose — only on the Literal enum.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .llm import Runner, run_cell

# TrialClassifier labels (local_runner.TRIAL_LABELS) the pure gate routes on.
GOOD_FAILURE = "GOOD_FAILURE"   # failed legitimately — real capability headroom
BAD_FAILURE = "BAD_FAILURE"     # failed on a task/harness defect — NOT difficulty
_SUCCESS_LABELS = ("GOOD_SUCCESS", "BAD_SUCCESS")

# Trajectory-tail bounds for the deep audit: enough to see HOW each trial died (the last error, the
# final diff loop, the give-up message) without shipping whole multi-MB transcripts into one prompt.
TAIL_LINES = 200
_MAX_PROMPT_CHARS = 60_000
# Per-tail floor. Tails are LINE-bounded first (TAIL_LINES) then CHAR-capped to an equal share of
# the remaining prompt budget — stream-json transcripts (claude-code) pack a whole event per line,
# so 200 "lines" can be 300KB and a naive line-only bound let the FIRST tail eat the entire budget,
# eliding every other trial. keep_hard requires the audit to have SEEN every failed trial (the
# coverage guard), so every tail must land in the prompt, capped, never dropped.
_MIN_TAIL_CHARS = 2_000
_TRAJECTORY_SUFFIXES = (".json", ".jsonl", ".log", ".txt", ".md", ".cast")
_NON_TRAJECTORY_NAMES = ("result.json", "grade_result.json", "metrics.json")


def label_gate(labels: list[str], *, pending: int = 0) -> dict:
    """PURE decision over TrialClassifier labels for a zero-pass sweep. Returns
    {"verdict": "keep_hard" | "ease" | "inconclusive", "reason", "counts"}.

      * any BAD_FAILURE          → "ease"  (a task/env defect blocked the trial — surgical fix:
                                   remove universal blockers / fix ambiguous instructions)
      * all GOOD_FAILURE (≥1)    → "keep_hard" (every miss is a genuine capability miss)
      * anything else            → "inconclusive" (empty, HARNESS_ERROR-dominated, or mixed with
                                   success labels that contradict a zero-pass band) — the caller
                                   escalates (deep_audit at frontier; proceed-and-measure at smoke).

    `pending` is the count of trials the classifier has NOT yet labelled. keep_hard REQUIRES a
    complete label set: shipping a 0-pass task on a partial all-GOOD_FAILURE view could hide a
    still-unclassified BAD_FAILURE (a design/env defect). With pending>0, keep_hard is withheld →
    "inconclusive" so the caller escalates to the deep audit, which reads the actual trajectories
    on disk rather than trusting an incomplete label set. A BAD_FAILURE already visible still
    short-circuits to "ease" (a known defect is a defect regardless of what's still pending).
    """
    counts: dict[str, int] = {}
    for l in labels:
        k = str(l).upper()
        counts[k] = counts.get(k, 0) + 1
    if counts.get(BAD_FAILURE):
        return {"verdict": "ease",
                "reason": f"{counts[BAD_FAILURE]} BAD_FAILURE trial(s) — task/env defect, not difficulty",
                "counts": counts}
    if pending and pending > 0:
        return {"verdict": "inconclusive",
                "reason": f"{pending} trial(s) still pending classification — cannot confirm "
                          f"all-headroom from a partial label set; escalating to the deep audit",
                "counts": counts}
    if counts and all(k == GOOD_FAILURE for k in counts):
        return {"verdict": "keep_hard",
                "reason": f"all {counts[GOOD_FAILURE]} failure(s) labelled GOOD_FAILURE — genuine headroom",
                "counts": counts}
    return {"verdict": "inconclusive",
            "reason": ("no labels available" if not counts else
                       f"labels inconclusive ({', '.join(f'{v} {k}' for k, v in sorted(counts.items()))})"),
            "counts": counts}


class TrialAudit(BaseModel):
    trial_id: str
    # The ONLY field the gate routes on — a validated enum, never prose (invariant #4).
    failure_mode: Literal["capability_headroom", "task_design_failure", "environment_failure"]
    evidence: str


class GoodFailReport(BaseModel):
    trials: list[TrialAudit] = Field(min_length=1)  # the cell must audit at least one trial
    summary: str


_AUDIT_SYSTEM = """You are the GOOD-FAILURE AUDITOR of a task factory. A frontier sweep just \
scored 0 passes on a generated task, and the factory must decide whether that zero is EARNED \
(genuine capability headroom — the task is hard and stays) or FAKE (a task-design or environment \
defect blocked every attempt — the task must be eased or fixed). You are given the TAIL of each \
failed trial's trajectory plus the names of the verifier's per-case diff artifacts. Classify EVERY \
trial's failure_mode:

  * capability_headroom — the agent genuinely engaged, made partial progress, and ran out of \
skill/time on the real problem (wrong outputs on legitimately hard cases, incomplete features).
  * task_design_failure — the TASK blocked it: ambiguous/contradictory instructions, a universal \
blocker case every trial dies on identically, expected outputs that are unreachable from the given \
docs, grading stricter than the stated contract.
  * environment_failure — the ENVIRONMENT/harness blocked it: missing tools, network denials on \
required steps, verifier crashes, image defects — the agent never got a fair attempt.

Cite concrete evidence from the tails (quote the failing pattern). Do NOT grade generously: a \
zero-pass task ships only on verified headroom, so an unclear trajectory is a design/environment \
concern, not headroom."""


def _trajectory_tails(pull_dir: Path) -> list[tuple[str, str]]:
    """Collect (label, last-TAIL_LINES text) for each trial's trajectory-ish files under a pulled
    sweep dir. Deterministic walk (sorted); skips the structured result/grade records (the labels
    came from those already — the audit wants the raw transcript the classifier may have skimmed).

    The local sweep engine records each solve transcript at `<trial>/<task>/.trajectory` (a
    suffix-less dotfile, plus mini-swe's `.trajectory.json`) INSIDE a full copy of the task
    workspace. When those canonical records exist, use ONLY them — the generic suffix walk would
    skip the dotfile entirely and flood the prompt budget with the workspace's own .md/.txt/.json
    fixtures (the audit would read the task tree, not the transcripts). Imported/remote pull trees
    without `.trajectory` records keep the generic walk."""
    tails: list[tuple[str, str]] = []
    root = Path(pull_dir)
    if not root.exists():
        return tails
    files = [p for p in sorted(root.rglob("*")) if p.is_file()]
    canonical = [p for p in files if p.name in (".trajectory", ".trajectory.json")]
    if canonical:
        picked = canonical
    else:
        picked = [p for p in files
                  if p.suffix.lower() in _TRAJECTORY_SUFFIXES
                  and p.name not in _NON_TRAJECTORY_NAMES and not p.name.startswith("diff_")]
    for p in picked:
        try:
            lines = p.read_text(errors="replace").splitlines()
        except OSError:
            continue
        if not lines:
            continue
        tails.append((str(p.relative_to(root)), "\n".join(lines[-TAIL_LINES:])))
    return tails


def _diff_names(pull_dir: Path) -> list[str]:
    """The verifier's per-case diff artifact NAMES (diff_<case>.txt) — the case ids alone reveal a
    universal blocker (every trial leaves the same diff file) without shipping diff contents."""
    root = Path(pull_dir)
    if not root.exists():
        return []
    return sorted({p.name for p in root.rglob("diff_*.txt")})


def build_audit_prompt(pull_dir: str | Path, trials: list[dict]) -> str:
    root = Path(pull_dir)
    listed = "\n".join(f"- {t.get('trial_id', '?')}: label={t.get('label', '?')}" for t in trials) \
        or "- (trial list unavailable — audit every failed trajectory found below)"
    diffs = _diff_names(root)
    diff_block = "\n".join(f"- {n}" for n in diffs) or "- (none found)"
    parts = [f"{_AUDIT_SYSTEM}\n\nFAILED TRIALS:\n{listed}\n\n"
             f"VERIFIER DIFF ARTIFACTS (names only; a name repeated across trials = candidate "
             f"universal blocker):\n{diff_block}\n"]
    budget = _MAX_PROMPT_CHARS - len(parts[0])
    tails = _trajectory_tails(root)
    # EVERY tail must land in the prompt (the coverage guard requires the audit to have seen every
    # failed trial), so each gets an equal CHAR share of the budget: stream-json transcripts pack a
    # whole event per line, and a line-only bound let the first multi-hundred-KB tail evict the
    # rest (the tengo blind audit — the model honestly reported it saw nothing and eased a keep).
    share = max(_MIN_TAIL_CHARS, budget // max(1, len(tails)) - 120)   # 120 ≈ the header line
    for label, tail in tails:
        if len(tail) > share:
            tail = "…" + tail[-share:]
        block = f"\n--- TRAJECTORY TAIL: {label} (last {TAIL_LINES} lines, char-capped) ---\n{tail}\n"
        if budget - len(block) < 0:
            parts.append("\n--- (further trajectory tails elided: prompt budget) ---\n")
            break
        parts.append(block)
        budget -= len(block)
    parts.append("\nAudit every failed trial now.")
    return "".join(parts)


def deep_audit(pull_dir: str | Path, trials: list[dict], *, runner: Runner | None = None,
               model: str | None = None) -> GoodFailReport:
    """One-shot LLM audit of the failed trials' trajectory tails (quarantined via run_cell —
    schema-validated before any gate sees it). `model` defaults to LhConfig.cell_model_analysis
    (the heavy analysis model, ADR-0042); `runner` is injectable so tests run offline."""
    if model is None:
        try:
            from .config import LhConfig
            model = LhConfig.load().cell_model_analysis
        except Exception:  # noqa: BLE001 — config unavailable → llm.py's default cell model
            model = None
    return run_cell(build_audit_prompt(pull_dir, trials), GoodFailReport,
                    runner=runner, model=model)


def audit_gate(report: GoodFailReport, *, expected_trial_ids: list[str] | None = None) -> dict:
    """PURE decision over a validated GoodFailReport. Returns {"verdict", "reason"}:
      * every failure_mode == capability_headroom → "keep_hard" (ship as a hard task)
      * any task_design_failure                   → "ease"      (surgical blocker removal)
      * any environment_failure (no design fault) → "revise"    (fix the env/verifier)
    task_design_failure outranks environment_failure (DESIGN §4.3 ordering): an ease patch also
    re-runs sanity, so the design fix subsumes; the reason still names both.

    COVERAGE GUARD (invariant #4): keep_hard is the only verdict that SHIPS a 0-pass task, so it
    requires the audit to cover EVERY failed trial. Without this, the LLM's choice of which trials
    to include in `report.trials` would control the outcome — it could report only the one trial
    that was headroom and omit the two that were design failures. When `expected_trial_ids` is
    given and the report omits any, keep_hard is withheld → "revise" (re-audit fresh trajectories;
    bounded by the revise budget, so persistent under-coverage escalates to a human, never ships).
    ease/revise (non-shipping) verdicts don't need full coverage — one confirmed defect is enough."""
    modes = [t.failure_mode for t in report.trials]
    n_design = sum(m == "task_design_failure" for m in modes)
    n_env = sum(m == "environment_failure" for m in modes)
    if not n_design and not n_env:
        if expected_trial_ids:
            covered = {t.trial_id for t in report.trials}
            missing = [tid for tid in expected_trial_ids if tid not in covered]
            if missing:
                return {"verdict": "revise",
                        "reason": f"deep audit covered {len(covered)}/{len(expected_trial_ids)} "
                                  f"failed trials (missing {missing[:3]}) — cannot confirm all-headroom "
                                  f"on partial coverage; re-audit"}
        return {"verdict": "keep_hard",
                "reason": f"deep audit: all {len(modes)} failure(s) are capability_headroom"}
    if n_design:
        extra = f" (+{n_env} environment_failure)" if n_env else ""
        return {"verdict": "ease",
                "reason": f"deep audit: {n_design} task_design_failure{extra} — ease the blockers"}
    return {"verdict": "revise",
            "reason": f"deep audit: {n_env} environment_failure — fix the environment/verifier"}
