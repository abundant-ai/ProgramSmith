"""Tests for the UI read layer (RunStore) + pause/stop, and a guarded HTTP smoke test."""

import json
import os

import pytest

from programsmith.manifest import Manifest
from programsmith.state import RunState
from programsmith.ui.store import RunStore


def _make_run(runs, key, verdicts, sweeps=None):
    s = RunState.start(f"run-{key}", f"task:{key}", key)
    for v in verdicts:
        s.advance(v)
    s.save(runs / key)
    m = Manifest(run_id=s.run_id, task_identity=s.task_identity, slug=key)
    if sweeps:
        m.sweeps = sweeps
    m.save(runs / key)


def test_list_summaries_and_counters(tmp_path):
    runs = tmp_path / "runs"
    _make_run(runs, "minpack", ["pass", "selected", "pass"],  # at CREATE
              sweeps={"difficulty": {"claude_code_pass_at_1": ">=0.5"}})
    _make_run(runs, "dropped-one", ["fail"])  # DROPPED
    store = RunStore(runs)
    sums = {s.key: s for s in store.list_summaries()}
    assert sums["minpack"].stage == "CREATE" and sums["minpack"].difficulty_pass_at_1 == ">=0.5"
    assert sums["minpack"].source_admitted is True
    assert sums["dropped-one"].status == "dropped" and sums["dropped-one"].screened_out
    c = store.fleet_counters()
    assert c["total"] == c["sourced"] == 2
    assert c["screened_out"] == 1 and c["dropped"] == 0
    assert c["admitted"] == 1 and c["in_progress"] == 1


def test_task_matrix_empty_is_counted_as_source_screened_out(tmp_path):
    runs = tmp_path / "runs"
    _make_run(runs, "screened", ["pass", "none_selected"])
    store = RunStore(runs)
    summary = store.summary("screened")
    assert summary.status == "dropped" and summary.screened_out is True
    counters = store.fleet_counters()
    assert counters["screened_out"] == 1 and counters["dropped"] == 0
    assert counters["admitted"] == 0 and counters["screening"] == 0


def test_screened_out_http_detail_surfaces_specific_matrix_reason(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    runs = tmp_path / "runs"
    _make_run(runs, "screened", ["pass", "none_selected"])
    reason = "interactive TUI with no deterministic batch output mode"
    (runs / "screened" / "task_matrix.json").write_text(json.dumps({
        "source_ref": "o/n@abc", "candidates": [], "no_candidate_reason": reason,
    }))
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.delenv("ODDISH_API_KEY", raising=False)
    from programsmith.config import LhConfig
    LhConfig(runs_dir=str(runs)).save()
    from programsmith.ui.app import app

    detail = TestClient(app).get("/api/runs/screened").json()
    assert detail["summary"]["screened_out"] is True
    assert detail["waiting"]["reason"] == reason
    assert detail["waiting"]["can_reopen"] is False


def test_progress_is_nonzero_when_looping_at_synthesize(tmp_path):
    """Regression: a run looping at SYNTHESIZE (off the forward chain) must not show 0% progress —
    `progress` reflects the FURTHEST forward stage reached, from history."""
    runs = tmp_path / "runs"
    _make_run(runs, "r", ["pass", "selected", "pass", "pass", "fail"])  # SANITY fail -> SYNTHESIZE
    s = RunStore(runs).summary("r")
    assert s.stage == "SYNTHESIZE"
    assert 0.0 < s.progress < 1.0  # reached SANITY then looped, not stuck at 0


def test_progress_tracks_current_stage_after_backward_move(tmp_path):
    """Regression (pb10 fleet view): a run routed BACKWARD onto an earlier forward stage must show
    its CURRENT position, not the furthest stage history ever touched — runs rewound from SANITY to
    ORACLE_GOLDEN all read the same 45% as the run actually AT SANITY, making the bar meaningless."""
    runs = tmp_path / "runs"
    _make_run(runs, "fwd", ["pass", "selected", "pass", "pass"])         # at SANITY, never looped
    at_sanity = RunStore(runs).summary("fwd").progress
    key = "back"
    s = RunState.start(f"run-{key}", f"task:{key}", key)
    for v in ("pass", "selected", "pass", "pass"):                        # reach SANITY
        s.advance(v)
    s.current_stage = __import__("programsmith.fsm", fromlist=["Stage"]).Stage.ORACLE_GOLDEN
    s.save(runs / key)
    Manifest(run_id=s.run_id, task_identity=s.task_identity, slug=key).save(runs / key)
    back = RunStore(runs).summary(key)
    assert back.stage == "ORACLE_GOLDEN"
    assert back.progress < at_sanity  # shows the real (earlier) position, not the high-water mark


def test_progress_full_when_done(tmp_path):
    runs = tmp_path / "runs"
    # ADR-0039 flow: FULL_SWEEP done → QA_GATE, accept → DONE (exported; no QA_ON_GPT/PR hops)
    _make_run(runs, "r", ["pass", "selected", "pass", "pass", "pass", "pass", "done",
                          "proceed", "clean", "done", "accept"])  # -> DONE
    s = RunStore(runs).summary("r")
    assert s.status == "done" and s.progress == 1.0
    assert s.exported is True and s.ready_for_pr is True   # ready_for_pr = deprecated alias


def test_completed_draft_is_not_reported_as_in_progress(tmp_path):
    """Drafts park at DIFFICULTY_SWEEP so they remain resumable, but the UI must present the
    recorded draft halt as a completed, 100% Static-CI output—not a stuck sweep at 64%."""
    import json

    runs = tmp_path / "runs"
    _make_run(runs, "draft", ["pass", "selected", "pass", "pass", "pass", "pass"])
    manifest = Manifest.load(runs / "draft")
    manifest.pipeline_mode = "draft"
    manifest.save(runs / "draft")
    (runs / "draft" / "drive.json").write_text(json.dumps({
        "halted": "draft",
        "final_stage": "DIFFICULTY_SWEEP",
        "halt_reason": "draft complete — passed Static CI; exported to out/drafts/draft",
    }))

    store = RunStore(runs)
    summary = store.summary("draft")
    assert summary.status == "draft" and summary.progress == 1.0
    assert summary.blocked is False and summary.waiting is False
    assert store.fleet_counters()["drafts"] == 1
    assert store.fleet_counters()["in_progress"] == 0
    nodes = store.node_statuses(store.get_state("draft"), draft_complete=True)
    assert nodes["STATIC_CI"] == "done" and nodes["DIFFICULTY_SWEEP"] == "pending"


def test_completed_draft_http_detail_uses_draft_waiting_panel(tmp_path, monkeypatch):
    import json

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    runs = tmp_path / "runs"
    _make_run(runs, "draft", ["pass", "selected", "pass", "pass", "pass", "pass"])
    (runs / "draft" / "drive.json").write_text(json.dumps({
        "halted": "draft",
        "final_stage": "DIFFICULTY_SWEEP",
        "halt_reason": "draft complete — passed Static CI",
    }))
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    from programsmith.config import LhConfig
    cfg = LhConfig(runs_dir=str(runs))
    cfg.save()
    from programsmith.ui.app import app

    detail = TestClient(app).get("/api/runs/draft").json()
    assert detail["summary"]["status"] == "draft"
    assert detail["summary"]["progress"] == 1.0
    assert detail["waiting"]["kind"] == "draft"
    assert detail["node_statuses"]["STATIC_CI"] == "done"
    assert detail["node_statuses"]["DIFFICULTY_SWEEP"] == "pending"


def test_exported_task_has_download_and_oddish_actions(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    runs = tmp_path / "runs"
    _make_run(runs, "draft", ["pass", "selected", "pass", "pass", "pass", "pass"])
    task = tmp_path / "out" / "drafts" / "draft"
    task.mkdir(parents=True)
    (task / "task.toml").write_text("version = 1\n")
    manifest = Manifest.load(runs / "draft")
    manifest.pipeline_mode = "draft"
    manifest.snapshot = {"outbox_path": str(task)}
    manifest.save(runs / "draft")
    (runs / "draft" / "drive.json").write_text(json.dumps({
        "halted": "draft", "final_stage": "DIFFICULTY_SWEEP", "halt_reason": "done",
    }))

    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.delenv("ODDISH_API_KEY", raising=False)
    from programsmith.config import LhConfig
    LhConfig(runs_dir=str(runs)).save()
    from programsmith.ui.app import app

    client = TestClient(app)
    detail = client.get("/api/runs/draft").json()
    assert detail["artifact"] == {
        "available": True,
        "download_url": "/api/runs/draft/download",
        "calibrated": False,
    }
    downloaded = client.get("/api/runs/draft/download")
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "application/zip"
    missing_key = client.post("/api/runs/draft/oddish", json={})
    assert missing_key.status_code == 422 and "Connect Oddish" in missing_key.json()["detail"]


def test_difficulty_pass_at_1_reads_real_value(tmp_path):
    """The fleet card reads the real pass@1 the orchestrator records under `pass_at_1` (not the legacy
    `claude_code_pass_at_1` key, which left the card showing '—')."""
    runs = tmp_path / "runs"
    _make_run(runs, "r", ["pass", "selected", "pass", "pass", "pass", "pass", "done"],
              sweeps={"difficulty": {"pass_at_1": 1.0, "status": "done"},
                      "full": {"claude_code": 1.0, "codex": 0.2}})
    s = RunStore(runs).summary("r")
    assert s.difficulty_pass_at_1 == "1.0"
    assert s.full_sweep_band == "cc=100% cx=20%"


_TO_FULL_SWEEP =["pass", "selected", "pass", "pass", "pass", "pass", "done", "proceed", "clean"]


def test_polling_sweep_reads_as_waiting_not_blocked(tmp_path):
    """A run only POLLING a launched sweep is WAITING, not blocked — the fleet card shows a calm
    'waiting' pill, not the red 'blocked' badge (it advances itself when the sweep lands)."""
    import json
    runs = tmp_path / "runs"
    _make_run(runs, "r", _TO_FULL_SWEEP,
              sweeps={"full": {"status": "running", "experiment": "f1ccb313", "attempts": 2}})
    (runs / "r" / "drive.json").write_text(json.dumps({"halted": "blocked",
        "halt_reason": "full running on local (f1ccb313): 1 task(s) running, 5/25 trials"}))
    s = RunStore(runs).summary("r")
    assert s.stage == "FULL_SWEEP" and s.waiting is True and s.blocked is False
    assert "running on local" in (s.halt_reason or "")


def test_genuine_block_still_reads_as_blocked(tmp_path):
    """A real operational block (no sweep launched — needs spend/bundle) stays `blocked`, not waiting."""
    import json
    runs = tmp_path / "runs"
    _make_run(runs, "r", _TO_FULL_SWEEP)  # at FULL_SWEEP with NO 'full' sweep entry
    (runs / "r" / "drive.json").write_text(json.dumps({"halted": "blocked",
        "halt_reason": "FULL SWEEP needs a live dual-family sweep (billable)"}))
    s = RunStore(runs).summary("r")
    assert s.blocked is True and s.waiting is False


def test_credential_queue_reads_as_waiting_not_blocked(tmp_path):
    """UX regression: an agentic cell QUEUED on the credential ladder (all sources at capacity/cooling)
    is a THROUGHPUT wait, not a failure — it launches when a slot frees. Under a big farm (20-50 tasks)
    this is common, so it must show the calm 'waiting' pill, never the red 'blocked' badge."""
    import json
    runs = tmp_path / "runs"
    _make_run(runs, "openjpeg", ["pass", "selected", "pass"])  # at CREATE
    (runs / "openjpeg" / "drive.json").write_text(json.dumps({"halted": "blocked",
        "halt_reason": "create-fill: queued — all 3 credential source(s) at capacity/cooling"}))
    s = RunStore(runs).summary("openjpeg")
    assert s.stage == "CREATE" and s.waiting is True and s.blocked is False


def test_node_statuses(tmp_path):
    runs = tmp_path / "runs"
    _make_run(runs, "r", ["pass", "selected", "pass"])  # current = CREATE
    store = RunStore(runs)
    ns = store.node_statuses(store.get_state("r"))
    assert ns["INGEST_LOCK"] == "done" and ns["TASK_MATRIX"] == "done"
    assert ns["CREATE"] == "current"
    assert ns["STATIC_CI"] == "pending" and ns["QA_GATE"] == "pending"
    assert "PR" not in ns and "QA_ON_GPT" not in ns   # legacy drains never render as forward nodes


def test_forward_list_and_easy_shelf_bucket(tmp_path):
    """FORWARD carries no legacy stages (ADR-0039) and an EASY_SHELF run lands in its OWN fleet
    bucket ('easy' — a product output, not an accept/drop) with status 'easy' (ADR-0040)."""
    from programsmith.fsm import Stage
    from programsmith.ui.store import FORWARD
    assert Stage.QA_ON_GPT not in FORWARD and Stage.PR not in FORWARD
    assert FORWARD[-1] is Stage.QA_GATE
    runs = tmp_path / "runs"
    _make_run(runs, "shelved", _TO_FULL_SWEEP + ["shelve"])   # frontier says too easy to harden
    _make_run(runs, "done", _TO_FULL_SWEEP + ["done", "accept"])
    store = RunStore(runs)
    s = store.summary("shelved")
    assert s.status == "easy" and s.stage == "EASY_SHELF"
    assert s.exported is False and not s.blocked              # shelved ≠ exported ≠ stuck
    c = store.fleet_counters()
    assert c["easy"] == 1 and c["accepted"] == 1 and c["in_progress"] == 0


def test_node_statuses_resets_downstream_after_harden_loopback(tmp_path):
    """A run that reached Full Sweep in an EARLIER generation then hardened BACK to Difficulty Sweep
    must show Full Sweep (+ other downstream nodes) as `pending` again — not a stale `done` (the
    reported confusion: DAG showed Full Sweep complete with no full-sweep run on record)."""
    from programsmith.fsm import Stage
    from programsmith.state import StageEvent
    runs = tmp_path / "runs"
    st = RunState.start("r", "task:r", "r")
    st.current_stage = Stage.DIFFICULTY_SWEEP        # looped back here after a harden
    st.harden = 2
    st.history = [
        StageEvent(stage=Stage.FULL_SWEEP, verdict="harden", next=Stage.SYNTHESIZE, reason="saturated"),
        StageEvent(stage=Stage.STATIC_CI, verdict="pass", next=Stage.DIFFICULTY_SWEEP, reason="green"),
    ]
    st.save(runs / "r")
    Manifest(run_id="r", task_identity="task:r", slug="r").save(runs / "r")
    ns = RunStore(runs).node_statuses(RunStore(runs).get_state("r"))
    assert ns["DIFFICULTY_SWEEP"] == "current"
    assert ns["FULL_SWEEP"] == "pending"             # downstream of current → pending, not stale done
    assert ns["CALIBRATE"] == "pending" and ns["QA_PROBE"] == "pending"
    assert ns["STATIC_CI"] == "done" and ns["CREATE"] == "done"
    assert ns["SYNTHESIZE"] == "done"                # off-path loop node, used (harden>0)


def test_node_statuses_synthesize_shows_reached_stage_not_rejoin(tmp_path):
    """A run patching at SYNTHESIZE after hardening from CALIBRATE shows done-through-CALIBRATE +
    SYNTHESIZE current — NOT regressed to SANITY (the rejoin point). The reported cjson confusion:
    'how is it on Sanity but also synthesizing?'."""
    from programsmith.fsm import Stage
    from programsmith.state import StageEvent
    runs = tmp_path / "runs"
    st = RunState.start("r", "task:r", "r")
    st.current_stage = Stage.SYNTHESIZE
    st.harden = 1
    st.synthesize_rejoin = Stage.STATIC_CI       # will re-enter here, but it REACHED Calibrate
    st.history = [
        StageEvent(stage=Stage.DIFFICULTY_SWEEP, verdict="done", next=Stage.CALIBRATE, reason="x"),
        StageEvent(stage=Stage.CALIBRATE, verdict="harden", next=Stage.SYNTHESIZE, reason="saturated"),
    ]
    st.save(runs / "r")
    Manifest(run_id="r", task_identity="task:r", slug="r").save(runs / "r")
    ns = RunStore(runs).node_statuses(RunStore(runs).get_state("r"))
    assert ns["SYNTHESIZE"] == "current"
    assert ns["CALIBRATE"] == "done"             # reached it (triggered the harden) — not pending/Sanity
    assert ns["DIFFICULTY_SWEEP"] == "done" and ns["SANITY"] == "done"
    assert ns["QA_PROBE"] == "pending"           # downstream of where it reached


def test_pause_resume(tmp_path):
    runs = tmp_path / "runs"
    _make_run(runs, "r", ["pass"])
    store = RunStore(runs)
    st = store.set_paused("r", True)
    assert st.paused and st.halted
    assert RunStore(runs).get_state("r").paused  # persisted
    store.set_paused("r", False)
    assert not RunStore(runs).get_state("r").paused


def test_http_api(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    runs = tmp_path / "runs"
    _make_run(runs, "minpack", ["pass", "selected", "pass"])
    # config drives the runs dir
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    from programsmith.config import LhConfig
    cfg = LhConfig(); cfg.runs_dir = str(runs); cfg.save()
    from programsmith.ui.app import app

    c = TestClient(app)
    # preflight + settings
    assert "checks" in c.get("/api/preflight").json()
    assert c.get("/api/settings").status_code == 200
    # fleet
    fleet = c.get("/api/runs").json()
    assert any(r["key"] == "minpack" for r in fleet["runs"])
    assert fleet["counters"]["total"] == 1
    # run detail
    detail = c.get("/api/runs/minpack").json()
    assert detail["node_statuses"]["CREATE"] == "current"
    assert detail["summary"]["stage"] == "CREATE"
    # the read-only "waiting on" preview is surfaced proactively (no Advance click needed)
    assert detail["waiting"]["stage"] == "CREATE" and detail["waiting"]["kind"] == "runnable"
    # pause via API persists
    assert c.post("/api/runs/minpack/pause").json()["paused"] is True
    assert RunStore(runs).get_state("minpack").paused
    # agent-output: live tail of the cell-agent log + running flag (404 for an unknown run)
    (runs / "minpack" / "agent-logs").mkdir(parents=True, exist_ok=True)
    (runs / "minpack" / "agent-logs" / "agent.log").write_text("hello from the synthesize agent\n")
    ao = c.get("/api/runs/minpack/agent-output").json()
    assert ao["exists"] and "synthesize agent" in ao["tail"] and ao["running"] is False
    assert ao["slow"] is False and ao["elapsed_sec"] is None     # no live agent → no throttle hint
    # A bounded byte tail starts mid-event for large JSONL logs. The API drops that partial line so
    # the UI never renders a wall of encoded signature/base64 as a raw transcript entry.
    (runs / "minpack" / "agent-logs" / "agent.log").write_text(
        "x" * 2048 + "\n" + '{"type":"result","result":"readable"}\n')
    aligned = c.get("/api/runs/minpack/agent-output", params={"tail_kb": 1}).json()["tail"]
    assert aligned == '{"type":"result","result":"readable"}\n'
    # a long-running agent surfaces the throttle hint (slow=True). Ownership keys on boot_id (a live
    # agent belongs to THIS server boot); a synthetic job must carry the current boot id or get_jobs
    # correctly orphans it (see jobs._BOOT_ID / the container-pid-collision fix).
    import json as _json
    import time as _time

    from programsmith import jobs as _jobs
    jp = runs / "minpack" / "jobs.json"
    jp.write_text(_json.dumps({"synthesize-h1-r0": {"status": "running", "boot_id": _jobs._BOOT_ID,
                                                    "started_at": _time.time() - 300}}))  # >150 slow, <900 stale
    ao2 = c.get("/api/runs/minpack/agent-output").json()
    assert ao2["running"] and ao2["slow"] is True and ao2["elapsed_sec"] > 150
    jp.unlink()
    assert c.get("/api/runs/nope/agent-output").status_code == 404
    # SPA fallback serves something (placeholder JSON when no build)
    assert c.get("/").status_code == 200
    # qa-gate decide rejects when the run isn't at QA_GATE
    assert c.post("/api/runs/minpack/qa-gate", json={"decision": "accept"}).status_code == 409


def test_runtime_surfaces_execution_knobs(tmp_path, monkeypatch):
    """Settings/UI contract: the agentic_concurrency knob round-trips and /runtime surfaces it.
    No credential endpoints exist — auth is a plain env passthrough to the `claude` CLI."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    from programsmith.config import LhConfig
    LhConfig().save()
    from programsmith.ui.app import app

    c = TestClient(app)
    # default: knob = 2 (ADR-0038 scale posture)
    rt = c.get("/api/runtime").json()
    assert rt["agentic_concurrency"] == 2
    assert "cell_auth" not in rt                       # the credential-ladder UI is gone
    # the knob round-trips through POST /settings and shows in /runtime (orchestrator re-reads it)
    assert c.post("/api/settings", json={"agentic_concurrency": 5}).json()["agentic_concurrency"] == 5
    assert c.get("/api/runtime").json()["agentic_concurrency"] == 5
    # the credential endpoints are gone entirely
    assert c.get("/api/cell-auth").status_code in (404, 405)


def test_qa_gate_endpoint_auto_409_then_human_accepts(tmp_path, monkeypatch):
    """In auto mode (the ADR-0039 default) the endpoint refuses — the driver's gate decides itself.
    Flipping qa_gate_mode=human (via settings, read per-request) re-enables the manual verdict;
    accept lands on DONE (export), never PR."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    runs = tmp_path / "runs"
    outbox = tmp_path / "outbox"
    # drive a run all the way to QA_GATE (FULL_SWEEP done → QA_GATE directly)
    _make_run(runs, "r", _TO_FULL_SWEEP + ["done"])
    task = runs / "r" / "task" / "r"; task.mkdir(parents=True)
    (task / "task.toml").write_text("[task]\n")   # a real task dir so accept EXPORTS it
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    from programsmith.config import LhConfig
    cfg = LhConfig(); cfg.runs_dir = str(runs); cfg.outbox_dir = str(outbox); cfg.save()
    from programsmith.ui.app import app

    c = TestClient(app)
    assert c.get("/api/runs/r").json()["summary"]["stage"] == "QA_GATE"
    r = c.post("/api/runs/r/qa-gate", json={"decision": "accept"})
    assert r.status_code == 409 and r.json()["detail"] == "qa_gate_mode is auto"
    assert c.post("/api/settings", json={"qa_gate_mode": "human"}).status_code == 200
    out = c.post("/api/runs/r/qa-gate", json={"decision": "accept"}).json()
    assert out["stage"] == "DONE"
    # the human accept EXPORTED to the outbox (the review-fixed gap — was silently dropped before)
    assert (outbox / "tasks" / "r" / "task.toml").exists()
    assert (outbox / "tasks" / "r" / ".provenance.json").exists()
    assert "exported" in out.get("export", "")


def test_outbox_endpoint_lists_exported_and_easy(tmp_path, monkeypatch):
    """GET /api/outbox surfaces the pipeline's OUTPUT: <outbox>/tasks/<slug> (QA_GATE accepts) and
    <outbox>/easy/<slug> (EASY_SHELF), each with its .provenance.json stamp when present."""
    pytest.importorskip("fastapi")
    import json
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    outbox = tmp_path / "outbox"
    (outbox / "tasks" / "ripgrep").mkdir(parents=True)
    (outbox / "tasks" / "ripgrep" / ".provenance.json").write_text(
        json.dumps({"run_id": "run-x", "task_identity": "task:abc"}))
    (outbox / "tasks" / "ripgrep" / "task.toml").write_text("[task]\n")
    (outbox / "easy" / "jq-lite").mkdir(parents=True)          # shelved, no provenance stamp
    (outbox / "tasks" / "stray.txt").write_text("not a bundle")  # files are ignored, dirs only
    from programsmith.config import LhConfig
    cfg = LhConfig(); cfg.outbox_dir = str(outbox); cfg.save()
    from programsmith.ui.app import app

    c = TestClient(app)
    out = c.get("/api/outbox").json()
    assert [t["slug"] for t in out["tasks"]] == ["ripgrep"]
    assert out["tasks"][0]["provenance"]["task_identity"] == "task:abc"
    assert [t["slug"] for t in out["easy"]] == ["jq-lite"]
    assert out["easy"][0]["provenance"] is None
    # a missing outbox dir is an EMPTY listing, not an error (nothing exported yet)
    cfg.outbox_dir = str(tmp_path / "nowhere"); cfg.save()
    assert c.get("/api/outbox").json() == {"tasks": [], "easy": [], "drafts": []}


def test_settings_new_config_keys_roundtrip(tmp_path, monkeypatch):
    """The ADR-0039/0042 config keys (gate modes, outbox, cell-model routing, sweep models/bands,
    authorship, overlap guard) persist via POST /settings, read back on GET, and the gate modes
    surface on /runtime (the SPA keys its review panels off them)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    from programsmith.config import LhConfig
    LhConfig().save()
    from programsmith.ui.app import app

    c = TestClient(app)
    body = {"task_matrix_mode": "human", "qa_gate_mode": "human", "outbox_dir": "/tmp/box",
            "cell_model_light": "claude-haiku-4", "cell_model_analysis": "claude-opus-4-8",
            "default_cell_model": "claude-opus-4-8",
            "smoke_model": "anthropic/claude-sonnet-5", "frontier_band_max": 0.6,
            "author_name": "Ops", "author_email": "ops@example.org",
            "author_organization": "Ops Org", "allow_programbench_overlap": True}
    assert c.post("/api/settings", json=body).status_code == 200
    got = c.get("/api/settings").json()
    for k, v in body.items():
        assert got[k] == v, k
    rt = c.get("/api/runtime").json()
    assert rt["task_matrix_mode"] == "human" and rt["qa_gate_mode"] == "human"
    assert rt["outbox_dir"] == "/tmp/box"


def test_file_browser_endpoints(tmp_path, monkeypatch):
    """The run file browser lists the working-dir tree and previews a file, confined to the run dir."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    runs = tmp_path / "runs"
    _make_run(runs, "r", ["pass", "selected"])  # at ORACLE_GOLDEN
    (runs / "r" / "task" / "r").mkdir(parents=True)
    (runs / "r" / "task" / "r" / "instruction.md").write_text("# rewrite\nhello")
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    from programsmith.config import LhConfig
    cfg = LhConfig(); cfg.runs_dir = str(runs); cfg.save()
    from programsmith.ui.app import app

    c = TestClient(app)
    tree = c.get("/api/runs/r/files").json()
    assert tree["root"] == "r" and tree["tree"]["type"] == "dir"
    names = {n["name"] for n in tree["tree"]["children"]}
    assert "task" in names
    f = c.get("/api/runs/r/file", params={"path": "task/r/instruction.md"}).json()
    assert f["kind"] == "text" and "hello" in f["content"] and f["lang"] == "markdown"
    # traversal rejected, missing file 404s
    assert c.get("/api/runs/r/file", params={"path": "../../../etc/hosts"}).status_code == 400
    assert c.get("/api/runs/r/file", params={"path": "task/r/nope"}).status_code == 404


def test_run_detail_surfaces_recorded_sweeps(tmp_path, monkeypatch):
    """The run detail surfaces every recorded sweep (experiment handle + band) via the manifest
    context (no manual Advance endpoint anymore — the background auto-driver flows runs)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    runs = tmp_path / "runs"
    _make_run(runs, "r", ["pass", "selected"],  # at ORACLE_GOLDEN, with a real-shaped sweep recorded
              sweeps={"difficulty": {"experiment": "0b65b9a7", "pass_at_1": 0.2}})
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    from programsmith.config import LhConfig
    cfg = LhConfig(); cfg.runs_dir = str(runs); cfg.save()
    from programsmith.ui.app import app

    c = TestClient(app)
    ctx = c.get("/api/runs/r").json()["context"]
    assert ctx["sweeps"]["difficulty"]["experiment"] == "0b65b9a7"
    # the manual advance endpoint is gone (no POST handler anymore)
    assert c.post("/api/runs/r/advance", json={}).status_code in (404, 405)


def test_runtime_and_execution_settings_roundtrip(tmp_path, monkeypatch):
    """The new execution knobs persist and the /runtime endpoint reflects them (so Settings can show
    the effective config)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.delenv("PROGRAMSMITH_CI_REPO_ROOT", raising=False)
    monkeypatch.delenv("PROGRAMSMITH_RUNS_DIR", raising=False)
    from programsmith.config import LhConfig
    LhConfig().save()
    from programsmith.ui.app import app

    c = TestClient(app)
    out = c.post("/api/settings", json={"difficulty_trials": 4, "harden_drop_after": 1,
                                        "ci_repo_root": str(tmp_path / "nope")}).json()
    assert out["difficulty_trials"] == 4 and out["harden_drop_after"] == 1
    rt = c.get("/api/runtime").json()
    assert rt["difficulty_trials"] == 4 and rt["harden_drop_after"] == 1
    assert rt["ci_repo_ok"] is False  # the path has no ci_checks/


def test_catalog_and_presets_and_run_config(tmp_path, monkeypatch):
    """New-Run options backend: catalog of harnesses/models, preset save/list/delete, and a per-run
    config persisted onto the manifest at creation."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.delenv("PROGRAMSMITH_RUNS_DIR", raising=False)
    # the run below bills anthropic + openai — pin both keys so the creation-time credential
    # gate (tested separately at the end) lets it through
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    from programsmith.config import LhConfig
    cfg = LhConfig(); cfg.runs_dir = str(runs); cfg.save()
    from programsmith.ui.app import app

    c = TestClient(app)
    cat = c.get("/api/catalog").json()
    assert "claude-code" in cat["harnesses"] and "codex" in cat["harnesses"]
    # mini-swe is the universal local harness (pre-installed in every task image, any litellm
    # model on the provider's key); gemini-cli rides the solver overlay like claude-code/codex.
    assert cat["harnesses"]["mini-swe"]["recommended"] is True
    assert "gemini-cli" in cat["harnesses"]
    assert "anthropic/claude-opus-4-8" in cat["models"]
    assert "anthropic/claude-haiku-4-5" in cat["models"]
    assert "gemini/gemini-3-flash" in cat["models"]     # litellm id — gemini/ prefix, not google/

    # preset save / list / delete
    rc = {"difficulty": {"agents": [{"harness": "claude-code", "model": "anthropic/claude-opus-4-8",
                                      "n_trials": 5}], "band": {"basis": "aggregate", "max_pass": 0.4}},
          "full": {"agents": [{"harness": "codex", "model": "openai/gpt-5.5", "n_trials": 3}]}}
    assert c.post("/api/presets", json={"name": "aggressive", "config": rc}).status_code == 200
    assert "aggressive" in c.get("/api/presets").json()["presets"]
    assert "aggressive" not in c.delete("/api/presets/aggressive").json()["presets"]

    # create a run WITH a config → persisted on the manifest
    from programsmith.manifest import Manifest
    r = c.post("/api/runs", json={"repo": "o/n", "sha": "abc123", "slug": "cfgrun", "config": rc,
                                  "brief": "Port the FFT subsystem; differential oracle."})
    assert r.status_code == 200
    man = Manifest.load(runs / "cfgrun")
    assert man.run_config["difficulty"]["agents"][0]["n_trials"] == 5
    assert man.run_config["difficulty"]["band"]["max_pass"] == 0.4
    assert man.task_brief == "Port the FFT subsystem; differential oracle."
    # invalid config is rejected
    bad = c.post("/api/runs", json={"repo": "o/n", "sha": "x", "slug": "bad", "config": {"nope": 1}})
    assert bad.status_code == 422
    # a config billing a keyless provider is rejected AT CREATION with the remediation named
    # (never a mid-sweep error) — no GEMINI_API_KEY is set above
    gm = {"difficulty": {"agents": [{"harness": "gemini-cli",
                                     "model": "gemini/gemini-3-flash", "n_trials": 3}],
                         "band": {}},
          "full": {"agents": [{"harness": "codex", "model": "openai/gpt-5.5", "n_trials": 3}],
                   "band": {}}}
    nocred = c.post("/api/runs", json={"repo": "o/n", "sha": "y", "slug": "nocred", "config": gm})
    assert nocred.status_code == 422 and "GEMINI_API_KEY" in nocred.json()["detail"]


def test_exported_and_polling_not_shown_as_blocked(tmp_path):
    """UX regression: a DONE run (accepted + exported to the outbox, ADR-0039) is a SUCCESS — never a
    red 'blocked' badge even under a stale drive.json. A FULL_SWEEP run polling --run-analysis is
    WAITING, not blocked. A genuine agentic park (create-fill can't reach oracle) IS blocked."""
    import json
    runs = tmp_path / "runs"

    def _drive(key, reason):
        (runs / key).mkdir(parents=True, exist_ok=True)
        (runs / key / "drive.json").write_text(json.dumps({"halted": "blocked", "halt_reason": reason}))

    # exported (DONE) → exported flag + deprecated ready_for_pr alias, not blocked
    _make_run(runs, "shipped", _TO_FULL_SWEEP + ["done", "accept"])
    _drive("shipped", "stale trace from an earlier blocked pass")
    s = RunStore(runs).summary("shipped")
    assert s.stage == "DONE" and s.exported and s.ready_for_pr and not s.blocked and not s.waiting

    # FULL_SWEEP polling the --run-analysis phase → waiting, not blocked
    _make_run(runs, "an-poll", _TO_FULL_SWEEP)
    _drive("an-poll", "full: --run-analysis still running (3/3 trials pending) — polling")
    s2 = RunStore(runs).summary("an-poll")
    assert s2.stage == "FULL_SWEEP" and s2.waiting and not s2.blocked

    # a genuine agentic park (create-fill can't reach oracle) → really blocked
    _make_run(runs, "stuck", ["pass", "selected", "pass"])  # at CREATE
    _drive("stuck", "create-fill: agent errored after 3 attempt(s) — did not reach oracle=1/nop=0")
    s3 = RunStore(runs).summary("stuck")
    assert s3.blocked and not s3.waiting and not s3.exported


def test_frontend_dist_honors_env_override():
    """Regression: the served UI must serve the SPA from PROGRAMSMITH_FRONTEND_DIST (a path outside the
    source mount) — otherwise the gitignored source-tree dist gets shadowed and the UI 404s to the JSON
    fallback. Checked in a fresh interpreter so module-level FRONTEND_DIST is computed with the env set."""
    import subprocess, sys
    code = ("import os; os.environ['PROGRAMSMITH_FRONTEND_DIST']='/tmp/custom-ui-dist';"
            "import programsmith.ui.app as a; print(str(a.FRONTEND_DIST))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "/tmp/custom-ui-dist" in out.stdout
