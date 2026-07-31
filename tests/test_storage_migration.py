from __future__ import annotations

import json
import sqlite3

from aiflow_server.storage import Storage, hash_token


def test_storage_uses_wal_with_normal_synchronous_mode(tmp_path):
    storage = Storage(tmp_path / "aiflow.sqlite3")

    with storage.connect() as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]

    assert journal_mode == "wal"
    assert synchronous == 1


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
