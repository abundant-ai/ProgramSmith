"""QA / PROBE — adversarial shortcut-finder on the frontier difficulty-sweep traces.

Launches probe trials that try to make the verifier reward without solving, audit the verifier for
bugs, and check task-construction soundness; then a DETERMINISTIC verdict routes `clean` | `harden`
on the validated findings (gate decides; the probe LLM only annotates).

Reuse basis: the operator probe presets (preset_{cheat_detector,verifier_critic,auditor}*.txt) +
the "Task Construction Auditor" auto-probe + the reward-hack-catalog. A live launch spends on the
operator's own key (spend-gated); the verdict logic + config build are unit-tested offline.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..gates import GateResult

# Probe presets to run (by role). Live launch maps these to the probe overlays.
PROBE_PRESETS = ("cheat-detector", "verifier-critic", "task-construction-auditor")

# Auditor verdicts that indicate the task is NOT cleanly solvable as written.
GAMEABLE_AUDITOR_VERDICTS = frozenset({"SOLVABLE_ONLY_BY_GUESSING", "UNSOLVABLE"})


class ProbeFinding(BaseModel):
    kind: str                              # cheat-detector | verifier-critic | task-construction-auditor
    exploit_found: bool = False            # cheat-detector achieved reward without solving
    reward: float | None = None            # reward the exploit obtained, if any
    catalog_id: str | None = None          # matched reward-hack-catalog entry (H1..H12), advisory
    auditor_verdict: str | None = None     # SOLVABLE_AS_WRITTEN | SOLVABLE_ONLY_BY_GUESSING | UNSOLVABLE
    detail: str = ""


def probe_verdict(findings: list[ProbeFinding]) -> GateResult:
    """`harden` iff any probe surfaced a reward-hack (an exploit that scored without solving) or the
    auditor judged the task gameable; otherwise `clean`. The matched catalog id is reported so the
    SYNTHESIZE patch + WORKFLOW_NOTES know what to close."""
    exploits = [f for f in findings if f.exploit_found and (f.reward or 0) > 0]
    gameable = [f for f in findings if f.auditor_verdict in GAMEABLE_AUDITOR_VERDICTS]
    if exploits or gameable:
        ids = sorted({f.catalog_id for f in exploits if f.catalog_id})
        verdicts = sorted({f.auditor_verdict for f in gameable if f.auditor_verdict})
        reason = "reward-hack found" + (f" (catalog {ids})" if ids else "")
        if verdicts:
            reason += f"; auditor: {verdicts}"
        return GateResult("harden", reason,
                          {"exploits": [f.model_dump() for f in exploits],
                           "gameable": [f.model_dump() for f in gameable]})
    return GateResult("clean", "no reward-hack found; auditor clean",
                      {"n_findings": len(findings)})


def build_probe_config(task_names: list[str], *, presets: tuple[str, ...] = PROBE_PRESETS,
                       environment: str = "local") -> dict:
    """Config for a probe sweep (one probe trial per preset per task). Live launch is spend-gated."""
    return {
        "environment": environment,
        "mode": "probe",
        "presets": list(presets),
        "task_names": list(task_names),
    }
