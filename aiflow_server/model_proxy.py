from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

import httpx

from .ai_quota import (
    AiQuotaAuthorization,
    AiQuotaClient,
    AiQuotaDenied,
    AiQuotaError,
)
from .config import Settings
from .storage import Storage

INTERNAL_MODEL_PROXY_PREFIX = "/.aiflow-internal/model"
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

EmitCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
TokenUsage = tuple[int, int, int, int]
LOGGER = logging.getLogger(__name__)


class ModelProxyError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        quota_error: AiQuotaError | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.quota_error = quota_error


@dataclass
class ModelQuotaLease:
    request_index: int
    request_id: str
    authorization: AiQuotaAuthorization
    forwarded: bool = False
    finalized: bool = False


@dataclass
class ModelProxyResponse:
    status_code: int
    headers: dict[str, str]
    content: bytes | None = None
    body_iterator: AsyncIterator[bytes] | None = None


def _request_id(task_id: str, request_index: int) -> str:
    candidate = f"{task_id}:model:{request_index}"
    if len(candidate) <= 128:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    return f"aiflow-model-{digest}"


def _public_settlement(
    settlement: dict[str, Any],
    usage: TokenUsage,
    request_index: int,
) -> dict[str, Any]:
    input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens = usage
    public = {
        "stage": "coding",
        "message": "AI token usage settled for one model request",
        "model_request_index": request_index,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "actual_tokens": settlement.get("actualTokens", input_tokens + output_tokens),
        "confirmed_by_status": bool(settlement.get("confirmedByStatus")),
    }
    for source, target in (
        ("dailyFreeAvailableTokens", "daily_free_available_tokens"),
        ("lifetimeFreeAvailableTokens", "lifetime_free_available_tokens"),
        ("effectiveFreeAvailableTokens", "effective_free_available_tokens"),
        ("paidAvailableTokens", "paid_available_tokens"),
    ):
        value = settlement.get(source)
        if isinstance(value, int) and not isinstance(value, bool):
            public[target] = value
    return public


class _UsageAccumulator:
    def __init__(self) -> None:
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.output_tokens_final = False
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0

    @staticmethod
    def _token(data: Mapping[str, Any], name: str) -> int | None:
        if name not in data:
            return None
        value = data[name]
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        raise AiQuotaError(
            "ai_quota_usage_missing",
            f"Trusted model usage contained an invalid {name}",
            retryable=False,
        )

    def add(self, usage: Any, *, output_is_final: bool) -> None:
        if not isinstance(usage, Mapping):
            return
        input_tokens = self._token(usage, "input_tokens")
        output_tokens = self._token(usage, "output_tokens")
        cache_creation = self._token(usage, "cache_creation_input_tokens")
        cache_read = self._token(usage, "cache_read_input_tokens")
        if input_tokens is not None:
            self.input_tokens = input_tokens
        if output_tokens is not None:
            self.output_tokens = output_tokens
            if output_is_final:
                self.output_tokens_final = True
        if cache_creation is not None:
            self.cache_creation_input_tokens = cache_creation
        if cache_read is not None:
            self.cache_read_input_tokens = cache_read

    def add_event(self, event: Any) -> None:
        if not isinstance(event, Mapping):
            return
        event_type = event.get("type")
        if event_type == "message_start":
            message = event.get("message")
            if isinstance(message, Mapping):
                self.add(message.get("usage"), output_is_final=False)
        elif event_type == "message_delta":
            self.add(event.get("usage"), output_is_final=True)
        else:
            self.add(event.get("usage"), output_is_final=True)

    def result(self) -> TokenUsage:
        if (
            self.input_tokens is None
            or self.output_tokens is None
            or not self.output_tokens_final
        ):
            raise AiQuotaError(
                "ai_quota_usage_missing",
                "Trusted usage was missing from the individual model response",
                retryable=False,
            )
        # Anthropic-compatible usage reports uncached input separately from
        # cache creation/read tokens. The quota API expects inputTokens to be
        # the complete input total, with both cache values repeated as detail.
        total_input_tokens = (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )
        return (
            total_input_tokens,
            self.output_tokens,
            self.cache_creation_input_tokens,
            self.cache_read_input_tokens,
        )


def _parse_sse_frame(frame: bytes) -> tuple[dict[str, Any] | None, bool]:
    data_lines: list[bytes] = []
    event_name = ""
    for line in frame.replace(b"\r\n", b"\n").split(b"\n"):
        if line.startswith(b"data:"):
            data_lines.append(line[5:].lstrip())
        elif line.startswith(b"event:"):
            event_name = line[6:].strip().decode("utf-8", errors="replace")
    if not data_lines:
        return None, event_name == "message_stop"
    data = b"\n".join(data_lines)
    if data.strip() == b"[DONE]":
        return None, True
    try:
        parsed = json.loads(data)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None, event_name == "message_stop"
    is_terminal = event_name == "message_stop" or (
        isinstance(parsed, dict) and parsed.get("type") == "message_stop"
    )
    return parsed if isinstance(parsed, dict) else None, is_terminal


def _split_sse_frame(buffer: bytes) -> tuple[bytes, bytes] | None:
    positions = [
        (position, separator)
        for separator in (b"\n\n", b"\r\n\r\n")
        if (position := buffer.find(separator)) >= 0
    ]
    if not positions:
        return None
    position, separator = min(positions, key=lambda item: item[0])
    end = position + len(separator)
    return buffer[:end], buffer[end:]


class ModelQuotaSession:
    def __init__(
        self,
        *,
        task_id: str,
        mac: str,
        token: str,
        proxy_base_url: str,
        upstream: SplitResult,
        settings: Settings,
        storage: Storage,
        quota_client: AiQuotaClient,
        emit: EmitCallback,
    ):
        self.task_id = task_id
        self.mac = mac
        self.token = token
        self.proxy_base_url = proxy_base_url
        self.upstream = upstream
        self.settings = settings
        self.storage = storage
        self.quota_client = quota_client
        self.emit = emit
        self.failure: AiQuotaError | None = None
        self._request_index = 0
        self._request_lock = asyncio.Lock()
        self._active_lease: ModelQuotaLease | None = None

    @property
    def secret_values(self) -> list[str]:
        return [self.token, self.proxy_base_url]

    @property
    def base_path(self) -> str:
        return self.upstream.path.rstrip("/")

    def target_url(self, upstream_path: str, query: str) -> str:
        path = "/" + upstream_path.lstrip("/")
        base_path = self.base_path
        if base_path and path != base_path and not path.startswith(base_path + "/"):
            raise ModelProxyError(404, "model_proxy_path_invalid", "Model proxy path is outside the configured upstream base URL")
        return urlunsplit((self.upstream.scheme, self.upstream.netloc, path, query, ""))

    def is_model_request(self, method: str, upstream_path: str) -> bool:
        return method.upper() == "POST" and ("/" + upstream_path.lstrip("/")).rstrip("/").endswith("/messages")

    async def authorize(self) -> ModelQuotaLease:
        await self._request_lock.acquire()
        if self.failure is not None:
            self._request_lock.release()
            raise self.failure
        try:
            self._request_index += 1
            request_index = self._request_index
            request_id = _request_id(self.task_id, request_index)
            try:
                await asyncio.to_thread(
                    self.storage.begin_ai_quota_request,
                    self.task_id,
                    request_id,
                    self.settings.ai_quota.model,
                    request_index=request_index,
                )
            except Exception:
                LOGGER.exception(
                    "Could not persist AI quota authorization attempt for model request %s",
                    request_index,
                )
            await self._emit_safely(
                "ai_quota_authorizing",
                {
                    "stage": "coding",
                    "message": "Asking the quota service whether one model request is allowed",
                    "model_request_index": request_index,
                },
            )
            authorization = await self.quota_client.authorize(request_id, self.mac)
            try:
                await asyncio.to_thread(
                    self.storage.authorize_ai_quota,
                    request_id,
                    authorization.authorization_id,
                    authorization.granted_tokens,
                    authorization.expires_at,
                )
            except Exception:
                LOGGER.exception(
                    "Could not persist AI quota authorization for model request %s",
                    request_index,
                )
            lease = ModelQuotaLease(request_index, request_id, authorization)
            self._active_lease = lease
            await self._emit_safely(
                "ai_quota_authorized",
                {
                    "stage": "coding",
                    "message": "The quota service allowed one model request",
                    "model_request_index": request_index,
                    "quota": authorization.quota,
                },
            )
            return lease
        except AiQuotaDenied as exc:
            self.failure = exc
            if "request_id" in locals():
                try:
                    await asyncio.to_thread(
                        self.storage.update_ai_quota_status,
                        request_id,
                        "DENIED",
                    )
                except Exception:
                    LOGGER.exception(
                        "Could not persist AI quota denial for model request %s",
                        request_index,
                    )
            if self._request_lock.locked():
                self._request_lock.release()
            raise
        except AiQuotaError as exc:
            self.failure = exc
            if self._request_lock.locked():
                self._request_lock.release()
            raise
        except Exception as exc:
            error = AiQuotaError(
                "ai_quota_internal_error",
                "AI quota request preparation failed before the model request was sent",
                retryable=False,
            )
            self.failure = error
            if self._request_lock.locked():
                self._request_lock.release()
            raise error from exc

    async def mark_forwarded(self, lease: ModelQuotaLease) -> None:
        lease.forwarded = True
        await self._record_status(lease, "USAGE_UNKNOWN")

    async def settle(self, lease: ModelQuotaLease, usage: TokenUsage) -> None:
        if lease.finalized:
            return
        input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens = usage
        try:
            await self._record_status(
                lease,
                "SETTLEMENT_REQUIRED",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_creation_input_tokens=cache_creation_input_tokens,
                cache_read_input_tokens=cache_read_input_tokens,
            )
            await self._record_status(lease, "SETTLING")
            settlement = await self.quota_client.settle(
                lease.authorization,
                input_tokens,
                output_tokens,
                cache_creation_input_tokens,
                cache_read_input_tokens,
            )
            await self._record_status(lease, "SETTLED")
            await self._emit_safely(
                "ai_quota_settled",
                _public_settlement(settlement, usage, lease.request_index),
            )
        except Exception as exc:  # noqa: BLE001 - accounting must not invalidate model output
            quota_error = exc if isinstance(exc, AiQuotaError) else None
            LOGGER.warning(
                "AI quota settlement remains pending for model request %s: error_type=%s",
                lease.request_index,
                type(exc).__name__,
            )
            await self._emit_safely(
                "ai_quota_settlement_pending",
                {
                    "stage": "coding",
                    "message": "AI token usage was saved but settlement could not be confirmed",
                    "model_request_index": lease.request_index,
                    "reason": "settlement_unconfirmed",
                    "retryable": quota_error.retryable if quota_error else True,
                    "error_code": quota_error.code if quota_error else "ai_quota_accounting_error",
                    "service_error_code": quota_error.service_error_code if quota_error else None,
                },
            )
        finally:
            self._finish(lease)

    async def mark_usage_unknown(self, lease: ModelQuotaLease) -> None:
        if lease.finalized:
            return
        await self._record_status(lease, "USAGE_UNKNOWN")
        await self._emit_safely(
            "ai_quota_settlement_pending",
            {
                "stage": "coding",
                "message": "Model usage could not be confirmed and needs accounting review",
                "model_request_index": lease.request_index,
                "reason": "usage_unknown",
                "retryable": False,
            },
        )
        self._finish(lease)

    async def mark_no_usage(self, lease: ModelQuotaLease, reason: str) -> None:
        if lease.finalized:
            return
        await self._record_status(lease, "NO_USAGE")
        await self._emit_safely(
            "ai_quota_no_usage",
            {
                "stage": "coding",
                "message": "The allowed model request completed without billable usage",
                "model_request_index": lease.request_index,
                "reason": reason,
            },
        )
        self._finish(lease)

    async def _record_status(self, lease: ModelQuotaLease, status: str, **usage: Any) -> None:
        try:
            await asyncio.to_thread(
                self.storage.update_ai_quota_status,
                lease.request_id,
                status,
                **usage,
            )
        except Exception:
            LOGGER.exception(
                "Could not persist AI quota accounting status %s for model request %s",
                status,
                lease.request_index,
            )

    async def _emit_safely(self, event_type: str, data: dict[str, Any]) -> None:
        try:
            await self.emit(event_type, data)
        except Exception:
            LOGGER.exception("Could not emit AI quota event %s", event_type)

    def _finish(self, lease: ModelQuotaLease) -> None:
        lease.finalized = True
        if self._active_lease is lease:
            self._active_lease = None
        if self._request_lock.locked():
            self._request_lock.release()

    async def close(self, reason: str) -> None:
        lease = self._active_lease
        if lease is None or lease.finalized:
            return
        try:
            if lease.forwarded:
                await self.mark_usage_unknown(lease)
            else:
                await self.mark_no_usage(lease, reason)
        except Exception:
            LOGGER.exception(
                "Could not close AI quota accounting for model request %s",
                lease.request_index,
            )
            self._finish(lease)

    def raise_if_failed(self) -> None:
        if self.failure is not None:
            raise self.failure


class ModelProxyRegistry:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        quota_client: AiQuotaClient,
        *,
        upstream_client: httpx.AsyncClient | None = None,
    ):
        self.settings = settings
        self.storage = storage
        self.quota_client = quota_client
        self._owns_client = upstream_client is None
        self._upstream_client = upstream_client or httpx.AsyncClient(timeout=None, follow_redirects=False)
        self._sessions: dict[str, ModelQuotaSession] = {}

    def create_session(self, task_id: str, mac: str, emit: EmitCallback) -> ModelQuotaSession:
        original_base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
        parsed = urlsplit(original_base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise AiQuotaError(
                "ai_quota_model_base_url_invalid",
                "ANTHROPIC_BASE_URL is not a valid fixed HTTP upstream URL",
                retryable=False,
            )
        token = secrets.token_urlsafe(32)
        while token in self._sessions:
            token = secrets.token_urlsafe(32)
        proxy_root = f"http://127.0.0.1:{self.settings.port}{INTERNAL_MODEL_PROXY_PREFIX}/{token}"
        proxy_base_url = proxy_root + parsed.path.rstrip("/")
        session = ModelQuotaSession(
            task_id=task_id,
            mac=mac,
            token=token,
            proxy_base_url=proxy_base_url,
            upstream=parsed,
            settings=self.settings,
            storage=self.storage,
            quota_client=self.quota_client,
            emit=emit,
        )
        self._sessions[token] = session
        return session

    def get(self, token: str) -> ModelQuotaSession | None:
        return self._sessions.get(token)

    async def remove(self, session: ModelQuotaSession, reason: str) -> None:
        await session.close(reason)
        self._sessions.pop(session.token, None)

    async def close(self) -> None:
        sessions = list(self._sessions.values())
        for session in sessions:
            await self.remove(session, "AIFLOW_REQUEST_FAILED")
        if self._owns_client:
            await self._upstream_client.aclose()

    async def proxy(
        self,
        session: ModelQuotaSession,
        method: str,
        upstream_path: str,
        query: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> ModelProxyResponse:
        target_url = session.target_url(upstream_path, query)
        outgoing_headers = {
            name: value
            for name, value in headers.items()
            if name.lower() not in HOP_BY_HOP_HEADERS
            and name.lower() not in {"host", "content-length", "cookie"}
        }
        outgoing_headers["accept-encoding"] = "identity"
        if not session.is_model_request(method, upstream_path):
            response = await self._upstream_client.request(
                method,
                target_url,
                content=body,
                headers=outgoing_headers,
            )
            return ModelProxyResponse(
                response.status_code,
                _response_headers(response),
                content=response.content,
            )

        request = self._upstream_client.build_request(
            method,
            target_url,
            content=body,
            headers=outgoing_headers,
        )
        try:
            lease = await session.authorize()
        except AiQuotaError as exc:
            raise ModelProxyError(402, exc.code, str(exc), quota_error=exc) from exc
        await session.mark_forwarded(lease)
        try:
            response = await self._upstream_client.send(request, stream=True)
        except Exception as exc:
            await session.mark_usage_unknown(lease)
            raise ModelProxyError(
                502,
                "model_upstream_response_unknown",
                "Model upstream response could not be confirmed",
                quota_error=session.failure,
            ) from exc

        if response.status_code >= 300:
            content = await response.aread()
            await response.aclose()
            await session.mark_no_usage(lease, "MODEL_REQUEST_REJECTED")
            return ModelProxyResponse(
                response.status_code,
                _response_headers(response),
                content=content,
            )

        content_type = response.headers.get("content-type", "").lower()
        if "text/event-stream" in content_type:
            return ModelProxyResponse(
                response.status_code,
                _response_headers(response),
                body_iterator=self._stream_sse(session, lease, response),
            )

        try:
            content = await response.aread()
            accumulator = _UsageAccumulator()
            parsed = json.loads(content)
            try:
                accumulator.add_event(parsed)
                usage = accumulator.result()
            except AiQuotaError:
                await session.mark_usage_unknown(lease)
            else:
                await session.settle(lease, usage)
            return ModelProxyResponse(
                response.status_code,
                _response_headers(response),
                content=content,
            )
        except Exception as exc:
            if not lease.finalized:
                await session.mark_usage_unknown(lease)
            raise ModelProxyError(
                502,
                "model_upstream_response_invalid",
                "Model upstream returned an invalid response",
                quota_error=session.failure,
            ) from exc
        finally:
            await response.aclose()

    async def _stream_sse(
        self,
        session: ModelQuotaSession,
        lease: ModelQuotaLease,
        response: httpx.Response,
    ) -> AsyncIterator[bytes]:
        accumulator = _UsageAccumulator()
        buffer = b""
        terminal_frames: list[bytes] = []
        usage_invalid = False
        try:
            async for chunk in response.aiter_bytes():
                buffer += chunk
                while split := _split_sse_frame(buffer):
                    frame, buffer = split
                    event, terminal = _parse_sse_frame(frame)
                    if event is not None and not usage_invalid:
                        try:
                            accumulator.add_event(event)
                        except AiQuotaError:
                            usage_invalid = True
                    if terminal:
                        terminal_frames.append(frame)
                    else:
                        yield frame
            if buffer:
                event, terminal = _parse_sse_frame(buffer)
                if event is not None and not usage_invalid:
                    try:
                        accumulator.add_event(event)
                    except AiQuotaError:
                        usage_invalid = True
                if terminal:
                    terminal_frames.append(buffer)
                else:
                    yield buffer
            if usage_invalid:
                await session.mark_usage_unknown(lease)
            else:
                try:
                    usage = accumulator.result()
                except AiQuotaError:
                    await session.mark_usage_unknown(lease)
                else:
                    await session.settle(lease, usage)
            for frame in terminal_frames:
                yield frame
        except (GeneratorExit, asyncio.CancelledError):
            if not lease.finalized:
                try:
                    usage = accumulator.result()
                except AiQuotaError:
                    await session.mark_usage_unknown(lease)
                else:
                    await session.settle(lease, usage)
            raise
        except Exception:
            if not lease.finalized:
                await session.mark_usage_unknown(lease)
            raise
        finally:
            await response.aclose()


def _response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        name: value
        for name, value in response.headers.items()
        if name.lower() not in HOP_BY_HOP_HEADERS
        and name.lower() not in {"content-length", "content-encoding", "set-cookie"}
    }
