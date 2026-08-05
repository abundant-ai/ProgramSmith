"""Preflight is local-only: Docker + one Anthropic credential + disk headroom are REQUIRED; every
other provider key (OpenAI / Google / Z.ai) is OPTIONAL (enables that solver family, never
blocks). There is NO GitHub check (ADR-0039: PR automation removed; INGEST clones public repos
anonymously over https)."""

from programsmith import preflight
from programsmith.config import LhConfig


def _no_env_creds(monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
              "GEMINI_API_KEY", "GOOGLE_API_KEY", "ZAI_API_KEY"):
        monkeypatch.delenv(v, raising=False)


def test_local_preflight_checks_docker_cred_disk(monkeypatch):
    _no_env_creds(monkeypatch)
    monkeypatch.setattr(preflight, "_docker_ok", lambda: (True, "Docker running"))
    monkeypatch.setattr(preflight, "_disk_ok", lambda: (True, "100 GB free"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    out = preflight.check_preflight(LhConfig(anthropic_api_key="sk-ant-xxx"))
    assert out["backend"] == "local" and out["ready"] is True
    names = {c["name"] for c in out["checks"]}
    assert names == {"docker", "anthropic_cred", "disk",
                     "openai_optional", "google_optional", "zai_optional"}
    # no cloud-service or GitHub credential is ever required
    assert "github" not in names   # no PR output + anonymous clone → gh never required
    # the per-provider table rides along for doctor / run-creation fail-fast
    assert out["providers"]["anthropic"]["present"] is True
    assert out["providers"]["google"]["present"] is False


def test_local_preflight_not_ready_without_anthropic_cred(monkeypatch):
    _no_env_creds(monkeypatch)
    monkeypatch.setattr(preflight, "_docker_ok", lambda: (True, "Docker running"))
    monkeypatch.setattr(preflight, "_disk_ok", lambda: (True, "100 GB free"))
    monkeypatch.setattr(preflight.shutil, "which", lambda _b: None)   # no claude CLI either
    out = preflight.check_preflight(LhConfig())
    assert out["ready"] is False
    assert any(c["name"] == "anthropic_cred" and not c["ok"] for c in out["checks"])


def test_local_preflight_claude_cli_keychain_counts_as_cred(monkeypatch):
    """The `claude` CLI on PATH carries its own keychain login — that alone satisfies the
    Anthropic-credential check (no env var required)."""
    _no_env_creds(monkeypatch)
    monkeypatch.setattr(preflight, "_docker_ok", lambda: (True, "Docker running"))
    monkeypatch.setattr(preflight, "_disk_ok", lambda: (True, "100 GB free"))
    monkeypatch.setattr(preflight.shutil, "which", lambda _b: "/usr/local/bin/claude")
    out = preflight.check_preflight(LhConfig())
    assert out["ready"] is True
    cred = next(c for c in out["checks"] if c["name"] == "anthropic_cred")
    assert cred["ok"] and "keychain" in cred["detail"]


def test_local_preflight_not_ready_without_docker(monkeypatch):
    monkeypatch.setattr(preflight, "_docker_ok", lambda: (False, "Docker not found"))
    monkeypatch.setattr(preflight, "_disk_ok", lambda: (True, "100 GB free"))
    out = preflight.check_preflight(LhConfig(anthropic_api_key="sk-ant-x"))
    assert out["ready"] is False
    assert any(c["name"] == "docker" and not c["ok"] for c in out["checks"])


def test_openai_key_is_optional_and_never_blocks(monkeypatch):
    """No OpenAI key → the codex family is reported disabled but the check stays ok=True; with a
    key it reports the family enabled."""
    _no_env_creds(monkeypatch)
    monkeypatch.setattr(preflight, "_docker_ok", lambda: (True, "Docker running"))
    monkeypatch.setattr(preflight, "_disk_ok", lambda: (True, "100 GB free"))
    monkeypatch.setattr(preflight.shutil, "which", lambda _b: None)
    out = preflight.check_preflight(LhConfig(anthropic_api_key="sk-ant-x"))
    opt = next(c for c in out["checks"] if c["name"] == "openai_optional")
    assert opt["ok"] and "disabled" in opt["detail"]
    assert out["ready"] is True                      # absence never gates readiness
    out2 = preflight.check_preflight(LhConfig(anthropic_api_key="sk-ant-x", openai_api_key="sk-oai"))
    opt2 = next(c for c in out2["checks"] if c["name"] == "openai_optional")
    assert opt2["ok"] and "enabled" in opt2["detail"]


def test_disk_floor_blocks_readiness(monkeypatch):
    _no_env_creds(monkeypatch)
    monkeypatch.setattr(preflight, "_docker_ok", lambda: (True, "Docker running"))
    monkeypatch.setattr(preflight, "_disk_ok", lambda: (False, "only 3 GB free"))
    out = preflight.check_preflight(LhConfig(anthropic_api_key="sk-ant-x"))
    assert out["ready"] is False
    assert any(c["name"] == "disk" and not c["ok"] for c in out["checks"])


# ---- credential_for: the (harness, provider) pair matters ------------------------------------

def test_credential_for_oauth_only_satisfies_claude_code_but_not_mini_swe(monkeypatch):
    """An OAuth-only operator (CLAUDE_CODE_OAUTH_TOKEN, no API key) can bill through the
    claude-code CLI — but NOT through mini-swe (litellm needs the API key), and the failure
    message must name the remediation (switch harness or set the key)."""
    _no_env_creds(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    ok, detail = preflight.credential_for("claude-code", "anthropic", LhConfig())
    assert ok and "OAUTH" in detail.upper()
    ok2, detail2 = preflight.credential_for("mini-swe", "anthropic", LhConfig())
    assert not ok2 and "ANTHROPIC_API_KEY" in detail2 and "claude-code" in detail2


def test_credential_for_other_providers(monkeypatch):
    _no_env_creds(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    assert preflight.credential_for("mini-swe", "google", LhConfig())[0] is True
    assert preflight.credential_for("codex", "openai", LhConfig())[0] is False
    assert preflight.credential_for("mini-swe", "zai", LhConfig())[0] is False
    # an unknown provider is never blocked (custom setups are the operator's call)
    assert preflight.credential_for("mini-swe", None, LhConfig())[0] is True


def test_missing_provider_creds_names_stage_and_remediation(monkeypatch):
    """The run-creation fail-fast: a config whose frontier agent needs a keyless provider is
    rejected with the stage, agent, and remediation named — never a mid-sweep error."""
    _no_env_creds(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    cfg = {"difficulty": {"agents": [{"harness": "mini-swe",
                                      "model": "anthropic/claude-haiku-4-5", "n_trials": 3}],
                          "band": {}},
           "full": {"agents": [{"harness": "gemini-cli",
                                "model": "gemini/gemini-3.1-pro-preview", "n_trials": 3}],
                    "band": {}}}
    missing = preflight.missing_provider_creds(cfg, LhConfig(anthropic_api_key="sk-ant-x"))
    assert len(missing) == 1
    assert "frontier" in missing[0] and "gemini-cli" in missing[0] and "GEMINI_API_KEY" in missing[0]
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    assert preflight.missing_provider_creds(cfg, LhConfig(anthropic_api_key="sk-ant-x")) == []
