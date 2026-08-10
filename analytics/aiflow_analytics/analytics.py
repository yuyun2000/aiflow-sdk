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
    def __init__(self, database: Database, timezone_name: str) -> None:
        self.database = database
        self.timezone = ZoneInfo(timezone_name)

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
        total_tokens = int(_sum(rows, "total_tokens"))
        cost = round(float(_sum(rows, "total_cost_usd")), 6)
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
        return {
            "period": period.as_dict(self.timezone),
            "volume": {
                "turns": total,
                "conversations": len({row["conversation_id"] for row in rows}),
                "projects": len({row["project_id"] for row in rows}),
                "completed": completed,
                "failed": failed,
                "cancelled": cancelled,
                "incomplete": incomplete,
                "completion_rate": _ratio(completed, total),
                "failure_rate": _ratio(failed, total),
            },
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "cache_read_input_tokens": int(_sum(rows, "cache_read_input_tokens")),
                "cache_creation_input_tokens": int(
                    _sum(rows, "cache_creation_input_tokens")
                ),
                "tokens_per_turn": _ratio(total_tokens, total),
                "output_input_ratio": _ratio(output_tokens, input_tokens),
            },
            "cost": {
                "total_usd": cost,
                "per_turn_usd": _ratio(cost, total),
                "per_completed_turn_usd": _ratio(cost, completed),
                "per_1k_tokens_usd": _ratio(cost * 1000, total_tokens),
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
            "volume.completion_rate",
            "usage.total_tokens",
            "usage.output_tokens",
            "cost.total_usd",
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
        for name, field in (
            ("statuses", "status"),
            ("turn_kinds", "turn_kind"),
            ("stop_reasons", "stop_reason"),
            ("terminal_reasons", "terminal_reason"),
            ("error_codes", "error_code"),
            ("projects", "project_id"),
        ):
            dimensions[name] = self.database.query(
                f"""
                SELECT {field} AS value, COUNT(*) AS turns,
                       SUM(total_tokens) AS tokens,
                       ROUND(SUM(COALESCE(total_cost_usd, 0)), 6) AS cost_usd
                FROM turns
                WHERE input_time_ms>=? AND input_time_ms<? AND {field}!=''
                GROUP BY {field}
                ORDER BY turns DESC, value
                LIMIT ?
                """,
                (*params, limit),
            )
        return {
            "period": period.as_dict(self.timezone),
            "models": models,
            "tools": tools[:limit],
            **dimensions,
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
            f"SELECT COUNT(DISTINCT conversation_id) AS count FROM turns WHERE {where}",
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
                   SUM(total_tokens) AS total_tokens,
                   ROUND(SUM(COALESCE(total_cost_usd, 0)), 6) AS total_cost_usd,
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
