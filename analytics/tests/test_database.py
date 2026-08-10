from __future__ import annotations

from .fixtures import complete_turn, event_records


def test_database_reconstructs_complete_turn_usage_content_and_tools(database) -> None:
    records = complete_turn()
    result = database.insert_logs(reversed(records))

    assert result["inserted"] == len(records)
    assert result["turns"] == 1
    turn = database.query_one("SELECT * FROM turns WHERE turn_id='turn_alpha'")
    assert turn is not None
    assert turn["status"] == "completed"
    assert turn["input_tokens"] == 100
    assert turn["output_tokens"] == 40
    assert turn["total_tokens"] == 140
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
