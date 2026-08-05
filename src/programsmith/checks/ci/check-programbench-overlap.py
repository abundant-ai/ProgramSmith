#!/usr/bin/env python3
"""Fail if a non-pb task or build driver targets an official ProgramBench upstream.

Checks:
  1. resources/programbench-farm/build_*.py examples — repo= and upstream_repo= GitHub URLs
     (grandfathered slugs in programbench_grandfathered_slugs.txt are skipped)
  2. tasks/implement-*/instruction.md — canonical implements [...](url) line
     when task.toml tags include non-programbench (same grandfather list)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Guard-library resolution, most-specific first: a checkout that ships its own
# resources/programbench-farm/programbench_guard.py wins; otherwise the pipeline's vendored
# guard (programsmith.programbench.guard — the same canonical repo list). When neither is
# importable (a bare standalone checkout), the overlap check degrades to a LOUD no-op — the
# pipeline enforces the same guard structurally at INGEST, so nothing ships unguarded.
_FARM = ROOT / "resources" / "programbench-farm"
if _FARM.is_dir():
    sys.path.insert(0, str(_FARM))
    from programbench_guard import (  # noqa: E402
        GRANDFATHERED_SLUGS,
        is_programbench_upstream,
        parse_github_repo,
        programbench_entry_id,
        slug_from_build_driver,
    )
else:
    try:
        from programsmith.programbench.guard import (  # noqa: E402
            is_programbench_upstream,
            parse_github_repo,
            programbench_entry_id,
        )
    except ImportError:
        is_programbench_upstream = None  # type: ignore[assignment]
        parse_github_repo = programbench_entry_id = None  # type: ignore[assignment]
    GRANDFATHERED_SLUGS: frozenset[str] = frozenset()

    def slug_from_build_driver(path: Path) -> str | None:  # no build drivers outside the farm
        return None

_BUILD_REPO_RE = re.compile(
    r"""(?:repo|upstream_repo)\s*=\s*["']([^"']+)["']""",
)
_IMPLEMENTS_RE = re.compile(
    r"implements\s+\[`[^`]+`\]\((https://github\.com/[^)]+)\)",
    re.IGNORECASE,
)


def _check_build_drivers() -> list[str]:
    errors: list[str] = []
    for path in sorted((ROOT / "resources" / "programbench-farm").glob("build_*.py")):
        slug = slug_from_build_driver(path)
        if not slug or not (ROOT / "tasks" / slug).is_dir():
            continue
        if slug in GRANDFATHERED_SLUGS:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in _BUILD_REPO_RE.finditer(text):
            url = m.group(1)
            if not is_programbench_upstream(url):
                continue
            owner, repo = parse_github_repo(url)
            errors.append(
                f"{path.relative_to(ROOT)}: {owner}/{repo} overlaps ProgramBench "
                f"({programbench_entry_id(owner, repo)}*)"
            )
    return errors


def _task_has_non_pb_tag(task_dir: Path) -> bool:
    toml = task_dir / "task.toml"
    if not toml.is_file():
        return False
    return "non-programbench" in toml.read_text(encoding="utf-8")


def _check_task(task_dir: Path) -> list[str]:
    slug = task_dir.name
    if slug in GRANDFATHERED_SLUGS:
        return []
    if not _task_has_non_pb_tag(task_dir):
        return []
    instr = task_dir / "instruction.md"
    if not instr.is_file():
        return [f"{task_dir.relative_to(ROOT)}: missing instruction.md"]
    text = instr.read_text(encoding="utf-8", errors="replace")
    m = _IMPLEMENTS_RE.search(text)
    if not m:
        return [
            f"{task_dir.relative_to(ROOT)}: no implements [...](github) line in instruction.md"
        ]
    url = m.group(1)
    if not is_programbench_upstream(url):
        return []
    owner, repo = parse_github_repo(url)
    return [
        f"{task_dir.relative_to(ROOT)}: {owner}/{repo} overlaps ProgramBench "
        f"({programbench_entry_id(owner, repo)}*)"
    ]


def main(argv: list[str]) -> int:
    if is_programbench_upstream is None:
        # Degraded standalone mode (see the import note): the INGEST-time guard already enforced
        # the overlap ban; report the skip loudly instead of failing every staged task.
        print("SKIPPED: programbench guard library unavailable (overlap enforced at INGEST)")
        return 0
    errors: list[str] = []
    errors.extend(_check_build_drivers())

    if argv:
        task_dirs = [Path(a) for a in argv]
    else:
        task_dirs = sorted((ROOT / "tasks").glob("implement-*"))

    for task_dir in task_dirs:
        if task_dir.is_dir():
            errors.extend(_check_task(task_dir))

    if not errors:
        print("OK: no ProgramBench upstream overlap detected")
        return 0

    print("ProgramBench overlap detected:", file=sys.stderr)
    for err in errors:
        print(f"  {err}", file=sys.stderr)
    print(
        "\nUse an upstream not listed in resources/programbench-farm/programbench_repos.txt.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
