"""JSON API for the UI — makes the pipeline fully operable from the browser.

Endpoints (all under /api):
  GET  /preflight                 preflight status (onboarding)
  GET  /settings                  config (secrets redacted)
  POST /settings                  update config (keys, models, defaults)
  GET  /runs                      fleet: summaries + counters
  POST /runs                      create a run: resolve SHA → INGEST → state+manifest
  GET  /runs/{key}                run detail: summary + node statuses + history + context
  POST /runs/{key}/task-matrix    run the TASK MATRIX cell (live) → candidates  (review #1, human mode)
  GET  /runs/{key}/task-matrix    stored candidates
  POST /runs/{key}/select         record the human's pick (or none)
  POST /runs/{key}/qa-gate        final-gate verdict (human mode only; 409 in auto mode)
  GET  /outbox                    exported tasks + easy-shelf tasks (the pipeline's OUTPUT)
  POST /runs/{key}/pause | resume operational halt / continue
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from ..cells.task_matrix import TaskMatrixOutput, apply_selection, propose
from ..config import LhConfig
from ..gates.ingest import ingest
from ..jobs import get_jobs, run_in_background
from ..manifest import Manifest, source_identity
from ..orchestrator import peek
from ..preflight import check_preflight
from ..state import RunState
from ..statestore import run_state_exists
from .store import RunStore


router = APIRouter(prefix="/api")
TASK_MATRIX_FILE = "task_matrix.json"

# Fleet summaries touch several small control-plane files per run. That is inexpensive for one
# caller, but a handful of dashboard tabs polling together used to launch duplicate 421-run scans
# every four seconds. Once a scan took longer than the poll interval, setInterval piled up more
# requests and the dashboard entered a self-sustaining 20s+ backlog. Keep one short-lived,
# single-flight snapshot per runs directory so concurrent tabs share the same filesystem read.
_FLEET_CACHE_TTL_SEC = 1.5
_FLEET_CACHE_LOCK = threading.Lock()
_FLEET_CACHE: tuple[str, float, dict] | None = None
_FLEET_CACHE_REFRESHING = False


def _store() -> RunStore:
    return RunStore(LhConfig.load().runs_dir)


def _invalidate_fleet_cache() -> None:
    global _FLEET_CACHE
    with _FLEET_CACHE_LOCK:
        if _FLEET_CACHE is not None:
            root, _cached_at, payload = _FLEET_CACHE
            # Preserve the last good response so readers stay instant while a fresh snapshot is
            # rebuilt. A changed runs directory still hard-misses by cache key.
            _FLEET_CACHE = (root, 0.0, payload)


def _build_fleet_payload(root: str) -> dict:
    s = RunStore(root)
    summaries = s.list_summaries()
    return {
        "runs": [r.model_dump() for r in summaries],
        "counters": s.fleet_counters(summaries),
    }


def _refresh_fleet_cache(root: str) -> None:
    global _FLEET_CACHE, _FLEET_CACHE_REFRESHING
    try:
        payload = _build_fleet_payload(root)
        with _FLEET_CACHE_LOCK:
            _FLEET_CACHE = (root, time.monotonic(), payload)
    finally:
        with _FLEET_CACHE_LOCK:
            _FLEET_CACHE_REFRESHING = False


def prime_fleet_cache() -> None:
    """Build the first fleet snapshot before the HTTP server begins accepting traffic."""
    global _FLEET_CACHE, _FLEET_CACHE_REFRESHING
    root = str(Path(_store().runs_dir).expanduser().resolve())
    payload = _build_fleet_payload(root)
    with _FLEET_CACHE_LOCK:
        _FLEET_CACHE = (root, time.monotonic(), payload)
        _FLEET_CACHE_REFRESHING = False


# ---- settings / preflight ------------------------------------------------------------

@router.get("/preflight")
def preflight() -> dict:
    return check_preflight()


@router.get("/settings")
def get_settings() -> dict:
    return LhConfig.load().redacted()


@router.get("/costs")
def get_costs() -> dict:
    """Provider-reported model usage for local ProgramSmith runs; no infrastructure estimates."""
    from ..costlog import dashboard
    return dashboard(LhConfig.load().runs_dir)


class SettingsBody(BaseModel):
    # cell model routing (ADR-0042): heavy default + light one-shots + trajectory-audit model
    default_cell_model: str | None = None
    cell_model_light: str | None = None
    cell_model_analysis: str | None = None
    # sweep models + band overrides (bands are model-relative)
    smoke_model: str | None = None
    frontier_model: str | None = None
    smoke_band_max: float | None = None
    frontier_band_min: float | None = None
    frontier_band_max: float | None = None
    runs_dir: str | None = None
    ci_repo_root: str | None = None
    difficulty_trials: int | None = None
    full_trials: int | None = None
    harden_drop_after: int | None = None
    harden_min_improvement: float | None = None
    agentic_concurrency: int | None = None
    local_trial_concurrency: int | None = None
    # human gates (ADR-0039: both default auto) + the outbox the accepted/easy tasks export to
    task_matrix_mode: str | None = None
    qa_gate_mode: str | None = None
    outbox_dir: str | None = None
    # ProgramBench task authorship (stamped into generated task.toml) + the overlap hard-guard
    author_name: str | None = None
    author_email: str | None = None
    author_organization: str | None = None
    allow_programbench_overlap: bool | None = None
    # Credentials saved locally in the owner-only config file. Empty string clears a saved secret.
    claude_code_oauth_token: str | None = None
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    gemini_api_key: str | None = None
    zai_api_key: str | None = None
    # Hosted Oddish handoff. The key is treated exactly like the provider secrets above.
    oddish_api_key: str | None = None
    oddish_api_url: str | None = None
    oddish_dashboard_url: str | None = None
    oddish_agent: str | None = None
    oddish_model: str | None = None


@router.post("/settings")
def set_settings(body: SettingsBody) -> dict:
    # Never copy environment-provided secrets into the local config as a side effect of saving an
    # unrelated field. Environment values remain effective, but only explicitly submitted secrets
    # are persisted.
    cfg = LhConfig.load_persisted()
    for field, val in body.model_dump(exclude_none=True).items():
        if field in {
            "claude_code_oauth_token", "anthropic_api_key", "openai_api_key",
            "gemini_api_key", "zai_api_key", "oddish_api_key",
        }:
            val = val.strip() or None
        setattr(cfg, field, val)
        # Keep the persisted Anthropic choice unambiguous. Explicit environment variables remain
        # authoritative at process start, as documented in config.LhConfig.load.
        if val and field == "claude_code_oauth_token":
            cfg.anthropic_api_key = None
        elif val and field == "anthropic_api_key":
            cfg.claude_code_oauth_token = None
    cfg.save()
    return LhConfig.load().redacted()


@router.get("/runtime")
def runtime() -> dict:
    """Effective runtime the served auto-driver is using — so Settings can SHOW what's actually on
    (autodrive, spend, agentic, the resolved STATIC-CI checkout) instead of the operator guessing."""
    import os

    from ..orchestrator import _agentic_concurrency

    cfg = LhConfig.load()
    # ci_repo_root is an OVERRIDE of the vendored in-tree check suite; ci_repo_ok reports whether
    # the override (when set) actually carries a ci_checks/ dir.
    ci = cfg.ci_repo_root
    ci_ok = bool(ci and (Path(ci) / "ci_checks").exists()) if ci else True
    return {
        "autodrive": os.getenv("PROGRAMSMITH_AUTODRIVE") == "1",
        "spend": os.getenv("PROGRAMSMITH_AUTODRIVE_SPEND", "1") != "0",
        "agentic": os.getenv("PROGRAMSMITH_AGENTIC", "1") != "0",
        "interval_sec": float(os.getenv("PROGRAMSMITH_AUTODRIVE_INTERVAL", "8")),
        "runs_dir": cfg.runs_dir,
        "ci_repo_root": ci,
        "ci_repo_ok": ci_ok,
        "difficulty_trials": cfg.difficulty_trials,
        "full_trials": cfg.full_trials,
        "harden_drop_after": cfg.harden_drop_after,
        "harden_min_improvement": cfg.harden_min_improvement,
        "agentic_concurrency": _agentic_concurrency(),
        # Gate modes (ADR-0039) — the SPA renders the TASK MATRIX / QA GATE review panels ONLY when
        # the respective mode is "human"; in auto mode the driver decides and the panel would race it.
        "task_matrix_mode": cfg.task_matrix_mode,
        "qa_gate_mode": cfg.qa_gate_mode,
        "outbox_dir": cfg.outbox_dir,
    }


@router.get("/catalog")
def catalog() -> dict:
    """Harnesses + models the New Run dialog offers (shared source of truth with the backend)."""
    from ..runconfig import HARNESSES, MODELS, default_run_config
    return {"harnesses": HARNESSES, "models": MODELS,
            "default_config": default_run_config().model_dump()}


@router.get("/presets")
def list_presets() -> dict:
    return {"presets": LhConfig.load().presets or {}}


class PresetBody(BaseModel):
    name: str
    config: dict


@router.post("/presets")
def save_preset(body: PresetBody) -> dict:
    """Save (or overwrite) a named New-Run config preset, after validating it against the schema."""
    from ..runconfig import RunConfig
    try:
        norm = RunConfig.model_validate(body.config).model_dump()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"invalid preset config: {str(e)[:200]}")
    cfg = LhConfig.load()
    cfg.presets = {**(cfg.presets or {}), body.name: norm}
    cfg.save()
    return {"presets": cfg.presets}


@router.delete("/presets/{name}")
def delete_preset(name: str) -> dict:
    cfg = LhConfig.load()
    presets = {k: v for k, v in (cfg.presets or {}).items() if k != name}
    cfg.presets = presets
    cfg.save()
    return {"presets": presets}


# ---- fleet / runs --------------------------------------------------------------------

@router.get("/runs")
def list_runs() -> dict:
    global _FLEET_CACHE, _FLEET_CACHE_REFRESHING
    s = _store()
    root = str(Path(s.runs_dir).expanduser().resolve())
    now = time.monotonic()
    with _FLEET_CACHE_LOCK:
        if _FLEET_CACHE is not None:
            cached_root, cached_at, payload = _FLEET_CACHE
            if cached_root == root:
                if now - cached_at >= _FLEET_CACHE_TTL_SEC and not _FLEET_CACHE_REFRESHING:
                    _FLEET_CACHE_REFRESHING = True
                    threading.Thread(
                        target=_refresh_fleet_cache, args=(root,), daemon=True,
                        name="programsmith-fleet-cache",
                    ).start()
                # Stale-while-revalidate: UI reads never wait behind a 421-run filesystem scan.
                return payload

        # First read for this runs directory is single-flight; subsequent readers share it.
        payload = _build_fleet_payload(root)
        _FLEET_CACHE = (root, time.monotonic(), payload)
        return payload


def _manifest_context(man: Manifest | None) -> dict:
    if not man:
        return {}
    return {
        "source": man.source.model_dump() if man.source else None,
        "dimensions": man.dimensions.model_dump() if man.dimensions else None,
        "oracle": man.oracle,
        "sweeps": man.sweeps,
        "harden_history": man.harden_history or [],
        "run_config": man.run_config,
        "task_brief": man.task_brief,
        "source_screen": man.source_screen,
    }


def _exported_task_dir(run_dir: Path, manifest: Manifest | None) -> Path | None:
    """Resolve only a finished/exported task artifact, never the mutable work-in-progress tree."""
    if manifest is None:
        return None
    snapshot = manifest.snapshot or {}
    raw = snapshot.get("outbox_path") if isinstance(snapshot, dict) else None
    if not raw:
        return None
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path if path.is_dir() else None


@router.get("/runs/{key}")
def get_run(key: str) -> dict:
    s = _store()
    try:
        st = s.get_state(key)
    except FileNotFoundError:
        raise HTTPException(404, f"no run {key!r}")
    run_dir = Path(s.runs_dir) / key
    from ..statestore import store_for
    _dstore, _dkey = store_for(run_dir)
    _draw = _dstore.read(f"{_dkey}/drive.json")
    drive_info = json.loads(_draw) if _draw else None
    summary = s.summary(key)
    manifest = s.get_manifest(key)
    if drive_info and drive_info.get("halted") == "draft":
        waiting = {
            "stage": drive_info.get("final_stage", summary.stage),
            "kind": "draft",
            "reason": drive_info.get("halt_reason", "draft complete after Static CI"),
            "can_reopen": False,
        }
    else:
        try:
            waiting = peek(run_dir)
        except FileNotFoundError:
            waiting = None
    if summary.screened_out and waiting:
        waiting["can_reopen"] = False
        # The FSM transition has a deliberately generic routing reason ("no viable candidate").
        # Prefer the actual deterministic screen or Task Matrix explanation so the terminal panel
        # tells the operator what was wrong with this source.
        source_screen = (manifest.source_screen or {}) if manifest else {}
        specific_reason = None
        if source_screen and source_screen.get("eligible") is False:
            specific_reason = source_screen.get("reason")
        if not specific_reason:
            matrix_raw = _dstore.read(f"{_dkey}/task_matrix.json")
            if matrix_raw:
                try:
                    specific_reason = (json.loads(matrix_raw) or {}).get("no_candidate_reason")
                except json.JSONDecodeError:
                    pass
        if specific_reason:
            waiting["reason"] = specific_reason
    return {
        "summary": summary.model_dump(),
        "node_statuses": s.node_statuses(st, draft_complete=summary.status == "draft"),
        "history": [e.model_dump() for e in st.history],
        "context": _manifest_context(manifest),
        "drive": drive_info,
        "waiting": waiting,
        "jobs": get_jobs(run_dir),
        "artifact": {
            "available": _exported_task_dir(run_dir, manifest) is not None,
            "download_url": f"/api/runs/{key}/download",
            "calibrated": summary.status in {"done", "easy"},
        },
    }


class CreateRunBody(BaseModel):
    repo: str                       # owner/name or full URL
    sha: str | None = None          # pinned SHA; resolved from HEAD if omitted
    slug: str | None = None         # run key (defaults to the repo name)
    config: dict | None = None      # optional per-run RunConfig (agents + band per stage)
    brief: str | None = None        # optional operator brief to steer the TASK MATRIX agent
    mode: str = "full"             # full | draft (draft exports after STATIC_CI)
    cell_model: str | None = None   # optional per-run override for every generation cell


def _resolve_head(repo_url: str) -> str:
    p = subprocess.run(["git", "ls-remote", repo_url, "HEAD"], capture_output=True, text=True, timeout=60)
    if p.returncode != 0 or not p.stdout.strip():
        raise HTTPException(400, f"could not resolve HEAD for {repo_url}: {p.stderr.strip()[:200]}")
    return p.stdout.split()[0]


def _validated_run_config(raw: dict | None) -> dict | None:
    """Validate an incoming RunConfig against the catalog/schema; store the normalized dict (None ⇒
    the run uses the built-in defaults)."""
    if not raw:
        return None
    from ..runconfig import RunConfig
    try:
        return RunConfig.model_validate(raw).model_dump()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"invalid run config: {str(e)[:200]}")


@router.post("/runs")
def create_run(body: CreateRunBody) -> dict:
    cfg = LhConfig.load()
    repo = body.repo.replace("https://github.com/", "").rstrip("/").removesuffix(".git")
    url = body.repo if body.repo.startswith("http") else f"https://github.com/{repo}"
    sha = body.sha or _resolve_head(url)
    from ..runkey import validate_run_key
    try:
        key = validate_run_key(body.slug or repo.split("/")[-1])
    except ValueError as e:
        raise HTTPException(422, str(e))
    if body.mode not in {"full", "draft"}:
        raise HTTPException(422, "mode must be 'full' or 'draft'")
    run_dir = Path(cfg.runs_dir) / key
    if run_state_exists(run_dir):
        raise HTTPException(409, f"run {key!r} already exists")
    run_config = _validated_run_config(body.config)
    # Creation-time credential gate: a run configured for a provider with no usable key errors NOW
    # with the remediation named — never mid-sweep, three stages later (SWE-gen fail-fast lesson).
    from ..preflight import missing_provider_creds
    # Draft mode has no solver trials; only the required Anthropic generation credential matters.
    if body.mode == "draft":
        from ..preflight import credential_for
        ok, detail = credential_for("claude-code", "anthropic")
        missing = [] if ok else [detail]
    else:
        missing = missing_provider_creds(run_config)
    if missing:
        raise HTTPException(422, "missing provider credentials: " + "; ".join(missing))

    identity = source_identity(repo, sha)
    brief = (body.brief or "").strip() or None
    # persist the run immediately (so it shows in the fleet) then ingest in the background
    Manifest(run_id=f"run-{identity}", task_identity=identity, slug=key,
             run_config=run_config, task_brief=brief, pipeline_mode=body.mode,
             cell_model=(body.cell_model or "").strip() or None).save(run_dir)
    RunState.start(f"run-{identity}", identity, slug=key).save(run_dir)

    def _ingest_job() -> str:
        m = Manifest.load(run_dir)
        s = RunState.load(run_dir)
        res = ingest(m, repo, sha, work_dir=run_dir, repo_url=url)
        s.advance(res.verdict)
        m.save(run_dir)
        s.save(run_dir)
        return res.reason

    run_in_background(run_dir, "ingest", _ingest_job)
    _invalidate_fleet_cache()
    return {"key": key, "status": "ingesting", "stage": "INGEST_LOCK"}


# ---- TASK MATRIX (human review #1) ---------------------------------------------------

@router.post("/runs/{key}/task-matrix")
def run_task_matrix(key: str) -> dict:
    """Kick off the TASK MATRIX cell (live LLM) in the background — returns immediately so the call
    survives the client leaving the page. Poll the run detail's `jobs.task_matrix` for status, then
    GET /task-matrix for the candidates. Idempotent while running."""
    s = _store()
    run_dir = Path(s.runs_dir) / key
    if s.get_manifest(key) is None:
        raise HTTPException(404, f"no run {key!r}")
    if get_jobs(run_dir).get("task_matrix", {}).get("status") == "running":
        return {"status": "running"}

    def _task_matrix_job() -> str:
        from ..cells.task_matrix import build_prompt
        from ..promptlog import write_prompt
        m = Manifest.load(run_dir)
        write_prompt(run_dir, "TASK_MATRIX", build_prompt(m))  # inspectable in the step viewer
        # TASK MATRIX is a light one-shot annotation, not a heavy synthesis cell → cell_model_light
        # (ADR-0042 routing: heavy default for oracle/create/synthesize, light for propose).
        out = propose(m, model=m.cell_model or LhConfig.load().cell_model_light)
        (run_dir / TASK_MATRIX_FILE).write_text(out.model_dump_json(indent=2) + "\n")
        return f"{len(out.candidates)} candidates"

    run_in_background(run_dir, "task_matrix", _task_matrix_job)
    return {"status": "running"}


@router.get("/runs/{key}/task-matrix")
def get_task_matrix(key: str) -> dict:
    path = Path(_store().runs_dir) / key / TASK_MATRIX_FILE
    if not path.exists():
        raise HTTPException(404, "TASK MATRIX not run yet")
    return TaskMatrixOutput.model_validate_json(path.read_text()).model_dump()


class SelectBody(BaseModel):
    pick: int | None = None   # candidate index; None => select nothing (drops the run)


@router.post("/runs/{key}/select")
def select(key: str, body: SelectBody) -> dict:
    s = _store()
    run_dir = Path(s.runs_dir) / key
    manifest = s.get_manifest(key)
    state = s.get_state(key)
    if body.pick is None:
        state.advance("none_selected")
        state.save(run_dir)
        _invalidate_fleet_cache()
        return {"stage": state.current_stage.value, "status": state.status}
    out = TaskMatrixOutput.model_validate_json((run_dir / TASK_MATRIX_FILE).read_text())
    c = out.candidates[body.pick]
    apply_selection(manifest, c)   # ONE shared code path with `programsmith pick` + the auto-pick handler
    state.task_identity = manifest.task_identity
    state.advance("selected")
    manifest.save(run_dir)
    state.save(run_dir)
    _invalidate_fleet_cache()
    return {"stage": state.current_stage.value, "identity": manifest.task_identity,
            "dimensions": manifest.dimensions.model_dump()}


class QaGateBody(BaseModel):
    decision: str   # accept | revise | reject (HUMAN REVIEW #2)


@router.post("/runs/{key}/qa-gate")
def qa_gate_decide(key: str, body: QaGateBody) -> dict:
    """Final-gate verdict — accept/revise/reject. HUMAN MODE ONLY (qa_gate_mode="human"): in auto
    mode (the ADR-0039 default) the driver computes the verdict from the recorded sweeps itself, so
    a manual verdict here would race/override it → 409. Advances the FSM (accept→DONE + outbox
    export, revise→SYNTHESIZE, reject→DROPPED) and logs backward moves to WORKFLOW_NOTES.md."""
    from ..workflow_notes import record_backward_move
    if LhConfig.load().qa_gate_mode != "human":
        raise HTTPException(409, "qa_gate_mode is auto")
    s = _store()
    run_dir = Path(s.runs_dir) / key
    state = s.get_state(key)
    if state.current_stage.value != "QA_GATE":
        raise HTTPException(409, f"run is at {state.current_stage.value}, not QA_GATE")
    if body.decision not in ("accept", "revise", "reject"):
        raise HTTPException(422, "decision must be accept | revise | reject")
    dec = state.advance(body.decision)
    record_backward_move(state, dec, trigger=f"qa_gate: human {body.decision}",
                         notes_path=Path(s.runs_dir).parent / "WORKFLOW_NOTES.md",
                         what_failed="human review #2")
    state.save(run_dir)
    export_note = None
    if body.decision == "accept":
        # accept → DONE is a pure FSM transition; the outbox export is a handler side effect the
        # auto path does inline but this manual path must trigger itself (else the accepted task —
        # the pipeline's only output — is silently dropped).
        from ..orchestrator import export_on_human_accept
        _dest, export_note = export_on_human_accept(run_dir)
    out = {"stage": state.current_stage.value, "status": state.status, "reason": dec.reason}
    if export_note:
        out["export"] = export_note
    _invalidate_fleet_cache()
    return out


# ---- outbox (the pipeline's OUTPUT — ADR-0039: export, not PR) ------------------------

@router.get("/outbox")
def outbox() -> dict:
    """List exported task bundles: QA accept → tasks/, easy shelf → easy/, Static-CI-only → drafts/.
    Each entry carries the `.provenance.json` stamp (run_id, task_identity,
    repo@sha, band, sweeps summary, ts) when present — a corrupt/missing stamp is surfaced as
    provenance=None rather than hiding the bundle (the export itself is the ground truth)."""
    root = Path(LhConfig.load().outbox_dir)

    def _entries(sub: str) -> list[dict]:
        d = root / sub
        out: list[dict] = []
        if not d.is_dir():
            return out
        for p in sorted(d.iterdir()):
            if not p.is_dir():
                continue
            prov = None
            pj = p / ".provenance.json"
            if pj.is_file():
                try:
                    prov = json.loads(pj.read_text())
                except (json.JSONDecodeError, OSError):
                    prov = None
            out.append({"slug": p.name, "path": str(p), "provenance": prov})
        return out

    return {"tasks": _entries("tasks"), "easy": _entries("easy"), "drafts": _entries("drafts")}


# ---- finished task actions ------------------------------------------------------------

@router.get("/runs/{key}/download")
def download_task(key: str):
    """Download the immutable exported Harbor task as a zip archive."""
    s = _store()
    run_dir = Path(s.runs_dir) / key
    try:
        manifest = s.get_manifest(key)
    except FileNotFoundError:
        raise HTTPException(404, f"no run {key!r}")
    task_dir = _exported_task_dir(run_dir, manifest)
    if task_dir is None:
        raise HTTPException(409, "The task is not exported yet")

    temp_dir = Path(tempfile.mkdtemp(prefix="programsmith-download-"))
    archive = Path(shutil.make_archive(str(temp_dir / task_dir.name), "zip", task_dir.parent, task_dir.name))
    return FileResponse(
        archive,
        media_type="application/zip",
        filename=f"{task_dir.name}.zip",
        background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
    )


class OddishRunBody(BaseModel):
    agent: str | None = None
    model: str | None = None


@router.get("/runs/{key}/oddish")
def oddish_status(key: str) -> dict:
    """Compact hosted-run status. Public experiment reads require no browser credential."""
    s = _store()
    run_dir = Path(s.runs_dir) / key
    if not run_state_exists(run_dir):
        raise HTTPException(404, f"no run {key!r}")
    from ..oddish import refresh_state
    return refresh_state(run_dir, api_url=LhConfig.load().oddish_api_url)


@router.get("/runs/{key}/oddish/trajectory")
def oddish_trajectory(key: str, trial_id: str | None = None):
    s = _store()
    run_dir = Path(s.runs_dir) / key
    if not run_state_exists(run_dir):
        raise HTTPException(404, f"no run {key!r}")
    from ..oddish import OddishError, get_trajectory
    try:
        value = get_trajectory(
            run_dir,
            api_url=LhConfig.load().oddish_api_url,
            trial_id=trial_id,
        )
    except OddishError as exc:
        raise HTTPException(502, str(exc))
    if value is None:
        raise HTTPException(404, "No Oddish trajectory is available yet")
    return value


@router.post("/runs/{key}/oddish")
def run_on_oddish(key: str, body: OddishRunBody) -> dict:
    """Launch one hosted Oddish trial for an exported task and publish its experiment."""
    s = _store()
    run_dir = Path(s.runs_dir) / key
    if not run_state_exists(run_dir):
        raise HTTPException(404, f"no run {key!r}")
    manifest = s.get_manifest(key)
    task_dir = _exported_task_dir(run_dir, manifest)
    if task_dir is None:
        raise HTTPException(409, "Finish and export the task before running it on Oddish")

    cfg = LhConfig.load()
    if not cfg.oddish_api_key:
        raise HTTPException(
            422,
            "Connect Oddish in Settings first. Create a full-scope key at "
            f"{cfg.oddish_dashboard_url}/settings.",
        )
    from ..oddish import load_state, save_state, submit_task
    existing = load_state(run_dir)
    if existing.get("status") in {"submitting", "queued", "running", "complete"}:
        return existing

    agent = (body.agent or cfg.oddish_agent).strip()
    model = (body.model or cfg.oddish_model).strip()
    if not agent or not model:
        raise HTTPException(422, "Oddish agent and model are required")
    pending = save_state(
        run_dir,
        {
            "status": "submitting",
            "task_name": task_dir.name,
            "agent": agent,
            "model": model,
            "trials": [],
        },
    )

    def _submit() -> str:
        result = submit_task(
            run_dir,
            task_dir,
            api_key=cfg.oddish_api_key or "",
            api_url=cfg.oddish_api_url,
            dashboard_url=cfg.oddish_dashboard_url,
            agent=agent,
            model=model,
        )
        return result.get("public_url") or result.get("experiment_url") or "submitted"

    run_in_background(run_dir, "oddish", _submit, stale_sec=1800)
    return pending


# ---- file / directory browser (task-detail viewer) ------------------------------------

@router.get("/runs/{key}/files")
def list_files(key: str, path: str = "") -> dict:
    """Directory tree for the run's working dir (generated task, source clone, sweep artifacts,
    manifests). Read-only, confined to the run dir. `path` scopes to a subtree (for lazy expansion)."""
    from . import files as fb
    s = _store()
    run_dir = Path(s.runs_dir) / key
    if not run_state_exists(run_dir):
        raise HTTPException(404, f"no run {key!r}")
    try:
        return {"root": key, "tree": fb.tree(run_dir, path)}
    except ValueError:
        raise HTTPException(400, "invalid path")


@router.get("/runs/{key}/file")
def read_file(key: str, path: str) -> dict:
    """Preview a single file under the run dir: text (with a language hint), an inlined small image,
    or metadata for binary/oversized files. Path-traversal guarded."""
    from . import files as fb
    s = _store()
    run_dir = Path(s.runs_dir) / key
    if not run_state_exists(run_dir):
        raise HTTPException(404, f"no run {key!r}")
    try:
        return fb.read(run_dir, path)
    except ValueError:
        raise HTTPException(400, "invalid path")
    except FileNotFoundError:
        raise HTTPException(404, f"no file {path!r}")


@router.get("/runs/{key}/agent-output")
def agent_output(key: str, tail_kb: int = 64) -> dict:
    """Live tail of the run's cell-agent (`claude -p`) output for the 'Agent output' panel. The session
    streams to `<run>/agent-logs/agent.log`; we return its last `tail_kb` KB plus whether an agentic
    job is currently running (so the UI can poll while active and stop when idle)."""
    import time

    from ..cells.agentic import AGENT_LOG_DIR, AGENT_LOG_FILE
    from ..jobs import active_job, get_jobs
    from ..orchestrator import _AGENT_SLOW_SEC
    run_dir = Path(_store().runs_dir) / key
    if not run_state_exists(run_dir):
        raise HTTPException(404, f"no run {key!r}")
    log = run_dir / AGENT_LOG_DIR / AGENT_LOG_FILE
    tail = ""
    if log.exists():
        raw = log.read_bytes()
        limit = max(1, tail_kb) * 1024
        chunk = raw[-limit:]
        if len(raw) > limit:
            # A byte tail usually begins inside one stream-json event. Drop that partial first line;
            # otherwise the SPA parses it as raw text and can render tens of KB of signatures/base64.
            newline = chunk.find(b"\n")
            chunk = chunk[newline + 1:] if newline >= 0 else b""
        tail = chunk.decode(errors="replace")
    job = active_job(run_dir)
    # Throttle hint: how long the live agent has been running. Past _AGENT_SLOW_SEC a cell is likely
    # throttled (shared-subscription contention) rather than doing legit long work — surfaced so the
    # operator can SEE it instead of guessing (the cjson lesson). None when no agent is running.
    elapsed = None
    if job:
        started = get_jobs(run_dir).get(job, {}).get("started_at")
        if started:
            elapsed = int(time.time() - started)
    return {"exists": log.exists(), "running": job is not None, "active_job": job, "tail": tail,
            "elapsed_sec": elapsed, "slow": elapsed is not None and elapsed > _AGENT_SLOW_SEC}


@router.post("/runs/{key}/pause")
def pause(key: str) -> dict:
    st = _store().set_paused(key, True)
    _invalidate_fleet_cache()
    return {"paused": st.paused, "halted": st.halted}


@router.post("/runs/{key}/resume")
def resume(key: str) -> dict:
    st = _store().set_paused(key, False)
    _invalidate_fleet_cache()
    return {"paused": st.paused}


@router.post("/runs/{key}/reopen")
def reopen(key: str) -> dict:
    """Human override: re-open a DROPPED/BLOCKED run and re-enter the harden loop with a fresh budget
    (the operator disagrees with the auto-drop / exhausted bound and wants to try more hardening). The
    driver picks it up on the next pass; if it still can't be hardened into band it honestly re-drops."""
    s = _store()
    run_dir = Path(s.runs_dir) / key
    try:
        st = s.get_state(key)
    except FileNotFoundError:
        raise HTTPException(404, f"no run {key!r}")
    try:
        st.reopen_for_harden()
    except ValueError as e:
        raise HTTPException(409, str(e))   # not terminal → nothing to reopen
    st.save(run_dir)
    _invalidate_fleet_cache()
    return {"stage": st.current_stage.value, "status": st.status, "harden": st.harden}


@router.post("/runs/{key}/retry")
def retry(key: str) -> dict:
    """Clear errored/orphaned agentic job(s) AND errored sweep records for a run parked at a blocked
    stage so the driver relaunches them FRESH on its next pass. Use after the bounded auto-retry is
    exhausted (3/3) and the operator has fixed the underlying cause (e.g. a redeploy) — the run is NOT
    terminal, it just re-blocks each pass on the dead job entry. Does not touch run state; the stage
    re-runs from its current node. Idempotent (returns [] when nothing was errored)."""
    from ..jobs import clear_errored_jobs
    from ..manifest import Manifest
    from ..orchestrator import clear_errored_sweeps
    s = _store()
    run_dir = Path(s.runs_dir) / key
    try:
        s.get_state(key)
    except FileNotFoundError:
        raise HTTPException(404, f"no run {key!r}")
    cleared = clear_errored_jobs(run_dir)
    try:
        cleared += [f"sweep[{k}]" for k in clear_errored_sweeps(Manifest.load(run_dir), run_dir)]
    except FileNotFoundError:   # pre-INGEST run: no manifest yet, so no sweeps to clear
        pass
    _invalidate_fleet_cache()
    return {"cleared": cleared}


@router.delete("/runs/{key}")
def delete_run_endpoint(key: str) -> dict:
    """Delete a run ENTIRELY (control-plane state + working tree). Irreversible — the UI confirms first.
    Used to clear out dropped/contaminated/dead runs. 404 if the run doesn't exist."""
    from ..statestore import delete_run
    s = _store()
    run_dir = Path(s.runs_dir) / key
    if not run_state_exists(run_dir):
        raise HTTPException(404, f"no run {key!r}")
    delete_run(run_dir)
    _invalidate_fleet_cache()
    return {"deleted": key}
