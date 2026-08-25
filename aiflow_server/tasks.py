from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .agent import AgentCancelled, AgentError, ClaudeRunner
from .ai_quota import AiQuotaAuthorization, AiQuotaClient, AiQuotaDenied, AiQuotaError, AiQuotaNotFound
from .config import Settings
from .device_push import DeploymentError, DevicePusher
from .storage import TERMINAL_STATUSES, Storage, new_token, utc_now
from .telemetry import TLS_EVENT_DATA_KEY, TlsTelemetry
from .workspaces import WorkspaceManager


AGENT_ACTIVITY_WRITE_INTERVAL_SECONDS = 1.0
AUTHORIZATION_EXPIRY_SAFETY_SECONDS = 5.0
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

    async def startup(self) -> None:
        if self.settings.ai_quota.enabled and self.settings.ai_quota.configured:
            self._quota_reconcile_job = asyncio.create_task(
                self._reconcile_ai_quota_reservations(),
                name="aiflow-ai-quota-reconcile",
            )

    async def _reconcile_ai_quota_reservations(self) -> None:
        rows = await asyncio.to_thread(self.storage.list_open_ai_quota_reservations)
        for row in rows:
            try:
                local_status = str(row.get("status") or "UNKNOWN")
                authorization_id = row.get("authorization_id")
                granted_tokens = row.get("granted_tokens")
                expires_at = row.get("expires_at")
                if not authorization_id:
                    try:
                        state = await self.quota_client.status(row["request_id"])
                    except AiQuotaNotFound:
                        await asyncio.to_thread(
                            self.storage.update_ai_quota_status,
                            row["task_id"],
                            "NOT_FOUND",
                        )
                        continue
                    remote_status = state.get("status")
                    if remote_status != "RESERVED":
                        await asyncio.to_thread(
                            self.storage.update_ai_quota_status,
                            row["task_id"],
                            str(remote_status or "UNKNOWN"),
                        )
                        continue
                    authorization_id = state.get("authorizationId")
                    granted_tokens = state.get("reservedTokens")
                    expires_at = state.get("expiresAt")
                if not isinstance(authorization_id, str) or not isinstance(granted_tokens, int):
                    continue
                authorization = AiQuotaAuthorization(
                    request_id=row["request_id"],
                    authorization_id=authorization_id,
                    granted_tokens=granted_tokens,
                    expires_at=expires_at if isinstance(expires_at, str) else None,
                    quota={},
                )
                if local_status == "SETTLING":
                    input_tokens = row.get("input_tokens")
                    output_tokens = row.get("output_tokens")
                    if not all(
                        isinstance(value, int) and not isinstance(value, bool) and value >= 0
                        for value in (input_tokens, output_tokens)
                    ):
                        LOGGER.error(
                            "Cannot reconcile AI quota settlement for task %s without trusted usage",
                            row["task_id"],
                        )
                        continue
                    await self.quota_client.settle(
                        authorization,
                        input_tokens,
                        output_tokens,
                    )
                    await asyncio.to_thread(
                        self.storage.update_ai_quota_status,
                        row["task_id"],
                        "SETTLED",
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    )
                    continue
                result = await self.quota_client.release(authorization, "AIFLOW_REQUEST_FAILED")
                status = str(result.get("status") or "RELEASED")
                await asyncio.to_thread(
                    self.storage.update_ai_quota_status,
                    row["task_id"],
                    status,
                    authorization_id=authorization.authorization_id,
                    granted_tokens=authorization.granted_tokens,
                    expires_at=authorization.expires_at,
                )
            except Exception as exc:
                LOGGER.warning(
                    "Failed to reconcile AI quota reservation for task %s: error_type=%s",
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
        authorization: AiQuotaAuthorization | None = None
        model_completed = False
        quota_finalized = False
        try:
            now = utc_now()
            initial_stage = "authorizing_ai_quota" if self.settings.ai_quota.enabled else "preparing_workspace"
            self.storage.update_task(
                task_id,
                status="running",
                stage=initial_stage,
                started_at=now,
                last_agent_event_at=now,
            )
            self._last_agent_activity_write_at[task_id] = time.monotonic()
            await self._emit(
                task_id,
                "task_started",
                {
                    "status": "running",
                    "stage": initial_stage,
                    "message": (
                        "Checking device AI token quota"
                        if self.settings.ai_quota.enabled
                        else "Preparing isolated workspace"
                    ),
                    TLS_EVENT_DATA_KEY: {
                        "status": "running",
                        "stage": initial_stage,
                    },
                },
            )
            if self.settings.ai_quota.enabled:
                mac = str(context.get("device", {}).get("mac_address") or "").strip()
                if not mac:
                    raise AiQuotaError(
                        "device_mac_required_for_ai_quota",
                        "A paired device MAC is required before an AI request can be authorized",
                        retryable=False,
                    )
                await asyncio.to_thread(
                    self.storage.begin_ai_quota_request,
                    task_id,
                    task_id,
                    self.settings.ai_quota.model,
                )
                authorization = await self.quota_client.authorize(task_id, mac)
                await asyncio.to_thread(
                    self.storage.authorize_ai_quota,
                    task_id,
                    authorization.authorization_id,
                    authorization.granted_tokens,
                    authorization.expires_at,
                )
                await self._emit(
                    task_id,
                    "ai_quota_authorized",
                    {
                        "stage": "preparing_workspace",
                        "message": "AI token quota authorized",
                        "granted_tokens": authorization.granted_tokens,
                        "expires_at": authorization.expires_at,
                        "quota": authorization.quota,
                    },
                )
                self.storage.update_task(task_id, stage="preparing_workspace")
                if cancel_event.is_set():
                    raise AgentCancelled()
            self.workspaces.initialize(context["context_id"], context["device"])
            if cancel_event.is_set():
                raise AgentCancelled()

            self.storage.update_task(task_id, stage="coding")

            async def emit(event_type: str, data: dict[str, Any]) -> None:
                await self._emit(task_id, event_type, data, agent_event=True)

            runner_context = {
                **context,
                "workspace": str(self.workspaces.workspace_for(context["context_id"])),
                "message_attachments": request.get("attachments", []),
            }
            timeout_seconds = (
                self._authorization_timeout_seconds(authorization)
                if authorization is not None
                else None
            )
            if timeout_seconds is not None and timeout_seconds <= 0:
                raise AiQuotaError(
                    "ai_quota_authorization_expired",
                    "AI quota authorization expired before the model request could start",
                    retryable=False,
                )
            runner_call = self.runner.run(
                task_id,
                runner_context,
                request["prompt"],
                request.get("deploy_mode", "none"),
                emit,
                cancel_event,
            )
            if timeout_seconds is not None:
                try:
                    result = await asyncio.wait_for(runner_call, timeout=timeout_seconds)
                except asyncio.TimeoutError as exc:
                    await self.runner.cancel(task_id)
                    raise AiQuotaError(
                        "ai_quota_authorization_expired",
                        "AI quota authorization expired before the Agent finished",
                        retryable=False,
                    ) from exc
            else:
                result = await runner_call
            model_completed = True
            if authorization is not None:
                try:
                    input_tokens, output_tokens = self._trusted_usage_tokens(result.usage)
                except AiQuotaError:
                    await asyncio.to_thread(
                        self.storage.update_ai_quota_status,
                        task_id,
                        "SETTLEMENT_REQUIRED",
                    )
                    raise
                await asyncio.to_thread(
                    self.storage.update_ai_quota_status,
                    task_id,
                    "SETTLING",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
                settlement = await self.quota_client.settle(
                    authorization,
                    input_tokens,
                    output_tokens,
                )
                await asyncio.to_thread(
                    self.storage.update_ai_quota_status,
                    task_id,
                    "SETTLED",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
                quota_finalized = True
                await self._emit(
                    task_id,
                    "ai_quota_settled",
                    self._public_settlement(settlement, input_tokens, output_tokens),
                )
            if cancel_event.is_set():
                raise AgentCancelled()
            self.storage.set_session_id(context["context_id"], result.session_id)
            self.storage.update_task(task_id, session_id=result.session_id, stage="collecting_files")
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
            if authorization is not None and not model_completed and not quota_finalized:
                await self._release_ai_quota(task_id, authorization, "CLIENT_CANCELLED")
            self._finish_cancelled(task_id)
        except AiQuotaDenied as exc:
            await asyncio.to_thread(self.storage.update_ai_quota_status, task_id, "DENIED")
            self._finish_failed(
                task_id,
                exc.code,
                str(exc),
                exc.retryable,
                {"quota_reason": exc.reason, "quota": exc.quota},
            )
        except AiQuotaError as exc:
            if authorization is not None and not model_completed and not quota_finalized:
                await self._release_ai_quota(task_id, authorization, "AIFLOW_REQUEST_FAILED")
            self._finish_failed(
                task_id,
                exc.code,
                str(exc),
                exc.retryable,
                {"quota_error_code": exc.service_error_code} if exc.service_error_code else None,
            )
        except (AgentError, DeploymentError) as exc:
            if authorization is not None and not model_completed and not quota_finalized:
                await self._release_ai_quota(
                    task_id,
                    authorization,
                    "DEEPSEEK_REQUEST_FAILED" if isinstance(exc, AgentError) else "AIFLOW_REQUEST_FAILED",
                )
            if cancel_event.is_set():
                self._finish_cancelled(task_id)
            else:
                self._finish_failed(task_id, exc.code, str(exc), exc.retryable)
        except Exception as exc:
            if authorization is not None and not model_completed and not quota_finalized:
                await self._release_ai_quota(task_id, authorization, "AIFLOW_REQUEST_FAILED")
            if cancel_event.is_set():
                self._finish_cancelled(task_id)
            else:
                self._finish_failed(task_id, "internal_error", str(exc), False)
        finally:
            cancel_event.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            self._cancel_events.pop(task_id, None)

    @staticmethod
    def _authorization_timeout_seconds(authorization: AiQuotaAuthorization) -> float:
        if not authorization.expires_at:
            raise AiQuotaError(
                "ai_quota_invalid_response",
                "AI quota authorization did not include an expiry time",
                retryable=False,
            )
        try:
            expires_at = datetime.fromisoformat(authorization.expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AiQuotaError(
                "ai_quota_invalid_response",
                "AI quota authorization included an invalid expiry time",
                retryable=False,
            ) from exc
        if expires_at.tzinfo is None:
            raise AiQuotaError(
                "ai_quota_invalid_response",
                "AI quota authorization expiry time must include a timezone",
                retryable=False,
            )
        return (
            expires_at.astimezone(timezone.utc) - datetime.now(timezone.utc)
        ).total_seconds() - AUTHORIZATION_EXPIRY_SAFETY_SECONDS

    @staticmethod
    def _trusted_usage_tokens(usage: dict[str, Any]) -> tuple[int, int]:
        def token_value(*names: str) -> int:
            for name in names:
                value = usage.get(name)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    return value
            raise AiQuotaError(
                "ai_quota_usage_missing",
                "Trusted model token usage was unavailable, so AI quota settlement could not be completed",
                retryable=False,
            )

        return token_value("input_tokens", "inputTokens"), token_value("output_tokens", "outputTokens")

    @staticmethod
    def _public_settlement(
        settlement: dict[str, Any],
        input_tokens: int,
        output_tokens: int,
    ) -> dict[str, Any]:
        public = {
            "stage": "collecting_files",
            "message": "AI token usage settled",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "actual_tokens": settlement.get("actualTokens", input_tokens + output_tokens),
            "released_tokens": settlement.get("releasedTokens"),
            "confirmed_by_status": bool(settlement.get("confirmedByStatus")),
        }
        for source, target in (
            ("dailyFreeAvailableTokens", "daily_free_available_tokens"),
            ("lifetimeFreeAvailableTokens", "lifetime_free_available_tokens"),
            ("effectiveFreeAvailableTokens", "effective_free_available_tokens"),
            ("paidAvailableTokens", "paid_available_tokens"),
        ):
            if isinstance(settlement.get(source), int):
                public[target] = settlement[source]
        return public

    async def _release_ai_quota(
        self,
        task_id: str,
        authorization: AiQuotaAuthorization,
        reason: str,
    ) -> bool:
        try:
            result = await self.quota_client.release(authorization, reason)
        except AiQuotaError as exc:
            await self._emit(
                task_id,
                "ai_quota_release_failed",
                {
                    "message": "AI token quota release could not be confirmed",
                    "retryable": exc.retryable,
                },
            )
            return False
        remote_status = str(result.get("status") or "RELEASED")
        await asyncio.to_thread(
            self.storage.update_ai_quota_status,
            task_id,
            remote_status,
        )
        await self._emit(
            task_id,
            "ai_quota_released",
            {
                "message": "AI token quota reservation released",
                "reason": reason,
                "status": remote_status,
                "confirmed_by_status": bool(result.get("confirmedByStatus")),
            },
        )
        return True

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
        except Exception as exc:
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
            await asyncio.gather(self._quota_reconcile_job, return_exceptions=True)
        await self.quota_client.close()
