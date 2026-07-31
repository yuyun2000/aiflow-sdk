from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


class SessionCapacityFull(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


class Storage:
    def __init__(self, database_path: Path, event_retention: int = 10000):
        self.database_path = database_path
        self.event_retention = event_retention
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = NORMAL")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS contexts (
                    context_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL UNIQUE,
                    token_hash TEXT NOT NULL UNIQUE,
                    conversation_id TEXT NOT NULL,
                    session_id TEXT,
                    label TEXT NOT NULL,
                    device_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    context_id TEXT NOT NULL,
                    stream_token_hash TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    prompt TEXT,
                    request_json TEXT NOT NULL,
                    result_json TEXT,
                    error_json TEXT,
                    session_id TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    heartbeat_at TEXT,
                    last_agent_event_at TEXT,
                    FOREIGN KEY(context_id) REFERENCES contexts(context_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_context_status ON tasks(context_id, status);
                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(task_id, sequence),
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_events_task_sequence ON task_events(task_id, sequence);
                CREATE TABLE IF NOT EXISTS client_nonces (
                    key_id TEXT NOT NULL,
                    nonce_hash TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY(key_id, nonce_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_client_nonces_expiry ON client_nonces(expires_at);
                CREATE TABLE IF NOT EXISTS client_rate_counters (
                    key_id TEXT NOT NULL,
                    counter_type TEXT NOT NULL,
                    window_start INTEGER NOT NULL,
                    count INTEGER NOT NULL,
                    PRIMARY KEY(key_id, counter_type, window_start)
                );
                """
            )
            self._migrate_context_columns(db)
            now = utc_now()
            db.execute(
                """
                UPDATE tasks
                SET status='failed', stage='server_restarted', progress=100,
                    error_json=?, updated_at=?, finished_at=?
                WHERE status NOT IN ('completed', 'failed', 'cancelled')
                """,
                (json.dumps({"code": "server_restarted", "message": "service restarted while task was active"}), now, now),
            )

    @staticmethod
    def _counter_value(
        db: sqlite3.Connection,
        key_id: str,
        counter_type: str,
        window_start: int,
    ) -> int:
        row = db.execute(
            "SELECT count FROM client_rate_counters WHERE key_id=? AND counter_type=? AND window_start=?",
            (key_id, counter_type, window_start),
        ).fetchone()
        return int(row["count"]) if row else 0

    @staticmethod
    def _increment_counter(
        db: sqlite3.Connection,
        key_id: str,
        counter_type: str,
        window_start: int,
    ) -> None:
        db.execute(
            """
            INSERT INTO client_rate_counters(key_id, counter_type, window_start, count)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(key_id, counter_type, window_start)
            DO UPDATE SET count=count+1
            """,
            (key_id, counter_type, window_start),
        )

    def claim_client_request(
        self,
        key_id: str,
        nonce: str,
        *,
        expires_at: int,
        now: int | None = None,
        requests_per_minute: int,
    ) -> str:
        current = int(time.time()) if now is None else int(now)
        minute = current - current % 60
        nonce_hash = hash_token(nonce)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM client_nonces WHERE expires_at<?", (current,))
            try:
                db.execute(
                    "INSERT INTO client_nonces(key_id, nonce_hash, expires_at, created_at) VALUES (?, ?, ?, ?)",
                    (key_id, nonce_hash, expires_at, current),
                )
            except sqlite3.IntegrityError:
                return "replay"
            count = self._counter_value(db, key_id, "request-minute", minute)
            if count >= requests_per_minute:
                return "rate_limited"
            self._increment_counter(db, key_id, "request-minute", minute)
            db.execute("DELETE FROM client_rate_counters WHERE window_start<?", (current - 172800,))
        return "ok"

    def reserve_ai_task(
        self,
        caller_id: str,
        *,
        now: int | None = None,
        per_client_minute: int,
        per_client_day: int,
        global_day: int,
    ) -> str:
        current = int(time.time()) if now is None else int(now)
        minute = current - current % 60
        day = current - current % 86400
        counters = (
            (caller_id, "ai-minute", minute, per_client_minute, "client_minute"),
            (caller_id, "ai-day", day, per_client_day, "client_day"),
            ("__global__", "ai-day", day, global_day, "global_day"),
        )
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for key_id, counter_type, window_start, limit, reason in counters:
                if self._counter_value(db, key_id, counter_type, window_start) >= limit:
                    return reason
            for key_id, counter_type, window_start, _, _ in counters:
                self._increment_counter(db, key_id, counter_type, window_start)
        return "ok"

    @staticmethod
    def _migrate_context_columns(db: sqlite3.Connection) -> None:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(contexts)").fetchall()}
        if "device_id" not in columns:
            db.execute("ALTER TABLE contexts ADD COLUMN device_id TEXT")
        if "last_seen_at" not in columns:
            db.execute("ALTER TABLE contexts ADD COLUMN last_seen_at TEXT")

        rows = db.execute(
            "SELECT context_id, device_id, device_json, updated_at, last_seen_at FROM contexts ORDER BY updated_at DESC"
        ).fetchall()
        used: set[str] = set()
        for row in rows:
            candidate = str(row["device_id"] or "").strip()
            if not candidate:
                try:
                    device = json.loads(row["device_json"])
                except (TypeError, json.JSONDecodeError):
                    device = {}
                candidate = str(device.get("device_id") or "").strip()
            if not candidate or candidate in used:
                candidate = f"legacy:{row['context_id']}"
            used.add(candidate)
            db.execute(
                "UPDATE contexts SET device_id=?, last_seen_at=COALESCE(last_seen_at, updated_at) WHERE context_id=?",
                (candidate, row["context_id"]),
            )
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_contexts_device_id ON contexts(device_id)")

    def connect_context(
        self,
        context_id: str,
        token: str,
        conversation_id: str,
        label: str,
        device: dict[str, Any],
        max_sessions: int,
    ) -> tuple[dict[str, Any], bool]:
        now = utc_now()
        device_id = str(device["device_id"])
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT context_id FROM contexts WHERE device_id=?", (device_id,)
            ).fetchone()
            if existing:
                context_id = existing["context_id"]
                db.execute(
                    """
                    UPDATE contexts
                    SET token_hash=?, label=?, device_json=?, updated_at=?, last_seen_at=?
                    WHERE context_id=?
                    """,
                    (
                        hash_token(token),
                        label,
                        json.dumps(device, ensure_ascii=False),
                        now,
                        now,
                        context_id,
                    ),
                )
                created = False
            else:
                count = int(db.execute("SELECT COUNT(*) FROM contexts").fetchone()[0])
                if count >= max_sessions:
                    raise SessionCapacityFull("server session capacity is full")
                db.execute(
                    """
                    INSERT INTO contexts(
                        context_id, device_id, token_hash, conversation_id, label,
                        device_json, created_at, updated_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        context_id,
                        device_id,
                        hash_token(token),
                        conversation_id,
                        label,
                        json.dumps(device, ensure_ascii=False),
                        now,
                        now,
                        now,
                    ),
                )
                created = True
        context = self.get_context(context_id)
        if context is None:
            raise RuntimeError("context connection was not persisted")
        return context, created

    def get_context(self, context_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM contexts WHERE context_id=?", (context_id,)).fetchone()
        return self._context_row(row) if row else None

    def get_context_by_token(self, token: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM contexts WHERE token_hash=?", (hash_token(token),)).fetchone()
        return self._context_row(row) if row else None

    def get_context_by_device_id(self, device_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM contexts WHERE device_id=?", (device_id,)).fetchone()
        return self._context_row(row) if row else None

    def touch_context(self, context_id: str) -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as db:
            db.execute("UPDATE contexts SET last_seen_at=? WHERE context_id=?", (now, context_id))
        return self.get_context(context_id)

    def count_contexts(self) -> int:
        with self.connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM contexts").fetchone()[0])

    def count_recent_contexts(self, window_seconds: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).isoformat()
        with self.connect() as db:
            return int(
                db.execute(
                    "SELECT COUNT(*) FROM contexts WHERE last_seen_at>=?", (cutoff,)
                ).fetchone()[0]
            )

    def update_device(self, context_id: str, device: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as db:
            db.execute(
                "UPDATE contexts SET device_json=?, updated_at=? WHERE context_id=?",
                (json.dumps(device, ensure_ascii=False), now, context_id),
            )
        return self.get_context(context_id)

    def update_conversation(self, context_id: str, conversation_id: str, session_id: str | None) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE contexts SET conversation_id=?, session_id=?, updated_at=? WHERE context_id=?",
                (conversation_id, session_id, utc_now(), context_id),
            )

    def set_session_id(self, context_id: str, session_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE contexts SET session_id=?, updated_at=? WHERE context_id=?",
                (session_id, utc_now(), context_id),
            )

    def delete_context(self, context_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM contexts WHERE context_id=?", (context_id,))

    @staticmethod
    def _context_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["device"] = json.loads(data.pop("device_json"))
        legacy_client_id = data["device"].pop("push_client_id", None)
        if not data["device"].get("client_id") and legacy_client_id:
            data["device"]["client_id"] = legacy_client_id
        data["device"]["device_id"] = data["device_id"]
        data.pop("token_hash", None)
        return data

    def create_task(
        self,
        task_id: str,
        context_id: str,
        stream_token: str,
        kind: str,
        request: dict[str, Any],
        prompt: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO tasks(
                    task_id, context_id, stream_token_hash, kind, status, stage, progress,
                    prompt, request_json, created_at, updated_at, heartbeat_at
                ) VALUES (?, ?, ?, ?, 'queued', 'queued', 0, ?, ?, ?, ?, ?)
                """,
                (task_id, context_id, hash_token(stream_token), kind, prompt, json.dumps(request, ensure_ascii=False), now, now, now),
            )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._task_row(row) if row else None

    def get_owned_task(self, task_id: str, context_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE task_id=? AND context_id=?", (task_id, context_id)
            ).fetchone()
        return self._task_row(row) if row else None

    def validate_stream_token(self, task_id: str, token: str) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT 1 FROM tasks WHERE task_id=? AND stream_token_hash=?",
                (task_id, hash_token(token)),
            ).fetchone()
        return row is not None

    def get_active_task(self, context_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT * FROM tasks
                WHERE context_id=? AND status NOT IN ('completed', 'failed', 'cancelled')
                ORDER BY created_at DESC LIMIT 1
                """,
                (context_id,),
            ).fetchone()
        return self._task_row(row) if row else None

    def task_counts(self) -> dict[str, int]:
        counts = {"queued": 0, "running": 0}
        with self.connect() as db:
            rows = db.execute(
                "SELECT status, COUNT(*) AS value FROM tasks WHERE status IN ('queued', 'running') GROUP BY status"
            ).fetchall()
        for row in rows:
            counts[row["status"]] = int(row["value"])
        return counts

    def queue_position(self, task_id: str) -> int | None:
        with self.connect() as db:
            task = db.execute(
                "SELECT created_at, task_id, status FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if not task or task["status"] != "queued":
                return None
            row = db.execute(
                """
                SELECT COUNT(*) AS value FROM tasks
                WHERE status='queued'
                  AND (created_at < ? OR (created_at = ? AND task_id <= ?))
                """,
                (task["created_at"], task["created_at"], task_id),
            ).fetchone()
        return int(row["value"])

    def update_task(self, task_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {
            "status", "stage", "progress", "result_json", "error_json", "session_id",
            "cancel_requested", "started_at", "finished_at", "heartbeat_at", "last_agent_event_at",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unsupported task fields: {sorted(unknown)}")
        values = dict(fields)
        for key in ("result_json", "error_json"):
            if key in values and values[key] is not None and not isinstance(values[key], str):
                values[key] = json.dumps(values[key], ensure_ascii=False)
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key}=?" for key in values)
        with self.connect() as db:
            db.execute(
                f"UPDATE tasks SET {assignments} WHERE task_id=?",
                (*values.values(), task_id),
            )
        return self.get_task(task_id)

    def heartbeat(self, task_id: str) -> None:
        now = utc_now()
        with self.connect() as db:
            db.execute("UPDATE tasks SET heartbeat_at=?, updated_at=? WHERE task_id=?", (now, now, task_id))

    def append_event(self, task_id: str, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS value FROM task_events WHERE task_id=?", (task_id,)
            ).fetchone()
            sequence = int(row["value"]) + 1
            db.execute(
                "INSERT INTO task_events(task_id, sequence, event_type, data_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, sequence, event_type, json.dumps(data, ensure_ascii=False), now),
            )
            cutoff = sequence - self.event_retention
            if cutoff > 0:
                db.execute("DELETE FROM task_events WHERE task_id=? AND sequence<=?", (task_id, cutoff))
        return {"sequence": sequence, "type": event_type, "data": data, "created_at": now}

    def list_events(self, task_id: str, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT sequence, event_type, data_json, created_at
                FROM task_events WHERE task_id=? AND sequence>?
                ORDER BY sequence ASC LIMIT ?
                """,
                (task_id, after, limit),
            ).fetchall()
        return [
            {
                "sequence": int(row["sequence"]),
                "type": row["event_type"],
                "data": json.loads(row["data_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def last_event(self, task_id: str) -> dict[str, Any] | None:
        events = self.list_events(task_id, after=0, limit=self.event_retention)
        return events[-1] if events else None

    @staticmethod
    def _task_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["request"] = json.loads(data.pop("request_json"))
        data["result"] = json.loads(data.pop("result_json")) if data.get("result_json") else None
        data["error"] = json.loads(data.pop("error_json")) if data.get("error_json") else None
        data["cancel_requested"] = bool(data["cancel_requested"])
        data.pop("stream_token_hash", None)
        return data
