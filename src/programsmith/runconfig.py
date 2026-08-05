"""Per-run sweep configuration — the agents (harness × model × trials) and the pass-rate band used at
the DIFFICULTY (smoke) and FULL (frontier) sweep checkpoints, chosen when a run is created (New Run →
Advanced options).

Defaults implement the ADR-0040 difficulty ladder: a cheap Haiku SMOKE sweep (claude-code ×3,
saturate-above 0.90 — at k=3 only a 3/3 clears that ceiling, matching the farm's "3/3 = TOO EASY →
harden; ≤2/3 = OK") gates the expensive Opus FRONTIER sweep (claude-code ×3, target window
0.30–0.70 — the 1/3–2/3 frontier band; 0.333 and 0.667 are both in-band). The bands are
MODEL-RELATIVE: a different smoke/frontier pair shifts the pass@1 distribution, so override the
bands with the models. The catalog (HARNESSES / MODELS) is the single source the UI and backend
share, so a config is validated against the same option set the operator picked from.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ---- catalog (UI + backend share this) ----------------------------------------------
# provider keys drive which brand logo the UI shows. `recommended=False` renders a caveat.
# `sweep_name` is the harness's registered name in sweep configs/trial records: launch paths
# translate a catalog key → sweep_name (via `sweep_agent_name`) when building sweep agents; read
# paths fold it back (`trials._canon_agent`) so pass@1 group keys stay keyed by the CATALOG name
# and a band `basis` matches AgentSpec.group_key. The two differ only for mini-swe (registered as
# "mini-swe-agent"). Locally, mini-swe is the UNIVERSAL harness (pre-installed in every task
# image, drives any litellm model id on the provider's API key); claude-code / codex / gemini-cli
# run via a lazily-built solver overlay image (local_runner.NATIVE_CLI_PKGS) on that vendor's CLI.
HARNESSES: dict[str, dict] = {
    "mini-swe": {"label": "mini-SWE-agent", "provider": "miniswe", "recommended": True,
                 "sweep_name": "mini-swe-agent"},
    "claude-code": {"label": "Claude Code", "provider": "anthropic", "recommended": True,
                    "sweep_name": "claude-code"},
    "codex": {"label": "Codex", "provider": "openai", "recommended": True,
              "sweep_name": "codex"},
    "gemini-cli": {"label": "Gemini CLI", "provider": "google", "recommended": True,
                   "sweep_name": "gemini-cli"},
}


def sweep_agent_name(harness_key: str) -> str:
    """The sweep-side harness name for a catalog key (launch-path translation; identity for a key
    not in the catalog, so a custom/legacy stored config still launches). The read path inverts this
    via `trials._canon_agent`, keeping the whole band pipeline keyed by catalog names."""
    return (HARNESSES.get(harness_key) or {}).get("sweep_name") or harness_key
# Model ids are LITELLM ids (`<provider-prefix>/<model>`) — mini-swe passes them to litellm
# verbatim, so Google models use the `gemini/` prefix (litellm's Google AI Studio route;
# `google/...` is NOT a litellm provider — verified in-container against the pinned mini-swe).
# Native-CLI harnesses strip the prefix and pass the bare model name to their own CLI.
MODELS: dict[str, dict] = {
    "anthropic/claude-opus-4-8": {"label": "Opus 4.8", "provider": "anthropic"},
    "anthropic/claude-sonnet-5": {"label": "Sonnet 5", "provider": "anthropic"},
    "anthropic/claude-haiku-4-5": {"label": "Haiku 4.5", "provider": "anthropic"},
    "openai/gpt-5.5": {"label": "GPT-5.5", "provider": "openai"},
    "gemini/gemini-3.1-pro-preview": {"label": "Gemini 3.1 Pro", "provider": "google"},
    "gemini/gemini-3-flash": {"label": "Gemini 3 Flash", "provider": "google"},
    "zai/glm-5.2": {"label": "GLM 5.2", "provider": "zai"},
}

# litellm model-id prefix → provider (for models typed free-form, outside the MODELS catalog).
_PREFIX_PROVIDER = {"anthropic": "anthropic", "openai": "openai", "gemini": "google",
                    "google": "google", "vertex_ai": "google", "zai": "zai"}


def model_provider(model_id: str) -> str | None:
    """The credential provider for a model id: catalog entry wins, else the litellm prefix.
    None = unknown prefix (caller decides whether to warn or reject)."""
    entry = MODELS.get(model_id)
    if entry:
        return entry.get("provider")
    prefix = model_id.split("/", 1)[0] if "/" in model_id else ""
    return _PREFIX_PROVIDER.get(prefix)

BASELINE_TRIALS = 1  # oracle + nop, always added by the orchestrator (not part of the user config)


class AgentSpec(BaseModel):
    harness: str                       # a key in HARNESSES
    model: str                         # a key in MODELS (provider/name)
    n_trials: int = 3                  # the k in pass@k for this agent

    @property
    def group_key(self) -> str:
        """Matches trials.pass_at_1's grouping (by harness name) so a band `basis` can target it."""
        return self.harness


class ModelBand(BaseModel):
    """A per-model acceptance window used by the `any`/`all` combinator. `basis` is a harness key
    (matches `pass_at_1` grouping — see AgentSpec.group_key); `max_pass` is that model's saturation
    ceiling (a pass ABOVE it means the model finds the task too easy). `min_pass` stays advisory (LH
    tasks may legitimately sit at pass@1=0 for a model — that is not a reason to reject)."""

    basis: str
    min_pass: float = 0.0
    max_pass: float = 0.60


class BandSpec(BaseModel):
    # The acceptable pass-rate window for the checkpoint, measured over the configured trials.
    # `max_pass` is the saturation ceiling: a band ABOVE it is too easy → harden. `min_pass` is an
    # advisory floor shown in the UI (a pass@1=0 task with real headroom is still KEPT — LH tasks are
    # meant to be hard — so the floor does not auto-drop). `basis` is "aggregate" (best agent) or a
    # specific harness key from the stage's agents.
    basis: str = "aggregate"
    min_pass: float = 0.0
    max_pass: float = 0.60
    # What a BELOW-FLOOR frontier band does (ADR-0048). Both paths run the deep trajectory audit
    # (our own LLM over the failed trials' transcripts — never classifier labels alone) to classify
    # WHY the trials failed; the policy decides what a verified capability_headroom zero is worth:
    #   * "keep_verified_hard" (default) — audit-verified headroom SHIPS as a hard task (hard_keep);
    #     a design/env failure still eases/revises. The LH doctrine: headroom is the product.
    #   * "enforce_window"    — the window is a hard contract: audit-verified headroom still routes
    #     to EASE (reduce difficulty toward the window, bounded by the tune budgets); only a task
    #     that measures INSIDE the window ships. Exhausted ease budget → DROPPED, never a 0% export.
    on_too_hard: Literal["keep_verified_hard", "enforce_window"] = "keep_verified_hard"
    # PER-MODEL acceptance (optional). When `combinator` is "any"/"all" AND `per_model` is non-empty,
    # the keep-vs-harden decision uses the per-model windows instead of the single `basis` number:
    #   * "any"  → KEEP if AT LEAST ONE listed model finds the task hard (pass ≤ its max_pass); the task
    #              is "too easy" only when EVERY listed model saturates. This is the "sellable to
    #              whichever vendor finds it hard" policy — e.g. Opus aces (1.0) but GPT struggles
    #              (0.33) → KEEP (a good OpenAI task), where the legacy aggregate band would harden it.
    #   * "all"  → every listed model must find it hard → too easy if ANY listed model saturates
    #              (equivalent to the legacy aggregate default, made explicit and per-model).
    # "aggregate" (default) preserves the historical single-number behavior exactly. A listed model
    # whose harness wasn't measured this sweep is skipped.
    combinator: str = "aggregate"       # "aggregate" (legacy) | "any" | "all"
    per_model: list[ModelBand] = Field(default_factory=list)


class StageSpec(BaseModel):
    agents: list[AgentSpec] = Field(default_factory=list)
    band: BandSpec = Field(default_factory=BandSpec)


class RunConfig(BaseModel):
    difficulty: StageSpec
    full: StageSpec


def default_local_harness() -> str:
    """Credential-aware default harness (never guesses a provider the operator can't bill):
    an ANTHROPIC_API_KEY (env or config) drives the universal mini-swe harness (litellm inside the
    task image); an OAuth-only setup (CLAUDE_CODE_OAUTH_TOKEN, or just a logged-in `claude` CLI)
    can only bill through the claude-code CLI, so it rides the claude-code solver overlay. With no
    Anthropic credential at all, mini-swe stays the default — preflight/doctor flags the missing
    key with remediation, rather than this function silently picking something unusable."""
    import os
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        try:
            from .config import LhConfig
            api_key = LhConfig.load().anthropic_api_key
        except Exception:  # noqa: BLE001 — config is an input, not a dependency
            api_key = None
    if api_key:
        return "mini-swe"
    import shutil
    if os.getenv("CLAUDE_CODE_OAUTH_TOKEN") or shutil.which("claude"):
        return "claude-code"
    return "mini-swe"


def default_run_config() -> RunConfig:
    # ADR-0040 difficulty ladder — a cheap smoke model gates the expensive frontier:
    #   * SMOKE saturates ONLY at 3/3 (max_pass=0.90; at k=3 the only value above it is 1.0). Its
    #     floor stays 0.0 (advisory): a smoke 0/3 is decided by CALIBRATE's good-failure label gate,
    #     never auto-dropped by the band.
    #   * FRONTIER targets pass@1 ∈ [1/3, 2/3]; 0.30–0.70 keeps both 0.333 and 0.667 in-band.
    #     The floor is REAL (min_pass>0): a 0/3 reads "too_hard" and must earn its keep through the
    #     trajectory-verified good-failure gate — never a silent pass-through to QA_GATE.
    # Model + band choices come from the operator config (cost-conscious defaults: Haiku smoke,
    # Opus frontier — bands are model-relative, see the module docstring). The harness is
    # credential-aware (API key → mini-swe; OAuth-only → the claude-code overlay).
    c = _configured_sweep_defaults()
    harness = default_local_harness()
    return RunConfig(
        difficulty=StageSpec(
            agents=[AgentSpec(harness=harness, model=c["smoke_model"], n_trials=3)],
            band=BandSpec(basis="aggregate", min_pass=0.0, max_pass=c["smoke_band_max"])),
        full=StageSpec(
            agents=[AgentSpec(harness=harness, model=c["frontier_model"], n_trials=3)],
            band=BandSpec(basis="aggregate", min_pass=c["frontier_band_min"],
                          max_pass=c["frontier_band_max"])),
    )


def _configured_sweep_defaults() -> dict:
    """Smoke/frontier model ids + band overrides from the operator config, with the built-in
    defaults as the fallback — so `lh` works before any config file exists and a malformed one
    never wedges a run."""
    out = {"smoke_model": "anthropic/claude-haiku-4-5",
           "frontier_model": "anthropic/claude-opus-4-8",
           "smoke_band_max": 0.90, "frontier_band_min": 0.30, "frontier_band_max": 0.70}
    try:
        from .config import LhConfig
        cfg = LhConfig.load()
        for k in out:
            v = getattr(cfg, k, None)
            if v is not None and v != "":
                out[k] = v
    except Exception:  # noqa: BLE001 — config is an input, not a dependency
        pass
    return out


def effective_run_config(manifest) -> RunConfig:
    """The run's config, or the built-in default (back-compat for runs created before per-run config)."""
    raw = getattr(manifest, "run_config", None)
    if not raw:
        return default_run_config()
    try:
        return RunConfig.model_validate(raw)
    except Exception:  # noqa: BLE001 — a malformed stored config must never wedge a run; fall back
        return default_run_config()


def band_value(groups: dict | None, basis: str) -> float | None:
    """Resolve the band's pass-rate from a `pass_at_1().groups` map ({"<harness>@<model>": {pass_at_1}}).
    basis "aggregate" = the best (max) agent — the task is "solved" if ANY agent solves it; otherwise
    the named harness's group. Returns None when nothing measured.

    Keyed by the (harness, pass_at_1) PAIR, not by harness alone: two groups on the same harness but
    different models (e.g. `claude-code@haiku-4-5` and `claude-code@opus-4-8` in one stage) must not collide.
    aggregate = max over every measured group; a named harness = max over that harness's model(s)."""
    pairs: list[tuple[str, float]] = []
    for key, g in (groups or {}).items():
        pa = g.get("pass_at_1") if isinstance(g, dict) else None
        if isinstance(pa, (int, float)):
            pairs.append((key.split("@", 1)[0], pa))
    if not pairs:
        return None
    if basis == "aggregate":
        return max(pa for _, pa in pairs)
    matching = [pa for h, pa in pairs if h == basis]
    return max(matching) if matching else None


def band_too_easy(groups: dict | None, band: BandSpec) -> bool | None:
    """Is the task TOO EASY at this checkpoint (→ harden) given the measured per-model pass rates?

    Returns True (saturated → harden), False (keep → proceed), or None (nothing measured → the caller
    can't assess, so it proceeds and lets the authoritative gate decide).

    - Legacy path (`combinator == "aggregate"` OR empty `per_model`): saturated iff the single
      basis-resolved pass rate exceeds `max_pass` — byte-for-byte the historical behavior (aggregate =
      max across families, so "harden unless ALL families find it hard").
    - "any": KEEP if at least one listed model finds the task hard (pass ≤ its own max_pass); too easy
      only when EVERY listed (and measured) model saturates. The "sellable to whichever vendor finds it
      hard" policy.
    - "all": every listed model must find it hard → too easy if ANY listed (measured) model saturates.
    A listed model whose harness produced no measurement this sweep is skipped (not counted either way).
    """
    if band.combinator not in ("any", "all") or not band.per_model:
        v = band_value(groups, band.basis)
        return None if v is None else v > band.max_pass
    saturates: list[bool] = []
    for mb in band.per_model:
        p = band_value(groups, mb.basis)
        if p is None:
            continue                       # this model wasn't measured this sweep → ignore it
        saturates.append(p > mb.max_pass)  # True = this model finds the task too easy
    if not saturates:
        return None                        # none of the listed models measured → can't assess
    # "any": too easy only if ALL listed models saturate (none finds it hard).
    # "all": too easy if ANY listed model saturates (not uniformly hard).
    return all(saturates) if band.combinator == "any" else any(saturates)


def band_verdict(groups: dict | None, band: BandSpec) -> str | None:
    """FULL keep/too-easy/too-hard verdict for a checkpoint, honoring BOTH bounds of the band — unlike
    `band_too_easy`, which only checks the ceiling. Returns:
      * "keep"       — a task is IN the target window → proceed;
      * "too_easy"   — above `max_pass` → harden (make it harder);
      * "too_hard"   — below `min_pass` → DROP (nobody can reach the floor; hardening only makes it
                       worse). ONLY fires when a floor is SET (min_pass > 0);
      * None         — nothing measured.

    The floor is enforced ONLY when `min_pass > 0`, so a default band (min_pass=0.0) is byte-for-byte
    unchanged — a pass@1=0 task is still KEPT (the historical "LH tasks are meant to be hard" behavior).
    A band with an explicit floor (e.g. 10-60%) rejects a task NO model can solve at the floor rate
    (e.g. every family at 0% is outside the window, not a valid calibrated task) — closing the gap
    where such tasks silently reached QA_GATE.

    Per-model (any/all): a model is IN-BAND iff `min_pass <= pass <= max_pass`.
      * "any": KEEP if ANY listed model is in-band; else "too_easy" if any is above its max (hardening
        can pull it into range); else "too_hard" (every measured model is below its floor).
      * "all": KEEP iff every measured model is in-band; else "too_easy" if any is above max; else
        "too_hard".
    """
    def _cls(p: float, lo: float, hi: float) -> str:
        if p > hi:
            return "hi"
        if lo > 0 and p < lo:
            return "lo"
        return "in"

    if band.combinator not in ("any", "all") or not band.per_model:
        v = band_value(groups, band.basis)
        if v is None:
            return None
        c = _cls(v, band.min_pass, band.max_pass)
        return {"hi": "too_easy", "lo": "too_hard", "in": "keep"}[c]
    classes: list[str] = []
    for mb in band.per_model:
        p = band_value(groups, mb.basis)
        if p is None:
            continue
        classes.append(_cls(p, mb.min_pass, mb.max_pass))
    if not classes:
        return None
    if band.combinator == "any":
        if "in" in classes:
            return "keep"
        return "too_easy" if "hi" in classes else "too_hard"
    # "all": every measured model must be in-band
    if all(c == "in" for c in classes):
        return "keep"
    return "too_easy" if "hi" in classes else "too_hard"
