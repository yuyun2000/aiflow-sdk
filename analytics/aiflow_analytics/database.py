from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .parser import (
    ParsedRecord,
    normalize_mac_address,
    parse_record,
    raw_error_id,
    safe_float,
    safe_int,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_records (
    record_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    project_id TEXT NOT NULL,
    mac_address TEXT NOT NULL DEFAULT '',
    conversation_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    turn_index INTEGER NOT NULL,
    turn_kind TEXT NOT NULL,
    event_sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    event_time_ms INTEGER NOT NULL,
    is_terminal INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    payload_chunk TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    UNIQUE(event_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_raw_records_event ON raw_records(event_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_raw_records_turn ON raw_records(turn_id, event_sequence);
CREATE INDEX IF NOT EXISTS idx_raw_records_time ON raw_records(event_time_ms);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    project_id TEXT NOT NULL,
    mac_address TEXT NOT NULL DEFAULT '',
    conversation_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    turn_index INTEGER NOT NULL,
    turn_kind TEXT NOT NULL,
    event_sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    event_time_ms INTEGER NOT NULL,
    is_terminal INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    assembled_at TEXT NOT NULL,
    UNIQUE(turn_id, event_sequence)
);

CREATE INDEX IF NOT EXISTS idx_events_time ON events(event_time_ms);
CREATE INDEX IF NOT EXISTS idx_events_turn ON events(turn_id, event_sequence);
CREATE INDEX IF NOT EXISTS idx_events_conversation ON events(conversation_id, turn_index);
CREATE INDEX IF NOT EXISTS idx_events_type_time ON events(event_type, event_time_ms);

CREATE TABLE IF NOT EXISTS turns (
    turn_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    mac_address TEXT NOT NULL DEFAULT '',
    conversation_id TEXT NOT NULL,
    turn_index INTEGER NOT NULL,
    turn_kind TEXT NOT NULL,
    input_time_ms INTEGER NOT NULL,
    started_time_ms INTEGER,
    finished_time_ms INTEGER,
    first_event_ms INTEGER NOT NULL,
    last_event_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    primary_model TEXT NOT NULL,
    stop_reason TEXT NOT NULL,
    terminal_reason TEXT NOT NULL,
    error_code TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    attachment_count INTEGER NOT NULL,
    input_chars INTEGER NOT NULL,
    query_chars INTEGER NOT NULL,
    assistant_message_count INTEGER NOT NULL,
    assistant_chars INTEGER NOT NULL,
    thinking_block_count INTEGER NOT NULL,
    thinking_chars INTEGER NOT NULL,
    partial_block_count INTEGER NOT NULL,
    tool_call_count INTEGER NOT NULL,
    tool_result_count INTEGER NOT NULL,
    tool_error_count INTEGER NOT NULL,
    file_count INTEGER NOT NULL,
    deployment_attempted INTEGER NOT NULL,
    deployment_succeeded INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_input_tokens INTEGER NOT NULL,
    cache_creation_input_tokens INTEGER NOT NULL,
    total_tokens INTEGER NOT NULL,
    total_cost_usd REAL,
    duration_ms INTEGER,
    duration_api_ms INTEGER,
    queue_duration_ms INTEGER,
    service_duration_ms INTEGER,
    num_agent_turns INTEGER,
    has_agent_result INTEGER NOT NULL,
    has_terminal INTEGER NOT NULL,
    has_partial INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(conversation_id, turn_index)
);

CREATE INDEX IF NOT EXISTS idx_turns_input_time ON turns(input_time_ms);
CREATE INDEX IF NOT EXISTS idx_turns_status_time ON turns(status, input_time_ms);
CREATE INDEX IF NOT EXISTS idx_turns_project_time ON turns(project_id, input_time_ms);
CREATE INDEX IF NOT EXISTS idx_turns_conversation ON turns(conversation_id, turn_index);
CREATE INDEX IF NOT EXISTS idx_turns_model_time ON turns(primary_model, input_time_ms);

CREATE TABLE IF NOT EXISTS tool_calls (
    turn_id TEXT NOT NULL,
    tool_key TEXT NOT NULL,
    tool_use_id TEXT NOT NULL,
    tool_type TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    started_time_ms INTEGER,
    finished_time_ms INTEGER,
    duration_ms INTEGER,
    is_error INTEGER NOT NULL,
    input_json TEXT,
    result_json TEXT,
    PRIMARY KEY(turn_id, tool_key),
    FOREIGN KEY(turn_id) REFERENCES turns(turn_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_name ON tool_calls(tool_name, started_time_ms);
CREATE INDEX IF NOT EXISTS idx_tool_calls_error ON tool_calls(is_error, started_time_ms);

CREATE TABLE IF NOT EXISTS turn_model_usage (
    turn_id TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_input_tokens INTEGER NOT NULL,
    cache_creation_input_tokens INTEGER NOT NULL,
    web_search_requests INTEGER NOT NULL,
    cost_usd REAL,
    context_window INTEGER,
    max_output_tokens INTEGER,
    provider TEXT NOT NULL,
    canonical_model TEXT NOT NULL,
    PRIMARY KEY(turn_id, model),
    FOREIGN KEY(turn_id) REFERENCES turns(turn_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_turn_model_usage_model ON turn_model_usage(model);

CREATE TABLE IF NOT EXISTS ingest_errors (
    error_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    error_type TEXT NOT NULL,
    error_message TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    occurrences INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_days (
    event_date TEXT PRIMARY KEY,
    completed_at TEXT NOT NULL,
    fetched_count INTEGER NOT NULL,
    inserted_count INTEGER NOT NULL,
    assembled_count INTEGER NOT NULL,
    error_count INTEGER NOT NULL,
    fallback_checked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    status TEXT NOT NULL,
    fetched_count INTEGER NOT NULL DEFAULT 0,
    inserted_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    assembled_count INTEGER NOT NULL DEFAULT 0,
    turn_count INTEGER NOT NULL DEFAULT 0,
    ignored_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
"""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _json_object(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _int_value(data: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = safe_int(data.get(key))
        if value is not None:
            return value
    return 0


def _float_value(data: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = safe_float(data.get(key))
        if value is not None:
            return value
    return None


class Database:
    def __init__(self, path: Path, schema_version: int) -> None:
        self.path = path
        self.schema_version = schema_version
        self._write_lock = threading.RLock()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            sync_day_columns = self._table_columns(connection, "sync_days")
            if "fallback_checked" not in sync_day_columns:
                connection.execute(
                    "ALTER TABLE sync_days "
                    "ADD COLUMN fallback_checked INTEGER NOT NULL DEFAULT 0"
                )
            mac_backfill_needed = False
            for table in ("raw_records", "events", "turns"):
                if "mac_address" not in self._table_columns(connection, table):
                    connection.execute(
                        f"ALTER TABLE {table} "
                        "ADD COLUMN mac_address TEXT NOT NULL DEFAULT ''"
                    )
                    mac_backfill_needed = True
            if mac_backfill_needed:
                self._backfill_mac_addresses(connection)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_raw_records_mac_time "
                "ON raw_records(mac_address, event_time_ms)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_mac_time "
                "ON events(mac_address, event_time_ms)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_turns_mac_time "
                "ON turns(mac_address, input_time_ms)"
            )
            connection.execute(
                """
                UPDATE turns
                SET total_tokens = input_tokens + output_tokens
                    + cache_read_input_tokens + cache_creation_input_tokens
                WHERE total_tokens != input_tokens + output_tokens
                    + cache_read_input_tokens + cache_creation_input_tokens
                """
            )

    @staticmethod
    def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }

    def _backfill_mac_addresses(self, connection: sqlite3.Connection) -> None:
        raw_rows = connection.execute(
            "SELECT record_id, raw_json FROM raw_records WHERE mac_address=''"
        ).fetchall()
        for row in raw_rows:
            mac_address = normalize_mac_address(
                _json_object(str(row["raw_json"])).get("mac_address")
            )
            if mac_address:
                connection.execute(
                    "UPDATE raw_records SET mac_address=? WHERE record_id=?",
                    (mac_address, row["record_id"]),
                )

        event_ids = connection.execute(
            """
            SELECT DISTINCT r.event_id
            FROM raw_records r
            LEFT JOIN events e ON e.event_id=r.event_id
            WHERE r.mac_address!='' AND COALESCE(e.mac_address, '')=''
            """
        ).fetchall()
        affected_turns: set[str] = set()
        for row in event_ids:
            turn_id = self._assemble_event(connection, str(row["event_id"]))
            if turn_id:
                affected_turns.add(turn_id)
        turn_ids = connection.execute(
            """
            SELECT DISTINCT e.turn_id
            FROM events e
            LEFT JOIN turns t ON t.turn_id=e.turn_id
            WHERE e.mac_address!='' AND COALESCE(t.mac_address, '')=''
            """
        ).fetchall()
        affected_turns.update(str(row["turn_id"]) for row in turn_ids)
        for turn_id in sorted(affected_turns):
            self._rebuild_turn(connection, turn_id)

    def ping(self) -> bool:
        try:
            with self.connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    @staticmethod
    def _record_error(
        connection: sqlite3.Connection,
        *,
        error_id: str,
        event_id: str,
        error_type: str,
        message: str,
        raw_json: str,
    ) -> None:
        now = _utc_now()
        connection.execute(
            """
            INSERT INTO ingest_errors(
                error_id, event_id, error_type, error_message, raw_json,
                first_seen_at, last_seen_at, occurrences
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(error_id) DO UPDATE SET
                error_type=excluded.error_type,
                error_message=excluded.error_message,
                raw_json=excluded.raw_json,
                last_seen_at=excluded.last_seen_at,
                occurrences=ingest_errors.occurrences + 1
            """,
            (
                error_id,
                event_id,
                error_type,
                message[:2000],
                raw_json[:20000],
                now,
                now,
            ),
        )

    def insert_logs(self, logs: Iterable[dict[str, Any]]) -> dict[str, int]:
        fetched = 0
        ignored = 0
        parse_errors: list[tuple[str, str, str, str, str]] = []
        records: list[ParsedRecord] = []
        for log in logs:
            fetched += 1
            try:
                parsed = parse_record(log, self.schema_version)
            except ValueError as exc:
                parse_errors.append(
                    (
                        raw_error_id(log),
                        str(log.get("event_id") or ""),
                        "record_parse",
                        str(exc),
                        json.dumps(log, ensure_ascii=False, sort_keys=True, default=str),
                    )
                )
                continue
            if parsed is None:
                ignored += 1
                continue
            records.append(parsed)

        inserted = 0
        assembled = 0
        affected_events: set[str] = set()
        affected_turns: set[str] = set()
        with self._write_lock, self.connect() as connection:
            for error in parse_errors:
                self._record_error(
                    connection,
                    error_id=error[0],
                    event_id=error[1],
                    error_type=error[2],
                    message=error[3],
                    raw_json=error[4],
                )
            synced_at = _utc_now()
            for record in records:
                values = asdict(record)
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO raw_records(
                        record_id, event_id, schema_version, project_id,
                        mac_address, conversation_id, turn_id, turn_index, turn_kind,
                        event_sequence, event_type, event_time_ms, is_terminal,
                        chunk_index, chunk_count, payload_chunk, raw_json, synced_at
                    ) VALUES (
                        :record_id, :event_id, :schema_version, :project_id,
                        :mac_address, :conversation_id, :turn_id, :turn_index, :turn_kind,
                        :event_sequence, :event_type, :event_time_ms, :is_terminal,
                        :chunk_index, :chunk_count, :payload_chunk, :raw_json, :synced_at
                    )
                    """,
                    {**values, "is_terminal": int(record.is_terminal), "synced_at": synced_at},
                )
                if cursor.rowcount:
                    inserted += 1
                    affected_events.add(record.event_id)

            for event_id in sorted(affected_events):
                turn_id = self._assemble_event(connection, event_id)
                if turn_id:
                    assembled += 1
                    affected_turns.add(turn_id)
            for turn_id in sorted(affected_turns):
                self._rebuild_turn(connection, turn_id)

        return {
            "fetched": fetched,
            "inserted": inserted,
            "duplicates": len(records) - inserted,
            "assembled": assembled,
            "turns": len(affected_turns),
            "ignored": ignored,
            "errors": len(parse_errors),
        }

    def _assemble_event(self, connection: sqlite3.Connection, event_id: str) -> str | None:
        rows = connection.execute(
            "SELECT * FROM raw_records WHERE event_id=? ORDER BY chunk_index",
            (event_id,),
        ).fetchall()
        if not rows:
            return None
        expected = int(rows[0]["chunk_count"])
        indexes = [int(row["chunk_index"]) for row in rows]
        if len(rows) != expected or indexes != list(range(expected)):
            return None
        envelope_fields = (
            "schema_version",
            "project_id",
            "mac_address",
            "conversation_id",
            "turn_id",
            "turn_index",
            "turn_kind",
            "event_sequence",
            "event_type",
            "event_time_ms",
            "is_terminal",
            "chunk_count",
        )
        first = rows[0]
        if any(any(row[name] != first[name] for name in envelope_fields) for row in rows[1:]):
            self._record_error(
                connection,
                error_id=f"event:{event_id}",
                event_id=event_id,
                error_type="event_assembly",
                message="event chunks disagree on envelope metadata",
                raw_json="",
            )
            return None
        try:
            payload = json.loads("".join(str(row["payload_chunk"]) for row in rows))
            if not isinstance(payload, dict):
                raise ValueError("event payload must decode to an object")
        except (json.JSONDecodeError, ValueError) as exc:
            self._record_error(
                connection,
                error_id=f"event:{event_id}",
                event_id=event_id,
                error_type="event_assembly",
                message=str(exc),
                raw_json="",
            )
            return None
        connection.execute(
            """
            INSERT INTO events(
                event_id, schema_version, project_id, mac_address, conversation_id, turn_id,
                turn_index, turn_kind, event_sequence, event_type, event_time_ms,
                is_terminal, chunk_count, payload_json, assembled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                mac_address=excluded.mac_address,
                payload_json=excluded.payload_json,
                chunk_count=excluded.chunk_count,
                assembled_at=excluded.assembled_at
            """,
            (
                event_id,
                first["schema_version"],
                first["project_id"],
                first["mac_address"],
                first["conversation_id"],
                first["turn_id"],
                first["turn_index"],
                first["turn_kind"],
                first["event_sequence"],
                first["event_type"],
                first["event_time_ms"],
                first["is_terminal"],
                expected,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                _utc_now(),
            ),
        )
        connection.execute("DELETE FROM ingest_errors WHERE error_id=?", (f"event:{event_id}",))
        return str(first["turn_id"])

    @staticmethod
    def _tool_key(tool_type: str, tool_use_id: str, event_id: str) -> str:
        return f"{tool_type}:{tool_use_id or event_id}"

    def _rebuild_turn(self, connection: sqlite3.Connection, turn_id: str) -> None:
        rows = connection.execute(
            "SELECT * FROM events WHERE turn_id=? ORDER BY event_sequence, event_id",
            (turn_id,),
        ).fetchall()
        if not rows:
            return
        first = rows[0]
        mac_addresses = {
            str(row["mac_address"]) for row in rows if str(row["mac_address"])
        }
        mac_address = next(iter(mac_addresses)) if len(mac_addresses) == 1 else ""
        first_ms = min(int(row["event_time_ms"]) for row in rows)
        last_ms = max(int(row["event_time_ms"]) for row in rows)
        input_ms = first_ms
        started_ms: int | None = None
        finished_ms: int | None = None
        status = "incomplete"
        primary_model = ""
        stop_reason = ""
        terminal_reason = ""
        error_code = ""
        attachment_count = 0
        input_chars = 0
        query_chars = 0
        assistant_message_count = 0
        assistant_chars = 0
        thinking_block_count = 0
        thinking_chars = 0
        partial_block_count = 0
        file_paths: set[str] = set()
        deployment_attempted = False
        deployment_succeeded = False
        input_tokens = 0
        output_tokens = 0
        cache_read_tokens = 0
        cache_creation_tokens = 0
        total_cost_usd: float | None = None
        duration_ms: int | None = None
        duration_api_ms: int | None = None
        num_agent_turns: int | None = None
        has_agent_result = False
        has_terminal = False
        has_partial = False
        model_usage: dict[str, dict[str, Any]] = {}
        tools: dict[str, dict[str, Any]] = {}

        for row in rows:
            event_id = str(row["event_id"])
            event_type = str(row["event_type"])
            event_time_ms = int(row["event_time_ms"])
            payload = _json_object(row["payload_json"])
            if event_type in {"user_input", "direct_deploy_input"}:
                input_ms = event_time_ms
                prompt = str(payload.get("prompt") or "")
                input_chars = len(prompt)
                attachments = payload.get("attachments")
                attachment_count = len(attachments) if isinstance(attachments, list) else 0
            elif event_type == "agent_connected":
                query_chars = len(str(payload.get("query") or ""))
                runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
                primary_model = primary_model or str(runtime.get("model") or "")
            elif event_type == "task_started":
                started_ms = started_ms or event_time_ms
            elif event_type == "assistant_message":
                assistant_message_count += 1
                assistant_chars += len(str(payload.get("text") or ""))
            elif event_type == "agent_reasoning":
                thinking_block_count += 1
                thinking_chars += len(str(payload.get("thinking") or ""))
            elif event_type == "agent_partial_capture":
                partial_block_count += 1
                has_partial = True
                block_type = str(payload.get("block_type") or "")
                if block_type == "thinking":
                    thinking_block_count += 1
                    thinking_chars += len(str(payload.get("thinking") or ""))
                elif block_type == "text":
                    assistant_message_count += 1
                    assistant_chars += len(str(payload.get("text") or ""))
            elif event_type == "assistant_message_finished":
                if not payload.get("parent_tool_use_id"):
                    primary_model = str(payload.get("model") or primary_model)
                stop_reason = str(payload.get("stop_reason") or stop_reason)
            elif event_type in {"tool_started", "server_tool_started"}:
                tool_type = "server" if event_type.startswith("server_") else "client"
                tool_use_id = str(payload.get("tool_use_id") or "")
                key = self._tool_key(tool_type, tool_use_id, event_id)
                tools[key] = {
                    "tool_key": key,
                    "tool_use_id": tool_use_id,
                    "tool_type": tool_type,
                    "tool_name": str(payload.get("tool") or "unknown"),
                    "started_time_ms": event_time_ms,
                    "finished_time_ms": None,
                    "is_error": 0,
                    "input_json": json.dumps(payload.get("input"), ensure_ascii=False),
                    "result_json": None,
                }
            elif event_type in {"tool_finished", "server_tool_finished"}:
                tool_type = "server" if event_type.startswith("server_") else "client"
                tool_use_id = str(payload.get("tool_use_id") or "")
                key = self._tool_key(tool_type, tool_use_id, event_id)
                tool = tools.setdefault(
                    key,
                    {
                        "tool_key": key,
                        "tool_use_id": tool_use_id,
                        "tool_type": tool_type,
                        "tool_name": "unknown",
                        "started_time_ms": None,
                        "finished_time_ms": None,
                        "is_error": 0,
                        "input_json": None,
                        "result_json": None,
                    },
                )
                tool["finished_time_ms"] = event_time_ms
                tool["is_error"] = int(bool(payload.get("is_error")))
                tool["result_json"] = json.dumps(payload.get("content"), ensure_ascii=False)
            elif event_type in {"agent_result", "agent_result_error"}:
                has_agent_result = True
                usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
                input_tokens = _int_value(usage, "input_tokens", "inputTokens")
                output_tokens = _int_value(usage, "output_tokens", "outputTokens")
                cache_read_tokens = _int_value(
                    usage,
                    "cache_read_input_tokens",
                    "cacheReadInputTokens",
                )
                cache_creation_tokens = _int_value(
                    usage,
                    "cache_creation_input_tokens",
                    "cacheCreationInputTokens",
                )
                total_cost_usd = _float_value(payload, "total_cost_usd", "totalCostUsd")
                duration_ms = safe_int(payload.get("duration_ms"))
                duration_api_ms = safe_int(payload.get("duration_api_ms"))
                num_agent_turns = safe_int(payload.get("num_turns"))
                stop_reason = str(payload.get("stop_reason") or stop_reason)
                terminal_reason = str(payload.get("terminal_reason") or terminal_reason)
                raw_model_usage = payload.get("model_usage")
                if isinstance(raw_model_usage, dict):
                    model_usage = {
                        str(model): values
                        for model, values in raw_model_usage.items()
                        if isinstance(values, dict)
                    }
                if event_type == "agent_result_error":
                    error_code = str(payload.get("subtype") or "agent_result_error")
            elif event_type == "file_ready":
                file_paths.add(str(payload.get("path") or event_id))
            elif event_type == "deployment_started":
                deployment_attempted = True
            elif event_type == "deployment_finished":
                deployment_attempted = True
                result = payload.get("result")
                if isinstance(result, dict):
                    deployment_succeeded = bool(
                        result.get("ok", result.get("success", not result.get("error")))
                    )
                else:
                    deployment_succeeded = result is not None
            elif event_type in {"task_completed", "task_failed", "task_cancelled"}:
                has_terminal = True
                finished_ms = event_time_ms
                status = event_type.removeprefix("task_")
                error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
                error_code = str(error.get("code") or error_code)

        queue_duration_ms = (
            max(0, started_ms - input_ms) if started_ms is not None else None
        )
        service_duration_ms = (
            max(0, finished_ms - input_ms) if finished_ms is not None else None
        )
        metrics = {
            "turn_id": turn_id,
            "project_id": first["project_id"],
            "mac_address": mac_address,
            "conversation_id": first["conversation_id"],
            "turn_index": first["turn_index"],
            "turn_kind": first["turn_kind"],
            "input_time_ms": input_ms,
            "started_time_ms": started_ms,
            "finished_time_ms": finished_ms,
            "first_event_ms": first_ms,
            "last_event_ms": last_ms,
            "status": status,
            "primary_model": primary_model,
            "stop_reason": stop_reason,
            "terminal_reason": terminal_reason,
            "error_code": error_code,
            "event_count": len(rows),
            "attachment_count": attachment_count,
            "input_chars": input_chars,
            "query_chars": query_chars,
            "assistant_message_count": assistant_message_count,
            "assistant_chars": assistant_chars,
            "thinking_block_count": thinking_block_count,
            "thinking_chars": thinking_chars,
            "partial_block_count": partial_block_count,
            "tool_call_count": len(tools),
            "tool_result_count": sum(
                1 for tool in tools.values() if tool["finished_time_ms"] is not None
            ),
            "tool_error_count": sum(int(tool["is_error"]) for tool in tools.values()),
            "file_count": len(file_paths),
            "deployment_attempted": int(deployment_attempted),
            "deployment_succeeded": int(deployment_succeeded),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read_tokens,
            "cache_creation_input_tokens": cache_creation_tokens,
            "total_tokens": (
                input_tokens
                + output_tokens
                + cache_read_tokens
                + cache_creation_tokens
            ),
            "total_cost_usd": total_cost_usd,
            "duration_ms": duration_ms,
            "duration_api_ms": duration_api_ms,
            "queue_duration_ms": queue_duration_ms,
            "service_duration_ms": service_duration_ms,
            "num_agent_turns": num_agent_turns,
            "has_agent_result": int(has_agent_result),
            "has_terminal": int(has_terminal),
            "has_partial": int(has_partial),
            "updated_at": _utc_now(),
        }
        connection.execute(
            """
            INSERT INTO turns(
                turn_id, project_id, mac_address, conversation_id, turn_index, turn_kind,
                input_time_ms, started_time_ms, finished_time_ms, first_event_ms,
                last_event_ms, status, primary_model, stop_reason, terminal_reason,
                error_code, event_count, attachment_count, input_chars, query_chars,
                assistant_message_count, assistant_chars, thinking_block_count,
                thinking_chars, partial_block_count, tool_call_count,
                tool_result_count, tool_error_count, file_count,
                deployment_attempted, deployment_succeeded, input_tokens,
                output_tokens, cache_read_input_tokens, cache_creation_input_tokens,
                total_tokens, total_cost_usd, duration_ms, duration_api_ms,
                queue_duration_ms, service_duration_ms, num_agent_turns,
                has_agent_result, has_terminal, has_partial, updated_at
            ) VALUES (
                :turn_id, :project_id, :mac_address, :conversation_id, :turn_index, :turn_kind,
                :input_time_ms, :started_time_ms, :finished_time_ms, :first_event_ms,
                :last_event_ms, :status, :primary_model, :stop_reason, :terminal_reason,
                :error_code, :event_count, :attachment_count, :input_chars, :query_chars,
                :assistant_message_count, :assistant_chars, :thinking_block_count,
                :thinking_chars, :partial_block_count, :tool_call_count,
                :tool_result_count, :tool_error_count, :file_count,
                :deployment_attempted, :deployment_succeeded, :input_tokens,
                :output_tokens, :cache_read_input_tokens, :cache_creation_input_tokens,
                :total_tokens, :total_cost_usd, :duration_ms, :duration_api_ms,
                :queue_duration_ms, :service_duration_ms, :num_agent_turns,
                :has_agent_result, :has_terminal, :has_partial, :updated_at
            )
            ON CONFLICT(turn_id) DO UPDATE SET
                project_id=excluded.project_id,
                mac_address=excluded.mac_address,
                conversation_id=excluded.conversation_id,
                turn_index=excluded.turn_index,
                turn_kind=excluded.turn_kind,
                input_time_ms=excluded.input_time_ms,
                started_time_ms=excluded.started_time_ms,
                finished_time_ms=excluded.finished_time_ms,
                first_event_ms=excluded.first_event_ms,
                last_event_ms=excluded.last_event_ms,
                status=excluded.status,
                primary_model=excluded.primary_model,
                stop_reason=excluded.stop_reason,
                terminal_reason=excluded.terminal_reason,
                error_code=excluded.error_code,
                event_count=excluded.event_count,
                attachment_count=excluded.attachment_count,
                input_chars=excluded.input_chars,
                query_chars=excluded.query_chars,
                assistant_message_count=excluded.assistant_message_count,
                assistant_chars=excluded.assistant_chars,
                thinking_block_count=excluded.thinking_block_count,
                thinking_chars=excluded.thinking_chars,
                partial_block_count=excluded.partial_block_count,
                tool_call_count=excluded.tool_call_count,
                tool_result_count=excluded.tool_result_count,
                tool_error_count=excluded.tool_error_count,
                file_count=excluded.file_count,
                deployment_attempted=excluded.deployment_attempted,
                deployment_succeeded=excluded.deployment_succeeded,
                input_tokens=excluded.input_tokens,
                output_tokens=excluded.output_tokens,
                cache_read_input_tokens=excluded.cache_read_input_tokens,
                cache_creation_input_tokens=excluded.cache_creation_input_tokens,
                total_tokens=excluded.total_tokens,
                total_cost_usd=excluded.total_cost_usd,
                duration_ms=excluded.duration_ms,
                duration_api_ms=excluded.duration_api_ms,
                queue_duration_ms=excluded.queue_duration_ms,
                service_duration_ms=excluded.service_duration_ms,
                num_agent_turns=excluded.num_agent_turns,
                has_agent_result=excluded.has_agent_result,
                has_terminal=excluded.has_terminal,
                has_partial=excluded.has_partial,
                updated_at=excluded.updated_at
            """,
            metrics,
        )
        connection.execute("DELETE FROM tool_calls WHERE turn_id=?", (turn_id,))
        for tool in tools.values():
            started = tool["started_time_ms"]
            finished = tool["finished_time_ms"]
            connection.execute(
                """
                INSERT INTO tool_calls(
                    turn_id, tool_key, tool_use_id, tool_type, tool_name,
                    started_time_ms, finished_time_ms, duration_ms, is_error,
                    input_json, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    tool["tool_key"],
                    tool["tool_use_id"],
                    tool["tool_type"],
                    tool["tool_name"],
                    started,
                    finished,
                    max(0, finished - started)
                    if started is not None and finished is not None
                    else None,
                    tool["is_error"],
                    tool["input_json"],
                    tool["result_json"],
                ),
            )
        connection.execute("DELETE FROM turn_model_usage WHERE turn_id=?", (turn_id,))
        for model, values in model_usage.items():
            connection.execute(
                """
                INSERT INTO turn_model_usage(
                    turn_id, model, input_tokens, output_tokens,
                    cache_read_input_tokens, cache_creation_input_tokens,
                    web_search_requests, cost_usd, context_window,
                    max_output_tokens, provider, canonical_model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    model,
                    _int_value(values, "inputTokens", "input_tokens"),
                    _int_value(values, "outputTokens", "output_tokens"),
                    _int_value(values, "cacheReadInputTokens", "cache_read_input_tokens"),
                    _int_value(
                        values,
                        "cacheCreationInputTokens",
                        "cache_creation_input_tokens",
                    ),
                    _int_value(values, "webSearchRequests", "web_search_requests"),
                    _float_value(values, "costUSD", "cost_usd"),
                    safe_int(values.get("contextWindow") or values.get("context_window")),
                    safe_int(values.get("maxOutputTokens") or values.get("max_output_tokens")),
                    str(values.get("provider") or ""),
                    str(values.get("canonicalModel") or values.get("canonical_model") or ""),
                ),
            )

    def mark_day_synced(
        self,
        event_date: str,
        result: dict[str, int],
        *,
        fallback_checked: bool = False,
    ) -> None:
        with self._write_lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO sync_days(
                    event_date, completed_at, fetched_count, inserted_count,
                    assembled_count, error_count, fallback_checked
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_date) DO UPDATE SET
                    completed_at=excluded.completed_at,
                    fetched_count=excluded.fetched_count,
                    inserted_count=excluded.inserted_count,
                    assembled_count=excluded.assembled_count,
                    error_count=excluded.error_count,
                    fallback_checked=excluded.fallback_checked
                """,
                (
                    event_date,
                    _utc_now(),
                    result["fetched"],
                    result["inserted"],
                    result["assembled"],
                    result["errors"],
                    int(fallback_checked),
                ),
            )

    def day_is_synced(self, event_date: str) -> bool:
        return (
            self.query_one(
                """
                SELECT 1
                FROM sync_days
                WHERE event_date=?
                  AND error_count=0
                  AND (fetched_count > 0 OR fallback_checked=1)
                """,
                (event_date,),
            )
            is not None
        )

    def historical_sync_needed(self, start_date: str, end_date: str) -> bool:
        """Return whether any day in an inclusive historical range lacks a clean sync marker."""
        if start_date > end_date:
            return False
        expected_days = (
            datetime.fromisoformat(end_date).date()
            - datetime.fromisoformat(start_date).date()
        ).days + 1
        row = self.query_one(
            """
            SELECT COUNT(*) AS count
            FROM sync_days
            WHERE event_date>=? AND event_date<=?
              AND error_count=0
              AND (fetched_count > 0 OR fallback_checked=1)
            """,
            (start_date, end_date),
        )
        return int(row["count"] if row else 0) < expected_days

    def start_sync_run(self, start_date: str, end_date: str) -> int:
        with self._write_lock, self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO sync_runs(started_at, start_date, end_date, status)
                VALUES (?, ?, ?, 'running')
                """,
                (_utc_now(), start_date, end_date),
            )
            return int(cursor.lastrowid)

    def finish_sync_run(
        self,
        run_id: int,
        *,
        status: str,
        totals: dict[str, int],
        error: str = "",
    ) -> None:
        with self._write_lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE sync_runs SET
                    finished_at=?, status=?, fetched_count=?, inserted_count=?,
                    duplicate_count=?, assembled_count=?, turn_count=?,
                    ignored_count=?, error_count=?, error=?
                WHERE id=?
                """,
                (
                    _utc_now(),
                    status,
                    totals["fetched"],
                    totals["inserted"],
                    totals["duplicates"],
                    totals["assembled"],
                    totals["turns"],
                    totals["ignored"],
                    totals["errors"],
                    error[:2000],
                    run_id,
                ),
            )

    def latest_event_time_ms(self) -> int | None:
        row = self.query_one("SELECT MAX(event_time_ms) AS value FROM events")
        return safe_int(row["value"]) if row else None

    def sync_status(self) -> dict[str, Any]:
        latest = self.query_one("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1")
        coverage = self.query_one(
            """
            SELECT MIN(event_time_ms) AS start_ms, MAX(event_time_ms) AS end_ms,
                   COUNT(*) AS event_count, COUNT(DISTINCT turn_id) AS turn_count
            FROM events
            """
        )
        records = self.query_one("SELECT COUNT(*) AS count FROM raw_records")
        incomplete = self.query_one(
            """
            SELECT COUNT(*) AS count FROM (
                SELECT event_id
                FROM raw_records
                GROUP BY event_id
                HAVING COUNT(*) != MAX(chunk_count)
            )
            """
        )
        errors = self.query_one("SELECT COUNT(*) AS count FROM ingest_errors")
        return {
            "latest_run": latest,
            "coverage": coverage,
            "stored_records": int(records["count"] if records else 0),
            "incomplete_events": int(incomplete["count"] if incomplete else 0),
            "ingest_errors": int(errors["count"] if errors else 0),
        }

    def query(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [dict(row) for row in rows]

    def query_one(
        self,
        sql: str,
        parameters: tuple[Any, ...] = (),
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(sql, parameters).fetchone()
        return dict(row) if row else None
