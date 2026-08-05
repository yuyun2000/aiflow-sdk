from __future__ import annotations

import base64
import json

import pytest

from aiflow_server.config import ConfigError, load_settings


AUTH_ENV = (
    "AIFLOW_CLIENT_AUTH_ENABLED",
    "AIFLOW_CLIENT_KEYS_FILE",
    "AIFLOW_CLAUDE_SUPPORTS_IMAGE_INPUT",
)


def clear_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in AUTH_ENV:
        monkeypatch.delenv(name, raising=False)


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


def test_model_image_input_capability_rejects_invalid_value(tmp_path, monkeypatch):
    clear_auth_env(monkeypatch)
    config = tmp_path / "server.json"
    config.write_text(json.dumps({"claude": {"supports_image_input": "sometimes"}}), encoding="utf-8")

    with pytest.raises(ConfigError, match="claude.supports_image_input"):
        load_settings(config)
