from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from claude_agent_sdk import get_session_messages, list_sessions
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .agent import ClaudeRunner, _event_secrets, _sanitize_event
from .asr import AsrError, SaucAsrClient
from .config import Settings, load_settings
from .device_push import DeploymentError, DevicePusher
from .schemas import (
    CodingTaskRequest,
    ContextInfoResponse,
    ContextResponse,
    CreateContextRequest,
    DeviceInfo,
    DirectRunRequest,
    FileInfo,
    ResetConversationRequest,
    TaskCreatedResponse,
    TaskStatusResponse,
    UpdateDeviceRequest,
)
from .security import (
    AUTH_VERSION_HEADER,
    CONTENT_HASH_HEADER,
    KEY_ID_HEADER,
    NONCE_HEADER,
    RESPONSE_SIGNATURE_HEADER,
    RESPONSE_TIMESTAMP_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    ClientAuthenticator,
    ClientAuthError,
)
from .storage import SessionCapacityFull, TERMINAL_STATUSES, Storage, new_token, utc_now
from .tasks import TaskConflict, TaskManager, TaskNotFound, TaskQueueFull
from .telemetry import TlsTelemetry
from .workspaces import AttachmentError, WorkspaceError, WorkspaceManager


TOKEN_HEADER = "X-AIFlow-Context-Token"
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
CLIENT_CACHE_HEADERS = {"Cache-Control": "no-store, max-age=0"}


class ClientStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: dict[str, Any]):
        response = await super().get_response(path, scope)
        response.headers.update(CLIENT_CACHE_HEADERS)
        return response


def _model_name(settings: Settings) -> str:
    return settings.claude_model or "claude-code-default"


def _task_created(
    task: dict[str, Any],
    stream_token: str,
    device_id: str,
    tasks: TaskManager,
) -> TaskCreatedResponse:
    task_id = task["task_id"]
    return TaskCreatedResponse(
        task_id=task_id,
        device_id=device_id,
        kind=task["kind"],
        status=task["status"],
        status_url=f"/api/v3/tasks/{task_id}",
        events_url=f"/api/v3/tasks/{task_id}/events",
        stream_token=stream_token,
        queue_position=tasks.storage.queue_position(task_id),
        system_status=tasks.system_status(),
    )


def _redact_paths(value: Any, roots: list[Path]) -> Any:
    if isinstance(value, str):
        result = value
        for root in roots:
            result = result.replace(str(root), "<workspace>")
        return result
    if isinstance(value, list):
        return [_redact_paths(item, roots) for item in value]
    if isinstance(value, dict):
        return {key: _redact_paths(item, roots) for key, item in value.items()}
    return value


def create_app(
    settings: Settings | None = None,
    *,
    runner: ClaudeRunner | None = None,
    pusher: DevicePusher | None = None,
    asr_client: SaucAsrClient | None = None,
) -> FastAPI:
    configured = settings or load_settings()
    telemetry = TlsTelemetry(configured.tls_logging, configured.database_path)
    storage = Storage(
        configured.database_path,
        configured.event_retention,
        tls_logging=configured.tls_logging,
        telemetry_notify=telemetry.notify,
    )
    workspaces = WorkspaceManager(configured)
    effective_runner = runner or ClaudeRunner(configured, workspaces)
    effective_pusher = pusher or DevicePusher(configured, workspaces)
    effective_asr_client = asr_client or SaucAsrClient(configured.asr)
    tasks = TaskManager(
        configured,
        storage,
        workspaces,
        effective_runner,
        effective_pusher,
        telemetry,
    )
    client_auth = ClientAuthenticator(configured, storage)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        telemetry.start()
        try:
            yield
        finally:
            await tasks.shutdown()
            telemetry.shutdown()

    app = FastAPI(
        title="AIFlow Web Agent Service",
        version=__version__,
        description="Anonymous capability-token contexts for isolated Claude Code UIFlow development and device deployment.",
        lifespan=lifespan,
    )
    app.state.settings = configured
    app.state.storage = storage
    app.state.workspaces = workspaces
    app.state.tasks = tasks
    app.state.pusher = effective_pusher
    app.state.client_auth = client_auth
    app.state.telemetry = telemetry
    app.state.asr_client = effective_asr_client

    @app.middleware("http")
    async def authenticate_official_client(request: Request, call_next):
        request.state.client_key_id = None
        principal = None
        if client_auth.requires_authentication(request):
            try:
                principal = await client_auth.authenticate(request)
            except ClientAuthError as exc:
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": {"code": exc.code, "message": str(exc)}},
                )
            request.state.client_key_id = principal.key_id
        response = await call_next(request)
        if principal is not None:
            for name, value in client_auth.response_headers(principal, response.status_code).items():
                response.headers[name] = value
        return response

    app.mount("/client-assets", ClientStaticFiles(directory=WEB_DIR), name="client-assets")

    if configured.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(configured.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=[
                "Content-Type",
                TOKEN_HEADER,
                "Last-Event-ID",
                KEY_ID_HEADER,
                TIMESTAMP_HEADER,
                NONCE_HEADER,
                CONTENT_HASH_HEADER,
                SIGNATURE_HEADER,
            ],
            expose_headers=[
                "Content-Disposition",
                AUTH_VERSION_HEADER,
                RESPONSE_TIMESTAMP_HEADER,
                RESPONSE_SIGNATURE_HEADER,
            ],
        )

    async def require_context(
        token: Annotated[str | None, Header(alias=TOKEN_HEADER)] = None,
    ) -> dict[str, Any]:
        if not token:
            raise HTTPException(status_code=401, detail={"code": "context_token_required", "message": f"{TOKEN_HEADER} header is required"})
        context = storage.get_context_by_token(token)
        if not context:
            raise HTTPException(status_code=401, detail={"code": "invalid_context_token", "message": "context token is invalid"})
        return storage.touch_context(context["context_id"]) or context

    def owned_task(task_id: str, context: dict[str, Any]) -> dict[str, Any]:
        task = storage.get_owned_task(task_id, context["context_id"])
        if not task:
            raise HTTPException(status_code=404, detail={"code": "task_not_found", "message": "task does not exist in this context"})
        return task

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "time": utc_now()}

    @app.get("/client", include_in_schema=False)
    async def web_client() -> FileResponse:
        return FileResponse(
            WEB_DIR / "index.html",
            media_type="text/html",
            headers=CLIENT_CACHE_HEADERS,
        )

    @app.get("/ready")
    async def ready() -> dict[str, Any]:
        available = workspaces.available_skills()
        missing = [name for name in configured.enabled_skills if name not in available]
        return {
            "ready": not missing,
            "database": "ok",
            "skills": {"available": available, "missing": missing},
            "model": _model_name(configured),
            "system_status": tasks.system_status(),
        }

    @app.get("/api/v3/capabilities")
    async def capabilities() -> dict[str, Any]:
        return {
            **configured.public_dict(workspaces.available_skills()),
            "authentication": (
                "official client signature plus capability token"
                if configured.client_auth_enabled
                else "capability token (official client signature disabled)"
            ),
            "features": [
                "isolated_context",
                "claude_code_coding",
                "task_status_polling",
                "sse_events",
                "task_cancel",
                "server_auto_deploy",
                "agent_deploy",
                "direct_rerun",
                "file_upload_download",
                "conversation_reset",
                "session_history",
                "device_id_reconnect",
                "device_client_id_binding",
                "bounded_task_queue",
                "base64_image_audio_messages",
                "sauc_nostream_asr",
                "sauc_nostream_asr_stream_upload",
            ],
        }

    @app.post("/api/v3/asr")
    async def asr_endpoint(
        file: Annotated[UploadFile, File(description="WAV audio, mono or stereo")],
        language: Annotated[str | None, Form()] = None,
        enable_punc: Annotated[bool, Form()] = True,
        enable_itn: Annotated[bool, Form()] = True,
        enable_ddc: Annotated[bool, Form()] = True,
        show_utterances: Annotated[bool, Form()] = True,
        context: dict[str, Any] = Depends(require_context),
    ) -> dict[str, Any]:
        del context
        filename = file.filename or "audio.wav"
        if not filename.lower().endswith(".wav"):
            raise HTTPException(status_code=400, detail={"code": "invalid_audio", "message": "ASR currently accepts WAV audio only"})
        if file.content_type and file.content_type not in {"audio/wav", "audio/x-wav", "audio/wave", "application/octet-stream"}:
            raise HTTPException(status_code=400, detail={"code": "invalid_audio", "message": "audio content type must be audio/wav"})
        audio = await file.read(configured.max_upload_bytes + 1)
        await file.close()
        if not audio:
            raise HTTPException(status_code=400, detail={"code": "empty_file", "message": "audio file must not be empty"})
        if len(audio) > configured.max_upload_bytes:
            raise HTTPException(status_code=413, detail={"code": "file_too_large", "message": "audio file exceeds configured upload limit"})
        try:
            return await effective_asr_client.transcribe(
                audio,
                filename=filename,
                language=language.strip() if language and language.strip() else None,
                enable_punc=enable_punc,
                enable_itn=enable_itn,
                enable_ddc=enable_ddc,
                show_utterances=show_utterances,
            )
        except AsrError as exc:
            raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}) from exc

    @app.post("/api/v3/asr/stream")
    async def asr_stream_endpoint(
        request: Request,
        format: Annotated[str, Query()] = "pcm",
        rate: Annotated[int, Query(ge=8000, le=48000)] = 16000,
        bits: Annotated[int, Query()] = 16,
        channel: Annotated[int, Query(ge=1, le=2)] = 1,
        language: Annotated[str | None, Query()] = None,
        enable_punc: Annotated[bool, Query()] = True,
        enable_itn: Annotated[bool, Query()] = True,
        enable_ddc: Annotated[bool, Query()] = True,
        show_utterances: Annotated[bool, Query()] = True,
        context: dict[str, Any] = Depends(require_context),
    ) -> dict[str, Any]:
        del context
        normalized_format = format.strip().lower()
        if normalized_format != "pcm":
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_audio", "message": "streaming ASR accepts raw PCM; send format=pcm"},
            )
        if request.headers.get("content-type", "").split(";", 1)[0].lower() not in {
            "audio/pcm", "application/octet-stream", "audio/raw", "",
        }:
            raise HTTPException(status_code=400, detail={"code": "invalid_audio", "message": "streaming ASR content type must be audio/pcm"})

        total = 0

        async def bounded_chunks():
            nonlocal total
            async for chunk in request.stream():
                total += len(chunk)
                if total > configured.max_upload_bytes:
                    raise AsrError("audio stream exceeds configured upload limit", code="file_too_large", status_code=413)
                if chunk:
                    yield chunk

        try:
            return await effective_asr_client.transcribe_stream(
                bounded_chunks(), channels=channel, bits=bits, rate=rate,
                audio_format="pcm",
                language=language.strip() if language and language.strip() else None,
                enable_punc=enable_punc, enable_itn=enable_itn, enable_ddc=enable_ddc,
                show_utterances=show_utterances,
            )
        except AsrError as exc:
            raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}) from exc

    @app.get("/api/v3/system/status")
    async def system_status_endpoint() -> dict[str, Any]:
        return tasks.system_status()

    @app.post(
        "/api/v3/contexts",
        response_model=ContextResponse,
        responses={201: {"description": "New device session created"}},
    )
    async def create_context_endpoint(request: CreateContextRequest, response: Response) -> ContextResponse:
        context_id = "ctx_" + uuid.uuid4().hex[:16]
        access_token = new_token("ctx_secret_")
        conversation_id = "conv_" + uuid.uuid4().hex[:16]
        incoming_device = request.device.model_dump(exclude_none=True)
        existing = storage.get_context_by_device_id(incoming_device["device_id"])
        # Reconnects may come from older clients that do not know about MAC.
        # Merge only supplied values so an omitted optional field never erases
        # the value already bound to the device project.
        device = dict(existing["device"]) if existing else {}
        device.update(incoming_device)
        if existing and not storage.get_active_task(existing["context_id"]):
            workspaces.initialize(existing["context_id"], device)
        try:
            context, created = storage.connect_context(
                context_id,
                access_token,
                conversation_id,
                request.label,
                device,
                configured.max_sessions,
            )
        except SessionCapacityFull as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "session_capacity_full",
                    "message": str(exc),
                    "system_status": tasks.system_status(),
                },
            ) from exc
        try:
            if created:
                workspaces.initialize(context["context_id"], context["device"])
        except Exception:
            if created:
                storage.delete_context(context["context_id"])
            raise
        response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return ContextResponse(
            context_id=context["context_id"],
            device_id=context["device_id"],
            client_id=context["device"]["client_id"],
            mac_address=context["device"].get("mac_address"),
            access_token=access_token,
            conversation_id=context["conversation_id"],
            label=context["label"],
            device=DeviceInfo(**context["device"]),
            created_at=context["created_at"],
            model=_model_name(configured),
            created=created,
            system_status=tasks.system_status(),
        )

    @app.get("/api/v3/context", response_model=ContextInfoResponse)
    async def get_context_endpoint(context: dict[str, Any] = Depends(require_context)) -> ContextInfoResponse:
        active = storage.get_active_task(context["context_id"])
        return ContextInfoResponse(
            context_id=context["context_id"],
            device_id=context["device_id"],
            client_id=context["device"].get("client_id"),
            mac_address=context["device"].get("mac_address"),
            conversation_id=context["conversation_id"],
            label=context["label"],
            device=DeviceInfo(**context["device"]),
            created_at=context["created_at"],
            updated_at=context["updated_at"],
            model=_model_name(configured),
            active_task_id=active["task_id"] if active else None,
        )

    @app.patch("/api/v3/context/device", response_model=ContextInfoResponse)
    async def update_device_endpoint(
        request: UpdateDeviceRequest,
        context: dict[str, Any] = Depends(require_context),
    ) -> ContextInfoResponse:
        device = dict(context["device"])
        device.update(request.model_dump(exclude_unset=True))
        updated = storage.update_device(context["context_id"], DeviceInfo(**device).model_dump())
        workspaces.write_device_config(workspaces.workspace_for(context["context_id"]), updated["device"])
        active = storage.get_active_task(context["context_id"])
        return ContextInfoResponse(
            context_id=updated["context_id"],
            device_id=updated["device_id"],
            client_id=updated["device"].get("client_id"),
            mac_address=updated["device"].get("mac_address"),
            conversation_id=updated["conversation_id"],
            label=updated["label"],
            device=DeviceInfo(**updated["device"]),
            created_at=updated["created_at"],
            updated_at=updated["updated_at"],
            model=_model_name(configured),
            active_task_id=active["task_id"] if active else None,
        )

    @app.get("/api/v3/project")
    async def project_endpoint(context: dict[str, Any] = Depends(require_context)) -> dict[str, Any]:
        workspace = workspaces.workspace_for(context["context_id"])
        active = storage.get_active_task(context["context_id"])
        sessions = list_sessions(directory=str(workspace))
        return {
            "device_id": context["device_id"],
            "client_id": context["device"].get("client_id"),
            "mac_address": context["device"].get("mac_address"),
            "context_id": context["context_id"],
            "conversation_id": context["conversation_id"],
            "current_session_id": context.get("session_id"),
            "active_task_id": active["task_id"] if active else None,
            "files": workspaces.list_files(workspace),
            "sessions": [
                {
                    "session_id": item.session_id,
                    "summary": item.summary,
                    "last_modified": item.last_modified,
                    "file_size": item.file_size,
                }
                for item in sessions
            ],
        }

    @app.delete("/api/v3/context")
    async def delete_context_endpoint(
        confirm: bool = Query(False),
        context: dict[str, Any] = Depends(require_context),
    ) -> dict[str, Any]:
        if not confirm:
            raise HTTPException(status_code=400, detail={"code": "confirmation_required", "message": "pass confirm=true to delete this context"})
        active = storage.get_active_task(context["context_id"])
        if active:
            raise HTTPException(status_code=409, detail={"code": "active_task", "message": "cancel or wait for the active task first", "task_id": active["task_id"]})
        storage.delete_context(context["context_id"])
        workspaces.delete_context(context["context_id"])
        return {
            "deleted": True,
            "context_id": context["context_id"],
            "device_id": context["device_id"],
            "system_status": tasks.system_status(),
        }

    @app.post("/api/v3/tasks/coding", response_model=TaskCreatedResponse, status_code=status.HTTP_202_ACCEPTED)
    async def start_coding_endpoint(
        request: CodingTaskRequest,
        http_request: Request,
        context: dict[str, Any] = Depends(require_context),
    ) -> TaskCreatedResponse:
        caller_id = http_request.state.client_key_id or f"context:{context['context_id']}"
        quota = storage.reserve_ai_task(
            caller_id,
            per_client_minute=configured.max_ai_tasks_per_client_minute,
            per_client_day=configured.max_ai_tasks_per_client_day,
            global_day=configured.max_ai_tasks_global_day,
        )
        if quota != "ok":
            raise HTTPException(
                status_code=429,
                detail={
                    "code": f"ai_task_limit_{quota}",
                    "message": "AI task cost guard limit reached; retry after the current quota window",
                },
            )
        try:
            task, stream_token = await tasks.start_coding(context, request.model_dump())
        except TaskConflict as exc:
            raise HTTPException(status_code=409, detail={"code": "context_busy", "message": str(exc), "task_id": exc.task_id}) from exc
        except TaskQueueFull as exc:
            raise HTTPException(status_code=429, detail={"code": "task_queue_full", "message": str(exc), "system_status": tasks.system_status()}) from exc
        except AttachmentError as exc:
            raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}) from exc
        return _task_created(task, stream_token, context["device_id"], tasks)

    @app.post("/api/v3/tasks/direct-run", response_model=TaskCreatedResponse, status_code=status.HTTP_202_ACCEPTED)
    async def direct_run_endpoint(
        request: DirectRunRequest,
        context: dict[str, Any] = Depends(require_context),
    ) -> TaskCreatedResponse:
        try:
            task, stream_token = await tasks.start_direct_deploy(context, request.model_dump())
        except TaskConflict as exc:
            raise HTTPException(status_code=409, detail={"code": "context_busy", "message": str(exc), "task_id": exc.task_id}) from exc
        except TaskQueueFull as exc:
            raise HTTPException(status_code=429, detail={"code": "task_queue_full", "message": str(exc), "system_status": tasks.system_status()}) from exc
        return _task_created(task, stream_token, context["device_id"], tasks)

    @app.post("/api/v3/deployments/plan")
    async def deployment_plan_endpoint(
        request: DirectRunRequest,
        context: dict[str, Any] = Depends(require_context),
    ) -> dict[str, Any]:
        try:
            return await effective_pusher.plan(context, request.code_path, request.include_resources)
        except DeploymentError as exc:
            raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc

    @app.get("/api/v3/tasks/{task_id}", response_model=TaskStatusResponse)
    async def task_status_endpoint(
        task_id: str,
        context: dict[str, Any] = Depends(require_context),
    ) -> TaskStatusResponse:
        return TaskStatusResponse(**tasks.status(owned_task(task_id, context)))

    @app.get("/api/v3/tasks/{task_id}/events/history")
    async def task_events_history_endpoint(
        task_id: str,
        after: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=1000),
        context: dict[str, Any] = Depends(require_context),
    ) -> dict[str, Any]:
        owned_task(task_id, context)
        return {
            "device_id": context["device_id"],
            "task_id": task_id,
            "events": storage.list_events(task_id, after=after, limit=limit),
        }

    @app.get("/api/v3/tasks/{task_id}/events")
    async def task_events_endpoint(
        request: Request,
        task_id: str,
        after: int = Query(0, ge=0),
        stream_token: str | None = Query(None),
        context_token: Annotated[str | None, Header(alias=TOKEN_HEADER)] = None,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        authorized = bool(stream_token and storage.validate_stream_token(task_id, stream_token))
        if not authorized and context_token:
            context = storage.get_context_by_token(context_token)
            authorized = bool(context and storage.get_owned_task(task_id, context["context_id"]))
        if not authorized:
            raise HTTPException(status_code=401, detail={"code": "stream_not_authorized", "message": "valid context or task stream token required"})
        task = storage.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail={"code": "task_not_found", "message": "task not found"})
        cursor = after
        if last_event_id and last_event_id.isdigit():
            cursor = max(cursor, int(last_event_id))

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
                        configured.heartbeat_seconds - (time.monotonic() - last_heartbeat),
                    )
                    try:
                        await asyncio.wait_for(signal.wait(), timeout=heartbeat_wait)
                    except asyncio.TimeoutError:
                        status_payload = tasks.status(current) if current else {"task_id": task_id, "status": "missing"}
                        yield "event: heartbeat\ndata: " + json.dumps(status_payload, ensure_ascii=False, separators=(",", ":")) + "\n\n"
                        last_heartbeat = time.monotonic()
            finally:
                tasks.unsubscribe_events(task_id, signal)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    @app.post("/api/v3/tasks/{task_id}/cancel", response_model=TaskStatusResponse)
    async def cancel_task_endpoint(
        task_id: str,
        context: dict[str, Any] = Depends(require_context),
    ) -> TaskStatusResponse:
        try:
            task = await tasks.cancel(task_id, context["context_id"])
        except TaskNotFound as exc:
            raise HTTPException(status_code=404, detail={"code": "task_not_found", "message": "task does not exist in this context"}) from exc
        return TaskStatusResponse(**tasks.status(task))

    @app.post("/api/v3/conversation/reset")
    async def reset_conversation_endpoint(
        request: ResetConversationRequest,
        context: dict[str, Any] = Depends(require_context),
    ) -> dict[str, Any]:
        active = storage.get_active_task(context["context_id"])
        if active:
            raise HTTPException(status_code=409, detail={"code": "context_busy", "message": "cannot reset while a task is active", "task_id": active["task_id"]})
        conversation_id = "conv_" + uuid.uuid4().hex[:16]
        workspace = workspaces.workspace_for(context["context_id"])
        if not request.keep_files:
            workspaces.clear_user_files(workspace)
        storage.update_conversation(context["context_id"], conversation_id, None)
        return {
            "device_id": context["device_id"],
            "conversation_id": conversation_id,
            "files_kept": request.keep_files,
        }

    @app.get("/api/v3/conversations")
    async def list_conversations_endpoint(context: dict[str, Any] = Depends(require_context)) -> dict[str, Any]:
        workspace = workspaces.workspace_for(context["context_id"])
        sessions = list_sessions(directory=str(workspace))
        return {
            "device_id": context["device_id"],
            "current_conversation_id": context["conversation_id"],
            "current_session_id": context.get("session_id"),
            "sessions": [
                {
                    "session_id": item.session_id,
                    "summary": item.summary,
                    "last_modified": item.last_modified,
                    "file_size": item.file_size,
                }
                for item in sessions
            ],
        }

    @app.get("/api/v3/conversations/{session_id}/messages")
    async def conversation_messages_endpoint(
        session_id: str,
        limit: int | None = Query(None, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        context: dict[str, Any] = Depends(require_context),
    ) -> dict[str, Any]:
        workspace = workspaces.workspace_for(context["context_id"])
        known = {item.session_id for item in list_sessions(directory=str(workspace))}
        if session_id not in known:
            raise HTTPException(status_code=404, detail={"code": "session_not_found", "message": "session does not exist in this context"})
        messages = get_session_messages(session_id=session_id, directory=str(workspace), limit=limit, offset=offset)
        roots = [workspace, configured.data_dir]
        return {
            "device_id": context["device_id"],
            "session_id": session_id,
            "messages": [
                {
                    "type": item.type,
                    "uuid": item.uuid,
                    "message": _sanitize_event(
                        _redact_paths(item.message, roots),
                        workspace,
                        _event_secrets(context["device"]),
                        max_text_chars=None,
                        max_items=None,
                        max_depth=32,
                        redact_reasoning=False,
                    ),
                }
                for item in messages
            ],
        }

    @app.get("/api/v3/files", response_model=list[FileInfo])
    async def list_files_endpoint(context: dict[str, Any] = Depends(require_context)) -> list[FileInfo]:
        workspace = workspaces.workspace_for(context["context_id"])
        return [FileInfo(**item) for item in workspaces.list_files(workspace)]

    @app.post("/api/v3/files", response_model=FileInfo, status_code=status.HTTP_201_CREATED)
    async def upload_file_endpoint(
        file: Annotated[UploadFile, File()],
        path: Annotated[str | None, Form()] = None,
        context: dict[str, Any] = Depends(require_context),
    ) -> FileInfo:
        relative = path or file.filename
        if not relative:
            raise HTTPException(status_code=400, detail={"code": "file_path_required", "message": "file name or path is required"})
        workspace = workspaces.workspace_for(context["context_id"])
        try:
            destination = workspaces.safe_path(workspace, relative)
        except WorkspaceError as exc:
            raise HTTPException(status_code=400, detail={"code": "invalid_file_path", "message": str(exc)}) from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".upload-", delete=False) as temporary:
                temporary_path = Path(temporary.name)
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > configured.max_upload_bytes:
                        raise HTTPException(status_code=413, detail={"code": "file_too_large", "message": "file exceeds configured upload limit"})
                    temporary.write(chunk)
            if size == 0:
                raise HTTPException(status_code=400, detail={"code": "empty_file", "message": "file must not be empty"})
            os.replace(temporary_path, destination)
            temporary_path = None
        finally:
            await file.close()
            if temporary_path and temporary_path.exists():
                temporary_path.unlink()
        stat = destination.stat()
        return FileInfo(path=destination.relative_to(workspace).as_posix(), size=stat.st_size, modified_at=datetime_from_timestamp(stat.st_mtime))

    @app.get("/api/v3/files/{file_path:path}")
    async def download_file_endpoint(
        file_path: str,
        context: dict[str, Any] = Depends(require_context),
    ) -> FileResponse:
        workspace = workspaces.workspace_for(context["context_id"])
        try:
            path = workspaces.safe_path(workspace, file_path)
        except WorkspaceError as exc:
            raise HTTPException(status_code=400, detail={"code": "invalid_file_path", "message": str(exc)}) from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail={"code": "file_not_found", "message": "file not found"})
        return FileResponse(path, filename=path.name)

    return app


def datetime_from_timestamp(timestamp: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


app = create_app()
