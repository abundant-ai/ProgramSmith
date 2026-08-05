"""Layout-contract tests for the vendored ProgramBench generator (build_task) against a REAL
tiny oracle: a 20-line C tool compiled in-test (falls back to /bin/echo + /bin/cat when no C
compiler is on the host). Asserts the exported-exemplar contract: directory layout, task.toml
constants, the 9-tier anti-cheat markers in tests/test.sh, the mini-swe-preinstalled
ubuntu:24.04 Dockerfile (ADR-0043), the encrypted private bundle, and that check_determinism
passes over the captured suite."""

import json
import shutil
import subprocess

import pytest

from programsmith.programbench.fixtures import STERILIZED_ENV, build_test_fixtures, check_determinism
from programsmith.programbench.generator import build_task, sha256_file

pytestmark = pytest.mark.skipif(
    shutil.which("gpg") is None,
    reason="build_task encrypts the private bundle with gpg (production requirement; "
    "not stubbed here so the test proves the real path)",
)

_MINITOOL_C = r"""
#include <stdio.h>
#include <string.h>
static const char pad[] = PAD;   /* differs between builds -> byte-distinct pair, same behavior */
int main(int argc, char **argv) {
    if (argc > 99) puts(pad);    /* unreachable: keeps pad out of the optimizer's reach */
    if (argc > 1 && strcmp(argv[1], "--version") == 0) { printf("minitool 1.0-nonpb\n"); return 0; }
    if (argc > 1 && strcmp(argv[1], "--help") == 0) { printf("usage: minitool [--upper]\n"); return 0; }
    int upper = 0;
    if (argc > 1) {
        if (strcmp(argv[1], "--upper") == 0) upper = 1;
        else { fprintf(stderr, "bad flag\n"); return 2; }
    }
    int c;
    while ((c = getchar()) != EOF) putchar(upper && c >= 'a' && c <= 'z' ? c - 32 : c);
    return 0;
}
"""


@pytest.fixture(scope="module")
def oracle_pair(tmp_path_factory):
    """(oracle, prebuilt) — a byte-distinct pair of the same tiny tool, or (/bin/echo, /bin/cat)
    when the host has no C compiler (behavior parity doesn't matter for the fallback: the
    prebuilt is only hashed/bundled, never run, in these tests)."""
    d = tmp_path_factory.mktemp("minitool")
    cc = shutil.which("cc") or shutil.which("gcc")
    if cc is None:
        return "/bin/echo", "/bin/cat", False
    src = d / "minitool.c"
    src.write_text(_MINITOOL_C)
    oracle, prebuilt = d / "minitool-oracle", d / "minitool-pb"
    subprocess.run([cc, "-O0", '-DPAD="oracle-pad-A"', "-o", str(oracle), str(src)], check=True)
    subprocess.run([cc, "-O0", '-DPAD="prebuilt-pad-B"', "-o", str(prebuilt), str(src)], check=True)
    return str(oracle), str(prebuilt), True


def _cases():
    return [
        {"id": "identity", "args": [], "stdin": "hello world\n"},
        {"id": "upper", "args": ["--upper"], "stdin": "hello\n"},
        {"id": "err-bad-flag", "args": ["--zzzz"], "stdin": ""},
        # per-case cwd tree (the files_dir extension used by just/editorconfig-style tools)
        {"id": "with-files", "args": [], "stdin": "tree case\n", "files": {"sub/a.txt": "x\n"}},
    ]


@pytest.fixture(scope="module")
def built_task(oracle_pair, tmp_path_factory):
    oracle, prebuilt, _real = oracle_pair
    ws = tmp_path_factory.mktemp("ws")
    readme = ws / "README.md"
    readme.write_text("# minitool\nupstream readme\n")
    task_dir = build_task(
        slug="implement-minitool",
        tool="minitool",
        workspace=ws,
        oracle_binary=oracle,
        prebuilt_binary=prebuilt,
        docs_help=b"usage: minitool [--upper]\n",
        docs_version=b"minitool 1.0-nonpb\n",
        docs_readme=readme,
        upstream_repo="https://github.com/example/minitool",
        upstream_commit="deadbeefcafe",
        upstream_short="deadbeefca",
        language="c",
        apt_extra=[],
        toolchain_install="RUN echo 'C/C++ toolchain already present (build-essential)'",
        cases=_cases(),
        passphrase="ngn-minitool-testpassphrase000000000000",
        description_short="Reimplement 'minitool' — a test tool — from scratch.",
        difficulty_explanation="test difficulty explanation",
        solution_explanation="Oracle ships the prebuilt. Prebuilt is byte-distinct (-O0 pad).",
        verification_explanation="tests/test.sh runs compile.sh BEFORE decrypting /private.",
        tags_extra=["testing", "binary-reverse-engineering"],  # deliberate dup: must dedupe
        expert_hours=14,
        strip_system_tools=["minitool"],
    )
    return task_dir, oracle, prebuilt


def test_layout_contract(built_task):
    task_dir, _o, _p = built_task
    assert task_dir.name == "implement-minitool"  # build_task returns <workspace>/<slug>
    for rel in (
        "instruction.md", "task.toml",
        "solution/solve.sh",
        "environment/Dockerfile", "environment/docker-compose.yaml",
        "environment/binary/minitool",
        "environment/docs_seed/help.txt", "environment/docs_seed/version.txt",
        "environment/docs_seed/README.md", "environment/app_test.sh", "environment/timer.sh",
        "environment/submission_stub/compile.sh", "environment/private_bundle.tar.gz.gpg",
        "tests/test.sh", "tests/verify.py", "tests/scan_submission.py",
        "tests/scan_smuggle.py", "tests/scan_similarity.py", "tests/testsuite/cases.json",
    ):
        assert (task_dir / rel).exists(), rel
    # gpg bundle is non-empty binary, not a plaintext tar
    bundle = (task_dir / "environment/private_bundle.tar.gz.gpg").read_bytes()
    assert len(bundle) > 100 and not bundle.startswith(b"private/")


def test_cases_and_fixtures(built_task):
    task_dir, _o, _p = built_task
    cases = json.loads((task_dir / "tests/testsuite/cases.json").read_text())
    assert [c["id"] for c in cases] == ["identity", "upper", "err-bad-flag", "with-files"]
    fix = task_dir / "tests/testsuite/fixtures"
    for c in cases:
        assert (fix / c["in"]).exists() and (fix / c["expected_stdout"]).exists()
    assert cases[2]["expected_rc"] != 0 or _o == "/bin/echo"  # error-path case carries its rc
    assert cases[3]["files_dir"] == "with-files.files"
    assert (fix / "with-files.files" / "sub" / "a.txt").read_text() == "x\n"


def test_task_toml_constants(built_task):
    task_dir, _o, _p = built_task
    toml = (task_dir / "task.toml").read_text()
    assert 'difficulty = "hard"' in toml
    assert 'category = "reverse-engineering"' in toml
    assert "timeout_sec = 1500.0" in toml and "timeout_sec = 18000.0" in toml
    assert "build_timeout_sec = 1800.0" in toml and "allow_internet = false" in toml
    assert "cpus = 4" in toml and "memory_mb = 16384" in toml and "storage_mb = 16384" in toml
    assert "expert_time_estimate_hours = 14" in toml
    # tags: base four + extras, deduped (the farm's duplicated binary-reverse-engineering quirk
    # must NOT be reproduced — DESIGN §6.5)
    assert toml.count('"binary-reverse-engineering"') == 1
    for t in ('"cli-reimplementation"', '"non-programbench"', '"c"', '"testing"'):
        assert t in toml, t


def test_test_sh_anti_cheat_envelope(built_task):
    task_dir, oracle, _p = built_task
    sh = (task_dir / "tests/test.sh").read_text()
    # the baked-in verifier-private oracle hash matches the shipped oracle
    assert f"ORACLE_HASH_PRE='{sha256_file(oracle)}'" in sh
    assert "PASSPHRASE='ngn-minitool-" in sh
    for marker in (
        "Tier-1:", "Tier-1.5:", "scan_submission.py", "Tier-8 vendored-upstream-tree scan",
        "oracle_binary_copy", "scan_smuggle.py", "oracle_section_copy",
        "Tier-9 byte-similarity check", "scan_similarity.py",
        "strace wrapper-detection probe", "memfd_create", "EXECVE_ALLOW",
        "verify.py",
    ):
        assert marker in sh, marker
    assert 'LEGIT_ORACLE_SOLVER=1' in sh
    assert 'known oracle control; skipping wrapper-detection probes' in sh
    assert sh.rstrip().endswith("exit 0")


def test_dockerfile_base_git_and_mini_swe_block(built_task):
    task_dir, _o, _p = built_task
    df = (task_dir / "environment/Dockerfile").read_text()
    lines = df.splitlines()
    assert lines[1] == "FROM ubuntu:24.04"   # after the check-dockerfile-base-image comment
    # Agent runtimes must be complete at image-build time because Harbor disables networking before
    # trial setup. Git supports mini-swe/upstream clones; tmux + asciinema support terminus-2.
    apt = next(line for line in lines if "build-essential" in line)
    assert all(tool in apt for tool in ("git", "tmux", "asciinema"))
    # ADR-0043: the mini-swe pre-install block (uv 0.7.13 pinned, PATH primed, tool installed)
    assert 'ENV PATH="/root/.local/bin:${PATH}"' in df
    assert "https://astral.sh/uv/0.7.13/install.sh" in df
    assert "uv tool install mini-swe-agent" in df
    # Tier-6 purge covers the tool's own name; Tier-7 blocks the Go proxy
    assert "-name 'minitool'" in df
    assert "ENV GOPROXY=off" in df and "127.0.0.1 proxy.golang.org" in df
    assert "chmod 0111 /app/oracle_binary/minitool" in df
    compose = (task_dir / "environment/docker-compose.yaml").read_text()
    assert compose == "services:\n  main:\n    platform: linux/amd64\n"


def test_dockerfile_never_strips_harness_essentials(oracle_pair, tmp_path):
    """Regression (ADR-0046, the pb10 toybox-grep wedge): tool=grep put /bin/grep on the Tier-6
    strip list; harbor's agent-tools build layer (appended on top of the task image) execs `grep`
    while parsing the node SHASUMS, so EVERY remote image build died with exit 127 — while local
    SANITY, which never sees that layer, passed. Harness-essential names must survive the strip
    filter; the strace probe-2 execve allowlist stays the runtime catch."""
    oracle, prebuilt, _real = oracle_pair
    ws = tmp_path / "ws"; ws.mkdir()
    (ws / "README.md").write_text("# toygrep\n")
    task_dir = build_task(
        slug="implement-toygrep", tool="grep", workspace=ws,
        oracle_binary=oracle, prebuilt_binary=prebuilt,
        docs_help=b"usage: grep PATTERN\n", docs_version=b"grep 1.0-nonpb\n",
        docs_readme=ws / "README.md",
        upstream_repo="https://github.com/example/toybox", upstream_commit="deadbeefcafe",
        upstream_short="deadbeefca", language="c", apt_extra=[],
        toolchain_install="RUN echo 'toolchain present'",
        cases=_cases(), passphrase="ngn-toygrep-testpassphrase0000000000000",
        description_short="Reimplement 'grep' from scratch.",
        difficulty_explanation="x", solution_explanation="x", verification_explanation="x",
        tags_extra=[], expert_hours=10,
        strip_system_tools=["grep"],
    )
    df = (task_dir / "environment/Dockerfile").read_text()
    assert "/usr/bin/grep" not in df and "-name 'grep'" not in df
    # the default non-essential strips are still present
    assert "-name 'shfmt'" in df


def test_instruction_and_solve(built_task):
    task_dir, _o, _p = built_task
    instr = (task_dir / "instruction.md").read_text()
    assert instr.startswith("# Reimplement `minitool` from scratch")
    assert "/app/oracle_binary/minitool" in instr
    assert "The grader runs 4 black-box test cases" in instr  # default farm template count slot
    solve = (task_dir / "solution/solve.sh").read_text()
    assert "gpg --batch" in solve and "/private/prebuilt_executable" in solve


def test_check_determinism_passes(built_task, oracle_pair):
    task_dir, oracle, prebuilt = built_task
    _o, _p, real_pair = oracle_pair
    report = check_determinism(task_dir, oracle, prebuilt if real_pair else None, n=3)
    assert report.ok, report.summary()
    assert report.n_cases == 4 and not report.oracle_mismatch


def test_prebuilt_testsuite_copy_path(built_task, oracle_pair, tmp_path):
    """The pipeline seam: CREATE passes the ORACLE-stage capture verbatim instead of re-running
    the oracle — the copied suite must be byte-identical to a live capture."""
    task_dir, oracle, prebuilt = built_task
    readme = tmp_path / "README.md"
    readme.write_text("# minitool\n")
    task2 = build_task(
        slug="implement-minitool2",
        tool="minitool",
        workspace=tmp_path / "ws2",
        oracle_binary=oracle,
        prebuilt_binary=prebuilt,
        docs_help=b"h\n", docs_version=b"v\n", docs_readme=readme,
        upstream_repo="https://github.com/example/minitool",
        upstream_commit="deadbeefcafe", upstream_short="deadbeefca",
        language="c", apt_extra=[],
        toolchain_install="RUN echo 'C/C++ toolchain already present (build-essential)'",
        prebuilt_cases_json=task_dir / "tests/testsuite/cases.json",
        prebuilt_fixtures_dir=task_dir / "tests/testsuite/fixtures",
        passphrase="ngn-minitool-testpassphrase000000000000",
        description_short="d", difficulty_explanation="e",
        solution_explanation="s", verification_explanation="v",
        expert_hours=14,
    )
    assert (task2 / "tests/testsuite/cases.json").read_bytes() == \
        (task_dir / "tests/testsuite/cases.json").read_bytes()
    assert (task2 / "tests/testsuite/fixtures/identity.in").read_bytes() == \
        (task_dir / "tests/testsuite/fixtures/identity.in").read_bytes()
    # instruction's grader-count slot derives from the COPIED suite
    assert "The grader runs 4 black-box test cases" in (task2 / "instruction.md").read_text()


def test_guard_blocks_official_upstream(oracle_pair, tmp_path):
    oracle, prebuilt, _real = oracle_pair
    readme = tmp_path / "README.md"
    readme.write_text("# bat\n")
    with pytest.raises(RuntimeError, match="official ProgramBench"):
        build_task(
            slug="implement-bat", tool="bat", workspace=tmp_path / "ws",
            oracle_binary=oracle, prebuilt_binary=prebuilt,
            docs_help=b"h\n", docs_version=b"v\n", docs_readme=readme,
            upstream_repo="https://github.com/sharkdp/bat",
            upstream_commit="x", upstream_short="x", language="rust", apt_extra=[],
            toolchain_install="RUN true", cases=_cases(),
            passphrase="ngn-bat-x", description_short="d", difficulty_explanation="e",
            solution_explanation="s", verification_explanation="v",
        )


def test_sterilized_env_matches_verify_py(built_task):
    """STERILIZED_ENV (fixture capture) and the sterilized_env baked into the generated
    tests/verify.py MUST agree key-for-key — a drift ships fixtures the verifier can't
    reproduce (farm lesson 7.1)."""
    task_dir, _o, _p = built_task
    verify = (task_dir / "tests/verify.py").read_text()
    assert STERILIZED_ENV["LC_ALL"] == "C"
    assert STERILIZED_ENV["TZ"] == "UTC"
    for k, v in STERILIZED_ENV.items():
        assert f'"{k}": "{v}"' in verify, (k, v)
