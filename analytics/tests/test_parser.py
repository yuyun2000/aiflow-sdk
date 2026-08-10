from __future__ import annotations

import pytest

from aiflow_analytics.parser import decode_event_payload, parse_record

from .fixtures import event_records


def test_parser_accepts_schema_v2_and_reassembles_utf8_chunks() -> None:
    logs = event_records(
        turn_id="turn-1",
        sequence=3,
        event_type="agent_reasoning",
        payload={"thinking": "先检查代码，再验证结果"},
        chunk_size=5,
    )
    parsed = [parse_record(log, 2) for log in reversed(logs)]
    records = [item for item in parsed if item is not None]

    assert len(records) > 1
    assert decode_event_payload(records) == {"thinking": "先检查代码，再验证结果"}
    assert records[0].turn_id == "turn-1"


def test_parser_ignores_other_event_names_and_rejects_bad_envelopes() -> None:
    log = event_records(
        turn_id="turn-1",
        sequence=0,
        event_type="user_input",
        payload={"prompt": "hello"},
    )[0]
    unrelated = dict(log, event="http_request")
    assert parse_record(unrelated, 2) is None

    with pytest.raises(ValueError, match="unsupported schema"):
        parse_record(dict(log, schema_version="3"), 2)
    with pytest.raises(ValueError, match="missing envelope"):
        parse_record({key: value for key, value in log.items() if key != "turn_id"}, 2)
