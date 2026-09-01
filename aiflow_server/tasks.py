from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .agent import AgentCancelled, AgentError, ClaudeRunner
from .ai_quota import (
    AiQuotaAuthorization,
    AiQuotaClient,
    AiQuotaDenied,
    AiQuotaError,
    AiQuotaNotFound,
)
from .config import Settings
from .device_push import DeploymentError, DevicePusher
from .model_proxy import ModelProxyRegistry, ModelQuotaSession
from .storage import TERMINAL_STATUSES, Storage, new_token, utc_now
from .telemetry import TLS_EVENT_DATA_KEY, TlsTelemetry
from .workspaces import WorkspaceManager

AGENT_ACTIVITY_WRITE_INTERVAL_SECONDS = 1.0
AI_QUOTA_RECONCILE_INTERVAL_SECONDS = 30.0
TERMINAL_EVENT_TYPES = {"task_completed", "task_failed", "task_cancelled"}
LOGGER = logging.getLogger(__name__)


class TaskConflict(RuntimeError):
    def __init__(self, task_id: str):
        super().__init__("another task is active for this client context")
        self.task_id = task_id


class TaskQueueFull(RuntimeError):
    pass


class TaskNotFound(RuntimeError):
    pass


def _seconds_since(value: str | None) -> float | None:
    if not value:
        return None
    timestamp = datetime.fromisoformat(value)
    return max(0.0, (datetime.now(timezone.utc) - timestamp).total_seconds())


def _initial_agent_session_id(event_type: str, data: dict[str, Any]) -> str | None:
    if event_type != "agent_system" or data.get("subtype") != "init":
        return None
    system_data = data.get("data")
    if not isinstance(system_data, dict):
        return None
    session_id = system_data.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    return session_id.strip()


class TaskManager:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        workspaces: WorkspaceManager,
        runner: ClaudeRunner,
        pusher: DevicePusher,
        telemetry: TlsTelemetry | None = None,
        quota_client: AiQuotaClient | None = None,
    ):
        self.settings = settings
        self.storage = storage
        self.workspaces = workspaces
        self.runner = runner
        self.pusher = pusher
        self.quota_client = quota_client or AiQuotaClient(settings.ai_quota)
        self.telemetry = telemetry
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._event_subscribers: dict[str, set[asyncio.Event]] = {}
        self._last_agent_activity_write_at: dict[str, float] = {}
        self._start_lock = asyncio.Lock()
        self._execution_slots = asyncio.Semaphore(settings.max_concurrent_tasks)
        self._quota_reconcile_job: asyncio.Task[None] | None = None
        self.model_proxy_registry = ModelProxyRegistry(
            settings,
            storage,
            self.quota_client,
        )

    async def startup(self) -> None:
        if self.settings.ai_quota.enabled and self.settings.ai_quota.configured:
            self._quota_reconcile_job = asyncio.create_task(
                self._quota_reconcile_loop(),
                name="aiflow-ai-quota-reconcile",
            )

    async def _quota_reconcile_loop(self) -> None:
        """Retry durable quota accounting until shutdown instead of only at boot."""
        while True:
            try:
                await self._reconcile_ai_quota_reservations()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("AI quota reconciliation pass failed")
            await asyncio.sleep(AI_QUOTA_RECONCILE_INTERVAL_SECONDS)

    async def _reconcile_ai_quota_reservations(self) -> None:
        rows = await asyncio.to_thread(self.storage.list_open_ai_quota_reservations)
        for row in rows:
            try:
                local_status = str(row.get("status") or "UNKNOWN")
                authorization_id = row.get("authorization_id")
                if not isinstance(authorization_id, str) or not authorization_id:
                    authorization_id = None
                granted_tokens = row.get("granted_tokens")
                if (
                    isinstance(granted_tokens, bool)
                    or not isinstance(granted_tokens, int)
                    or granted_tokens <= 0
                ):
                    granted_tokens = None
                expires_at = row.get("expires_at")
                authorization = AiQuotaAuthorization(
                    request_id=row["request_id"],
                    authorization_id=authorization_id,
                    granted_tokens=granted_tokens,
                    expires_at=expires_at if isinstance(expires_at, str) else None,
                    quota={},
                )
                if local_status in {"SETTLEMENT_REQUIRED", "SETTLING"}:
                    input_tokens = row.get("input_tokens")
                    output_tokens = row.get("output_tokens")
                    cache_creation_input_tokens = row.get("cache_creation_input_tokens")
                    cache_read_input_tokens = row.get("cache_read_input_tokens")
                    if not all(
                        isinstance(value, int) and not isinstance(value, bool) and value >= 0
                        for value in (
                            input_tokens,
                            output_tokens,
                            cache_creation_input_tokens,
                            cache_read_input_tokens,
                        )
                    ):
                        LOGGER.error(
                            "Cannot reconcile AI quota settlement for request %s without trusted usage",
                            row["request_id"],
                        )
                        continue
                    cache_input_tokens = (
                        cache_creation_input_tokens + cache_read_input_tokens
                    )
                    if cache_input_tokens > input_tokens:
                        # Builds that briefly persisted the provider's uncached
                        # input count can be identified by this quota-contract
                        # violation. Repair it once before idempotent settlement.
                        input_tokens += cache_input_tokens
                        await asyncio.to_thread(
                            self.storage.update_ai_quota_status,
                            row["request_id"],
                            "SETTLEMENT_REQUIRED",
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            cache_creation_input_tokens=cache_creation_input_tokens,
                            cache_read_input_tokens=cache_read_input_tokens,
                        )
                    await self.quota_client.settle(
                        authorization,
                        input_tokens,
                        output_tokens,
                        cache_creation_input_tokens,
                        cache_read_input_tokens,
                    )
                    await asyncio.to_thread(
                        self.storage.update_ai_quota_status,
                        row["request_id"],
                        "SETTLED",
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_creation_input_tokens=cache_creation_input_tokens,
                        cache_read_input_tokens=cache_read_input_tokens,
                    )
                    continue
                if local_status == "USAGE_UNKNOWN":
                    state = await self.quota_client.status(row["request_id"])
                    remote_status = str(state.get("status") or "UNKNOWN")
                    if remote_status in {"SETTLED", "RELEASED", "EXPIRED"}:
                        await asyncio.to_thread(
                            self.storage.update_ai_quota_status,
                            row["request_id"],
                            remote_status,
                        )
                    else:
                        LOGGER.error(
                            "AI quota usage remains unknown for model request index %s in task %s",
                            row.get("request_index"),
                            row["task_id"],
                        )
                    continue
                if local_status == "AUTHORIZED":
                    # Authorization succeeded, but the process may have died
                    # before the request reached the model upstream. Query the
                    # authoritative state and close only the no-usage case.
                    try:
                        state = await self.quota_client.status(row["request_id"])
                    except AiQuotaNotFound:
                        await asyncio.to_thread(
                            self.storage.update_ai_quota_status,
                            row["request_id"],
                            "NOT_FOUND",
                        )
                        continue
                    remote_status = str(state.get("status") or "UNKNOWN")
                    if remote_status in {"AUTHORIZED", "ALLOWED"}:
                        await asyncio.to_thread(
                            self.storage.update_ai_quota_status,
                            row["request_id"],
                            "NO_USAGE",
                        )
                        continue
                    if remote_status == "SETTLED":
                        await asyncio.to_thread(
                            self.storage.update_ai_quota_status,
                            row["request_id"],
                            "SETTLED",
                        )
                        continue
                    if remote_status in {"RELEASED", "EXPIRED"}:
                        await asyncio.to_thread(
                            self.storage.update_ai_quota_status,
                            row["request_id"],
                            remote_status,
                        )
                        continue
                    # A legacy service may still expose RESERVED. Preserve the
                    # old cleanup behavior only for that explicit state.
                    if remote_status != "RESERVED":
                        LOGGER.error(
                            "AI quota authorization remains unresolved for request %s: status=%s",
                            row["request_id"],
                            remote_status,
                        )
                        continue
                if authorization.authorization_id is None:
                    try:
                        state = await self.quota_client.status(row["request_id"])
                    except AiQuotaNotFound:
                        await asyncio.to_thread(
                            self.storage.update_ai_quota_status,
                            row["request_id"],
                            "NOT_FOUND",
                        )
                        continue
                    remote_status = str(state.get("status") or "UNKNOWN")
                    if remote_status in {"AUTHORIZED", "ALLOWED"}:
                        await asyncio.to_thread(
                            self.storage.update_ai_quota_status,
                            row["request_id"],
                            "NO_USAGE",
                        )
                        continue
                    if remote_status != "RESERVED":
                        await asyncio.to_thread(
                            self.storage.update_ai_quota_status,
                            row["request_id"],
                            remote_status,
                        )
                        continue
                    state_authorization_id = state.get("authorizationId")
                    if not isinstance(state_authorization_id, str) or not state_authorization_id:
                        continue
                    state_granted_tokens = state.get(
                        "grantedTokens", state.get("reservedTokens")
                    )
                    authorization = AiQuotaAuthorization(
                        request_id=row["request_id"],
                        authorization_id=state_authorization_id,
                        granted_tokens=(
                            state_granted_tokens
                            if isinstance(state_granted_tokens, int)
                            and not isinstance(state_granted_tokens, bool)
                            and state_granted_tokens > 0
                            else None
                        ),
                        expires_at=(
                            state.get("expiresAt")
                            if isinstance(state.get("expiresAt"), str)
                            else None
                        ),
                        quota={},
                    )
                result = await self.quota_client.release(authorization, "AIFLOW_REQUEST_FAILED")
                status = str(result.get("status") or "RELEASED")
                await asyncio.to_thread(
                    self.storage.update_ai_quota_status,
                    row["request_id"],
                    status,
                    authorization_id=authorization.authorization_id,
                    granted_tokens=authorization.granted_tokens,
                    expires_at=authorization.expires_at,
                )
            except Exception as exc:  # noqa: BLE001 - one failed row must not stop reconciliation
                LOGGER.warning(
                    "Failed to reconcile AI quota request index %s for task %s: error_type=%s",
                    row.get("request_index"),
                    row["task_id"],
                    type(exc).__name__,
                )

    def subscribe_events(self, task_id: str) -> asyncio.Event:
        signal = asyncio.Event()
        self._event_subscribers.setdefault(task_id, set()).add(signal)
        return signal

    def unsubscribe_events(self, task_id: str, signal: asyncio.Event) -> None:
        subscribers = self._event_subscribers.get(task_id)
        if not subscribers:
            return
        subscribers.discard(signal)
        if not subscribers:
            self._event_subscribers.pop(task_id, None)

    def _publish_event(self, task_id: str, event_type: str, event: dict[str, Any]) -> dict[str, Any]:
        if event_type in TERMINAL_EVENT_TYPES:
            self._last_agent_activity_write_at.pop(task_id, None)
        for signal in tuple(self._event_subscribers.get(task_id, ())):
            signal.set()
        return event

    def _append_event(
        self,
        task_id: str,
        event_type: str,
        data: dict[str, Any],
        telemetry_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if telemetry_data is None:
            event = self.storage.append_event(task_id, event_type, data)
        else:
            event = self.storage.append_event(
                task_id,
                event_type,
                data,
                telemetry_data=telemetry_data,
            )
        return self._publish_event(task_id, event_type, event)

    async def _append_event_async(
        self,
        task_id: str,
        event_type: str,
        data: dict[str, Any],
        telemetry_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if telemetry_data is None:
            event = await asyncio.to_thread(
                self.storage.append_event,
                task_id,
                event_type,
                data,
            )
        else:
            event = await asyncio.to_thread(
                self.storage.append_event,
                task_id,
                event_type,
                data,
                telemetry_data=telemetry_data,
            )
        return self._publish_event(task_id, event_type, event)

    async def start_coding(self, context: dict[str, Any], request: dict[str, Any]) -> tuple[dict[str, Any], str]:
        return await self._start(context, "coding", request, request.get("prompt"), self._run_coding)

    async def start_direct_deploy(self, context: dict[str, Any], request: dict[str, Any]) -> tuple[dict[str, Any], str]:
        return await self._start(context, "direct_deploy", request, None, self._run_direct_deploy)

    async def _start(self, context, kind, request, prompt, worker):
        async with self._start_lock:
            active = self.storage.get_active_task(context["context_id"])
            if active:
                raise TaskConflict(active["task_id"])
            counts = self.storage.task_counts()
            capacity = self.settings.max_concurrent_tasks + self.settings.max_queued_tasks
            if counts["running"] + counts["queued"] >= capacity:
                raise TaskQueueFull("global task queue is full")
            task_id = "task_" + uuid.uuid4().hex[:16]
            stream_token = new_token("stream_")
            prepared_request = dict(request)
            attachments = prepared_request.pop("attachments", [])
            workspace = self.workspaces.workspace_for(context["context_id"])
            saved_attachments = []
            if kind == "coding":
                saved_attachments = self.workspaces.save_message_attachments(
                    workspace,
                    context["conversation_id"],
                    task_id,
                    attachments,
                )
                prepared_request["attachments"] = saved_attachments
            try:
                task = self.storage.create_task(
                    task_id,
                    context["context_id"],
                    stream_token,
                    kind,
                    prepared_request,
                    prompt=prompt,
                )
            except Exception:
                if saved_attachments:
                    self.workspaces.delete_task_inputs(
                        workspace, context["conversation_id"], task_id
                    )
                raise
            self._append_event(
                task_id,
                "task_queued",
                {
                    "status": "queued",
                    "stage": "queued",
                    "progress": 0,
                    "message": "Task accepted",
                    "attachment_count": len(saved_attachments),
                },
            )
            cancel_event = asyncio.Event()
            self._cancel_events[task_id] = cancel_event
            job = asyncio.create_task(
                self._run_limited(task_id, context, prepared_request, cancel_event, worker),
                name=task_id,
            )
            self._jobs[task_id] = job
            job.add_done_callback(lambda _: self._jobs.pop(task_id, None))
            return task, stream_token

    async def _run_limited(self, task_id, context, request, cancel_event, worker) -> None:
        try:
            async with self._execution_slots:
                current = self.storage.get_task(task_id)
                if not current or current["status"] in TERMINAL_STATUSES:
                    return
                if cancel_event.is_set():
                    self._finish_cancelled(task_id)
                    return
                await worker(task_id, context, request, cancel_event)
        except asyncio.CancelledError:
            current = self.storage.get_task(task_id)
            if current and current["status"] not in TERMINAL_STATUSES:
                self._finish_cancelled(task_id)
            raise

    async def _emit(self, task_id: str, event_type: str, data: dict[str, Any], agent_event: bool = False) -> None:
        payload = dict(data)
        telemetry_data = payload.pop(TLS_EVENT_DATA_KEY, None)
        if agent_event:
            payload.pop("progress", None)
        updates: dict[str, Any] = {}
        if agent_event:
            now = time.monotonic()
            previous = self._last_agent_activity_write_at.get(task_id)
            if previous is None or now - previous >= AGENT_ACTIVITY_WRITE_INTERVAL_SECONDS:
                updates["last_agent_event_at"] = utc_now()
                self._last_agent_activity_write_at[task_id] = now
        elif "progress" in payload:
            current = await asyncio.to_thread(self.storage.get_task, task_id)
            event_progress = int(payload["progress"])
            current_progress = int(current["progress"]) if current else 0
            if event_progress >= current_progress:
                updates["progress"] = event_progress
                if "stage" in payload:
                    updates["stage"] = payload["stage"]
        elif "stage" in payload:
            updates["stage"] = payload["stage"]
        if updates:
            await asyncio.to_thread(self.storage.update_task, task_id, **updates)
        await self._append_event_async(
            task_id,
            event_type,
            payload,
            telemetry_data=telemetry_data,
        )

    async def _heartbeat(self, task_id: str, cancel_event: asyncio.Event) -> None:
        while not cancel_event.is_set():
            await asyncio.to_thread(self.storage.heartbeat, task_id)
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=self.settings.heartbeat_seconds)
            except asyncio.TimeoutError:
                continue

    async def _run_coding(self, task_id: str, context: dict[str, Any], request: dict[str, Any], cancel_event: asyncio.Event) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(task_id, cancel_event))
        quota_session: ModelQuotaSession | None = None
        persisted_session_id: str | None = None
        try:
            now = utc_now()
            self.storage.update_task(
                task_id,
                status="running",
                stage="preparing_workspace",
                started_at=now,
                last_agent_event_at=now,
            )
            self._last_agent_activity_write_at[task_id] = time.monotonic()
            await self._emit(
                task_id,
                "task_started",
                {
                    "status": "running",
                    "stage": "preparing_workspace",
                    "message": "Preparing isolated workspace",
                    TLS_EVENT_DATA_KEY: {
                        "status": "running",
                        "stage": "preparing_workspace",
                    },
                },
            )
            mac = ""
            if self.settings.ai_quota.enabled:
                mac = str(context.get("device", {}).get("mac_address") or "").strip()
                if not mac:
                    raise AiQuotaError(
                        "device_mac_required_for_ai_quota",
                        "A paired device MAC is required before an AI request can be authorized",
                        retryable=False,
                    )

            self.workspaces.initialize(context["context_id"], context["device"])
            if cancel_event.is_set():
                raise AgentCancelled()
            self.storage.update_task(task_id, stage="coding")

            async def emit(event_type: str, data: dict[str, Any]) -> None:
                nonlocal persisted_session_id
                session_id = _initial_agent_session_id(event_type, data)
                if session_id and session_id != persisted_session_id:
                    persisted = await asyncio.to_thread(
                        self.storage.set_task_session_id,
                        context["context_id"],
                        task_id,
                        session_id,
                    )
                    if not persisted:
                        raise AgentError(
                            "session_persistence_failed",
                            "Claude session could not be linked to the active conversation",
                            retryable=False,
                        )
                    persisted_session_id = session_id
                await self._emit(task_id, event_type, data, agent_event=True)

            runner_context = {
                **context,
                "workspace": str(self.workspaces.workspace_for(context["context_id"])),
                "message_attachments": request.get("attachments", []),
            }
            if self.settings.ai_quota.enabled:
                quota_session = self.model_proxy_registry.create_session(
                    task_id,
                    mac,
                    lambda event_type, data: self._emit(task_id, event_type, data),
                )
                result = await self.runner.run(
                    task_id,
                    runner_context,
                    request["prompt"],
                    request.get("deploy_mode", "none"),
                    emit,
                    cancel_event,
                    quota_session=quota_session,
                )
                quota_session.raise_if_failed()
                await self.model_proxy_registry.remove(quota_session, "AIFLOW_REQUEST_FAILED")
                quota_session = None
            else:
                result = await self.runner.run(
                    task_id,
                    runner_context,
                    request["prompt"],
                    request.get("deploy_mode", "none"),
                    emit,
                    cancel_event,
                )
            if cancel_event.is_set():
                raise AgentCancelled()

            # The SDK's ResultMessage usage remains part of the task result for
            # observability only. Every individual HTTP model response has
            # already been settled by the model proxy.
            persisted = await asyncio.to_thread(
                self.storage.set_task_session_id,
                context["context_id"],
                task_id,
                result.session_id,
            )
            if not persisted:
                raise AgentError(
                    "session_persistence_failed",
                    "Claude session could not be linked to the active conversation",
                    retryable=False,
                )
            self.storage.update_task(task_id, stage="collecting_files")
            workspace = self.workspaces.workspace_for(context["context_id"])
            files = self.workspaces.list_files(workspace)
            for item in files:
                await self._emit(
                    task_id,
                    "file_ready",
                    {
                        **item,
                        "download_url": f"/api/v3/files/{item['path']}",
                        TLS_EVENT_DATA_KEY: dict(item),
                    },
                )

            deployment = None
            if request.get("deploy_mode") == "server":
                self.storage.update_task(task_id, stage="deploying")
                await self._emit(
                    task_id,
                    "deployment_started",
                    {
                        "stage": "deploying",
                        "message": "Pushing generated files to device",
                        TLS_EVENT_DATA_KEY: {"stage": "deploying"},
                    },
                )
                deployment = await self.pusher.deploy(context)
                await self._emit(task_id, "deployment_finished", {"stage": "finalizing", "result": deployment})

            payload = {"agent": result.as_dict(), "files": files, "deployment": deployment}
            self.storage.update_task(
                task_id,
                status="completed",
                stage="completed",
                progress=100,
                result_json=payload,
                finished_at=utc_now(),
            )
            self._append_event(
                task_id,
                "task_completed",
                {
                    "status": "completed",
                    "stage": "completed",
                    "progress": 100,
                    "result": payload,
                },
                telemetry_data={
                    "status": "completed",
                    "stage": "completed",
                    "progress": 100,
                    "references": {
                        "agent_result": True,
                        "file_ready_count": len(files),
                        "deployment_finished": deployment is not None,
                    },
                },
            )
        except AgentCancelled:
            if quota_session is not None:
                await self.model_proxy_registry.remove(quota_session, "CLIENT_CANCELLED")
                quota_session = None
            self._finish_cancelled(task_id)
        except AiQuotaDenied as exc:
            if quota_session is not None:
                await self.model_proxy_registry.remove(quota_session, "AIFLOW_REQUEST_FAILED")
                quota_session = None
            self._finish_failed(
                task_id,
                exc.code,
                str(exc),
                exc.retryable,
                {"quota_reason": exc.reason, "quota": exc.quota},
            )
        except AiQuotaError as exc:
            if quota_session is not None:
                await self.model_proxy_registry.remove(quota_session, "AIFLOW_REQUEST_FAILED")
                quota_session = None
            self._finish_failed(
                task_id,
                exc.code,
                str(exc),
                exc.retryable,
                {"quota_error_code": exc.service_error_code} if exc.service_error_code else None,
            )
        except (AgentError, DeploymentError) as exc:
            quota_error = quota_session.failure if quota_session is not None else None
            if quota_session is not None:
                await self.model_proxy_registry.remove(
                    quota_session,
                    "DEEPSEEK_REQUEST_FAILED" if isinstance(exc, AgentError) else "AIFLOW_REQUEST_FAILED",
                )
                quota_session = None
            if cancel_event.is_set():
                self._finish_cancelled(task_id)
            elif isinstance(quota_error, AiQuotaDenied):
                self._finish_failed(
                    task_id,
                    quota_error.code,
                    str(quota_error),
                    quota_error.retryable,
                    {"quota_reason": quota_error.reason, "quota": quota_error.quota},
                )
            elif quota_error is not None:
                self._finish_failed(
                    task_id,
                    quota_error.code,
                    str(quota_error),
                    quota_error.retryable,
                    {"quota_error_code": quota_error.service_error_code}
                    if quota_error.service_error_code
                    else None,
                )
            else:
                self._finish_failed(task_id, exc.code, str(exc), exc.retryable)
        except Exception as exc:  # noqa: BLE001 - normalize unexpected Agent failures into task errors
            quota_error = quota_session.failure if quota_session is not None else None
            if quota_session is not None:
                await self.model_proxy_registry.remove(quota_session, "AIFLOW_REQUEST_FAILED")
                quota_session = None
            if cancel_event.is_set():
                self._finish_cancelled(task_id)
            elif quota_error is not None:
                self._finish_failed(
                    task_id,
                    quota_error.code,
                    str(quota_error),
                    quota_error.retryable,
                )
            else:
                self._finish_failed(task_id, "internal_error", str(exc), False)
        finally:
            if quota_session is not None:
                await self.model_proxy_registry.remove(quota_session, "AIFLOW_REQUEST_FAILED")
            cancel_event.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            self._cancel_events.pop(task_id, None)

    async def _run_direct_deploy(self, task_id: str, context: dict[str, Any], request: dict[str, Any], cancel_event: asyncio.Event) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(task_id, cancel_event))
        try:
            self.storage.update_task(
                task_id,
                status="running",
                stage="validating_deployment",
                started_at=utc_now(),
            )
            await self._emit(
                task_id,
                "task_started",
                {
                    "status": "running",
                    "stage": "validating_deployment",
                    "message": "Validating saved code and device target",
                    TLS_EVENT_DATA_KEY: {
                        "status": "running",
                        "stage": "validating_deployment",
                    },
                },
            )
            if cancel_event.is_set():
                raise AgentCancelled()
            self.storage.update_task(task_id, stage="deploying")
            await self._emit(
                task_id,
                "deployment_started",
                {
                    "stage": "deploying",
                    "message": "Re-running saved code without Agent",
                    TLS_EVENT_DATA_KEY: {"stage": "deploying"},
                },
            )
            result = await self.pusher.deploy(
                context,
                code_path=request.get("code_path", "main.py"),
                include_resources=bool(request.get("include_resources", True)),
            )
            if cancel_event.is_set():
                raise AgentCancelled()
            self.storage.update_task(
                task_id,
                status="completed",
                stage="completed",
                progress=100,
                result_json=result,
                finished_at=utc_now(),
            )
            self._append_event(task_id, "task_completed", {"status": "completed", "stage": "completed", "progress": 100, "result": result})
        except AgentCancelled:
            self._finish_cancelled(task_id)
        except DeploymentError as exc:
            self._finish_failed(task_id, exc.code, str(exc), exc.retryable)
        except Exception as exc:  # noqa: BLE001 - direct-run must persist an explicit terminal error
            self._finish_failed(task_id, "internal_error", str(exc), False)
        finally:
            cancel_event.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            self._cancel_events.pop(task_id, None)

    def _finish_cancelled(self, task_id: str) -> None:
        current = self.storage.get_task(task_id)
        if not current or current["status"] in TERMINAL_STATUSES:
            return
        self.storage.update_task(
            task_id,
            status="cancelled",
            stage="cancelled",
            progress=100,
            finished_at=utc_now(),
        )
        self._append_event(task_id, "task_cancelled", {"status": "cancelled", "stage": "cancelled", "progress": 100})

    def _finish_failed(
        self,
        task_id: str,
        code: str,
        message: str,
        retryable: bool,
        details: dict[str, Any] | None = None,
    ) -> None:
        error = {"code": code, "message": message, "retryable": retryable}
        if details:
            error.update(details)
        self.storage.update_task(
            task_id,
            status="failed",
            stage="failed",
            progress=100,
            error_json=error,
            finished_at=utc_now(),
        )
        self._append_event(task_id, "task_failed", {"status": "failed", "stage": "failed", "progress": 100, "error": error})

    async def cancel(self, task_id: str, context_id: str) -> dict[str, Any]:
        task = self.storage.get_owned_task(task_id, context_id)
        if not task:
            raise TaskNotFound()
        if task["status"] in TERMINAL_STATUSES:
            return task
        self.storage.update_task(task_id, cancel_requested=1)
        self._append_event(task_id, "cancellation_requested", {"message": "Cancellation requested"})
        event = self._cancel_events.get(task_id)
        if event:
            event.set()
        if task["status"] == "queued":
            self._finish_cancelled(task_id)
            job = self._jobs.get(task_id)
            if job:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
            self._cancel_events.pop(task_id, None)
            return self.storage.get_owned_task(task_id, context_id)
        await self.runner.cancel(task_id)
        return self.storage.get_owned_task(task_id, context_id)

    def system_status(self) -> dict[str, Any]:
        sessions_used = self.storage.count_contexts()
        recently_active = self.storage.count_recent_contexts(
            self.settings.session_active_window_seconds
        )
        counts = self.storage.task_counts()
        task_capacity = self.settings.max_concurrent_tasks + self.settings.max_queued_tasks
        task_used = counts["running"] + counts["queued"]
        if sessions_used >= self.settings.max_sessions:
            state = "session_full"
        elif task_used >= task_capacity:
            state = "queue_full"
        elif counts["queued"]:
            state = "busy"
        else:
            state = "available"
        return {
            "state": state,
            "sessions": {
                "limit": self.settings.max_sessions,
                "used": sessions_used,
                "recently_active": recently_active,
                "activity_window_seconds": self.settings.session_active_window_seconds,
                "available": max(0, self.settings.max_sessions - sessions_used),
                "accepting_new": sessions_used < self.settings.max_sessions,
            },
            "tasks": {
                "concurrency_limit": self.settings.max_concurrent_tasks,
                "running": counts["running"],
                "queue_limit": self.settings.max_queued_tasks,
                "queued": counts["queued"],
                "total_capacity": task_capacity,
                "available": max(0, task_capacity - task_used),
                "accepting_new": task_used < task_capacity,
            },
            "conversation_logging": (
                self.telemetry.status()
                if self.telemetry
                else {
                    "enabled": False,
                    "pending_records": 0,
                    "oldest_created_at": None,
                    "max_attempts": 0,
                    "worker_running": False,
                }
            ),
        }

    def status(self, task: dict[str, Any]) -> dict[str, Any]:
        heartbeat_age = _seconds_since(task.get("heartbeat_at"))
        silence_age = _seconds_since(task.get("last_agent_event_at"))
        running = task["status"] == "running"
        heartbeat_stale = heartbeat_age is not None and heartbeat_age > self.settings.heartbeat_seconds * 3
        agent_silent = task["stage"] in {"coding", "agent_starting", "agent_working", "reading_context", "consulting_skills", "writing_files", "running_checks"} and silence_age is not None and silence_age > self.settings.agent_stall_seconds
        return {
            "task_id": task["task_id"],
            "device_id": self.storage.get_context(task["context_id"])["device_id"],
            "kind": task["kind"],
            "status": task["status"],
            "stage": task["stage"],
            "progress": task["progress"],
            "created_at": task["created_at"],
            "updated_at": task["updated_at"],
            "started_at": task["started_at"],
            "finished_at": task["finished_at"],
            "session_id": task["session_id"],
            "result": task["result"],
            "error": task["error"],
            "cancel_requested": task["cancel_requested"],
            "heartbeat_age_seconds": round(heartbeat_age, 3) if heartbeat_age is not None else None,
            "agent_silence_seconds": round(silence_age, 3) if silence_age is not None else None,
            "possibly_stalled": bool(running and (heartbeat_stale or agent_silent)),
            "queue_position": self.storage.queue_position(task["task_id"]),
            "last_event": self.storage.last_event(task["task_id"]),
        }

    async def shutdown(self) -> None:
        for event in self._cancel_events.values():
            event.set()
        for task_id, job in list(self._jobs.items()):
            task = self.storage.get_task(task_id)
            if task and task["status"] == "queued":
                self._finish_cancelled(task_id)
                job.cancel()
        await self.runner.shutdown()
        jobs = list(self._jobs.values())
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        if self._quota_reconcile_job is not None:
            self._quota_reconcile_job.cancel()
            await asyncio.gather(self._quota_reconcile_job, return_exceptions=True)
            self._quota_reconcile_job = None
        await self.model_proxy_registry.close()
        await self.quota_client.close()
