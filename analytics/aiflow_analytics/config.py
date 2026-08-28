from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


def _env_float(name: str, default: float | None) -> float | None:
    raw = os.getenv(name)
    return float(raw) if raw and raw.strip() else default


def _parse_model_pricing(value: object, source: str) -> dict[str, dict[str, float]]:
    if not isinstance(value, dict):
        raise ValueError(f"{source} must be an object")
    result: dict[str, dict[str, float]] = {}
    for model, prices in value.items():
        if not isinstance(model, str) or not model.strip():
            raise ValueError(f"model pricing keys in {source} must be non-empty model names")
        if not isinstance(prices, dict):
            raise ValueError(f"pricing for model {model!r} in {source} must be an object")
        normalized: dict[str, float] = {}
        for key in ("input", "output", "cache_read", "cache_creation"):
            if key not in prices or prices[key] is None:
                continue
            price = prices[key]
            if isinstance(price, bool) or not isinstance(price, (int, float)):
                raise ValueError(f"pricing {model!r}.{key} in {source} must be a number")
            if not math.isfinite(float(price)) or float(price) < 0:
                raise ValueError(
                    f"pricing {model!r}.{key} in {source} must be finite and non-negative"
                )
            normalized[key] = float(price)
        result[model.strip()] = normalized
    return result


def _read_model_pricing_file(path: Path) -> dict[str, dict[str, float]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"unable to read model pricing file {path}: {exc}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"model pricing file {path} must contain valid JSON") from exc
    return _parse_model_pricing(value, f"model pricing file {path}")


def _env_model_pricing() -> dict[str, dict[str, float]]:
    raw = os.getenv("AIFLOW_ANALYTICS_MODEL_PRICING_JSON", "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "AIFLOW_ANALYTICS_MODEL_PRICING_JSON must be valid JSON"
        ) from exc
    return _parse_model_pricing(value, "AIFLOW_ANALYTICS_MODEL_PRICING_JSON")


def _resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


@dataclass(frozen=True, slots=True)
class Settings:
    host: str
    port: int
    timezone: str
    data_dir: Path
    log_level: str
    tls_region: str
    tls_endpoint: str
    tls_topic_id: str
    tls_access_key: str
    tls_secret_key: str
    tls_query: str
    tls_schema_version: int
    tls_page_size: int
    tls_max_pages: int
    tls_timeout_seconds: int
    analytics_start_date: str
    sync_on_startup: bool
    sync_interval_seconds: int
    sync_overlap_minutes: int
    auth_disabled: bool
    api_token: str
    default_range_days: int
    model_pricing: dict[str, dict[str, float]] = field(default_factory=dict)
    model_pricing_file: Path | None = None
    # Deprecated global values remain readable so existing deployments can migrate.
    input_price_usd_per_million: float | None = None
    output_price_usd_per_million: float | None = None
    cache_read_price_usd_per_million: float | None = None
    cache_creation_price_usd_per_million: float | None = None

    @property
    def database_path(self) -> Path:
        return self.data_dir / "analytics.sqlite3"

    @property
    def tls_configured(self) -> bool:
        return bool(self.tls_topic_id and self.tls_access_key and self.tls_secret_key)

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> Settings:
        load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
        pricing_file_raw = os.getenv("AIFLOW_ANALYTICS_MODEL_PRICING_FILE", "").strip()
        pricing_file = _resolve_path(pricing_file_raw or "./model_pricing.json")
        if pricing_file.exists():
            model_pricing = _read_model_pricing_file(pricing_file)
        else:
            model_pricing = _env_model_pricing()
            if not model_pricing and pricing_file_raw:
                raise ValueError(f"model pricing file does not exist: {pricing_file}")
        settings = cls(
            host=os.getenv("AIFLOW_ANALYTICS_HOST", "0.0.0.0"),
            port=_env_int("AIFLOW_ANALYTICS_PORT", 5090),
            timezone=os.getenv("AIFLOW_ANALYTICS_TIMEZONE", "Asia/Shanghai"),
            data_dir=_resolve_path(os.getenv("AIFLOW_ANALYTICS_DATA_DIR", "./data")),
            log_level=os.getenv("AIFLOW_ANALYTICS_LOG_LEVEL", "INFO").upper(),
            tls_region=os.getenv("TLS_REGION", "cn-beijing"),
            tls_endpoint=os.getenv("TLS_ENDPOINT", "tls-cn-beijing.volces.com"),
            tls_topic_id=os.getenv(
                "LOG_TLS_TOPIC_ID",
                "d66e9a86-bbd5-419b-91db-d5aef9a4a42a",
            ),
            tls_access_key=os.getenv("TLS_ACCESS_KEY", ""),
            tls_secret_key=os.getenv("TLS_SECRET_KEY", ""),
            tls_query=os.getenv(
                "AIFLOW_ANALYTICS_TLS_QUERY",
                "event:aiflow_conversation_trace",
            ),
            tls_schema_version=_env_int("TLS_LOG_SCHEMA_VERSION", 2),
            # SearchLogsV2 in the Volcengine Python SDK accepts at most 100 logs per page.
            tls_page_size=min(_env_int("AIFLOW_ANALYTICS_TLS_PAGE_SIZE", 100), 100),
            tls_max_pages=_env_int("AIFLOW_ANALYTICS_TLS_MAX_PAGES", 10000),
            tls_timeout_seconds=_env_int("AIFLOW_ANALYTICS_TLS_TIMEOUT_SECONDS", 10),
            analytics_start_date=os.getenv("AIFLOW_ANALYTICS_START_DATE", "2026-08-01"),
            sync_on_startup=_env_bool("AIFLOW_ANALYTICS_SYNC_ON_STARTUP", True),
            sync_interval_seconds=_env_int("AIFLOW_ANALYTICS_SYNC_INTERVAL_SECONDS", 60),
            sync_overlap_minutes=_env_int("AIFLOW_ANALYTICS_SYNC_OVERLAP_MINUTES", 15),
            auth_disabled=_env_bool("AIFLOW_ANALYTICS_AUTH_DISABLED", False),
            api_token=os.getenv("AIFLOW_ANALYTICS_API_TOKEN", ""),
            default_range_days=_env_int("AIFLOW_ANALYTICS_DEFAULT_RANGE_DAYS", 7),
            model_pricing=model_pricing,
            model_pricing_file=pricing_file if pricing_file.exists() else None,
            input_price_usd_per_million=_env_float(
                "AIFLOW_ANALYTICS_INPUT_PRICE_USD_PER_MILLION", None
            ),
            output_price_usd_per_million=_env_float(
                "AIFLOW_ANALYTICS_OUTPUT_PRICE_USD_PER_MILLION", None
            ),
            cache_read_price_usd_per_million=_env_float(
                "AIFLOW_ANALYTICS_CACHE_READ_PRICE_USD_PER_MILLION", None
            ),
            cache_creation_price_usd_per_million=_env_float(
                "AIFLOW_ANALYTICS_CACHE_CREATION_PRICE_USD_PER_MILLION", None
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Invalid AIFLOW_ANALYTICS_TIMEZONE: {self.timezone}") from exc
        try:
            date.fromisoformat(self.analytics_start_date)
        except ValueError as exc:
            raise ValueError("AIFLOW_ANALYTICS_START_DATE must use YYYY-MM-DD") from exc
        if not 1 <= self.port <= 65535:
            raise ValueError("AIFLOW_ANALYTICS_PORT must be between 1 and 65535")
        if self.tls_schema_version < 1:
            raise ValueError("TLS_LOG_SCHEMA_VERSION must be positive")
        if not 1 <= self.tls_page_size <= 100:
            raise ValueError("AIFLOW_ANALYTICS_TLS_PAGE_SIZE must be between 1 and 100")
        if self.tls_max_pages < 1:
            raise ValueError("AIFLOW_ANALYTICS_TLS_MAX_PAGES must be positive")
        if self.tls_timeout_seconds < 1:
            raise ValueError("AIFLOW_ANALYTICS_TLS_TIMEOUT_SECONDS must be positive")
        if self.sync_interval_seconds < 10:
            raise ValueError("AIFLOW_ANALYTICS_SYNC_INTERVAL_SECONDS must be at least 10")
        if self.sync_overlap_minutes < 1:
            raise ValueError("AIFLOW_ANALYTICS_SYNC_OVERLAP_MINUTES must be positive")
        if self.default_range_days < 1:
            raise ValueError("AIFLOW_ANALYTICS_DEFAULT_RANGE_DAYS must be positive")
        for name, value in (
            ("AIFLOW_ANALYTICS_INPUT_PRICE_USD_PER_MILLION", self.input_price_usd_per_million),
            ("AIFLOW_ANALYTICS_OUTPUT_PRICE_USD_PER_MILLION", self.output_price_usd_per_million),
            (
                "AIFLOW_ANALYTICS_CACHE_READ_PRICE_USD_PER_MILLION",
                self.cache_read_price_usd_per_million,
            ),
            (
                "AIFLOW_ANALYTICS_CACHE_CREATION_PRICE_USD_PER_MILLION",
                self.cache_creation_price_usd_per_million,
            ),
        ):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} must not be negative")
        if not self.auth_disabled and len(self.api_token) < 24:
            raise ValueError(
                "AIFLOW_ANALYTICS_API_TOKEN must contain at least 24 characters "
                "when auth is enabled"
            )


def settings_summary(settings: Settings) -> dict[str, object]:
    return {
        "listen": f"{settings.host}:{settings.port}",
        "timezone": settings.timezone,
        "database": str(settings.database_path),
        "tls_configured": settings.tls_configured,
        "tls_endpoint": settings.tls_endpoint,
        "tls_region": settings.tls_region,
        "tls_topic_configured": bool(settings.tls_topic_id),
        "tls_schema_version": settings.tls_schema_version,
        "tls_page_size": settings.tls_page_size,
        "tls_max_pages": settings.tls_max_pages,
        "sync_on_startup": settings.sync_on_startup,
        "sync_interval_seconds": settings.sync_interval_seconds,
        "sync_overlap_minutes": settings.sync_overlap_minutes,
        "pricing_models": sorted(settings.model_pricing),
        "pricing_file": str(settings.model_pricing_file)
        if settings.model_pricing_file
        else None,
        "legacy_global_pricing_configured": any(
            value is not None
            for value in (
                settings.input_price_usd_per_million,
                settings.output_price_usd_per_million,
                settings.cache_read_price_usd_per_million,
                settings.cache_creation_price_usd_per_million,
            )
        ),
        "auth": "disabled" if settings.auth_disabled else "bearer",
    }
