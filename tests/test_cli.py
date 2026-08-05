"""Tests for the fleet-level CLI (key-based commands that mirror the UI). Network/LLM/spend paths
(new/farm ingest, matrix propose, probe --confirm-spend) are exercised only in their offline-safe
modes."""

import json
from argparse import Namespace

from programsmith.cells.task_matrix import TaskCandidate, TaskMatrixOutput
from programsmith.cli import main
from programsmith.manifest import Manifest, SourceInfo
from programsmith.runconfig import default_run_config
from programsmith.state import RunState


class _FakeResp:
    """Minimal urlopen() context-manager stand-in returning canned JSON bytes."""
    def __init__(self, payload: dict):
        self._d = json.dumps(payload).encode()

    def read(self):
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_version_flag(capsys):
    import pytest

    from programsmith import __version__

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"programsmith {__version__}"


def _capture_http(monkeypatch, response: dict) -> dict:
    """Patch urllib so the CLI's HTTP client hits no network; record the request it built."""
    import urllib.request
    seen: dict = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.method
        seen["body"] = json.loads(req.data.decode()) if req.data else None
        return _FakeResp(response)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    # neutralize any persisted config so api_url/presets don't leak into the test
    monkeypatch.setattr("programsmith.config.LhConfig.load", classmethod(lambda cls: cls()))
    return seen


def _make_run(runs, key, verdicts, *, sweeps=None, source=False):
    s = RunState.start(f"run-{key}", f"task:{key}", key)
    for v in verdicts:
        s.advance(v)
    s.save(runs / key)
    m = Manifest(run_id=s.run_id, task_identity=s.task_identity, slug=key)
    if source:
        m.source = SourceInfo(repo="o/n", pinned_sha="abcdef123456", primary_language="C")
    if sweeps:
        m.sweeps = sweeps
    m.save(runs / key)


def test_fleet_json_and_table(tmp_path, capsys):
    runs = tmp_path / "runs"
    _make_run(runs, "minpack", ["pass", "selected", "pass", "pass", "pass", "pass", "done"],
              sweeps={"difficulty": {"pass_at_1": 1.0, "status": "done"}})
    _make_run(runs, "dropped", ["fail"])
    rc = main(["fleet", "--runs-dir", str(runs), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["counters"]["total"] == 2 and out["counters"]["screened_out"] == 1
    assert out["counters"]["dropped"] == 0 and out["counters"]["admitted"] == 1
    keys = {r["key"] for r in out["runs"]}
    assert keys == {"minpack", "dropped"}
    # table mode renders the keys + pass@1
    main(["fleet", "--runs-dir", str(runs)])
    text = capsys.readouterr().out
    assert "minpack" in text and "1.0" in text and "STAGE" in text


def test_show_run(tmp_path, capsys):
    runs = tmp_path / "runs"
    _make_run(runs, "minpack", ["pass", "selected", "pass", "pass", "pass", "pass", "done"],
              sweeps={"difficulty": {"pass_at_1": 1.0}, "full": {"claude_code": 1.0, "codex": 0.2,
                                                                  "experiment": "993910a0"}},
              source=True)
    rc = main(["status", "minpack", "--runs-dir", str(runs)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "CALIBRATE" in out and "pass@1 1.0" in out
    assert "993910a0" in out and "sweep[full]" in out


def test_show_run_prefers_live_background_job_over_context_free_peek(tmp_path, capsys):
    from programsmith.jobs import set_job

    runs = tmp_path / "runs"
    _make_run(runs, "working", ["pass", "selected", "pass", "pass", "pass", "pass", "done",
                                  "proceed", "clean", "done", "revise"])
    set_job(runs / "working", "synthesize-h0-r1-e0", "running", stale_sec=6000)

    assert main(["status", "working", "--runs-dir", str(runs), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["active_job"] == "synthesize-h0-r1-e0"
    assert payload["waiting"] == {
        "stage": "SYNTHESIZE",
        "kind": "waiting",
        "reason": "synthesize-h0-r1-e0: background job running",
    }


def test_files_and_cat(tmp_path, capsys):
    runs = tmp_path / "runs"
    _make_run(runs, "r", ["pass", "selected"])
    task = runs / "r" / "task" / "r"
    task.mkdir(parents=True)
    (task / "instruction.md").write_text("# rewrite\nhello world")
    assert main(["dev", "files", "r", "--runs-dir", str(runs)]) == 0
    assert "instruction.md" in capsys.readouterr().out
    assert main(["dev", "cat", "r", "task/r/instruction.md", "--runs-dir", str(runs)]) == 0
    assert "hello world" in capsys.readouterr().out
    # missing file is a clean error, not a crash
    assert main(["dev", "cat", "r", "task/r/nope", "--runs-dir", str(runs)]) == 2


# The forward chain to QA_GATE (ADR-0039: FULL_SWEEP done → QA_GATE directly; no QA_ON_GPT hop).
_TO_QA_GATE = ["pass", "selected", "pass", "pass", "pass", "pass", "done", "proceed", "clean", "done"]


def test_qa_gate_cli_human_mode(tmp_path, capsys, monkeypatch):
    from programsmith.config import LhConfig
    outbox = tmp_path / "outbox"
    monkeypatch.setattr(LhConfig, "load",
                        classmethod(lambda cls: cls(qa_gate_mode="human", outbox_dir=str(outbox))))
    runs = tmp_path / "runs"
    _make_run(runs, "r", _TO_QA_GATE)  # at QA_GATE
    # a real task dir so the accept EXPORTS it (the human path must export, like the auto path)
    task = runs / "r" / "task" / "r"; task.mkdir(parents=True)
    (task / "task.toml").write_text("[task]\n")
    (task / "tests").mkdir(); (task / "tests" / "test.sh").write_text("#!/bin/sh\n")
    rc = main(["qa-gate", "r", "--decision", "accept", "--runs-dir", str(runs)])
    out = capsys.readouterr().out
    assert rc == 0 and "DONE" in out
    assert RunState.load(runs / "r").current_stage.value == "DONE"   # accept → export, never a PR
    # the accepted task was EXPORTED to the outbox (the pipeline's only output — the review-fixed gap)
    dest = outbox / "tasks" / "r"
    assert (dest / "task.toml").exists() and (dest / "tests" / "test.sh").exists()
    assert (dest / ".provenance.json").exists()
    # wrong stage is rejected
    _make_run(runs, "early", ["pass"])
    assert main(["qa-gate", "early", "--decision", "accept", "--runs-dir", str(runs)]) == 2


def test_qa_gate_cli_refuses_in_auto_mode(tmp_path, capsys, monkeypatch):
    """qa_gate_mode=auto (the default): the gate decides itself — a manual verdict would race it, so
    the CLI refuses with a pointer at the config key instead of advancing the FSM."""
    from programsmith.config import LhConfig
    monkeypatch.setattr(LhConfig, "load", classmethod(lambda cls: cls()))  # defaults → auto
    runs = tmp_path / "runs"
    _make_run(runs, "r", _TO_QA_GATE)
    rc = main(["qa-gate", "r", "--decision", "accept", "--runs-dir", str(runs)])
    assert rc == 2
    out = capsys.readouterr().out
    assert "qa_gate_mode=auto" in out and "qa_gate_mode=human" in out
    assert RunState.load(runs / "r").current_stage.value == "QA_GATE"  # untouched


def test_pr_command_removed():
    """`programsmith pr` is gone (ADR-0039: the pipeline exports to the outbox; no PR is ever opened)."""
    import pytest
    with pytest.raises(SystemExit):
        main(["pr", "--run-dir", "x"])


def test_forward_list_has_no_legacy_stages():
    from programsmith.cli import _FORWARD, _SWEEP_STAGE
    from programsmith.fsm import Stage
    assert Stage.QA_ON_GPT not in _FORWARD and Stage.PR not in _FORWARD
    assert _FORWARD[-1] is Stage.QA_GATE
    assert "qa_gpt" not in _SWEEP_STAGE and "qa_probe" in _SWEEP_STAGE


def test_sweep_read_qa_probe_parses_auditor_verdict(tmp_path, capsys):
    """The known bug: _SWEEP_STAGE mapped qa_probe but --kind choices excluded it. Now a pulled
    auditor trajectory reads back into manifest.sweeps['qa_probe'] with the SAME deterministic
    mapping the auto-driver uses (clean / harden; no verdict invented on unparsable output)."""
    from programsmith.manifest import Manifest as M
    runs = tmp_path / "runs"
    _make_run(runs, "r", ["pass", "selected", "pass", "pass", "pass", "pass", "done", "proceed"])
    pull = tmp_path / "pull"
    pull.mkdir()
    (pull / "trajectory.log").write_text('report: {"verdict": "SOLVABLE_AS_WRITTEN", "findings": []}')
    rc = main(["dev", "sweep-read", "--run-dir", str(runs / "r"), "--kind", "qa_probe",
               "--from-pull", str(pull), "--advance"])
    assert rc == 0
    assert "clean" in capsys.readouterr().out
    assert RunState.load(runs / "r").current_stage.value == "FULL_SWEEP"
    entry = M.load(runs / "r").sweeps["qa_probe"]
    assert entry["verdict"] == "clean" and entry["auditor_verdict"] == "SOLVABLE_AS_WRITTEN"
    # a gameable verdict maps to harden (rc 1); unparsable output records complete_unparsed
    _make_run(runs, "bad", ["pass", "selected", "pass", "pass", "pass", "pass", "done", "proceed"])
    (pull / "trajectory.log").write_text('{"verdict": "UNSOLVABLE"}')
    assert main(["dev", "sweep-read", "--run-dir", str(runs / "bad"), "--kind", "qa_probe",
                 "--from-pull", str(pull)]) == 1
    assert M.load(runs / "bad").sweeps["qa_probe"]["verdict"] == "harden"
    (pull / "trajectory.log").write_text("no json here")
    assert main(["dev", "sweep-read", "--run-dir", str(runs / "bad"), "--kind", "qa_probe",
                 "--from-pull", str(pull)]) == 1
    assert M.load(runs / "bad").sweeps["qa_probe"]["status"] == "complete_unparsed"


def test_pick_cli(tmp_path, capsys):
    runs = tmp_path / "runs"
    _make_run(runs, "r", ["pass"], source=True)  # at TASK_MATRIX
    out = TaskMatrixOutput(source_ref="o/n@abcdef1234", candidates=[
        TaskCandidate(tool_name="minpack", binary_name="minpack", upstream_language="c",
                      flag_surface="solve hybrd lmdif --tol --maxfev",
                      case_families=["solvers", "tolerances", "errors", "io-modes", "version"],
                      est_kloc=12, stdin_friendly=True, needs_files_dir=False,
                      deterministic_output=True, expected_difficulty="hard", expert_hours=20,
                      recommendation="recommended", rationale="rich CLI surface",
                      basis_ref="programbench-farm task_generator")])
    (runs / "r" / "task_matrix.json").write_text(out.model_dump_json())
    rc = main(["pick", "r", "--index", "0", "--runs-dir", str(runs)])
    assert rc == 0
    assert RunState.load(runs / "r").current_stage.value == "ORACLE_GOLDEN"
    # the pick went through apply_selection: ProgramBench dimensions + the ADR-0038 identity axes
    from programsmith.manifest import Manifest as M, programbench_task_identity
    man = M.load(runs / "r")
    assert man.dimensions.tool_name == "minpack" and man.dimensions.upstream_language == "c"
    assert man.task_identity == programbench_task_identity(
        "o/n", "abcdef123456", "minpack", "solve hybrd lmdif --tol --maxfev")
    assert RunState.load(runs / "r").task_identity == man.task_identity


def test_pause_resume_cli(tmp_path):
    runs = tmp_path / "runs"
    _make_run(runs, "r", ["pass"])
    assert main(["pause", "r", "--runs-dir", str(runs)]) == 0
    assert RunState.load(runs / "r").paused
    assert main(["pause", "r", "--resume", "--runs-dir", str(runs)]) == 0
    assert not RunState.load(runs / "r").paused


def test_advance_all_is_safe_when_blocked(tmp_path, capsys):
    runs = tmp_path / "runs"
    _make_run(runs, "r", ["pass", "selected", "pass", "pass"])  # at SANITY, no baseline/docker
    rc = main(["dev", "advance", "--all", "--runs-dir", str(runs)])
    assert rc == 0  # parks blocked, never crashes


# ---- run-creation parity: config/brief threading + the API-client path (feature 3) ---------------

def test_resolve_cli_run_config_from_file(tmp_path):
    from programsmith.cli import _resolve_cli_run_config
    f = tmp_path / "rc.json"
    f.write_text(json.dumps(default_run_config().model_dump()))
    rc = _resolve_cli_run_config(Namespace(config=str(f), preset=None))
    assert rc["full"]["band"]["max_pass"] == 0.70   # the 1/3–2/3 Opus frontier band (ADR-0040)
    assert _resolve_cli_run_config(Namespace(config=None, preset=None)) is None


def test_parse_agent_spec_forms(monkeypatch, tmp_path):
    """`[harness:]provider/model` — the bare form takes the credential-aware default harness;
    an unknown harness or an un-routable model prefix fails fast with the options named."""
    import pytest
    from programsmith.cli import _parse_agent_spec
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "cfg.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")     # → default harness mini-swe
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    a = _parse_agent_spec("gemini/gemini-3-flash")
    assert (a.harness, a.model, a.n_trials) == ("mini-swe", "gemini/gemini-3-flash", 3)
    b = _parse_agent_spec("codex:openai/gpt-5.5", n_trials=5)
    assert (b.harness, b.model, b.n_trials) == ("codex", "openai/gpt-5.5", 5)
    with pytest.raises(SystemExit, match="unknown harness"):
        _parse_agent_spec("terminus:openai/gpt-5.5")
    with pytest.raises(SystemExit, match="provider prefix"):
        _parse_agent_spec("mini-swe:gpt-5.5")                  # bare model, no litellm prefix


def test_smoke_frontier_flags_override_stage_agents(monkeypatch, tmp_path):
    """`--smoke`/`--frontier` replace that stage's agent over the defaults (or a file/preset),
    leaving the other stage untouched — the one-flag multi-provider path."""
    from programsmith.cli import _resolve_cli_run_config
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "cfg.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    rc = _resolve_cli_run_config(Namespace(config=None, preset=None,
                                           smoke="gemini/gemini-3-flash",
                                           frontier="codex:openai/gpt-5.5"))
    assert rc["difficulty"]["agents"] == [
        {"harness": "mini-swe", "model": "gemini/gemini-3-flash", "n_trials": 3}]
    assert rc["full"]["agents"] == [
        {"harness": "codex", "model": "openai/gpt-5.5", "n_trials": 3}]
    assert rc["full"]["band"]["max_pass"] == 0.70              # bands keep their defaults
    only_frontier = _resolve_cli_run_config(Namespace(config=None, preset=None, smoke=None,
                                                      frontier="zai/glm-5.2"))
    assert only_frontier["difficulty"]["agents"][0]["model"] == "anthropic/claude-haiku-4-5"
    assert only_frontier["full"]["agents"][0]["model"] == "zai/glm-5.2"


def test_api_base_precedence(monkeypatch):
    from programsmith.cli import _api_base
    monkeypatch.setattr("programsmith.config.LhConfig.load", classmethod(lambda cls: cls()))
    monkeypatch.delenv("PROGRAMSMITH_API", raising=False)
    assert _api_base(Namespace(api="https://x")) == "https://x"          # explicit flag wins
    monkeypatch.setenv("PROGRAMSMITH_API", "https://env")
    assert _api_base(Namespace(api=None)) == "https://env"               # env next
    monkeypatch.delenv("PROGRAMSMITH_API", raising=False)
    assert _api_base(Namespace(api=None)) is None                        # config default → None


def test_new_via_api_posts_full_body_to_prod(tmp_path, monkeypatch, capsys):
    """`programsmith dev new --api <url> --config … --brief …` POSTs the same shape the New-run button
    does, so a CLI-created run lands on a served fleet with full config — the parity requirement."""
    seen = _capture_http(monkeypatch, {"key": "foo", "status": "ingesting"})
    f = tmp_path / "rc.json"
    f.write_text(json.dumps(default_run_config().model_dump()))
    rc = main(["dev", "new", "--repo", "o/n", "--slug", "foo", "--brief", "port it",
               "--config", str(f), "--api", "https://prod.example", "--yes"])
    assert rc == 0
    assert seen["url"] == "https://prod.example/api/runs" and seen["method"] == "POST"
    assert seen["body"]["repo"] == "o/n" and seen["body"]["slug"] == "foo"
    assert seen["body"]["brief"] == "port it" and "full" in seen["body"]["config"]
    assert "foo" in capsys.readouterr().out


def test_farm_via_api_posts_each_spec(monkeypatch, capsys):
    seen = _capture_http(monkeypatch, {"key": "k", "status": "ingesting"})
    rc = main(["farm", "a/one", "b/two@deadbeef", "--api", "https://prod", "--yes"])
    assert rc == 0
    # the LAST spec's body is retained; it carries the parsed @sha
    assert seen["url"] == "https://prod/api/runs" and seen["body"]["sha"] == "deadbeef"
    assert "FARM" in capsys.readouterr().out


def test_remote_creation_requires_action_time_model_usage_consent(monkeypatch):
    monkeypatch.setattr("programsmith.config.LhConfig.load", classmethod(lambda cls: cls()))
    monkeypatch.setattr("programsmith.ux.confirm_or_abort", lambda **kwargs: False)
    monkeypatch.setattr("programsmith.cli._api_post",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("posted")))
    rc = main(["dev", "new", "--repo", "a/one", "--api", "https://prod"])
    assert rc == 130


def test_presets_list_via_api(monkeypatch, capsys):
    seen = _capture_http(monkeypatch, {"presets": {"fast": {}, "deep": {}}})
    rc = main(["presets", "--api", "https://prod"])
    assert rc == 0 and seen["method"] == "GET" and seen["url"] == "https://prod/api/presets"
    out = capsys.readouterr().out
    assert "fast" in out and "deep" in out
