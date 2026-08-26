from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from .config import AiQuotaSettings

CANONICAL_PREFIX = "/internal/v1/aiQuota"


class AiQuotaError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        service_error_code: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.service_error_code = service_error_code


class AiQuotaNotFound(AiQuotaError):
    pass


class AiQuotaDenied(AiQuotaError):
    def __init__(self, reason: str, quota: dict[str, Any] | None = None):
        messages = {
            "DAILY_FREE_QUOTA_EXHAUSTED": "今日免费 AI Token 额度已用完，请明日再试",
            "LIFETIME_FREE_QUOTA_EXHAUSTED": "该设备的终身免费 AI Token 额度已用完",
            "INSUFFICIENT_QUOTA": "额度服务未放行本次 AI 模型请求",
            "DEVICE_DISABLED": "该设备的 AI 服务已停用，请联系管理员",
            "AUTHORIZATION_RELEASED": "本次 AI 额度授权已释放，请重新提交任务",
            "AUTHORIZATION_EXPIRED": "本次 AI 额度授权已过期，请重新提交任务",
        }
        super().__init__(
            "ai_quota_denied",
            messages.get(reason, "AI Token 额度未放行本次请求"),
            retryable=False,
            service_error_code=reason,
        )
        self.reason = reason
        self.quota = quota_summary(quota)


@dataclass(frozen=True)
class AiQuotaAuthorization:
    request_id: str
    authorization_id: str | None
    granted_tokens: int | None
    expires_at: str | None
    quota: dict[str, Any]


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def build_hmac_headers(
    settings: AiQuotaSettings,
    method: str,
    canonical_path: str,
    body: bytes,
    *,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    request_timestamp = int(time.time()) if timestamp is None else timestamp
    request_nonce = nonce or str(uuid.uuid4())
    body_hash = hashlib.sha256(body).hexdigest()
    signing_text = "\n".join(
        (method.upper(), canonical_path, str(request_timestamp), request_nonce, body_hash)
    )
    signature = hmac.new(
        settings.hmac_secret.encode("utf-8"),
        signing_text.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Client-Id": settings.client_id,
        "X-Timestamp": str(request_timestamp),
        "X-Nonce": request_nonce,
        "X-Body-SHA256": body_hash,
        "X-Signature": signature,
        "Content-Type": "application/json; charset=UTF-8",
    }


def _required_string(data: dict[str, Any], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value:
        raise AiQuotaError(
            "ai_quota_invalid_response",
            f"AI quota service response is missing {name}",
            retryable=False,
        )
    return value


def _required_positive_int(data: dict[str, Any], name: str) -> int:
    value = data.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AiQuotaError(
            "ai_quota_invalid_response",
            f"AI quota service response has invalid {name}",
            retryable=False,
        )
    return value


def quota_summary(quota: dict[str, Any] | None) -> dict[str, int]:
    if not quota:
        return {}
    limit_fields = {
        "dailyFreeLimitTokens",
        "lifetimeFreeLimitTokens",
    }
    allowed = {
        "dailyFreeLimitTokens",
        "lifetimeFreeLimitTokens",
        "dailyFreeAvailableTokens",
        "lifetimeFreeAvailableTokens",
        "effectiveFreeAvailableTokens",
        "paidAvailableTokens",
    }
    return {
        name: value
        for name, value in quota.items()
        if name in allowed
        and isinstance(value, int)
        and not isinstance(value, bool)
        and (name not in limit_fields or value >= 0)
    }


class AiQuotaClient:
    def __init__(
        self,
        settings: AiQuotaSettings,
        *,
        client: httpx.AsyncClient | None = None,
        timestamp_factory: Callable[[], int] | None = None,
        nonce_factory: Callable[[], str] | None = None,
    ):
        self.settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=settings.timeout_seconds)
        self._timestamp_factory = timestamp_factory or (lambda: int(time.time()))
        self._nonce_factory = nonce_factory or (lambda: str(uuid.uuid4()))

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _ensure_configured(self) -> None:
        if not self.settings.configured:
            raise AiQuotaError(
                "ai_quota_not_configured",
                "AI quota protection is enabled but its server credentials are not configured; model request was not started",
                retryable=False,
            )

    async def _request(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._ensure_configured()
        body = _json_bytes(payload) if payload is not None else b""
        canonical_path = CANONICAL_PREFIX + endpoint
        headers = build_hmac_headers(
            self.settings,
            method,
            canonical_path,
            body,
            timestamp=self._timestamp_factory(),
            nonce=self._nonce_factory(),
        )
        try:
            response = await self._client.request(
                method,
                self.settings.base_url + endpoint,
                content=body,
                headers=headers,
            )
        except httpx.TransportError as exc:
            raise AiQuotaError(
                "ai_quota_transport_error",
                "AI quota service could not be reached",
                retryable=True,
            ) from exc

        if response.status_code == 401:
            raise AiQuotaError(
                "ai_quota_authentication_failed",
                "AI quota service rejected the internal request authentication",
                retryable=False,
            )

        try:
            envelope = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise AiQuotaError(
                "ai_quota_invalid_response",
                "AI quota service returned an invalid response",
                retryable=response.status_code >= 500,
            ) from exc
        if not isinstance(envelope, dict):
            raise AiQuotaError(
                "ai_quota_invalid_response",
                "AI quota service returned an invalid response",
                retryable=response.status_code >= 500,
            )

        data = envelope.get("data")
        if not isinstance(data, dict):
            data = {}
        service_error_code = data.get("errorCode")
        if not isinstance(service_error_code, str):
            service_error_code = None

        if response.status_code == 404 and service_error_code == "AUTHORIZATION_NOT_FOUND":
            raise AiQuotaNotFound(
                "ai_quota_authorization_not_found",
                "AI quota authorization was not found",
                retryable=False,
                service_error_code=service_error_code,
            )
        if response.status_code >= 400 or envelope.get("code") != 200:
            retryable = response.status_code >= 500 or service_error_code == "QUOTA_CONCURRENTLY_CHANGED"
            raise AiQuotaError(
                "ai_quota_service_error",
                f"AI quota service rejected the request ({service_error_code or response.status_code})",
                retryable=retryable,
                service_error_code=service_error_code,
            )
        return data

    @staticmethod
    def _authorization_from_data(
        data: dict[str, Any],
        *,
        request_id: str | None = None,
    ) -> AiQuotaAuthorization:
        response_request_id = data.get("requestId")
        if request_id is None:
            request_id = _required_string(data, "requestId")
        elif response_request_id not in {None, request_id}:
            raise AiQuotaError(
                "ai_quota_invalid_response",
                "AI quota authorization response did not match the requested ID",
                retryable=False,
            )
        authorization_id = data.get("authorizationId")
        if not isinstance(authorization_id, str) or not authorization_id:
            authorization_id = None
        granted_tokens = data.get("grantedTokens", data.get("reservedTokens"))
        if (
            isinstance(granted_tokens, bool)
            or not isinstance(granted_tokens, int)
            or granted_tokens <= 0
        ):
            granted_tokens = None
        expires_at = data.get("expiresAt")
        if not isinstance(expires_at, str) or not expires_at:
            expires_at = None
        return AiQuotaAuthorization(
            request_id=request_id,
            authorization_id=authorization_id,
            granted_tokens=granted_tokens,
            expires_at=expires_at,
            quota=quota_summary(data.get("quota") if isinstance(data.get("quota"), dict) else None),
        )

    @staticmethod
    def _validate_settlement(
        data: dict[str, Any],
        authorization: AiQuotaAuthorization,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int,
        cache_read_input_tokens: int,
    ) -> dict[str, Any]:
        actual_tokens = data.get("actualTokens")
        response_usage = (
            (data.get("inputTokens"), input_tokens),
            (data.get("outputTokens"), output_tokens),
            (data.get("cacheCreationInputTokens"), cache_creation_input_tokens),
            (data.get("cacheReadInputTokens"), cache_read_input_tokens),
        )
        if (
            data.get("requestId") != authorization.request_id
            or data.get("settled") is not True
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value != expected
                for value, expected in response_usage
            )
            or isinstance(actual_tokens, bool)
            or not isinstance(actual_tokens, int)
            or actual_tokens != input_tokens + output_tokens
        ):
            raise AiQuotaError(
                "ai_quota_invalid_response",
                "AI quota settlement response did not match the submitted usage",
                retryable=False,
            )
        return data

    @staticmethod
    def _validate_release(
        data: dict[str, Any],
        authorization: AiQuotaAuthorization,
    ) -> dict[str, Any]:
        if (
            data.get("requestId") != authorization.request_id
            or data.get("authorizationId") != authorization.authorization_id
            or data.get("status") not in {"RELEASED", "EXPIRED", "SETTLED"}
        ):
            raise AiQuotaError(
                "ai_quota_invalid_response",
                "AI quota release response did not match the authorization",
                retryable=False,
            )
        return data

    async def status(self, request_id: str) -> dict[str, Any]:
        data = await self._request("GET", f"/status/{quote(request_id, safe='')}")
        if data.get("requestId") != request_id:
            raise AiQuotaError(
                "ai_quota_invalid_response",
                "AI quota status response did not match the requested ID",
                retryable=False,
            )
        return data

    async def authorize(
        self,
        request_id: str,
        mac: str,
    ) -> AiQuotaAuthorization:
        payload: dict[str, Any] = {
            "requestId": request_id,
            "mac": mac,
            "model": self.settings.model,
        }

        last_error: AiQuotaError | None = None
        for _ in range(self.settings.max_attempts):
            try:
                data = await self._request("POST", "/authorize", payload)
            except AiQuotaError as exc:
                if not exc.retryable:
                    raise
                last_error = exc
                try:
                    state = await self.status(request_id)
                except AiQuotaNotFound:
                    continue
                except AiQuotaError:
                    continue
                status = state.get("status")
                if status in {"RESERVED", "AUTHORIZED", "ALLOWED"}:
                    return self._authorization_from_data(state, request_id=request_id)
                if status == "SETTLED":
                    raise AiQuotaError(
                        "ai_quota_already_settled",
                        "AI quota request was already settled; model request was not repeated",
                        retryable=False,
                    )
                if status in {"RELEASED", "EXPIRED"}:
                    raise AiQuotaError(
                        "ai_quota_authorization_inactive",
                        "AI quota authorization is no longer active; submit a new task",
                        retryable=False,
                    )
                continue

            allowed = data.get("allowed")
            if data.get("requestId") not in {None, request_id}:
                raise AiQuotaError(
                    "ai_quota_invalid_response",
                    "AI quota authorization response did not match the requested ID",
                    retryable=False,
                )
            if allowed is False:
                reason = data.get("reason") if isinstance(data.get("reason"), str) else "UNKNOWN"
                raise AiQuotaDenied(
                    reason,
                    data.get("quota") if isinstance(data.get("quota"), dict) else None,
                )
            if allowed is not True:
                raise AiQuotaError(
                    "ai_quota_invalid_response",
                    "AI quota authorization response did not include an allowed decision",
                    retryable=False,
                )
            return self._authorization_from_data(data, request_id=request_id)

        if last_error is not None:
            raise last_error
        raise AiQuotaError(
            "ai_quota_authorization_failed",
            "AI quota authorization could not be confirmed; model request was not started",
            retryable=True,
        )

    async def settle(
        self,
        authorization: AiQuotaAuthorization,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int,
        cache_read_input_tokens: int,
    ) -> dict[str, Any]:
        usage_values = (
            input_tokens,
            output_tokens,
            cache_creation_input_tokens,
            cache_read_input_tokens,
        )
        if not all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in usage_values
        ):
            raise AiQuotaError(
                "ai_quota_usage_invalid",
                "AI quota settlement usage must contain non-negative integer token counts",
                retryable=False,
            )
        if cache_creation_input_tokens + cache_read_input_tokens > input_tokens:
            raise AiQuotaError(
                "ai_quota_usage_invalid",
                "AI quota cache token details cannot exceed total input tokens",
                retryable=False,
            )
        payload = {
            "requestId": authorization.request_id,
            "model": self.settings.model,
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "cacheCreationInputTokens": cache_creation_input_tokens,
            "cacheReadInputTokens": cache_read_input_tokens,
        }
        if authorization.authorization_id:
            payload["authorizationId"] = authorization.authorization_id
        last_error: AiQuotaError | None = None
        for _ in range(self.settings.max_attempts):
            try:
                data = await self._request("POST", "/settle", payload)
                return self._validate_settlement(
                    data,
                    authorization,
                    input_tokens,
                    output_tokens,
                    cache_creation_input_tokens,
                    cache_read_input_tokens,
                )
            except AiQuotaError as exc:
                last_error = exc
                if not exc.retryable:
                    break
        try:
            state = await self.status(authorization.request_id)
        except AiQuotaError:
            if last_error is not None:
                raise last_error
            raise
        if state.get("status") == "SETTLED":
            if state.get("authorizationId") not in {None, authorization.authorization_id}:
                raise AiQuotaError(
                    "ai_quota_invalid_response",
                    "AI quota settlement status did not match the authorization",
                    retryable=False,
                )
            return self._validate_settlement(
                {
                    **state,
                    "settled": True,
                    "inputTokens": input_tokens,
                    "outputTokens": output_tokens,
                    "cacheCreationInputTokens": cache_creation_input_tokens,
                    "cacheReadInputTokens": cache_read_input_tokens,
                    "confirmedByStatus": True,
                },
                authorization,
                input_tokens,
                output_tokens,
                cache_creation_input_tokens,
                cache_read_input_tokens,
            )
        if last_error is not None:
            raise last_error
        raise AiQuotaError(
            "ai_quota_settlement_unconfirmed",
            "AI quota settlement could not be confirmed",
            retryable=True,
        )

    async def release(
        self,
        authorization: AiQuotaAuthorization,
        reason: str,
    ) -> dict[str, Any]:
        payload = {
            "authorizationId": authorization.authorization_id,
            "requestId": authorization.request_id,
            "reason": reason,
        }
        last_error: AiQuotaError | None = None
        for _ in range(self.settings.max_attempts):
            try:
                data = await self._request("POST", "/release", payload)
                return self._validate_release(data, authorization)
            except AiQuotaError as exc:
                last_error = exc
                if exc.retryable:
                    continue
                break
        try:
            state = await self.status(authorization.request_id)
        except AiQuotaError:
            if last_error is not None:
                raise last_error
            raise
        if state.get("status") in {"RELEASED", "EXPIRED", "SETTLED"}:
            return self._validate_release(
                {**state, "confirmedByStatus": True},
                authorization,
            )
        if last_error is not None:
            raise last_error
        raise AiQuotaError(
            "ai_quota_release_unconfirmed",
            "AI quota release could not be confirmed",
            retryable=True,
        )
