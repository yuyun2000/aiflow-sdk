from __future__ import annotations

import base64
import json

import pytest

from aiflow_server.config import ConfigError, load_settings

AUTH_ENV = (
    "AIFLOW_CLIENT_AUTH_ENABLED",
    "AIFLOW_CLIENT_KEYS_FILE",
    "AIFLOW_CLAUDE_SUPPORTS_IMAGE_INPUT",
    "AIFLOW_CLAUDE_CONTEXT_WINDOW_TOKENS",
    "AIFLOW_CLAUDE_MAX_TURNS",
)
TLS_ENV = (
    "TLS_LOG_ENABLED",
    "TLS_ACCESS_KEY",
    "TLS_SECRET_KEY",
    "TLS_PSEUDONYM_KEY",
    "LOG_TLS_TOPIC_ID",
)
QUOTA_ENV = (
    "AIFLOW_AI_QUOTA_ENABLED",
    "AIFLOW_AI_QUOTA_BASE_URL",
    "AIFLOW_AI_QUOTA_CLIENT_ID",
    "AIFLOW_AI_QUOTA_HMAC_SECRET",
    "AIFLOW_AI_QUOTA_MODEL",
    "AIFLOW_AI_QUOTA_TIMEOUT_SECONDS",
    "AIFLOW_AI_QUOTA_MAX_ATTEMPTS",
)


def clear_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in AUTH_ENV:
        monkeypatch.delenv(name, raising=False)


def clear_tls_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in TLS_ENV:
        monkeypatch.delenv(name, raising=False)


def clear_quota_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in QUOTA_ENV:
        monkeypatch.delenv(name, raising=False)


def test_ai_quota_is_fail_closed_and_reads_secret_only_from_environment(tmp_path, monkeypatch):
    clear_quota_env(monkeypatch)
    config = tmp_path / "server.json"
    config.write_text(json.dumps({"ai_quota": {"enabled": True}}), encoding="utf-8")

    missing = load_settings(config)
    assert missing.ai_quota.enabled is True
    assert missing.ai_quota.configured is False
    assert missing.public_dict([])["api_version"] == "3.7"
    assert missing.public_dict([])["ai_quota"] == {
        "enabled": True,
        "configured": False,
        "model": "deepseek-pro",
        "authorization_mode": "server_decision_per_model_request",
    }

    monkeypatch.setenv("AIFLOW_AI_QUOTA_HMAC_SECRET", "fake-quota-secret-with-at-least-32-bytes")
    configured = load_settings(config)
    assert configured.ai_quota.configured is True
    assert "secret" not in json.dumps(configured.public_dict([])).lower()


def test_ai_quota_rejects_checked_in_secret_and_ignores_legacy_reservation_config(
    tmp_path, monkeypatch
):
    clear_quota_env(monkeypatch)
    config = tmp_path / "server.json"
    config.write_text(
        json.dumps({"ai_quota": {"hmac_secret": "must-not-be-checked-in"}}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="AIFLOW_AI_QUOTA_HMAC_SECRET"):
        load_settings(config)

    config.write_text(
        json.dumps({"ai_quota": {"requested_tokens": 500001}}),
        encoding="utf-8",
    )
    settings = load_settings(config)
    assert "requested_tokens" not in settings.public_dict([])["ai_quota"]


def test_tls_logging_requires_credentials_and_pseudonym_key(tmp_path, monkeypatch):
    clear_tls_env(monkeypatch)
    config = tmp_path / "server.json"
    config.write_text(
        json.dumps(
            {
                "telemetry": {
                    "tls_enabled": True,
                    "tls_topic_id": "fake-topic-id",
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="TLS_ACCESS_KEY.*TLS_SECRET_KEY.*TLS_PSEUDONYM_KEY"):
        load_settings(config)


def test_tls_logging_reads_secrets_only_from_environment(tmp_path, monkeypatch):
    clear_tls_env(monkeypatch)
    config = tmp_path / "server.json"
    config.write_text(
        json.dumps(
            {
                "telemetry": {
                    "tls_enabled": True,
                    "tls_topic_id": "fake-topic-id",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TLS_ACCESS_KEY", "fake-access")
    monkeypatch.setenv("TLS_SECRET_KEY", "fake-secret")
    monkeypatch.setenv("TLS_PSEUDONYM_KEY", "fake-pseudonym-key-with-at-least-32-bytes")

    settings = load_settings(config)

    assert settings.tls_logging.enabled is True
    assert settings.tls_logging.topic_id == "fake-topic-id"
    assert settings.tls_logging.access_key == "fake-access"
    assert settings.tls_logging.secret_key == "fake-secret"


def test_client_auth_enabled_requires_keys_file(tmp_path, monkeypatch):
    clear_auth_env(monkeypatch)
    config = tmp_path / "server.json"
    config.write_text(json.dumps({"client_auth": {"enabled": True}}), encoding="utf-8")

    with pytest.raises(ConfigError, match="no client keys file"):
        load_settings(config)


def test_client_auth_loads_base64url_key_file(tmp_path, monkeypatch):
    clear_auth_env(monkeypatch)
    secret = bytes(range(32))
    encoded = base64.urlsafe_b64encode(secret).decode("ascii").rstrip("=")
    keys = tmp_path / "client-keys.json"
    keys.write_text(
        json.dumps({"clients": {"official-client-v1": encoded}}),
        encoding="utf-8",
    )
    config = tmp_path / "server.json"
    config.write_text(
        json.dumps(
            {
                "client_auth": {
                    "enabled": True,
                    "keys_file": keys.name,
                }
            }
        ),
        encoding="utf-8",
    )

    settings = load_settings(config)

    assert settings.client_auth_enabled is True
    assert settings.client_auth_keys_file == keys
    assert settings.client_auth_keys == (("official-client-v1", secret),)
    assert settings.event_retention == 10000


def test_model_image_input_capability_defaults_true_and_supports_env_override(tmp_path, monkeypatch):
    clear_auth_env(monkeypatch)
    config = tmp_path / "server.json"
    config.write_text(json.dumps({"claude": {"supports_image_input": True}}), encoding="utf-8")

    assert load_settings(config).claude_supports_image_input is True

    monkeypatch.setenv("AIFLOW_CLAUDE_SUPPORTS_IMAGE_INPUT", "false")
    settings = load_settings(config)
    assert settings.claude_supports_image_input is False
    assert settings.public_dict([])["supports_image_input"] is False


def test_claude_context_and_turn_defaults_and_env_overrides(tmp_path, monkeypatch):
    clear_auth_env(monkeypatch)
    config = tmp_path / "server.json"
    config.write_text(json.dumps({"claude": {}}), encoding="utf-8")

    defaults = load_settings(config)
    assert defaults.claude_context_window_tokens == 258000
    assert defaults.claude_max_turns == 30
    assert defaults.public_dict([])["context_window_tokens"] == 258000
    assert defaults.public_dict([])["max_turns"] == 30

    monkeypatch.setenv("AIFLOW_CLAUDE_CONTEXT_WINDOW_TOKENS", "300000")
    monkeypatch.setenv("AIFLOW_CLAUDE_MAX_TURNS", "40")
    overridden = load_settings(config)
    assert overridden.claude_context_window_tokens == 300000
    assert overridden.claude_max_turns == 40


def test_default_claude_skills_include_ui_designer(tmp_path):
    config = tmp_path / "server.json"
    config.write_text("{}", encoding="utf-8")
    settings = load_settings(config)

    assert settings.enabled_skills == (
        "uiflow2-coder",
        "uiflow2-ui-designer",
        "m5stack-assistant",
        "aiflow-device-push",
    )


def test_claude_context_and_turn_reject_non_positive_values(tmp_path, monkeypatch):
    clear_auth_env(monkeypatch)
    config = tmp_path / "server.json"
    config.write_text(json.dumps({"claude": {"context_window_tokens": 0}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="claude.context_window_tokens"):
        load_settings(config)

    config.write_text(json.dumps({"claude": {"max_turns": 0}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="claude.max_turns"):
        load_settings(config)


def test_model_image_input_capability_rejects_invalid_value(tmp_path, monkeypatch):
    clear_auth_env(monkeypatch)
    config = tmp_path / "server.json"
    config.write_text(json.dumps({"claude": {"supports_image_input": "sometimes"}}), encoding="utf-8")

    with pytest.raises(ConfigError, match="claude.supports_image_input"):
        load_settings(config)
