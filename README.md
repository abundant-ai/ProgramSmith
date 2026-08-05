<p align="center">
  <a href="https://github.com/abundant-ai/ProgramSmith">
    <img src="https://raw.githubusercontent.com/abundant-ai/ProgramSmith/main/assets/icon.png" style="height: 9em" alt="ProgramSmith forge" />
  </a>
</p>

<p align="center">
  <a href="https://www.python.org/downloads/">
    <img alt="Python" src="https://img.shields.io/badge/python-3.11+-blue.svg">
  </a>
  <a href="https://opensource.org/licenses/Apache-2.0">
    <img alt="License" src="https://img.shields.io/badge/License-Apache%202.0-blue.svg">
  </a>
  <a href="https://pypi.org/project/programsmith/">
    <img alt="PyPI" src="https://img.shields.io/pypi/v/programsmith.svg">
  </a>
</p>

# ProgramSmith

> Convert GitHub repos into [Harbor](https://github.com/laude-institute/harbor) tasks automatically.

## Overview

Automates task creation from real open-source repos. Input any GitHub repo, and it produces a long-horizon ProgramBench-style reverse engineering task.

Every task is difficulty-calibrated and audited before export. If reward-hacking, task environment problems, or verifier design issues are found, the pipeline continues iterating through a loop until all QA and difficulty calibration is met. Task execution and grading are containerized, with required dependencies installed when each task image is built.

## Quick Start

```bash
# Install
uv pip install programsmith

# Generate a task from a repo
programsmith create --repo d5/tengo

# Or farm multiple tasks at once
programsmith farm repos.txt
```

## Installation

```bash
uv pip install programsmith
```

Ensure Docker is running and at least one model credential is set:

```bash
export CLAUDE_CODE_OAUTH_TOKEN=<token>   # (run `claude setup-token` in your CLI)
# or
export ANTHROPIC_API_KEY=<api-key>
```

Trials can run on any provider:

```bash
export OPENAI_API_KEY=<api-key>          # codex CLI + any openai/* model
export GEMINI_API_KEY=<api-key>          # gemini CLI + any gemini/* model
export ZAI_API_KEY=<api-key>             # GLM models via the mini-SWE-agent harness
```

## Usage

Alias: `psmith <cmd>` is automatically installed

**Commands:**
- `programsmith create` — Generate a task from a repo
- `programsmith farm` — Farm a list of repos into multiple tasks
- `programsmith serve` — Start the browser dashboard in the background
- `programsmith stop` — Stop the background dashboard
- `programsmith doctor` — Preflight checks (Docker, credentials, disk)
- `programsmith status` — Pipeline status

### Generate a Task

```bash
programsmith create --repo <owner/repo>
```

Runs the full pipeline (see below). Accepted tasks are exported to `out/tasks/`, tasks that fail
calibration but are sound are exported to `out/easy/`.

Re-running the same command resumes the run from wherever it parked. Ctrl-C stops active model
trials, keeps completed trials, and leaves unfinished work ready to resume.

<details>
<summary>Options</summary>

- `--sha SHA` — Pin a commit (default: resolve HEAD)
- `--slug NAME` — Run key (default: repo name)
- `--smoke [HARNESS:]PROVIDER/MODEL` — Initial smoke sweep agent
  (default: `anthropic/claude-haiku-4-5` on the credential-aware harness)
- `--frontier [HARNESS:]PROVIDER/MODEL` — Difficulty calibration agent
  (default: `anthropic/claude-opus-4-8`; e.g. `codex:openai/gpt-5.5`,
  `gemini-cli:gemini/gemini-3.1-pro-preview`, `mini-swe:zai/glm-5.2`)
- `--config FILE.json` / `--preset NAME` — Full RunConfig (agents + per-model bands)
- `--brief TEXT` — Steer the task generation scope (eg "port the FFT subsystem...")
- `--review` — Pause at the two human gates (scope pick, final QA) instead of auto
- `--yes` — Skip the cost preview confirmation
- `--runs-dir PATH` — Choose the directory for runs (default: `.programsmith/runs`)
- `--allow-copyleft` — Allow copyleft-licensed sources

</details>

### Farm Multiple Repositories

```bash
programsmith farm repos.txt
```

Streams through a repo list (one `owner/name[@sha]` per line) and drives every run to
completion in the foreground. Re-running the command picks
up existing runs where they stopped. A curated starter list of known-good repos is included
([`pb10-repos.txt`](pb10-repos.txt)).

<details>
<summary>Options</summary>

- All flags from `create`: `--smoke`, `--frontier`, `--config`, `--preset`,
  `--review`, `--yes`, `--runs-dir`, shared for all runs
- `--no-drive` — Only create + ingest the runs (drive them later with `serve`)
- `--no-prune` — Skip the `docker image prune` hygiene between completed runs

</details>

### Dashboard

```bash
programsmith serve
# The dashboard runs in the background and survives Ctrl-C / closing this shell.
programsmith stop
```

Serves the local dashboard at `http://localhost:8765`: live pipeline DAG for each run, agent
output, sweep results, file explorer, and optional review gates. Evaluation sweeps remain parked
unless `serve --spend` is used.
`programsmith serve` returns after the dashboard is healthy; `programsmith stop` is the explicit
shutdown command.

## Task Requirements

<details>
<summary>What repos work well</summary>

**Best sources:** small-to-medium CLI tools with deterministic stdin/stdout behavior
(formatters, converters, parsers, interpreters, compression tools, query tools).

**A good source repo:**
- Builds cleanly in a container (Go, Rust, C, C++, etc)
- Has a CLI surface with deterministic, byte-reproducible output
- Is permissively licensed (verified in pipeline)
- Is not already saturated by frontier models

The pipeline also auto-rejects repos already used by public ProgramBench-style
datasets.

</details>

## Pipeline

![ProgramSmith pipeline](assets/pipeline.png?raw=1)

<details>
<summary>Pipeline details</summary>

ProgramSmith runs a fixed DAG of deterministic gates. LLM work is quarantined to
synthesis cells whose JSON output is schema-validated before passing any gate. The
orchestrator routes only on gate verdicts.

1. **Ingest + Lock** — clone, pin SHA, check license and overlap
2. **Task Matrix** — pick the task scope
3. **Oracle + Goldens** — build the reference oracle and generate public + held-out
   golden I/O cases (including adversarial cases)
4. **Create** — make the task: repo, instruction, verifier, Dockerfile
5. **Sanity Check** — check oracle scores 1, nop (empty solution) scores 0
6. **Static Checks** — anti-cheat check suite (closed internet, asset
   encryption, no reviewer-visible goldens, reward format, etc)
7. **Smoke Sweep** — N smoke-model trials; 100% pass ⇒ saturated ⇒ ease or drop
8. **Calibrate** — band check; out-of-band tasks are hardened/eased via patches
9. **Audit Probe** — adversarial agent looks for reward hacks, exploits are fixed
10. **Frontier Sweep** — N frontier-model trials; target pass@1 in [1/3, 2/3] or
    whatever is specified
11. **QA Gate** — good-failure analysis on 0-pass tasks, accepted tasks are exported

</details>

## License

[Apache License 2.0](LICENSE)
