from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import replace
from types import SimpleNamespace

from aiflow_server.config import TlsLoggingSettings
from aiflow_server.storage import Storage
from aiflow_server.tasks import TaskManager
from aiflow_server.telemetry import TLS_EVENT_DATA_KEY, TlsTelemetry


def telemetry_settings(**overrides) -> TlsLoggingSettings:
    settings = TlsLoggingSettings(
        enabled=True,
        schema_version=2,
        endpoint="tls.example.test",
        region="test-region",
        access_key="fake-access-key",
        secret_key="fake-secret-key",
        topic_id="fake-topic-id",
        source="aiflow-conversation-test",
        filename="conversation-trace.log",
        pseudonym_key="fake-pseudonym-key-with-at-least-32-bytes",
        batch_size=20,
        batch_wait_seconds=0,
        upload_timeout_seconds=1,
        shutdown_timeout_seconds=0,
        retry_base_seconds=0,
        retry_max_seconds=0,
        max_payload_bytes=131072,
    )
    return replace(settings, **overrides)


def create_storage(tmp_path, settings: TlsLoggingSettings) -> Storage:
    storage = Storage(tmp_path / "aiflow.sqlite3", tls_logging=settings)
    storage.connect_context(
        "ctx_private_identifier",
        "ctx_secret_fixture",
        "conv_first",
        "fixture",
        {
            "device_id": "device-private-fixture",
            "client_id": "client-private-fixture",
            "product": "CoreS3",
        },
        10,
    )
    return storage


def read_outbox(database_path):
    with sqlite3.connect(database_path) as db:
        db.row_factory = sqlite3.Row
        return db.execute(
            "SELECT * FROM tls_log_outbox ORDER BY id ASC"
        ).fetchall()


def decoded_contents(rows):
    return [json.loads(row["contents_json"]) for row in rows]


def test_trace_records_capture_input_events_and_multiturn_identity(tmp_path):
    settings = telemetry_settings()
    storage = create_storage(tmp_path, settings)

    first = storage.create_task(
        "task_first",
        "ctx_private_identifier",
        "stream_first",
        "coding",
        {
            "prompt": "画一个温度仪表盘",
            "deploy_mode": "none",
            "attachments": [
                {
                    "kind": "image",
                    "mime_type": "image/png",
                    "name": "参考图.png",
                    "path": "inputs/conv_first/task_first/参考图.png",
                    "size": 123,
                    "sha256": "a" * 64,
                }
            ],
        },
        prompt="画一个温度仪表盘",
    )
    storage.append_event(
        "task_first",
        "tool_started",
        {"tool": "Write", "input": {"file_path": "main.py"}},
    )
    second = storage.create_task(
        "task_second",
        "ctx_private_identifier",
        "stream_second",
        "coding",
        {"prompt": "把字体调大", "deploy_mode": "none", "attachments": []},
        prompt="把字体调大",
    )

    assert first["conversation_id"] == second["conversation_id"] == "conv_first"
    assert first["turn_index"] == 1
    assert second["turn_index"] == 2

    records = decoded_contents(read_outbox(storage.database_path))
    assert [record["event_id"] for record in records] == [
        "task_first:00000000",
        "task_first:00000001",
        "task_second:00000000",
    ]
    assert records[0]["event_type"] == "user_input"
    assert records[0]["turn_id"] == "task_first"
    assert records[0]["turn_index"] == 1
    assert records[2]["turn_index"] == 2
    assert records[0]["project_id"] == records[2]["project_id"]
    assert "ctx_private_identifier" not in json.dumps(records, ensure_ascii=False)
    assert "device-private-fixture" not in json.dumps(records, ensure_ascii=False)

    first_payload = json.loads(records[0]["payload"])
    assert first_payload["prompt"] == "画一个温度仪表盘"
    assert first_payload["attachments"][0]["name"] == "参考图.png"
    tool_payload = json.loads(records[1]["payload"])
    assert tool_payload == {"tool": "Write", "input": {"file_path": "main.py"}}


def test_turn_index_restarts_after_conversation_reset(tmp_path):
    settings = telemetry_settings()
    storage = create_storage(tmp_path, settings)
    storage.create_task(
        "task_first",
        "ctx_private_identifier",
        "stream_first",
        "coding",
        {"prompt": "first", "attachments": []},
    )
    storage.update_conversation("ctx_private_identifier", "conv_second", None)
    task = storage.create_task(
        "task_second",
        "ctx_private_identifier",
        "stream_second",
        "coding",
        {"prompt": "second", "attachments": []},
    )

    assert task["conversation_id"] == "conv_second"
    assert task["turn_index"] == 1
    records = decoded_contents(read_outbox(storage.database_path))
    assert records[0]["project_id"] == records[1]["project_id"]
    assert records[0]["conversation_id"] != records[1]["conversation_id"]


def test_large_utf8_payload_is_losslessly_chunked(tmp_path):
    settings = telemetry_settings(max_payload_bytes=17)
    storage = create_storage(tmp_path, settings)
    request = {"prompt": "你好 UIFlow2 " * 20, "attachments": []}
    storage.create_task(
        "task_chunked",
        "ctx_private_identifier",
        "stream_chunked",
        "coding",
        request,
    )

    contents = decoded_contents(read_outbox(storage.database_path))
    assert len(contents) > 1
    assert [item["chunk_index"] for item in contents] == list(range(len(contents)))
    assert {item["chunk_count"] for item in contents} == {len(contents)}
    rebuilt = "".join(item["payload"] for item in contents)
    assert json.loads(rebuilt) == request
    assert all(len(item["payload"].encode("utf-8")) <= 17 for item in contents)


def test_uploader_deletes_only_acknowledged_records(tmp_path):
    settings = telemetry_settings()
    storage = create_storage(tmp_path, settings)
    storage.create_task(
        "task_upload",
        "ctx_private_identifier",
        "stream_upload",
        "coding",
        {"prompt": "upload me", "attachments": []},
    )
    batches = []
    uploader = TlsTelemetry(
        settings,
        storage.database_path,
        sender=lambda records: batches.append(records),
    )

    assert uploader.flush_once() == 1
    assert len(batches) == 1
    assert batches[0][0].contents["event_id"] == "task_upload:00000000"
    assert read_outbox(storage.database_path) == []


def test_failed_upload_remains_durable_for_next_uploader(tmp_path):
    settings = telemetry_settings()
    storage = create_storage(tmp_path, settings)
    storage.create_task(
        "task_retry",
        "ctx_private_identifier",
        "stream_retry",
        "coding",
        {"prompt": "retry me", "attachments": []},
    )

    def fail(_records):
        raise RuntimeError("temporary failure with fake-secret-key")

    first_uploader = TlsTelemetry(settings, storage.database_path, sender=fail)
    assert first_uploader.flush_once() == 0
    rows = read_outbox(storage.database_path)
    assert len(rows) == 1
    assert rows[0]["attempts"] == 1
    assert "fake-secret-key" not in rows[0]["last_error"]

    uploaded = []
    resumed_uploader = TlsTelemetry(
        settings,
        storage.database_path,
        sender=lambda records: uploaded.extend(records),
    )
    assert resumed_uploader.flush_once() == 1
    assert [record.record_id for record in uploaded] == ["task_retry:00000000:0000"]
    assert read_outbox(storage.database_path) == []


def test_disabled_logging_keeps_outbox_empty(tmp_path):
    settings = replace(telemetry_settings(), enabled=False)
    storage = create_storage(tmp_path, settings)
    storage.create_task(
        "task_disabled",
        "ctx_private_identifier",
        "stream_disabled",
        "coding",
        {"prompt": "local only", "attachments": []},
    )
    storage.append_event("task_disabled", "assistant_message", {"text": "done"})

    assert read_outbox(storage.database_path) == []


def test_final_thinking_is_public_but_tls_uses_complete_private_payload(tmp_path):
    settings = telemetry_settings()
    storage = create_storage(tmp_path, settings)
    storage.create_task(
        "task_thinking",
        "ctx_private_identifier",
        "stream_thinking",
        "coding",
        {"prompt": "think", "attachments": []},
    )
    storage.append_event(
        "task_thinking",
        "agent_reasoning",
        {
            "thinking": "完整的模型思考",
            "finalized": True,
            "response_id": "msg-1",
        },
        telemetry_data={
            "block_type": "thinking",
            "thinking": "完整的模型思考",
            "finalized": True,
            "response_id": "msg-1",
        },
    )

    events = storage.list_events("task_thinking")
    assert events[-1]["data"]["thinking"] == "完整的模型思考"
    records = decoded_contents(read_outbox(storage.database_path))
    thinking_records = [
        record for record in records if record["event_type"] == "agent_reasoning"
    ]
    assert len(thinking_records) == 1
    assert json.loads(thinking_records[0]["payload"])["thinking"] == "完整的模型思考"


def test_task_manager_strips_private_payload_before_public_persistence(tmp_path):
    settings = telemetry_settings()
    storage = create_storage(tmp_path, settings)
    storage.create_task(
        "task_private_boundary",
        "ctx_private_identifier",
        "stream_private_boundary",
        "coding",
        {"prompt": "private", "attachments": []},
    )
    manager = TaskManager(
        SimpleNamespace(max_concurrent_tasks=1),
        storage,
        None,
        None,
        None,
    )

    asyncio.run(
        manager._emit(
            "task_private_boundary",
            "agent_reasoning",
            {
                "thinking": "公开脱敏思考",
                "finalized": True,
                TLS_EVENT_DATA_KEY: {
                    "block_type": "thinking",
                    "thinking": "TLS 完整脱敏思考",
                    "finalized": True,
                },
            },
            agent_event=True,
        )
    )

    public_event = storage.last_event("task_private_boundary")
    assert public_event["data"] == {
        "thinking": "公开脱敏思考",
        "finalized": True,
    }
    assert TLS_EVENT_DATA_KEY not in json.dumps(public_event, ensure_ascii=False)
    tls_records = decoded_contents(read_outbox(storage.database_path))
    reasoning = [item for item in tls_records if item["event_type"] == "agent_reasoning"]
    assert json.loads(reasoning[0]["payload"])["thinking"] == "TLS 完整脱敏思考"


def test_stream_deltas_including_public_reasoning_are_not_uploaded(tmp_path):
    settings = telemetry_settings()
    storage = create_storage(tmp_path, settings)
    storage.create_task(
        "task_deltas",
        "ctx_private_identifier",
        "stream_deltas",
        "coding",
        {"prompt": "stream", "attachments": []},
    )
    initial_count = len(read_outbox(storage.database_path))

    storage.append_event(
        "task_deltas",
        "assistant_text_delta",
        {"text": "一", "finalized": False},
    )
    storage.append_event(
        "task_deltas",
        "agent_reasoning",
        {"thinking": "一段思考", "finalized": False},
    )
    storage.append_event("task_deltas", "task_queued", {"status": "queued"})
    storage.append_event(
        "task_deltas",
        "agent_stream_event",
        {"sdk_event_type": "content_block_start"},
    )
    storage.append_event(
        "task_deltas",
        "assistant_message_started",
        {"response_id": "msg-1"},
    )
    storage.append_event(
        "task_deltas",
        "agent_user_message",
        {"text": "replayed query"},
        telemetry_data={
            "duplicate_of": {
                "event_type": "agent_connected",
                "field": "query",
            }
        },
    )

    assert len(read_outbox(storage.database_path)) == initial_count
    assert [event["type"] for event in storage.list_events("task_deltas")] == [
        "assistant_text_delta",
        "agent_reasoning",
        "task_queued",
        "agent_stream_event",
        "assistant_message_started",
        "agent_user_message",
    ]


def test_coding_completion_uploads_references_instead_of_duplicate_result(tmp_path):
    settings = telemetry_settings()
    storage = create_storage(tmp_path, settings)
    storage.create_task(
        "task_completion",
        "ctx_private_identifier",
        "stream_completion",
        "coding",
        {"prompt": "complete", "attachments": []},
    )
    manager = TaskManager(
        SimpleNamespace(max_concurrent_tasks=1),
        storage,
        None,
        None,
        None,
    )
    public_result = {
        "agent": {"usage": {"output_tokens": 10}},
        "files": [{"path": "main.py", "size": 20}],
        "deployment": {"ok": True},
    }
    telemetry_summary = {
        "status": "completed",
        "stage": "completed",
        "progress": 100,
        "references": {
            "agent_result": True,
            "file_ready_count": 1,
            "deployment_finished": True,
        },
    }

    manager._append_event(
        "task_completion",
        "task_completed",
        {"status": "completed", "result": public_result},
        telemetry_data=telemetry_summary,
    )

    assert storage.last_event("task_completion")["data"]["result"] == public_result
    records = decoded_contents(read_outbox(storage.database_path))
    terminal = [record for record in records if record["event_type"] == "task_completed"]
    assert json.loads(terminal[0]["payload"]) == telemetry_summary


def test_background_worker_uploads_on_dedicated_thread(tmp_path):
    settings = telemetry_settings(shutdown_timeout_seconds=1)
    storage = create_storage(tmp_path, settings)
    storage.create_task(
        "task_background",
        "ctx_private_identifier",
        "stream_background",
        "coding",
        {"prompt": "background", "attachments": []},
    )
    called = threading.Event()
    sender_threads = []

    def sender(_records):
        sender_threads.append(threading.current_thread().name)
        called.set()

    uploader = TlsTelemetry(settings, storage.database_path, sender=sender)
    uploader.start()
    try:
        assert called.wait(timeout=2)
    finally:
        uploader.shutdown()

    assert sender_threads == ["aiflow-tls-uploader"]
    assert read_outbox(storage.database_path) == []
