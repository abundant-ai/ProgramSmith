"""ORACLE + GOLDEN cell — the ProgramBench oracle pair + docs + case suite + fixtures.

Produces, for the selected tool, the frozen bundle CREATE seals into a task (ADR-0038):
  - oracle_bin / prebuilt_bin : two byte-distinct builds of the pinned upstream, version string
    patched to `-nonpb` (the ONLY allowed patch — a behavior patch reduces the task to "sed your
    fetched upstream");
  - docs/                     : help.txt + version.txt captured from the oracle, upstream README;
  - testsuite/cases.json + testsuite/fixtures/ : the Golden-I/O case suite, captured by RUNNING
    the oracle under the sterilized env (never hand-written), determinism-checked (n=3).

Two modes (ADR-0016 shape kept):
  * `adopt_existing` (deterministic) — ingest a bundle that already exists on disk (a farm-built
    bundle, or the output of a finished generate session). THE completeness gate lives here.
  * `generate` (agentic) — drive an agent session through the 5-step farm recipe via the vendored
    helpers (`programsmith.programbench.build_helpers` / `.fixtures`), iterating run_to_green on
    bundle completeness, then adopt.

Grading is BYTE-EXACT in ProgramBench — the epsilon machinery (EpsilonField/MINPACK_EPSILON/
validate_epsilon) is DEPRECATED, kept only so legacy rewrite-port bundles and imports keep
working; the new path never uses it.

Reuse basis: harbor-lh/resources/programbench-farm (_build_helpers.py + task_generator.py
build_test_fixtures + _check_determinism.py + HANDOFF.md §4 case-design rules).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from ..gates import GateResult
from ..manifest import Manifest

# ---- bundle layout contract (single source of truth: adopt + validator + prompt) --------------
# <bundle_dir>/
#   oracle_bin          sealed oracle build (version patched -nonpb)
#   prebuilt_bin        byte-distinct second build (the oracle solver's payload)
#   docs/               help.txt, version.txt, README.md
#   cases.py            authored case suite (source of truth; never agent-visible)
#   testsuite/          cases.json + fixtures/  (emitted by fixtures.build_test_fixtures)
#   determinism.ok      marker touched ONLY after check_determinism(n=3) exits 0
BUNDLE_ORACLE_BIN = "oracle_bin"
BUNDLE_PREBUILT_BIN = "prebuilt_bin"
BUNDLE_DOCS_DIR = "docs"
BUNDLE_TESTSUITE_DIR = "testsuite"
DETERMINISM_MARKER = "determinism.ok"
# Case-count floor: the smallest exported ProgramBench exemplar ships 66 cases (implement-just);
# the farm target is 100-300. Below 66 the surface is too thin to grade a reimplementation.
MIN_CASES = 66
# Case volume alone is not quality. A nominal 100-case suite made entirely of help/version/error
# variants can collapse to two observable outputs while exercising no implementation behavior.
# Require both a small absolute floor and 10% output diversity. Real exported tasks comfortably
# exceed this (dnote 104/133, vale 90/164); the rejected rmstale draft had 2/103 stdout bodies.
MIN_UNIQUE_OUTCOMES = 10


def bundle_paths(bundle_dir: str | Path) -> dict[str, Path]:
    b = Path(bundle_dir)
    ts = b / BUNDLE_TESTSUITE_DIR
    return {
        "oracle_bin": b / BUNDLE_ORACLE_BIN,
        "prebuilt_bin": b / BUNDLE_PREBUILT_BIN,
        "docs_dir": b / BUNDLE_DOCS_DIR,
        "cases_json": ts / "cases.json",
        "fixtures_dir": ts / "fixtures",
        "determinism_marker": b / DETERMINISM_MARKER,
    }


class EpsilonField(BaseModel):
    """DEPRECATED (ADR-0038): tolerance for one compared field in the retired rewrite-port genre.
    ProgramBench grading is byte-exact; kept only for legacy bundle adoption + old imports."""

    rel: float | None = None
    abs: float | None = None
    exact: bool = False

    @model_validator(mode="after")
    def _well_formed(self) -> "EpsilonField":
        if self.exact:
            if self.rel is not None or self.abs is not None:
                raise ValueError("exact field must not set rel/abs")
        else:
            if self.rel is None and self.abs is None:
                raise ValueError("non-exact field needs at least one of rel/abs")
            for v in (self.rel, self.abs):
                if v is not None and v < 0:
                    raise ValueError("rel/abs tolerances must be >= 0")
        return self


Approach = str  # "oracle-pair" (new) | "a1-reference-port" | "promoted-trajectory" (legacy)


class OracleGoldenOutput(BaseModel):
    approach: str = "oracle-pair"  # legacy values a1-reference-port / promoted-trajectory still load
    # ---- ProgramBench oracle-pair bundle (ADR-0038, the live path) ----
    oracle_bin: str | None = None
    prebuilt_bin: str | None = None
    docs_dir: str | None = None
    cases_json: str | None = None
    fixtures_dir: str | None = None
    oracle_sha256: str | None = None
    prebuilt_sha256: str | None = None
    text_distinct: bool | None = None   # assert_text_distinct held (.text section hashes differ)
    n_cases: int | None = None
    # ---- legacy rewrite-port fields (kept so old manifests/bundles load; unused by new path) ----
    reference_port_ref: str | None = None
    reference_port_clean_room: bool = True
    golden_public_ref: str | None = None
    golden_heldout_ref: str | None = None
    capture_method: str | None = None
    reproducible: bool = False
    epsilon: dict[str, EpsilonField] = Field(default_factory=dict)  # DEPRECATED: byte-exact now
    epsilon_justification: str | None = None
    exercises_port: bool = True


# DEPRECATED: minpack tolerances from the retired rewrite-port dogfood (kept verbatim for legacy
# imports + the legacy adopt path; extracted from _build/minpack-crate/oracle/tests/parity.rs).
MINPACK_EPSILON: dict[str, dict] = {
    "x": {"rel": 1e-3, "abs": 1.5e-8},     # rel 1e-3, abs 1.5e-8 fallback when |expected| < 1e-5
    "fnorm": {"rel": 1e-4, "abs": 1e-7},   # rel 1e-4, abs 1e-7 when expected == 0
    "info": {"exact": True},
    "nfev": {"exact": True},
}


def validate_epsilon(epsilon: dict[str, EpsilonField]) -> GateResult:
    """DEPRECATED (legacy adopt path only): well-formedness check on a rewrite-port epsilon."""
    if not epsilon:
        return GateResult("fail", "epsilon is empty")
    bad = [k for k, f in epsilon.items()
           if not f.exact and (f.rel is None and f.abs is None)]
    if bad:
        return GateResult("fail", f"epsilon fields lack a tolerance: {bad}")
    return GateResult("pass", f"epsilon well-formed over fields {sorted(epsilon)}")


# ---- deterministic completeness gate ----------------------------------------------------------

# The grading env (task images, local sweep containers, exported exemplars) is linux/amd64 — the farm's
# drivers cross-compile (GOOS=linux GOARCH=amd64) or build in an amd64 container. A native macOS
# build LOOKS complete on disk but can never exec in the task container (the pb10 SANITY wipe-out:
# all Mach-O arm64 oracles → oracle_reward_1 failed on every run). e_machine 0x3E = EM_X86_64.
_ELF_AMD64_REASON = "must be a linux/amd64 ELF executable (the grading env is linux/amd64)"


def _is_linux_amd64_elf(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            head = f.read(20)
    except OSError:
        return False
    return (len(head) >= 20 and head[:4] == b"\x7fELF" and head[4] == 2 and head[5] == 1
            and int.from_bytes(head[18:20], "little") == 0x3E)


def _bundle_status(bundle_dir: Path) -> tuple[list[str], int | None, bool]:
    """Pure read of the bundle contract: (missing components, case count or None, determinism ok).
    Shared by adopt_existing (the gate) and the generate validator (the run_to_green loop).
    A binary of the wrong format (non-linux/amd64) counts as MISSING — present-but-unrunnable is
    the same defect as absent, and surfacing it here feeds the generate loop the exact fix."""
    p = bundle_paths(bundle_dir)
    missing: list[str] = []
    if not p["oracle_bin"].is_file():
        missing.append(BUNDLE_ORACLE_BIN)
    elif not _is_linux_amd64_elf(p["oracle_bin"]):
        missing.append(f"{BUNDLE_ORACLE_BIN} ({_ELF_AMD64_REASON})")
    if not p["prebuilt_bin"].is_file():
        missing.append(BUNDLE_PREBUILT_BIN)
    elif not _is_linux_amd64_elf(p["prebuilt_bin"]):
        missing.append(f"{BUNDLE_PREBUILT_BIN} ({_ELF_AMD64_REASON})")
    # docs/ must at least carry the captured help text — version.txt/README ride along in practice.
    if not (p["docs_dir"].is_dir() and (p["docs_dir"] / "help.txt").is_file()):
        missing.append(f"{BUNDLE_DOCS_DIR}/help.txt")
    n_cases: int | None = None
    if p["cases_json"].is_file():
        try:
            n_cases = len(json.loads(p["cases_json"].read_text()))
        except Exception:  # noqa: BLE001 — a malformed cases.json counts as absent, not a crash
            n_cases = None
    if n_cases is None:
        missing.append(f"{BUNDLE_TESTSUITE_DIR}/cases.json")
    if not p["fixtures_dir"].is_dir():
        missing.append(f"{BUNDLE_TESTSUITE_DIR}/fixtures/")
    return missing, n_cases, p["determinism_marker"].is_file()


def _minimum_unique_outcomes(n_cases: int) -> int:
    return max(MIN_UNIQUE_OUTCOMES, (n_cases + 9) // 10)


def _bundle_output_diversity(bundle_dir: Path) -> int | None:
    """Count distinct (stdout bytes, exit code) outcomes in a captured ProgramBench suite.

    Return None for a malformed case list, a missing fixture, or an unsafe fixture path so the
    deterministic adoption gate refuses an unverifiable bundle rather than trusting its count.
    """
    p = bundle_paths(bundle_dir)
    try:
        cases = json.loads(p["cases_json"].read_text())
        fixtures = p["fixtures_dir"].resolve()
        outcomes: set[tuple[str, int]] = set()
        for case in cases:
            rel = case["expected_stdout"]
            rc = int(case["expected_rc"])
            expected = (fixtures / rel).resolve()
            if not expected.is_relative_to(fixtures) or not expected.is_file():
                return None
            outcomes.add((_sha256(expected), rc))
        return len(outcomes)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def adopt_existing(
    manifest: Manifest,
    bundle_dir: Path,
    epsilon: dict[str, dict] | None = None,
    *,
    epsilon_justification: str | None = None,
    reproducible: bool = True,
    capture_method: str | None = None,
) -> tuple[OracleGoldenOutput, GateResult]:
    """Adopt an oracle bundle from disk (deterministic — THE completeness gate).

    ProgramBench layout (see bundle_paths) → manifest.oracle gets EXACTLY the nine bundle keys
    {oracle_bin, prebuilt_bin, docs_dir, cases_json, fixtures_dir, oracle_sha256, prebuilt_sha256,
    text_distinct, n_cases}. Requires >= MIN_CASES cases and the determinism marker (touched only
    after check_determinism(n=3) exits 0 — a hand-vetted external bundle can `touch determinism.ok`).

    Legacy rewrite-port layout (oracle/ + goldens/goldens_{public,heldout}.json) is still adopted
    with the pre-ADR-0038 manifest.oracle shape so old runs/bundles keep draining."""
    bundle_dir = Path(bundle_dir)

    # ---- legacy rewrite-port bundle? (detected by shape; never by config) ----
    legacy_oracle = bundle_dir / "oracle"
    legacy_public = bundle_dir / "goldens" / "goldens_public.json"
    legacy_heldout = bundle_dir / "goldens" / "goldens_heldout.json"
    if legacy_oracle.is_dir() or legacy_public.exists() or legacy_heldout.exists():
        return _adopt_legacy(manifest, bundle_dir, epsilon or MINPACK_EPSILON,
                             epsilon_justification=epsilon_justification
                             or "legacy bundle adopted (pre-ADR-0038)",
                             reproducible=reproducible, capture_method=capture_method)

    # ---- ProgramBench oracle-pair bundle ----
    missing, n_cases, determinism_ok = _bundle_status(bundle_dir)
    if missing:
        return _stub_output(), GateResult("fail", f"bundle incomplete; missing: {missing}")
    if n_cases is not None and n_cases < MIN_CASES:
        return _stub_output(), GateResult(
            "fail", f"case suite too thin: {n_cases} < {MIN_CASES} (farm target 100-300)")
    if not determinism_ok:
        return _stub_output(), GateResult(
            "fail", f"determinism marker missing: run fixtures.check_determinism (n=3) and touch "
                    f"{DETERMINISM_MARKER} in {bundle_dir}")

    unique_outcomes = _bundle_output_diversity(bundle_dir)
    required_outcomes = _minimum_unique_outcomes(n_cases or 0)
    if unique_outcomes is None:
        return _stub_output(), GateResult(
            "fail", "case suite outcomes are unreadable or reference missing/unsafe stdout fixtures")
    if unique_outcomes < required_outcomes:
        return _stub_output(), GateResult(
            "fail", f"case suite lacks observable behavior diversity: {unique_outcomes} unique "
                    f"stdout/exit outcomes < {required_outcomes} required for {n_cases} cases; "
                    "do not pad with help/version/validation/error variants")

    p = bundle_paths(bundle_dir)
    # VERIFY .text distinctness here rather than trusting the in-session recipe: the generated
    # test.sh's anti-cheat layer 5 compares the objcopy'd .text SECTION hash, and two builds can
    # differ in whole-file bytes (build-id, timestamps) while sharing an identical .text — which
    # would make the shipped prebuilt solver trip `oracle_section_copy` and fail the oracle trial.
    # _text_section_sha uses objcopy when present and a pure-Python ELF parse otherwise.
    oracle_text = prebuilt_text = None
    try:
        from ..programbench.build_helpers import _text_section_sha
        oracle_text = _text_section_sha(p["oracle_bin"])
        prebuilt_text = _text_section_sha(p["prebuilt_bin"])
    except Exception:  # noqa: BLE001 — non-ELF/unreadable → fall back to the whole-file check below
        pass
    text_distinct = (oracle_text != prebuilt_text) if oracle_text is not None else None
    out = OracleGoldenOutput(
        approach="oracle-pair",
        oracle_bin=str(p["oracle_bin"]),
        prebuilt_bin=str(p["prebuilt_bin"]),
        docs_dir=str(p["docs_dir"]),
        cases_json=str(p["cases_json"]),
        fixtures_dir=str(p["fixtures_dir"]),
        oracle_sha256=_sha256(p["oracle_bin"]),
        prebuilt_sha256=_sha256(p["prebuilt_bin"]),
        # The actual computed .text distinctness (True/False), or None when the binaries aren't
        # ELF-parseable here (then the whole-file check below + the SANITY oracle trial are the guard).
        text_distinct=bool(text_distinct) if text_distinct is not None else True,
        n_cases=n_cases,
        capture_method=capture_method,
        reproducible=reproducible,
    )
    if text_distinct is False:
        # Identical .text defeats anti-cheat layer 5: the shipped prebuilt solver would trip
        # `oracle_section_copy`. Refuse — the flag-recipe delta didn't change codegen.
        return _stub_output(), GateResult(
            "fail", f"oracle_bin and prebuilt_bin have IDENTICAL .text sections "
                    f"(sha={oracle_text[:16]}) — rebuild the prebuilt with a codegen-changing flag "
                    f"recipe (e.g. -O2 vs -O3, lto/codegen-units, -ldflags='-s -w')")
    if out.oracle_sha256 == out.prebuilt_sha256:
        # Byte-identical pair defeats Tier-9 (the legit solver must be distinct from the sealed
        # oracle) — the flag-recipe delta was skipped; refuse the bundle.
        return _stub_output(), GateResult(
            "fail", "oracle_bin and prebuilt_bin are byte-identical — rebuild the prebuilt with a "
                    "distinct flag recipe (assert_text_distinct would fail)")
    # EXACTLY the nine bundle keys (DESIGN §6.4) — downstream CREATE/generator consumes these.
    manifest.oracle = {
        "oracle_bin": out.oracle_bin,
        "prebuilt_bin": out.prebuilt_bin,
        "docs_dir": out.docs_dir,
        "cases_json": out.cases_json,
        "fixtures_dir": out.fixtures_dir,
        "oracle_sha256": out.oracle_sha256,
        "prebuilt_sha256": out.prebuilt_sha256,
        "text_distinct": out.text_distinct,
        "n_cases": out.n_cases,
    }
    return out, GateResult(
        "pass", f"adopted ProgramBench oracle bundle from {bundle_dir.name}: "
                f"{n_cases} cases, determinism checked",
        {"n_cases": n_cases, "oracle_sha256": out.oracle_sha256},
    )


def _adopt_legacy(
    manifest: Manifest,
    bundle_dir: Path,
    epsilon: dict[str, dict],
    *,
    epsilon_justification: str,
    reproducible: bool,
    capture_method: str | None,
) -> tuple[OracleGoldenOutput, GateResult]:
    """Pre-ADR-0038 rewrite-port bundle adoption, byte-for-byte the old behavior (legacy keys
    preserved in manifest.oracle so old runs render/drain)."""
    oracle_dir = bundle_dir / "oracle"
    public = bundle_dir / "goldens" / "goldens_public.json"
    heldout = bundle_dir / "goldens" / "goldens_heldout.json"
    missing = [str(p) for p in (oracle_dir, public, heldout) if not p.exists()]
    if missing:
        return _stub_output(epsilon), GateResult("fail", f"bundle incomplete; missing: {missing}")

    eps = {k: EpsilonField(**v) for k, v in epsilon.items()}
    out = OracleGoldenOutput(
        approach="a1-reference-port",
        reference_port_ref=str(oracle_dir),
        reference_port_clean_room=True,
        golden_public_ref=str(public),
        golden_heldout_ref=str(heldout),
        capture_method=capture_method,
        reproducible=reproducible,
        epsilon=eps,
        epsilon_justification=epsilon_justification,
        exercises_port=True,
    )
    verdict = validate_epsilon(eps)
    if verdict.verdict == "pass":
        manifest.oracle = out.model_dump()
        verdict = GateResult(
            "pass",
            f"adopted legacy oracle+golden from {bundle_dir.name}; {verdict.reason}",
            {"reference_port": str(oracle_dir), "epsilon_fields": sorted(eps)},
        )
    return out, verdict


def _stub_output(epsilon: dict[str, dict] | None = None) -> OracleGoldenOutput:
    return OracleGoldenOutput(
        approach="oracle-pair" if epsilon is None else "a1-reference-port",
        epsilon={k: EpsilonField(**v) for k, v in (epsilon or {}).items()},
    )


def generate_prompt(manifest: Manifest, bundle_dir: Path,
                    epsilon: dict[str, dict] | None = None) -> str:
    """The ProgramBench oracle-capture prompt: 5-step farm recipe driven through the vendored
    helpers. `epsilon` is accepted for legacy call-site compat and IGNORED — grading is byte-exact.

    The build runs inside the agentic session (it needs the toolchain + network); the prompt
    instructs the agent to drive the deterministic helpers via a generated capture.py — the agent
    orchestrates, the helpers do the load-bearing work (invariant #3: wrap, don't invent)."""
    del epsilon  # byte-exact genre; kept in the signature only for legacy callers
    src = manifest.source
    dims = manifest.dimensions
    repo = src.repo if src else "the source repo"
    src_lang = ((dims.upstream_language if dims else None)
                or (src.primary_language if src else None) or "the upstream language")
    src_path = (src.clone_path if src else None) or "the cloned source checkout"
    tool = (dims.tool_name if dims else None) or (repo.rsplit("/", 1)[-1])
    binary = (dims.binary_name if dims else None) or tool
    flag_surface = (dims.flag_surface if dims else None) or "the full documented flag surface"
    families = ", ".join(dims.case_families) if (dims and dims.case_families) else "(pick 5-12 feature families)"
    p = bundle_paths(bundle_dir)

    return f"""\
## Your Task: capture the ProgramBench ORACLE bundle for `{tool}` — oracle pair + docs + case suite + fixtures

Source: {repo} ({src_lang}), already cloned at `{src_path}` (REUSE the INGEST clone — do not re-clone).
Tool: `{tool}` (executable `{binary}`). Flag surface the grader will exercise: {flag_surface}.
Case families to cover: {families}.

Produce under `{bundle_dir}` (write a `capture.py` there that drives the vendored helpers —
`programsmith.programbench.build_helpers` and `programsmith.programbench.fixtures` do the heavy lifting):
  - `{p['oracle_bin']}`   — the sealed oracle build (version patched to `-nonpb`)
  - `{p['prebuilt_bin']}` — a byte-distinct second build of the SAME pinned source (the solver payload)
  - `{p['docs_dir']}/` — help.txt + version.txt (captured from the oracle) + the upstream README.md
  - `{bundle_dir}/cases.py` — the authored case suite: a list of case(cid, args, stdin, files=None) entries
  - `{p['cases_json']}` + `{p['fixtures_dir']}/` — captured by the fixture builder, NEVER by hand
  - `{p['determinism_marker']}` — touch ONLY after the determinism check exits 0

## Viability gate — do this BEFORE building or authoring the full suite
Probe every selected functional case family against a native build of the pinned source, repeating
representative invocations under the sterilized environment. The task is viable only if its real
core behavior has a meaningful byte-deterministic surface. Help, version, shell completion,
configuration inspection, flag parsing, input validation, and error-only behavior are supporting
cases; they cannot be the surviving functional surface.

If core functionality emits unpinnable timestamps, random values, host paths, unstable ordering, or
other nondeterministic bytes, STOP. Do not build an oracle bundle, do not pad `cases.py`, and report
that the selected candidate has no viable deterministic functional surface. The same applies if the
only deterministic invocations left are help/version/argument-validation/error paths. This clean
failure is cheaper and more useful than a formally deterministic but meaningless task.

## Recipe (the farm's 5 steps — in order)
1. VERSION PATCH — the ONLY allowed patch: `build_helpers.patch_version_string(...)` suffixes the
   upstream version string with `-nonpb` (so a verbatim upstream rebuild fails the version case).
   NO other edit to upstream sources, ever.
2. ORACLE PAIR — `build_helpers.ensure_oracle_pair(...)` builds oracle vs prebuilt with byte-distinct
   flag recipes per language: Rust `cargo build --release` vs a release-strip profile
   (strip="symbols", lto=true, codegen-units=1); Go `go build` vs `go build -ldflags='-s -w'`;
   C/C++ `-O2 -g` vs `-O3 -s` + strip. Then `build_helpers.assert_text_distinct(oracle, prebuilt)`
   MUST pass (.text section sha256s differ — the legit solver ships the prebuilt).
   ARCHITECTURE (hard requirement — the adopt gate REJECTS anything else): `oracle_bin` and
   `prebuilt_bin` must be **linux/amd64 ELF** executables — the grading env is linux/amd64.
   On a linux/amd64 host a native build already is. On any OTHER host (e.g. macOS) cross-build:
   Go = prefix both builds with `GOOS=linux GOARCH=amd64` (the farm's standard); Rust/C/C++ =
   run the same two flag recipes inside `docker run --rm --platform linux/amd64
   -v <src>:/src -w /src rust:1-bookworm` (or golang:/gcc: for other toolchains).
   Then ALSO make ONE additional native host build (any flags) as `<bundle>/capture_bin` — the
   fixture capture in step 5 EXECUTES the binary on THIS host, which a cross-built ELF cannot do.
3. DOCS — `build_helpers.collect_docs(oracle)` captures help.txt + version.txt; copy the upstream
   README.md from the clone into `{p['docs_dir']}/`.
4. CASE SUITE — author `cases.py` per the farm case-design rules:
   - 100-300 cases total, 15-30 per feature family across the selected families; cover EVERY output
     mode the flag surface exposes ({MIN_CASES} is the hard floor, not the target).
   - A clear majority of cases must exercise real successful functional behavior across ALL selected
     functional families. Never reach the count floor by multiplying help/version/completion,
     flag-parsing, validation, or error-path variants.
   - Include the version case (`--version`), a nonexistent-flag case, and 8-12 error-path cases
     (bad input / bad regex / bad numeric / missing arg / conflicting flags).
   - kebab-case ids, unique case-insensitively (macOS fixture clobber).
   - AVOID nondeterminism: no $HOME/$USER/$TERM/$PWD reads, no timing fields, no random/hash-order/
     PID output, no Go map iteration order, no embedded dates. Pin flags if needed.
5. CAPTURE + CHECK — `fixtures.build_test_fixtures(cases, <runnable build>, {bundle_dir}/{BUNDLE_TESTSUITE_DIR})`
   runs every case through the tool under the sterilized env; then `fixtures.check_determinism`
   with n=3 re-runs each case and must exit 0. Only then `touch {p['determinism_marker']}`.
   The <runnable build> is the oracle itself on a linux/amd64 host, else the native `capture_bin`
   from step 2 (the farm's capture_binary pattern — same pinned source + version patch, so the
   captured bytes are the oracle's; a case whose output differs across OS/arch is DROPPED per the
   determinism rules). A nondeterministic or divergent case is FIXED or DROPPED and re-captured —
   never patched by hand.

## MUST NOT (hard boundaries — violating any of these makes the task invalid)
- NO behavior patches to upstream — the `-nonpb` version-string suffix is the ONLY allowed patch.
- NO hand-authored or hand-edited expected outputs — every fixture byte is captured from the real
  oracle under the sterilized env.
- The bundle must stay well under ~150 MB (it uploads to the sweep runner): small/representative
  inputs, never duplicate large inputs, never include build trees or runtime artifacts.
- The oracle pair, cases and fixtures are HELD OUT — never placed in the agent-visible task env
  (CREATE seals them; the solving agent sees only the execute-only oracle + docs).

## Done
All bundle files present; oracle_bin + prebuilt_bin are linux/amd64 ELF; assert_text_distinct passed;
>= {MIN_CASES} cases (target 100-300) captured deterministically (check_determinism n=3 exit 0,
marker written)."""


def generate(
    manifest: Manifest,
    bundle_dir: Path,
    epsilon: dict[str, dict] | None = None,
    *,
    epsilon_justification: str = "unused (ProgramBench grading is byte-exact)",
    capture_method: str | None = None,
    session=None,
    max_iters: int = 3,
    model: str | None = None,
) -> tuple[OracleGoldenOutput, GateResult, "object"]:
    """Agentic generate-mode: drive an agent session through the farm capture recipe into
    `bundle_dir`, then ADOPT the produced bundle (adopt_existing is the single completeness gate +
    manifest writer). The empirical oracle=1/nop=0 proof stays SANITY's job downstream.

    The agent session is injectable (`cells.agentic`); the default rides the OAuth subscription
    (ADR-0015). Returns (output, gate_verdict, agent_result)."""
    from .agentic import AgentResult, claude_code_session, run_to_green

    bundle_dir = Path(bundle_dir)
    bundle_dir.mkdir(parents=True, exist_ok=True)  # the agent session runs with this as its cwd
    session = session or claude_code_session(model=model)
    prompt = generate_prompt(manifest, bundle_dir, epsilon)

    # The generate-mode "validator" is bundle COMPLETENESS + case-count floor + determinism marker,
    # surfaced through the agentic ValidationState contract so the loop iterates with feedback.
    # (Correctness — oracle rewards 1, nop rewards 0 — is proven at SANITY, kept separate by design.)
    def _bundle_complete(_task_dir):
        from .agentic import ValidationState
        missing, n_cases, determinism_ok = _bundle_status(bundle_dir)
        unique_outcomes = _bundle_output_diversity(bundle_dir)
        required_outcomes = _minimum_unique_outcomes(n_cases or 0)
        binaries_ok = not any(m in (BUNDLE_ORACLE_BIN, BUNDLE_PREBUILT_BIN,
                                    f"{BUNDLE_DOCS_DIR}/help.txt") for m in missing)
        suite_ok = (not any(m.startswith(BUNDLE_TESTSUITE_DIR) for m in missing)
                    and n_cases is not None and n_cases >= MIN_CASES and determinism_ok
                    and unique_outcomes is not None and unique_outcomes >= required_outcomes)
        return ValidationState(
            oracle_passed=binaries_ok, nop_passed=suite_ok,
            detail={"missing": missing, "n_cases": n_cases, "determinism_ok": determinism_ok,
                    "min_cases": MIN_CASES, "unique_outcomes": unique_outcomes,
                    "min_unique_outcomes": required_outcomes})

    agent: AgentResult = run_to_green(prompt, bundle_dir, session=session,
                                      validator=_bundle_complete, max_iters=max_iters,
                                      label="oracle-generate")
    if not agent.success:
        return (
            _stub_output(),
            GateResult("fail", f"oracle generate did not produce a complete bundle: {agent.reason}",
                       {"agent": agent.reason}),
            agent,
        )
    out, verdict = adopt_existing(
        manifest, bundle_dir,
        epsilon_justification=epsilon_justification,
        reproducible=True, capture_method=capture_method or "agentic farm-recipe capture",
    )
    return out, verdict, agent
