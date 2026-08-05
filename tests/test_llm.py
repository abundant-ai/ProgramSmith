"""Offline tests for the LLM quarantine boundary (run_cell): envelope parsing, JSON extraction,
schema validation, and bounded retry. No subprocess is spawned — the runner is injected.
"""

import json

import pytest
from pydantic import BaseModel

from programsmith.llm import CellError, _envelope_text, _extract_json, run_cell


class Toy(BaseModel):
    name: str
    n: int


def _envelope(text: str) -> str:
    return json.dumps({"type": "result", "result": text})


def test_default_cell_model_is_cost_conscious():
    """ADR-0042 cell-model routing (reworked): heavy cells (oracle/golden capture, create fill,
    synthesize) default to Sonnet — the operator owns the bill; light one-shots (task matrix,
    annotations) pass cfg.cell_model_light explicitly at the call site — the constant is the
    HEAVY default."""
    from programsmith.llm import DEFAULT_CELL_MODEL
    assert DEFAULT_CELL_MODEL == "claude-sonnet-5"


def test_envelope_text_extracts_result():
    assert _envelope_text(_envelope("hello")) == "hello"


def test_envelope_text_falls_back_to_raw():
    assert _envelope_text("not-json-just-text") == "not-json-just-text"


def test_extract_json_handles_fence():
    assert _extract_json('prose ```json\n{"a": 1}\n``` trailing') == {"a": 1}


def test_extract_json_handles_bare_object():
    assert _extract_json('here: {"a": 1, "b": 2} done') == {"a": 1, "b": 2}


def test_run_cell_validates_bare_json():
    runner = lambda _p: _envelope('{"name": "x", "n": 3}')
    out = run_cell("go", Toy, runner=runner)
    assert isinstance(out, Toy) and out.name == "x" and out.n == 3


def test_run_cell_validates_fenced_json():
    runner = lambda _p: _envelope('```json\n{"name": "y", "n": 7}\n```')
    out = run_cell("go", Toy, runner=runner)
    assert out.n == 7


def test_run_cell_retries_then_succeeds():
    calls = {"i": 0}

    def runner(_p: str) -> str:
        calls["i"] += 1
        if calls["i"] == 1:
            return _envelope('{"name": "x"}')  # missing n -> invalid
        return _envelope('{"name": "x", "n": 1}')

    out = run_cell("go", Toy, runner=runner, max_retries=1)
    assert out.n == 1
    assert calls["i"] == 2  # retried exactly once


def test_run_cell_raises_after_exhausting_retries():
    runner = lambda _p: _envelope('{"name": "x"}')  # always missing n
    with pytest.raises(CellError):
        run_cell("go", Toy, runner=runner, max_retries=1)


def test_cli_runner_injects_saved_settings_and_surfaces_stdout_errors(monkeypatch):
    """The default runner passes a resolved environment so locally saved credentials reach the
    child. And a failure whose
    detail lands in STDOUT (`--output-format json` writes API errors there, stderr empty) must be
    surfaced in the CellError message — not swallowed."""
    import subprocess
    from programsmith.llm import claude_cli_runner

    captured = {}

    def fake_run(args, **kw):
        captured["args"] = args
        captured["env"] = kw.get("env")   # absent or None both mean "inherit the parent env"
        stdout = json.dumps({"type": "result", "is_error": True, "api_error_status": 401,
                             "result": "Failed to authenticate. API Error: 401 Invalid bearer token"})
        return subprocess.CompletedProcess(args, 1, stdout=stdout, stderr="")

    monkeypatch.setattr("programsmith.llm.subprocess.run", fake_run)
    monkeypatch.setattr("programsmith.config.model_subprocess_env",
                        lambda: {"ANTHROPIC_API_KEY": "sk-ant-test"})
    with pytest.raises(CellError, match="401"):
        claude_cli_runner(model="x")("go")
    assert isinstance(captured["env"], dict)
    assert "--bare" in captured["args"]
