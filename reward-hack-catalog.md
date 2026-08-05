# Reward-hacking defense catalog

This is ProgramSmith's living catalog of known task reward exploits. The QA/PROBE cell checks
against it, STATIC CI scans for banned patterns, and each entry names a **structural backstop**:
a capability, sandbox, or encryption defense instead of a brittle source-code regex.

- **Status legend:** `CONFIRMED` (achieved reward=1 end-to-end) · `POC` (proven exploit, not full
  reward) · `VECTOR` (plausible, not yet demonstrated) · `BLOCKED` (closed by current design).

| # | Exploit | Class | Status | Structural backstop | Validation |
|---|---------|-------|--------|---------------------|----------|
| H1 | **Stdout tally injection** — agent code spawns a thread spamming fake `PUBLIC: N/N` / `HELDOUT: N/N`; a verifier that `grep … \| tail -1` trusts the fabricated line | output-forgery | **CONFIRMED** | Verifier must compute the tally itself in Phase B from trusted produced files, never parse agent stdout for the score; produced data written by privilege-dropped Phase A, graded by root Phase B with no agent code in scope | Reproduced end-to-end against vulnerable verifier designs |
| H2 | **Read golden files off disk at runtime** — agent `compile()`/`interpret()` scans `/app/tests/goldens/*` and returns the matching expected output | golden-exfil | **CONFIRMED POC** | Goldens never on the agent-readable filesystem during the run; expected values injected only into the root-only `$SECRET` dir at grade time; held-out *inputs* only (never expected) staged to Phase A | Reproduced against a verifier that exposed golden files |
| H3 | **Hardcode / special-case public outputs** — bake expected outputs keyed by input string | overfit-public | **VECTOR** | reward=1 requires **all public AND all held-out** to pass; held-out inputs are hidden from the agent → memorizing public cannot win | Covered by held-out cases |
| H4 | **Decrypt world-readable `/private.enc`** | asset-exfil | **BLOCKED** | passphrase absent from the agent-time image (lives only in `test.sh`, mounted at verify time); `check-asset-encryption` enforces encrypt-at-build + `rm` plaintext | Checked by asset-encryption CI |
| H5 | **Grade-time golden read by Phase-A code** — agent code, while running under the grader, reads expected values | priv-escalation | **BLOCKED** | Phase A runs as `nobody`; `/private.enc` chmod 600; expected files in a root-0700 dir; the verifier probes that `nobody` cannot read either | Checked by privilege-boundary probes |
| H6 | **Verifier-surface tampering** — modify `tests/`, dependency manifests, goldens, or build scripts to subvert grading | surface-tamper | **BLOCKED (partial)** | SETUP restores a pristine harness over `/app`. Grading must not trust anything mutable by `/app/src` | Checked by pristine-restore probes |
| H7 | **Held-out input leak to Phase-A code** | info-leak | **ACCEPTED (by design)** | Phase A sees held-out *inputs* (it must, to produce outputs) but never expected outputs; leaking inputs is harmless | Explicit trust-boundary decision |
| H8 | **Build-time supply-chain backdoor** — tamper with a dependency or build hook to write an answer key to a shared temporary location | supply-chain | **POC (disarmed)** | dependencies pre-vendored offline; closed internet; verifier process isolation from temporary artifacts | Covered by offline-build and isolation checks |
| H9 | **FFI / link to the source-language library** — call the original implementation instead of porting it | non-port | **VECTOR** | closed internet; source toolchain absent from the build image; linkage and symbol checks; original sources absent from the agent environment | Covered by task-specific anti-hack rules |
| H10 | **Vendor an existing third-party port** of the target | non-port | **VECTOR** | closed internet; dependencies pre-vendored and known; similarity check and dependency allowlist | Covered by dependency and similarity checks |
| H11 | **Weaken or delete tests** | test-tamper | **BLOCKED** | pristine restore of the test harness at setup; tests mounted only at verify time | Covered by pristine-restore checks |
| H12 | **Network egress to fetch any of the above** | egress | **BLOCKED** | `allow_internet=false` plus the closed-internet check; verifier environment denies egress | Checked by STATIC CI |

## Verifier-correctness risks (false negatives — not hacks, but catalog-tracked)
A too-tight ε or fragile comparison makes a *correct* port fail (task looks impossibly hard).
The ORACLE cell's ε proposal + the reference-port SANITY pass guard against these.
- **F1** — Relative-only tolerances become unsatisfiable for near-zero residuals; combine absolute
  and relative tolerances.
- **F2** — Trailing-newline behavior can diverge between language runtimes; normalize it explicitly.
- **F3** — Unanchored cleanup regexes can silently remove valid output.
- **F4** — Empty strings, Unicode escapes, and diagnostic column offsets need explicit edge cases.

## Static-scan patterns
These ban *brittle author anti-cheat*, steering toward capability-level defenses. The probe and
STATIC CI reuse them; they also document the shapes an exploit author would target.
- **B1** import-scan regex literal: `r"…\b?(?:import|from)\s+…"`.
- **B2** shell `grep` for `import`/`from` over `/app` / `rglob` / `/workspace`.
- **B3** bare lowercase library-name regex `\b<ident≥5>\b` (deny-list excludes scoring tokens
  `passed|failed|total|score|reward|metrics|logs|tests|…`).
- Suppression: same-line `# anti-cheat-ok: <reason ≥10 chars>`.
- Sensitive-asset basename (must be encrypted): `^(solution|solve|hidden|private|secret|oracle|answer|goldens?|gold)([._-]|$)`.

## Structural lesson (load-bearing)
Both confirmed hacks (H1, H2) exploit one weakness: **agent `/app/src` code runs in the same
process & filesystem as the grader's trusted data, and `/app/src` is the one tree not restored.**
The hardened two-phase, privilege-dropped verifier (PRODUCE as `nobody` with inputs only → GRADE
as root with no agent code) is the canonical defense and must be the CREATE/SYNTHESIZE default for
any task whose verifier executes agent code in-process.
