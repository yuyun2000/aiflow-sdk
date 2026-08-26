from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx
import pytest

from aiflow_server.ai_quota import (
    AiQuotaAuthorization,
    AiQuotaClient,
    AiQuotaDenied,
    AiQuotaError,
    build_hmac_headers,
)
from aiflow_server.config import AiQuotaSettings

SECRET = "fake-quota-hmac-secret-with-at-least-32-bytes"


def quota_settings(**overrides) -> AiQuotaSettings:
    values = {
        "enabled": True,
        "base_url": "https://quota.example.test/m5stack/internal/v1/aiQuota",
        "client_id": "test-aiflow-client",
        "hmac_secret": SECRET,
        "model": "deepseek-pro",
        "timeout_seconds": 1,
        "max_attempts": 2,
    }
    values.update(overrides)
    return AiQuotaSettings(**values)


def assert_request_signature(request: httpx.Request, canonical_path: str) -> None:
    body_hash = hashlib.sha256(request.content).hexdigest()
    assert request.headers["X-Body-SHA256"] == body_hash
    signing_text = "\n".join(
        (
            request.method,
            canonical_path,
            request.headers["X-Timestamp"],
            request.headers["X-Nonce"],
            body_hash,
        )
    )
    expected = hmac.new(
        SECRET.encode("utf-8"),
        signing_text.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert request.headers["X-Signature"] == expected


def test_hmac_headers_use_exact_canonical_text_and_empty_get_hash():
    settings = quota_settings()
    headers = build_hmac_headers(
        settings,
        "GET",
        "/internal/v1/aiQuota/status/task_1",
        b"",
        timestamp=1787295600,
        nonce="fixed-nonce",
    )

    assert headers["X-Body-SHA256"] == hashlib.sha256(b"").hexdigest()
    signing_text = (
        "GET\n/internal/v1/aiQuota/status/task_1\n1787295600\nfixed-nonce\n"
        + hashlib.sha256(b"").hexdigest()
    )
    assert headers["X-Signature"] == hmac.new(
        SECRET.encode("utf-8"),
        signing_text.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def test_authorize_preserves_context_path_and_signs_final_body():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/m5stack/internal/v1/aiQuota/authorize"
        assert_request_signature(request, "/internal/v1/aiQuota/authorize")
        assert json.loads(request.content) == {
            "requestId": "task_123",
            "mac": "AA:BB:CC:DD:EE:FF",
            "model": "deepseek-pro",
        }
        return httpx.Response(
            200,
            json={
                "code": 200,
                "msg": "",
                "data": {
                    "allowed": True,
                    "requestId": "task_123",
                    "authorizationId": "qa_test_authorization",
                    "grantedTokens": 123456,
                    "expiresAt": "2026-08-25T12:10:00+08:00",
                    "quota": {
                        "dailyFreeLimitTokens": 10000000,
                        "lifetimeFreeLimitTokens": 25000000,
                        "dailyFreeAvailableTokens": 1500000,
                        "lifetimeFreeAvailableTokens": 16500000,
                        "effectiveFreeAvailableTokens": 1500000,
                        "paidAvailableTokens": 0,
                        "internalReservationCount": 1,
                    },
                },
            },
        )

    async def exercise():
        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(transport=transport)
        client = AiQuotaClient(
            quota_settings(),
            client=http_client,
            timestamp_factory=lambda: 1787295600,
            nonce_factory=lambda: "fixed-nonce",
        )
        authorization = await client.authorize("task_123", "AA:BB:CC:DD:EE:FF")
        await http_client.aclose()
        return authorization

    authorization = asyncio.run(exercise())
    assert authorization.authorization_id == "qa_test_authorization"
    assert authorization.granted_tokens == 123456
    assert authorization.quota == {
        "dailyFreeLimitTokens": 10000000,
        "lifetimeFreeLimitTokens": 25000000,
        "dailyFreeAvailableTokens": 1500000,
        "lifetimeFreeAvailableTokens": 16500000,
        "effectiveFreeAvailableTokens": 1500000,
        "paidAvailableTokens": 0,
    }
    assert len(requests) == 1


def test_authorize_allows_request_when_response_only_contains_allowed_decision():
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "requestId": "task_allowed_only",
            "mac": "aabbccddeeff",
            "model": "deepseek-pro",
        }
        return httpx.Response(
            200,
            json={
                "code": 200,
                "msg": "",
                "data": {"allowed": True},
            },
        )

    async def exercise():
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = AiQuotaClient(quota_settings(), client=http_client)
        authorization = await client.authorize("task_allowed_only", "aabbccddeeff")
        await http_client.aclose()
        return authorization

    authorization = asyncio.run(exercise())
    assert authorization.request_id == "task_allowed_only"
    assert authorization.authorization_id is None
    assert authorization.granted_tokens is None
    assert authorization.expires_at is None


def test_authorize_preserves_negative_server_available_quota():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 200,
                "msg": "",
                "data": {
                    "allowed": False,
                    "reason": "DAILY_FREE_QUOTA_EXHAUSTED",
                    "requestId": "task_negative_quota",
                    "quota": {
                        "dailyFreeLimitTokens": 2000000,
                        "lifetimeFreeLimitTokens": 5000000,
                        "dailyFreeAvailableTokens": -120,
                        "lifetimeFreeAvailableTokens": 3100000,
                        "effectiveFreeAvailableTokens": -120,
                        "paidAvailableTokens": 0,
                    },
                },
            },
        )

    async def exercise():
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = AiQuotaClient(quota_settings(), client=http_client)
        with pytest.raises(AiQuotaDenied) as captured:
            await client.authorize("task_negative_quota", "aabbccddeeff")
        await http_client.aclose()
        return captured.value

    error = asyncio.run(exercise())
    assert error.quota["dailyFreeAvailableTokens"] == -120
    assert error.quota["effectiveFreeAvailableTokens"] == -120


def test_authorize_timeout_uses_status_before_reusing_reservation():
    paths: list[str] = []
    nonces: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        nonces.append(request.headers["X-Nonce"])
        if request.url.path.endswith("/authorize"):
            raise httpx.ReadTimeout("unknown authorize result", request=request)
        assert request.url.path.endswith("/status/task_timeout")
        assert request.content == b""
        assert_request_signature(request, "/internal/v1/aiQuota/status/task_timeout")
        return httpx.Response(
            200,
            json={
                "code": 200,
                "msg": "",
                "data": {
                    "requestId": "task_timeout",
                    "authorizationId": "qa_timeout",
                    "status": "RESERVED",
                    "reservedTokens": 500000,
                    "expiresAt": "2026-08-25T12:10:00+08:00",
                },
            },
        )

    async def exercise():
        counter = 0

        def nonce() -> str:
            nonlocal counter
            counter += 1
            return f"nonce-{counter}"

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = AiQuotaClient(quota_settings(), client=http_client, nonce_factory=nonce)
        authorization = await client.authorize("task_timeout", "aabbccddeeff")
        await http_client.aclose()
        return authorization

    authorization = asyncio.run(exercise())
    assert authorization.authorization_id == "qa_timeout"
    assert paths == [
        "/m5stack/internal/v1/aiQuota/authorize",
        "/m5stack/internal/v1/aiQuota/status/task_timeout",
    ]
    assert nonces == ["nonce-1", "nonce-2"]


def test_settle_retries_identical_business_body_with_fresh_nonce():
    bodies: list[bytes] = []
    nonces: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/settle")
        assert_request_signature(request, "/internal/v1/aiQuota/settle")
        bodies.append(request.content)
        nonces.append(request.headers["X-Nonce"])
        if len(bodies) == 1:
            raise httpx.ReadTimeout("unknown settle result", request=request)
        return httpx.Response(
            200,
            json={
                "code": 200,
                "msg": "",
                "data": {
                    "settled": True,
                    "requestId": "task_settle",
                    "inputTokens": 10,
                    "outputTokens": 5,
                    "cacheCreationInputTokens": 2,
                    "cacheReadInputTokens": 3,
                    "actualTokens": 15,
                    "releasedTokens": 499985,
                },
            },
        )

    async def exercise():
        counter = 0

        def nonce() -> str:
            nonlocal counter
            counter += 1
            return f"settle-nonce-{counter}"

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = AiQuotaClient(quota_settings(), client=http_client, nonce_factory=nonce)
        authorization = AiQuotaAuthorization(
            request_id="task_settle",
            authorization_id="qa_settle",
            granted_tokens=500000,
            expires_at="2026-08-25T12:10:00+08:00",
            quota={},
        )
        result = await client.settle(authorization, 10, 5, 2, 3)
        await http_client.aclose()
        return result

    result = asyncio.run(exercise())
    assert result["settled"] is True
    assert json.loads(bodies[0])["cacheReadInputTokens"] == 3
    assert bodies[0] == bodies[1]
    assert nonces == ["settle-nonce-1", "settle-nonce-2"]


def test_settle_mismatched_success_response_recovers_from_status():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/settle"):
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "msg": "",
                    "data": {
                        "settled": True,
                        "requestId": "wrong-task",
                        "actualTokens": 15,
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "code": 200,
                "msg": "",
                "data": {
                    "requestId": "task_settle_status",
                    "authorizationId": "qa_settle_status",
                    "status": "SETTLED",
                    "actualTokens": 15,
                    "reservedTokens": 500000,
                    "expiresAt": "2026-08-25T12:10:00+08:00",
                },
            },
        )

    async def exercise():
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = AiQuotaClient(quota_settings(), client=http_client)
        authorization = AiQuotaAuthorization(
            request_id="task_settle_status",
            authorization_id="qa_settle_status",
            granted_tokens=500000,
            expires_at="2026-08-25T12:10:00+08:00",
            quota={},
        )
        result = await client.settle(authorization, 10, 5, 2, 3)
        await http_client.aclose()
        return result

    result = asyncio.run(exercise())
    assert result["settled"] is True
    assert result["confirmedByStatus"] is True
    assert paths == [
        "/m5stack/internal/v1/aiQuota/settle",
        "/m5stack/internal/v1/aiQuota/status/task_settle_status",
    ]


def test_settle_rejects_response_with_mismatched_cache_usage():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/settle"):
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "msg": "",
                    "data": {
                        "settled": True,
                        "requestId": "task_cache_mismatch",
                        "inputTokens": 10,
                        "outputTokens": 5,
                        "cacheCreationInputTokens": 1,
                        "cacheReadInputTokens": 3,
                        "actualTokens": 15,
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "code": 200,
                "msg": "",
                "data": {
                    "requestId": "task_cache_mismatch",
                    "authorizationId": "qa_cache_mismatch",
                    "status": "RESERVED",
                    "reservedTokens": 500000,
                    "expiresAt": "2026-08-25T12:10:00+08:00",
                },
            },
        )

    async def exercise():
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = AiQuotaClient(quota_settings(), client=http_client)
        authorization = AiQuotaAuthorization(
            request_id="task_cache_mismatch",
            authorization_id="qa_cache_mismatch",
            granted_tokens=500000,
            expires_at="2026-08-25T12:10:00+08:00",
            quota={},
        )
        with pytest.raises(AiQuotaError) as captured:
            await client.settle(authorization, 10, 5, 2, 3)
        await http_client.aclose()
        return captured.value

    error = asyncio.run(exercise())
    assert error.code == "ai_quota_invalid_response"


def test_settle_reports_actual_usage_even_when_it_exceeds_legacy_granted_tokens():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "code": 200,
                "msg": "",
                "data": {
                    "settled": True,
                    "requestId": "task_over_limit",
                    "inputTokens": payload["inputTokens"],
                    "outputTokens": payload["outputTokens"],
                    "cacheCreationInputTokens": payload["cacheCreationInputTokens"],
                    "cacheReadInputTokens": payload["cacheReadInputTokens"],
                    "actualTokens": payload["inputTokens"] + payload["outputTokens"],
                },
            },
        )

    async def exercise():
        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        client = AiQuotaClient(quota_settings(), client=http_client)
        authorization = AiQuotaAuthorization(
            request_id="task_over_limit",
            authorization_id="qa_over_limit",
            granted_tokens=12,
            expires_at="2026-08-25T12:10:00+08:00",
            quota={},
        )
        result = await client.settle(authorization, 10, 5, 2, 3)
        await http_client.aclose()
        return result

    result = asyncio.run(exercise())
    assert result["actualTokens"] == 15
    assert len(requests) == 1


def test_release_validates_authorization_identity():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/release"):
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "msg": "",
                    "data": {
                        "requestId": "wrong-task",
                        "authorizationId": "qa_release",
                        "status": "RELEASED",
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "code": 200,
                "msg": "",
                "data": {
                    "requestId": "task_release",
                    "authorizationId": "qa_release",
                    "status": "RELEASED",
                    "reservedTokens": 500000,
                    "expiresAt": "2026-08-25T12:10:00+08:00",
                },
            },
        )

    async def exercise():
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = AiQuotaClient(quota_settings(), client=http_client)
        authorization = AiQuotaAuthorization(
            request_id="task_release",
            authorization_id="qa_release",
            granted_tokens=500000,
            expires_at="2026-08-25T12:10:00+08:00",
            quota={},
        )
        result = await client.release(authorization, "AIFLOW_REQUEST_FAILED")
        await http_client.aclose()
        return result

    result = asyncio.run(exercise())
    assert result["status"] == "RELEASED"
    assert result["confirmedByStatus"] is True


def test_quota_denial_is_a_normal_fail_closed_decision():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 200,
                "msg": "",
                "data": {
                    "allowed": False,
                    "reason": "DAILY_FREE_QUOTA_EXHAUSTED",
                    "requestId": "task_denied",
                    "quota": {
                        "dailyFreeLimitTokens": 10000000,
                        "lifetimeFreeLimitTokens": 25000000,
                        "dailyFreeAvailableTokens": 0,
                        "lifetimeFreeAvailableTokens": 23200000,
                        "effectiveFreeAvailableTokens": 0,
                        "paidAvailableTokens": 0,
                    },
                },
            },
        )

    async def exercise():
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = AiQuotaClient(quota_settings(), client=http_client)
        with pytest.raises(AiQuotaDenied) as captured:
            await client.authorize("task_denied", "aabbccddeeff")
        await http_client.aclose()
        return captured.value

    error = asyncio.run(exercise())
    assert error.reason == "DAILY_FREE_QUOTA_EXHAUSTED"
    assert error.quota["dailyFreeLimitTokens"] == 10000000
    assert error.quota["lifetimeFreeLimitTokens"] == 25000000
    assert error.quota["effectiveFreeAvailableTokens"] == 0
