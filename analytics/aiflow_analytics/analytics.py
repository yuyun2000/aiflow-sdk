from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .database import Database


def _ratio(numerator: float, denominator: float) -> float | None:
    if not denominator:
        return None
    return round(numerator / denominator, 6)


def _average(values: Iterable[float | int | None]) -> float | None:
    selected = [float(value) for value in values if value is not None]
    return round(sum(selected) / len(selected), 3) if selected else None


def _percentile(values: Iterable[float | int | None], percentile: float) -> float | None:
    selected = sorted(float(value) for value in values if value is not None)
    if not selected:
        return None
    position = (len(selected) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(selected[lower], 3)
    weight = position - lower
    return round(selected[lower] * (1 - weight) + selected[upper] * weight, 3)


def _sum(rows: list[dict[str, Any]], field: str) -> int | float:
    return sum(row.get(field) or 0 for row in rows)


def _priced_tokens(tokens: int | float, price: float | None) -> float | None:
    if price is None:
        return None
    return round(float(tokens) * price / 1_000_000, 6)


def _token_usage_fields(row: dict[str, Any]) -> dict[str, Any]:
    input_tokens = int(row.get("input_tokens") or 0)
    output_tokens = int(row.get("output_tokens") or 0)
    cache_read_tokens = int(row.get("cache_read_input_tokens") or 0)
    cache_creation_tokens = int(row.get("cache_creation_input_tokens") or 0)
    total_input_tokens = input_tokens + cache_read_tokens + cache_creation_tokens
    return {
        "input_tokens_including_cache": total_input_tokens,
        "total_tokens": total_input_tokens + output_tokens,
        "cache_hit_rate": _ratio(cache_read_tokens, total_input_tokens),
    }


_UNKNOWN_MODEL_NAMES = {
    "",
    "unknown",
    "none",
    "null",
    "n/a",
    "na",
    "<unknown>",
}


def _model_name(row: dict[str, Any]) -> str:
    for field in ("model", "primary_model", "canonical_model"):
        value = str(row.get(field) or "").strip()
        if value.lower() not in _UNKNOWN_MODEL_NAMES:
            return value
    return "unknown"


@dataclass(frozen=True, slots=True)
class Period:
    start_ms: int
    end_ms: int

    def validate(self) -> None:
        if self.start_ms >= self.end_ms:
            raise ValueError("period start must be before end")

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def as_dict(self, timezone: ZoneInfo) -> dict[str, Any]:
        return {
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "start": datetime.fromtimestamp(self.start_ms / 1000, tz=timezone).isoformat(),
            "end": datetime.fromtimestamp(self.end_ms / 1000, tz=timezone).isoformat(),
        }


class Analytics:
    _PRICE_FIELDS = ("input", "output", "cache_read", "cache_creation")

    def __init__(
        self,
        database: Database,
        timezone_name: str,
        pricing: dict[str, float | None] | None = None,
        model_pricing: dict[str, dict[str, float | None]] | None = None,
    ) -> None:
        self.database = database
        self.timezone = ZoneInfo(timezone_name)
        if model_pricing is not None:
            self.model_pricing = {
                str(model): dict(values) for model, values in model_pricing.items()
            }
        elif pricing is not None:
            # Backwards-compatible explicit wildcard pricing. The application
            # only creates this fallback when legacy env vars are present.
            self.model_pricing = {"*": dict(pricing)}
        else:
            self.model_pricing = {}

    def _pricing_for_model(self, model: str) -> dict[str, float | None] | None:
        prices = self.model_pricing.get(model)
        if prices is not None:
            return prices
        return self.model_pricing.get("*")

    def _model_estimates_from_rows(
        self,
        model_rows: list[dict[str, Any]],
        fallback_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if model_rows:
            covered_turn_ids = {
                str(row["turn_id"])
                for row in model_rows
                if row.get("turn_id") is not None
            }
            if covered_turn_ids:
                model_rows = [
                    *model_rows,
                    *(
                        row
                        for row in fallback_rows
                        if row.get("turn_id") is not None
                        and str(row["turn_id"]) not in covered_turn_ids
                    ),
                ]
        else:
            model_rows = [
                {
                    "model": row.get("primary_model") or "unknown",
                    "canonical_model": row.get("primary_model") or "",
                    "primary_model": row.get("primary_model") or "",
                    "input_tokens": row.get("input_tokens") or 0,
                    "output_tokens": row.get("output_tokens") or 0,
                    "cache_read_input_tokens": row.get("cache_read_input_tokens") or 0,
                    "cache_creation_input_tokens": row.get("cache_creation_input_tokens") or 0,
                }
                for row in fallback_rows
            ]
        grouped: dict[str, dict[str, int]] = {}
        for row in model_rows:
            model = _model_name(row)
            token_fields = (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
            if model == "unknown" and not any(
                int(row.get(field) or 0) for field in token_fields
            ):
                # Incomplete historical turns can have no model and no usage.
                # They must not create a fake unpriced model or block totals.
                continue
            current = grouped.setdefault(
                model,
                {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            )
            for field in current:
                current[field] += int(row.get(field) or 0)

        estimates: list[dict[str, Any]] = []
        for model, counts in sorted(grouped.items()):
            prices = self._pricing_for_model(model)
            breakdown = {
                "input_usd": _priced_tokens(
                    counts["input_tokens"], prices.get("input") if prices else None
                ),
                "output_usd": _priced_tokens(
                    counts["output_tokens"], prices.get("output") if prices else None
                ),
                "cache_read_usd": _priced_tokens(
                    counts["cache_read_input_tokens"],
                    prices.get("cache_read") if prices else None,
                ),
                "cache_creation_usd": _priced_tokens(
                    counts["cache_creation_input_tokens"],
                    prices.get("cache_creation") if prices else None,
                ),
            }
            values = [value for value in breakdown.values() if value is not None]
            configured = bool(prices) and all(
                prices.get(key) is not None for key in self._PRICE_FIELDS
            )
            estimates.append(
                {
                    "model": model,
                    "configured": configured,
                    "tokens": {**counts, **_token_usage_fields(counts)},
                    "unit_prices_usd_per_million": prices or {},
                    "estimated_breakdown_usd": breakdown,
                    "estimated_usd": round(sum(values), 6) if values else None,
                }
            )
        return estimates

    def _model_estimates(
        self,
        period: Period,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        model_rows = self.database.query(
            """
            SELECT m.model, m.canonical_model, t.primary_model,
                   m.turn_id,
                   m.input_tokens, m.output_tokens,
                   m.cache_read_input_tokens, m.cache_creation_input_tokens
            FROM turn_model_usage m
            JOIN turns t ON t.turn_id=m.turn_id
            WHERE t.input_time_ms>=? AND t.input_time_ms<?
            """,
            (period.start_ms, period.end_ms),
        )
        return self._model_estimates_from_rows(model_rows, rows)

    def _cost_fields(
        self,
        model_rows: list[dict[str, Any]],
        fallback_rows: list[dict[str, Any]],
        sdk_reported_usd: float | None,
    ) -> dict[str, Any]:
        estimates = self._model_estimates_from_rows(model_rows, fallback_rows)
        complete = bool(estimates) and all(item["configured"] for item in estimates)
        configured_cost = (
            round(sum(item["estimated_usd"] or 0 for item in estimates), 6)
            if complete
            else None
        )
        return {
            "configured_actual_usd": configured_cost,
            "pricing_complete": complete,
            "sdk_reported_usd": sdk_reported_usd,
        }

    def _turn_cost_fields(self, turn: dict[str, Any]) -> dict[str, Any]:
        model_rows = self.database.query(
            """
            SELECT m.model, m.canonical_model, m.turn_id, t.primary_model,
                   m.input_tokens, m.output_tokens,
                   m.cache_read_input_tokens, m.cache_creation_input_tokens
            FROM turn_model_usage m
            JOIN turns t ON t.turn_id=m.turn_id
            WHERE m.turn_id=?
            """,
            (turn["turn_id"],),
        )
        return self._cost_fields(model_rows, [turn], turn.get("total_cost_usd"))

    def _decorate_turns(self, turns: list[dict[str, Any]]) -> None:
        if not turns:
            return
        model_rows_by_turn: dict[str, list[dict[str, Any]]] = defaultdict(list)
        turn_ids = [str(turn["turn_id"]) for turn in turns]
        for start in range(0, len(turn_ids), 500):
            batch = turn_ids[start : start + 500]
            placeholders = ",".join("?" for _ in batch)
            model_rows = self.database.query(
                f"""
                SELECT m.*, t.primary_model
                FROM turn_model_usage m
                JOIN turns t ON t.turn_id=m.turn_id
                WHERE m.turn_id IN ({placeholders})
                """,
                tuple(batch),
            )
            for row in model_rows:
                model_rows_by_turn[str(row["turn_id"])].append(row)
        for turn in turns:
            turn.update(_token_usage_fields(turn))
            turn.update(
                self._cost_fields(
                    model_rows_by_turn.get(str(turn["turn_id"]), []),
                    [turn],
                    turn.get("total_cost_usd"),
                )
            )

    def _conversation_cost_fields(
        self,
        period: Period,
        conversation_id: str,
        project_id: str,
        sdk_reported_usd: float | None,
    ) -> dict[str, Any]:
        model_rows = self.database.query(
            """
            SELECT m.model, m.canonical_model, m.turn_id, t.primary_model,
                   m.input_tokens, m.output_tokens,
                   m.cache_read_input_tokens, m.cache_creation_input_tokens
            FROM turn_model_usage m
            JOIN turns t ON t.turn_id=m.turn_id
            WHERE t.input_time_ms>=? AND t.input_time_ms<?
              AND t.conversation_id=? AND t.project_id=?
            """,
            (period.start_ms, period.end_ms, conversation_id, project_id),
        )
        fallback_rows = self.database.query(
            """
            SELECT turn_id, primary_model, input_tokens, output_tokens,
                   cache_read_input_tokens, cache_creation_input_tokens
            FROM turns
            WHERE input_time_ms>=? AND input_time_ms<?
              AND conversation_id=? AND project_id=?
            """,
            (period.start_ms, period.end_ms, conversation_id, project_id),
        )
        return self._cost_fields(model_rows, fallback_rows, sdk_reported_usd)

    def _device_cost_fields(
        self,
        period: Period,
        mac_address: str,
        sdk_reported_usd: float | None,
    ) -> dict[str, Any]:
        model_rows = self.database.query(
            """
            SELECT m.model, m.canonical_model, m.turn_id, t.primary_model,
                   m.input_tokens, m.output_tokens,
                   m.cache_read_input_tokens, m.cache_creation_input_tokens
            FROM turn_model_usage m
            JOIN turns t ON t.turn_id=m.turn_id
            WHERE t.input_time_ms>=? AND t.input_time_ms<?
              AND t.mac_address=?
            """,
            (period.start_ms, period.end_ms, mac_address),
        )
        fallback_rows = self.database.query(
            """
            SELECT turn_id, primary_model, input_tokens, output_tokens,
                   cache_read_input_tokens, cache_creation_input_tokens
            FROM turns
            WHERE input_time_ms>=? AND input_time_ms<? AND mac_address=?
            """,
            (period.start_ms, period.end_ms, mac_address),
        )
        return self._cost_fields(model_rows, fallback_rows, sdk_reported_usd)

    def _turn_rows(self, period: Period) -> list[dict[str, Any]]:
        period.validate()
        return self.database.query(
            "SELECT * FROM turns WHERE input_time_ms>=? AND input_time_ms<? ORDER BY input_time_ms",
            (period.start_ms, period.end_ms),
        )

    def _summary(self, rows: list[dict[str, Any]], period: Period) -> dict[str, Any]:
        total = len(rows)
        completed = sum(row["status"] == "completed" for row in rows)
        failed = sum(row["status"] == "failed" for row in rows)
        cancelled = sum(row["status"] == "cancelled" for row in rows)
        incomplete = total - completed - failed - cancelled
        input_tokens = int(_sum(rows, "input_tokens"))
        output_tokens = int(_sum(rows, "output_tokens"))
        cache_read_tokens = int(_sum(rows, "cache_read_input_tokens"))
        cache_creation_tokens = int(_sum(rows, "cache_creation_input_tokens"))
        input_tokens_including_cache = (
            input_tokens + cache_read_tokens + cache_creation_tokens
        )
        total_tokens = input_tokens_including_cache + output_tokens
        cost_values = [
            float(row["total_cost_usd"])
            for row in rows
            if row.get("total_cost_usd") is not None
        ]
        actual_cost = round(sum(cost_values), 6) if cost_values else None
        # Keep total_usd as the established numeric field while exposing whether
        # the SDK returned a billable value for this period.
        cost = actual_cost if actual_cost is not None else 0.0
        model_estimates = self._model_estimates(period, rows)
        estimated_breakdown = {
            field: (
                round(
                    sum(
                        item["estimated_breakdown_usd"][field]
                        for item in model_estimates
                        if item["estimated_breakdown_usd"][field] is not None
                    ),
                    6,
                )
                if any(
                    item["estimated_breakdown_usd"][field] is not None
                    for item in model_estimates
                )
                else None
            )
            for field in (
                "input_usd",
                "output_usd",
                "cache_read_usd",
                "cache_creation_usd",
            )
        }
        estimated_values = [
            value for value in estimated_breakdown.values() if value is not None
        ]
        estimated_total = round(sum(estimated_values), 6) if estimated_values else None
        pricing_complete = bool(model_estimates) and all(
            item["configured"] for item in model_estimates
        )
        configured_actual_cost = (
            round(sum(item["estimated_usd"] or 0 for item in model_estimates), 6)
            if pricing_complete
            else None
        )
        assistant_chars = int(_sum(rows, "assistant_chars"))
        thinking_chars = int(_sum(rows, "thinking_chars"))
        tool_calls = int(_sum(rows, "tool_call_count"))
        tool_results = int(_sum(rows, "tool_result_count"))
        tool_errors = int(_sum(rows, "tool_error_count"))
        deployment_attempts = int(_sum(rows, "deployment_attempted"))
        deployment_successes = int(_sum(rows, "deployment_succeeded"))
        duration_values = [row["duration_ms"] for row in rows]
        service_values = [row["service_duration_ms"] for row in rows]
        queue_values = [row["queue_duration_ms"] for row in rows]
        api_values = [row["duration_api_ms"] for row in rows]
        conversation_count = len(
            {(row["project_id"], row["conversation_id"]) for row in rows}
        )
        project_count = len({row["project_id"] for row in rows})
        device_count = len(
            {str(row["mac_address"]) for row in rows if str(row["mac_address"])}
        )
        return {
            "period": period.as_dict(self.timezone),
            "volume": {
                "turns": total,
                "conversations": conversation_count,
                "projects": project_count,
                "devices": device_count,
                "completed": completed,
                "failed": failed,
                "cancelled": cancelled,
                "incomplete": incomplete,
                "completion_rate": _ratio(completed, total),
                "failure_rate": _ratio(failed, total),
                "turns_per_conversation": _ratio(total, conversation_count),
            },
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "cache_read_input_tokens": cache_read_tokens,
                "cache_creation_input_tokens": cache_creation_tokens,
                "input_tokens_including_cache": input_tokens_including_cache,
                "cache_hit_rate": _ratio(
                    cache_read_tokens, input_tokens_including_cache
                ),
                "tokens_per_turn": _ratio(total_tokens, total),
                "output_input_ratio": _ratio(
                    output_tokens, input_tokens_including_cache
                ),
            },
            "cost": {
                # total_usd is retained for API compatibility and is the SDK's
                # Claude-priced value. actual_usd is the configured model-price total.
                "total_usd": cost,
                "actual_usd": configured_actual_cost,
                "actual_source": "model_pricing_file",
                "configured_actual_usd": configured_actual_cost,
                "sdk_reported_usd": actual_cost,
                "sdk_reported_source": "claude_sdk.total_cost_usd",
                "pricing_complete": pricing_complete,
                "estimated_usd": estimated_total,
                "estimated_breakdown_usd": estimated_breakdown,
                "model_estimates": model_estimates,
                "pricing_models": [
                    item["model"] for item in model_estimates if item["configured"]
                ],
                "unpriced_models": [
                    item["model"] for item in model_estimates if not item["configured"]
                ],
                "pricing_basis": "per_model_configured_unit_prices",
                "per_turn_usd": _ratio(cost, total),
                "per_completed_turn_usd": _ratio(cost, completed),
                "per_1k_tokens_usd": _ratio(cost * 1000, total_tokens),
                "actual_per_turn_usd": (
                    _ratio(configured_actual_cost, total)
                    if configured_actual_cost is not None
                    else None
                ),
                "actual_per_conversation_usd": (
                    _ratio(configured_actual_cost, conversation_count)
                    if configured_actual_cost is not None
                    else None
                ),
                "sdk_reported_per_turn_usd": (
                    _ratio(actual_cost, total) if actual_cost is not None else None
                ),
                "sdk_reported_per_conversation_usd": (
                    _ratio(actual_cost, conversation_count)
                    if actual_cost is not None
                    else None
                ),
            },
            "latency_ms": {
                "agent_avg": _average(duration_values),
                "agent_p50": _percentile(duration_values, 0.50),
                "agent_p95": _percentile(duration_values, 0.95),
                "api_avg": _average(api_values),
                "api_p95": _percentile(api_values, 0.95),
                "queue_avg": _average(queue_values),
                "queue_p95": _percentile(queue_values, 0.95),
                "service_avg": _average(service_values),
                "service_p95": _percentile(service_values, 0.95),
                "api_time_ratio": _ratio(
                    float(_sum(rows, "duration_api_ms")),
                    float(_sum(rows, "duration_ms")),
                ),
            },
            "content": {
                "input_chars": int(_sum(rows, "input_chars")),
                "assistant_messages": int(_sum(rows, "assistant_message_count")),
                "assistant_chars": assistant_chars,
                "thinking_blocks": int(_sum(rows, "thinking_block_count")),
                "thinking_chars": thinking_chars,
                "thinking_output_char_ratio": _ratio(thinking_chars, assistant_chars),
                "partial_turns": sum(bool(row["has_partial"]) for row in rows),
                "attachments": int(_sum(rows, "attachment_count")),
                "files": int(_sum(rows, "file_count")),
            },
            "tools": {
                "calls": tool_calls,
                "results": tool_results,
                "errors": tool_errors,
                "result_rate": _ratio(tool_results, tool_calls),
                "error_rate": _ratio(tool_errors, tool_results),
                "calls_per_turn": _ratio(tool_calls, total),
            },
            "deployment": {
                "attempts": deployment_attempts,
                "successes": deployment_successes,
                "success_rate": _ratio(deployment_successes, deployment_attempts),
            },
        }

    def overview(self, period: Period) -> dict[str, Any]:
        return self._summary(self._turn_rows(period), period)

    @staticmethod
    def _metric(summary: dict[str, Any], path: str) -> float:
        value: Any = summary
        for part in path.split("."):
            value = value[part]
        return float(value or 0)

    def compare(self, period: Period) -> dict[str, Any]:
        period.validate()
        previous = Period(period.start_ms - period.duration_ms, period.start_ms)
        current_summary = self.overview(period)
        previous_summary = self.overview(previous)
        metric_paths = (
            "volume.turns",
            "volume.conversations",
            "volume.projects",
            "volume.turns_per_conversation",
            "volume.completion_rate",
            "usage.total_tokens",
            "usage.output_tokens",
            "usage.cache_hit_rate",
            "cost.actual_usd",
            "cost.actual_per_turn_usd",
            "cost.actual_per_conversation_usd",
            "latency_ms.agent_p95",
            "latency_ms.service_p95",
            "content.thinking_chars",
            "content.assistant_chars",
            "tools.calls",
            "tools.error_rate",
            "deployment.success_rate",
        )
        changes: dict[str, Any] = {}
        for path in metric_paths:
            current = self._metric(current_summary, path)
            prior = self._metric(previous_summary, path)
            changes[path] = {
                "current": current,
                "previous": prior,
                "delta": round(current - prior, 6),
                "change_rate": _ratio(current - prior, abs(prior)),
            }
        return {
            "current": current_summary,
            "previous": previous_summary,
            "changes": changes,
        }

    def _bucket_start(self, timestamp_ms: int, bucket: str) -> datetime:
        value = datetime.fromtimestamp(timestamp_ms / 1000, tz=self.timezone)
        if bucket == "hour":
            return value.replace(minute=0, second=0, microsecond=0)
        if bucket == "week":
            day = value.replace(hour=0, minute=0, second=0, microsecond=0)
            return day - timedelta(days=day.weekday())
        return value.replace(hour=0, minute=0, second=0, microsecond=0)

    def trends(self, period: Period, bucket: str = "day") -> dict[str, Any]:
        if bucket not in {"hour", "day", "week"}:
            raise ValueError("bucket must be hour, day, or week")
        grouped: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
        for row in self._turn_rows(period):
            grouped[self._bucket_start(int(row["input_time_ms"]), bucket)].append(row)
        points = []
        for start, rows in sorted(grouped.items()):
            point_end = start + (
                timedelta(hours=1)
                if bucket == "hour"
                else timedelta(days=7 if bucket == "week" else 1)
            )
            summary = self._summary(
                rows,
                Period(int(start.timestamp() * 1000), int(point_end.timestamp() * 1000)),
            )
            points.append(
                {
                    "bucket": start.isoformat(),
                    "turns": summary["volume"]["turns"],
                    "conversations": summary["volume"]["conversations"],
                    "projects": summary["volume"]["projects"],
                    "completion_rate": summary["volume"]["completion_rate"],
                    "tokens": summary["usage"]["total_tokens"],
                    "cost_usd": summary["cost"]["total_usd"],
                    "configured_actual_usd": summary["cost"]["actual_usd"],
                    "agent_p95_ms": summary["latency_ms"]["agent_p95"],
                    "service_p95_ms": summary["latency_ms"]["service_p95"],
                    "tool_calls": summary["tools"]["calls"],
                    "tool_errors": summary["tools"]["errors"],
                    "thinking_chars": summary["content"]["thinking_chars"],
                    "assistant_chars": summary["content"]["assistant_chars"],
                }
            )
        return {"period": period.as_dict(self.timezone), "bucket": bucket, "points": points}

    def breakdowns(self, period: Period, limit: int = 20) -> dict[str, Any]:
        period.validate()
        params = (period.start_ms, period.end_ms)
        models = self.database.query(
            """
            SELECT m.model, m.provider, m.canonical_model,
                   COUNT(DISTINCT m.turn_id) AS turns,
                   SUM(m.input_tokens) AS input_tokens,
                   SUM(m.output_tokens) AS output_tokens,
                   SUM(m.cache_read_input_tokens) AS cache_read_input_tokens,
                   SUM(m.cache_creation_input_tokens) AS cache_creation_input_tokens,
                   SUM(m.web_search_requests) AS web_search_requests,
                   ROUND(SUM(COALESCE(m.cost_usd, 0)), 6) AS cost_usd
            FROM turn_model_usage m
            JOIN turns t ON t.turn_id=m.turn_id
            WHERE t.input_time_ms>=? AND t.input_time_ms<?
            GROUP BY m.model, m.provider, m.canonical_model
            ORDER BY cost_usd DESC, output_tokens DESC
            LIMIT ?
            """,
            (*params, limit),
        )
        for model in models:
            model.update(_token_usage_fields(model))
            prices = self._pricing_for_model(str(model["model"]))
            model["configured"] = bool(prices) and all(
                prices.get(key) is not None for key in self._PRICE_FIELDS
            )
            model["unit_prices_usd_per_million"] = prices or {}
            model["estimated_breakdown_usd"] = {
                "input_usd": _priced_tokens(
                    model["input_tokens"], prices.get("input") if prices else None
                ),
                "output_usd": _priced_tokens(
                    model["output_tokens"], prices.get("output") if prices else None
                ),
                "cache_read_usd": _priced_tokens(
                    model["cache_read_input_tokens"],
                    prices.get("cache_read") if prices else None,
                ),
                "cache_creation_usd": _priced_tokens(
                    model["cache_creation_input_tokens"],
                    prices.get("cache_creation") if prices else None,
                ),
            }
            model["configured_actual_usd"] = (
                round(
                    sum(
                        value
                        for value in model["estimated_breakdown_usd"].values()
                        if value is not None
                    ),
                    6,
                )
                if model["configured"]
                else None
            )
        tool_rows = self.database.query(
            """
            SELECT c.* FROM tool_calls c
            JOIN turns t ON t.turn_id=c.turn_id
            WHERE t.input_time_ms>=? AND t.input_time_ms<?
            """,
            params,
        )
        grouped_tools: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in tool_rows:
            grouped_tools[(row["tool_type"], row["tool_name"])].append(row)
        tools = []
        for (tool_type, name), rows in grouped_tools.items():
            completed = sum(row["finished_time_ms"] is not None for row in rows)
            errors = sum(bool(row["is_error"]) for row in rows)
            durations = [row["duration_ms"] for row in rows]
            tools.append(
                {
                    "tool_type": tool_type,
                    "tool_name": name,
                    "calls": len(rows),
                    "turns": len({row["turn_id"] for row in rows}),
                    "completed": completed,
                    "errors": errors,
                    "completion_rate": _ratio(completed, len(rows)),
                    "error_rate": _ratio(errors, completed),
                    "duration_avg_ms": _average(durations),
                    "duration_p95_ms": _percentile(durations, 0.95),
                }
            )
        tools.sort(key=lambda item: (-item["calls"], item["tool_name"]))
        dimensions = {}
        period_turns = self._turn_rows(period)
        period_turn_costs = {
            turn["turn_id"]: self._turn_cost_fields(turn) for turn in period_turns
        }
        for name, field in (
            ("statuses", "status"),
            ("turn_kinds", "turn_kind"),
            ("stop_reasons", "stop_reason"),
            ("terminal_reasons", "terminal_reason"),
            ("error_codes", "error_code"),
            ("projects", "project_id"),
        ):
            totals: dict[str, float] = defaultdict(float)
            complete: dict[str, bool] = defaultdict(lambda: True)
            for turn in period_turns:
                value = str(turn.get(field) or "")
                if not value:
                    continue
                configured_cost = period_turn_costs[turn["turn_id"]][
                    "configured_actual_usd"
                ]
                if configured_cost is None:
                    complete[value] = False
                else:
                    totals[value] += configured_cost
            dimensions[name] = self.database.query(
                f"""
                SELECT {field} AS value, COUNT(*) AS turns,
                       SUM(input_tokens + output_tokens
                           + cache_read_input_tokens
                           + cache_creation_input_tokens) AS tokens,
                       ROUND(SUM(COALESCE(total_cost_usd, 0)), 6) AS cost_usd
                FROM turns
                WHERE input_time_ms>=? AND input_time_ms<? AND {field}!=''
                GROUP BY {field}
                ORDER BY turns DESC, value
                LIMIT ?
                """,
                (*params, limit),
            )
            for item in dimensions[name]:
                value = str(item["value"])
                item["configured_actual_usd"] = (
                    round(totals[value], 6) if complete[value] else None
                )
        return {
            "period": period.as_dict(self.timezone),
            "models": models,
            "tools": tools[:limit],
            **dimensions,
        }

    def devices(
        self,
        period: Period,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        period.validate()
        params = (period.start_ms, period.end_ms)
        total_row = self.database.query_one(
            """
            SELECT COUNT(DISTINCT mac_address) AS count
            FROM turns
            WHERE input_time_ms>=? AND input_time_ms<? AND mac_address!=''
            """,
            params,
        )
        offset = (page - 1) * page_size
        items = self.database.query(
            """
            SELECT mac_address,
                   COUNT(DISTINCT project_id) AS projects,
                   COUNT(DISTINCT project_id || X'1F' || conversation_id)
                       AS conversations,
                   COUNT(*) AS turns,
                   SUM(status='completed') AS completed,
                   SUM(status='failed') AS failed,
                   SUM(status='cancelled') AS cancelled,
                   SUM(status NOT IN ('completed', 'failed', 'cancelled')) AS incomplete,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cache_read_input_tokens) AS cache_read_input_tokens,
                   SUM(cache_creation_input_tokens) AS cache_creation_input_tokens,
                   SUM(input_tokens + output_tokens
                       + cache_read_input_tokens
                       + cache_creation_input_tokens) AS total_tokens,
                   ROUND(SUM(total_cost_usd), 6) AS total_cost_usd,
                   SUM(tool_call_count) AS tool_calls,
                   MAX(COALESCE(finished_time_ms, last_event_ms)) AS last_turn_ms
            FROM turns
            WHERE input_time_ms>=? AND input_time_ms<? AND mac_address!=''
            GROUP BY mac_address
            ORDER BY last_turn_ms DESC, mac_address
            LIMIT ? OFFSET ?
            """,
            (*params, page_size, offset),
        )
        for item in items:
            item.update(_token_usage_fields(item))
            item.update(
                self._device_cost_fields(
                    period,
                    str(item["mac_address"]),
                    item.get("total_cost_usd"),
                )
            )
        total = int(total_row["count"] if total_row else 0)
        return {
            "period": period.as_dict(self.timezone),
            "items": items,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "has_next": offset + len(items) < total,
            },
        }

    def conversations(
        self,
        period: Period,
        *,
        page: int = 1,
        page_size: int = 50,
        project_id: str = "",
    ) -> dict[str, Any]:
        period.validate()
        conditions = ["input_time_ms>=?", "input_time_ms<?"]
        params: list[Any] = [period.start_ms, period.end_ms]
        if project_id:
            conditions.append("project_id=?")
            params.append(project_id)
        where = " AND ".join(conditions)
        total_row = self.database.query_one(
            f"""
            SELECT COUNT(*) AS count FROM (
                SELECT 1 FROM turns WHERE {where}
                GROUP BY project_id, conversation_id
            )
            """,
            tuple(params),
        )
        offset = (page - 1) * page_size
        items = self.database.query(
            f"""
            SELECT conversation_id, project_id, MIN(input_time_ms) AS first_turn_ms,
                   MAX(COALESCE(finished_time_ms, last_event_ms)) AS last_turn_ms,
                   COUNT(*) AS turns, MAX(turn_index) AS max_turn_index,
                   SUM(status='completed') AS completed,
                   SUM(status='failed') AS failed,
                   SUM(status='cancelled') AS cancelled,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cache_read_input_tokens) AS cache_read_input_tokens,
                   SUM(cache_creation_input_tokens) AS cache_creation_input_tokens,
                   SUM(input_tokens + output_tokens
                       + cache_read_input_tokens
                       + cache_creation_input_tokens) AS total_tokens,
                   ROUND(SUM(total_cost_usd), 6) AS total_cost_usd,
                   SUM(tool_call_count) AS tool_calls,
                   SUM(thinking_chars) AS thinking_chars,
                   SUM(assistant_chars) AS assistant_chars
            FROM turns WHERE {where}
            GROUP BY conversation_id, project_id
            ORDER BY last_turn_ms DESC
            LIMIT ? OFFSET ?
            """,
            (*params, page_size, offset),
        )
        for item in items:
            item.update(_token_usage_fields(item))
            item.update(
                self._conversation_cost_fields(
                    period,
                    str(item["conversation_id"]),
                    str(item["project_id"]),
                    item.get("total_cost_usd"),
                )
            )
        total = int(total_row["count"] if total_row else 0)
        return {
            "items": items,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "has_next": offset + len(items) < total,
            },
        }

    def recent_activity(
        self,
        period: Period,
        *,
        page: int = 1,
        page_size: int = 12,
    ) -> dict[str, Any]:
        conversation_page = self.conversations(
            period,
            page=page,
            page_size=page_size,
        )
        conversations = conversation_page["items"]
        if not conversations:
            return {"projects": [], "pagination": conversation_page["pagination"]}

        key_conditions = []
        key_params: list[Any] = [period.start_ms, period.end_ms]
        for conversation in conversations:
            key_conditions.append("(project_id=? AND conversation_id=?)")
            key_params.extend(
                [conversation["project_id"], conversation["conversation_id"]]
            )
        turn_rows = self.database.query(
            f"""
            SELECT * FROM turns
            WHERE input_time_ms>=? AND input_time_ms<?
              AND ({" OR ".join(key_conditions)})
            ORDER BY input_time_ms, turn_index, turn_id
            """,
            tuple(key_params),
        )
        self._decorate_turns(turn_rows)

        user_messages: dict[str, str] = {}
        turn_ids = [str(turn["turn_id"]) for turn in turn_rows]
        for start in range(0, len(turn_ids), 500):
            batch = turn_ids[start : start + 500]
            placeholders = ",".join("?" for _ in batch)
            event_rows = self.database.query(
                f"""
                SELECT turn_id, payload_json
                FROM events
                WHERE turn_id IN ({placeholders})
                  AND event_type IN ('user_input', 'direct_deploy_input')
                ORDER BY event_sequence, event_id
                """,
                tuple(batch),
            )
            for event in event_rows:
                turn_id = str(event["turn_id"])
                if turn_id in user_messages:
                    continue
                payload = json.loads(event["payload_json"])
                user_messages[turn_id] = str(payload.get("prompt") or "")

        turns_by_conversation: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for turn in turn_rows:
            turn["user_message"] = user_messages.get(str(turn["turn_id"]), "")
            turns_by_conversation[
                (str(turn["project_id"]), str(turn["conversation_id"]))
            ].append(turn)

        projects_by_id: dict[str, dict[str, Any]] = {}
        for conversation in conversations:
            project_id = str(conversation["project_id"])
            project = projects_by_id.setdefault(
                project_id,
                {
                    "project_id": project_id,
                    "last_turn_ms": conversation["last_turn_ms"],
                    "conversation_count": 0,
                    "turns": 0,
                    "conversations": [],
                },
            )
            project["last_turn_ms"] = max(
                int(project["last_turn_ms"] or 0),
                int(conversation["last_turn_ms"] or 0),
            )
            project["conversation_count"] += 1
            project["turns"] += int(conversation["turns"] or 0)
            conversation["tasks"] = turns_by_conversation.get(
                (project_id, str(conversation["conversation_id"])), []
            )
            project["conversations"].append(conversation)

        projects = sorted(
            projects_by_id.values(),
            key=lambda item: int(item["last_turn_ms"] or 0),
            reverse=True,
        )
        return {"projects": projects, "pagination": conversation_page["pagination"]}

    def turns(
        self,
        period: Period,
        *,
        page: int = 1,
        page_size: int = 50,
        status: str = "",
        project_id: str = "",
        conversation_id: str = "",
        model: str = "",
        tool_name: str = "",
    ) -> dict[str, Any]:
        period.validate()
        conditions = ["t.input_time_ms>=?", "t.input_time_ms<?"]
        params: list[Any] = [period.start_ms, period.end_ms]
        for field, value in (
            ("t.status", status),
            ("t.project_id", project_id),
            ("t.conversation_id", conversation_id),
            ("t.primary_model", model),
        ):
            if value:
                conditions.append(f"{field}=?")
                params.append(value)
        if tool_name:
            conditions.append(
                "EXISTS (SELECT 1 FROM tool_calls c WHERE c.turn_id=t.turn_id AND c.tool_name=?)"
            )
            params.append(tool_name)
        where = " AND ".join(conditions)
        total_row = self.database.query_one(
            f"SELECT COUNT(*) AS count FROM turns t WHERE {where}",
            tuple(params),
        )
        offset = (page - 1) * page_size
        items = self.database.query(
            f"""
            SELECT t.* FROM turns t WHERE {where}
            ORDER BY t.input_time_ms DESC LIMIT ? OFFSET ?
            """,
            (*params, page_size, offset),
        )
        self._decorate_turns(items)
        total = int(total_row["count"] if total_row else 0)
        return {
            "items": items,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "has_next": offset + len(items) < total,
            },
        }

    def turn_detail(self, turn_id: str) -> dict[str, Any] | None:
        turn = self.database.query_one("SELECT * FROM turns WHERE turn_id=?", (turn_id,))
        if not turn:
            return None
        turn.update(_token_usage_fields(turn))
        events = self.database.query(
            """
            SELECT event_id, event_sequence, event_type, event_time_ms,
                   is_terminal, chunk_count, payload_json
            FROM events WHERE turn_id=? ORDER BY event_sequence, event_id
            """,
            (turn_id,),
        )
        for event in events:
            event["payload"] = json.loads(event.pop("payload_json"))
        tools = self.database.query(
            "SELECT * FROM tool_calls WHERE turn_id=? ORDER BY started_time_ms, tool_key",
            (turn_id,),
        )
        for tool in tools:
            for field in ("input_json", "result_json"):
                raw = tool.pop(field)
                tool[field.removesuffix("_json")] = json.loads(raw) if raw else None
        models = self.database.query(
            "SELECT * FROM turn_model_usage WHERE turn_id=? ORDER BY cost_usd DESC, model",
            (turn_id,),
        )
        turn.update(self._turn_cost_fields(turn))
        for model in models:
            model.update(_token_usage_fields(model))
            prices = self._pricing_for_model(str(model["model"]))
            model["configured_actual_usd"] = (
                round(
                    sum(
                        value
                        for value in (
                            _priced_tokens(
                                model["input_tokens"],
                                prices.get("input") if prices else None,
                            ),
                            _priced_tokens(
                                model["output_tokens"],
                                prices.get("output") if prices else None,
                            ),
                            _priced_tokens(
                                model["cache_read_input_tokens"],
                                prices.get("cache_read") if prices else None,
                            ),
                            _priced_tokens(
                                model["cache_creation_input_tokens"],
                                prices.get("cache_creation") if prices else None,
                            ),
                        )
                        if value is not None
                    ),
                    6,
                )
                if prices
                and all(prices.get(key) is not None for key in self._PRICE_FIELDS)
                else None
            )
        return {"turn": turn, "events": events, "tools": tools, "models": models}

    def data_quality(self, period: Period | None = None) -> dict[str, Any]:
        params: tuple[Any, ...] = ()
        where = ""
        if period is not None:
            period.validate()
            where = "WHERE input_time_ms>=? AND input_time_ms<?"
            params = (period.start_ms, period.end_ms)
        turn_quality = self.database.query_one(
            f"""
            SELECT COUNT(*) AS turns,
                   SUM(has_terminal=0) AS missing_terminal,
                   SUM(turn_kind='coding' AND has_agent_result=0) AS missing_agent_result,
                   SUM(has_partial=1) AS partial_turns
            FROM turns {where}
            """,
            params,
        ) or {}
        record_quality = self.database.query_one(
            """
            SELECT COUNT(*) AS physical_records, COUNT(DISTINCT event_id) AS event_groups
            FROM raw_records
            """
        ) or {}
        logical = self.database.query_one("SELECT COUNT(*) AS logical_events FROM events") or {}
        incomplete = self.database.query_one(
            """
            SELECT COUNT(*) AS incomplete_events FROM (
                SELECT event_id FROM raw_records
                GROUP BY event_id HAVING COUNT(*) != MAX(chunk_count)
            )
            """
        ) or {}
        tool_quality = self.database.query_one(
            """
            SELECT SUM(started_time_ms IS NOT NULL AND finished_time_ms IS NULL) AS missing_results,
                   SUM(started_time_ms IS NULL AND finished_time_ms IS NOT NULL) AS orphan_results
            FROM tool_calls
            """
        ) or {}
        error_counts = self.database.query(
            """
            SELECT error_type, COUNT(*) AS unique_errors, SUM(occurrences) AS occurrences
            FROM ingest_errors GROUP BY error_type ORDER BY occurrences DESC
            """
        )
        event_types = self.database.query(
            """
            SELECT event_type, COUNT(*) AS events
            FROM events GROUP BY event_type ORDER BY events DESC, event_type
            """
        )
        duplicates = self.database.query_one(
            "SELECT SUM(duplicate_count) AS count FROM sync_runs WHERE status='completed'"
        ) or {}
        return {
            "turns": turn_quality,
            "records": {
                **record_quality,
                **logical,
                **incomplete,
                "duplicate_fetches": int(duplicates.get("count") or 0),
            },
            "tools": tool_quality,
            "ingest_errors": error_counts,
            "event_types": event_types,
            "sync": self.database.sync_status(),
            "notes": {
                "event_sequence_gaps": "expected because streaming-only events are not uploaded",
                "physical_duplicates": "TLS is at-least-once; record_id is the idempotency key",
            },
        }

    def dashboard(self, period: Period, bucket: str = "day", limit: int = 20) -> dict[str, Any]:
        return {
            "overview": self.overview(period),
            "comparison": self.compare(period),
            "trends": self.trends(period, bucket),
            "breakdowns": self.breakdowns(period, limit),
            "data_quality": self.data_quality(period),
        }
