from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from aiflow_analytics.analytics import Analytics, Period

from .fixtures import BASE_TIME_MS, complete_turn, event_records

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
    analytics = Analytics(
        database,
        "Asia/Shanghai",
        model_pricing={
            "claude-sonnet-test": {
                "input": 3.0,
                "output": 15.0,
                "cache_read": 0.30,
                "cache_creation": 3.75,
            }
        },
    )
    period = Period(BASE_TIME_MS, BASE_TIME_MS + DAY_MS)

    overview = analytics.overview(period)
    assert overview["volume"]["turns"] == 2
    assert overview["volume"]["conversations"] == 1
    assert overview["volume"]["completion_rate"] == 0.5
    assert overview["usage"]["total_tokens"] == 360
    assert overview["usage"]["input_tokens"] == 200
    assert overview["usage"]["output_tokens"] == 80
    assert overview["usage"]["cache_read_input_tokens"] == 60
    assert overview["usage"]["cache_creation_input_tokens"] == 20
    assert overview["usage"]["input_tokens_including_cache"] == 280
    assert overview["usage"]["cache_hit_rate"] == 0.214286
    assert overview["volume"]["turns_per_conversation"] == 2.0
    assert overview["volume"]["devices"] == 0
    assert overview["cost"]["total_usd"] == 0.5
    assert overview["cost"]["actual_usd"] == 0.001893
    assert overview["cost"]["actual_source"] == "model_pricing_file"
    assert overview["cost"]["sdk_reported_usd"] == 0.5
    assert overview["cost"]["sdk_reported_source"] == "claude_sdk.total_cost_usd"
    assert overview["cost"]["estimated_breakdown_usd"] == {
        "input_usd": 0.0006,
        "output_usd": 0.0012,
        "cache_read_usd": 0.000018,
        "cache_creation_usd": 0.000075,
    }
    assert overview["cost"]["estimated_usd"] == 0.001893
    assert overview["cost"]["actual_per_turn_usd"] == 0.000946
    assert overview["cost"]["actual_per_conversation_usd"] == 0.001893
    assert overview["tools"]["errors"] == 1
    assert overview["latency_ms"]["agent_p95"] == 9000
    assert overview["latency_ms"]["api_p95"] == 7000
    assert overview["deployment"]["attempts"] == 2
    assert overview["deployment"]["successes"] == 2
    assert overview["deployment"]["success_rate"] == 1.0

    comparison = analytics.compare(period)
    assert comparison["previous"]["volume"]["turns"] == 1
    assert comparison["changes"]["volume.turns"]["delta"] == 1

    trends = analytics.trends(period, "hour")
    assert trends["points"][0]["turns"] == 2
    assert trends["points"][0]["tokens"] == 360

    breakdowns = analytics.breakdowns(period)
    assert breakdowns["models"][0]["turns"] == 2
    assert breakdowns["models"][0]["cost_usd"] == 0.5
    assert breakdowns["models"][0]["total_tokens"] == 360
    assert breakdowns["models"][0]["cache_hit_rate"] == 0.214286
    assert breakdowns["tools"][0]["calls"] == 2
    assert breakdowns["tools"][0]["errors"] == 1
    assert {item["value"] for item in breakdowns["statuses"]} == {"completed", "failed"}

    activity = analytics.recent_activity(period)
    tasks = activity["projects"][0]["conversations"][0]["tasks"]
    assert [task["turn_id"] for task in tasks] == [
        "turn-current-ok",
        "turn-current-failed",
    ]


def test_conversation_turn_filters_detail_and_quality(database) -> None:
    database.insert_logs(complete_turn("turn-one"))
    analytics = Analytics(database, "Asia/Shanghai")
    period = Period(BASE_TIME_MS, BASE_TIME_MS + DAY_MS)

    conversations = analytics.conversations(period)
    assert conversations["pagination"]["total"] == 1
    assert conversations["items"][0]["turns"] == 1
    assert conversations["items"][0]["total_tokens"] == 180
    assert conversations["items"][0]["cache_hit_rate"] == 0.214286

    activity = analytics.recent_activity(period)
    assert activity["pagination"]["total"] == 1
    assert activity["projects"][0]["project_id"] == "project_alpha"
    assert activity["projects"][0]["conversations"][0]["turns"] == 1
    activity_turn = activity["projects"][0]["conversations"][0]["tasks"][0]
    assert activity_turn["user_message"] == "请创建温度仪表盘"
    assert activity_turn["total_tokens"] == 180

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


def test_device_metrics_exclude_missing_mac_and_normalize_formats(database) -> None:
    database.insert_logs(
        complete_turn(
            "turn-device-one",
            conversation_id="conversation-device",
            turn_index=1,
            mac_address="aa-bb-cc-dd-ee-ff",
        )
    )
    database.insert_logs(
        complete_turn(
            "turn-device-two",
            base_time_ms=BASE_TIME_MS + 60_000,
            conversation_id="conversation-device",
            turn_index=2,
            mac_address="AABB.CCDD.EEFF",
        )
    )
    database.insert_logs(
        complete_turn(
            "turn-without-device",
            base_time_ms=BASE_TIME_MS + 120_000,
            conversation_id="conversation-without-device",
        )
    )
    analytics = Analytics(
        database,
        "Asia/Shanghai",
        model_pricing={
            "claude-sonnet-test": {
                "input": 3.0,
                "output": 15.0,
                "cache_read": 0.30,
                "cache_creation": 3.75,
            }
        },
    )
    period = Period(BASE_TIME_MS, BASE_TIME_MS + DAY_MS)

    assert analytics.overview(period)["volume"]["devices"] == 1
    output = analytics.devices(period, page=1, page_size=1)
    assert output["pagination"] == {
        "page": 1,
        "page_size": 1,
        "total": 1,
        "has_next": False,
    }
    device = output["items"][0]
    assert device["mac_address"] == "AA:BB:CC:DD:EE:FF"
    assert device["projects"] == 1
    assert device["conversations"] == 1
    assert device["turns"] == 2
    assert device["total_tokens"] == 360
    assert device["cache_hit_rate"] == 0.214286
    assert device["configured_actual_usd"] == 0.001893
    assert device["sdk_reported_usd"] == 0.24


def test_cost_estimates_match_models_without_cross_model_fallback(database) -> None:
    database.insert_logs(
        complete_turn(
            "turn-deepseek",
            base_time_ms=BASE_TIME_MS,
            model="deepseek-v4-flash-ga-260731",
            conversation_id="conversation-deepseek",
            turn_index=1,
            cost_usd=0.20,
        )
    )
    database.insert_logs(
        complete_turn(
            "turn-other",
            base_time_ms=BASE_TIME_MS + 60_000,
            model="other-model",
            conversation_id="conversation-other",
            turn_index=1,
            cost_usd=0.10,
        )
    )
    analytics = Analytics(
        database,
        "Asia/Shanghai",
        model_pricing={
            "deepseek-v4-flash-ga-260731": {
                "input": 1.0,
                "output": 2.0,
                "cache_read": 0.1,
                "cache_creation": 1.0,
            }
        },
    )
    summary = analytics.overview(Period(BASE_TIME_MS, BASE_TIME_MS + DAY_MS))
    models = {item["model"]: item for item in summary["cost"]["model_estimates"]}
    assert models["deepseek-v4-flash-ga-260731"]["configured"] is True
    assert models["deepseek-v4-flash-ga-260731"]["estimated_usd"] == 0.000193
    assert models["other-model"]["configured"] is False
    assert models["other-model"]["estimated_usd"] is None
    assert summary["cost"]["unpriced_models"] == ["other-model"]
    assert summary["cost"]["actual_usd"] is None
    assert summary["cost"]["estimated_usd"] == 0.000193
    assert summary["cost"]["sdk_reported_usd"] == 0.3


def test_empty_unknown_turn_does_not_block_configured_cost(database) -> None:
    database.insert_logs(
        complete_turn(
            "turn-priced",
            model="deepseek-v4-flash-ga-260731",
            cost_usd=0.2,
        )
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE turn_model_usage SET model=? WHERE turn_id=?",
            ("unknown", "turn-priced"),
        )
    database.insert_logs(
        event_records(
            turn_id="turn-without-model",
            sequence=0,
            event_type="user_input",
            payload={"prompt": "历史残缺记录"},
            conversation_id="conversation-without-model",
        )
    )
    analytics = Analytics(
        database,
        "Asia/Shanghai",
        model_pricing={
            "deepseek-v4-flash-ga-260731": {
                "input": 1.0,
                "output": 2.0,
                "cache_read": 0.1,
                "cache_creation": 1.0,
            }
        },
    )

    summary = analytics.overview(Period(BASE_TIME_MS, BASE_TIME_MS + DAY_MS))

    assert summary["cost"]["pricing_complete"] is True
    assert summary["cost"]["unpriced_models"] == []
    assert (
        summary["cost"]["model_estimates"][0]["model"]
        == "deepseek-v4-flash-ga-260731"
    )
    assert summary["cost"]["actual_usd"] == 0.000193


def test_zero_synthetic_usage_is_retained_but_excluded_from_metrics(database) -> None:
    database.insert_logs(
        complete_turn(
            "turn-with-synthetic",
            model="deepseek-v4-flash-ga-260731",
            cost_usd=0.2,
            mac_address="AA:BB:CC:DD:EE:FF",
        )
    )
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO turn_model_usage(
                turn_id, model, input_tokens, output_tokens,
                cache_read_input_tokens, cache_creation_input_tokens,
                web_search_requests, cost_usd, context_window,
                max_output_tokens, provider, canonical_model
            ) VALUES (?, ?, 0, 0, 0, 0, 0, 0, NULL, NULL, ?, ?)
            """,
            ("turn-with-synthetic", "<synthetic>", "", ""),
        )
    analytics = Analytics(
        database,
        "Asia/Shanghai",
        model_pricing={
            "deepseek-v4-flash-ga-260731": {
                "input": 1.0,
                "output": 2.0,
                "cache_read": 0.1,
                "cache_creation": 1.0,
            }
        },
    )
    period = Period(BASE_TIME_MS, BASE_TIME_MS + DAY_MS)

    summary = analytics.overview(period)
    assert summary["cost"]["pricing_complete"] is True
    assert summary["cost"]["actual_usd"] == 0.000193
    assert [item["model"] for item in summary["cost"]["model_estimates"]] == [
        "deepseek-v4-flash-ga-260731"
    ]
    assert [item["model"] for item in analytics.breakdowns(period)["models"]] == [
        "deepseek-v4-flash-ga-260731"
    ]
    assert analytics.turns(period)["items"][0]["configured_actual_usd"] == 0.000193
    assert (
        analytics.conversations(period)["items"][0]["configured_actual_usd"]
        == 0.000193
    )
    assert analytics.devices(period)["items"][0]["configured_actual_usd"] == 0.000193
    detail = analytics.turn_detail("turn-with-synthetic")
    assert detail is not None
    assert [item["model"] for item in detail["models"]] == [
        "deepseek-v4-flash-ga-260731"
    ]
    assert database.query_one(
        "SELECT COUNT(*) AS count FROM turn_model_usage WHERE model='<synthetic>'"
    ) == {"count": 1}


def test_nonzero_synthetic_usage_remains_visible_and_unpriced(database) -> None:
    database.insert_logs(
        complete_turn(
            "turn-with-billable-synthetic",
            model="deepseek-v4-flash-ga-260731",
        )
    )
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO turn_model_usage(
                turn_id, model, input_tokens, output_tokens,
                cache_read_input_tokens, cache_creation_input_tokens,
                web_search_requests, cost_usd, context_window,
                max_output_tokens, provider, canonical_model
            ) VALUES (?, ?, 1, 0, 0, 0, 0, 0, NULL, NULL, ?, ?)
            """,
            ("turn-with-billable-synthetic", "<synthetic>", "", ""),
        )
    analytics = Analytics(
        database,
        "Asia/Shanghai",
        model_pricing={
            "deepseek-v4-flash-ga-260731": {
                "input": 1.0,
                "output": 2.0,
                "cache_read": 0.1,
                "cache_creation": 1.0,
            }
        },
    )
    period = Period(BASE_TIME_MS, BASE_TIME_MS + DAY_MS)

    summary = analytics.overview(period)
    assert summary["cost"]["pricing_complete"] is False
    assert summary["cost"]["actual_usd"] is None
    assert {item["model"] for item in summary["cost"]["model_estimates"]} == {
        "deepseek-v4-flash-ga-260731",
        "<synthetic>",
    }
    assert {item["model"] for item in analytics.breakdowns(period)["models"]} == {
        "deepseek-v4-flash-ga-260731",
        "<synthetic>",
    }


def test_zero_usage_synthetic_fallback_does_not_block_costs(database) -> None:
    database.insert_logs(
        complete_turn(
            "turn-priced-before-zero",
            model="deepseek-v4-flash-ga-260731",
            cost_usd=0.2,
            mac_address="AA:BB:CC:DD:EE:FF",
        )
    )
    database.insert_logs(
        complete_turn(
            "turn-zero-synthetic-fallback",
            base_time_ms=BASE_TIME_MS + 60_000,
            conversation_id="conversation-zero-synthetic",
            model="deepseek-v4-flash-ga-260731",
            cost_usd=0,
            mac_address="AA:BB:CC:DD:EE:FF",
        )
    )
    with database.connect() as connection:
        connection.execute(
            "DELETE FROM turn_model_usage WHERE turn_id=?",
            ("turn-zero-synthetic-fallback",),
        )
        connection.execute(
            """
            UPDATE turns
            SET primary_model='<synthetic>', input_tokens=0, output_tokens=0,
                cache_read_input_tokens=0, cache_creation_input_tokens=0,
                total_tokens=0, total_cost_usd=0
            WHERE turn_id=?
            """,
            ("turn-zero-synthetic-fallback",),
        )
    analytics = Analytics(
        database,
        "Asia/Shanghai",
        model_pricing={
            "deepseek-v4-flash-ga-260731": {
                "input": 1.0,
                "output": 2.0,
                "cache_read": 0.1,
                "cache_creation": 1.0,
            }
        },
    )
    period = Period(BASE_TIME_MS, BASE_TIME_MS + DAY_MS)

    summary = analytics.overview(period)
    assert summary["cost"]["pricing_complete"] is True
    assert summary["cost"]["actual_usd"] == 0.000193
    assert summary["cost"]["unpriced_models"] == []
    assert [item["model"] for item in analytics.breakdowns(period)["models"]] == [
        "deepseek-v4-flash-ga-260731"
    ]
    zero_turn = analytics.turn_detail("turn-zero-synthetic-fallback")
    assert zero_turn is not None
    assert zero_turn["turn"]["configured_actual_usd"] == 0.0
    assert zero_turn["turn"]["pricing_complete"] is True
    assert zero_turn["models"] == []
    assert analytics.devices(period)["items"][0]["configured_actual_usd"] == 0.000193
    conversations = {
        item["conversation_id"]: item for item in analytics.conversations(period)["items"]
    }
    assert conversations["conversation-zero-synthetic"]["configured_actual_usd"] == 0.0


def test_period_labels_use_configured_timezone(database) -> None:
    analytics = Analytics(database, "Asia/Shanghai")
    period = Period(BASE_TIME_MS, BASE_TIME_MS + DAY_MS)
    output = analytics.overview(period)
    expected = datetime.fromtimestamp(BASE_TIME_MS / 1000, tz=ZoneInfo("Asia/Shanghai"))
    assert output["period"]["start"] == expected.isoformat()


def test_null_model_prices_are_not_treated_as_complete(database) -> None:
    database.insert_logs(complete_turn("turn-unpriced"))
    analytics = Analytics(
        database,
        "Asia/Shanghai",
        model_pricing={
            "claude-sonnet-test": {
                "input": None,
                "output": None,
                "cache_read": None,
                "cache_creation": None,
            }
        },
    )

    summary = analytics.overview(Period(BASE_TIME_MS, BASE_TIME_MS + DAY_MS))

    assert summary["cost"]["pricing_complete"] is False
    assert summary["cost"]["actual_usd"] is None
    assert summary["cost"]["actual_per_turn_usd"] is None
