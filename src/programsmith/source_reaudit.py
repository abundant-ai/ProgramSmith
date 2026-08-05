"""Auditable recovery for sources rejected before task generation.

The default operation is a free dry-run. It selects only high-confidence sources with a detected
executable and multiple documented offline/data-processing signals. Applying a plan resets those
sources to TASK_MATRIX, preserving the old decision files as backups; the normal driver then makes
a fresh evidence-dossier-backed decision.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .fsm import Stage
from .manifest import Manifest
from .source_screen import recommend_source_review, screen_source
from .state import RunState


_HARD_MISMATCH_SIGNALS = (
    " library", "framework", " sdk", "not a standalone", "no standalone", "demo/example",
    "interactive", " tui", "terminal ui", "full-screen", " gui", "system-tray",
    "requires authentication", "api client", "rest client", "remote service", "live server",
    "network",
    "no deterministic", "non-deterministic", "nondeterministic", "cannot be pinned",
    "no executable", "monorepo", "multi-binary", "written to disk", "binary output",
)
_DIFFICULTY_ONLY_SIGNALS = (
    "too small", "below the 5 kloc", "under the 5 kloc", "5 kloc floor", "5 kloc bar",
    "too easy", "thin surface", "few flags", "limited flag", "insufficient complexity",
    "insufficient surface", "expert-hour", "difficulty band", "calibration target",
)
_DOCUMENTED_ESCAPE_SIGNALS = {
    "stdout", "standard output", "non-interactive", "batch mode", "offline",
    "json output", "csv output",
}


def is_source_rejection(state: RunState) -> bool:
    return bool(
        state.status == "dropped"
        and state.history
        and state.history[-1].stage is Stage.TASK_MATRIX
        and state.history[-1].verdict == "none_selected"
    )


def audit_source_rejection(run_dir: str | Path) -> dict | None:
    run_dir = Path(run_dir)
    state = RunState.load(run_dir)
    if not is_source_rejection(state):
        return None
    manifest = Manifest.load(run_dir)
    screen = screen_source(manifest)
    review = recommend_source_review(manifest)
    old_reason = ""
    matrix_path = run_dir / "task_matrix.json"
    prior_decision = "deterministic_screen" if not matrix_path.exists() else "semantic_matrix"
    if matrix_path.exists():
        try:
            old_reason = str((json.loads(matrix_path.read_text()) or {}).get("no_candidate_reason") or "")
        except (OSError, json.JSONDecodeError):
            pass
    reason_l = f" {old_reason.lower()}"
    hard_mismatch = any(signal in reason_l for signal in _HARD_MISMATCH_SIGNALS)
    difficulty_only = any(signal in reason_l for signal in _DIFFICULTY_ONLY_SIGNALS)
    # Do not overturn a prior semantic hard-mismatch judgment using keyword counts. The safe
    # recovery set is: deterministic prefilter decisions (the known xan/goaccess class), old
    # full-profile difficulty-only decisions, or a missing/corrupt semantic explanation.
    warning_needs_escape = any(
        "TUI-only" in warning or "remote-service" in warning
        for warning in review.warnings
    )
    has_documented_escape = bool(_DOCUMENTED_ESCAPE_SIGNALS & set(review.positive_signals))
    safe_to_retry = (
        prior_decision == "deterministic_screen"
        or difficulty_only
        or not old_reason.strip()
    ) and not hard_mismatch
    if prior_decision == "deterministic_screen" and warning_needs_escape and not has_documented_escape:
        safe_to_retry = False
    decision = "retry" if review.decision == "review" and safe_to_retry else "keep_rejected"
    return {
        "key": run_dir.name,
        "repo": manifest.source.repo if manifest.source else None,
        "decision": decision,
        "review_score": review.score,
        "review_reasons": review.reasons,
        "positive_signals": review.positive_signals,
        "warnings": review.warnings,
        "old_reason": old_reason,
        "prior_decision": prior_decision,
        "hard_mismatch": hard_mismatch,
        "difficulty_only": difficulty_only,
        "documented_escape": has_documented_escape,
        "source_screen": screen.model_dump(),
    }


def build_reaudit_plan(runs_dir: str | Path) -> list[dict]:
    root = Path(runs_dir)
    items: list[dict] = []
    for run_dir in sorted(root.iterdir() if root.exists() else ()):
        if not run_dir.is_dir() or not (run_dir / "state.json").exists():
            continue
        try:
            item = audit_source_rejection(run_dir)
        except (FileNotFoundError, ValueError):
            continue
        if item:
            items.append(item)
    return sorted(items, key=lambda item: (-int(item["review_score"]), item["key"]))


def _backup(path: Path, suffix: str = ".pre-source-reaudit") -> None:
    if not path.exists():
        return
    backup = path.with_name(path.stem + suffix + path.suffix)
    if not backup.exists():
        shutil.copy2(path, backup)


def apply_reaudit(run_dir: str | Path, item: dict | None = None) -> None:
    """Reset one high-confidence source rejection to TASK_MATRIX, reversibly."""
    run_dir = Path(run_dir)
    item = item or audit_source_rejection(run_dir)
    if not item or item.get("decision") != "retry":
        raise ValueError(f"{run_dir.name} is not a high-confidence source re-audit candidate")

    state_path = run_dir / "state.json"
    matrix_path = run_dir / "task_matrix.json"
    drive_path = run_dir / "drive.json"
    source_screen_path = run_dir / "source_screen.json"
    jobs_path = run_dir / "jobs.json"
    for path in (state_path, matrix_path, drive_path, source_screen_path, jobs_path):
        _backup(path)

    state = RunState.load(run_dir)
    if not is_source_rejection(state):
        raise ValueError(f"{run_dir.name} is no longer a source rejection")
    state.history.pop()
    state.current_stage = Stage.TASK_MATRIX
    state.status = "in_progress"
    state.paused = False
    state.save(run_dir)

    manifest = Manifest.load(run_dir)
    manifest.source_screen = item["source_screen"]
    task_matrix_entry = (manifest.sweeps or {}).get("task_matrix")
    if task_matrix_entry is not None:
        manifest.sweeps.pop("task_matrix", None)
    manifest.save(run_dir)
    source_screen_path.write_text(json.dumps(item["source_screen"], indent=2) + "\n")

    matrix_path.unlink(missing_ok=True)
    drive_path.unlink(missing_ok=True)
    if jobs_path.exists():
        try:
            jobs = json.loads(jobs_path.read_text()) or {}
        except (OSError, json.JSONDecodeError):
            jobs = {}
        if isinstance(jobs, dict) and "task_matrix" in jobs:
            jobs.pop("task_matrix", None)
            jobs_path.write_text(json.dumps(jobs, indent=2) + "\n")
