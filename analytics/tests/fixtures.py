from __future__ import annotations

import json
from typing import Any

BASE_TIME_MS = 1_785_988_800_000  # 2026-08-06T12:00:00+08:00


def event_records(
    *,
    turn_id: str,
    sequence: int,
    event_type: str,
    payload: dict[str, Any],
    timestamp_ms: int | None = None,
    project_id: str = "project_alpha",
    conversation_id: str = "conversation_alpha",
    turn_index: int = 1,
    turn_kind: str = "coding",
    chunk_size: int | None = None,
    mac_address: str | None = None,
) -> list[dict[str, Any]]:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    chunks = (
        [encoded[index : index + chunk_size] for index in range(0, len(encoded), chunk_size)]
        if chunk_size
        else [encoded]
    )
    event_id = f"{turn_id}:{sequence:08d}"
    event_time = timestamp_ms if timestamp_ms is not None else BASE_TIME_MS + sequence * 1000
    return [
        {
            "schema_version": "2",
            "event": "aiflow_conversation_trace",
            "record_id": f"{event_id}:{chunk_index:04d}",
            "event_id": event_id,
            "project_id": project_id,
            **({"mac_address": mac_address} if mac_address is not None else {}),
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "turn_index": str(turn_index),
            "turn_kind": turn_kind,
            "event_sequence": str(sequence),
            "event_type": event_type,
            "event_time": "2026-08-06T04:00:00+00:00",
            "event_time_unix_ms": str(event_time),
            "is_terminal": str(
                event_type.startswith("task_") and event_type != "task_started"
            ).lower(),
            "chunk_index": str(chunk_index),
            "chunk_count": str(len(chunks)),
            "payload_encoding": "json_utf8_chunks",
            "payload": chunk,
            "__source__": "aiflow-conversation-test",
            "__package_offset__": str(sequence * 10 + chunk_index),
        }
        for chunk_index, chunk in enumerate(chunks)
    ]


def complete_turn(
    turn_id: str = "turn_alpha",
    *,
    base_time_ms: int = BASE_TIME_MS,
    project_id: str = "project_alpha",
    conversation_id: str = "conversation_alpha",
    turn_index: int = 1,
    status: str = "completed",
    model: str = "claude-sonnet-test",
    tool_error: bool = False,
    cost_usd: float = 0.12,
    mac_address: str | None = None,
) -> list[dict[str, Any]]:
    common = {
        "turn_id": turn_id,
        "project_id": project_id,
        "conversation_id": conversation_id,
        "turn_index": turn_index,
        "mac_address": mac_address,
    }
    events: list[tuple[str, dict[str, Any]]] = [
        ("user_input", {"prompt": "请创建温度仪表盘", "attachments": [{"name": "a.png"}]}),
        ("task_started", {"status": "running", "stage": "preparing_workspace"}),
        (
            "agent_connected",
            {"query": "User request:\n请创建温度仪表盘", "runtime": {"model": model}},
        ),
        (
            "agent_reasoning",
            {
                "response_id": "msg-1",
                "block_index": 0,
                "thinking": "先查文档再写代码",
                "finalized": True,
            },
        ),
        (
            "tool_started",
            {
                "response_id": "msg-1",
                "tool": "Read",
                "tool_use_id": "tool-1",
                "input": {"file_path": "main.py"},
            },
        ),
        (
            "tool_finished",
            {"tool_use_id": "tool-1", "content": "file contents", "is_error": tool_error},
        ),
        (
            "assistant_message",
            {"response_id": "msg-1", "block_index": 2, "text": "代码已经完成", "finalized": True},
        ),
        (
            "assistant_message_finished",
            {
                "response_id": "msg-1",
                "model": model,
                "stop_reason": "end_turn",
                "usage": {"output_tokens": 40},
            },
        ),
        (
            "agent_result",
            {
                "duration_ms": 9000,
                "duration_api_ms": 7000,
                "num_turns": 2,
                "stop_reason": "end_turn",
                "terminal_reason": "completed",
                "total_cost_usd": cost_usd,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 40,
                    "cache_read_input_tokens": 30,
                    "cache_creation_input_tokens": 10,
                },
                "model_usage": {
                    model: {
                        "inputTokens": 100,
                        "outputTokens": 40,
                        "cacheReadInputTokens": 30,
                        "cacheCreationInputTokens": 10,
                        "webSearchRequests": 1,
                        "costUSD": cost_usd,
                        "contextWindow": 200000,
                        "maxOutputTokens": 32000,
                        "provider": "gateway",
                        "canonicalModel": "claude-sonnet-test-canonical",
                    }
                },
            },
        ),
        ("file_ready", {"path": "main.py", "size": 200}),
        ("deployment_started", {"stage": "deploying"}),
        ("deployment_finished", {"result": {"ok": True}}),
    ]
    terminal_payload: dict[str, Any]
    if status == "failed":
        terminal_payload = {"status": "failed", "error": {"code": "fixture_failure"}}
    else:
        terminal_payload = {"status": status}
    events.append((f"task_{status}", terminal_payload))

    records: list[dict[str, Any]] = []
    for sequence, (event_type, payload) in enumerate(events):
        records.extend(
            event_records(
                **common,
                sequence=sequence,
                event_type=event_type,
                payload=payload,
                timestamp_ms=base_time_ms + sequence * 1000,
                chunk_size=12 if event_type == "agent_reasoning" else None,
            )
        )
    return records
