"""Tests for per-run sweep config (runconfig) — the ADR-0040 ladder defaults (Haiku smoke → Opus
frontier), sweep-name translation, band resolution, and validation fallback."""

from programsmith.manifest import Manifest
from programsmith.runconfig import (
    HARNESSES,
    AgentSpec,
    BandSpec,
    ModelBand,
    RunConfig,
    StageSpec,
    band_too_easy,
    band_value,
    band_verdict,
    default_run_config,
    effective_run_config,
    sweep_agent_name,
)


def _pin_anthropic_api_key(monkeypatch, tmp_path):
    """Deterministic credential state for default-config tests: an API key present (→ the
    universal mini-swe harness), no OAuth token."""
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "cfg.json"))  # built-in defaults, no repo config
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)


def test_default_config_is_haiku_smoke_then_opus_frontier(monkeypatch, tmp_path):
    """ADR-0040 ladder: a cheap Haiku smoke sweep (×3, saturate above 0.90) gates the Opus
    frontier sweep (×3, the 1/3–2/3 target window 0.30–0.70). With an API key present the
    credential-aware harness is mini-swe (the universal litellm harness)."""
    _pin_anthropic_api_key(monkeypatch, tmp_path)
    cfg = default_run_config()
    assert [(a.harness, a.model, a.n_trials) for a in cfg.difficulty.agents] == [
        ("mini-swe", "anthropic/claude-haiku-4-5", 3)]
    assert cfg.difficulty.band.basis == "aggregate"
    assert cfg.difficulty.band.min_pass == 0.0 and cfg.difficulty.band.max_pass == 0.90
    assert [(a.harness, a.model, a.n_trials) for a in cfg.full.agents] == [
        ("mini-swe", "anthropic/claude-opus-4-8", 3)]
    assert cfg.full.band.basis == "aggregate"
    assert cfg.full.band.min_pass == 0.30 and cfg.full.band.max_pass == 0.70


def test_default_harness_is_credential_aware(monkeypatch, tmp_path):
    """ANTHROPIC_API_KEY → mini-swe (litellm bills the key); OAuth-only → the claude-code overlay
    (the ONLY harness that can bill a subscription token); keyless → mini-swe (preflight flags
    the missing key — the default never silently picks something unusable)."""
    from programsmith.runconfig import default_local_harness
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "cfg.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    assert default_local_harness() == "mini-swe"           # API key wins (works for any model)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert default_local_harness() == "claude-code"        # OAuth-only rides the overlay
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    import programsmith.runconfig as rcmod
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda _b: None)  # no claude CLI either
    assert default_local_harness() == "mini-swe"


def test_default_config_honors_operator_model_and_band_overrides(monkeypatch, tmp_path):
    """The smoke/frontier models + band edges come from the operator config (bands are
    model-relative — override them together)."""
    cfgfile = tmp_path / "cfg.json"
    cfgfile.write_text('{"smoke_model": "anthropic/claude-sonnet-5", '
                       '"frontier_band_min": 0.2, "frontier_band_max": 0.6}')
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(cfgfile))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    cfg = default_run_config()
    assert cfg.difficulty.agents[0].model == "anthropic/claude-sonnet-5"
    assert cfg.full.band.min_pass == 0.2 and cfg.full.band.max_pass == 0.6


def test_default_band_windows_at_k3(monkeypatch, tmp_path):
    """The exact k=3 readings the ladder is tuned for: smoke saturates ONLY at 3/3 (farm rule:
    '3/3 = TOO EASY → harden; ≤2/3 = OK') and never band-drops a 0/3 (the label gate decides);
    frontier keeps 1/3 and 2/3 (both in-band), hardens 3/3, and reads 0/3 as too_hard (routed to
    the good-failure gate downstream, not silently kept)."""
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "cfg.json"))
    cfg = default_run_config()
    smoke, frontier = cfg.difficulty.band, cfg.full.band

    def g(p):
        return {"mini-swe@m": {"pass_at_1": p}}

    assert band_verdict(g(1.0), smoke) == "too_easy"       # 3/3 → saturated
    assert band_verdict(g(2 / 3), smoke) == "keep"         # 2/3 → fine
    assert band_verdict(g(0.0), smoke) == "keep"           # 0/3 → band keeps; label gate decides
    assert band_verdict(g(1 / 3), frontier) == "keep"      # 1/3 in the Opus window
    assert band_verdict(g(2 / 3), frontier) == "keep"      # 2/3 in the Opus window
    assert band_verdict(g(1.0), frontier) == "too_easy"    # 3/3 → harden/shelve path
    assert band_verdict(g(0.0), frontier) == "too_hard"    # 0/3 → good-failure gate path


def test_harness_catalog_carries_sweep_names():
    """Every catalog harness names its sweep-side registration; only mini-swe differs
    (registered as 'mini-swe-agent'). `sweep_agent_name` is the launch-path translation other
    modules call; unknown keys pass through unchanged so a custom stored config still launches."""
    assert all(v.get("sweep_name") for v in HARNESSES.values())
    assert HARNESSES["mini-swe"]["sweep_name"] == "mini-swe-agent"
    assert sweep_agent_name("mini-swe") == "mini-swe-agent"
    assert sweep_agent_name("claude-code") == "claude-code"
    assert sweep_agent_name("codex") == "codex"
    assert sweep_agent_name("not-in-catalog") == "not-in-catalog"


def test_band_value_aggregate_and_basis():
    groups = {
        "claude-code@opus": {"pass_at_1": 1.0},
        "codex@gpt": {"pass_at_1": 0.2},
        "gemini-cli@gemini": {"pass_at_1": 0.6},
    }
    assert band_value(groups, "aggregate") == 1.0          # best agent
    assert band_value(groups, "codex") == 0.2              # named basis
    assert band_value(groups, "gemini-cli") == 0.6
    assert band_value({}, "aggregate") is None             # nothing measured


def test_effective_run_config_falls_back_on_missing_or_bad(monkeypatch, tmp_path):
    _pin_anthropic_api_key(monkeypatch, tmp_path)                # built-in defaults
    m = Manifest(run_id="r", task_identity="t", slug="s")
    assert effective_run_config(m).full.band.max_pass == 0.70   # None → default (frontier ceiling)
    m.run_config = {"garbage": True}
    assert effective_run_config(m).difficulty.agents[0].harness == "mini-swe"  # invalid → default


# ---- per-model acceptance (BandSpec combinator + per_model) --------------------------------------

# opus aces the task (1.0), gpt struggles (0.33) — the "sellable to OpenAI" case.
_SELLABLE = {"claude-code@opus": {"pass_at_1": 1.0}, "codex@gpt": {"pass_at_1": 0.33}}
# every family aces it — genuinely too easy.
_ALL_EASY = {"claude-code@opus": {"pass_at_1": 1.0}, "codex@gpt": {"pass_at_1": 0.9}}


def test_band_too_easy_legacy_aggregate_unchanged():
    """No per_model → the historical single-number behavior (aggregate = max > max_pass → harden)."""
    legacy = BandSpec(basis="aggregate", max_pass=0.60)
    assert band_too_easy(_SELLABLE, legacy) is True     # max(1.0,0.33)=1.0 > 0.60 → too easy (today)
    assert band_too_easy({"codex@gpt": {"pass_at_1": 0.3}}, legacy) is False
    assert band_too_easy({}, legacy) is None            # nothing measured


def test_band_too_easy_any_keeps_task_hard_for_one_family():
    """'any' KEEPS the sellable task (Opus aces, GPT struggles) that the legacy band would harden."""
    band = BandSpec(combinator="any", per_model=[ModelBand(basis="claude-code", max_pass=0.60),
                                                 ModelBand(basis="codex", max_pass=0.60)])
    assert band_too_easy(_SELLABLE, band) is False      # codex 0.33 ≤ 0.60 → hard enough → KEEP
    assert band_too_easy(_ALL_EASY, band) is True       # both saturate → genuinely too easy → harden


def test_band_too_easy_all_requires_every_family_hard():
    band = BandSpec(combinator="all", per_model=[ModelBand(basis="claude-code", max_pass=0.60),
                                                 ModelBand(basis="codex", max_pass=0.60)])
    assert band_too_easy(_SELLABLE, band) is True       # opus saturates → not uniformly hard → harden
    hard_both = {"claude-code@opus": {"pass_at_1": 0.3}, "codex@gpt": {"pass_at_1": 0.33}}
    assert band_too_easy(hard_both, band) is False      # both hard → keep


def test_band_too_easy_skips_unmeasured_models():
    """A listed family with no trials this sweep is ignored; decision rests on what WAS measured."""
    band = BandSpec(combinator="any", per_model=[ModelBand(basis="codex", max_pass=0.60),
                                                 ModelBand(basis="gemini-cli", max_pass=0.60)])
    only_gemini_hard = {"gemini-cli@flash": {"pass_at_1": 0.2}}   # codex not measured
    assert band_too_easy(only_gemini_hard, band) is False         # gemini hard → keep
    assert band_too_easy({}, band) is None                        # neither measured


def _g(cc, cx):
    return {"claude-code@o": {"pass_at_1": cc}, "codex@g": {"pass_at_1": cx}}


def test_band_verdict_enforces_floor_only_when_set():
    """The window's LOWER bound is real once min_pass>0 — a task NO family can reach it on is 'too_hard'
    (→ drop). With the default floor of 0.0 it stays advisory (a 0% task is kept), preserving back-compat.
    This is the bug: both models at 0% is below a 10% floor but the old ceiling-only check kept it."""
    # per-model OR band, 10-60%
    band = BandSpec(combinator="any", per_model=[ModelBand(basis="claude-code", min_pass=0.10, max_pass=0.60),
                                                 ModelBand(basis="codex", min_pass=0.10, max_pass=0.60)])
    assert band_verdict(_g(0.0, 0.0), band) == "too_hard"    # both below floor → DROP (the reported bug)
    assert band_verdict(_g(0.0, 0.33), band) == "keep"       # codex in [10,60] → KEEP (OR)
    assert band_verdict(_g(0.11, 0.33), band) == "keep"      # both in band → keep
    assert band_verdict(_g(0.9, 0.9), band) == "too_easy"    # both above ceiling → harden
    assert band_verdict(_g(0.05, 0.9), band) == "too_easy"   # one hi, one lo, none in → hardenable

    # DEFAULT band (min_pass=0.0): floor is advisory → a 0% task is KEPT, never "too_hard"
    default = BandSpec(basis="aggregate", min_pass=0.0, max_pass=0.60)
    assert band_verdict(_g(0.0, 0.0), default) == "keep"     # unchanged historical behavior
    assert band_verdict(_g(0.9, 0.9), default) == "too_easy"


def test_band_verdict_default_frontier_floor_is_real():
    """The NEW default frontier band (0.30–0.70) has a REAL floor: a 0/3 result reads too_hard —
    band-level input to the good-failure gate (DESIGN §4.2), never a silent keep. (The orchestrator's
    _full_sweep_decide wiring is covered in test_orchestrator; this pins the band semantics it
    consumes.)"""
    frontier = default_run_config().full.band
    assert band_verdict(_g(0.0, 0.0), frontier) == "too_hard"     # below the 0.30 floor
    assert band_verdict(_g(0.33, 0.0), frontier) == "keep"        # aggregate = max = in-band
    assert band_verdict(_g(0.9, 0.9), frontier) == "too_easy"     # above the 0.70 ceiling


def test_band_verdict_per_model_any_keeps_sellable_task():
    """A per-model 'any' band keeps the sellable task (Opus aces, GPT struggles) that the legacy
    aggregate band would harden — the band-level truth _full_sweep_decide consumes."""
    any_band = BandSpec(combinator="any", per_model=[ModelBand(basis="claude-code", max_pass=0.60),
                                                     ModelBand(basis="codex", max_pass=0.60)])
    assert band_verdict(_SELLABLE, any_band) == "keep"             # kept (sellable to OpenAI)
    # the legacy aggregate band hardens the very same groups
    assert band_verdict(_SELLABLE, BandSpec(basis="aggregate", max_pass=0.60)) == "too_easy"


def test_effective_run_config_round_trips_custom():
    rc = RunConfig(
        difficulty=StageSpec(agents=[AgentSpec(harness="gemini-cli",
                                               model="google/gemini-3.1-pro-preview", n_trials=10)]),
        full=StageSpec(agents=[AgentSpec(harness="codex", model="openai/gpt-5.5", n_trials=5)]),
    )
    m = Manifest(run_id="r", task_identity="t", slug="s", run_config=rc.model_dump())
    eff = effective_run_config(m)
    assert eff.difficulty.agents[0].harness == "gemini-cli"
    assert eff.difficulty.agents[0].n_trials == 10
    assert eff.full.agents[0].harness == "codex"
