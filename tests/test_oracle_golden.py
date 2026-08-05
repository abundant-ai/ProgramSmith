"""Offline tests for the ORACLE + GOLDEN cell (ProgramBench oracle-pair bundle, ADR-0038):
the deterministic adopt gate (completeness + case floor + determinism marker + distinct pair),
the exact manifest.oracle key contract, the farm-recipe generate prompt, and legacy rewrite-port
bundle back-compat (deprecated epsilon machinery still exported)."""

import hashlib
import json

import pytest
from pydantic import ValidationError

from programsmith.cells.oracle_golden import (
    MIN_CASES,
    MINPACK_EPSILON,
    EpsilonField,
    adopt_existing,
    bundle_paths,
    generate_prompt,
    validate_epsilon,
)
from programsmith.manifest import Dimensions, Manifest, SourceInfo


# ---- ProgramBench bundle fixtures ------------------------------------------------------

def _elf_amd64(tail: bytes) -> bytes:
    """A minimal linux/amd64 ELF header (magic + ELFCLASS64 + LE + e_machine=EM_X86_64) with a
    distinguishing tail — enough to satisfy the arch gate and the whole-file distinctness checks."""
    return (b"\x7fELF" + bytes([2, 1, 1]) + bytes(9)          # ident: 64-bit, little-endian
            + (2).to_bytes(2, "little")                        # e_type = EXEC
            + (0x3E).to_bytes(2, "little")                     # e_machine = EM_X86_64
            + bytes(44)                                        # rest of the 64-byte header (zeroed)
            + tail)


ORACLE_ELF = _elf_amd64(b"ORACLE-ELF-BYTES")
PREBUILT_ELF = _elf_amd64(b"PREBUILT-ELF-BYTES")


def _make_pb_bundle(tmp_path, n_cases=70, *, marker=True, distinct=True, prebuilt=True):
    b = tmp_path / "bundle"
    (b / "docs").mkdir(parents=True)
    (b / "docs" / "help.txt").write_text("usage: tool [flags]")
    (b / "docs" / "version.txt").write_text("tool 1.0-nonpb")
    (b / "oracle_bin").write_bytes(ORACLE_ELF)
    if prebuilt:
        (b / "prebuilt_bin").write_bytes(ORACLE_ELF if not distinct else PREBUILT_ELF)
    ts = b / "testsuite"
    (ts / "fixtures").mkdir(parents=True)
    cases = [{"id": f"case-{i}", "args": [], "in": f"case-{i}.in",
              "expected_stdout": f"case-{i}.expected.stdout", "expected_rc": 0}
             for i in range(n_cases)]
    (ts / "cases.json").write_text(json.dumps(cases))
    for i in range(n_cases):
        (ts / "fixtures" / f"case-{i}.expected.stdout").write_text(f"result {i}\n")
    if marker:
        (b / "determinism.ok").write_text("")
    return b


def test_adopt_pb_bundle_populates_exact_manifest_oracle_keys(tmp_path):
    b = _make_pb_bundle(tmp_path, n_cases=101)
    m = Manifest(run_id="r", task_identity="task:abc")
    out, res = adopt_existing(m, b)
    assert res.verdict == "pass"
    assert out.approach == "oracle-pair" and out.n_cases == 101
    # DESIGN §6.4: manifest.oracle gets EXACTLY the nine bundle keys
    assert set(m.oracle) == {"oracle_bin", "prebuilt_bin", "docs_dir", "cases_json",
                             "fixtures_dir", "oracle_sha256", "prebuilt_sha256",
                             "text_distinct", "n_cases"}
    assert m.oracle["n_cases"] == 101 and m.oracle["text_distinct"] is True
    assert m.oracle["oracle_sha256"] == hashlib.sha256(ORACLE_ELF).hexdigest()
    assert m.oracle["prebuilt_sha256"] == hashlib.sha256(PREBUILT_ELF).hexdigest()
    p = bundle_paths(b)
    assert m.oracle["cases_json"] == str(p["cases_json"])
    assert m.oracle["fixtures_dir"] == str(p["fixtures_dir"])


def test_adopt_pb_bundle_fails_below_case_floor(tmp_path):
    b = _make_pb_bundle(tmp_path, n_cases=MIN_CASES - 1)
    m = Manifest(run_id="r", task_identity="task:abc")
    _out, res = adopt_existing(m, b)
    assert res.verdict == "fail" and "too thin" in res.reason
    assert m.oracle is None


def test_adopt_pb_bundle_rejects_case_count_padding_with_repeated_outputs(tmp_path):
    b = _make_pb_bundle(tmp_path, n_cases=103)
    for expected in (b / "testsuite" / "fixtures").glob("*.expected.stdout"):
        expected.write_text("usage: tool [flags]\n")
    m = Manifest(run_id="r", task_identity="task:abc")
    _out, res = adopt_existing(m, b)
    assert res.verdict == "fail"
    assert "observable behavior diversity" in res.reason
    assert "help/version/validation/error" in res.reason
    assert m.oracle is None


def test_adopt_pb_bundle_rejects_missing_expected_stdout_fixture(tmp_path):
    b = _make_pb_bundle(tmp_path, n_cases=101)
    (b / "testsuite" / "fixtures" / "case-0.expected.stdout").unlink()
    m = Manifest(run_id="r", task_identity="task:abc")
    _out, res = adopt_existing(m, b)
    assert res.verdict == "fail" and "outcomes are unreadable" in res.reason
    assert m.oracle is None


def test_adopt_pb_bundle_rejects_non_linux_amd64_binaries(tmp_path):
    """Regression (pb10 SANITY wipe-out): a native macOS (Mach-O arm64) oracle pair LOOKS complete
    on disk but can never exec in the linux/amd64 grading container — every SANITY oracle trial
    failed. The adopt gate must treat a wrong-format binary as MISSING, naming the requirement."""
    b = _make_pb_bundle(tmp_path, n_cases=101)
    (b / "oracle_bin").write_bytes(b"\xcf\xfa\xed\xfe" + bytes(60) + b"MACH-O-ARM64")  # Mach-O magic
    m = Manifest(run_id="r", task_identity="task:abc")
    _out, res = adopt_existing(m, b)
    assert res.verdict == "fail" and "linux/amd64 ELF" in res.reason
    assert m.oracle is None


def test_adopt_pb_bundle_fails_without_determinism_marker(tmp_path):
    b = _make_pb_bundle(tmp_path, marker=False)
    m = Manifest(run_id="r", task_identity="task:abc")
    _out, res = adopt_existing(m, b)
    assert res.verdict == "fail" and "determinism" in res.reason
    assert m.oracle is None


def test_adopt_pb_bundle_fails_on_missing_prebuilt(tmp_path):
    b = _make_pb_bundle(tmp_path, prebuilt=False)
    m = Manifest(run_id="r", task_identity="task:abc")
    _out, res = adopt_existing(m, b)
    assert res.verdict == "fail" and "prebuilt_bin" in res.reason
    assert m.oracle is None


def test_adopt_pb_bundle_refuses_byte_identical_pair(tmp_path):
    # A non-distinct pair defeats anti-cheat layer 5 / Tier-9 (the legit solver ships the prebuilt)
    # — hard fail. A byte-identical pair also has an identical .text section, so the .text check
    # (which subsumes the whole-file case and matches what the generated test.sh actually compares)
    # fires first; either rejection reason is acceptable so long as the bundle is refused.
    b = _make_pb_bundle(tmp_path, distinct=False)
    m = Manifest(run_id="r", task_identity="task:abc")
    _out, res = adopt_existing(m, b)
    assert res.verdict == "fail" and ("byte-identical" in res.reason or ".text" in res.reason)
    assert m.oracle is None


# ---- legacy rewrite-port bundle (back-compat) -------------------------------------------

def _make_legacy_bundle(tmp_path):
    b = tmp_path / "minpack-crate"
    (b / "oracle" / "src").mkdir(parents=True)
    (b / "oracle" / "Cargo.toml").write_text("[package]\nname='oracle'\n")
    (b / "goldens").mkdir(parents=True)
    (b / "goldens" / "goldens_public.json").write_text(json.dumps([{"id": "a"}]))
    (b / "goldens" / "goldens_heldout.json").write_text(json.dumps([{"id": "b"}]))
    return b


def test_adopt_legacy_bundle_preserves_legacy_keys(tmp_path):
    b = _make_legacy_bundle(tmp_path)
    m = Manifest(run_id="r", task_identity="task:abc")
    out, res = adopt_existing(m, b, MINPACK_EPSILON,
                              epsilon_justification="from parity.rs (minpack)",
                              capture_method="cminpack _gen")
    assert res.verdict == "pass"
    assert out.approach == "a1-reference-port"
    assert out.reference_port_ref.endswith("oracle") and out.reference_port_clean_room
    assert m.oracle is not None and m.oracle["golden_public_ref"].endswith("goldens_public.json")
    assert "x" in out.epsilon and out.epsilon["info"].exact


def test_adopt_legacy_bundle_fails_when_incomplete(tmp_path):
    b = tmp_path / "empty"
    (b / "oracle").mkdir(parents=True)  # goldens missing
    m = Manifest(run_id="r", task_identity="task:abc")
    _out, res = adopt_existing(m, b, MINPACK_EPSILON, epsilon_justification="x")
    assert res.verdict == "fail"
    assert m.oracle is None


# ---- deprecated epsilon machinery (kept for legacy imports) ------------------------------

def test_epsilon_exact_rejects_rel_abs():
    with pytest.raises(ValidationError):
        EpsilonField(exact=True, rel=1e-3)


def test_epsilon_nonexact_needs_a_band():
    with pytest.raises(ValidationError):
        EpsilonField()


def test_minpack_epsilon_still_well_formed():
    eps = {k: EpsilonField(**v) for k, v in MINPACK_EPSILON.items()}
    assert validate_epsilon(eps).verdict == "pass"
    assert eps["info"].exact and eps["x"].rel == 1e-3


# ---- generate prompt (farm 5-step recipe) ------------------------------------------------

def _pb_manifest() -> Manifest:
    return Manifest(
        run_id="r", task_identity="task:abc",
        source=SourceInfo(repo="casey/just", pinned_sha="abc123def0", primary_language="Rust",
                          clone_path="/data/runs/just/source"),
        dimensions=Dimensions(tool_name="just", binary_name="just", upstream_language="rust",
                              flag_surface="--list --summary --show --dump --fmt",
                              case_families=["recipe-listing", "dependencies", "variables",
                                             "conditionals", "error-paths"]),
    )


def test_generate_prompt_carries_the_five_step_recipe(tmp_path):
    p = generate_prompt(_pb_manifest(), tmp_path / "b")
    # step 1: version patch is the ONLY allowed patch
    assert "-nonpb" in p and "ONLY allowed patch" in p and "patch_version_string" in p
    # step 2: oracle pair + text-distinct flag recipes per language
    assert "ensure_oracle_pair" in p and "assert_text_distinct" in p
    assert "-ldflags='-s -w'" in p and "strip=\"symbols\"" in p and "-O3 -s" in p
    # step 3: docs
    assert "collect_docs" in p and "README.md" in p
    # step 4: case-design rules
    assert "cases.py" in p and "100-300 cases" in p and "15-30 per feature family" in p
    assert "8-12 error-path cases" in p and "kebab-case ids" in p
    assert "$HOME" in p and "map iteration order" in p
    # step 5: deterministic capture through the vendored fixtures module
    assert "build_test_fixtures" in p and "check_determinism" in p and "determinism.ok" in p
    # helpers driven via a generated capture.py (agent orchestrates, helpers do the work)
    assert "capture.py" in p
    # dimensions flow in
    assert "casey/just" in p and "/data/runs/just/source" in p and "recipe-listing" in p


def test_generate_prompt_has_functional_viability_gate_before_bundle_work(tmp_path):
    p = generate_prompt(_pb_manifest(), tmp_path / "b")
    assert "Viability gate" in p and "BEFORE building" in p
    assert "Probe every selected functional case family" in p
    assert "Help, version, shell completion" in p
    assert "STOP. Do not build an oracle bundle" in p
    assert "only deterministic invocations left" in p
    assert "clear majority of cases" in p
    assert "Never reach the count floor" in p


def test_generate_prompt_must_not_block(tmp_path):
    p = generate_prompt(_pb_manifest(), tmp_path / "b")
    assert "NO behavior patches to upstream" in p
    assert "NO hand-authored or hand-edited expected outputs" in p
    assert "150 MB" in p
    assert "HELD OUT" in p and "never placed in the agent-visible task env" in p
    # the retired rewrite-port framing must be gone
    assert "reference port" not in p.lower() and "epsilon" not in p.lower()


# ---- agentic generate-mode (session injected, offline) -----------------------------------

def _producing_session(n_cases=80):
    """A fake agent session that 'captures' a complete ProgramBench bundle into its cwd."""
    def _s(prompt, task_dir):
        from pathlib import Path
        td = Path(task_dir)
        (td / "docs").mkdir(parents=True, exist_ok=True)
        (td / "docs" / "help.txt").write_text("usage")
        (td / "docs" / "version.txt").write_text("just 1.0-nonpb")
        (td / "oracle_bin").write_bytes(_elf_amd64(b"O"))
        (td / "prebuilt_bin").write_bytes(_elf_amd64(b"P"))
        (td / "cases.py").write_text("CASES = []")
        (td / "testsuite" / "fixtures").mkdir(parents=True, exist_ok=True)
        (td / "testsuite" / "cases.json").write_text(json.dumps(
            [{"id": f"c-{i}", "args": [], "in": f"c-{i}.in",
              "expected_stdout": f"c-{i}.expected.stdout", "expected_rc": 0}
             for i in range(n_cases)]))
        for i in range(n_cases):
            (td / "testsuite" / "fixtures" / f"c-{i}.expected.stdout").write_text(
                f"result {i}\n")
        (td / "determinism.ok").write_text("")
        return "captured bundle"
    return _s


def test_generate_produces_then_adopts_bundle(tmp_path):
    from programsmith.cells.oracle_golden import generate
    m = _pb_manifest()
    bundle = tmp_path / "gen-bundle"
    out, res, agent = generate(m, bundle, session=_producing_session(), max_iters=2)
    assert agent.success and res.verdict == "pass"
    assert m.oracle is not None and m.oracle["n_cases"] == 80
    assert out.approach == "oracle-pair" and out.oracle_sha256


def test_generate_fails_when_session_produces_nothing(tmp_path):
    from programsmith.cells.oracle_golden import generate
    m = _pb_manifest()
    out, res, agent = generate(m, tmp_path / "empty", session=lambda p, td: "did nothing",
                               max_iters=2)
    assert not agent.success and res.verdict == "fail"
    assert m.oracle is None


def test_generate_fails_on_thin_case_suite(tmp_path):
    # A bundle below the case floor never validates — the loop exhausts and reports red.
    from programsmith.cells.oracle_golden import generate
    m = _pb_manifest()
    out, res, agent = generate(m, tmp_path / "thin", session=_producing_session(n_cases=10),
                               max_iters=2)
    assert not agent.success and res.verdict == "fail"
    assert m.oracle is None
