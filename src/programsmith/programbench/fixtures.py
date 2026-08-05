"""Golden-I/O fixture capture + determinism check for ProgramBench tasks.

Vendored from programbench-farm (invariant #3): `build_test_fixtures` is task_generator.py's
fixture builder verbatim (the STERILIZED_ENV dict hoisted to a module constant so the ORACLE
cell's capture.py and the vendored verify.py template can reference ONE source of truth);
`check_determinism` is _check_determinism.py library-ified (returns a report instead of
sys.exit, so gates route on it).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Sterilized env — MUST byte-match the sterilized_env baked into the generated tests/verify.py
# (generator.py) so the agent's binary sees at verify time exactly the env the oracle saw at
# fixture-capture time. All values CONSTANT (farm lesson 7.1: VM HOME=/home/ubuntu vs sandbox
# HOME=/root broke every $HOME-touching case). No TERM/CLICOLOR/LD_*/PYTHON* — avoids fixtures
# that depend on tty/color heuristics in the oracle (shfmt -d emits ANSI only when TERM is set).
# Pin locale and timezone so time- and date-bearing output remains byte-reproducible across
# fixture capture and grading.
STERILIZED_ENV: dict[str, str] = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C",
    "TZ": "UTC",
    "NO_COLOR": "1",
    "COLUMNS": "80",  # pin render width so width-sensitive stdout is reproducible
    "HOME": "/tmp/agent-home",  # fixed value so fixture matches verifier
}


def build_test_fixtures(cases: list, oracle: Path, out_dir: Path):
    """Run each case through oracle, snapshot stdout+rc into fixtures.

    Runs the oracle with a sterilized env that matches what test.sh uses
    inside the verifier (no TERM, no CLICOLOR, no LD_*, no PYTHON*, minimal
    PATH). This avoids fixtures that depend on tty/color heuristics in the
    oracle binary (e.g. shfmt -d emits ANSI codes only when TERM is set).
    """
    fix = out_dir / "fixtures"
    shutil.rmtree(fix, ignore_errors=True)
    fix.mkdir(parents=True)
    sterilized_env = dict(STERILIZED_ENV)
    idx = []
    seen = set()
    seen_ci = {}
    for c in cases:
        cid = c["id"]
        if cid in seen:
            raise ValueError(f"duplicate id {cid}")
        cid_lo = cid.lower()
        if cid_lo in seen_ci:
            # Two case IDs differ only in casing; the per-case files
            # `<cid>.in` / `<cid>.expected.stdout` would silently clobber
            # each other on case-insensitive filesystems (macOS / Windows
            # default), shipping a task that fails verify.py with
            # FileNotFoundError on the second case.
            other = seen_ci[cid_lo]
            raise ValueError(
                f"case-insensitive id collision: {cid!r} clashes with "
                f"{other!r}; rename one to avoid macOS fixture clobber"
            )
        seen.add(cid)
        seen_ci[cid_lo] = cid
        args = c["args"]
        inp_bytes = c["stdin"]
        if isinstance(inp_bytes, str):
            inp_bytes = inp_bytes.encode("utf-8")
        # Per-case optional file-tree fixture. If present, materialise under
        # <fixtures>/<cid>.files/ and run the oracle with cwd=that-dir so that
        # tools walking the cwd (fd, tokei, bat, delta, sqlite3) get a
        # deterministic on-disk tree across fixture-gen and verify time.
        files_spec = c.get("files")
        case_cwd = None
        files_dirname = None
        if files_spec:
            files_dirname = f"{cid}.files"
            files_dir = fix / files_dirname
            shutil.rmtree(files_dir, ignore_errors=True)
            files_dir.mkdir(parents=True)
            for rel, content in files_spec.items():
                # Reject anything that would escape the per-case dir; the
                # verifier replays into a fresh per-case scratch dir in the trial sandbox
                # and we want the same shape on both sides.
                if rel.startswith("/") or ".." in Path(rel).parts:
                    raise ValueError(f"case {cid}: unsafe files path {rel!r}")
                dest = files_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, str):
                    content_b = content.encode("utf-8")
                else:
                    content_b = bytes(content)
                dest.write_bytes(content_b)
            case_cwd = str(files_dir)
        try:
            r = subprocess.run(
                [str(oracle)] + args,
                input=inp_bytes,
                capture_output=True,
                timeout=c.get("timeout", 15),
                env=sterilized_env,
                cwd=case_cwd,
            )
        except subprocess.TimeoutExpired:
            print(f"  {cid:40s} TIMEOUT - skipping case")
            continue
        in_path = fix / f"{cid}.in"
        out_path = fix / f"{cid}.expected.stdout"
        in_path.write_bytes(inp_bytes)
        out_path.write_bytes(r.stdout)
        entry = {
            "id": cid,
            "args": args,
            "in": f"{cid}.in",
            "expected_stdout": f"{cid}.expected.stdout",
            "expected_rc": r.returncode,
        }
        if files_dirname:
            entry["files_dir"] = files_dirname
        idx.append(entry)
        suffix = f" files={len(files_spec)}" if files_spec else ""
        print(f"  {cid:40s} rc={r.returncode} stdout={len(r.stdout)}B{suffix}")
    (out_dir / "cases.json").write_text(json.dumps(idx, indent=2))
    print(f"  wrote {len(idx)} cases")
    return idx


@dataclass
class DeterminismReport:
    """Outcome of check_determinism — the ORACLE/CREATE run_to_green validators route on `ok`."""

    n_cases: int
    n_runs: int
    nondeterministic: list[tuple[str, str]] = field(default_factory=list)
    oracle_mismatch: list[tuple[str, str]] = field(default_factory=list)   # oracle != recorded expected
    prebuilt_mismatch: list[tuple[str, str]] = field(default_factory=list)  # prebuilt != expected

    @property
    def ok(self) -> bool:
        # Matches _check_determinism.py's exit code: nondeterminism or prebuilt drift fails;
        # oracle!=expected is REPORTED only (the farm treats it as informational — a stale
        # fixture surfaces as an oracle-trial failure in the sweep, the authoritative gate).
        return not (self.nondeterministic or self.prebuilt_mismatch)

    def summary(self) -> str:
        return (f"cases={self.n_cases} N={self.n_runs} nondet={len(self.nondeterministic)} "
                f"oracle!=expected={len(self.oracle_mismatch)} prebuilt!=oracle={len(self.prebuilt_mismatch)}")


def check_determinism(
    task_dir: Path,
    oracle: Path,
    prebuilt: Path | None = None,
    n: int = 3,
) -> DeterminismReport:
    """Local pre-sweep check: determinism + prebuilt/oracle equivalence (from _check_determinism.py).

    For each case in <task_dir>/tests/testsuite/cases.json:
      * run the oracle `n` times under STERILIZED_ENV; all runs must be byte-identical AND match
        the recorded expected_stdout / expected_rc;
      * if `prebuilt` given, run it once; it must equal the expected output+rc (solve.sh ships
        prebuilt as the agent executable, so any divergence would fail the oracle trial itself).
    """
    task_dir = Path(task_dir)
    fix = task_dir / "tests" / "testsuite" / "fixtures"
    cases = json.loads((task_dir / "tests" / "testsuite" / "cases.json").read_text())
    scratch = Path(tempfile.mkdtemp(prefix="_checkdet_"))

    def run(exe: Path | str, c: dict) -> tuple[int, bytes]:
        ar = list(c["args"])
        in_bytes = (fix / c["in"]).read_bytes()
        cwd = None
        fd = c.get("files_dir")
        if fd:
            dest = scratch / (c["id"] + "-" + Path(exe).name)
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(fix / fd, dest)
            cwd = str(dest)
        r = subprocess.run(
            [str(exe)] + ar, input=in_bytes, capture_output=True,
            timeout=c.get("timeout", 20), env=dict(STERILIZED_ENV), cwd=cwd,
        )
        return r.returncode, r.stdout

    report = DeterminismReport(n_cases=len(cases), n_runs=n)
    try:
        for c in cases:
            cid = c["id"]
            exp_out = (fix / c["expected_stdout"]).read_bytes()
            exp_rc = int(c["expected_rc"])
            outs: set[tuple[int, bytes]] = set()
            rc0: int | None = None
            out0: bytes | None = None
            for _ in range(n):
                try:
                    rc, out = run(oracle, c)
                except Exception as e:  # noqa: BLE001 — any exec failure = nondeterministic case
                    report.nondeterministic.append((cid, f"oracle exec err: {e}"))
                    break
                outs.add((rc, out))
                rc0, out0 = rc, out
            if len(outs) > 1:
                report.nondeterministic.append((cid, f"{len(outs)} distinct outputs over {n} runs"))
            if (rc0, out0) != (exp_rc, exp_out):
                report.oracle_mismatch.append(
                    (cid, f"rc {rc0} vs exp {exp_rc}; out {len(out0 or b'')} vs {len(exp_out)}")
                )
            if prebuilt:
                try:
                    prc, pout = run(prebuilt, c)
                    if (prc, pout) != (exp_rc, exp_out):
                        report.prebuilt_mismatch.append(
                            (cid, f"prebuilt rc {prc} vs {exp_rc}; out {len(pout)} vs {len(exp_out)}")
                        )
                except Exception as e:  # noqa: BLE001
                    report.prebuilt_mismatch.append((cid, f"prebuilt exec err: {e}"))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return report
