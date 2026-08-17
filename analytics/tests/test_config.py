from __future__ import annotations

import pytest

from aiflow_analytics.config import Settings


def test_model_pricing_json_is_loaded_by_exact_model_name(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AIFLOW_ANALYTICS_AUTH_DISABLED", "true")
    monkeypatch.setenv("AIFLOW_ANALYTICS_MODEL_PRICING_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv(
        "AIFLOW_ANALYTICS_MODEL_PRICING_JSON",
        '{"deepseek-v4-flash-ga-260731":{"input":0.27,"output":1.1}}',
    )
    settings = Settings.from_env(tmp_path / "missing.env")
    assert settings.model_pricing == {
        "deepseek-v4-flash-ga-260731": {"input": 0.27, "output": 1.1}
    }


def test_model_pricing_json_rejects_invalid_price(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AIFLOW_ANALYTICS_AUTH_DISABLED", "true")
    monkeypatch.setenv("AIFLOW_ANALYTICS_MODEL_PRICING_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv(
        "AIFLOW_ANALYTICS_MODEL_PRICING_JSON",
        '{"model-a":{"input":-1}}',
    )
    with pytest.raises(ValueError, match="finite and non-negative"):
        Settings.from_env(tmp_path / "missing.env")


def test_model_pricing_file_takes_precedence(monkeypatch, tmp_path) -> None:
    pricing_file = tmp_path / "model_pricing.json"
    pricing_file.write_text(
        '{"deepseek-v4-flash-ga-260731":{"input":0.27}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("AIFLOW_ANALYTICS_AUTH_DISABLED", "true")
    monkeypatch.setenv("AIFLOW_ANALYTICS_MODEL_PRICING_FILE", str(pricing_file))
    monkeypatch.setenv(
        "AIFLOW_ANALYTICS_MODEL_PRICING_JSON",
        '{"other-model":{"input":99}}',
    )
    settings = Settings.from_env(tmp_path / "missing.env")
    assert settings.model_pricing == {"deepseek-v4-flash-ga-260731": {"input": 0.27}}
    assert settings.model_pricing_file == pricing_file


def test_explicit_missing_model_pricing_file_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AIFLOW_ANALYTICS_AUTH_DISABLED", "true")
    monkeypatch.setenv("AIFLOW_ANALYTICS_MODEL_PRICING_FILE", str(tmp_path / "missing.json"))
    with pytest.raises(ValueError, match="does not exist"):
        Settings.from_env(tmp_path / "missing.env")
