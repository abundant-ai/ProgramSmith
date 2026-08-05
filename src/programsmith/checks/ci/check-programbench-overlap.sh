#!/bin/bash
# Check non-ProgramBench build drivers and tagged tasks for official PB overlap.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 "$ROOT/ci_checks/check-programbench-overlap.py" "$@"
