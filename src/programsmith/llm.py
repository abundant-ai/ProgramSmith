"""LLM quarantine boundary (invariant #4).

LLM cells call the model ONLY through `run_cell`, which:
  1. shells out to `claude -p --output-format json` (authenticated by the CLI's own keychain
     login, CLAUDE_CODE_OAUTH_TOKEN, or ANTHROPIC_API_KEY — a straight env passthrough),
     resolving the binary directly to bypass any shell alias;
  2. extracts the assistant text from the CLI's JSON envelope;
  3. parses the embedded JSON and **validates it against the cell's pydantic schema before
     returning** — with a bounded retry that feeds the validation error back to the model.

A cell can therefore never hand un-validated output to the next gate. The subprocess runner is
injectable (`runner=`) so tests run fully offline.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

# A runner maps a prompt string to the model's raw stdout (the CLI JSON envelope, or bare text).
Runner = Callable[[str], str]

DEFAULT_TIMEOUT_SEC = 600
# Cells must pass an explicit model (the CLI's env default drifts). ADR-0042 cell-model routing:
# HEAVY cells (oracle/golden capture, create fill, synthesize plan) run this default; light
# one-shots (task matrix, annotations) pass cfg.cell_model_light explicitly at the call site;
# trajectory audits pass cfg.cell_model_analysis. Cost-conscious default — the operator owns the bill.
DEFAULT_CELL_MODEL = "claude-sonnet-5"  # heavy-cell default (ADR-0042 rework)


class CellError(RuntimeError):
    """Raised when a cell's output cannot be validated against its schema after retries."""


def claude_cli_runner(
    model: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SEC,
) -> Runner:
    """Default runner: `claude -p --model <m> --output-format json`. Reads the prompt on stdin (no
    arg-length limit). The binary is resolved directly via PATH (not the shell), so the interactive
    `claude` alias does not interfere. An explicit `--model` is always passed (the env default model
    drifts).

    Credentials may come from the inherited environment, the CLI keychain, or the local owner-only
    ProgramSmith settings file."""
    from .cells.agentic import (  # deferred: avoid import-time cycle
        FOCUS_PROMPT,
        LEAN_MCP_FLAGS,
        api_key_cli_flags,
    )
    claude_bin = os.getenv("CC_LOGGER_REAL_CLAUDE") or "claude"
    # Lean config (see cells.agentic): a one-shot JSON completion must not drag in the host's
    # SuperClaude framework / MCP servers — that was making this call hang to its 10-min timeout.
    # AND it must run TOOL-FREE: this is the LLM-quarantine boundary (a structured completion, not an
    # agent). With tools enabled, a task prompt that names files (synthesize's instruction.md/grader,
    # the source repo) makes `claude -p` try to Read/Glob/Bash for that context — which isn't in the
    # neutral cwd — so it spins multi-turn and, since --output-format json doesn't stream, looks dead
    # until the timeout (the cjson plan-gen wedge). Disabling the filesystem tools forces the single-turn
    # JSON answer the schema expects. Safe for every run_cell caller: synthesize_plan + task_matrix both
    # pass all needed context IN the prompt; the agent/apply phase keeps its tools (claude_code_session,
    # not this runner). Only the CORE, long-stable tool names are listed — an unknown name (e.g. the
    # removed `MultiEdit`) makes the CLI hard-exit 1 ("matches no known tool"), so we keep this list to
    # foundational tools that won't churn. These six remove all read/search/exec/edit capability, which
    # is what prevents the exploration spin.
    _no_tools = ["--disallowedTools", "Bash", "Read", "Glob", "Grep", "Edit", "Write"]
    args = [claude_bin, "-p", "--model", model or DEFAULT_CELL_MODEL, "--output-format", "json",
            *LEAN_MCP_FLAGS, *_no_tools, "--append-system-prompt", FOCUS_PROMPT]

    def _run(prompt: str) -> str:
        from .config import model_subprocess_env
        env = model_subprocess_env()
        run_args = [args[0], *api_key_cli_flags(env), *args[1:]]
        proc = subprocess.run(
            run_args, input=prompt, capture_output=True, text=True,
            cwd=tempfile.gettempdir(), timeout=timeout,  # neutral cwd → no project CLAUDE.md
            env=env,
        )
        from .costlog import record_envelope
        record_envelope(proc.stdout, model=model or DEFAULT_CELL_MODEL)
        if proc.returncode != 0:
            # `--output-format json` writes API errors (e.g. 401 Invalid bearer token) to STDOUT,
            # not stderr — surface whichever carries the detail.
            detail = (proc.stderr.strip() or proc.stdout.strip())[:500]
            raise CellError(f"claude CLI exited {proc.returncode}: {detail}")
        return proc.stdout

    return _run


def _envelope_text(stdout: str) -> str:
    """Pull the assistant's final text out of the Claude Code `--output-format json` envelope.
    Falls back to raw stdout if it isn't the expected shape."""
    stdout = stdout.strip()
    try:
        obj = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout
    if isinstance(obj, dict):
        for key in ("result", "text", "content", "completion"):
            val = obj.get(key)
            if isinstance(val, str):
                return val
        # content as a list of blocks
        if isinstance(obj.get("content"), list):
            parts = [b.get("text", "") for b in obj["content"] if isinstance(b, dict)]
            if parts:
                return "\n".join(parts)
    return stdout


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)


def _extract_json(text: str) -> dict | list:
    """Extract the JSON value from model text (handles ```json fences and surrounding prose)."""
    m = _FENCE_RE.search(text)
    if m:
        return json.loads(m.group(1))
    # else take the outermost {...} or [...]
    start = min((text.find(c) for c in "{[" if text.find(c) != -1), default=-1)
    if start == -1:
        raise CellError(f"no JSON object found in model output: {text[:300]}")
    end = max(text.rfind("}"), text.rfind("]"))
    return json.loads(text[start : end + 1])


def run_cell(
    prompt: str,
    model_cls: type[T],
    *,
    runner: Runner | None = None,
    model: str | None = None,
    max_retries: int = 1,
) -> T:
    """Run an LLM cell and return a validated `model_cls` instance (or raise CellError).

    The model is instructed to emit JSON matching `model_cls`'s JSON Schema; the response is parsed
    and validated. On a parse/validation failure the error is appended and the call retried, up to
    `max_retries` extra attempts.
    """
    run = runner or claude_cli_runner(model=model)
    schema = json.dumps(model_cls.model_json_schema(), indent=2)
    full_prompt = (
        f"{prompt}\n\n"
        "Respond with ONLY a single JSON value conforming exactly to this JSON Schema "
        "(no prose, no markdown fences):\n"
        f"{schema}\n"
    )

    last_err: Exception | None = None
    attempt_prompt = full_prompt
    for _attempt in range(max_retries + 1):
        raw = run(attempt_prompt)
        try:
            parsed = _extract_json(_envelope_text(raw))
            return model_cls.model_validate(parsed)
        except (CellError, ValidationError, json.JSONDecodeError) as e:
            last_err = e
            attempt_prompt = (
                f"{full_prompt}\n\nYour previous response was invalid: {e}\n"
                "Return corrected JSON only."
            )
    raise CellError(f"cell output failed schema validation after retries: {last_err}")
