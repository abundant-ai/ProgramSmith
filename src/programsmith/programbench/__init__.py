"""Vendored ProgramBench task-farm generator (ADR-0038, invariant #3).

Basis: harbor-lh/resources/programbench-farm@origin/main (task_generator.py, _build_helpers.py,
programbench_guard.py, _check_determinism.py). Adapted minimally for in-process pipeline use:
explicit `workspace` params instead of the HARBOR_LH_WORKSPACE env-global, library exceptions
instead of SystemExit, and the mini-swe pre-install Dockerfile block (ADR-0043). ALL anti-cheat
tiers, templates, and constants are kept verbatim from the farm — this package is the single
source of the ProgramBench task-layout contract.

Modules:
  generator      — build_task(): emit a complete implement-<tool> task dir (the big template)
  build_helpers  — shallow_clone / patch_version_string / ensure_oracle_pair / collect_docs
  guard          — check_not_programbench(): refuse official-ProgramBench upstreams
  fixtures       — build_test_fixtures + STERILIZED_ENV + check_determinism
  data/          — programbench_repos.txt (the 204-repo official upstream list)
"""

from .fixtures import STERILIZED_ENV, build_test_fixtures, check_determinism  # noqa: F401
from .generator import build_task  # noqa: F401
from .guard import check_not_programbench, is_programbench_upstream, parse_github_repo  # noqa: F401
