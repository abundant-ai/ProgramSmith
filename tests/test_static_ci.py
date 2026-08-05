"""Offline tests for the STATIC CI gate runner: aggregation, pass/fail verdict, and the
no-silent-pass rule for a missing check. Uses stub check scripts (not the real harbor-lh checks)."""

import os
import subprocess
from pathlib import Path

from programsmith.gates.static_ci import run_static_ci


def _fake_repo(tmp_path: Path, scripts: dict[str, int]) -> Path:
    ci = tmp_path / "ci_checks"
    ci.mkdir(parents=True)
    (tmp_path / "tasks" / "demo").mkdir(parents=True)
    for name, rc in scripts.items():
        s = ci / f"{name}.sh"
        s.write_text(f"#!/usr/bin/env bash\necho running {name} on $1\nexit {rc}\n")
        os.chmod(s, 0o755)
    return tmp_path


def test_all_checks_pass(tmp_path):
    repo = _fake_repo(tmp_path, {"check-a": 0, "check-b": 0})
    res = run_static_ci(repo, "tasks/demo", checks=("check-a", "check-b"))
    assert res.verdict == "pass"
    assert all(r["status"] == "pass" for r in res.detail["results"].values())


def test_one_check_fails(tmp_path):
    repo = _fake_repo(tmp_path, {"check-a": 0, "check-b": 1})
    res = run_static_ci(repo, "tasks/demo", checks=("check-a", "check-b"))
    assert res.verdict == "fail"
    assert "check-b" in res.reason
    assert res.detail["results"]["check-b"]["rc"] == 1


def test_missing_check_is_not_a_silent_pass(tmp_path):
    repo = _fake_repo(tmp_path, {"check-a": 0})
    res = run_static_ci(repo, "tasks/demo", checks=("check-a", "check-missing"))
    assert res.verdict == "fail"
    assert res.detail["results"]["check-missing"]["status"] == "missing"


def test_vendored_ci_dir_ships_the_full_check_order():
    """The gate defaults to the VENDORED in-tree suite (no external checkout needed): every script
    in CHECK_ORDER must exist there, plus the helpers the checks shell out to."""
    from programsmith.gates.static_ci import CHECK_ORDER, vendored_ci_dir
    ci = vendored_ci_dir()
    assert ci.is_dir()
    missing = [c for c in CHECK_ORDER if not (ci / f"{c}.sh").exists()]
    assert not missing, f"vendored suite is missing: {missing}"
    for helper in ("_anti_cheat_scan.py", "replay_agent.py", "check-programbench-overlap.py"):
        assert (ci / helper).exists(), helper


def test_vendored_checks_run_standalone_on_a_staged_task(tmp_path):
    """A staging root holding ONLY the vendored ci_checks + a task must be enough for the scripts to
    run (each takes the task relpath as its argument and resolves its own dir via $0) — no external
    checkout, no repo-internal files. Exercises a representative script subset for real."""
    import shutil

    from programsmith.gates.static_ci import run_static_ci, vendored_ci_dir
    staging = tmp_path / "staging"
    (staging / "tasks" / "demo").mkdir(parents=True)
    shutil.copytree(vendored_ci_dir(), staging / "ci_checks")
    task = staging / "tasks" / "demo"
    (task / "task.toml").write_text(
        "[metadata]\nnetwork_mode = \"none\"\n\n[environment]\nnetwork_mode = \"none\"\n")
    # closed-internet: a task with no public network_mode passes; overlap: a non-tagged task passes
    res = run_static_ci(staging, "tasks/demo",
                        checks=("check-closed-internet", "check-programbench-overlap"))
    assert res.detail["results"]["check-closed-internet"]["status"] == "pass"
    assert res.detail["results"]["check-programbench-overlap"]["status"] == "pass"


def test_file_reference_check_ignores_materialized_dot_files_fixture_trees(tmp_path):
    """A golden-I/O fixture may itself contain tests/test_*.py input data.

    The checker must not mistake a ``<case>.files`` materialization tree for a
    standalone task and require task.toml/solution files inside it.
    """
    from programsmith.gates.static_ci import vendored_ci_dir

    staging = tmp_path / "staging"
    fixture_test = (
        staging
        / "tasks/demo/tests/testsuite/fixtures/duplicate.files/tests/test_alpha.py"
    )
    fixture_test.parent.mkdir(parents=True)
    fixture_test.write_text('OUTPUT_PATH = "/app/result.json"\n')

    result = subprocess.run(
        [str(vendored_ci_dir() / "check-test-file-references.sh"), "tasks/demo"],
        cwd=staging,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    assert result.returncode == 0, result.stdout
    assert "task.toml not found" not in result.stdout
