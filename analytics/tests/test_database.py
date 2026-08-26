from __future__ import annotations

import sqlite3

from aiflow_analytics.database import Database

from .fixtures import complete_turn, event_records


def test_database_reconstructs_complete_turn_usage_content_and_tools(database) -> None:
    records = complete_turn(mac_address="aa-bb-cc-dd-ee-ff")
    result = database.insert_logs(reversed(records))

    assert result["inserted"] == len(records)
    assert result["turns"] == 1
    turn = database.query_one("SELECT * FROM turns WHERE turn_id='turn_alpha'")
    assert turn is not None
    assert turn["status"] == "completed"
    assert turn["mac_address"] == "AA:BB:CC:DD:EE:FF"
    assert turn["input_tokens"] == 100
    assert turn["output_tokens"] == 40
    assert turn["total_tokens"] == 180
    assert turn["total_cost_usd"] == 0.12
    assert turn["duration_ms"] == 9000
    assert turn["duration_api_ms"] == 7000
    assert turn["thinking_chars"] == len("先查文档再写代码")
    assert turn["assistant_chars"] == len("代码已经完成")
    assert turn["tool_call_count"] == 1
    assert turn["tool_result_count"] == 1
    assert turn["deployment_succeeded"] == 1
    assert turn["service_duration_ms"] == 12000

    tool = database.query_one("SELECT * FROM tool_calls WHERE turn_id='turn_alpha'")
    assert tool is not None
    assert tool["tool_name"] == "Read"
    assert tool["duration_ms"] == 1000
    model = database.query_one("SELECT * FROM turn_model_usage WHERE turn_id='turn_alpha'")
    assert model is not None
    assert model["output_tokens"] == 40
    assert model["web_search_requests"] == 1

    duplicate = database.insert_logs(records)
    assert duplicate["inserted"] == 0
    assert duplicate["duplicates"] == len(records)
    assert database.query_one("SELECT COUNT(*) AS count FROM turns")["count"] == 1

    raw = database.query_one("SELECT mac_address FROM raw_records LIMIT 1")
    event = database.query_one("SELECT mac_address FROM events LIMIT 1")
    assert raw is not None and raw["mac_address"] == "AA:BB:CC:DD:EE:FF"
    assert event is not None and event["mac_address"] == "AA:BB:CC:DD:EE:FF"


def test_synthetic_error_message_does_not_replace_or_stick_as_primary_model(database) -> None:
    records = []
    for sequence, event_type, payload in (
        (
            0,
            "agent_connected",
            {"runtime": {"model": "deepseek-v4-pro-ga-260813"}},
        ),
        (
            1,
            "assistant_message_finished",
            {"model": "<synthetic>", "stop_reason": "stop_sequence"},
        ),
        (
            2,
            "agent_result_error",
            {
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "model_usage": {},
                "total_cost_usd": 0,
                "terminal_reason": "api_error",
            },
        ),
        (3, "task_failed", {"error": {"code": "quota_denied"}}),
    ):
        records.extend(
            event_records(
                turn_id="turn-synthetic-error",
                sequence=sequence,
                event_type=event_type,
                payload=payload,
            )
        )
    database.insert_logs(records)

    turn = database.query_one(
        "SELECT primary_model FROM turns WHERE turn_id='turn-synthetic-error'"
    )
    assert turn == {"primary_model": "deepseek-v4-pro-ga-260813"}

    with database.connect() as connection:
        connection.execute(
            "UPDATE turns SET primary_model='<synthetic>' WHERE turn_id='turn-synthetic-error'"
        )
    database.initialize()

    repaired = database.query_one(
        "SELECT primary_model FROM turns WHERE turn_id='turn-synthetic-error'"
    )
    assert repaired == {"primary_model": "deepseek-v4-pro-ga-260813"}


def test_incomplete_chunks_wait_for_later_sync(database) -> None:
    records = event_records(
        turn_id="turn-partial",
        sequence=0,
        event_type="user_input",
        payload={"prompt": "很长的中文输入" * 10},
        chunk_size=8,
    )
    first = database.insert_logs(records[:-1])
    assert first["assembled"] == 0
    assert database.query_one("SELECT COUNT(*) AS count FROM events")["count"] == 0
    assert database.sync_status()["incomplete_events"] == 1

    second = database.insert_logs([records[-1]])
    assert second["assembled"] == 1
    assert database.query_one("SELECT COUNT(*) AS count FROM turns")["count"] == 1
    assert database.sync_status()["incomplete_events"] == 0


def test_malformed_trace_is_retained_as_ingest_error(database) -> None:
    malformed = event_records(
        turn_id="turn-bad",
        sequence=0,
        event_type="user_input",
        payload={"prompt": "bad"},
    )[0]
    malformed["chunk_index"] = "9"

    result = database.insert_logs([malformed])
    assert result["errors"] == 1
    error = database.query_one("SELECT * FROM ingest_errors")
    assert error is not None
    assert error["error_type"] == "record_parse"


def test_historical_sync_needed_tracks_clean_completed_days(database) -> None:
    assert database.historical_sync_needed("2026-08-01", "2026-08-03") is True

    database.mark_day_synced(
        "2026-08-01",
        {"fetched": 1, "inserted": 1, "assembled": 1, "errors": 0},
    )
    database.mark_day_synced(
        "2026-08-02",
        {"fetched": 0, "inserted": 0, "assembled": 0, "errors": 1},
    )
    database.mark_day_synced(
        "2026-08-03",
        {"fetched": 1, "inserted": 1, "assembled": 1, "errors": 0},
    )

    assert database.day_is_synced("2026-08-01") is True
    assert database.day_is_synced("2026-08-02") is False
    assert database.historical_sync_needed("2026-08-01", "2026-08-03") is True

    database.mark_day_synced(
        "2026-08-02",
        {"fetched": 1, "inserted": 1, "assembled": 1, "errors": 0},
    )
    assert database.historical_sync_needed("2026-08-01", "2026-08-03") is False


def test_initialize_migrates_legacy_sync_days_and_requires_fallback_validation(
    tmp_path,
    settings,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE sync_days (
                event_date TEXT PRIMARY KEY,
                completed_at TEXT NOT NULL,
                fetched_count INTEGER NOT NULL,
                inserted_count INTEGER NOT NULL,
                assembled_count INTEGER NOT NULL,
                error_count INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO sync_days(
                event_date, completed_at, fetched_count, inserted_count,
                assembled_count, error_count
            ) VALUES ('2026-08-06', '2026-08-17T00:00:00Z', 0, 0, 0, 0)
            """
        )

    database = Database(path, settings.tls_schema_version)
    database.initialize()

    with database.connect() as connection:
        columns = connection.execute("PRAGMA table_info(sync_days)").fetchall()
    assert "fallback_checked" in {str(row["name"]) for row in columns}
    assert database.day_is_synced("2026-08-06") is False
    assert database.historical_sync_needed("2026-08-06", "2026-08-06") is True

    database.mark_day_synced(
        "2026-08-06",
        {"fetched": 0, "inserted": 0, "assembled": 0, "errors": 0},
        fallback_checked=True,
    )
    assert database.day_is_synced("2026-08-06") is True
    assert database.historical_sync_needed("2026-08-06", "2026-08-06") is False


def test_initialize_recalculates_legacy_total_tokens(database) -> None:
    database.insert_logs(complete_turn("turn-legacy-token-total"))
    with database.connect() as connection:
        connection.execute(
            "UPDATE turns SET total_tokens=140 WHERE turn_id=?",
            ("turn-legacy-token-total",),
        )

    database.initialize()

    turn = database.query_one(
        "SELECT total_tokens FROM turns WHERE turn_id=?",
        ("turn-legacy-token-total",),
    )
    assert turn is not None
    assert turn["total_tokens"] == 180


def test_initialize_backfills_mac_from_legacy_raw_json(database) -> None:
    database.insert_logs(
        complete_turn(
            "turn-legacy-mac",
            mac_address="aabbccddeeff",
        )
    )
    with database.connect() as connection:
        for index_name in (
            "idx_raw_records_mac_time",
            "idx_events_mac_time",
            "idx_turns_mac_time",
        ):
            connection.execute(f"DROP INDEX {index_name}")
        for table in ("raw_records", "events", "turns"):
            connection.execute(f"ALTER TABLE {table} DROP COLUMN mac_address")

    database.initialize()

    for table in ("raw_records", "events", "turns"):
        row = database.query_one(f"SELECT mac_address FROM {table} LIMIT 1")
        assert row is not None
        assert row["mac_address"] == "AA:BB:CC:DD:EE:FF"
