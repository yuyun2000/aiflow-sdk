from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from aiflow_analytics.tls_client import TLSLogClient


class FakeService:
    def __init__(self, pages):
        self.pages = list(pages)
        self.requests = []

    def search_logs_v2(self, request):
        self.requests.append(request)
        return SimpleNamespace(search_result=SimpleNamespace(**self.pages.pop(0)))


def test_tls_search_uses_context_pagination_and_record_id_dedup(settings) -> None:
    duplicate = {"record_id": "record-1", "payload": "{}"}
    service = FakeService(
        [
            {"logs": [duplicate], "context": "next-page", "list_over": False},
            {
                "logs": [duplicate, {"record_id": "record-2", "payload": "{}"}],
                "context": "",
                "list_over": True,
            },
        ]
    )
    client = TLSLogClient(settings)
    client._client = service  # type: ignore[assignment]

    logs = client.search(1, 2)

    assert [item["record_id"] for item in logs] == ["record-1", "record-2"]
    assert len(service.requests) == 2
    assert service.requests[1].context == "next-page"
    assert service.requests[0].limit == 100


def test_tls_search_clamps_legacy_page_size_to_sdk_limit(settings) -> None:
    settings = replace(settings, tls_page_size=1000, tls_query="*")
    service = FakeService([{"logs": [], "context": "", "list_over": True}])
    client = TLSLogClient(settings)
    client._client = service  # type: ignore[assignment]

    client.search(1, 2)

    assert service.requests[0].limit == 100


def test_tls_search_preserves_provider_error_details(settings) -> None:
    class ProviderError(Exception):
        error_code = "InvalidArgument"
        error_message = "limit must be less than or equal to 100"
        request_id = "request-123"

    class ErrorService:
        def search_logs_v2(self, _request):
            raise ProviderError()

    client = TLSLogClient(settings)
    client._client = ErrorService()  # type: ignore[assignment]

    message = "InvalidArgument: limit must be less than or equal to 100"
    with pytest.raises(RuntimeError, match=message):
        client.search(1, 2)


def test_tls_search_falls_back_to_wildcard_for_unindexed_event(settings) -> None:
    class QueryAwareService:
        def __init__(self):
            self.requests = []

        def search_logs_v2(self, request):
            self.requests.append(request)
            if request.query.startswith("event:"):
                return SimpleNamespace(
                    search_result=SimpleNamespace(logs=[], context="", list_over=True)
                )
            return SimpleNamespace(
                search_result=SimpleNamespace(
                    logs=[
                        {"record_id": "trace-1", "event": "aiflow_conversation_trace"},
                        {"record_id": "other-1", "event": "other_event"},
                    ],
                    context="",
                    list_over=True,
                )
            )

    service = QueryAwareService()
    client = TLSLogClient(settings)
    client._client = service  # type: ignore[assignment]

    logs = client.search(1, 2)

    assert [item["record_id"] for item in logs] == ["trace-1"]
    assert [request.query for request in service.requests] == [
        "event:aiflow_conversation_trace",
        "*",
    ]
    assert client.last_search_used_fallback is True


def test_tls_search_rejects_repeated_pagination_context(settings) -> None:
    service = FakeService(
        [
            {"logs": [{"record_id": "one"}], "context": "same", "list_over": False},
            {"logs": [{"record_id": "two"}], "context": "same", "list_over": False},
        ]
    )
    client = TLSLogClient(settings)
    client._client = service  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="context repeated"):
        client.search(1, 2)
