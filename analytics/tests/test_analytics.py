from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from aiflow_analytics.analytics import Analytics, Period

from .fixtures import BASE_TIME_MS, complete_turn

DAY_MS = 24 * 60 * 60 * 1000


def test_complex_metrics_trends_comparison_and_breakdowns(database) -> None:
    database.insert_logs(
        complete_turn(
            "turn-current-ok",
            base_time_ms=BASE_TIME_MS,
            conversation_id="conversation-current",
            turn_index=1,
            cost_usd=0.20,
        )
    )
    database.insert_logs(
        complete_turn(
            "turn-current-failed",
            base_time_ms=BASE_TIME_MS + 60_000,
            conversation_id="conversation-current",
            turn_index=2,
            status="failed",
            tool_error=True,
            cost_usd=0.30,
        )
    )
    database.insert_logs(
        complete_turn(
            "turn-previous",
            base_time_ms=BASE_TIME_MS - DAY_MS,
            conversation_id="conversation-previous",
            cost_usd=0.05,
        )
    )
    analytics = Analytics(database, "Asia/Shanghai")
    period = Period(BASE_TIME_MS, BASE_TIME_MS + DAY_MS)

    overview = analytics.overview(period)
    assert overview["volume"]["turns"] == 2
    assert overview["volume"]["conversations"] == 1
    assert overview["volume"]["completion_rate"] == 0.5
    assert overview["usage"]["total_tokens"] == 280
    assert overview["cost"]["total_usd"] == 0.5
    assert overview["tools"]["errors"] == 1
    assert overview["latency_ms"]["agent_p95"] == 9000

    comparison = analytics.compare(period)
    assert comparison["previous"]["volume"]["turns"] == 1
    assert comparison["changes"]["volume.turns"]["delta"] == 1

    trends = analytics.trends(period, "hour")
    assert trends["points"][0]["turns"] == 2
    assert trends["points"][0]["tokens"] == 280

    breakdowns = analytics.breakdowns(period)
    assert breakdowns["models"][0]["turns"] == 2
    assert breakdowns["models"][0]["cost_usd"] == 0.5
    assert breakdowns["tools"][0]["calls"] == 2
    assert breakdowns["tools"][0]["errors"] == 1
    assert {item["value"] for item in breakdowns["statuses"]} == {"completed", "failed"}


def test_conversation_turn_filters_detail_and_quality(database) -> None:
    database.insert_logs(complete_turn("turn-one"))
    analytics = Analytics(database, "Asia/Shanghai")
    period = Period(BASE_TIME_MS, BASE_TIME_MS + DAY_MS)

    conversations = analytics.conversations(period)
    assert conversations["pagination"]["total"] == 1
    assert conversations["items"][0]["turns"] == 1

    turns = analytics.turns(period, status="completed", tool_name="Read")
    assert turns["pagination"]["total"] == 1
    detail = analytics.turn_detail("turn-one")
    assert detail is not None
    assert detail["events"][0]["event_type"] == "user_input"
    assert any(event["event_type"] == "agent_reasoning" for event in detail["events"])
    assert detail["tools"][0]["input"]["file_path"] == "main.py"

    quality = analytics.data_quality(period)
    assert quality["turns"]["missing_terminal"] == 0
    assert quality["turns"]["missing_agent_result"] == 0
    assert quality["records"]["incomplete_events"] == 0


def test_period_labels_use_configured_timezone(database) -> None:
    analytics = Analytics(database, "Asia/Shanghai")
    period = Period(BASE_TIME_MS, BASE_TIME_MS + DAY_MS)
    output = analytics.overview(period)
    expected = datetime.fromtimestamp(BASE_TIME_MS / 1000, tz=ZoneInfo("Asia/Shanghai"))
    assert output["period"]["start"] == expected.isoformat()
