"""Preflight — verify everything needed before any spend (PRODUCT.md auth_preflight).

Each check returns {name, ok, detail}. The pipeline runs fully locally: trials execute in a local
Docker sandbox and every model call bills the operator's own credentials, so preflight verifies
exactly three REQUIRED things — Docker is up, an Anthropic credential is reachable
(CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_API_KEY, or the `claude` CLI's own keychain login), and there
is disk headroom for task images. Every OTHER provider (OpenAI / Google / Z.ai) is OPTIONAL: a
present key unlocks that solver family; absence never blocks readiness. The per-(harness,
provider) credential check (`credential_for`) is also the fail-fast used at RUN CREATION: a run
configured for a provider with no key is rejected up front with the remediation named, instead of
burning a sweep on trials that can only error. There is NO GitHub check: INGEST clones public
repos anonymously over https and the pipeline's output is an outbox directory (ADR-0039), so `gh`
auth is not required anywhere.
"""

from __future__ import annotations

import os
import shutil
import subprocess

from .config import LhConfig

# Task images (toolchain + vendored deps) run multi-GB each; below this free-space floor a sweep
# will start failing docker builds with cryptic ENOSPC errors, so preflight flags it up front.
MIN_FREE_DISK_GB = 20


def _docker_ok() -> tuple[bool, str]:
    try:
        p = subprocess.run(["docker", "info"], capture_output=True, timeout=8)
        return (p.returncode == 0,
                "Docker running" if p.returncode == 0 else "Docker installed but not running — start it")
    except FileNotFoundError:
        return False, "Docker not found — install Docker Desktop/engine (the local sandbox needs it)"
    except subprocess.TimeoutExpired:
        return False, "Docker not responding"


def _anthropic_cred(config: LhConfig | None = None) -> tuple[bool, str]:
    """One Anthropic credential path is REQUIRED (the cells + the claude-code solver ride it).
    Precedence mirrors the `claude` CLI's own resolution: CLAUDE_CODE_OAUTH_TOKEN >
    ANTHROPIC_API_KEY (env or config) > the CLI's keychain login (`claude` on PATH — the CLI
    manages its own stored session, so its presence is the signal)."""
    cfg = config or LhConfig.load()
    if os.getenv("CLAUDE_CODE_OAUTH_TOKEN") or cfg.claude_code_oauth_token:
        return True, "CLAUDE_CODE_OAUTH_TOKEN present (active credential)"
    if os.getenv("ANTHROPIC_API_KEY") or cfg.anthropic_api_key:
        return True, "ANTHROPIC_API_KEY present (active credential)"
    if shutil.which("claude"):
        return True, "claude CLI on PATH (its keychain login is the active credential)"
    return False, ("no Anthropic credential — set CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY, "
                   "or install + log in the `claude` CLI")


def _disk_ok() -> tuple[bool, str]:
    try:
        free_gb = shutil.disk_usage(".").free / 1_000_000_000
    except OSError:
        return True, "disk space unknown (stat failed) — proceeding"
    if free_gb < MIN_FREE_DISK_GB:
        return False, (f"only {free_gb:.0f} GB free — task images need headroom "
                       f"(≥{MIN_FREE_DISK_GB} GB recommended; prune docker images)")
    return True, f"{free_gb:.0f} GB free"


# ---- per-provider credential table (doctor + run-creation fail-fast) ---------------------------

def provider_credentials(config: LhConfig | None = None) -> dict[str, dict]:
    """One row per solver-credential provider: {present, detail, remediation}. `anthropic` is the
    only REQUIRED provider (the cells ride it); the rest unlock their solver family when present."""
    cfg = config or LhConfig.load()
    anth_ok, anth_detail = _anthropic_cred(cfg)
    rows = {
        "anthropic": {"present": anth_ok, "detail": anth_detail, "required": True,
                      "remediation": ("add an OAuth token or API key in Settings, or install + "
                                      "log in the `claude` CLI")},
        "openai": {"present": bool(cfg.openai_api_key or os.getenv("OPENAI_API_KEY")),
                   "detail": "OPENAI_API_KEY", "required": False,
                   "remediation": "set OPENAI_API_KEY (codex / OpenAI models bill it)"},
        "google": {"present": bool(cfg.gemini_api_key or os.getenv("GEMINI_API_KEY")
                                   or os.getenv("GOOGLE_API_KEY")),
                   "detail": "GEMINI_API_KEY / GOOGLE_API_KEY", "required": False,
                   "remediation": "set GEMINI_API_KEY (a Google AI Studio key; gemini models bill it)"},
        "zai": {"present": bool(cfg.zai_api_key or os.getenv("ZAI_API_KEY")),
                "detail": "ZAI_API_KEY", "required": False,
                "remediation": "set ZAI_API_KEY (GLM models bill it)"},
    }
    for name, row in rows.items():
        if name != "anthropic":
            row["detail"] = (f"{row['detail']} present — {name} solver family enabled"
                             if row["present"]
                             else f"no {name} key (optional) — {name} solver family disabled")
    return rows


def credential_for(harness: str, provider: str | None,
                   config: LhConfig | None = None) -> tuple[bool, str]:
    """Is there a usable credential for this (harness, provider) pair, and which one is active.

    The pair matters: on an Anthropic model, the claude-code CLI can bill the subscription OAuth
    token or the keychain login, but mini-swe (litellm) can ONLY bill ANTHROPIC_API_KEY — an
    OAuth-only operator must ride the claude-code overlay. An unknown provider passes (custom
    setups are the operator's call; nothing here can assess them)."""
    cfg = config or LhConfig.load()
    if provider == "anthropic":
        if harness == "claude-code":
            ok, detail = _anthropic_cred(cfg)
            return ok, detail if ok else ("no Anthropic credential — set CLAUDE_CODE_OAUTH_TOKEN "
                                          "or ANTHROPIC_API_KEY, or log in the `claude` CLI")
        if cfg.anthropic_api_key or os.getenv("ANTHROPIC_API_KEY"):
            return True, "ANTHROPIC_API_KEY"
        return False, (f"{harness} drives Anthropic models through litellm, which needs "
                       "ANTHROPIC_API_KEY — set it, or switch this stage to the claude-code "
                       "harness (it can bill your Claude subscription OAuth token)")
    if provider == "openai":
        if cfg.openai_api_key or os.getenv("OPENAI_API_KEY"):
            return True, "OPENAI_API_KEY"
        return False, "set OPENAI_API_KEY (codex / OpenAI models bill it)"
    if provider == "google":
        if cfg.gemini_api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
            return True, "GEMINI_API_KEY"
        return False, "set GEMINI_API_KEY (a Google AI Studio key; gemini models bill it)"
    if provider == "zai":
        if cfg.zai_api_key or os.getenv("ZAI_API_KEY"):
            return True, "ZAI_API_KEY"
        return False, "set ZAI_API_KEY (GLM models bill it)"
    return True, f"provider {provider!r} not credential-checked"


def missing_provider_creds(run_config: dict | None,
                           config: LhConfig | None = None) -> list[str]:
    """The RUN-CREATION fail-fast: for every (harness, model) agent this run configures, verify a
    usable credential exists NOW. Returns one remediation line per missing credential (empty =
    good to go). None/default config is checked against the built-in defaults, so a zero-config
    `new` on a keyless machine still fails fast instead of erroring mid-sweep."""
    from .runconfig import RunConfig, effective_run_config, model_provider
    if run_config is None:
        from .runconfig import default_run_config
        rc = default_run_config()
    else:
        try:
            rc = RunConfig.model_validate(run_config)
        except Exception:  # noqa: BLE001 — an invalid config is the schema validator's problem
            return []
    missing: list[str] = []
    seen: set[tuple[str, str | None]] = set()
    for stage_name, stage in (("smoke", rc.difficulty), ("frontier", rc.full)):
        for a in (stage.agents or []):
            provider = model_provider(a.model)
            if (a.harness, provider) in seen:
                continue
            seen.add((a.harness, provider))
            ok, detail = credential_for(a.harness, provider, config)
            if not ok:
                missing.append(f"{stage_name} agent {a.harness} @ {a.model}: {detail}")
    return missing


def check_preflight(config: LhConfig | None = None) -> dict:
    cfg = config or LhConfig.load()
    docker_ok, docker_detail = _docker_ok()
    cred_ok, cred_detail = _anthropic_cred(cfg)
    disk_ok, disk_detail = _disk_ok()
    providers = provider_credentials(cfg)
    checks = [
        {"name": "docker", "ok": docker_ok, "detail": docker_detail},
        {"name": "anthropic_cred", "ok": cred_ok, "detail": cred_detail},
        {"name": "disk", "ok": disk_ok, "detail": disk_detail},
    ]
    # OPTIONAL provider rows — never gate readiness; presence enables that solver family.
    for name in ("openai", "google", "zai"):
        checks.append({"name": f"{name}_optional", "ok": True,
                       "detail": providers[name]["detail"]})
    ready = docker_ok and cred_ok and disk_ok
    return {"ready": ready, "backend": "local", "checks": checks, "providers": providers}
