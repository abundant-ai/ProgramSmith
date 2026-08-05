"""Trial records + band math — the backend-neutral core of the sweep pipeline.

A *sweep* runs N solver trials (plus the deterministic oracle/nop baselines) against a task and
records one normalized trial per attempt: {agent, model, reward, is_probe, status}. This module owns
that record schema and everything computed over it: parsing pulled/produced trial artifacts
(`read_pulled_trials`), pass@1 per (agent, model) group (`pass_at_1`), the frontier-measurement
filter (`frontier_trials`), and the band reads the gates route on (`dual_family_band`).

Everything here is a pure function over on-disk JSON or in-memory dicts — no subprocess, no network,
no Docker. The local sweep engine (`sweepbackend.LocalSweepBackend` + `local_runner`) writes trial
records in exactly this schema; `programsmith sweep-read --from-pull` imports externally-produced trial
directories through the same readers. Launching/executing trials lives behind the `SweepBackend`
seam, never here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

OPUS = "anthropic/claude-opus-4-8"
GPT = "openai/gpt-5.5"
HAIKU = "anthropic/claude-haiku-4-5"
BASELINE_AGENTS = frozenset({"oracle", "nop"})


@dataclass(frozen=True)
class SweepAgent:
    name: str                      # harness name: claude-code | codex | mini-swe | oracle | nop | …
    model_name: str = "default"    # e.g. anthropic/claude-opus-4-8 ; "default" for oracle/nop
    n_trials: int = 1


def sanity_baseline_agents() -> list[SweepAgent]:
    """SANITY baseline — oracle + nop ONLY (no frontier model → cheap). Proves the verifier's
    oracle=1/nop=0 contract from recorded baseline trials when the full local gate hasn't run."""
    return [
        SweepAgent("oracle", "default", 1),
        SweepAgent("nop", "default", 1),
    ]


def auditor_probe_agents() -> list[SweepAgent]:
    """QA/PROBE — a single frontier trial running the Task Construction Auditor (the task's
    `instruction.md` is swapped for the auditor prompt; see programsmith.probes). No baselines: the
    auditor's own JSON verdict is the signal, not a reward. One trial on the frontier model."""
    return [SweepAgent("claude-code", OPUS, 1)]


def difficulty_sweep_agents(n_trials: int = 3) -> list[SweepAgent]:
    """DIFFICULTY = SMOKE SWEEP — a cheap smoke model ×3 (default) + oracle/nop baselines (the
    ADR-0040 ladder: a cheap smoke gates the expensive frontier). Three trials give a coarse pass@1
    (0, 1/3, 2/3, 1) rather than a single all-or-nothing coin flip, so CALIBRATE reads real spread;
    only 3/3 saturates the smoke band. Legacy CLI defaults — live runs build their agents from the
    per-run RunConfig (orchestrator._stage_sweep_agents); these mirror it."""
    return [
        SweepAgent("oracle", "default", 1),
        SweepAgent("nop", "default", 1),
        SweepAgent("claude-code", HAIKU, n_trials),
    ]


def full_sweep_agents(n_trials: int = 3) -> list[SweepAgent]:
    """FULL = FRONTIER SWEEP — the frontier model ×3 (default, the 1/3–2/3 target window) +
    oracle/nop ×3 baselines. The FINAL sweep runs the oracle/nop baselines 3× (not 1×) so a flaky
    verifier can't set the authoritative oracle=1/nop=0 contract off a single coin-flip trial.
    Legacy CLI defaults — live runs mirror this via the per-run RunConfig."""
    return [
        SweepAgent("oracle", "default", 3),
        SweepAgent("nop", "default", 3),
        SweepAgent("claude-code", OPUS, n_trials),
    ]


# ---- standardized sweep naming (no wall-clock timestamps) ------------------------------
# A sweep handle doubles as the experiment's on-disk directory name, so a re-measurement after a
# SYNTHESIZE patch must use a DISTINCT name or post-patch trials mix with pre-patch trials and
# pass@1 is computed over stale data. Uniqueness therefore comes from the run's GENERATION
# (harden+revise count — bumps on every patch+re-measure) and the per-stage error-retry ATTEMPT,
# both monotonic and meaningful, instead of a timestamp the operator can't read.

EXPERIMENT_PREFIX = "programsmith"


def experiment_name(slug: str, stage: str, *, generation: int = 0, attempt: int = 0) -> str:
    """Canonical, timestamp-free sweep name: `lh-<slug>-<stage>` for the first launch,
    `…-v<generation>` after a SYNTHESIZE patch re-measures the stage, `…-retry<attempt>` on an
    error re-launch. Stable enough to recognize, unique enough that two distinct measurements
    never fold into one experiment (see module note)."""
    name = f"{EXPERIMENT_PREFIX}-{slug}-{stage}"
    if generation:
        name += f"-v{generation}"
    if attempt:
        name += f"-retry{attempt}"
    return name


def probe_task_dirname(slug: str, *, generation: int = 0) -> str:
    """Directory name for the auditor-overlay probe task: `<slug>-audit`, plus `-v<generation>`
    after a patch so each generation is a visibly distinct probe bundle."""
    name = f"{slug}-audit"
    if generation:
        name += f"-v{generation}"
    return name


def build_sweep_config(task_names: list[str] | None, agents: list[SweepAgent]) -> dict:
    """Build a sweep-config dict (agents [+ optional task_names filter]). `task_names` filters a
    dataset; for a single task launch it must be omitted (pass None/[])."""
    cfg: dict = {
        "agents": [{"name": a.name, "model_name": a.model_name, "n_trials": a.n_trials}
                   for a in agents],
    }
    if task_names:
        cfg["task_names"] = list(task_names)
    return cfg


def read_trials(status_json: str | dict) -> list[dict]:
    """Parse a status-payload-shaped object ({"trials": [...]}) into the trials list (each: agent,
    model, reward, is_probe, status). Pure parser so `pass_at_1` is testable offline."""
    obj = json.loads(status_json) if isinstance(status_json, str) else status_json
    if isinstance(obj, dict):
        return obj.get("trials", []) or []
    return obj if isinstance(obj, list) else []


# ---- pulled-artifacts read path ---------------------------------------------------------
# The canonical per-trial record is a `result.json` under a trials directory. Two shapes are
# accepted: the NESTED harbor-style record (agent_info.name / agent_info.model_info.name /
# verifier_result.rewards.reward — the shape external trial exports carry) and the FLAT
# {agent, model, reward} shape the local sweep engine writes. A TOP-LEVEL result.json that is a job
# SUMMARY (a `stats`/`evals` block) is skipped. All shapes normalize to
# {agent, model, reward, is_probe, status}.

_AGENT_KEYS = ("agent", "agent_name", "harness")
_MODEL_KEYS = ("model", "model_name")
_REWARD_KEYS = ("reward", "score")


def _first(d: dict, keys: tuple[str, ...]):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _canon_agent(name: str | None) -> str:
    """Canonicalize a harness name back to the CATALOG key (runconfig.HARNESSES): closed-internet
    harness variants in imported artifacts carry an `-api-key-no-search` suffix — strip it — and
    mini-swe is often registered as "mini-swe-agent" — fold it to "mini-swe" so pass@1 grouping,
    band `basis` resolution (AgentSpec.group_key) and baseline-exclusion all match the catalog
    names. Inverse of `runconfig.sweep_agent_name` (the launch-path translation)."""
    if not name:
        return "?"
    name = name.replace("-api-key-no-search", "")   # suffix first: mini-swe-agent-api-key-… folds too
    return "mini-swe" if name == "mini-swe-agent" else name


def normalize_trial(result: dict, grade: dict | None = None) -> dict:
    """Normalize a per-trial `result.json` (+ optional `grade_result.json`) into the trial shape
    {agent, model, reward, is_probe, status}, across the nested harbor schema and the flat
    local/test schema. Reward is binary (verifier contract); a fractional `partial_score` is NOT a
    pass."""
    grade = grade or {}
    agent_info = result.get("agent_info")
    if agent_info is not None or "verifier_result" in result:
        # nested harbor-style per-trial record
        agent = _canon_agent((agent_info or {}).get("name"))
        model = ((agent_info or {}).get("model_info") or {}).get("name") or "default"
        reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
        if reward is None:
            reward = _first(grade, _REWARD_KEYS)
        errored = bool(result.get("exception_info"))
        return {"agent": agent, "model": model, "reward": reward,
                "is_probe": bool(result.get("is_probe", False)),
                "status": "errored" if errored else "completed"}

    # flat shape (local sweep records / tests)
    reward = _first(result, _REWARD_KEYS)
    if reward is None:
        reward = _first(grade, _REWARD_KEYS)
    if reward is None:
        ps = grade.get("partial_score", result.get("partial_score"))
        if ps in (0, 1, 0.0, 1.0):
            reward = ps
    return {
        "agent": _canon_agent(_first(result, _AGENT_KEYS)),
        "model": _first(result, _MODEL_KEYS) or "default",
        "reward": reward,
        "is_probe": bool(result.get("is_probe", False)),
        "status": result.get("status", grade.get("status", "unknown")),
    }


def _is_job_summary(obj: dict) -> bool:
    """A top-level trial `result.json` is a job summary (aggregate `stats`/`evals`), not a
    single-trial record — skip it so trials aren't double-counted."""
    return ("stats" in obj or "evals" in obj) and "agent_info" not in obj and "agent" not in obj


def read_pulled_trials(pull_dir: str | Path) -> list[dict]:
    """Walk a directory of trial artifacts (a local sweep's experiment dir, or an externally
    exported trial tree) and return the normalized trials list. Finds every `result.json`, skips
    job-summary files, merges a sibling `grade_result.json` when present."""
    root = Path(pull_dir)
    trials: list[dict] = []
    for result_path in sorted(root.rglob("result.json")):
        try:
            result = json.loads(result_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(result, dict) or _is_job_summary(result):
            continue
        grade_path = result_path.with_name("grade_result.json")
        grade = None
        if grade_path.exists():
            try:
                grade = json.loads(grade_path.read_text())
            except (OSError, json.JSONDecodeError):
                grade = None
        trials.append(normalize_trial(result, grade))
    return trials


_ANALYSIS_LABEL_KEYS = ("classification", "label", "analysis_label", "trial_classification", "verdict")


def read_pulled_analyses(pull_dir: str | Path, *, agents: tuple[str, ...] = ("codex",)) -> list[dict]:
    """Read trajectory-classifier labels from trial artifacts, restricted to the given agent
    families. Returns [{trial_id, label}]; EMPTY when the artifacts carry no analysis labels — so
    the caller blocks honestly rather than invent a verdict. Best-effort over the analysis schema:
    labels may sit top-level or under an `analysis`/`classification` block."""
    root = Path(pull_dir)
    out: list[dict] = []
    for result_path in sorted(root.rglob("result.json")):
        try:
            result = json.loads(result_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(result, dict) or _is_job_summary(result):
            continue
        agent = _canon_agent((result.get("agent_info") or {}).get("name") or _first(result, _AGENT_KEYS))
        if agents and agent not in agents:
            continue
        block = result.get("analysis") or result.get("classification")
        label = _first(block, _ANALYSIS_LABEL_KEYS) if isinstance(block, dict) else None
        if label is None:
            label = _first(result, _ANALYSIS_LABEL_KEYS)
        if label is None:
            continue
        tid = result.get("trial_id") or result_path.parent.name
        out.append({"trial_id": str(tid), "label": str(label)})
    return out


# The auditor emits a single-token verdict (e.g. "verdict": "SOLVABLE_AS_WRITTEN"). `\\?` tolerates
# backslash-escaped quotes (the report is often stored as a JSON string inside trajectory.json). The
# schema in the prompt has the pipe-delimited enum ("SOLVABLE_AS_WRITTEN|...|UNSOLVABLE"), which does
# NOT match (the closing quote isn't right after the first token) — so the prompt text in the pulled
# logs can't false-match the real verdict.
# `\\*` (any number of backslashes) — the report may sit N JSON-encoding levels deep: a plain
# text log escapes it once, a stream-json trajectory stored inside result.json escapes it twice
# (the tengo probe false-block: verdict emitted but unreadable at depth 2).
_AUDITOR_VERDICT_RE = re.compile(
    r'\\*"verdict\\*"\s*:\s*\\*"(SOLVABLE_AS_WRITTEN|SOLVABLE_ONLY_BY_GUESSING|UNSOLVABLE)\\*"')
_BLOCKER_RE = re.compile(r'\\*"severity\\*"\s*:\s*\\*"blocker\\*"')


def extract_auditor_verdict(pull_dir: str | Path) -> dict:
    """Scan a QA/PROBE trajectory directory for the Task Construction Auditor's JSON report and
    return {verdict, blockers, found}. `verdict` is the LAST single-token verdict the agent emitted
    (its final report); `blockers` counts `"severity": "blocker"` findings. `found` is False when no
    verdict is present, so the caller blocks for human review rather than inventing one."""
    root = Path(pull_dir)
    parts: list[str] = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in (".json", ".jsonl", ".txt", ".md", ".log", ".cast"):
            try:
                parts.append(p.read_text(errors="ignore"))
            except OSError:
                continue
    blob = "\n".join(parts)
    verdicts = _AUDITOR_VERDICT_RE.findall(blob)
    return {"verdict": verdicts[-1] if verdicts else None,
            "blockers": len(_BLOCKER_RE.findall(blob)), "found": bool(verdicts)}


def load_trials(source: str | dict | Path) -> list[dict]:
    """Read trials from whichever read path is available: a trial-artifacts directory, a
    status-payload JSON file/string, or an in-memory dict/list."""
    if isinstance(source, Path) or (isinstance(source, str) and Path(source).is_dir()):
        return read_pulled_trials(source)
    if isinstance(source, str) and Path(source).is_file():
        return read_trials(Path(source).read_text())
    return read_trials(source)


# A runnability PROBE trial (flagged `is_probe`) is NOT a difficulty measurement — but it can share
# the solver agent name, so without this filter it gets folded into the measured group and can
# BECOME the band (the minpack bug: a single failed probe read as pass@1=0.0, which CALIBRATE then
# "proceeded" on). A measurement trial is therefore any NON-baseline, NON-probe trial. The `is_probe`
# flag is authoritative — the local sweep engine records it explicitly on every trial and never
# injects probes of its own, so no model-name heuristic is applied (the configured smoke model may
# legitimately be a cheap one, e.g. Haiku, and its trials must count).


def _is_frontier_trial(t: dict) -> bool:
    return t.get("agent") not in BASELINE_AGENTS and not t.get("is_probe")


def frontier_trials(trials: list[dict]) -> list[dict]:
    """The real measurement set: drop the oracle/nop baselines AND any flagged probe trial. Used to
    decide whether a COMPLETED sweep actually measured anything — a completion carrying only
    baselines + a probe (every frontier trial errored, e.g. a cancelled sweep) measured nothing and
    must re-run, not finalize a band over the probe. Mirrors pass_at_1's own exclusions."""
    return [t for t in trials if _is_frontier_trial(t)]


def _canon_model(model) -> str:
    """Fold a model reference to its bare id so a sweep-config name matches whatever a runner
    renamed it to in the trial record: drop a `provider/` prefix (`anthropic/claude-opus-4-8`,
    `zai/glm-5.2`) and a Bedrock inference-profile prefix (`global.anthropic.claude-opus-4-8`,
    `us.anthropic.…` — the shape cloud workers rewrite Claude references to). Comparison is
    prefix-tolerant in `_model_matches` (a dated Bedrock id like `…claude-haiku-4-5-20251001-v1:0`
    still matches the dateless config id)."""
    s = str(model or "").strip().lower()
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    if "anthropic." in s:
        s = s.rsplit("anthropic.", 1)[-1]
    return s


def _model_matches(spec_model, trial_model) -> bool:
    a, b = _canon_model(spec_model), _canon_model(trial_model)
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))


def scope_trials(trials: list[dict], agents) -> list[dict]:
    """Restrict a results read to THIS sweep's own agents (+ the oracle/nop baselines, which the
    integrity check needs), BEFORE any reader touches them — validity guard, finalize/pass@1,
    goodfail labels. The upstream pb10 false-DONE chokepoint: a task-scoped pull carried ANOTHER
    stage's completed smoke trials, so a full sweep whose EVERY frontier trial errored at launch
    still finalized pass@1=0.0 over foreign data and sailed through QA_GATE to a false DONE.
    The local engine writes per-experiment dirs (no co-pull path), but the same seam admits
    imported trial trees (`sweep-read --from-pull`) and injected backends — scoping at the one
    place results enter the state machine makes the all-frontier-trials-errored guard fire
    honestly: no measurement → re-run → bounded hard-block, never a silent band over foreign trials.

    `agents` is the launch list (SweepAgent-shaped: .name/.model_name, sweep-registered names).
    Matching is rename-tolerant: harness via `_canon_agent` on both sides, model via
    `_model_matches` (provider prefixes and Bedrock profile renames fold away). Baselines always
    pass through."""
    specs = [(a if isinstance(a, dict) else {"name": getattr(a, "name", None),
                                             "model_name": getattr(a, "model_name", None)})
             for a in (agents or [])]
    frontier_specs = [(_canon_agent(s.get("name")), s.get("model_name"))
                      for s in specs if s.get("name") not in BASELINE_AGENTS]
    out = []
    for t in trials:
        if t.get("agent") in BASELINE_AGENTS:
            out.append(t)
            continue
        agent = _canon_agent(t.get("agent"))
        if any(agent == sa and (sm in (None, "", "default") or _model_matches(sm, t.get("model")))
               for sa, sm in frontier_specs):
            out.append(t)
    return out


def pass_at_1(trials: list[dict]) -> dict:
    """Compute pass@1 per (agent, model) group, excluding oracle/nop baselines AND flagged probe
    trials. Returns {"groups": {...}, "aggregate": <float|None>}.
    The aggregate is the best (max) measured group — the same "solved if ANY configured agent solves"
    semantics as `runconfig.band_value(..., "aggregate")` — generic over whatever harness/model the
    run configured. When only baselines/probes measured, groups is EMPTY and the aggregate is
    honestly None (so CALIBRATE flags 'no usable band' instead of proceeding on a probe's 0.0 — the
    minpack bug stays fixed)."""
    groups: dict[str, dict] = {}
    for t in trials:
        if t.get("reward") is None:           # errored-incomplete trial → skip
            continue
        if not _is_frontier_trial(t):         # drop baselines + flagged probe trials
            continue
        key = f"{t.get('agent', '?')}@{t.get('model', '?')}"
        g = groups.setdefault(key, {"passes": 0, "n": 0})
        g["n"] += 1
        if t.get("reward") == 1.0 or t.get("reward") == 1:
            g["passes"] += 1
    for g in groups.values():
        g["pass_at_1"] = (g["passes"] / g["n"]) if g["n"] else None

    vals = [g["pass_at_1"] for g in groups.values() if g["pass_at_1"] is not None]
    return {"groups": groups, "aggregate": max(vals) if vals else None}


def family_band(trials: list[dict]) -> dict:
    """FULL SWEEP read, generic over N frontier families: the best pass@1 per family (harness) +
    the authoritative aggregate (the max across families — the task is "solved" if ANY family can)
    + `fairness_gap` = the MAX PAIRWISE |a − b| across measured families, flagging a family that's
    unfairly (dis)advantaged. With <2 families measured the gap is honestly None (a one-family
    sweep has no fairness signal). Consumed by QA GATE and the run-detail UI."""
    pa = pass_at_1(trials)
    families: dict[str, float] = {}
    for key, g in pa["groups"].items():
        agent = key.split("@", 1)[0]
        cur = families.get(agent)
        if g["pass_at_1"] is not None and (cur is None or g["pass_at_1"] > cur):
            families[agent] = g["pass_at_1"]
    vals = list(families.values())
    gap = (max(abs(a - b) for i, a in enumerate(vals) for b in vals[i + 1:])
           if len(vals) >= 2 else None)
    return {
        "families": families,
        "aggregate": max(vals) if vals else None,
        "fairness_gap": gap,
    }


def dual_family_band(trials: list[dict]) -> dict:
    """Legacy dual-family shape over `family_band`: the claude-code/codex fields older manifests
    and readers expect, plus the generic `families` map. `fairness_gap` is the generalized max
    pairwise gap (identical to |cc − cx| when exactly those two families measured)."""
    fb = family_band(trials)
    return {
        "claude_code": fb["families"].get("claude-code"),
        "codex": fb["families"].get("codex"),
        "families": fb["families"],
        "aggregate": fb["aggregate"],
        "fairness_gap": fb["fairness_gap"],
    }
