from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import __version__
from .app import TOKEN_HEADER, create_app
from .config import Settings, load_settings
from .security import (
    AUTH_VERSION_HEADER,
    CONTENT_HASH_HEADER,
    KEY_ID_HEADER,
    NONCE_HEADER,
    RESPONSE_SIGNATURE_HEADER,
    RESPONSE_TIMESTAMP_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    body_hash,
    sign_bytes,
    sign_request,
    sign_response,
)
from .storage import TERMINAL_STATUSES


WEB_SESSION_COOKIE = "aiflow_web_session"
INTERNAL_KEY_ID = "anonymous-web-bff"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
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
INTERNAL_AUTH_HEADERS = {
    KEY_ID_HEADER.lower(),
    TIMESTAMP_HEADER.lower(),
    NONCE_HEADER.lower(),
    CONTENT_HASH_HEADER.lower(),
    SIGNATURE_HEADER.lower(),
    AUTH_VERSION_HEADER.lower(),
    RESPONSE_TIMESTAMP_HEADER.lower(),
    RESPONSE_SIGNATURE_HEADER.lower(),
}

RateCounter = tuple[str, str, int, int, str]


class GatewayRateLimiter:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self._request_lock = threading.Lock()
        self._request_counts: dict[tuple[str, str, int], int] = {}
        self._last_request_cleanup = 0
        database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS web_rate_counters (
                    identity_hash TEXT NOT NULL,
                    counter_type TEXT NOT NULL,
                    window_start INTEGER NOT NULL,
                    count INTEGER NOT NULL,
                    PRIMARY KEY(identity_hash, counter_type, window_start)
                )
                """
            )

    @staticmethod
    def _identity(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def claim_requests(self, counters: list[RateCounter], now: int) -> str:
        with self._request_lock:
            if now - self._last_request_cleanup >= 60:
                cutoff = now - 60
                self._request_counts = {
                    key: count
                    for key, count in self._request_counts.items()
                    if key[2] >= cutoff
                }
                self._last_request_cleanup = now

            resolved = [
                (
                    (self._identity(identity), counter_type, window_start),
                    limit,
                    reason,
                )
                for identity, counter_type, window_start, limit, reason in counters
            ]
            for key, limit, reason in resolved:
                if self._request_counts.get(key, 0) >= limit:
                    return reason
            for key, _, _ in resolved:
                self._request_counts[key] = self._request_counts.get(key, 0) + 1
        return "ok"

    def claim_persistent(self, counters: list[RateCounter]) -> str:
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            for identity, counter_type, window_start, limit, reason in counters:
                row = db.execute(
                    """
                    SELECT count FROM web_rate_counters
                    WHERE identity_hash=? AND counter_type=? AND window_start=?
                    """,
                    (self._identity(identity), counter_type, window_start),
                ).fetchone()
                if row and int(row["count"]) >= limit:
                    db.commit()
                    return reason
            for identity, counter_type, window_start, _, _ in counters:
                db.execute(
                    """
                    INSERT INTO web_rate_counters(identity_hash, counter_type, window_start, count)
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(identity_hash, counter_type, window_start)
                    DO UPDATE SET count=count+1
                    """,
                    (self._identity(identity), counter_type, window_start),
                )
            db.execute(
                "DELETE FROM web_rate_counters WHERE counter_type='request-minute' OR window_start<?",
                (int(time.time()) - 172800,),
            )
            db.commit()
            return "ok"
        finally:
            db.close()


def _session_cookie(secret: bytes, session_id: str) -> str:
    return f"{session_id}.{sign_bytes(secret, session_id.encode('utf-8'))}"


def _read_session_cookie(secret: bytes, value: str | None) -> str | None:
    if not value or "." not in value:
        return None
    session_id, supplied = value.rsplit(".", 1)
    if not session_id or not hmac.compare_digest(
        sign_bytes(secret, session_id.encode("utf-8")), supplied
    ):
        return None
    return session_id


def _request_target(request: Request) -> str:
    raw_path = request.scope.get("raw_path") or request.url.path.encode("utf-8")
    query = request.scope.get("query_string") or b""
    return raw_path.decode("latin-1") + (("?" + query.decode("latin-1")) if query else "")


def _origin_allowed(request: Request, settings: Settings) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return False
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.netloc.lower() == request.headers.get("host", "").lower():
        return True
    return origin.rstrip("/") in {value.rstrip("/") for value in settings.cors_origins}


def _client_ip(request: Request, settings: Settings) -> str:
    direct = request.client.host if request.client else "unknown"
    if direct in settings.web_trusted_proxy_ips:
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        if forwarded:
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
    return direct


def _retry_after_seconds(reason: str, now: int) -> int:
    if reason.endswith("_day"):
        return 86400 - now % 86400
    return 60 - now % 60


def _copy_request_headers(request: Request) -> dict[str, str]:
    return {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in HOP_BY_HOP_HEADERS
        and name.lower() not in INTERNAL_AUTH_HEADERS
        and name.lower() not in {"host", "content-length", "cookie"}
    }


def _copy_response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        name: value
        for name, value in response.headers.items()
        if name.lower() not in HOP_BY_HOP_HEADERS
        and name.lower() not in INTERNAL_AUTH_HEADERS
        and name.lower() not in {"content-length", "set-cookie"}
    }


def create_gateway_app(
    settings: Settings | None = None,
    *,
    runner=None,
    pusher=None,
) -> FastAPI:
    public_settings = settings or load_settings()
    internal_secret = secrets.token_bytes(32)
    session_secret = secrets.token_bytes(32)
    core_settings = replace(
        public_settings,
        client_auth_enabled=True,
        client_auth_keys_file=None,
        client_auth_keys=((INTERNAL_KEY_ID, internal_secret),),
        cors_origins=(),
    )
    core_app = create_app(core_settings, runner=runner, pusher=pusher)
    limiter = GatewayRateLimiter(public_settings.data_dir / "gateway.sqlite3")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with core_app.router.lifespan_context(core_app):
            transport = httpx.ASGITransport(app=core_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://aiflow-core", timeout=120) as client:
                app.state.core_client = client
                yield

    app = FastAPI(
        title="AIFlow Anonymous Web Gateway",
        version=__version__,
        description="Public same-origin BFF for the private signed AIFlow core API.",
        lifespan=lifespan,
    )
    app.state.core_app = core_app
    app.state.core_storage = core_app.state.storage
    app.state.core_tasks = core_app.state.tasks
    app.state.rate_limiter = limiter

    if public_settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(public_settings.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "PUT", "OPTIONS"],
            allow_headers=["Content-Type", TOKEN_HEADER, "Last-Event-ID"],
            expose_headers=["Content-Disposition"],
        )

    @app.middleware("http")
    async def anonymous_web_guard(request: Request, call_next):
        session_id = _read_session_cookie(session_secret, request.cookies.get(WEB_SESSION_COOKIE))
        new_session = session_id is None
        if session_id is None:
            session_id = secrets.token_urlsafe(24)
        request.state.web_session_id = session_id

        if request.url.path.startswith("/api/v3/") and request.method != "OPTIONS":
            if (
                public_settings.web_require_same_origin
                and request.method not in SAFE_METHODS
                and not _origin_allowed(request, public_settings)
            ):
                response = JSONResponse(
                    status_code=403,
                    content={
                        "detail": {
                            "code": "cross_site_request_rejected",
                            "message": "state-changing anonymous web requests must be same-origin",
                        }
                    },
                )
            else:
                now = int(time.time())
                minute = now - now % 60
                day = now - now % 86400
                client_ip = _client_ip(request, public_settings)
                request_counters = [
                    (
                        f"session:{session_id}",
                        "request-minute",
                        minute,
                        public_settings.web_requests_per_session_minute,
                        "session_minute",
                    ),
                    (
                        f"ip:{client_ip}",
                        "request-minute",
                        minute,
                        public_settings.web_requests_per_ip_minute,
                        "ip_minute",
                    ),
                ]
                ai_counters: list[RateCounter] = []
                if request.method == "POST" and request.url.path == "/api/v3/tasks/coding":
                    ai_counters.extend(
                        [
                            (
                                f"session:{session_id}",
                                "ai-minute",
                                minute,
                                public_settings.web_ai_tasks_per_session_minute,
                                "ai_session_minute",
                            ),
                            (
                                f"session:{session_id}",
                                "ai-day",
                                day,
                                public_settings.web_ai_tasks_per_session_day,
                                "ai_session_day",
                            ),
                            (
                                f"ip:{client_ip}",
                                "ai-day",
                                day,
                                public_settings.web_ai_tasks_per_ip_day,
                                "ai_ip_day",
                            ),
                        ]
                    )
                claim = limiter.claim_requests(request_counters, now)
                if claim == "ok" and ai_counters:
                    claim = await asyncio.to_thread(limiter.claim_persistent, ai_counters)
                if claim != "ok":
                    retry_after = _retry_after_seconds(claim, now)
                    scope = "session" if "session" in claim else "ip"
                    response = JSONResponse(
                        status_code=429,
                        content={
                            "detail": {
                                "code": f"web_rate_limit_{claim}",
                                "message": "anonymous web usage limit reached; retry after the quota window",
                                "scope": scope,
                                "retry_after_seconds": retry_after,
                            }
                        },
                        headers={"Retry-After": str(retry_after)},
                    )
                else:
                    response = await call_next(request)
        else:
            response = await call_next(request)

        if new_session:
            response.set_cookie(
                WEB_SESSION_COOKIE,
                _session_cookie(session_secret, session_id),
                httponly=True,
                secure=public_settings.web_cookie_secure,
                samesite="lax",
                max_age=86400,
                path="/",
            )
        return response

    async def call_core(request: Request, *, sign: bool) -> httpx.Response:
        body = await request.body()
        target = _request_target(request)
        headers = _copy_request_headers(request)
        request_nonce = None
        if sign:
            timestamp = str(int(time.time()))
            request_nonce = secrets.token_urlsafe(18)
            digest = body_hash(body)
            headers.update(
                {
                    KEY_ID_HEADER: INTERNAL_KEY_ID,
                    TIMESTAMP_HEADER: timestamp,
                    NONCE_HEADER: request_nonce,
                    CONTENT_HASH_HEADER: digest,
                    SIGNATURE_HEADER: sign_request(
                        internal_secret,
                        request.method,
                        target,
                        timestamp,
                        request_nonce,
                        digest,
                    ),
                }
            )
        core_response = await app.state.core_client.request(
            request.method,
            target,
            content=body,
            headers=headers,
        )
        if sign:
            response_timestamp = core_response.headers.get(RESPONSE_TIMESTAMP_HEADER, "")
            response_signature = core_response.headers.get(RESPONSE_SIGNATURE_HEADER, "")
            expected = sign_response(
                internal_secret,
                request_nonce or "",
                core_response.status_code,
                response_timestamp,
            )
            if not response_timestamp or not hmac.compare_digest(expected, response_signature):
                raise HTTPException(
                    status_code=502,
                    detail={
                        "code": "core_response_authentication_failed",
                        "message": "private AIFlow core returned an invalid response acknowledgement",
                    },
                )
        return core_response

    @app.get("/api/v3/capabilities")
    async def capabilities(request: Request) -> JSONResponse:
        core_response = await call_core(request, sign=False)
        payload = core_response.json()
        payload["client_auth"] = {
            "enabled": False,
            "mode": "server_bff",
            "browser_holds_secret": False,
            "core_authenticated": True,
        }
        payload["authentication"] = (
            "anonymous web session plus capability token; private core signed by server BFF"
        )
        payload["features"] = [
            *payload.get("features", []),
            "anonymous_web_gateway",
            "server_side_core_signing",
            "full_safe_agent_events",
        ]
        payload["web_gateway"] = {
            "anonymous": True,
            "same_origin_required": public_settings.web_require_same_origin,
            "requests_per_session_minute": public_settings.web_requests_per_session_minute,
            "requests_per_ip_minute": public_settings.web_requests_per_ip_minute,
            "ai_tasks_per_session_minute": public_settings.web_ai_tasks_per_session_minute,
            "ai_tasks_per_session_day": public_settings.web_ai_tasks_per_session_day,
            "ai_tasks_per_ip_day": public_settings.web_ai_tasks_per_ip_day,
        }
        return JSONResponse(payload, status_code=core_response.status_code)

    @app.get("/api/v3/tasks/{task_id}/events")
    async def task_events(
        request: Request,
        task_id: str,
        after: int = Query(0, ge=0),
        stream_token: str | None = Query(None),
        context_token: str | None = Header(None, alias=TOKEN_HEADER),
        last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        storage = app.state.core_storage
        tasks = app.state.core_tasks
        authorized = bool(stream_token and storage.validate_stream_token(task_id, stream_token))
        if not authorized and context_token:
            context = storage.get_context_by_token(context_token)
            authorized = bool(context and storage.get_owned_task(task_id, context["context_id"]))
        if not authorized:
            raise HTTPException(
                status_code=401,
                detail={"code": "stream_not_authorized", "message": "valid context or task stream token required"},
            )
        if not storage.get_task(task_id):
            raise HTTPException(status_code=404, detail={"code": "task_not_found", "message": "task not found"})
        cursor = max(after, int(last_event_id) if last_event_id and last_event_id.isdigit() else 0)

        async def generate():
            nonlocal cursor
            last_heartbeat = time.monotonic()
            signal = tasks.subscribe_events(task_id)
            try:
                while True:
                    signal.clear()
                    events = storage.list_events(task_id, after=cursor, limit=200)
                    for event in events:
                        cursor = event["sequence"]
                        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                        yield f"id: {cursor}\nevent: {event['type']}\ndata: {payload}\n\n"
                    current = storage.get_task(task_id)
                    if current and current["status"] in TERMINAL_STATUSES:
                        if len(events) < 200:
                            break
                        continue
                    if await request.is_disconnected():
                        break
                    heartbeat_wait = max(
                        0.0,
                        public_settings.heartbeat_seconds - (time.monotonic() - last_heartbeat),
                    )
                    try:
                        await asyncio.wait_for(signal.wait(), timeout=heartbeat_wait)
                    except asyncio.TimeoutError:
                        status_payload = tasks.status(current) if current else {"task_id": task_id, "status": "missing"}
                        yield "event: heartbeat\ndata: " + json.dumps(
                            status_payload, ensure_ascii=False, separators=(",", ":")
                        ) + "\n\n"
                        last_heartbeat = time.monotonic()
            finally:
                tasks.unsubscribe_events(task_id, signal)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PATCH", "DELETE", "PUT", "HEAD", "OPTIONS"],
    )
    async def proxy(request: Request, path: str) -> Response:
        api_path = request.url.path
        sign = api_path.startswith("/api/v3/") and api_path not in {
            "/api/v3/capabilities",
            "/api/v3/system/status",
        }
        core_response = await call_core(request, sign=sign)
        return Response(
            content=core_response.content,
            status_code=core_response.status_code,
            headers=_copy_response_headers(core_response),
        )

    return app


app = create_gateway_app()
