from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aiflow_server.agent import AgentCancelled, AgentRunResult
from aiflow_server.ai_quota import AiQuotaAuthorization, AiQuotaDenied, AiQuotaError
from aiflow_server.app import TOKEN_HEADER, create_app
from aiflow_server.config import load_settings


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.cancelled = set()
        self.active = 0
        self.max_active = 0

    async def run(self, task_id, context, prompt, deploy_mode, emit, cancel_event):
        self.calls.append(
            (
                task_id,
                context["context_id"],
                prompt,
                deploy_mode,
                context.get("message_attachments", []),
            )
        )
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if prompt == "slow-before-model":
            while not cancel_event.is_set():
                await asyncio.sleep(0.01)
            self.active -= 1
            raise AgentCancelled()
        await emit("model_request_started", {"stage": "coding"})
        await emit("tool_started", {"stage": "writing_files", "progress": 60, "message": "Writing main.py"})
        await emit("agent_status", {"stage": "agent_starting", "progress": 20, "message": "late SDK status"})
        if prompt == "thinking":
            await emit(
                "agent_reasoning",
                {
                    "response_id": "msg-thinking-api",
                    "block_index": 0,
                    "finalized": False,
                    "thinking": "first ",
                },
            )
            await emit(
                "agent_reasoning",
                {
                    "response_id": "msg-thinking-api",
                    "block_index": 0,
                    "finalized": True,
                    "thinking": "first second",
                },
            )
        workspace = Path(context["workspace"])
        workspace.joinpath("main.py").write_text("print('isolated')\n", encoding="utf-8")
        try:
            if prompt == "slow-with-usage":
                await emit(
                    "assistant_message_started",
                    {
                        "usage": {
                            "input_tokens": 7,
                            "output_tokens": 4,
                            "cache_creation_input_tokens": 2,
                            "cache_read_input_tokens": 3,
                        }
                    },
                )
                while not cancel_event.is_set():
                    await asyncio.sleep(0.01)
                raise AgentCancelled()
            if prompt == "slow":
                while not cancel_event.is_set():
                    await asyncio.sleep(0.01)
                raise AgentCancelled()
            await asyncio.sleep(0.3 if prompt == "delay" else 0.02)
            return AgentRunResult(
                session_id="00000000-0000-4000-8000-000000000001",
                usage={
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_creation_input_tokens": 2,
                    "cache_read_input_tokens": 3,
                },
                total_cost_usd=0.001,
                duration_ms=20,
                num_turns=1,
                stop_reason="end_turn",
            )
        finally:
            self.active -= 1

    async def cancel(self, task_id):
        self.cancelled.add(task_id)

    async def shutdown(self):
        return None


class FakePusher:
    def __init__(self):
        self.deployments = []

    async def plan(self, context, code_path, include_resources):
        return {
            "ok": True,
            "executed": False,
            "code": code_path,
            "include_resources": include_resources,
            "target": "masked",
        }

    async def deploy(self, context, code_path="main.py", include_resources=True):
        self.deployments.append((context["context_id"], code_path, include_resources))
        await asyncio.sleep(0.01)
        return {"ok": True, "action": "direct_deploy", "steps": [{"chunkCount": 1}]}


class FakeQuotaClient:
    def __init__(
        self,
        deny_reason: str | None = None,
        settle_error: AiQuotaError | None = None,
    ):
        self.deny_reason = deny_reason
        self.settle_error = settle_error
        self.runner = None
        self.authorize_calls = []
        self.settle_calls = []
        self.release_calls = []
        self.closed = False

    async def authorize(self, request_id, mac):
        if self.runner is not None:
            assert self.runner.calls == []
        self.authorize_calls.append((request_id, mac))
        if self.deny_reason:
            raise AiQuotaDenied(
                self.deny_reason,
                {"effectiveFreeAvailableTokens": 0},
            )
        return AiQuotaAuthorization(
            request_id=request_id,
            authorization_id="qa_fake_authorization",
            granted_tokens=500000,
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
            quota={"effectiveFreeAvailableTokens": 1500000},
        )

    async def settle(
        self,
        authorization,
        input_tokens,
        output_tokens,
        cache_creation_input_tokens,
        cache_read_input_tokens,
    ):
        assert self.runner is None or self.runner.calls
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
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "cacheCreationInputTokens": cache_creation_input_tokens,
            "cacheReadInputTokens": cache_read_input_tokens,
            "releasedTokens": authorization.granted_tokens - input_tokens - output_tokens,
            "effectiveFreeAvailableTokens": 1499985,
        }

    async def release(self, authorization, reason):
        self.release_calls.append((authorization.request_id, reason))
        return {"status": "RELEASED"}

    async def status(self, _request_id):
        raise AssertionError("status should not be needed in the normal fake flow")

    async def close(self):
        self.closed = True


@pytest.fixture()
def service(tmp_path):
    base = load_settings()
    settings = replace(
        base,
        data_dir=tmp_path / "data",
        heartbeat_seconds=1,
        agent_stall_seconds=2,
        ai_quota=replace(base.ai_quota, enabled=False),
    )
    runner = FakeRunner()
    pusher = FakePusher()
    app = create_app(settings, runner=runner, pusher=pusher)
    with TestClient(app) as client:
        yield client, app, runner, pusher


def create_context(client: TestClient, suffix: str, mac_address: str | None = None):
    device = {
        "device_id": f"device-{suffix}",
        "client_id": f"client-{suffix}",
        "product": "CoreS3",
    }
    if mac_address:
        device["mac_address"] = mac_address
    response = client.post(
        "/api/v3/contexts",
        json={
            "label": f"browser-{suffix}",
            "device": device,
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["device_id"] == f"device-{suffix}"
    assert payload["client_id"] == f"client-{suffix}"
    assert payload["created"] is True
    return payload, {TOKEN_HEADER: payload["access_token"]}


def quota_service(
    tmp_path,
    *,
    deny_reason: str | None = None,
    settle_error: AiQuotaError | None = None,
):
    base = load_settings()
    settings = replace(
        base,
        data_dir=tmp_path / "quota-data",
        heartbeat_seconds=1,
        ai_quota=replace(
            base.ai_quota,
            enabled=True,
            hmac_secret="fake-quota-secret-with-at-least-32-bytes",
        ),
    )
    runner = FakeRunner()
    quota = FakeQuotaClient(deny_reason, settle_error)
    quota.runner = runner
    app = create_app(
        settings,
        runner=runner,
        pusher=FakePusher(),
        quota_client=quota,
    )
    return app, runner, quota


def wait_terminal(client: TestClient, task_id: str, headers: dict[str, str], timeout: float = 3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v3/tasks/{task_id}", headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] in {"completed", "failed", "cancelled"}:
            return payload
        time.sleep(0.02)
    raise AssertionError("task did not finish")


def test_ai_quota_authorizes_before_runner_and_settles_trusted_usage(tmp_path):
    app, runner, quota = quota_service(tmp_path)
    with TestClient(app) as client:
        _, headers = create_context(client, "quota-success", "AA:BB:CC:DD:EE:01")
        response = client.post(
            "/api/v3/tasks/coding",
            headers=headers,
            json={"prompt": "build quota app", "deploy_mode": "none"},
        )
        assert response.status_code == 202, response.text
        task_id = response.json()["task_id"]
        terminal = wait_terminal(client, task_id, headers)
        history = client.get(
            f"/api/v3/tasks/{task_id}/events/history",
            headers=headers,
        ).json()["events"]

        assert terminal["status"] == "completed"
        assert quota.authorize_calls == [(task_id, "AA:BB:CC:DD:EE:01")]
        assert quota.settle_calls == [(task_id, 15, 5, 2, 3)]
        assert quota.release_calls == []
        assert runner.calls
        event_types = [event["type"] for event in history]
        assert event_types.index("ai_quota_authorized") < event_types.index("agent_status")
        assert event_types.index("agent_status") < event_types.index("ai_quota_settled")
        reservation = app.state.storage.get_ai_quota_reservation(task_id)
        assert reservation["status"] == "SETTLED"
        assert reservation["input_tokens"] == 15
        assert reservation["output_tokens"] == 5
        assert reservation["cache_creation_input_tokens"] == 2
        assert reservation["cache_read_input_tokens"] == 3
        settlement = next(event for event in history if event["type"] == "ai_quota_settled")
        assert settlement["data"]["input_tokens"] == 15
        assert settlement["data"]["cache_creation_input_tokens"] == 2
        assert settlement["data"]["cache_read_input_tokens"] == 3
        assert settlement["data"]["actual_tokens"] == 20

    assert quota.closed is True


def test_ai_quota_denial_fails_task_without_calling_runner(tmp_path):
    app, runner, quota = quota_service(
        tmp_path,
        deny_reason="DAILY_FREE_QUOTA_EXHAUSTED",
    )
    with TestClient(app) as client:
        _, headers = create_context(client, "quota-denied", "AA:BB:CC:DD:EE:02")
        response = client.post(
            "/api/v3/tasks/coding",
            headers=headers,
            json={"prompt": "must not reach model", "deploy_mode": "none"},
        )
        assert response.status_code == 202, response.text
        task_id = response.json()["task_id"]
        terminal = wait_terminal(client, task_id, headers)

        assert terminal["status"] == "failed"
        assert terminal["error"]["code"] == "ai_quota_denied"
        assert terminal["error"]["quota_reason"] == "DAILY_FREE_QUOTA_EXHAUSTED"
        assert terminal["error"]["quota"]["effectiveFreeAvailableTokens"] == 0
        assert runner.calls == []
        assert quota.settle_calls == []
        assert app.state.storage.get_ai_quota_reservation(task_id)["status"] == "DENIED"


def test_ai_quota_settlement_failure_keeps_trusted_usage_for_reconciliation(tmp_path):
    app, runner, quota = quota_service(
        tmp_path,
        settle_error=AiQuotaError(
            "ai_quota_transport_error",
            "settlement response was unavailable",
            retryable=True,
        ),
    )
    with TestClient(app) as client:
        _, headers = create_context(client, "quota-settle-failed", "AA:BB:CC:DD:EE:04")
        response = client.post(
            "/api/v3/tasks/coding",
            headers=headers,
            json={"prompt": "settlement must be retried", "deploy_mode": "none"},
        )
        assert response.status_code == 202, response.text
        task_id = response.json()["task_id"]
        terminal = wait_terminal(client, task_id, headers)

        assert terminal["status"] == "failed"
        assert terminal["error"]["code"] == "ai_quota_transport_error"
        assert runner.calls
        assert quota.release_calls == []
        reservation = app.state.storage.get_ai_quota_reservation(task_id)
        assert reservation["status"] == "SETTLING"
        assert reservation["input_tokens"] == 15
        assert reservation["output_tokens"] == 5
        assert reservation["cache_creation_input_tokens"] == 2
        assert reservation["cache_read_input_tokens"] == 3


def test_ai_quota_keeps_reservation_when_running_task_is_cancelled_without_usage(tmp_path):
    app, _, quota = quota_service(tmp_path)
    with TestClient(app) as client:
        _, headers = create_context(client, "quota-cancel", "AA:BB:CC:DD:EE:03")
        response = client.post(
            "/api/v3/tasks/coding",
            headers=headers,
            json={"prompt": "slow", "deploy_mode": "none"},
        )
        assert response.status_code == 202, response.text
        task_id = response.json()["task_id"]
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            reservation = app.state.storage.get_ai_quota_reservation(task_id)
            if reservation and reservation["status"] == "USAGE_UNKNOWN":
                break
            time.sleep(0.01)
        cancelled = client.post(f"/api/v3/tasks/{task_id}/cancel", headers=headers)
        assert cancelled.status_code == 200
        terminal = wait_terminal(client, task_id, headers)

        assert terminal["status"] == "cancelled"
        assert quota.release_calls == []
        assert quota.settle_calls == []
        assert app.state.storage.get_ai_quota_reservation(task_id)["status"] == "USAGE_UNKNOWN"


def test_ai_quota_releases_reservation_when_cancelled_before_model_request(tmp_path):
    app, _, quota = quota_service(tmp_path)
    with TestClient(app) as client:
        _, headers = create_context(client, "quota-cancel-before-model", "AA:BB:CC:DD:EE:05")
        response = client.post(
            "/api/v3/tasks/coding",
            headers=headers,
            json={"prompt": "slow-before-model", "deploy_mode": "none"},
        )
        assert response.status_code == 202, response.text
        task_id = response.json()["task_id"]
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            running = client.get(f"/api/v3/tasks/{task_id}", headers=headers).json()
            if running["stage"] == "coding":
                break
            time.sleep(0.01)
        assert client.post(f"/api/v3/tasks/{task_id}/cancel", headers=headers).status_code == 200
        assert wait_terminal(client, task_id, headers)["status"] == "cancelled"

        assert quota.release_calls == [(task_id, "CLIENT_CANCELLED")]
        assert quota.settle_calls == []
        assert app.state.storage.get_ai_quota_reservation(task_id)["status"] == "RELEASED"


def test_ai_quota_settles_partial_usage_when_running_task_is_cancelled(tmp_path):
    app, _, quota = quota_service(tmp_path)
    with TestClient(app) as client:
        _, headers = create_context(client, "quota-cancel-with-usage", "AA:BB:CC:DD:EE:06")
        response = client.post(
            "/api/v3/tasks/coding",
            headers=headers,
            json={"prompt": "slow-with-usage", "deploy_mode": "none"},
        )
        assert response.status_code == 202, response.text
        task_id = response.json()["task_id"]
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            reservation = app.state.storage.get_ai_quota_reservation(task_id)
            if reservation and reservation["status"] == "SETTLEMENT_REQUIRED":
                break
            time.sleep(0.01)
        assert client.post(f"/api/v3/tasks/{task_id}/cancel", headers=headers).status_code == 200
        assert wait_terminal(client, task_id, headers)["status"] == "cancelled"

        assert quota.release_calls == []
        assert quota.settle_calls == [(task_id, 12, 4, 2, 3)]
        assert app.state.storage.get_ai_quota_reservation(task_id)["status"] == "SETTLED"


def test_ai_quota_requires_mac_for_coding_but_not_direct_run(tmp_path):
    app, runner, quota = quota_service(tmp_path)
    with TestClient(app) as client:
        _, headers = create_context(client, "quota-no-mac")
        rejected = client.post(
            "/api/v3/tasks/coding",
            headers=headers,
            json={"prompt": "must have mac", "deploy_mode": "none"},
        )
        assert rejected.status_code == 422
        assert rejected.json()["detail"]["code"] == "device_mac_required_for_ai_quota"

        client.post(
            "/api/v3/files",
            headers=headers,
            files={"file": ("main.py", b"print('rerun')\n", "text/plain")},
        )
        direct = client.post(
            "/api/v3/tasks/direct-run",
            headers=headers,
            json={"code_path": "main.py", "include_resources": False},
        )
        assert direct.status_code == 202, direct.text
        assert wait_terminal(client, direct.json()["task_id"], headers)["status"] == "completed"

        assert runner.calls == []
        assert quota.authorize_calls == []
        assert quota.settle_calls == []
        assert quota.release_calls == []


def test_built_in_web_client_is_served(service):
    client, _, _, _ = service

    page = client.get("/client")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert 'id="device-id"' in page.text
    assert 'id="client-id"' in page.text
    assert 'id="mac-address"' in page.text
    assert 'id="runtime-log"' in page.text
    assert 'id="agent-log"' in page.text
    assert 'id="raw-stream"' in page.text
    assert 'id="technical-toggle"' not in page.text
    assert 'id="task-progress"' not in page.text
    assert 'class="progress-shell"' not in page.text
    assert 'id="agent-summary"' not in page.text
    assert 'src="/client-assets/assistant-stream.js?v=20260731-stream-perf"' in page.text
    assert 'src="/client-assets/app.js?v=20260731-stream-perf"' in page.text
    assert page.headers["cache-control"] == "no-store, max-age=0"

    script = client.get("/client-assets/app.js")
    stream_state = client.get("/client-assets/assistant-stream.js")
    stylesheet = client.get("/client-assets/app.css")
    assert script.status_code == 200
    assert stream_state.status_code == 200
    assert stylesheet.status_code == 200
    assert script.headers["cache-control"] == "no-store, max-age=0"
    assert stream_state.headers["cache-control"] == "no-store, max-age=0"
    assert stylesheet.headers["cache-control"] == "no-store, max-age=0"
    assert "X-AIFlow-Context-Token" in script.text
    assert 'appendEvent("heartbeat", payload)' not in script.text
    assert 'type !== "heartbeat"' not in script.text
    assert 'RUNTIME_EVENT_TYPES.has(type)' in script.text
    assert '"ai_quota_settlement_pending"' in script.text
    assert "cache_creation_input_tokens" in script.text
    assert "cache_read_input_tokens" in script.text
    assert 'assistant_text_delta' in script.text
    assert 'tool_started' in script.text
    assert 'events/history?after=${after}&limit=1000' in script.text
    assert "状态已更新" not in script.text
    assert "内部推理内容不公开" not in script.text
    assert "模型正在理解需求并规划实现步骤" not in script.text
    assert "模型正在检查已有信息和工具结果" not in script.text
    assert "调用 ${toolName(data)}" not in script.text
    assert "function appendRawEvent(type, event)" in script.text
    assert "const RAW_STREAM_VISIBLE_LIMIT = 2000" in script.text
    assert "function pruneRawWindow()" in script.text
    assert "rawTextNode" not in script.text
    assert "function flushRawEvents()" in script.text
    assert "function appendSseEvent(type, event)" in script.text
    assert "appendEvent(" not in script.text
    assert "requestAnimationFrame(() => flushAssistantEntry(entry))" in script.text
    assert "applyAssistantStreamEvent(state.assistantRows, type, event)" in script.text
    assert "applyReasoningStreamEvent(state.reasoningRows, type, event)" in script.text
    assert "function applyAssistantStreamEvent(entries, type, event)" in stream_state.text
    assert "function applyReasoningStreamEvent(entries, type, event)" in stream_state.text
    assert "function blockIdentity(data, event)" in stream_state.text
    assert "finalTexts" not in script.text + stream_state.text
    assert "setTimeout(flushRawEvents, 50)" in script.text
    assert "startPolling(task.task_id, 10000)" in script.text
    assert "setInterval(poll, 1800)" not in script.text
    assert "setInterval(refreshService, 5000)" not in script.text
    assert "setInterval(refreshCapacity, 20000)" in script.text
    assert "appendStreamProtocolEvent" not in script.text
    assert "模型正在组织工具参数" not in script.text
    assert "已聚合" not in script.text
    assert 'classList.toggle("show-technical"' not in script.text
    assert '`${progress}%`' not in script.text
    assert "ANTHROPIC_AUTH_TOKEN" not in page.text + script.text


def test_attachment_name_is_required_in_openapi(service):
    client, _, _, _ = service
    response = client.get("/openapi.json")
    assert response.status_code == 200
    attachment_schema = response.json()["components"]["schemas"]["Base64Attachment"]
    assert "name" in attachment_schema["required"]
    assert attachment_schema["properties"]["name"]["description"] == (
        "Client-provided file name used when the attachment is saved"
    )


def test_context_isolation_background_status_and_events(service):
    client, app, runner, _ = service
    first, first_headers = create_context(client, "aa")
    second, second_headers = create_context(client, "bb")

    created = client.post(
        "/api/v3/tasks/coding",
        headers=first_headers,
        json={"prompt": "build app", "deploy_mode": "none"},
    )
    assert created.status_code == 202, created.text
    task = created.json()

    assert client.get(task["status_url"], headers=second_headers).status_code == 404
    terminal = wait_terminal(client, task["task_id"], first_headers)
    assert terminal["status"] == "completed"
    assert terminal["possibly_stalled"] is False
    assert terminal["result"]["files"][0]["path"] == "main.py"

    events = client.get(
        f"/api/v3/tasks/{task['task_id']}/events/history",
        headers=first_headers,
    ).json()["events"]
    event_types = [event["type"] for event in events]
    assert event_types[0] == "task_queued"
    assert "tool_started" in event_types
    assert event_types[-1] == "task_completed"
    agent_events = [event for event in events if event["type"] in {"tool_started", "agent_status"}]
    assert agent_events
    assert all("progress" not in event["data"] for event in agent_events)

    first_workspace = app.state.workspaces.workspace_for(first["context_id"])
    second_workspace = app.state.workspaces.workspace_for(second["context_id"])
    assert first_workspace != second_workspace
    assert (first_workspace / "main.py").is_file()
    assert not (second_workspace / "main.py").exists()
    assert runner.calls[0][1] == first["context_id"]


def test_task_event_subscribers_wake_immediately_and_cleanup(service):
    client, app, _, _ = service
    _, headers = create_context(client, "event-signal")
    created = client.post(
        "/api/v3/tasks/coding",
        headers=headers,
        json={"prompt": "build app", "deploy_mode": "none"},
    ).json()
    wait_terminal(client, created["task_id"], headers)

    task_id = created["task_id"]
    first = app.state.tasks.subscribe_events(task_id)
    second = app.state.tasks.subscribe_events(task_id)
    unrelated = app.state.tasks.subscribe_events("other-task")
    app.state.tasks._append_event(task_id, "agent_warning", {"message": "fixture"})

    assert first.is_set()
    assert second.is_set()
    assert not unrelated.is_set()

    app.state.tasks.unsubscribe_events(task_id, first)
    app.state.tasks.unsubscribe_events(task_id, second)
    app.state.tasks.unsubscribe_events("other-task", unrelated)
    assert task_id not in app.state.tasks._event_subscribers
    assert "other-task" not in app.state.tasks._event_subscribers


def test_public_history_and_sse_include_thinking_content(service):
    client, _, _, _ = service
    _, headers = create_context(client, "thinking-public")
    created = client.post(
        "/api/v3/tasks/coding",
        headers=headers,
        json={"prompt": "thinking", "deploy_mode": "none"},
    ).json()
    wait_terminal(client, created["task_id"], headers)

    history = client.get(
        f"/api/v3/tasks/{created['task_id']}/events/history",
        headers=headers,
    ).json()["events"]
    reasoning = [event["data"] for event in history if event["type"] == "agent_reasoning"]
    assert reasoning == [
        {
            "response_id": "msg-thinking-api",
            "block_index": 0,
            "finalized": False,
            "thinking": "first ",
        },
        {
            "response_id": "msg-thinking-api",
            "block_index": 0,
            "finalized": True,
            "thinking": "first second",
        },
    ]

    stream = client.get(
        created["events_url"],
        params={"stream_token": created["stream_token"]},
    )
    assert stream.status_code == 200
    assert stream.text.count("event: agent_reasoning") == 2
    assert '"thinking":"first "' in stream.text
    assert '"thinking":"first second"' in stream.text


def test_conversation_messages_keep_thinking_but_redact_sensitive_fields(
    service,
    monkeypatch,
):
    client, app, _, _ = service
    context, headers = create_context(client, "session-thinking")
    workspace = app.state.workspaces.workspace_for(context["context_id"])
    session_id = "00000000-0000-4000-8000-000000000099"
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "provider-secret-value")
    monkeypatch.setattr(
        "aiflow_server.app.list_sessions",
        lambda directory: [SimpleNamespace(session_id=session_id)],
    )
    monkeypatch.setattr(
        "aiflow_server.app.get_session_messages",
        lambda **kwargs: [
            SimpleNamespace(
                type="assistant",
                uuid="assistant-session-message",
                message={
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": (
                                f"inspect {workspace} TOKEN=provider-secret-value "
                                "for device-session-thinking"
                            ),
                            "signature": "provider-signature",
                        }
                    ],
                },
            )
        ],
    )

    response = client.get(
        f"/api/v3/conversations/{session_id}/messages",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    thinking = response.json()["messages"][0]["message"]["content"][0]
    assert thinking == {
        "type": "thinking",
        "thinking": "inspect <workspace> TOKEN=<redacted> for <redacted>",
        "signature": "<redacted>",
    }


def test_sse_endpoints_wait_for_event_signals_instead_of_polling():
    root = Path(__file__).resolve().parents[1]
    for relative in ("aiflow_server/app.py", "aiflow_server/gateway.py"):
        source = root.joinpath(relative).read_text(encoding="utf-8")
        assert "tasks.subscribe_events(task_id)" in source
        assert "await asyncio.wait_for(signal.wait(), timeout=heartbeat_wait)" in source
        assert "asyncio.sleep(0.5)" not in source


def test_native_eventsource_token_and_direct_run_bypass_agent(service):
    client, _, runner, pusher = service
    context, headers = create_context(client, "cc")
    client.post("/api/v3/files", headers=headers, files={"file": ("main.py", b"print('uploaded')\n", "text/plain")})

    created = client.post(
        "/api/v3/tasks/direct-run",
        headers=headers,
        json={"code_path": "main.py", "include_resources": False},
    ).json()
    terminal = wait_terminal(client, created["task_id"], headers)
    assert terminal["status"] == "completed"
    assert runner.calls == []
    assert pusher.deployments == [(context["context_id"], "main.py", False)]

    response = client.get(
        created["events_url"],
        params={"stream_token": created["stream_token"]},
    )
    assert response.status_code == 200
    assert "event: task_completed" in response.text
    assert "event: heartbeat" not in response.text


def test_cancel_file_boundaries_and_conversation_reset(service):
    client, _, runner, _ = service
    _, headers = create_context(client, "dd")
    created = client.post(
        "/api/v3/tasks/coding",
        headers=headers,
        json={"prompt": "slow", "deploy_mode": "none"},
    ).json()
    time.sleep(0.05)
    running = client.get(f"/api/v3/tasks/{created['task_id']}", headers=headers).json()
    assert running["status"] == "running"
    assert running["stage"] == "coding"
    assert running["progress"] == 0
    cancelled = client.post(f"/api/v3/tasks/{created['task_id']}/cancel", headers=headers)
    assert cancelled.status_code == 200
    terminal = wait_terminal(client, created["task_id"], headers)
    assert terminal["status"] == "cancelled"
    assert created["task_id"] in runner.cancelled

    upload = client.post(
        "/api/v3/files",
        headers=headers,
        data={"path": "assets/logo.txt"},
        files={"file": ("logo.txt", b"asset", "text/plain")},
    )
    assert upload.status_code == 201
    assert upload.json()["path"] == "assets/logo.txt"
    assert client.get("/api/v3/files/assets/logo.txt", headers=headers).content == b"asset"
    assert client.post(
        "/api/v3/files",
        headers=headers,
        data={"path": "../escape.txt"},
        files={"file": ("escape.txt", b"bad", "text/plain")},
    ).status_code == 400
    assert client.get("/api/v3/files/.aiflow/config.json", headers=headers).status_code == 400

    reset = client.post("/api/v3/conversation/reset", headers=headers, json={"keep_files": False})
    assert reset.status_code == 200
    assert client.get("/api/v3/files", headers=headers).json() == []


def test_device_update_plan_and_no_global_listing(service):
    client, _, _, _ = service
    _, headers = create_context(client, "ee")
    assert client.get("/api/v3/context").status_code == 401
    assert client.get("/projects").status_code == 404

    update = client.patch(
        "/api/v3/context/device",
        headers=headers,
        json={"firmware_version": "2.3.1"},
    )
    assert update.status_code == 200
    assert update.json()["device"]["device_id"] == "device-ee"
    assert update.json()["device"]["client_id"] == "client-ee"
    assert update.json()["device"]["firmware_version"] == "2.3.1"
    mac_update = client.patch(
        "/api/v3/context/device",
        headers=headers,
        json={"mac": "aa:bb:cc:dd:ee:ff"},
    )
    assert mac_update.status_code == 200, mac_update.text
    assert mac_update.json()["mac_address"] == "aa:bb:cc:dd:ee:ff"
    assert mac_update.json()["device"]["mac_address"] == "aa:bb:cc:dd:ee:ff"
    assert client.patch(
        "/api/v3/context/device",
        headers=headers,
        json={"device_id": "device-other"},
    ).status_code == 422
    assert client.patch(
        "/api/v3/context/device",
        headers=headers,
        json={"client_id": "client-other"},
    ).status_code == 422

    client.post("/api/v3/files", headers=headers, files={"file": ("main.py", b"print(1)\n", "text/plain")})
    plan = client.post(
        "/api/v3/deployments/plan",
        headers=headers,
        json={"code_path": "main.py", "include_resources": True},
    )
    assert plan.status_code == 200
    assert plan.json()["executed"] is False


def test_device_id_reconnect_and_session_capacity(tmp_path):
    base = load_settings()
    settings = replace(
        base,
        data_dir=tmp_path / "data",
        max_sessions=2,
        ai_quota=replace(base.ai_quota, enabled=False),
    )
    runner = FakeRunner()
    app = create_app(settings, runner=runner, pusher=FakePusher())
    with TestClient(app) as client:
        first, first_headers = create_context(client, "capacity-a")
        create_context(client, "capacity-b")

        reconnect = client.post(
            "/api/v3/contexts",
            json={
                "label": "reconnected-device",
                "device": {
                    "device_id": "device-capacity-a",
                    "client_id": "client-capacity-a-new",
                    "macAddress": "AA:BB:CC:DD:EE:01",
                    "product": "CoreS3",
                },
            },
        )
        assert reconnect.status_code == 200, reconnect.text
        reconnected = reconnect.json()
        assert reconnected["created"] is False
        assert reconnected["context_id"] == first["context_id"]
        assert reconnected["client_id"] == "client-capacity-a-new"
        assert reconnected["mac_address"] == "AA:BB:CC:DD:EE:01"
        assert reconnected["device"]["mac_address"] == "AA:BB:CC:DD:EE:01"
        assert client.get("/api/v3/context", headers=first_headers).status_code == 401
        new_headers = {TOKEN_HEADER: reconnected["access_token"]}
        assert client.get("/api/v3/context", headers=new_headers).status_code == 200

        legacy_reconnect = client.post(
            "/api/v3/contexts",
            json={
                "device": {
                    "device_id": "device-capacity-a",
                    "client_id": "client-capacity-a-legacy-reconnect",
                },
            },
        )
        assert legacy_reconnect.status_code == 200, legacy_reconnect.text
        assert legacy_reconnect.json()["mac_address"] == "AA:BB:CC:DD:EE:01"

        full = client.post(
            "/api/v3/contexts",
            json={
                "device": {
                    "device_id": "device-capacity-c",
                    "client_id": "client-capacity-c",
                }
            },
        )
        assert full.status_code == 503
        assert full.json()["detail"]["code"] == "session_capacity_full"
        status_payload = client.get("/api/v3/system/status").json()
        assert status_payload["sessions"] == {
            "limit": 2,
            "used": 2,
            "recently_active": 2,
            "activity_window_seconds": 60,
            "available": 0,
            "accepting_new": False,
        }
        assert status_payload["conversation_logging"] == {
            "enabled": False,
            "pending_records": 0,
            "oldest_created_at": None,
            "max_attempts": 0,
            "worker_running": False,
        }


def test_context_creation_requires_client_id_and_accepts_camel_case_aliases(service):
    client, _, _, _ = service

    connect_schema = client.get("/openapi.json").json()["components"]["schemas"]["ConnectDeviceInfo"]
    assert "client_id" in connect_schema["required"]
    assert "mac_address" not in connect_schema["required"]

    missing = client.post(
        "/api/v3/contexts",
        json={"device": {"device_id": "device-missing-client"}},
    )
    assert missing.status_code == 422

    created = client.post(
        "/api/v3/contexts",
        json={
            "device": {
                "deviceId": "device-camel-case",
                "clientId": "client-camel-case",
                "macAddress": "11:22:33:44:55:66",
            }
        },
    )
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["device_id"] == "device-camel-case"
    assert payload["client_id"] == "client-camel-case"
    assert payload["mac_address"] == "11:22:33:44:55:66"
    assert payload["device"]["client_id"] == "client-camel-case"
    assert payload["device"]["mac_address"] == "11:22:33:44:55:66"

    legacy_alias = client.post(
        "/api/v3/contexts",
        json={
            "device": {
                "device_id": "device-legacy-alias",
                "push_client_id": "client-legacy-alias",
                "mac": "66:55:44:33:22:11",
            }
        },
    )
    assert legacy_alias.status_code == 201, legacy_alias.text
    assert legacy_alias.json()["client_id"] == "client-legacy-alias"
    assert legacy_alias.json()["mac_address"] == "66:55:44:33:22:11"
    assert "push_client_id" not in legacy_alias.json()["device"]

    top_level_alias = client.post(
        "/api/v3/contexts",
        json={
            "macAddress": "22:33:44:55:66:77",
            "device": {
                "device_id": "device-top-level-mac",
                "client_id": "client-top-level-mac",
            },
        },
    )
    assert top_level_alias.status_code == 201, top_level_alias.text
    assert top_level_alias.json()["mac_address"] == "22:33:44:55:66:77"
    assert top_level_alias.json()["device"]["mac_address"] == "22:33:44:55:66:77"


def test_global_concurrency_and_queue_limit(tmp_path):
    base = load_settings()
    settings = replace(
        base,
        data_dir=tmp_path / "data",
        max_sessions=10,
        max_concurrent_tasks=2,
        max_queued_tasks=1,
        ai_quota=replace(base.ai_quota, enabled=False),
    )
    runner = FakeRunner()
    app = create_app(settings, runner=runner, pusher=FakePusher())
    with TestClient(app) as client:
        contexts = [create_context(client, f"queue-{index}") for index in range(4)]
        accepted = []
        for _, headers in contexts[:3]:
            response = client.post(
                "/api/v3/tasks/coding",
                headers=headers,
                json={"prompt": "delay", "deploy_mode": "none"},
            )
            assert response.status_code == 202, response.text
            accepted.append((response.json(), headers))

        deadline = time.monotonic() + 2
        system_status = None
        while time.monotonic() < deadline:
            system_status = client.get("/api/v3/system/status").json()
            if system_status["tasks"]["running"] == 2 and system_status["tasks"]["queued"] == 1:
                break
            time.sleep(0.01)
        assert system_status["tasks"]["running"] == 2
        assert system_status["tasks"]["queued"] == 1
        assert system_status["tasks"]["accepting_new"] is False
        queued_status = client.get(
            f"/api/v3/tasks/{accepted[2][0]['task_id']}",
            headers=accepted[2][1],
        ).json()
        assert queued_status["status"] == "queued"
        assert queued_status["queue_position"] == 1
        assert queued_status["possibly_stalled"] is False

        rejected = client.post(
            "/api/v3/tasks/coding",
            headers=contexts[3][1],
            json={"prompt": "delay", "deploy_mode": "none"},
        )
        assert rejected.status_code == 429
        assert rejected.json()["detail"]["code"] == "task_queue_full"

        for task, headers in accepted:
            assert wait_terminal(client, task["task_id"], headers)["status"] == "completed"
        assert runner.max_active == 2


def test_base64_image_and_audio_use_client_names_without_persisting_payload(service):
    client, app, runner, _ = service
    context, headers = create_context(client, "media")
    image_bytes = b"\x89PNG\r\n\x1a\nmock-image"
    audio_bytes = b"RIFF\x04\x00\x00\x00WAVEmock-audio"
    created = client.post(
        "/api/v3/tasks/coding",
        headers=headers,
        json={
            "prompt": "",
            "deploy_mode": "none",
            "attachments": [
                {
                    "kind": "image",
                    "mime_type": "image/png",
                    "name": "screen.png",
                    "data_base64": base64.b64encode(image_bytes).decode("ascii"),
                },
                {
                    "kind": "audio",
                    "mime_type": "audio/wav",
                    "name": "question.wav",
                    "data_base64": base64.b64encode(audio_bytes).decode("ascii"),
                },
            ],
        },
    )
    assert created.status_code == 202, created.text
    task_id = created.json()["task_id"]
    terminal = wait_terminal(client, task_id, headers)
    assert terminal["status"] == "completed"

    saved = runner.calls[0][4]
    assert [item["kind"] for item in saved] == ["image", "audio"]
    assert [Path(item["path"]).name for item in saved] == ["screen.png", "question.wav"]
    assert [item["name"] for item in saved] == ["screen.png", "question.wav"]
    workspace = app.state.workspaces.workspace_for(context["context_id"])
    assert workspace.joinpath(saved[0]["path"]).read_bytes() == image_bytes
    assert workspace.joinpath(saved[1]["path"]).read_bytes() == audio_bytes
    stored_request = app.state.storage.get_task(task_id)["request"]
    assert all("data_base64" not in item for item in stored_request["attachments"])
    assert [item["sha256"] for item in stored_request["attachments"]] == [
        hashlib.sha256(image_bytes).hexdigest(),
        hashlib.sha256(audio_bytes).hexdigest(),
    ]

    invalid = client.post(
        "/api/v3/tasks/coding",
        headers=headers,
        json={
            "attachments": [
                {
                    "kind": "image",
                    "mime_type": "image/png",
                    "name": "invalid.png",
                    "data_base64": "not-base64",
                }
            ]
        },
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["code"] == "invalid_attachment_base64"


@pytest.mark.parametrize(
    ("name", "mime_type", "expected_code"),
    [
        ("../screen.png", "image/png", "invalid_attachment_name"),
        ("assets/screen.png", "image/png", "invalid_attachment_name"),
        ("screen.jpg", "image/png", "attachment_extension_mismatch"),
    ],
)
def test_attachment_names_reject_paths_and_mime_mismatches(
    service, name, mime_type, expected_code
):
    client, _, _, _ = service
    _, headers = create_context(client, expected_code)
    response = client.post(
        "/api/v3/tasks/coding",
        headers=headers,
        json={
            "attachments": [
                {
                    "kind": "image",
                    "mime_type": mime_type,
                    "name": name,
                    "data_base64": base64.b64encode(b"image").decode("ascii"),
                }
            ]
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == expected_code


def test_attachment_name_is_required_and_unique_per_message(service):
    client, _, _, _ = service
    _, headers = create_context(client, "attachment-name")
    encoded = base64.b64encode(b"image").decode("ascii")

    missing = client.post(
        "/api/v3/tasks/coding",
        headers=headers,
        json={
            "attachments": [
                {
                    "kind": "image",
                    "mime_type": "image/png",
                    "data_base64": encoded,
                }
            ]
        },
    )
    assert missing.status_code == 422
    assert missing.json()["detail"][0]["loc"][-1] == "name"

    duplicate = client.post(
        "/api/v3/tasks/coding",
        headers=headers,
        json={
            "attachments": [
                {
                    "kind": "image",
                    "mime_type": "image/png",
                    "name": "screen.png",
                    "data_base64": encoded,
                },
                {
                    "kind": "image",
                    "mime_type": "image/png",
                    "name": "SCREEN.PNG",
                    "data_base64": encoded,
                },
            ]
        },
    )
    assert duplicate.status_code == 400
    assert duplicate.json()["detail"]["code"] == "duplicate_attachment_name"
