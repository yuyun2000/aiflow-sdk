from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from aiflow_server.ai_quota import AiQuotaAuthorization, AiQuotaDenied, AiQuotaError
from aiflow_server.config import load_settings
from aiflow_server.model_proxy import ModelProxyError, ModelProxyRegistry
from aiflow_server.storage import Storage


class FakeQuotaClient:
    def __init__(
        self,
        *,
        deny: bool = False,
        settle_error: AiQuotaError | None = None,
        granted_tokens: int | None = 1,
    ):
        self.deny = deny
        self.settle_error = settle_error
        self.granted_tokens = granted_tokens
        self.authorize_calls = []
        self.settle_calls = []

    async def authorize(self, request_id, mac):
        self.authorize_calls.append((request_id, mac))
        if self.deny:
            raise AiQuotaDenied(
                "DAILY_FREE_QUOTA_EXHAUSTED",
                {
                    "dailyFreeLimitTokens": 10_000_000,
                    "lifetimeFreeLimitTokens": 25_000_000,
                    "dailyFreeAvailableTokens": 0,
                    "lifetimeFreeAvailableTokens": 20_000_000,
                    "effectiveFreeAvailableTokens": 0,
                    "paidAvailableTokens": 0,
                },
            )
        return AiQuotaAuthorization(
            request_id=request_id,
            authorization_id=f"qa_{len(self.authorize_calls)}",
            granted_tokens=self.granted_tokens,
            expires_at=None,
            quota={"effectiveFreeAvailableTokens": 9_000_000},
        )

    async def settle(
        self,
        authorization,
        input_tokens,
        output_tokens,
        cache_creation_input_tokens,
        cache_read_input_tokens,
    ):
        self.settle_calls.append(
            (
                authorization.request_id,
                input_tokens,
                output_tokens,
                cache_creation_input_tokens,
                cache_read_input_tokens,
            )
        )
        if self.settle_error:
            raise self.settle_error
        return {
            "settled": True,
            "actualTokens": input_tokens + output_tokens,
            "effectiveFreeAvailableTokens": 8_999_000,
        }


def make_registry(tmp_path, monkeypatch, upstream_handler, quota=None):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://models.example.test/provider-prefix")
    base = load_settings()
    settings = replace(
        base,
        data_dir=tmp_path,
        port=18880,
        ai_quota=replace(
            base.ai_quota,
            enabled=True,
            hmac_secret="fake-quota-secret-with-at-least-32-bytes",
        ),
    )
    storage = Storage(settings.database_path)
    storage.connect_context(
        "ctx_proxy",
        "context-token",
        "conv_proxy",
        "proxy fixture",
        {
            "device_id": "device-proxy",
            "client_id": "client-proxy",
            "mac_address": "AA:BB:CC:DD:EE:FF",
        },
        max_sessions=10,
    )
    storage.create_task(
        "task_proxy",
        "ctx_proxy",
        "stream-token",
        "coding",
        {"prompt": "proxy fixture", "attachments": []},
    )
    quota = quota or FakeQuotaClient()
    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream_handler))
    registry = ModelProxyRegistry(
        settings,
        storage,
        quota,
        upstream_client=upstream_client,
    )
    events = []

    async def emit(event_type, data):
        events.append((event_type, data))

    session = registry.create_session("task_proxy", "AA:BB:CC:DD:EE:FF", emit)
    return registry, session, storage, quota, upstream_client, events


def sse_response(input_tokens, output_tokens, cache_creation=0, cache_read=0):
    frames = [
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": input_tokens,
                    "cache_creation_input_tokens": cache_creation,
                    "cache_read_input_tokens": cache_read,
                }
            },
        },
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "message_delta", "usage": {"output_tokens": output_tokens}},
        {"type": "message_stop"},
    ]
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
        for event in frames
    )


async def read_proxy_response(response):
    if response.body_iterator is None:
        return response.content or b""
    return b"".join([chunk async for chunk in response.body_iterator])


def test_each_messages_request_authorizes_and_settles_its_own_sse_usage(tmp_path, monkeypatch):
    upstream_requests = []
    responses = iter(
        [
            sse_response(4, 5, cache_creation=12, cache_read=30),
            sse_response(3, 4, cache_creation=0, cache_read=21),
        ]
    )

    def upstream(request):
        upstream_requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=next(responses),
        )

    async def exercise():
        registry, session, storage, quota, client, events = make_registry(
            tmp_path, monkeypatch, upstream
        )
        try:
            for max_tokens in (64, 32):
                body = json.dumps(
                    {"model": "provider-model", "max_tokens": max_tokens, "messages": []}
                ).encode()
                response = await registry.proxy(
                    session,
                    "POST",
                    "provider-prefix/v1/messages",
                    "beta=true",
                    {"authorization": "Bearer fake-model-token"},
                    body,
                )
                streamed = await read_proxy_response(response)
                assert b"message_stop" in streamed
            return storage, quota, events
        finally:
            await registry.remove(session, "AIFLOW_REQUEST_FAILED")
            await client.aclose()

    storage, quota, events = asyncio.run(exercise())

    assert [request.url.path for request in upstream_requests] == [
        "/provider-prefix/v1/messages",
        "/provider-prefix/v1/messages",
    ]
    assert [request.url.query for request in upstream_requests] == [b"beta=true", b"beta=true"]
    assert all(request.headers["authorization"] == "Bearer fake-model-token" for request in upstream_requests)
    assert [call[0] for call in quota.authorize_calls] == [
        "task_proxy:model:1",
        "task_proxy:model:2",
    ]
    assert quota.settle_calls == [
        ("task_proxy:model:1", 46, 5, 12, 30),
        ("task_proxy:model:2", 24, 4, 0, 21),
    ]
    assert [row["status"] for row in storage.list_ai_quota_reservations("task_proxy")] == [
        "SETTLED",
        "SETTLED",
    ]
    assert [name for name, _ in events].count("ai_quota_authorizing") == 2
    assert [name for name, _ in events].count("ai_quota_authorized") == 2
    assert [name for name, _ in events].count("ai_quota_settled") == 2


def test_quota_denial_never_reaches_model_upstream(tmp_path, monkeypatch):
    upstream_calls = []

    def upstream(request):
        upstream_calls.append(request)
        return httpx.Response(200, content=b"should not happen")

    async def exercise():
        quota = FakeQuotaClient(deny=True)
        registry, session, storage, _, client, _ = make_registry(
            tmp_path, monkeypatch, upstream, quota
        )
        body = json.dumps({"max_tokens": 16, "messages": []}).encode()
        try:
            with pytest.raises(ModelProxyError) as captured:
                await registry.proxy(
                    session,
                    "POST",
                    "provider-prefix/v1/messages",
                    "",
                    {},
                    body,
                )
            return captured.value, storage, quota
        finally:
            await registry.remove(session, "AIFLOW_REQUEST_FAILED")
            await client.aclose()

    error, storage, quota = asyncio.run(exercise())
    assert error.code == "ai_quota_denied"
    assert upstream_calls == []
    assert len(quota.authorize_calls) == 1
    assert storage.get_ai_quota_reservation("task_proxy")["status"] == "DENIED"


def test_explicit_upstream_failure_records_no_usage_without_release(tmp_path, monkeypatch):
    def upstream(_request):
        return httpx.Response(503, json={"type": "error", "error": {"message": "unavailable"}})

    async def exercise():
        registry, session, storage, quota, client, _ = make_registry(
            tmp_path, monkeypatch, upstream
        )
        body = json.dumps({"max_tokens": 16, "messages": []}).encode()
        try:
            response = await registry.proxy(
                session,
                "POST",
                "provider-prefix/v1/messages",
                "",
                {},
                body,
            )
            assert response.status_code == 503
            return storage, quota
        finally:
            await registry.remove(session, "AIFLOW_REQUEST_FAILED")
            await client.aclose()

    storage, quota = asyncio.run(exercise())
    assert quota.settle_calls == []
    assert storage.get_ai_quota_reservation("task_proxy")["status"] == "NO_USAGE"


def test_missing_usage_is_kept_unknown_and_not_released(tmp_path, monkeypatch):
    def upstream(_request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b"event: message_start\n"
                b'data: {"type":"message_start","message":{"usage":'
                b'{"input_tokens":10,"output_tokens":1}}}\n\n'
                b"event: message_stop\n"
                b'data: {"type":"message_stop"}\n\n'
            ),
        )

    async def exercise():
        registry, session, storage, quota, client, _ = make_registry(
            tmp_path, monkeypatch, upstream
        )
        body = json.dumps({"max_tokens": 16, "messages": []}).encode()
        try:
            response = await registry.proxy(
                session,
                "POST",
                "provider-prefix/v1/messages",
                "",
                {},
                body,
            )
            streamed = await read_proxy_response(response)
            assert b"message_stop" in streamed
            return storage, quota
        finally:
            await registry.remove(session, "AIFLOW_REQUEST_FAILED")
            await client.aclose()

    storage, quota = asyncio.run(exercise())
    assert quota.settle_calls == []
    assert storage.get_ai_quota_reservation("task_proxy")["status"] == "USAGE_UNKNOWN"


def test_settlement_failure_does_not_block_the_response_or_next_model_request(
    tmp_path, monkeypatch
):
    upstream_calls = []

    def upstream(request):
        upstream_calls.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_response(4, 2),
        )

    async def exercise():
        quota = FakeQuotaClient(
            settle_error=AiQuotaError(
                "ai_quota_transport_error",
                "settlement unavailable",
                retryable=True,
            )
        )
        registry, session, storage, _, client, events = make_registry(
            tmp_path, monkeypatch, upstream, quota
        )
        body = json.dumps({"max_tokens": 16, "messages": []}).encode()
        try:
            first = await registry.proxy(
                session,
                "POST",
                "provider-prefix/v1/messages",
                "",
                {},
                body,
            )
            assert b"message_stop" in await read_proxy_response(first)
            second = await registry.proxy(
                session,
                "POST",
                "provider-prefix/v1/messages",
                "",
                {},
                body,
            )
            assert b"message_stop" in await read_proxy_response(second)
            session.raise_if_failed()
            return storage, quota, events
        finally:
            await registry.remove(session, "AIFLOW_REQUEST_FAILED")
            await client.aclose()

    storage, quota, events = asyncio.run(exercise())
    assert len(upstream_calls) == 2
    assert len(quota.authorize_calls) == 2
    assert len(quota.settle_calls) == 2
    assert [row["status"] for row in storage.list_ai_quota_reservations("task_proxy")] == [
        "SETTLING",
        "SETTLING",
    ]
    assert [name for name, _ in events].count("ai_quota_settlement_pending") == 2


def test_non_messages_request_bypasses_quota_but_preserves_base_path(tmp_path, monkeypatch):
    upstream_calls = []

    def upstream(request):
        upstream_calls.append(request)
        return httpx.Response(200, json={"data": []})

    async def exercise():
        registry, session, storage, quota, client, _ = make_registry(
            tmp_path, monkeypatch, upstream
        )
        try:
            response = await registry.proxy(
                session,
                "GET",
                "provider-prefix/v1/models",
                "limit=10",
                {},
                b"",
            )
            return response, storage, quota
        finally:
            await registry.remove(session, "AIFLOW_REQUEST_FAILED")
            await client.aclose()

    response, storage, quota = asyncio.run(exercise())
    assert response.status_code == 200
    assert upstream_calls[0].url.path == "/provider-prefix/v1/models"
    assert quota.authorize_calls == []
    assert storage.list_ai_quota_reservations("task_proxy") == []
