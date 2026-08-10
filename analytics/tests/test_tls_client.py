from __future__ import annotations

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
