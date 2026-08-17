from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

TRACE_EVENT_NAME = "aiflow_conversation_trace"


def safe_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _event_time_ms(log: dict[str, Any]) -> int:
    direct = safe_int(log.get("event_time_unix_ms"))
    if direct is not None:
        return direct
    raw = str(log.get("event_time") or "")
    if raw:
        try:
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            pass
    fallback = safe_int(log.get("__time__"))
    return (fallback or 0) * 1000


@dataclass(frozen=True, slots=True)
class ParsedRecord:
    record_id: str
    event_id: str
    schema_version: int
    project_id: str
    conversation_id: str
    turn_id: str
    turn_index: int
    turn_kind: str
    event_sequence: int
    event_type: str
    event_time_ms: int
    is_terminal: bool
    chunk_index: int
    chunk_count: int
    payload_chunk: str
    raw_json: str


def parse_record(log: dict[str, Any], expected_schema_version: int) -> ParsedRecord | None:
    if str(log.get("event") or "") != TRACE_EVENT_NAME:
        return None
    schema_version = safe_int(log.get("schema_version"))
    if schema_version != expected_schema_version:
        raise ValueError(f"unsupported schema_version={schema_version}")

    required = {
        name: str(log.get(name) or "")
        for name in (
            "record_id",
            "event_id",
            "project_id",
            "conversation_id",
            "turn_id",
            "turn_kind",
            "event_type",
        )
    }
    missing = sorted(name for name, value in required.items() if not value)
    if missing:
        raise ValueError(f"missing envelope fields: {', '.join(missing)}")

    turn_index = safe_int(log.get("turn_index"))
    event_sequence = safe_int(log.get("event_sequence"))
    chunk_index = safe_int(log.get("chunk_index"))
    chunk_count = safe_int(log.get("chunk_count"))
    if turn_index is None or turn_index < 1:
        raise ValueError("turn_index must be positive")
    if event_sequence is None or event_sequence < 0:
        raise ValueError("event_sequence must be non-negative")
    if chunk_index is None or chunk_count is None or chunk_count < 1:
        raise ValueError("invalid chunk metadata")
    if chunk_index < 0 or chunk_index >= chunk_count:
        raise ValueError("chunk_index is outside chunk_count")
    payload = log.get("payload")
    if payload is None:
        raise ValueError("payload is missing")
    payload_chunk = payload if isinstance(payload, str) else json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    return ParsedRecord(
        record_id=required["record_id"],
        event_id=required["event_id"],
        schema_version=schema_version,
        project_id=required["project_id"],
        conversation_id=required["conversation_id"],
        turn_id=required["turn_id"],
        turn_index=turn_index,
        turn_kind=required["turn_kind"],
        event_sequence=event_sequence,
        event_type=required["event_type"],
        event_time_ms=_event_time_ms(log),
        is_terminal=parse_bool(log.get("is_terminal")),
        chunk_index=chunk_index,
        chunk_count=chunk_count,
        payload_chunk=payload_chunk,
        raw_json=json.dumps(log, ensure_ascii=False, sort_keys=True, default=str),
    )


def decode_event_payload(records: Iterable[ParsedRecord]) -> dict[str, Any]:
    items = sorted(records, key=lambda item: item.chunk_index)
    if not items:
        raise ValueError("event has no chunks")
    expected = items[0].chunk_count
    indexes = [item.chunk_index for item in items]
    if len(items) != expected or indexes != list(range(expected)):
        raise ValueError(f"event chunks incomplete: got={indexes} expected=0..{expected - 1}")
    if any(item.chunk_count != expected for item in items):
        raise ValueError("event chunks disagree on chunk_count")
    payload = json.loads("".join(item.payload_chunk for item in items))
    if not isinstance(payload, dict):
        raise ValueError("event payload must decode to an object")
    return payload


def raw_error_id(log: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(log, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
