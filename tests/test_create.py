"""Offline tests for the rewritten CREATE cell (ProgramBench genre): deterministic assembly via
the vendored generator, the TaskCopy one-shot (injected/cached — never a live LLM here), the
oracle-bundle contract errors, and the agentic REPAIR loop interface. The oracle bundle uses
/bin/echo (host-runnable) captured through the REAL fixtures pipeline."""

import json
import shutil

import pytest

from programsmith.cells.create import (
    CANARY_GUID,
    TEMPLATE_PACK_VERSION,
    TaskCopy,
    assemble_skeleton,
    fill_prompt,
)
from programsmith.llm import CellError
from programsmith.manifest import Dimensions, Manifest, SourceInfo
from programsmith.programbench.fixtures import build_test_fixtures

pytestmark = pytest.mark.skipif(
    shutil.which("gpg") is None,
    reason="assemble_skeleton drives build_task, which gpg-encrypts the private bundle",
)


def _task_copy() -> TaskCopy:
    return TaskCopy(
        invocation_paragraph=(
            "Your binary echoes stdin to stdout. Example: `echo hi | echotool`. Probe "
            "`/app/oracle_binary/echotool --help` and exercise the reference binary to learn "
            "its exact output formatting."
        ),
        description_short="Reimplement 'echotool' — a line filter — from scratch. "
                          "ProgramBench-tier, ~6 kLOC c.",
        difficulty_explanation="The agent must reimplement (a) identity echo; (b) flag errors. "
                               "The grader exercises 2 cases over stdin including exit codes.",
        solution_explanation_flags="-O3 -s vs -O2 -g",
        domain_tags=["text", "filters", "unix"],
        expert_hours=14,
    )


def _manifest(tmp_path, *, with_dims_fields: bool = True) -> Manifest:
    m = Manifest(run_id="r", task_identity="task:abc", slug="implement-echotool")
    m.source = SourceInfo(repo="example/echotool", pinned_sha="deadbeefcafe1234",
                          repo_url="https://github.com/example/echotool",
                          primary_language="C", license="MIT", license_class="permissive",
                          size_loc=6000)
    m.dimensions = Dimensions()
    if with_dims_fields:
        # The ProgramBench dimension fields land in manifest.Dimensions in a concurrent change;
        # create.py reads them DEFENSIVELY via getattr, so simulate their presence directly on
        # the instance (pydantic v2 keeps fields in __dict__, so getattr sees these).
        for k, v in {"tool_name": "echotool", "binary_name": "echotool",
                     "upstream_language": "c", "flag_surface": "stdin echo",
                     "case_families": ["identity"], "est_kloc": 6,
                     "needs_files_dir": False, "expected_difficulty": "moderate",
                     "expert_hours": 14}.items():
            object.__setattr__(m.dimensions, k, v)
    # ---- oracle bundle (DESIGN §6.4), captured through the REAL fixture pipeline ----
    bundle_dir = tmp_path / "bundle"
    docs = bundle_dir / "docs"
    docs.mkdir(parents=True)
    (docs / "help.txt").write_text("usage: echotool\n")
    (docs / "version.txt").write_text("echotool 1.0-nonpb\n")
    (docs / "README.md").write_text("# echotool\n")
    suite = bundle_dir / "testsuite"
    build_test_fixtures(
        [{"id": "identity", "args": [], "stdin": "hi\n"},
         {"id": "version", "args": ["--version"], "stdin": ""}],
        "/bin/echo", suite,
    )
    m.oracle = {
        "oracle_bin": "/bin/echo", "prebuilt_bin": "/bin/cat",
        "docs_dir": str(docs), "cases_json": str(suite / "cases.json"),
        "fixtures_dir": str(suite / "fixtures"),
        "oracle_sha256": "0" * 64, "prebuilt_sha256": "1" * 64,
        "text_distinct": True, "n_cases": 2,
    }
    return m


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    # author fields come from LhConfig — point at a nonexistent tmp config so the defaults
    # (ProgramSmith / august@abundant.ai / Abundant AI) apply, never the operator's real file
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "cfg.json"))


def test_assemble_emits_complete_programbench_task(tmp_path):
    out_dir = tmp_path / "task" / "implement-echotool"
    out = assemble_skeleton(_manifest(tmp_path), out_dir, task_copy=_task_copy())
    assert out.template_pack_version == TEMPLATE_PACK_VERSION == "programbench-v1"
    assert out.todos == []          # deterministic generation is COMPLETE — no fill points
    for rel in ("task.toml", "instruction.md", "environment/Dockerfile",
                "environment/docker-compose.yaml",
                "environment/binary/echotool", "environment/private_bundle.tar.gz.gpg",
                "tests/test.sh", "tests/verify.py", "tests/testsuite/cases.json",
                "solution/solve.sh"):
        assert (out_dir / rel).exists(), rel
    # the ORACLE-stage capture was copied verbatim (no oracle re-exec at CREATE)
    cases = json.loads((out_dir / "tests/testsuite/cases.json").read_text())
    assert [c["id"] for c in cases] == ["identity", "version"]
    assert out.declarations.verifier_timeout_sec == 1500.0
    assert out.declarations.storage_mb == 16384
    assert out.verifier_mechanism == "golden-io"


def test_free_form_tool_name_never_reaches_paths(tmp_path):
    """Regression (pb10 docker-build failures): TASK MATRIX's free-form tool_name
    ('jc (system-commands parsers)', 'templ generate') was used as a FILENAME, emitting
    `COPY binary/jc (system-commands parsers) ...` in the Dockerfile. All path/executable slots
    must use the clean binary_name token; the free-form name survives as prose only."""
    m = _manifest(tmp_path)
    object.__setattr__(m.dimensions, "tool_name", "echotool (core filters)")
    object.__setattr__(m.dimensions, "binary_name", "echotool")
    out_dir = tmp_path / "task" / "implement-echotool"
    out = assemble_skeleton(m, out_dir, task_copy=_task_copy())
    assert (out_dir / "environment" / "binary" / "echotool").exists()
    df = (out_dir / "environment" / "Dockerfile").read_text()
    assert "COPY binary/echotool " in df and "(core filters)" not in df
    instr = (out_dir / "instruction.md").read_text()
    assert "/app/oracle_binary/echotool" in instr and "(core filters)" not in instr
    assert out.files["environment/binary/echotool"]


def test_assemble_refuses_bundle_inside_task_dir(tmp_path):
    """Regression (pb10 bundle wipe): build_task rmtree's out_dir first, so an oracle bundle that
    lives INSIDE the task dir would be destroyed by the very assembly that consumes it. CREATE
    must refuse up-front with a clear CellError (→ honest blocked halt), spending nothing and
    deleting nothing."""
    m = _manifest(tmp_path)
    out_dir = tmp_path / "task" / "implement-echotool"
    # relocate the bundle's oracle_bin into the task dir to simulate the collision
    out_dir.mkdir(parents=True)
    colliding = out_dir / "oracle_bin"
    colliding.write_bytes(b"ORACLE")
    m.oracle = {**m.oracle, "oracle_bin": str(colliding)}
    with pytest.raises(CellError, match="inside the task dir"):
        assemble_skeleton(m, out_dir, task_copy=_task_copy())
    assert colliding.exists()   # nothing was deleted


def test_task_toml_canary_authors_and_constants(tmp_path):
    out_dir = tmp_path / "task" / "implement-echotool"
    assemble_skeleton(_manifest(tmp_path), out_dir, task_copy=_task_copy())
    toml = (out_dir / "task.toml").read_text()
    assert CANARY_GUID in toml
    assert 'author_name = "ProgramSmith"' in toml
    assert 'author_email = ""' in toml
    assert 'author_organization = "ProgramSmith"' in toml
    assert 'difficulty = "hard"' in toml and 'category = "reverse-engineering"' in toml
    assert "timeout_sec = 1500.0" in toml and "timeout_sec = 18000.0" in toml
    assert "allow_internet = false" in toml and "expert_time_estimate_hours = 14" in toml
    assert toml.count('"binary-reverse-engineering"') == 1   # farm dup quirk NOT reproduced
    for t in ('"cli-reimplementation"', '"non-programbench"', '"c"', '"text"'):
        assert t in toml, t


def test_instruction_is_rigid_template_with_copy_slots(tmp_path):
    out_dir = tmp_path / "task" / "implement-echotool"
    assemble_skeleton(_manifest(tmp_path), out_dir, task_copy=_task_copy())
    instr = (out_dir / "instruction.md").read_text()
    assert instr.startswith("# Reimplement `echotool` from scratch")
    assert "https://github.com/example/echotool" in instr and "deadbeefca" in instr
    assert "## Invocation" in instr and "Your binary echoes stdin" in instr
    assert "You have 5 hours. Run `bash /app/timer.sh`" in instr
    assert "The grader runs many black-box test cases." in instr
    # toolchain line matches the Dockerfile (farm's Go+Rust-always-present quirk fixed)
    df = (out_dir / "environment/Dockerfile").read_text()
    assert "the standard C/C++ toolchain (build-essential)" in instr
    assert "C/C++ toolchain already present (build-essential)" in df
    assert "Rust 1.84" not in instr


def test_dockerfile_mini_swe_and_base(tmp_path):
    out_dir = tmp_path / "task" / "implement-echotool"
    assemble_skeleton(_manifest(tmp_path), out_dir, task_copy=_task_copy())
    df = (out_dir / "environment/Dockerfile").read_text()
    assert df.splitlines()[1] == "FROM ubuntu:24.04"
    assert "https://astral.sh/uv/0.7.13/install.sh" in df
    assert "uv tool install mini-swe-agent" in df
    assert 'ENV PATH="/root/.local/bin:${PATH}"' in df
    compose = (out_dir / "environment/docker-compose.yaml").read_text()
    assert "main:" in compose and "platform: linux/amd64" in compose


def test_passphrase_is_deterministic_per_slug(tmp_path):
    import hashlib
    out_dir = tmp_path / "task" / "implement-echotool"
    assemble_skeleton(_manifest(tmp_path), out_dir, task_copy=_task_copy())
    expected = "ngn-echotool-" + hashlib.sha256(b"implement-echotool").hexdigest()[:30]
    assert f"PASSPHRASE='{expected}'" in (out_dir / "tests/test.sh").read_text()
    assert f"PASSPHRASE='{expected}'" in (out_dir / "solution/solve.sh").read_text()


def test_taskcopy_one_shot_cached_across_reassembly(tmp_path):
    """No injected copy: the TaskCopy cell runs ONCE through the (injected) runner and is
    cached OUTSIDE the task dir, so re-assembly (the orchestrator's bg _produce path) never
    re-bills the model."""
    m = _manifest(tmp_path)
    out_dir = tmp_path / "task" / "implement-echotool"
    calls = []

    def runner(prompt: str) -> str:
        calls.append(prompt)
        return _task_copy().model_dump_json()

    assemble_skeleton(m, out_dir, runner=runner)
    assert len(calls) == 1
    assert "TASK COPY cell" in calls[0] and "echotool" in calls[0]
    cache = out_dir.parent / ".taskcopy-implement-echotool.json"
    assert cache.exists()
    assemble_skeleton(m, out_dir, runner=runner)   # re-assembly: cache hit, no second call
    assert len(calls) == 1
    assert (out_dir / "task.toml").exists()


def test_missing_oracle_bundle_raises_clear_cellerror(tmp_path):
    m = _manifest(tmp_path)
    m.oracle = {"reference_port_ref": "/old/legacy"}   # pre-ADR-0038 legacy shape
    with pytest.raises(CellError, match="oracle_bin"):
        assemble_skeleton(m, tmp_path / "task" / "implement-echotool", task_copy=_task_copy())
    m.oracle = None
    with pytest.raises(CellError, match="ORACLE_GOLDEN bundle"):
        assemble_skeleton(m, tmp_path / "task" / "implement-echotool", task_copy=_task_copy())


def test_fallbacks_without_new_dimension_fields(tmp_path):
    """Legacy manifests (Dimensions without the ProgramBench fields) still assemble: tool falls
    back to the slug, language to the detected source language."""
    m = _manifest(tmp_path, with_dims_fields=False)
    out_dir = tmp_path / "task" / "implement-echotool"
    out = assemble_skeleton(m, out_dir, task_copy=_task_copy())
    assert out.todos == []
    instr = (out_dir / "instruction.md").read_text()
    assert instr.startswith("# Reimplement `echotool` from scratch")  # slug-derived tool name
    assert "upstream is c" in instr                                    # C source → c axis


# ---- agentic repair loop (session + validator injected, offline) ----------------------

def test_fill_prompt_is_repair_brief_with_anti_cheat_constraints(tmp_path):
    m = _manifest(tmp_path)
    p = fill_prompt(m, tmp_path / "task" / "implement-echotool", [])
    assert "oracle=1, nop=0" in p
    assert "Tier-9" in p and "anti-cheat" in p
    assert "FROM ubuntu:24.04" in p and "mini-swe pre-install" in p
    assert "check-task-absolute-path" in p
    assert "REMOVE that\n  case" in p or "REMOVE that" in p  # broken-golden rule: drop, never hand-edit


def test_agentic_fill_drives_repair_loop(tmp_path):
    from programsmith.cells.agentic import ValidationState
    from programsmith.cells.create import agentic_fill
    m = _manifest(tmp_path)
    out_dir = tmp_path / "task" / "implement-echotool"
    assemble_skeleton(m, out_dir, task_copy=_task_copy())
    seen = []
    res = agentic_fill(
        m, out_dir,
        session=lambda prompt, td: seen.append(prompt) or "ok",
        validator=lambda _d: ValidationState(True, True),
        max_iters=2,
    )
    assert res.success and res.iterations == 1
    assert seen and "repair" in seen[0].lower()
    assert "(none — repair per validator feedback)" in seen[0]   # no TODO fill-points exist
