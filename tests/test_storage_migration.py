from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace

from aiflow_server.config import load_settings
from aiflow_server.storage import Storage, hash_token
from aiflow_server.tasks import TaskManager


def test_storage_uses_wal_with_normal_synchronous_mode(tmp_path):
    storage = Storage(tmp_path / "aiflow.sqlite3")

    with storage.connect() as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]

    assert journal_mode == "wal"
    assert synchronous == 1


def test_legacy_ai_quota_schema_adds_cache_usage_columns(tmp_path):
    database = tmp_path / "aiflow.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE ai_quota_reservations (
            task_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL UNIQUE,
            authorization_id TEXT UNIQUE,
            model TEXT NOT NULL,
            granted_tokens INTEGER,
            status TEXT NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.commit()
    connection.close()

    storage = Storage(database)
    with storage.connect() as migrated:
        columns = {
            row["name"]
            for row in migrated.execute("PRAGMA table_info(ai_quota_reservations)").fetchall()
        }

    assert "cache_creation_input_tokens" in columns
    assert "cache_read_input_tokens" in columns


def test_legacy_inflight_ai_quota_usage_is_marked_unknown(tmp_path):
    database = tmp_path / "legacy-quota.sqlite3"
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            stage TEXT NOT NULL
        );
        CREATE TABLE ai_quota_reservations (
            task_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL UNIQUE,
            authorization_id TEXT UNIQUE,
            model TEXT NOT NULL,
            granted_tokens INTEGER,
            status TEXT NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO tasks(task_id, status, stage)
        VALUES ('task_legacy_running', 'running', 'coding');
        INSERT INTO ai_quota_reservations(
            task_id, request_id, authorization_id, model, granted_tokens,
            status, input_tokens, output_tokens, expires_at, created_at, updated_at
        ) VALUES (
            'task_legacy_running', 'task_legacy_running', 'qa_legacy', 'deepseek-pro', 500000,
            'SETTLING', 12, 3, '2026-08-25T12:10:00+08:00',
            '2026-08-25T04:00:00+00:00', '2026-08-25T04:00:00+00:00'
        );
        """
    )

    Storage._migrate_ai_quota_columns(connection)
    row = connection.execute(
        "SELECT * FROM ai_quota_reservations WHERE task_id='task_legacy_running'"
    ).fetchone()
    connection.close()

    assert row["status"] == "USAGE_UNKNOWN"
    assert row["cache_creation_input_tokens"] is None
    assert row["cache_read_input_tokens"] is None


def test_legacy_context_schema_migrates_to_device_id_reconnect(tmp_path):
    database = tmp_path / "aiflow.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE contexts (
            context_id TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE,
            conversation_id TEXT NOT NULL,
            session_id TEXT,
            label TEXT NOT NULL,
            device_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO contexts(
            context_id, token_hash, conversation_id, label, device_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "ctx_legacy",
            hash_token("old-token"),
            "conv_legacy",
            "legacy project",
            json.dumps({"device_id": "device-legacy", "product": "CoreS3"}),
            "2026-07-30T00:00:00+00:00",
            "2026-07-30T00:00:00+00:00",
        ),
    )
    connection.commit()
    connection.close()

    storage = Storage(database)
    migrated = storage.get_context_by_device_id("device-legacy")
    assert migrated["context_id"] == "ctx_legacy"
    assert migrated["device"]["device_id"] == "device-legacy"
    assert "token_hash" not in migrated

    reconnected, created = storage.connect_context(
        "ctx_unused",
        "new-token",
        "conv_unused",
        "reconnected",
        {
            "device_id": "device-legacy",
            "client_id": "client-legacy",
            "product": "CoreS3",
        },
        max_sessions=1,
    )
    assert created is False
    assert reconnected["context_id"] == "ctx_legacy"
    assert storage.count_contexts() == 1
    assert storage.get_context_by_token("old-token") is None
    assert storage.get_context_by_token("new-token")["device_id"] == "device-legacy"
    assert storage.get_context_by_token("new-token")["device"]["client_id"] == "client-legacy"


def test_legacy_device_json_mac_alias_is_normalized(tmp_path):
    database = tmp_path / "aiflow.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE contexts (
            context_id TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE,
            conversation_id TEXT NOT NULL,
            session_id TEXT,
            label TEXT NOT NULL,
            device_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO contexts(
            context_id, token_hash, conversation_id, label, device_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "ctx_legacy_mac",
            hash_token("legacy-mac-token"),
            "conv_legacy_mac",
            "legacy mac project",
            json.dumps({"device_id": "device-legacy-mac", "client_id": "client-legacy", "mac": "AA:BB:CC:DD:EE:FF"}),
            "2026-07-30T00:00:00+00:00",
            "2026-07-30T00:00:00+00:00",
        ),
    )
    connection.commit()
    connection.close()

    context = Storage(database).get_context_by_device_id("device-legacy-mac")
    assert context["device"]["mac_address"] == "AA:BB:CC:DD:EE:FF"
    assert "mac" not in context["device"]


def test_restart_marks_interrupted_task_with_terminal_event(tmp_path):
    database = tmp_path / "aiflow.sqlite3"
    storage = Storage(database)
    storage.connect_context(
        "ctx_restart",
        "context-token",
        "conv_restart",
        "restart fixture",
        {"device_id": "device-restart", "client_id": "client-restart"},
        max_sessions=10,
    )
    task = storage.create_task(
        "task_restart",
        "ctx_restart",
        "stream-token",
        "coding",
        {"prompt": "unfinished", "attachments": []},
    )
    storage.append_event(task["task_id"], "task_started", {"status": "running"})
    storage.update_task(task["task_id"], status="running", stage="coding")

    restarted = Storage(database)

    recovered = restarted.get_task(task["task_id"])
    assert recovered["status"] == "failed"
    assert recovered["stage"] == "server_restarted"
    assert recovered["conversation_id"] == "conv_restart"
    assert recovered["turn_index"] == 1
    assert restarted.last_event(task["task_id"])["type"] == "task_failed"
    assert restarted.last_event(task["task_id"])["data"]["error"]["code"] == "server_restarted"


def test_ai_quota_reservation_survives_restart_for_release_reconciliation(tmp_path):
    database = tmp_path / "aiflow.sqlite3"
    storage = Storage(database)
    storage.connect_context(
        "ctx_quota_restart",
        "context-token",
        "conv_quota_restart",
        "quota restart fixture",
        {
            "device_id": "device-quota-restart",
            "client_id": "client-quota-restart",
            "mac_address": "AA:BB:CC:DD:EE:FF",
        },
        max_sessions=10,
    )
    storage.create_task(
        "task_quota_restart",
        "ctx_quota_restart",
        "stream-token",
        "coding",
        {"prompt": "unfinished quota task", "attachments": []},
    )
    storage.begin_ai_quota_request(
        "task_quota_restart",
        "task_quota_restart",
        "deepseek-pro",
    )
    storage.authorize_ai_quota(
        "task_quota_restart",
        "qa_restart_authorization",
        500000,
        "2026-08-25T12:10:00+08:00",
    )
    storage.update_task("task_quota_restart", status="running", stage="coding")

    restarted = Storage(database)

    open_reservations = restarted.list_open_ai_quota_reservations()
    assert len(open_reservations) == 1
    assert open_reservations[0]["task_id"] == "task_quota_restart"
    assert open_reservations[0]["status"] == "RESERVED"
    restarted.update_ai_quota_status(
        "task_quota_restart",
        "SETTLED",
        input_tokens=12,
        output_tokens=3,
        cache_creation_input_tokens=2,
        cache_read_input_tokens=1,
    )
    saved = restarted.get_ai_quota_reservation("task_quota_restart")
    assert saved["status"] == "SETTLED"
    assert saved["input_tokens"] == 12
    assert saved["output_tokens"] == 3
    assert saved["cache_creation_input_tokens"] == 2
    assert saved["cache_read_input_tokens"] == 1
    assert restarted.list_open_ai_quota_reservations() == []


def test_task_manager_releases_persisted_quota_reservation_after_restart(tmp_path):
    database = tmp_path / "aiflow.sqlite3"
    storage = Storage(database)
    storage.connect_context(
        "ctx_quota_reconcile",
        "context-token",
        "conv_quota_reconcile",
        "quota reconcile fixture",
        {
            "device_id": "device-quota-reconcile",
            "client_id": "client-quota-reconcile",
            "mac_address": "AA:BB:CC:DD:EE:FF",
        },
        max_sessions=10,
    )
    storage.create_task(
        "task_quota_reconcile",
        "ctx_quota_reconcile",
        "stream-token",
        "coding",
        {"prompt": "unfinished quota task", "attachments": []},
    )
    storage.begin_ai_quota_request(
        "task_quota_reconcile",
        "task_quota_reconcile",
        "deepseek-pro",
    )
    storage.authorize_ai_quota(
        "task_quota_reconcile",
        "qa_reconcile_authorization",
        500000,
        "2026-08-25T12:10:00+08:00",
    )
    storage.update_task("task_quota_reconcile", status="running", stage="coding")
    restarted = Storage(database)

    class ReconcileQuotaClient:
        def __init__(self):
            self.calls = []

        async def release(self, authorization, reason):
            self.calls.append((authorization.request_id, authorization.authorization_id, reason))
            return {"status": "RELEASED"}

    base = load_settings()
    settings = replace(
        base,
        data_dir=tmp_path,
        ai_quota=replace(
            base.ai_quota,
            enabled=True,
            hmac_secret="fake-quota-secret-with-at-least-32-bytes",
        ),
    )
    quota = ReconcileQuotaClient()
    manager = TaskManager(
        settings,
        restarted,
        None,
        None,
        None,
        quota_client=quota,
    )

    asyncio.run(manager._reconcile_ai_quota_reservations())

    assert quota.calls == [
        (
            "task_quota_reconcile",
            "qa_reconcile_authorization",
            "AIFLOW_REQUEST_FAILED",
        )
    ]
    assert restarted.get_task("task_quota_reconcile")["error"]["code"] == "server_restarted"
    assert restarted.get_ai_quota_reservation("task_quota_reconcile")["status"] == "RELEASED"


def test_task_manager_does_not_release_unknown_model_usage_after_restart(tmp_path):
    database = tmp_path / "aiflow.sqlite3"
    storage = Storage(database)
    storage.connect_context(
        "ctx_quota_unknown",
        "context-token",
        "conv_quota_unknown",
        "quota unknown fixture",
        {
            "device_id": "device-quota-unknown",
            "client_id": "client-quota-unknown",
            "mac_address": "AA:BB:CC:DD:EE:FF",
        },
        max_sessions=10,
    )
    storage.create_task(
        "task_quota_unknown",
        "ctx_quota_unknown",
        "stream-token",
        "coding",
        {"prompt": "unknown model usage", "attachments": []},
    )
    storage.begin_ai_quota_request(
        "task_quota_unknown",
        "task_quota_unknown",
        "deepseek-pro",
    )
    storage.authorize_ai_quota(
        "task_quota_unknown",
        "qa_unknown_authorization",
        500000,
        "2026-08-25T12:10:00+08:00",
    )
    storage.update_ai_quota_status("task_quota_unknown", "USAGE_UNKNOWN")
    storage.update_task("task_quota_unknown", status="running", stage="coding")
    restarted = Storage(database)

    class ReconcileQuotaClient:
        def __init__(self):
            self.status_calls = []
            self.release_calls = []

        async def status(self, request_id):
            self.status_calls.append(request_id)
            return {
                "requestId": request_id,
                "authorizationId": "qa_unknown_authorization",
                "status": "RESERVED",
                "reservedTokens": 500000,
                "expiresAt": "2026-08-25T12:10:00+08:00",
            }

        async def release(self, authorization, reason):
            self.release_calls.append((authorization.request_id, reason))
            return {"status": "RELEASED"}

    base = load_settings()
    settings = replace(
        base,
        data_dir=tmp_path,
        ai_quota=replace(
            base.ai_quota,
            enabled=True,
            hmac_secret="fake-quota-secret-with-at-least-32-bytes",
        ),
    )
    quota = ReconcileQuotaClient()
    manager = TaskManager(
        settings,
        restarted,
        None,
        None,
        None,
        quota_client=quota,
    )

    asyncio.run(manager._reconcile_ai_quota_reservations())

    assert quota.status_calls == ["task_quota_unknown"]
    assert quota.release_calls == []
    assert restarted.get_ai_quota_reservation("task_quota_unknown")["status"] == "USAGE_UNKNOWN"


def test_task_manager_settles_persisted_model_usage_after_restart(tmp_path):
    database = tmp_path / "aiflow.sqlite3"
    storage = Storage(database)
    storage.connect_context(
        "ctx_quota_settle",
        "context-token",
        "conv_quota_settle",
        "quota settle fixture",
        {
            "device_id": "device-quota-settle",
            "client_id": "client-quota-settle",
            "mac_address": "AA:BB:CC:DD:EE:FF",
        },
        max_sessions=10,
    )
    storage.create_task(
        "task_quota_settle",
        "ctx_quota_settle",
        "stream-token",
        "coding",
        {"prompt": "completed model request", "attachments": []},
    )
    storage.begin_ai_quota_request(
        "task_quota_settle",
        "task_quota_settle",
        "deepseek-pro",
    )
    storage.authorize_ai_quota(
        "task_quota_settle",
        "qa_settle_authorization",
        500000,
        "2026-08-25T12:10:00+08:00",
    )
    storage.update_ai_quota_status(
        "task_quota_settle",
        "SETTLING",
        input_tokens=12,
        output_tokens=3,
        cache_creation_input_tokens=2,
        cache_read_input_tokens=1,
    )
    storage.update_task("task_quota_settle", status="running", stage="coding")
    restarted = Storage(database)

    class ReconcileQuotaClient:
        def __init__(self):
            self.settle_calls = []
            self.release_calls = []

        async def settle(
            self,
            authorization,
            input_tokens,
            output_tokens,
            cache_creation_input_tokens,
            cache_read_input_tokens,
        ):
            self.settle_calls.append(
                (
                    authorization.request_id,
                    authorization.authorization_id,
                    input_tokens,
                    output_tokens,
                    cache_creation_input_tokens,
                    cache_read_input_tokens,
                )
            )
            return {"settled": True}

        async def release(self, authorization, reason):
            self.release_calls.append((authorization.request_id, reason))
            return {"status": "RELEASED"}

    base = load_settings()
    settings = replace(
        base,
        data_dir=tmp_path,
        ai_quota=replace(
            base.ai_quota,
            enabled=True,
            hmac_secret="fake-quota-secret-with-at-least-32-bytes",
        ),
    )
    quota = ReconcileQuotaClient()
    manager = TaskManager(
        settings,
        restarted,
        None,
        None,
        None,
        quota_client=quota,
    )

    asyncio.run(manager._reconcile_ai_quota_reservations())

    assert quota.settle_calls == [
        ("task_quota_settle", "qa_settle_authorization", 12, 3, 2, 1)
    ]
    assert quota.release_calls == []
    assert restarted.get_ai_quota_reservation("task_quota_settle")["status"] == "SETTLED"
