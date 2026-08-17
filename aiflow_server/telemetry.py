from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .config import TlsLoggingSettings


LOGGER = logging.getLogger(__name__)
TERMINAL_EVENT_TYPES = frozenset({"task_completed", "task_failed", "task_cancelled"})
TRACE_EVENT_NAME = "aiflow_conversation_trace"
TLS_EVENT_DATA_KEY = "__aiflow_tls_event_data__"
TLS_EXCLUDED_EVENT_TYPES = frozenset(
    {
        "agent_stream_event",
        "assistant_message_started",
        "assistant_text_delta",
        "task_queued",
    }
)


@dataclass(frozen=True)
class TlsOutboxRecord:
    row_id: int
    record_id: str
    topic_id: str
    source: str
    filename: str
    contents: dict[str, Any]
    log_time: int
    attempts: int


BatchSender = Callable[[list[TlsOutboxRecord]], None]


def should_enqueue_trace_event(
    event_type: str,
    public_data: dict[str, Any],
    telemetry_data: dict[str, Any] | None,
) -> bool:
    if event_type in TLS_EXCLUDED_EVENT_TYPES:
        return False
    if (
        event_type == "agent_user_message"
        and telemetry_data
        and telemetry_data.get("duplicate_of")
        == {"event_type": "agent_connected", "field": "query"}
    ):
        return False
    return not (
        event_type == "agent_reasoning"
        and public_data.get("finalized") is False
        and telemetry_data is None
    )


def initialize_tls_outbox(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS tls_log_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id TEXT NOT NULL UNIQUE,
            topic_id TEXT NOT NULL,
            source TEXT NOT NULL,
            filename TEXT NOT NULL,
            contents_json TEXT NOT NULL,
            log_time INTEGER NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tls_outbox_due
            ON tls_log_outbox(next_attempt_at, id);
        """
    )


def _project_id(settings: TlsLoggingSettings, context_id: str) -> str:
    digest = hmac.new(
        settings.pseudonym_key.encode("utf-8"),
        context_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"project_{digest[:32]}"


def _unix_milliseconds(timestamp: str) -> int:
    try:
        return int(datetime.fromisoformat(timestamp).timestamp() * 1000)
    except (TypeError, ValueError):
        return int(time.time() * 1000)


def _utf8_chunks(value: str, max_bytes: int) -> list[str]:
    if len(value.encode("utf-8")) <= max_bytes:
        return [value]
    chunks: list[str] = []
    remaining = value
    while remaining:
        low = 1
        high = len(remaining)
        best = 0
        while low <= high:
            middle = (low + high) // 2
            if len(remaining[:middle].encode("utf-8")) <= max_bytes:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        if best == 0:
            best = 1
        chunks.append(remaining[:best])
        remaining = remaining[best:]
    return chunks


def _device_binding(db: sqlite3.Connection, context_id: str) -> dict[str, Any]:
    """Read the raw device identifiers once for every physical TLS record."""
    row = db.execute(
        "SELECT device_id, device_json FROM contexts WHERE context_id=?",
        (context_id,),
    ).fetchone()
    if not row:
        return {"device_id": None, "client_id": None, "mac_address": None}
    try:
        device = json.loads(row["device_json"])
    except (TypeError, ValueError):
        device = {}
    if not isinstance(device, dict):
        device = {}
    return {
        "device_id": row["device_id"],
        "client_id": device.get("client_id") or device.get("push_client_id"),
        "mac_address": (
            device.get("mac_address")
            or device.get("macAddress")
            or device.get("mac")
        ),
    }


def enqueue_trace_event(
    db: sqlite3.Connection,
    settings: TlsLoggingSettings,
    *,
    context_id: str,
    conversation_id: str,
    turn_id: str,
    turn_index: int,
    turn_kind: str,
    event_sequence: int,
    event_type: str,
    event_data: dict[str, Any],
    created_at: str,
) -> int:
    if not settings.enabled:
        return 0

    payload_json = json.dumps(
        event_data,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    chunks = _utf8_chunks(payload_json, settings.max_payload_bytes)
    event_id = f"{turn_id}:{event_sequence:08d}"
    event_time_ms = _unix_milliseconds(created_at)
    device_binding = _device_binding(db, context_id)
    for chunk_index, chunk in enumerate(chunks):
        record_id = f"{event_id}:{chunk_index:04d}"
        contents = {
            "schema_version": settings.schema_version,
            "event": TRACE_EVENT_NAME,
            "record_id": record_id,
            "event_id": event_id,
            "project_id": _project_id(settings, context_id),
            **device_binding,
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "turn_index": turn_index,
            "turn_kind": turn_kind,
            "event_sequence": event_sequence,
            "event_type": event_type,
            "event_time": created_at,
            "event_time_unix_ms": event_time_ms,
            "is_terminal": event_type in TERMINAL_EVENT_TYPES,
            "chunk_index": chunk_index,
            "chunk_count": len(chunks),
            "payload_encoding": "json_utf8_chunks",
            "payload": chunk,
        }
        db.execute(
            """
            INSERT OR IGNORE INTO tls_log_outbox(
                record_id, topic_id, source, filename, contents_json,
                log_time, attempts, next_attempt_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)
            """,
            (
                record_id,
                settings.topic_id,
                settings.source,
                settings.filename,
                json.dumps(contents, ensure_ascii=False, separators=(",", ":")),
                event_time_ms // 1000,
                created_at,
            ),
        )
    return len(chunks)


class TlsTelemetry:
    """Drain durable conversation records to Volcengine TLS in one worker."""

    def __init__(
        self,
        settings: TlsLoggingSettings,
        database_path: Path,
        *,
        sender: BatchSender | None = None,
    ) -> None:
        self.settings = settings
        self.database_path = database_path
        self._sender = sender
        self._service: Any = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._shutdown_deadline: float | None = None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def start(self) -> None:
        if not self.settings.enabled:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._shutdown_deadline = None
        self._thread = threading.Thread(
            target=self._run,
            name="aiflow-tls-uploader",
            daemon=True,
        )
        self._thread.start()
        self._wake.set()

    def notify(self) -> None:
        if self.settings.enabled:
            self._wake.set()

    def _fetch_due(self) -> list[TlsOutboxRecord]:
        with self._connect() as db:
            first = db.execute(
                """
                SELECT topic_id, source, filename
                FROM tls_log_outbox
                WHERE next_attempt_at<=?
                ORDER BY id ASC
                LIMIT 1
                """,
                (time.time(),),
            ).fetchone()
            if not first:
                return []
            rows = db.execute(
                """
                SELECT id, record_id, topic_id, source, filename, contents_json,
                       log_time, attempts
                FROM tls_log_outbox
                WHERE next_attempt_at<=?
                  AND topic_id=? AND source=? AND filename=?
                ORDER BY id ASC
                LIMIT ?
                """,
                (
                    time.time(),
                    first["topic_id"],
                    first["source"],
                    first["filename"],
                    self.settings.batch_size,
                ),
            ).fetchall()
        return [
            TlsOutboxRecord(
                row_id=int(row["id"]),
                record_id=row["record_id"],
                topic_id=row["topic_id"],
                source=row["source"],
                filename=row["filename"],
                contents=json.loads(row["contents_json"]),
                log_time=int(row["log_time"]),
                attempts=int(row["attempts"]),
            )
            for row in rows
        ]

    def _default_send(self, records: list[TlsOutboxRecord]) -> None:
        if self._service is None:
            from volcengine.tls.TLSService import TLSService

            self._service = TLSService(
                self.settings.endpoint,
                self.settings.access_key,
                self.settings.secret_key,
                self.settings.region,
                timeout=self.settings.upload_timeout_seconds,
            )
        from volcengine.tls.tls_requests import PutLogsV2Logs, PutLogsV2Request

        groups: dict[tuple[str, str, str], list[TlsOutboxRecord]] = {}
        for record in records:
            groups.setdefault(
                (record.topic_id, record.source, record.filename), []
            ).append(record)
        for (topic_id, source, filename), grouped_records in groups.items():
            logs = PutLogsV2Logs(source=source, filename=filename)
            for record in grouped_records:
                logs.add_log(contents=record.contents, log_time=record.log_time)
            self._service.put_logs_v2(
                PutLogsV2Request(topic_id, logs, compression="zlib")
            )

    def _mark_uploaded(self, records: list[TlsOutboxRecord]) -> None:
        with self._connect() as db:
            db.executemany(
                "DELETE FROM tls_log_outbox WHERE id=?",
                [(record.row_id,) for record in records],
            )

    def _safe_error(self, error: Exception) -> str:
        message = f"{type(error).__name__}: {error}"
        for secret in (self.settings.access_key, self.settings.secret_key):
            if secret:
                message = message.replace(secret, "<redacted>")
        return message[:1000]

    def _mark_failed(self, records: list[TlsOutboxRecord], error: Exception) -> None:
        max_attempts = max(record.attempts for record in records) + 1
        retry_seconds = min(
            self.settings.retry_max_seconds,
            self.settings.retry_base_seconds * (2 ** min(max_attempts - 1, 10)),
        )
        next_attempt_at = time.time() + retry_seconds
        message = self._safe_error(error)
        with self._connect() as db:
            db.executemany(
                """
                UPDATE tls_log_outbox
                SET attempts=attempts+1, next_attempt_at=?, last_error=?
                WHERE id=?
                """,
                [(next_attempt_at, message, record.row_id) for record in records],
            )
        LOGGER.warning(
            "TLS conversation log upload failed; records remain in outbox: count=%d error_type=%s",
            len(records),
            type(error).__name__,
        )

    def flush_once(self) -> int:
        if not self.settings.enabled:
            return 0
        records = self._fetch_due()
        if not records:
            return 0
        try:
            (self._sender or self._default_send)(records)
        except Exception as exc:
            self._service = None
            self._mark_failed(records, exc)
            return 0
        self._mark_uploaded(records)
        return len(records)

    def _run(self) -> None:
        while True:
            self._wake.wait(timeout=0.25)
            self._wake.clear()
            if not self._stop.is_set() and self.settings.batch_wait_seconds:
                self._stop.wait(self.settings.batch_wait_seconds)

            while True:
                if self._stop.is_set() and (
                    self._shutdown_deadline is None
                    or time.monotonic() >= self._shutdown_deadline
                ):
                    return
                uploaded = self.flush_once()
                if uploaded == 0:
                    break
            if self._stop.is_set():
                return

    def shutdown(self) -> None:
        if not self._thread:
            return
        self._shutdown_deadline = time.monotonic() + self.settings.shutdown_timeout_seconds
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=self.settings.shutdown_timeout_seconds)

    def status(self) -> dict[str, Any]:
        if not self.settings.enabled:
            return {
                "enabled": False,
                "pending_records": 0,
                "oldest_created_at": None,
                "max_attempts": 0,
                "worker_running": False,
            }
        with self._connect() as db:
            row = db.execute(
                """
                SELECT COUNT(*) AS pending_records,
                       MIN(created_at) AS oldest_created_at,
                       MAX(attempts) AS max_attempts
                FROM tls_log_outbox
                """
            ).fetchone()
        return {
            "enabled": True,
            "pending_records": int(row["pending_records"]),
            "oldest_created_at": row["oldest_created_at"],
            "max_attempts": int(row["max_attempts"] or 0),
            "worker_running": bool(self._thread and self._thread.is_alive()),
        }
