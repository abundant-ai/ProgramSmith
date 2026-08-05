"""Manifest — the rich per-run context carried across stages.

Companion to the rigid `RunState` FSM tracker: this holds the *context* (source, dimensions,
oracle, sweeps, snapshot) that cells produce and gates read. Gates are pure functions over this
object. Persists to `<run_dir>/manifest.json`. Mirrors schemas/manifest.schema.json (repo root).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel, Field

from .statestore import store_for

MANIFEST_FILENAME = "manifest.json"

LICENSE_CLASSES = ("permissive", "weak-copyleft", "strong-copyleft", "unknown")


class SourceInfo(BaseModel):
    repo: str                      # "owner/name"
    pinned_sha: str
    repo_url: str | None = None
    license: str | None = None     # detected license name (e.g. "BSD-3-Clause-ish", "GPL-3.0")
    license_class: str = "unknown"  # one of LICENSE_CLASSES
    copyleft_blocked: bool = False  # strong/weak-copyleft => INGEST drops (ADR-0010 / 02-ideas gate)
    primary_language: str | None = None
    build_systems: list[str] = Field(default_factory=list)
    test_frameworks: list[str] = Field(default_factory=list)
    size_files: int | None = None
    size_loc: int | None = None
    clone_path: str | None = None  # where the locked checkout lives (per-run snapshot)
    has_cli_entrypoint: bool | None = None
    cli_entrypoint: str | None = None


class Dimensions(BaseModel):
    # Filled by TASK MATRIX (auto-picked by default, ADR-0039). ALL fields optional: None until the
    # pick, and the legacy rewrite-port axes stay loadable so pre-ADR-0038 manifests hydrate as-is.
    # ---- ProgramBench axes (ADR-0038): one CLI tool × the flag surface the grader exercises ----
    tool_name: str | None = None             # tool the agent reimplements (may differ from repo name)
    binary_name: str | None = None           # actual executable name (e.g. difft for difftastic)
    upstream_language: str | None = None     # go | rust | c | cpp — selects toolchain + oracle-pair flag recipe
    flag_surface: str | None = None          # subcommands/flags the grader exercises; part of task identity
    case_families: list[str] = Field(default_factory=list)  # 5-12 feature families the case suite covers
    stdin_friendly: bool | None = None       # verifier pipes stdin (file-walking tools need files_dir instead)
    needs_files_dir: bool | None = None      # per-case cwd fixture trees required (just/editorconfig style)
    deterministic_output: bool | None = None  # possibly only under pinned flags (--sort path --color never)
    expected_difficulty: str | None = None   # moderate | hard | frontier (advisory; the sweeps decide)
    expert_hours: int | None = None          # 12-60 per ProgramBench tier (stamped into task.toml)
    # ---- legacy rewrite-port axes (pre-ADR-0038; kept ONLY so old manifests load) ----
    target_language: str | None = None       # Rust | TypeScript (retired genre, ADR-0038)
    scope_unit: str | None = None            # whole-library | subsystem | single-algorithm
    verifier_mechanism: str | None = None    # golden-io | differential-oracle
    objective: str | None = None             # equivalence | equivalence+performance | +constraints


class Manifest(BaseModel):
    schema_version: str = "0.1.0"
    run_id: str
    task_identity: str
    slug: str | None = None

    source: SourceInfo | None = None
    dimensions: Dimensions | None = None
    oracle: dict | None = None
    sweeps: dict = Field(default_factory=dict)
    snapshot: dict | None = None
    notes: list[str] = Field(default_factory=list)
    # Per-run sweep configuration chosen at creation (agents + pass-rate band per stage). None ⇒ the
    # built-in defaults (runconfig.default_run_config). See programsmith.runconfig.RunConfig.
    run_config: dict | None = None
    # Optional operator brief fed to the TASK MATRIX agent at creation — an idea, constraints, or
    # specs to steer candidate proposal. Advisory; the cell still emits schema-validated candidates.
    task_brief: str | None = None
    # Deterministic, pre-model eligibility screen for the selected pipeline profile. Persisted so
    # the dashboard and release audit can distinguish "source screened out" from a failed task.
    source_screen: dict | None = None
    # "draft" stops and exports after STATIC_CI. The selected cell model is persisted so daemon/UI
    # resumes cannot silently fall back to a different model.
    pipeline_mode: str = "full"
    cell_model: str | None = None
    # Per-generation saturation record for the HARDEN REVIEW auditor: one entry each time a
    # saturation harden fires, so the auditor can see whether iterative hardening is moving the band
    # (and drop a task that's too easy to harden). Survives _finalize_patch (which clears `sweeps`).
    harden_history: list[dict] = Field(default_factory=list)

    # Attribution (WS5): the signed-in operator who created this run (email), when the cloud link is
    # authed. Shared fleet — everyone sees every run — but each is tagged with who kicked it off. None
    # for local/unauthenticated runs.
    created_by: str | None = None

    created_at: str | None = None
    updated_at: str | None = None

    # ---- persistence ----------------------------------------------------------------

    def save(self, run_dir: str | Path) -> Path:
        # Routed through the StateStore seam; `LocalFileStore` is the atomic write. Atomicity
        # matters: a background agentic job re-loads the manifest while the driver saves it —
        # write_atomic (tmp + os.replace) makes every read see a complete prior or new file,
        # never an empty/partial one.
        run_dir = Path(run_dir)
        store, key = store_for(run_dir)
        store.write_atomic(f"{key}/{MANIFEST_FILENAME}", self.model_dump_json(indent=2) + "\n")
        return run_dir / MANIFEST_FILENAME

    @classmethod
    def load(cls, run_dir: str | Path) -> "Manifest":
        store, key = store_for(run_dir)
        raw = store.read(f"{key}/{MANIFEST_FILENAME}")
        if raw is None:
            raise FileNotFoundError(str(Path(run_dir) / MANIFEST_FILENAME))
        return cls.model_validate_json(raw)


def source_identity(repo: str, sha: str) -> str:
    """Provisional source-level id used before TASK MATRIX fixes the task dimensions."""
    return "src:" + hashlib.sha256(f"{repo}@{sha}".encode()).hexdigest()[:16]


def task_identity(repo: str, sha: str, target_language: str, scope_unit: str) -> str:
    """LEGACY task-identity hash (ADR-0012, rewrite-port axes): source@SHA × target-lang ×
    scope-unit. Kept so old manifests/state stay interpretable; new runs use
    `programbench_task_identity`. The dedup-hash mechanism itself is never removed (invariant)."""
    key = f"{repo}@{sha}|{target_language}|{scope_unit}"
    return "task:" + hashlib.sha256(key.encode()).hexdigest()[:16]


def programbench_task_identity(repo: str, sha: str, tool_name: str, flag_surface: str) -> str:
    """ProgramBench task-identity hash (ADR-0038): source@SHA × tool × flag-surface. Same dedup
    mechanism as the legacy hash — only the axes changed — so concurrent/rerun fleets still never
    double-create the same task."""
    key = f"{repo}@{sha}|programbench|{tool_name}|{flag_surface}"
    return "task:" + hashlib.sha256(key.encode()).hexdigest()[:16]
