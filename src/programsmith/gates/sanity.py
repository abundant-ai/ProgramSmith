"""SANITY gate (deterministic, no agent) — the anti-hack backbone proof.

Builds the task image and proves the verifier is sound BEFORE any sweep spend:
  * ORACLE  : run solve.sh (reference port) → reward must be 1, and produced files must be owned by
              `nobody` (proves Phase A actually dropped privileges);
  * NOP     : run the verifier with no agent work → reward must be 0;
  * PRIV A/B: `nobody` must NOT be able to read `/private.enc` or a root-0700 secret dir.
Verdict `pass` iff all hold. Reuse basis: harbor-lh-rewrite-tasks/validate-generic.sh.

Docker calls are injectable (`builder`, `phase_runner`) so the verdict logic is unit-tested
offline; the live path shells out to `docker`.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable

from . import GateResult

Builder = Callable[[], tuple[bool, str]]          # () -> (ok, log)
PhaseRunner = Callable[[str], str]                 # (bash snippet) -> combined output

_ORACLE_SNIPPET = r'''
  bash /verify/solve.sh >/tmp/solve.log 2>&1 || { echo SOLVE_FAILED; tail -8 /tmp/solve.log; }
  bash /run_test.sh >/tmp/v.log 2>&1
  echo "REWARD=$(cat /logs/verifier/reward.txt 2>/dev/null)"
  f=$(find /logs/verifier/produced -type f 2>/dev/null | head -1)
  [ -n "$f" ] && stat -c "PRODUCED_OWNER=%U" "$f" || echo "PRODUCED_OWNER=none"
'''

_NOP_SNIPPET = r'''
  bash /run_test.sh >/tmp/v.log 2>&1
  echo "REWARD=$(cat /logs/verifier/reward.txt 2>/dev/null)"
'''

_PRIV_SNIPPET = r'''
  chmod 600 /private.enc 2>/dev/null || true
  printf "ENC="; setpriv --reuid=nobody --regid=nogroup --clear-groups cat /private.enc >/dev/null 2>/dev/null && echo "READ" || echo "DENIED"
  S=$(mktemp -d); chmod 700 "$S"; echo topsecret > "$S/k"
  printf "KEY="; setpriv --reuid=nobody --regid=nogroup --clear-groups cat "$S/k" >/dev/null 2>/dev/null && echo "READ" || echo "DENIED"
'''


# The ProgramBench genre is linux/amd64 end-to-end: the sealed oracle pair is amd64 ELF, the
# Dockerfile fetches amd64 toolchains, and sweep/grading environments are amd64. A local
# SANITY on an arm64 host (Apple Silicon) must therefore build+run the image under emulation —
# an unpinned build produces an arm64 image in which the amd64 oracle cannot exec (the pb10
# oracle_reward_1 failures).
_PLATFORM = "linux/amd64"


def _docker_builder(task_dir: Path, tag: str, timeout: int) -> Builder:
    def _build() -> tuple[bool, str]:
        proc = subprocess.run(
            ["docker", "build", "--platform", _PLATFORM, "-t", tag,
             "-f", str(task_dir / "environment" / "Dockerfile"),
             str(task_dir / "environment")],
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]
    return _build


def _docker_phase_runner(task_dir: Path, tag: str, timeout: int) -> PhaseRunner:
    # The WHOLE tests/ tree mounts at /tests — the harbor verifier convention the ProgramBench
    # test.sh is written against: it invokes /tests/verify.py, /tests/scan_*.py and reads
    # /tests/testsuite/{cases.json,fixtures}. Mounting test.sh alone (the pre-pivot harness)
    # made every anti-cheat scan a no-op and verify.py unreachable → oracle reward 0 on a
    # perfectly good task (the pb10 tengo failure). test.sh still copies to /run_test.sh so
    # the verifier body executes from a container-local path, same as before.
    tests_dir = task_dir / "tests"
    solve_sh = task_dir / "solution" / "solve.sh"

    def _run(snippet: str) -> str:
        wrapper = f"cp /tests/test.sh /run_test.sh && chmod 755 /run_test.sh\n{snippet}"
        proc = subprocess.run(
            ["docker", "run", "--rm", "--platform", _PLATFORM,
             "-v", f"{tests_dir}:/tests:ro",
             "-v", f"{solve_sh}:/verify/solve.sh:ro",
             tag, "bash", "-lc", wrapper],
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.stdout + proc.stderr
    return _run


def _reward(output: str) -> str | None:
    m = re.search(r"REWARD=(\S+)", output)
    return m.group(1) if m else None


def run_sanity_trials(trials: list[dict]) -> GateResult:
    """SANITY from recorded oracle/nop baseline trials — the Docker-less read path (ADR-0017).

    An `oracle` baseline trial replays the reference (must reward 1) and `nop` does nothing (must
    reward 0) — which IS the SANITY oracle=1/nop=0 contract. Verdict `pass` iff at least one oracle
    baseline trial rewards 1 AND every nop baseline trial rewards 0 (and both are present). Used
    when baseline trials were recorded by a sweep or imported via `programsmith sweep-read --kind sanity`.

    LIMITATION: the privilege-drop A/B probe (`nobody` cannot read `/private.enc` / a root-0700 dir)
    is NOT covered here — it needs local Docker and stays deferred to a Docker-capable env (ADR-0017).
    STATIC CI's `check-asset-encryption` is the structural backstop for the encryption side in the
    interim.
    """
    oracle = [t for t in trials if t.get("agent") == "oracle"]
    nop = [t for t in trials if t.get("agent") == "nop"]

    def _is(reward, val: float) -> bool:
        return reward == val or reward == int(val)

    oracle_reward = next((t.get("reward") for t in oracle), None)
    nop_reward = next((t.get("reward") for t in nop), None)
    checks = {
        "baselines_present": bool(oracle) and bool(nop),
        "oracle_baseline_reward_1": any(_is(t.get("reward"), 1.0) for t in oracle),
        "nop_baseline_reward_0": bool(nop) and all(_is(t.get("reward"), 0.0) for t in nop),
    }
    detail = {
        "checks": checks, "source": "baseline-trials",
        "oracle_reward": oracle_reward, "nop_reward": nop_reward,
        "priv_drop_ab": "deferred (needs local Docker, ADR-0017)",
    }
    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        return GateResult("fail", f"baseline SANITY failed: {failed}", detail)
    return GateResult("pass", "baseline oracle=1 / nop=0 (priv-drop A/B deferred to Docker, ADR-0017)",
                      detail)


def run_sanity(
    task_dir: Path,
    image_tag: str = "lh-sanity:task",
    *,
    build: bool = True,
    builder: Builder | None = None,
    phase_runner: PhaseRunner | None = None,
    build_timeout: int = 1800,
    run_timeout: int = 1800,
) -> GateResult:
    task_dir = Path(task_dir)
    builder = builder or _docker_builder(task_dir, image_tag, build_timeout)
    phase_runner = phase_runner or _docker_phase_runner(task_dir, image_tag, run_timeout)

    if build:
        ok, log = builder()
        if not ok:
            return GateResult("fail", "docker build failed", {"build_log_tail": log[-600:]})

    oracle_out = phase_runner(_ORACLE_SNIPPET)
    nop_out = phase_runner(_NOP_SNIPPET)
    priv_out = phase_runner(_PRIV_SNIPPET)

    oracle_reward = _reward(oracle_out)
    nop_reward = _reward(nop_out)
    owner = (re.search(r"PRODUCED_OWNER=(\S+)", oracle_out) or [None, None])[1]
    enc = (re.search(r"ENC=(\S+)", priv_out) or [None, None])[1]
    key = (re.search(r"KEY=(\S+)", priv_out) or [None, None])[1]

    checks = {
        "oracle_reward_1": oracle_reward == "1",
        # Files the verifier produced during phase A must be nobody-owned (proves the priv drop).
        # "none" = the verifier produced NO files — the rewrite-port verifier wrote result files,
        # the ProgramBench verify.py writes only reward/metrics (nothing under produced/), so an
        # empty set is vacuously fine (the pb10 false-fail); the ENC/KEY probes below still prove
        # the privilege boundary directly.
        "produced_owned_by_nobody": owner in ("nobody", "none"),
        "nop_reward_0": nop_reward == "0",
        "enc_denied_to_nobody": enc == "DENIED",
        "root700_denied_to_nobody": key == "DENIED",
    }
    detail = {
        "checks": checks,
        "oracle_reward": oracle_reward, "nop_reward": nop_reward,
        "produced_owner": owner, "enc_probe": enc, "key_probe": key,
    }
    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        return GateResult("fail", f"sanity failed: {failed}", detail)
    return GateResult("pass", "oracle=1 (nobody-owned), nop=0, priv probes DENIED", detail)
