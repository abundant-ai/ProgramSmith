"""Pipeline configuration — keys + defaults the UI manages (settings/onboarding).

Persisted to a local JSON file (gitignored), env vars fill/override. Secrets are never returned
raw to the UI in full — `redacted()` masks them. Local-first auth model: the `claude` CLI the cells
shell out to authenticates via its own keychain login, CLAUDE_CODE_OAUTH_TOKEN, or
ANTHROPIC_API_KEY; sweep trials bill the operator's own provider key(s).
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel

def _config_path() -> Path:
    """Resolved at CALL time (not import time) so PROGRAMSMITH_CONFIG_PATH set by tests/operators is honored
    regardless of when config.py was first imported — otherwise a test's monkeypatched path is
    ignored and it scribbles on the real .programsmith/config.json."""
    return Path(os.getenv("PROGRAMSMITH_CONFIG_PATH", ".programsmith/config.json"))


class LhConfig(BaseModel):
    # ---- directories ----
    runs_dir: str = ".programsmith/runs"
    # QA_GATE accept → <outbox_dir>/tasks/<slug>/ ; EASY_SHELF → <outbox_dir>/easy/<slug>/
    # (ADR-0039: the pipeline's output is a directory, not a PR).
    outbox_dir: str = "out"

    # ---- cell model routing (ADR-0042) ----
    # Heavy cells (oracle/golden capture, create fill, synthesize plan+apply) run on the default;
    # light one-shots (task matrix, annotations) on cell_model_light; trajectory good-failure
    # audits on cell_model_analysis. Cost-conscious defaults — OSS operators own the bill; the
    # analysis model stays the strongest available (a weak classifier misroutes goodfail).
    default_cell_model: str = "claude-sonnet-5"
    cell_model_light: str = "claude-haiku-4-5"
    cell_model_analysis: str = "claude-opus-4-8"

    # ---- sweep models + band overrides (ADR-0040 ladder; bands are MODEL-RELATIVE) ----
    # The cheap smoke gates the expensive frontier. Changing a model shifts the pass@1
    # distribution, so the band ceilings/floor are overridable alongside.
    smoke_model: str = "anthropic/claude-haiku-4-5"
    frontier_model: str = "anthropic/claude-opus-4-8"
    smoke_band_max: float = 0.90       # smoke saturation ceiling (3/3-only at k=3)
    frontier_band_min: float = 0.30    # frontier floor (0/3 must earn its keep via goodfail)
    frontier_band_max: float = 0.70    # frontier saturation ceiling (the 1/3–2/3 window)

    # ---- BYO provider keys (the local trial runner + solver harnesses bill these) ----
    # All optional individually — one Anthropic credential path is required overall (the cells
    # ride it); every other provider unlocks its solver family when its key is present.
    claude_code_oauth_token: str | None = None
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    gemini_api_key: str | None = None      # Google AI Studio key (GEMINI_API_KEY / GOOGLE_API_KEY)
    zai_api_key: str | None = None         # Z.ai key (GLM models)

    # ---- hosted Oddish handoff -------------------------------------------------
    # ProgramSmith can upload an exported task, launch one hosted trial, and publish the resulting
    # experiment. The key belongs to the local operator's Oddish account and is stored with the same
    # owner-only permissions as model credentials. It is never exposed unmasked by the dashboard.
    oddish_api_key: str | None = None
    oddish_api_url: str = "https://abundant-ai--api.modal.run"
    oddish_dashboard_url: str = "https://www.oddish.app"
    oddish_agent: str = "claude-code"
    oddish_model: str = "anthropic/claude-sonnet-4-6"
    # Per-TRIAL cost cap handed to the mini-swe solver (`-l`; 0 = disabled). Deliberately 0 by
    # default: the product policy is cost preview + confirm before a sweep, not a silent cap that
    # kills long legitimate trials. Always passed explicitly (mini's own default would cap quietly).
    trial_cost_limit: float = 0.0

    # ---- human gates (ADR-0039: both AUTO by default — zero-touch farm runs) ----
    # "auto": TASK_MATRIX auto-picks the best candidate; QA_GATE auto-accepts on green checks.
    # "human": the pre-ADR-0039 blocking review behavior (UI panel / programsmith pick / programsmith qa-gate).
    task_matrix_mode: str = "auto"
    qa_gate_mode: str = "auto"

    # ---- ProgramBench task authorship (stamped into generated task.toml) ----
    author_name: str = "ProgramSmith"
    author_email: str = ""
    author_organization: str = "ProgramSmith"
    # Hard guard: refuse repos that are in the official ProgramBench dataset (programbench/guard.py).
    allow_programbench_overlap: bool = False

    # ---- execution knobs the auto-driver actually reads (see ui/app.py _autodrive_loop) ----
    # Optional override for the STATIC_CI check suite: a checkout with ci_checks/ (default: the
    # vendored in-tree suite under programsmith/checks/ci).
    ci_repo_root: str | None = None
    # Sweep depth (frontier trials per stage). Difficulty is coarse (×3); full is authoritative (×3).
    difficulty_trials: int = 3
    full_trials: int = 3
    # Fleet-wide cap on concurrent `claude -p` cell agents (orchestrator._agentic_concurrency reads
    # this each step). A single OAuth subscription throttles under concurrent load — keep it small.
    agentic_concurrency: int = 2
    # Max concurrent sweep TRIALS per local sweep (each is a full solver loop billing one key).
    local_trial_concurrency: int = 2
    # HARDEN REVIEW policy (gates.harden_review): drop a saturated task after this many non-converging
    # hardens, where "converging" means pass@1 fell ≥ harden_min_improvement vs the best prior attempt.
    harden_drop_after: int = 3  # = HARDEN_MAX: exhaust the harden budget before dropping a too-easy task
    harden_min_improvement: float = 0.10
    # Saved New-Run configurations (name → RunConfig dict), managed from the New Run dialog.
    presets: dict = {}

    # ---- CLI → server (optional) ----
    # When set, `programsmith new`/`farm`/`presets` target this server's HTTP API (the same endpoints the web
    # UI uses) instead of driving the local runs-dir in-process — so a CLI run shows up on a served
    # fleet. `--api <url>` / `PROGRAMSMITH_API` override per-invocation.
    api_url: str | None = None

    @classmethod
    def load_persisted(cls) -> "LhConfig":
        """Load only defaults + the local file, without folding process environment into it."""
        cfg = cls()
        path = _config_path()
        if path.exists():
            cfg = cls.model_validate_json(path.read_text())
        return cfg

    @classmethod
    def load(cls) -> "LhConfig":
        cfg = cls.load_persisted()
        # env fills anything unset (env is authoritative for the keys/dirs if present):
        # PROGRAMSMITH_RUNS_DIR (set by `programsmith serve --runs-dir`) binds the API *and* the auto-driver to the
        # same fleet directory — otherwise the operator's --runs-dir is silently dropped.
        cfg.runs_dir = os.getenv("PROGRAMSMITH_RUNS_DIR") or cfg.runs_dir
        # PROGRAMSMITH_CI_REPO_ROOT (set by `programsmith serve --ci-repo-root`) wins, else the persisted override,
        # else the vendored in-tree check suite.
        cfg.ci_repo_root = os.getenv("PROGRAMSMITH_CI_REPO_ROOT") or cfg.ci_repo_root
        cfg.api_url = os.getenv("PROGRAMSMITH_API") or cfg.api_url
        cfg.claude_code_oauth_token = (os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
                                       or cfg.claude_code_oauth_token)
        cfg.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY") or cfg.anthropic_api_key
        cfg.openai_api_key = os.getenv("OPENAI_API_KEY") or cfg.openai_api_key
        cfg.gemini_api_key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
                              or cfg.gemini_api_key)
        cfg.zai_api_key = os.getenv("ZAI_API_KEY") or cfg.zai_api_key
        cfg.oddish_api_key = os.getenv("ODDISH_API_KEY") or cfg.oddish_api_key
        cfg.oddish_api_url = os.getenv("ODDISH_API_URL") or cfg.oddish_api_url
        cfg.oddish_dashboard_url = (
            os.getenv("ODDISH_DASHBOARD_URL") or cfg.oddish_dashboard_url
        )
        if v := os.getenv("PROGRAMSMITH_TRIAL_COST_LIMIT"):
            try:
                cfg.trial_cost_limit = float(v)
            except ValueError:
                pass   # a malformed env value never crashes config load; the 0 default stands
        return cfg

    def save(self) -> Path:
        path = _config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Provider keys may be persisted here. Create the file owner-only from the first byte (and
        # tighten an existing file) rather than relying on the operator's umask.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(self.model_dump_json(indent=2) + "\n")
        path.chmod(0o600)
        return path

    def redacted(self) -> dict:
        """Config for the UI with secrets masked (only presence + last 4 chars shown)."""
        def mask(v: str | None) -> str | None:
            if not v:
                return None
            return f"…{v[-4:]}" if len(v) > 4 else "…"
        d = self.model_dump()
        d["claude_code_oauth_token"] = mask(self.claude_code_oauth_token)
        d["anthropic_api_key"] = mask(self.anthropic_api_key)
        d["openai_api_key"] = mask(self.openai_api_key)
        d["gemini_api_key"] = mask(self.gemini_api_key)
        d["zai_api_key"] = mask(self.zai_api_key)
        d["oddish_api_key"] = mask(self.oddish_api_key)
        return d


def model_subprocess_env() -> dict[str, str]:
    """Environment for model subprocesses, including credentials saved from Settings.

    Explicit process environment variables still win because :meth:`LhConfig.load` applies them
    over the local owner-only config file.
    """
    env = os.environ.copy()
    cfg = LhConfig.load()
    values = {
        "CLAUDE_CODE_OAUTH_TOKEN": cfg.claude_code_oauth_token,
        "ANTHROPIC_API_KEY": cfg.anthropic_api_key,
        "OPENAI_API_KEY": cfg.openai_api_key,
        "GEMINI_API_KEY": cfg.gemini_api_key,
        "ZAI_API_KEY": cfg.zai_api_key,
    }
    for name, value in values.items():
        if value:
            env[name] = value
    return env
