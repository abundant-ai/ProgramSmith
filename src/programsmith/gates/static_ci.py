"""STATIC CI gate — replay the 18-script CHECK_ORDER (ADR-0007b).

The gate runs each `bash ci_checks/<check>.sh <task_relpath>` from a staging root; green iff every
check exits 0. Anti-hack backstops (closed-internet, asset-encryption, anti-cheat-soundness,
reviewer-visible-tests, dockerfile-references) are part of this set.

The check suite ships VENDORED in-tree (`programsmith/checks/ci/` — `vendored_ci_dir()`), so a bare
install replays the frozen set with no external checkout. An operator can point the gate at a
different suite via `PROGRAMSMITH_CI_REPO_ROOT` / `--ci-repo-root` / config `ci_repo_root` (a directory whose
`ci_checks/` is replayed instead).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import GateResult


def vendored_ci_dir() -> Path:
    """The in-tree check suite (shipped as package data): `programsmith/checks/ci/`."""
    return Path(__file__).resolve().parent.parent / "checks" / "ci"

# Verbatim from static-checks.yml CHECK_ORDER (DISCOVERY.md §B). Order preserved.
CHECK_ORDER: tuple[str, ...] = (
    "check-dockerfile-base-image",
    "check-dockerfile-references",
    "check-dockerfile-sanity",
    "check-task-absolute-path",
    "check-test-file-references",
    "check-test-sh-sanity",
    "check-task-fields",
    "check-task-resources",
    "check-timer",
    "check-closed-internet",
    "check-solution-format",
    "check-reward-format",
    "check-metrics-partial-score",
    "check-anti-cheat-soundness",
    "check-asset-encryption",
    "check-reviewer-visible-tests",
    "check-artifacts",
    "check-programbench-overlap",
)


def run_static_ci(
    repo_root: Path,
    task_relpath: str,
    *,
    checks: tuple[str, ...] = CHECK_ORDER,
    timeout: int = 300,
) -> GateResult:
    """Run the static gate. `repo_root` is a directory with a `ci_checks/` subdir (the staged
    vendored suite, or an operator-supplied checkout); `task_relpath` is the task dir relative to
    it (e.g. 'tasks/minpack-rust-rewrite'). Each check runs with cwd=repo_root. Verdict is `pass`
    iff every check exits 0."""
    repo_root = Path(repo_root)
    ci_dir = repo_root / "ci_checks"
    results: dict[str, dict] = {}
    all_pass = True

    for check in checks:
        script = ci_dir / f"{check}.sh"
        if not script.exists():
            results[check] = {"status": "missing", "rc": None}
            all_pass = False  # a missing required check is a failure (no silent pass)
            continue
        proc = subprocess.run(
            ["bash", str(script), task_relpath],
            cwd=repo_root, capture_output=True, text=True, timeout=timeout,
        )
        ok = proc.returncode == 0
        all_pass = all_pass and ok
        results[check] = {
            "status": "pass" if ok else "fail",
            "rc": proc.returncode,
            "tail": (proc.stdout + proc.stderr).strip()[-300:] if not ok else "",
        }

    failed = [c for c, r in results.items() if r["status"] != "pass"]
    verdict = "pass" if all_pass else "fail"
    reason = (f"all {len(checks)} static checks green" if all_pass
              else f"{len(failed)}/{len(checks)} failed: {failed}")
    return GateResult(verdict, reason, {"results": results})
