from __future__ import annotations

import json
import os
import base64
import binascii
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .asr import AsrSettings, DEFAULT_URL, DEFAULT_RESOURCE_ID


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "server_config.json"


class ConfigError(ValueError):
    pass


def _nested(data: dict[str, Any], section: str, key: str, default: Any) -> Any:
    value = data.get(section, {})
    if not isinstance(value, dict):
        raise ConfigError(f"config section {section!r} must be an object")
    return value.get(key, default)


def _path(value: str | Path, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _optional_float(value: Any, name: str) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} must be greater than zero")
    return parsed


def _non_negative_float(value: Any, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if parsed < 0:
        raise ConfigError(f"{name} must be zero or greater")
    return parsed


def _positive_int(value: Any, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} must be greater than zero")
    return parsed


def _boolean(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def _decode_client_secret(value: Any, key_id: str) -> bytes:
    if not isinstance(value, str):
        raise ConfigError(f"client_auth key {key_id!r} must be a base64url string")
    try:
        secret = base64.urlsafe_b64decode(value.strip() + "=" * (-len(value.strip()) % 4))
    except (ValueError, binascii.Error) as exc:
        raise ConfigError(f"client_auth key {key_id!r} is not valid base64url") from exc
    if len(secret) < 32:
        raise ConfigError(f"client_auth key {key_id!r} must decode to at least 32 bytes")
    return secret


def _load_client_keys(path: Path) -> tuple[tuple[str, bytes], ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read client_auth keys file: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError("client_auth keys file must contain a JSON object")
    values = payload.get("clients", payload)
    if not isinstance(values, dict) or not values:
        raise ConfigError("client_auth keys file must contain at least one client key")
    keys: list[tuple[str, bytes]] = []
    for raw_key_id, raw_secret in values.items():
        key_id = str(raw_key_id).strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", key_id):
            raise ConfigError("client_auth key IDs may contain only letters, digits, dot, underscore, and hyphen")
        keys.append((key_id, _decode_client_secret(raw_secret, key_id)))
    return tuple(keys)


def _normalize_base_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ConfigError("device_push.base_url must be an absolute HTTP(S) URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError("device_push.base_url must not include credentials, query, or fragment")
    return value.rstrip("/")


@dataclass(frozen=True)
class TlsLoggingSettings:
    enabled: bool
    schema_version: int
    endpoint: str
    region: str
    access_key: str
    secret_key: str
    topic_id: str
    source: str
    filename: str
    pseudonym_key: str
    batch_size: int
    batch_wait_seconds: float
    upload_timeout_seconds: int
    shutdown_timeout_seconds: float
    retry_base_seconds: float
    retry_max_seconds: float
    max_payload_bytes: int


@dataclass(frozen=True)
class Settings:
    config_path: Path
    root_dir: Path
    host: str
    port: int
    cors_origins: tuple[str, ...]
    data_dir: Path
    skills_dir: Path
    claude_model: str | None
    claude_fallback_model: str | None
    claude_supports_image_input: bool
    claude_context_window_tokens: int
    claude_max_turns: int
    claude_max_budget_usd: float | None
    claude_effort: str | None
    claude_permission_mode: str
    claude_sandbox_enabled: bool
    claude_allowed_tools: tuple[str, ...]
    enabled_skills: tuple[str, ...]
    m5stack_mcp_enabled: bool
    m5stack_mcp_url: str
    device_push_base_url: str
    device_push_timeout: float
    max_sessions: int
    session_active_window_seconds: int
    max_concurrent_tasks: int
    max_queued_tasks: int
    heartbeat_seconds: int
    agent_stall_seconds: int
    event_retention: int
    max_upload_bytes: int
    max_attachments: int
    max_attachment_bytes: int
    max_attachment_total_bytes: int
    client_auth_enabled: bool
    client_auth_keys_file: Path | None
    client_auth_keys: tuple[tuple[str, bytes], ...]
    client_auth_clock_skew_seconds: int
    client_auth_nonce_ttl_seconds: int
    client_auth_requests_per_minute: int
    max_ai_tasks_per_client_minute: int
    max_ai_tasks_per_client_day: int
    max_ai_tasks_global_day: int
    web_require_same_origin: bool
    web_cookie_secure: bool
    web_trusted_proxy_ips: tuple[str, ...]
    web_requests_per_session_minute: int
    web_requests_per_ip_minute: int
    web_ai_tasks_per_session_minute: int
    web_ai_tasks_per_session_day: int
    web_ai_tasks_per_ip_day: int
    tls_logging: TlsLoggingSettings
    asr: AsrSettings

    @property
    def database_path(self) -> Path:
        return self.data_dir / "aiflow.sqlite3"

    @property
    def clients_dir(self) -> Path:
        return self.data_dir / "clients"

    def public_dict(self, available_skills: list[str]) -> dict[str, Any]:
        return {
            "api_version": "3.4",
            "agent": "claude-code",
            "model": self.claude_model or "claude-code-default",
            "fallback_model": self.claude_fallback_model,
            "supports_image_input": self.claude_supports_image_input,
            "context_window_tokens": self.claude_context_window_tokens,
            "max_turns": self.claude_max_turns,
            "enabled_skills": list(self.enabled_skills),
            "available_skills": available_skills,
            "task_heartbeat_seconds": self.heartbeat_seconds,
            "agent_stall_seconds": self.agent_stall_seconds,
            "max_upload_bytes": self.max_upload_bytes,
            "max_sessions": self.max_sessions,
            "session_active_window_seconds": self.session_active_window_seconds,
            "max_concurrent_tasks": self.max_concurrent_tasks,
            "max_queued_tasks": self.max_queued_tasks,
            "max_attachments": self.max_attachments,
            "max_attachment_bytes": self.max_attachment_bytes,
            "max_attachment_total_bytes": self.max_attachment_total_bytes,
            "asr": {
                "enabled": self.asr.enabled,
                "configured": self.asr.auth_configured,
                "resource_id": self.asr.resource_id,
                "url": self.asr.url,
            },
            "client_auth": {
                "enabled": self.client_auth_enabled,
                "scheme": "AIFLOW-HMAC-SHA256-V1" if self.client_auth_enabled else None,
                "response_authentication": self.client_auth_enabled,
            },
            "cost_guard": {
                "max_ai_tasks_per_client_minute": self.max_ai_tasks_per_client_minute,
                "max_ai_tasks_per_client_day": self.max_ai_tasks_per_client_day,
                "max_ai_tasks_global_day": self.max_ai_tasks_global_day,
            },
            "web_gateway": {
                "anonymous": True,
                "require_same_origin": self.web_require_same_origin,
                "requests_per_session_minute": self.web_requests_per_session_minute,
                "ai_tasks_per_session_minute": self.web_ai_tasks_per_session_minute,
                "ai_tasks_per_session_day": self.web_ai_tasks_per_session_day,
            },
        }


def load_settings(path: str | Path | None = None) -> Settings:
    raw_path = path or os.environ.get("AIFLOW_SERVER_CONFIG") or DEFAULT_CONFIG_PATH
    config_path = Path(raw_path).expanduser().resolve()
    data: dict[str, Any] = {}
    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"cannot read config: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError("config root must be an object")
        data = loaded
    base = config_path.parent

    host = os.environ.get("AIFLOW_HOST") or str(_nested(data, "server", "host", "0.0.0.0"))
    port = _positive_int(os.environ.get("AIFLOW_PORT") or _nested(data, "server", "port", 8880), "server.port")
    cors = _nested(data, "server", "cors_origins", [])
    if not isinstance(cors, list) or not all(isinstance(item, str) for item in cors):
        raise ConfigError("server.cors_origins must be an array of strings")

    tools = _nested(data, "claude", "allowed_tools", ["Read", "Write", "Edit", "Glob", "Grep", "Bash"])
    skills = _nested(data, "claude", "skills", ["uiflow2-coder", "m5stack-assistant", "aiflow-device-push"])
    if not isinstance(tools, list) or not all(isinstance(item, str) for item in tools):
        raise ConfigError("claude.allowed_tools must be an array of strings")
    if not isinstance(skills, list) or not all(isinstance(item, str) for item in skills):
        raise ConfigError("claude.skills must be an array of strings")
    trusted_proxy_ips = _nested(data, "web_gateway", "trusted_proxy_ips", ["127.0.0.1", "::1"])
    if not isinstance(trusted_proxy_ips, list) or not all(isinstance(item, str) for item in trusted_proxy_ips):
        raise ConfigError("web_gateway.trusted_proxy_ips must be an array of strings")

    model = os.environ.get("AIFLOW_CLAUDE_MODEL") or _nested(data, "claude", "model", None)
    fallback_model = os.environ.get("AIFLOW_CLAUDE_FALLBACK_MODEL") or _nested(data, "claude", "fallback_model", None)
    effort = _nested(data, "claude", "effort", "high")
    if effort not in {None, "low", "medium", "high", "xhigh", "max"}:
        raise ConfigError("claude.effort must be low, medium, high, xhigh, max, or null")
    max_attachment_bytes = _positive_int(
        _nested(data, "messages", "max_attachment_bytes", 10 * 1024 * 1024),
        "messages.max_attachment_bytes",
    )
    max_attachment_total_bytes = _positive_int(
        _nested(data, "messages", "max_total_bytes", 20 * 1024 * 1024),
        "messages.max_total_bytes",
    )
    if max_attachment_total_bytes < max_attachment_bytes:
        raise ConfigError("messages.max_total_bytes must be at least max_attachment_bytes")

    client_auth_enabled = _boolean(
        os.environ.get("AIFLOW_CLIENT_AUTH_ENABLED")
        or _nested(data, "client_auth", "enabled", False),
        "client_auth.enabled",
    )
    raw_keys_file = os.environ.get("AIFLOW_CLIENT_KEYS_FILE") or _nested(data, "client_auth", "keys_file", None)
    client_auth_keys_file = _path(str(raw_keys_file), base) if raw_keys_file else None
    client_auth_keys = _load_client_keys(client_auth_keys_file) if client_auth_keys_file else ()
    if client_auth_enabled and not client_auth_keys:
        raise ConfigError("client_auth is enabled but no client keys file is configured")

    tls_enabled = _boolean(
        os.environ.get("TLS_LOG_ENABLED")
        or _nested(data, "telemetry", "tls_enabled", False),
        "telemetry.tls_enabled",
    )
    tls_endpoint = str(
        os.environ.get("TLS_ENDPOINT")
        or _nested(data, "telemetry", "tls_endpoint", "tls-cn-beijing.volces.com")
    ).strip()
    tls_region = str(
        os.environ.get("TLS_REGION")
        or _nested(data, "telemetry", "tls_region", "cn-beijing")
    ).strip()
    tls_topic_id = str(
        os.environ.get("LOG_TLS_TOPIC_ID")
        or _nested(data, "telemetry", "tls_topic_id", "")
    ).strip()
    tls_access_key = os.environ.get("TLS_ACCESS_KEY", "").strip()
    tls_secret_key = os.environ.get("TLS_SECRET_KEY", "").strip()
    tls_pseudonym_key = os.environ.get("TLS_PSEUDONYM_KEY", "").strip()
    if tls_enabled:
        missing = [
            name
            for name, value in (
                ("TLS_ENDPOINT", tls_endpoint),
                ("TLS_REGION", tls_region),
                ("TLS_ACCESS_KEY", tls_access_key),
                ("TLS_SECRET_KEY", tls_secret_key),
                ("LOG_TLS_TOPIC_ID", tls_topic_id),
                ("TLS_PSEUDONYM_KEY", tls_pseudonym_key),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "TLS logging is enabled but required settings are missing: "
                + ", ".join(missing)
            )
        if len(tls_pseudonym_key.encode("utf-8")) < 32:
            raise ConfigError("TLS_PSEUDONYM_KEY must be at least 32 bytes")

    retry_base_seconds = _non_negative_float(
        os.environ.get("TLS_LOG_RETRY_BASE_SECONDS")
        or _nested(data, "telemetry", "retry_base_seconds", 1),
        "telemetry.retry_base_seconds",
    )
    retry_max_seconds = _non_negative_float(
        os.environ.get("TLS_LOG_RETRY_MAX_SECONDS")
        or _nested(data, "telemetry", "retry_max_seconds", 300),
        "telemetry.retry_max_seconds",
    )
    if retry_max_seconds < retry_base_seconds:
        raise ConfigError("telemetry.retry_max_seconds must be at least retry_base_seconds")

    asr_url = str(os.environ.get("AIFLOW_ASR_URL") or _nested(data, "asr", "url", DEFAULT_URL)).strip()
    if not asr_url.startswith("wss://"):
        raise ConfigError("asr.url must be a wss:// URL")
    asr_enabled = _boolean(
        os.environ.get("AIFLOW_ASR_ENABLED") or _nested(data, "asr", "enabled", False),
        "asr.enabled",
    )
    asr_timeout = _non_negative_float(
        os.environ.get("AIFLOW_ASR_TIMEOUT_SECONDS") or _nested(data, "asr", "timeout_seconds", 30),
        "asr.timeout_seconds",
    )
    if asr_timeout <= 0:
        raise ConfigError("asr.timeout_seconds must be greater than zero")
    asr_segment_duration = _positive_int(
        os.environ.get("AIFLOW_ASR_SEGMENT_DURATION_MS") or _nested(data, "asr", "segment_duration_ms", 200),
        "asr.segment_duration_ms",
    )

    return Settings(
        config_path=config_path,
        root_dir=ROOT_DIR,
        host=host,
        port=port,
        cors_origins=tuple(cors),
        data_dir=_path(os.environ.get("AIFLOW_DATA_DIR") or _nested(data, "storage", "data_dir", "./projects_data_v3"), base),
        skills_dir=_path(os.environ.get("AIFLOW_SKILLS_DIR") or _nested(data, "skills", "directory", "./skills"), base),
        claude_model=str(model) if model else None,
        claude_fallback_model=str(fallback_model) if fallback_model else None,
        claude_supports_image_input=_boolean(
            os.environ.get("AIFLOW_CLAUDE_SUPPORTS_IMAGE_INPUT")
            or _nested(data, "claude", "supports_image_input", True),
            "claude.supports_image_input",
        ),
        claude_context_window_tokens=_positive_int(
            os.environ.get("AIFLOW_CLAUDE_CONTEXT_WINDOW_TOKENS")
            or _nested(data, "claude", "context_window_tokens", 258000),
            "claude.context_window_tokens",
        ),
        claude_max_turns=_positive_int(
            os.environ.get("AIFLOW_CLAUDE_MAX_TURNS") or _nested(data, "claude", "max_turns", 30),
            "claude.max_turns",
        ),
        claude_max_budget_usd=_optional_float(_nested(data, "claude", "max_budget_usd", None), "claude.max_budget_usd"),
        claude_effort=effort,
        claude_permission_mode=str(_nested(data, "claude", "permission_mode", "dontAsk")),
        claude_sandbox_enabled=bool(_nested(data, "claude", "sandbox_enabled", True)),
        claude_allowed_tools=tuple(tools),
        enabled_skills=tuple(skills),
        m5stack_mcp_enabled=bool(_nested(data, "mcp", "m5stack_enabled", True)),
        m5stack_mcp_url=str(_nested(data, "mcp", "m5stack_url", "https://mcp.m5stack.com/sse")),
        device_push_base_url=_normalize_base_url(
            str(_nested(data, "device_push", "base_url", "https://uiflow2.m5stack.com/m5stack/"))
        ),
        device_push_timeout=float(_nested(data, "device_push", "timeout_seconds", 120)),
        max_sessions=_positive_int(
            os.environ.get("AIFLOW_MAX_SESSIONS") or _nested(data, "capacity", "max_sessions", 100),
            "capacity.max_sessions",
        ),
        session_active_window_seconds=_positive_int(
            _nested(data, "capacity", "session_active_window_seconds", 60),
            "capacity.session_active_window_seconds",
        ),
        max_concurrent_tasks=_positive_int(
            os.environ.get("AIFLOW_MAX_CONCURRENT_TASKS")
            or _nested(data, "capacity", "max_concurrent_tasks", 4),
            "capacity.max_concurrent_tasks",
        ),
        max_queued_tasks=_positive_int(
            os.environ.get("AIFLOW_MAX_QUEUED_TASKS")
            or _nested(data, "capacity", "max_queued_tasks", 20),
            "capacity.max_queued_tasks",
        ),
        heartbeat_seconds=_positive_int(_nested(data, "tasks", "heartbeat_seconds", 10), "tasks.heartbeat_seconds"),
        agent_stall_seconds=_positive_int(_nested(data, "tasks", "agent_stall_seconds", 120), "tasks.agent_stall_seconds"),
        event_retention=_positive_int(_nested(data, "tasks", "event_retention", 10000), "tasks.event_retention"),
        max_upload_bytes=_positive_int(_nested(data, "uploads", "max_bytes", 10 * 1024 * 1024), "uploads.max_bytes"),
        max_attachments=_positive_int(_nested(data, "messages", "max_attachments", 6), "messages.max_attachments"),
        max_attachment_bytes=max_attachment_bytes,
        max_attachment_total_bytes=max_attachment_total_bytes,
        client_auth_enabled=client_auth_enabled,
        client_auth_keys_file=client_auth_keys_file,
        client_auth_keys=client_auth_keys,
        client_auth_clock_skew_seconds=_positive_int(
            _nested(data, "client_auth", "clock_skew_seconds", 60),
            "client_auth.clock_skew_seconds",
        ),
        client_auth_nonce_ttl_seconds=_positive_int(
            _nested(data, "client_auth", "nonce_ttl_seconds", 300),
            "client_auth.nonce_ttl_seconds",
        ),
        client_auth_requests_per_minute=_positive_int(
            _nested(data, "client_auth", "max_requests_per_minute", 120),
            "client_auth.max_requests_per_minute",
        ),
        max_ai_tasks_per_client_minute=_positive_int(
            _nested(data, "cost_guard", "max_ai_tasks_per_client_minute", 10),
            "cost_guard.max_ai_tasks_per_client_minute",
        ),
        max_ai_tasks_per_client_day=_positive_int(
            _nested(data, "cost_guard", "max_ai_tasks_per_client_day", 200),
            "cost_guard.max_ai_tasks_per_client_day",
        ),
        max_ai_tasks_global_day=_positive_int(
            _nested(data, "cost_guard", "max_ai_tasks_global_day", 1000),
            "cost_guard.max_ai_tasks_global_day",
        ),
        web_require_same_origin=_boolean(
            os.environ.get("AIFLOW_WEB_REQUIRE_SAME_ORIGIN")
            or _nested(data, "web_gateway", "require_same_origin", True),
            "web_gateway.require_same_origin",
        ),
        web_cookie_secure=_boolean(
            os.environ.get("AIFLOW_WEB_COOKIE_SECURE")
            or _nested(data, "web_gateway", "cookie_secure", False),
            "web_gateway.cookie_secure",
        ),
        web_trusted_proxy_ips=tuple(trusted_proxy_ips),
        web_requests_per_session_minute=_positive_int(
            _nested(data, "web_gateway", "max_requests_per_session_minute", 120),
            "web_gateway.max_requests_per_session_minute",
        ),
        web_requests_per_ip_minute=_positive_int(
            _nested(data, "web_gateway", "max_requests_per_ip_minute", 300),
            "web_gateway.max_requests_per_ip_minute",
        ),
        web_ai_tasks_per_session_minute=_positive_int(
            _nested(data, "web_gateway", "max_ai_tasks_per_session_minute", 3),
            "web_gateway.max_ai_tasks_per_session_minute",
        ),
        web_ai_tasks_per_session_day=_positive_int(
            _nested(data, "web_gateway", "max_ai_tasks_per_session_day", 20),
            "web_gateway.max_ai_tasks_per_session_day",
        ),
        web_ai_tasks_per_ip_day=_positive_int(
            _nested(data, "web_gateway", "max_ai_tasks_per_ip_day", 100),
            "web_gateway.max_ai_tasks_per_ip_day",
        ),
        tls_logging=TlsLoggingSettings(
            enabled=tls_enabled,
            schema_version=_positive_int(
                os.environ.get("TLS_LOG_SCHEMA_VERSION")
                or _nested(data, "telemetry", "schema_version", 2),
                "telemetry.schema_version",
            ),
            endpoint=tls_endpoint,
            region=tls_region,
            access_key=tls_access_key,
            secret_key=tls_secret_key,
            topic_id=tls_topic_id,
            source=str(_nested(data, "telemetry", "source", "aiflow-conversation")),
            filename=str(_nested(data, "telemetry", "filename", "conversation-trace.log")),
            pseudonym_key=tls_pseudonym_key,
            batch_size=_positive_int(
                os.environ.get("TLS_LOG_BATCH_SIZE")
                or _nested(data, "telemetry", "batch_size", 20),
                "telemetry.batch_size",
            ),
            batch_wait_seconds=_non_negative_float(
                os.environ.get("TLS_LOG_BATCH_WAIT_SECONDS")
                or _nested(data, "telemetry", "batch_wait_seconds", 0.05),
                "telemetry.batch_wait_seconds",
            ),
            upload_timeout_seconds=_positive_int(
                os.environ.get("TLS_UPLOAD_TIMEOUT_SECONDS")
                or _nested(data, "telemetry", "upload_timeout_seconds", 5),
                "telemetry.upload_timeout_seconds",
            ),
            shutdown_timeout_seconds=_non_negative_float(
                os.environ.get("TLS_LOG_SHUTDOWN_TIMEOUT_SECONDS")
                or _nested(data, "telemetry", "shutdown_timeout_seconds", 2),
                "telemetry.shutdown_timeout_seconds",
            ),
            retry_base_seconds=retry_base_seconds,
            retry_max_seconds=retry_max_seconds,
            max_payload_bytes=_positive_int(
                os.environ.get("TLS_LOG_MAX_PAYLOAD_BYTES")
                or _nested(data, "telemetry", "max_payload_bytes", 131072),
                "telemetry.max_payload_bytes",
            ),
        ),
        asr=AsrSettings(
            enabled=asr_enabled,
            url=asr_url,
            api_key=os.environ.get("AIFLOW_ASR_API_KEY", "").strip(),
            app_key=os.environ.get("AIFLOW_ASR_APP_KEY", "").strip(),
            access_key=os.environ.get("AIFLOW_ASR_ACCESS_KEY", "").strip(),
            resource_id=str(os.environ.get("AIFLOW_ASR_RESOURCE_ID") or _nested(data, "asr", "resource_id", DEFAULT_RESOURCE_ID)).strip(),
            timeout_seconds=asr_timeout,
            segment_duration_ms=asr_segment_duration,
        ),
    )
