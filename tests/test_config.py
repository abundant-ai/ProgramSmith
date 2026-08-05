"""Configuration persistence and secret-handling invariants."""

import stat

from programsmith.config import LhConfig


def test_saved_config_is_owner_only_and_redacts_keys(tmp_path, monkeypatch):
    path = tmp_path / "programsmith" / "config.json"
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(path))
    cfg = LhConfig(claude_code_oauth_token="oauth-secret", anthropic_api_key="sk-ant-secret",
                   openai_api_key="sk-openai-secret")

    assert cfg.save() == path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert LhConfig.load().anthropic_api_key == "sk-ant-secret"
    assert cfg.redacted()["anthropic_api_key"] == "…cret"
    assert cfg.redacted()["claude_code_oauth_token"] == "…cret"
    assert "sk-ant-secret" not in str(cfg.redacted())


def test_persisted_load_does_not_copy_environment_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("PROGRAMSMITH_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    assert LhConfig.load().anthropic_api_key == "from-env"
    assert LhConfig.load_persisted().anthropic_api_key is None
