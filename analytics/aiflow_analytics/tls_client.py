from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any

from volcengine.tls.tls_requests import SearchLogsRequest
from volcengine.tls.TLSService import TLSService

from .config import Settings

LOGGER = logging.getLogger(__name__)
SEARCH_LOGS_MAX_LIMIT = 100


class TLSLogClient:
    """Read AIFlow trace records through Volcengine SearchLogsV2."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: TLSService | None = None
        self.last_search_used_fallback = False

    @property
    def configured(self) -> bool:
        return self.settings.tls_configured

    def _get_client(self) -> TLSService:
        if not self.configured:
            raise RuntimeError("Volcengine TLS credentials are not configured")
        if self._client is None:
            self._client = TLSService(
                self.settings.tls_endpoint,
                self.settings.tls_access_key,
                self.settings.tls_secret_key,
                self.settings.tls_region,
                timeout=self.settings.tls_timeout_seconds,
            )
        return self._client

    @staticmethod
    def _error_detail(exc: Exception) -> str:
        error_code = getattr(exc, "error_code", None)
        error_message = getattr(exc, "error_message", None)
        request_id = getattr(exc, "request_id", None)
        if error_code or error_message:
            detail = f"{error_code or type(exc).__name__}: {error_message or ''}"
            if request_id:
                detail += f" (request_id={request_id})"
        else:
            detail = str(exc) or type(exc).__name__
        return " ".join(detail.split())[:1000]

    @staticmethod
    def _identity(log: dict[str, Any]) -> str:
        record_id = str(log.get("record_id") or "")
        if record_id:
            return record_id
        stable = (
            str(log.get("__source__") or ""),
            str(log.get("__package_offset__") or ""),
            str(log.get("event_time_unix_ms") or log.get("__time__") or ""),
        )
        if any(stable):
            return "|".join(stable)
        return hashlib.sha256(
            json.dumps(log, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def _search_pages(
        self,
        start_ms: int,
        end_ms: int,
        query: str,
    ) -> list[dict[str, Any]]:
        client = self._get_client()
        context = ""
        seen_contexts: set[str] = set()
        seen_records: set[str] = set()
        logs: list[dict[str, Any]] = []

        page_limit = min(self.settings.tls_page_size, SEARCH_LOGS_MAX_LIMIT)
        if self.settings.tls_page_size > SEARCH_LOGS_MAX_LIMIT:
            LOGGER.warning(
                "AIFLOW_ANALYTICS_TLS_PAGE_SIZE=%d exceeds the Volcengine SDK limit; using %d",
                self.settings.tls_page_size,
                SEARCH_LOGS_MAX_LIMIT,
            )

        for page in range(1, self.settings.tls_max_pages + 1):
            request = SearchLogsRequest(
                topic_id=self.settings.tls_topic_id,
                query=query,
                limit=page_limit,
                start_time=start_ms,
                end_time=end_ms,
                sort="desc",
                context=context,
            )
            try:
                response = client.search_logs_v2(request)
            except Exception as exc:
                self._client = None
                raise RuntimeError(
                    f"Volcengine TLS search failed: {self._error_detail(exc)}"
                ) from exc

            result = getattr(response, "search_result", None)
            page_logs = list(getattr(result, "logs", None) or [])
            for item in page_logs:
                log = dict(item)
                identity = self._identity(log)
                if identity in seen_records:
                    continue
                seen_records.add(identity)
                logs.append(log)

            next_context = str(getattr(result, "context", None) or "")
            list_over = bool(getattr(result, "list_over", False))
            LOGGER.debug(
                "TLS page=%d fetched=%d unique_total=%d complete=%s",
                page,
                len(page_logs),
                len(logs),
                list_over,
            )
            if list_over or not page_logs or not next_context:
                break
            if next_context in seen_contexts:
                raise RuntimeError("TLS pagination context repeated before completion")
            seen_contexts.add(next_context)
            context = next_context
        else:
            raise RuntimeError(
                "TLS pagination exceeded "
                f"AIFLOW_ANALYTICS_TLS_MAX_PAGES={self.settings.tls_max_pages}"
            )
        return logs

    def search(
        self,
        start_ms: int,
        end_ms: int,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        self.last_search_used_fallback = False
        if start_ms > end_ms:
            return []
        effective_query = query if query is not None else self.settings.tls_query
        logs = self._search_pages(start_ms, end_ms, effective_query)
        if logs or not effective_query.startswith("event:"):
            return logs

        event_name = effective_query.removeprefix("event:").strip()
        if not event_name or any(char.isspace() for char in event_name):
            return logs
        LOGGER.warning(
            "TLS query returned no records for %s; retrying wildcard and filtering locally",
            effective_query,
        )
        self.last_search_used_fallback = True
        fallback_logs = self._search_pages(start_ms, end_ms, "*")
        return [
            log for log in fallback_logs if str(log.get("event") or "") == event_name
        ]


def milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1000)
